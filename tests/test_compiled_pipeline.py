"""Guards for the compile-once pipeline: positional and named sources must agree, and the
compiled program must match the interpreter shim on a broad op/type corpus."""
import json
from decimal import Context, Decimal, localcontext, ROUND_HALF_UP
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from sql_exporter import engine
from sql_exporter.common import canonical, file_hash
from sql_exporter.config import load_config
from sql_exporter.mapping import load_mapping
from sql_exporter.serialization import record
from sql_exporter.sources import JsonlSource, source_columns
from sql_exporter.transforms import (DECIMAL, ExportError, compile_columns, decimal,
                                     format_value, transform)
from conftest import ROOT


def allops():
    config = load_config(ROOT / "tests/golden/allops.json")
    return config, load_mapping(config["mapping"])


class Positional:
    """Stands in for a driver that yields sequences instead of mappings."""

    identity = {"kind": "positional_test"}

    def __init__(self, config, mapping):
        self.layout = source_columns(mapping)
        self.inner = JsonlSource(config, mapping)

    def rows(self, start, end):
        for row in self.inner.rows(start, end):
            yield tuple(row[name] for name in self.layout)

    def close(self):
        self.inner.close()


def test_positional_source_matches_named_source(tmp_path):
    """A sequence-yielding source must produce byte-identical output to a dict source."""
    outputs = []
    for index, factory in enumerate((JsonlSource, Positional)):
        config, mapping = allops()
        config["output_dir"] = str(tmp_path / f"run{index}")
        engine.export(config, mapping, factory(config, mapping))
        outputs.append({p.name: file_hash(p) for p in sorted(Path(config["output_dir"]).glob("*.txt"))})
    assert outputs[0] == outputs[1] and outputs[0]


def test_positional_index_zero_is_not_treated_as_absent():
    """The first projected column has index 0, which is falsy; it must still be read."""
    columns = [{"ordinal": 1, "source": "First", "target": "t", "type": "string",
                "nullable": False, "rules": [], "scale": None}]
    compiled = compile_columns(columns, {"First": 0})
    assert [run(("value-at-zero",)) for run in compiled] == ["value-at-zero"]


def test_missing_positional_column_keeps_the_error_contract():
    """Positional access raises IndexError where dict access raises KeyError; both must be
    converted to an ExportError that names the target and leaks no value."""
    columns = [{"ordinal": 1, "source": "Missing", "target": "secret_target", "type": "string",
                "nullable": False, "rules": [], "scale": None}]
    for keys, row in (({"Missing": 5}, ("only-one",)), (None, {"Other": "SECRET"})):
        with pytest.raises(ExportError) as caught:
            [run(row) for run in compile_columns(columns, keys)]
        assert "secret_target" in str(caught.value)
        assert "SECRET" not in str(caught.value) and "only-one" not in str(caught.value)


def test_compiled_matches_shim_over_the_allops_corpus():
    """Differential test: compiled program vs the transform() shim, over every op and type."""
    config, mapping = allops()
    columns = mapping["model"]["columns"]
    compiled = compile_columns(columns)
    rows = [json.loads(line) for line in
            (ROOT / "tests/golden/allops.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 10
    for row in rows:
        assert [run(row) for run in compiled] == transform(row, columns)
        assert record([run(row) for run in compiled]) == record(transform(row, columns))


DECIMALS = ["1.005", "-1.005", "2.675", "-2.5", "0", "-0", "0.5", "1.5", "9007199254740993.01",
            "1E+10", "1E-10", "-1E-3", "0.000005", "99999999999999999999999999999999999.99"]


@pytest.mark.parametrize("value", DECIMALS)
@pytest.mark.parametrize("scale", [0, 2, 6, 18])
def test_pinned_context_matches_localcontext(value, scale):
    """The module Context(prec=80) must behave exactly like the previous per-value localcontext."""
    quantum = Decimal(1).scaleb(-scale)
    try:
        with localcontext() as context:
            context.prec = 80
            expected, error = Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP), None
    except Exception as exc:
        expected, error = None, type(exc)
    try:
        actual = Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP, context=DECIMAL)
    except Exception as exc:
        assert type(exc) is error
    else:
        assert error is None and format(actual, "f") == format(expected, "f")


@pytest.mark.parametrize("factor", ["1.20", "0.001", "-3.5", "1000000"])
def test_pinned_context_multiply_matches_localcontext(factor):
    for value in DECIMALS:
        with localcontext() as context:
            context.prec = 80
            expected = decimal(value) * decimal(factor)
        assert str(DECIMAL.multiply(decimal(value), decimal(factor))) == str(expected)


def test_namespace_cache_matches_nested_uuid5():
    from sql_exporter.transforms import namespace_uuid
    for namespace in ["a", "adventureworks/order", "x" * 200, "José/東京", "\U0001D11E"]:
        payload = canonical([["integer", 43659]])
        assert str(uuid5(namespace_uuid(namespace), payload)) == \
               str(uuid5(uuid5(NAMESPACE_URL, namespace), payload))


def test_compiled_columns_reject_unknown_op():
    """The interpreter silently passed unknown ops through; the compiler must refuse them."""
    columns = [{"ordinal": 1, "source": "a", "target": "t", "type": "string",
                "nullable": False, "rules": [{"op": "exfiltrate", "value": "x"}], "scale": None}]
    with pytest.raises(ExportError, match="Unsupported transformation"):
        compile_columns(columns)


def test_format_value_shim_still_accepts_ad_hoc_columns():
    assert format_value("1.005", {"type": "decimal", "nullable": False, "scale": 2}) == "1.01"
    assert format_value(None, {"type": "string", "nullable": True}) is None
    with pytest.raises(ExportError, match="NULL is forbidden"):
        format_value(None, {"type": "string", "nullable": False})
