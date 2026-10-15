import json
from pathlib import Path

import pytest

from sql_exporter import engine
from sql_exporter.common import ExportError
from sql_exporter.config import load_config
from sql_exporter.serialization import record
from sql_exporter.sources import JsonlSource


def test_record_delimiter_and_line_ending():
    assert record(["a,b", None, "c"], "NULL", ",", "\r\n") == b'"a,b",NULL,c\r\n'
    assert record(["a|b"], "\\N", ",") == b"a|b\n"


def test_custom_pattern_delimiter_crlf_bom(config, mapping):
    config = config | {"file_pattern": "{mapping_id}_{start:%Y%m}", "file_extension": ".csv",
                       "delimiter": ",", "line_ending": "\r\n", "encoding": "utf-8-sig"}
    result = engine.export(config, mapping, JsonlSource(config, mapping))
    assert result["rows"] == 5 and result["windows_written"] == 36
    output = Path(config["output_dir"])
    names = sorted(p.name for p in output.glob("*.csv"))
    assert names[0] == "orders_202309.csv" and len(names) == 36
    data = (output / "orders_202309.csv").read_bytes()
    assert data.startswith(b"\xef\xbb\xbforder_id,created_at,customer,") and b"\r\n" in data
    manifest = json.loads((output / "orders_202309.json").read_text())
    assert manifest["bytes"] == len(data)
    result = engine.export(config, mapping, JsonlSource(config, mapping), resume=True)
    assert result["windows_skipped"] == 36


def test_pattern_must_be_unique_per_window(config, mapping):
    with pytest.raises(ExportError, match="unique"):
        engine.make_plan(config | {"file_pattern": "{mapping_id}_{start:%Y}"}, mapping)


@pytest.mark.parametrize("change", [
    {"file_pattern": "{mapping_id}"}, {"file_pattern": "../{start}"}, {"file_pattern": "{start:%B}"},
    {"file_pattern": "{table}_{start}"}, {"file_extension": ".json"}, {"file_extension": "txt"},
    {"delimiter": "||"}, {"delimiter": '"'}, {"line_ending": "\r"}, {"encoding": "latin-1"},
    {"delimiter": ",", "null_token": "a,b"},
])
def test_format_config_validation(config, tmp_path, change):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(config | change))
    with pytest.raises(ExportError):
        load_config(path)
