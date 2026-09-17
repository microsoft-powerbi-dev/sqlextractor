"""Acceptance guard for engine optimization: output bytes must never change.

Goldens were captured from engine 1.1.0 before the compiled-pipeline refactor. They pin the
per-window TXT SHA-256, byte count, row count, and the mapping fingerprint. run.json is
deliberately excluded: it embeds engine_version and package versions, which change legitimately
on a release bump, while the data files must not.
"""
import json
from pathlib import Path

import pytest

from sql_exporter import engine
from sql_exporter.common import file_hash
from sql_exporter.config import load_config
from sql_exporter.mapping import load_mapping
from sql_exporter.sources import JsonlSource
from conftest import ROOT

EXPECTED = json.loads((ROOT / "tests/golden/expected.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,config_path", [("demo", "examples/demo.json"),
                                              ("allops", "tests/golden/allops.json")])
def test_output_bytes_match_golden(tmp_path, name, config_path):
    expected = EXPECTED[name]
    config = load_config(ROOT / config_path)
    config["output_dir"] = str(tmp_path / "export")
    mapping = load_mapping(config["mapping"])
    assert mapping["fingerprint"] == expected["mapping_fingerprint"]

    result = engine.export(config, mapping, JsonlSource(config, mapping))
    assert result["rows"] == expected["rows"]
    assert result["windows_written"] == expected["windows"]

    produced = sorted(p.name for p in Path(config["output_dir"]).glob("*.txt"))
    assert produced == sorted(expected["txt"])
    for filename, golden in expected["txt"].items():
        path = Path(config["output_dir"]) / filename
        manifest = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        assert file_hash(path) == golden["sha256"], f"{filename} bytes changed"
        assert path.stat().st_size == golden["bytes"]
        assert manifest["bytes"] == golden["bytes"] and manifest["rows"] == golden["rows"]
