from decimal import Decimal

import pytest

from sql_exporter.common import ExportError, strict_json
from sql_exporter.transforms import apply_rules, format_value, transform, validate_rules


def column(kind="string", nullable=False, scale=None):
    return {"type": kind, "nullable": nullable, "scale": scale}


def test_rule_order_nulls_and_empty():
    rules = validate_rules([{"op": "default", "value": "  unknown  "}, {"op": "trim"}, {"op": "upper"}])
    assert apply_rules(None, rules) == "UNKNOWN"
    assert apply_rules("", rules) == ""
    assert apply_rules(None, [{"op": "upper"}]) is None
    assert apply_rules(" aB ", [{"op": "trim"}, {"op": "lower"}, {"op": "replace", "old": "b", "new": "c"}]) == "ac"


@pytest.mark.parametrize("value,expected", [("1.005", "1.01"), ("-1.005", "-1.01"), ("9007199254740993.01", "9007199254740993.01"), ("0", "0.00")])
def test_decimal_exact_rounding(value, expected):
    assert format_value(value, column("decimal", scale=2)) == expected


def test_multiply_exact():
    rules = validate_rules([{"op": "multiply", "factor": "1.20"}])
    assert apply_rules("10.005", rules) == Decimal("12.00600")
    assert format_value(apply_rules("10.005", rules), column("decimal", scale=2)) == "12.01"


@pytest.mark.parametrize("value", [1.1, "NaN", "Infinity", "1e999", "9" * 39, True])
def test_reject_inexact_or_unbounded_decimal(value):
    with pytest.raises(ExportError):
        format_value(value, column("decimal", scale=2))


def test_integer_bounds_and_no_truncation():
    assert format_value("9223372036854775807", column("integer")) == "9223372036854775807"
    for value in ("1.5", "9223372036854775808"):
        with pytest.raises(ExportError):
            format_value(value, column("integer"))


def test_date_boolean_null():
    assert format_value("2024-01-01T01:30:00+02:00", column("date")) == "2023-12-31"
    assert format_value("2024-01-01", column("datetime")) == "2024-01-01T00:00:00.000000Z"
    assert format_value("false", column("boolean")) == "false"
    assert format_value(1, column("boolean")) == "true"
    assert format_value(None, column(nullable=True)) is None
    with pytest.raises(ExportError):
        format_value(None, column())


def test_lookup_requires_explicit_missing_policy():
    rule = {"op": "lookup", "values": {"A": "ACTIVE"}, "on_missing": "error"}
    assert apply_rules("A", [rule]) == "ACTIVE"
    with pytest.raises(ExportError):
        apply_rules("X", [rule])
    assert apply_rules("X", [rule | {"on_missing": "null"}]) is None
    assert apply_rules("X", [rule | {"on_missing": "keep"}]) == "X"


@pytest.mark.parametrize("rules", [[{"op": "eval", "code": "1+1"}], [{"op": "trim", "extra": 1}], [{"op": "multiply", "factor": 1.1}], [{"op": "trim"}, {"op": "constant", "value": "x"}], "make uppercase"])
def test_reject_unsupported_rules(rules):
    with pytest.raises(ExportError):
        validate_rules(rules)


def test_no_values_leak_in_transform_error():
    col = column("integer") | {"target": "id", "source": "id", "rules": []}
    with pytest.raises(ExportError) as caught:
        transform({"id": "SECRET"}, [col])
    assert "SECRET" not in str(caught.value)
    assert "id" in str(caught.value)


@pytest.mark.parametrize("text", ['{"op":"trim","op":"upper"}', '{"value":NaN}', 'uppercase it'])
def test_strict_json(text):
    with pytest.raises(ExportError):
        strict_json(text)

