from copy import deepcopy
from pathlib import Path

import pytest

from sql_exporter.common import ExportError
from sql_exporter.mapping import load_mapping, normalize_model
from sql_exporter.sources import source_columns, select_sql
from sql_exporter.transforms import apply_rules, identity_value, transform, validate_rules
from conftest import ROOT


def test_parent_foreign_keys_match_without_shared_mutable_state():
    a = identity_value({"SalesOrderID": 43659}, "adventureworks/order", ["SalesOrderID"])
    b = identity_value({"ParentOrderID": 43659}, "adventureworks/order", ["ParentOrderID"])
    assert a == b
    assert a != identity_value({"id": 43659}, "adventureworks/customer", ["id"])
    assert a != identity_value({"id": "43659"}, "adventureworks/order", ["id"])


def test_composite_key_framing_and_order():
    values = [identity_value({"a": a, "b": b}, "test", ["a", "b"]) for a, b in [(1, 23), (12, 3), (23, 1), ("a|b", "c"), ("a", "b|c")]]
    assert len(set(values)) == 5


@pytest.mark.parametrize("value", [None, "", 1.5, True])
def test_reject_invalid_identity_key(value):
    with pytest.raises(ExportError):
        identity_value({"id": value}, "test", ["id"])


def test_prefix_suffix_numeric_conversion_and_null():
    rules = validate_rules([{"op": "to_string"}, {"op": "prefix", "value": "AW-"}, {"op": "suffix", "value": "-WEB"}])
    assert apply_rules(42, rules) == "AW-42-WEB"
    assert apply_rules(None, rules) is None
    with pytest.raises(ExportError):
        apply_rules(42, [{"op": "prefix", "value": "AW-"}])


def test_template_uses_only_source_and_preserves_escaped_braces():
    rules = validate_rules([{"op": "template", "value": "{{AW}}-{OrderID}/{LineID}"}, {"op": "suffix", "value": "-LINE"}])
    assert apply_rules(None, rules, {"OrderID": 42, "LineID": 7}) == "{AW}-42/7-LINE"
    with pytest.raises(ExportError):
        apply_rules(None, rules, {"OrderID": 42, "LineID": None})


@pytest.mark.parametrize("rule", [
    {"op": "template", "value": "{id.__class__}"}, {"op": "template", "value": "{id[0]}"},
    {"op": "template", "value": "{id!r}"}, {"op": "template", "value": "{id:04d}"},
    {"op": "template", "value": "{}"}, {"op": "template", "value": "{broken"},
    {"op": "identity", "namespace": "x", "keys": []},
    {"op": "identity", "namespace": "x", "keys": ["id", "id"]},
    {"op": "prefix", "value": 42},
])
def test_bad_generated_rules_rejected(rule):
    with pytest.raises(ExportError):
        validate_rules([rule])


@pytest.mark.parametrize("name", ["orders", "details", "customers", "products"])
def test_workbook_and_json_same_model(name):
    base = ROOT / "examples/adventureworks" / (name + ".mapping")
    assert load_mapping(str(base) + ".xlsx")["model"] == load_mapping(str(base) + ".json")["model"]


def test_generated_columns_add_their_dependencies_to_sql(config):
    mapping = load_mapping(ROOT / "examples/adventureworks/orders.mapping.json")
    assert source_columns(mapping) == ["SalesOrderID", "CustomerID", "OrderDate", "SubTotal", "Comment"]
    sql = select_sql(config | {"date_column": "OrderDate"}, mapping)
    assert "[CustomerID]" in sql and "[order_uid]" not in sql


def test_multiple_ids_and_affixes_from_actual_source_keys():
    mapping = load_mapping(ROOT / "examples/adventureworks/details.mapping.json")
    row = {"SalesOrderID": 43659, "SalesOrderDetailID": 1, "CustomerID": 29825, "ProductID": 776,
           "OrderDate": "2022-05-30", "OrderQty": 1, "UnitPrice": "2024.9940", "UnitPriceDiscount": "0.0000", "LineTotal": "2024.994000", "CarrierTrackingNumber": None}
    values = transform(row, mapping["model"]["columns"])
    assert len(set(values[:4])) == 4
    assert values[6] == "AW-LINE-43659/1-ITEM"
    assert values[-1] == "TRACK-UNTRACKED-AW"
    assert transform(row, mapping["model"]["columns"]) == values


def test_json_mapping_rejects_unknown_fields(mapping):
    model = deepcopy(mapping["model"])
    model["columns"][0]["execute"] = "bad"
    with pytest.raises(ExportError):
        normalize_model(model)
