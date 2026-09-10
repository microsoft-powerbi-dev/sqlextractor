import json
from pathlib import Path
import sys
import time

import pytest

from sql_exporter.common import ExportError
from sql_exporter.mapping import load_mapping
from sql_exporter.mapping_jobs import cancel_job, document_text, start_job, status_job, validate_response
from conftest import ROOT


def response():
    model = load_mapping(ROOT / "examples/adventureworks/orders.mapping.json")["model"]
    for column in model["columns"]:
        column["rules_json"] = json.dumps(column.pop("rules"))
    return {"mappings": [model], "issues": []}


def wait_for_job(directory):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = status_job(directory)
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.05)
    raise AssertionError("Background test worker did not finish")


def command_file(tmp_path, code):
    path = tmp_path / "command.json"
    path.write_text(json.dumps([sys.executable, "-c", code]))
    return path


def test_actual_detached_external_job_validates_output(tmp_path):
    source = tmp_path / "general.md"
    source.write_text("Test business mapping")
    result_file = tmp_path / "response.json"
    result_file.write_text(json.dumps(response()))
    cmd = command_file(tmp_path, f"import pathlib,sys; assert 'Test business mapping' in sys.stdin.read(); print(pathlib.Path({str(result_file)!r}).read_text())")
    started = start_job(source, tmp_path / "jobs", "external", cmd, timeout=5)
    finished = wait_for_job(started["job_dir"])
    assert finished["status"] == "completed"
    actual = load_mapping(finished["outputs"][0]["path"])
    expected = load_mapping(ROOT / "examples/adventureworks/orders.mapping.json")
    assert actual["fingerprint"] == expected["fingerprint"]
    assert (Path(started["job_dir"]) / "result.json").is_file()


@pytest.mark.parametrize("behavior,status", [("print('not json')", "failed"), ("import time; time.sleep(20)", "timed_out"), ("raise SystemExit(5)", "failed")])
def test_job_failure_modes(tmp_path, behavior, status):
    source = tmp_path / "general.txt"
    source.write_text("test")
    started = start_job(source, tmp_path / "jobs", "external", command_file(tmp_path, behavior), timeout=1)
    finished = wait_for_job(started["job_dir"])
    assert finished["status"] == status
    assert not (Path(started["job_dir"]) / "result.json").exists()


def test_cancel_requests_worker_owned_process(tmp_path):
    source = tmp_path / "general.txt"
    source.write_text("test")
    started = start_job(source, tmp_path / "jobs", "external", command_file(tmp_path, "import time; time.sleep(20)"), timeout=30)
    cancel_job(started["job_dir"])
    assert wait_for_job(started["job_dir"])["status"] == "cancelled"


def test_unsupported_business_rule_is_not_published():
    value = response()
    value["issues"] = [{"severity": "error", "location": "column X", "message": "Underspecified join"}]
    with pytest.raises(ExportError, match="unresolved"):
        validate_response(value)


def test_ai_cannot_bypass_declarative_rule_validation():
    value = response()
    value["mappings"][0]["columns"][0]["rules_json"] = '[{"op":"eval","code":"secret"}]'
    with pytest.raises(ExportError):
        validate_response(value)


def test_general_excel_text_extraction():
    text = document_text(ROOT / "examples/mapping.xlsx")
    assert '"Columns"' in text and '"Customer"' in text


def test_recorded_codex_result_matches_documented_example():
    root = ROOT / "examples/adventureworks"
    actual = load_mapping(root / "normalization-example/aw_orders.json")
    assert actual["fingerprint"] == load_mapping(root / "orders.mapping.json")["fingerprint"]


def test_missing_codex_fails_before_creating_job(tmp_path):
    source = tmp_path / "general.txt"
    source.write_text("test")
    with pytest.raises(ExportError, match="not found"):
        start_job(source, tmp_path / "jobs", codex_bin="definitely-not-installed-normalizer")
    assert not (tmp_path / "jobs").exists()
