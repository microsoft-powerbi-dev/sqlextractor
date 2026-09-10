import csv
import tracemalloc
from pathlib import Path

import pytest
from filelock import FileLock

from sql_exporter import engine
from sql_exporter.common import ExportError, fingerprint
from sql_exporter.sources import JsonlSource


def data_rows(output):
    result = []
    for path in sorted(Path(output).glob("*.txt")):
        with path.open(encoding="utf-8", newline="") as stream:
            result.extend(csv.DictReader(stream, delimiter="|"))
    return result


def test_offline_export_boundaries_and_resume(config, mapping):
    result = engine.export(config, mapping, JsonlSource(config, mapping))
    assert result["windows_written"] == 36 and result["rows"] == 5
    rows = data_rows(config["output_dir"])
    assert [r["order_id"] for r in rows] == ["1", "2", "3", "4", "5"]
    assert [r["amount"] for r in rows] == ["10.01", "-1.01", "0.00", "9007199254740993.01", "12.50"]
    assert rows[0]["customer"] == "JOSÉ" and rows[-1]["customer"] == "UNKNOWN"
    assert rows[2]["note"] == "line one\nline two"
    paths = list(Path(config["output_dir"]).glob("*.txt"))
    mtimes = [p.stat().st_mtime_ns for p in paths]
    result = engine.export(config, mapping, JsonlSource(config, mapping), resume=True)
    assert result["windows_written"] == 0 and result["windows_skipped"] == 36
    assert [p.stat().st_mtime_ns for p in paths] == mtimes


def test_failure_leaves_partial_and_resume_restarts_window(config, mapping):
    real = JsonlSource(config, mapping)

    class Failing:
        identity = real.identity

        def rows(self, start, end):
            for row in real.rows(start, end):
                yield row
                raise RuntimeError("sensitive source data")

    with pytest.raises(ExportError) as caught:
        engine.export(config, mapping, Failing())
    assert "sensitive source data" not in str(caught.value)
    output = Path(config["output_dir"])
    assert len(list(output.glob("*.txt.part"))) == 1
    assert not list(output.glob("*.txt"))
    assert "sensitive" not in (output / "failure.json").read_text()
    result = engine.export(config, mapping, real, resume=True)
    assert result["rows"] == 5
    assert not list(output.glob("*.part")) and not (output / "failure.json").exists()


def test_crash_after_data_rename_before_manifest(config, mapping, monkeypatch):
    original = engine.atomic_json

    def fail(path, value):
        if value.get("status") == "complete":
            raise KeyboardInterrupt
        return original(path, value)

    monkeypatch.setattr(engine, "atomic_json", fail)
    with pytest.raises(KeyboardInterrupt):
        engine.export(config, mapping, JsonlSource(config, mapping))
    assert len(list(Path(config["output_dir"]).glob("*.txt"))) == 1
    monkeypatch.setattr(engine, "atomic_json", original)
    assert engine.export(config, mapping, JsonlSource(config, mapping), resume=True)["rows"] == 5


@pytest.mark.parametrize("change", [{"header": False}, {"batch_size": 12}, {"granularity": "quarter"}, {"end": "2026-08-01"}, {"null_token": "NULL"}, {"source_revision": "v2"}])
def test_resume_rejects_settings_changes(config, mapping, change):
    engine.export(config, mapping, JsonlSource(config, mapping))
    with pytest.raises(ExportError, match="Resume refused"):
        engine.export(config | change, mapping, JsonlSource(config, mapping), resume=True)


def test_resume_rejects_mapping_and_source_changes(config, mapping):
    engine.export(config, mapping, JsonlSource(config, mapping))
    with pytest.raises(ExportError, match="Resume refused"):
        engine.export(config, mapping | {"fingerprint": "changed"}, JsonlSource(config, mapping), resume=True)
    source = JsonlSource(config, mapping)
    source.identity = source.identity | {"sha256": "changed"}
    with pytest.raises(ExportError, match="Resume refused"):
        engine.export(config, mapping, source, resume=True)


def test_resume_checks_all_files_before_writing(config, mapping):
    engine.export(config, mapping, JsonlSource(config, mapping))
    output = Path(config["output_dir"])
    paths = sorted(output.glob("*.txt"))
    paths[0].with_suffix(".json").unlink()
    paths[0].unlink()
    paths[-1].write_text("tampered")
    with pytest.raises(ExportError, match="integrity"):
        engine.export(config, mapping, JsonlSource(config, mapping), resume=True)
    assert not paths[0].exists()


def test_lock_and_no_accidental_overwrite(config, mapping):
    output = Path(config["output_dir"])
    output.mkdir()
    with FileLock(str(output / ".export.lock")):
        with pytest.raises(ExportError, match="lock"):
            engine.export(config, mapping, JsonlSource(config, mapping))
    (output / "important.txt").write_text("keep")
    with pytest.raises(ExportError, match="not empty"):
        engine.export(config, mapping, JsonlSource(config, mapping))
    assert (output / "important.txt").read_text() == "keep"


def test_implicit_now_is_frozen_on_resume(config, mapping):
    config["end"] = None
    engine.export(config, mapping, JsonlSource(config, mapping))
    run = Path(config["output_dir"]) / "run.json"
    original = run.read_bytes()
    result = engine.export(config, mapping, JsonlSource(config, mapping), resume=True)
    assert result["windows_written"] == 0 and run.read_bytes() == original


def test_run_tampering_rejected(config, mapping):
    engine.export(config, mapping, JsonlSource(config, mapping))
    path = Path(config["output_dir"]) / "run.json"
    run = engine.read_manifest(path)
    run["plan"]["end"] = "2027-01-01T00:00:00Z"
    engine.atomic_json(path, run)
    with pytest.raises(ExportError, match="fingerprint"):
        engine.export(config, mapping, JsonlSource(config, mapping), resume=True)


def test_large_window_does_not_accumulate_rows(config, mapping):
    config = config | {"start": "2023-09-01", "end": "2023-10-01"}

    class Generated:
        identity = {"kind": "generated_test"}

        def rows(self, start, end):
            for index in range(20000):
                yield {"OrderId": index, "CreatedAt": "2023-09-01", "Customer": "test", "Amount": "1.00",
                       "Status": "A", "Note": str(index) + "x" * 1024}

    tracemalloc.start()
    try:
        result = engine.export(config, mapping, Generated())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["rows"] == 20000
    assert sum(p.stat().st_size for p in Path(config["output_dir"]).glob("*.txt")) > 20_000_000
    assert peak < 5 * 1024 * 1024


def test_formatting_only_workbook_change_can_resume(config, mapping):
    engine.export(config, mapping, JsonlSource(config, mapping))
    assert engine.export(config, mapping | {"workbook_sha256": "new styling"}, JsonlSource(config, mapping), resume=True)["windows_written"] == 0
