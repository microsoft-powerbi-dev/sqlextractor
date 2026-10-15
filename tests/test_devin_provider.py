import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sql_exporter.common import ExportError
from sql_exporter.mapping import load_mapping
from sql_exporter.mapping_jobs import cancel_job, start_job, status_job
from sql_exporter.targets import import_target
from conftest import ROOT
from test_mapping_jobs import wait_for_job

KEY = "devin-test-key-7f3a"
METADATA = [{"name": n, "sql_type": "nvarchar"} for n in ["OrderId", "CreatedAt", "Customer", "Amount", "Status", "Note"]]


def proposal(**change):
    model = load_mapping(ROOT / "examples/mapping.xlsx")["model"]
    for column in model["columns"]:
        column["rules_json"] = json.dumps(column.pop("rules"))
    model["columns"][2].update(change)
    return {"mappings": [model], "issues": []}


class FakeDevin:
    """Local stand-in for the Devin v1 sessions API; records every call."""

    def __init__(self, script):
        self.script, self.calls, self.prompts = list(script), [], []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value, code=200):
                data = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                fake.calls.append(("POST", self.path, self.headers.get("Authorization")))
                fake.prompts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["prompt"])
                self.reply({"session_id": "devin-abc", "url": "https://app.devin.ai/sessions/abc"})

            def do_GET(self):
                fake.calls.append(("GET", self.path, self.headers.get("Authorization")))
                self.reply(fake.script.pop(0) if len(fake.script) > 1 else fake.script[0])

            def do_DELETE(self):
                fake.calls.append(("DELETE", self.path, self.headers.get("Authorization")))
                self.reply({})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def devin(monkeypatch):
    servers = []

    def make(script):
        server = FakeDevin(script)
        servers.append(server)
        monkeypatch.setenv("DEVIN_API_BASE", server.url)
        monkeypatch.setenv("DEVIN_API_KEY", KEY)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        return server
    yield make
    for server in servers:
        server.close()


def start(tmp_path, **kwargs):
    document = tmp_path / "business.md"
    document.write_text("Orders feed: customer name upper-cased, default UNKNOWN; status codes A/C/P.")
    target = import_target(ROOT / "examples/targets/orders.xsd")["spec"]
    options = {"source_metadata": METADATA, "target_spec": target, "timeout": 10, "poll_interval": 0} | kwargs
    return start_job(document, tmp_path / "jobs", "devin", **options)


def test_devin_session_proposal_is_validated(tmp_path, devin):
    fake = devin([{"status_enum": "working", "structured_output": None},
                  {"status_enum": "finished", "structured_output": proposal()}])
    started = start(tmp_path)
    finished = wait_for_job(started["job_dir"])
    assert finished["status"] == "completed", finished
    assert finished["session_url"] == "https://app.devin.ai/sessions/abc"
    assert load_mapping(finished["outputs"][0]["path"])["fingerprint"] == load_mapping(ROOT / "examples/mapping.xlsx")["fingerprint"]
    assert all(auth == "Bearer " + KEY for _, _, auth in fake.calls)
    assert "OrderId" in fake.prompts[0] and "source_system" in fake.prompts[0] and "structured output" in fake.prompts[0]
    for path in Path(started["job_dir"]).rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(encoding="utf-8", errors="ignore")


def test_devin_proposal_must_fit_source_and_target(tmp_path, devin):
    devin([{"status_enum": "blocked", "structured_output": proposal(source="CustomerName")}])
    finished = wait_for_job(start(tmp_path)["job_dir"])
    assert finished["status"] == "failed" and "Unknown source column CustomerName" in finished["error"]


def test_devin_expired_and_timeout_terminate(tmp_path, devin):
    fake = devin([{"status_enum": "expired", "structured_output": None}])
    assert wait_for_job(start(tmp_path)["job_dir"])["status"] == "failed"
    fake = devin([{"status_enum": "working", "structured_output": None}])
    started = start(tmp_path, timeout=1)
    deadline_status = wait_for_job(started["job_dir"])
    assert deadline_status["status"] == "timed_out"
    assert ("DELETE", "/v1/sessions/devin-abc", "Bearer " + KEY) in fake.calls


def test_devin_cancel(tmp_path, devin):
    import time
    fake = devin([{"status_enum": "working", "structured_output": None}])
    started = start(tmp_path, timeout=30, poll_interval=0)
    for _ in range(200):
        if status_job(started["job_dir"]).get("session_id"):
            break
        time.sleep(0.05)
    cancel_job(started["job_dir"])
    assert wait_for_job(started["job_dir"])["status"] == "cancelled"
    assert any(call[0] == "DELETE" for call in fake.calls)


def test_devin_requires_key_and_inputs(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    with pytest.raises(ExportError, match="DEVIN_API_KEY"):
        start(tmp_path)
    monkeypatch.setenv("DEVIN_API_KEY", KEY)
    with pytest.raises(ExportError, match="document"):
        start_job(None, tmp_path / "jobs", "devin", source_metadata=METADATA)
