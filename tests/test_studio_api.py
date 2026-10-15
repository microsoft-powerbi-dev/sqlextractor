import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from sql_exporter.control import Control
from sql_exporter.mapping import load_mapping
from sql_exporter.studio.server import make_server
from conftest import ROOT

TOKEN = "studio-test-token-0123456789"


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    Control(tmp_path / "ws", create=True)
    server, token = make_server(tmp_path / "ws", 0, TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(method, path, body=None, raw=None, token=TOKEN, host=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        request = urllib.request.Request(base + path, data=data, method=method)
        if token:
            request.add_header("X-Studio-Token", token)
        if host:
            request.add_header("Host", host)
        try:
            with opener.open(request, timeout=10) as response:
                payload = response.read()
                kind = response.headers.get("Content-Type", "")
                return response.status, json.loads(payload) if "json" in kind else payload
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
    call.root = tmp_path / "ws"
    yield call
    server.shutdown()
    server.server_close()


def test_static_page_and_token_enforcement(studio):
    status, page = studio("GET", "/", token=None)
    assert status == 200 and b"Mapping Studio" in page and b"<script" in page
    assert studio("GET", "/api/state", token=None)[0] == 401
    assert studio("GET", "/api/state", token="wrong")[0] == 401
    assert studio("GET", "/api/state", host="evil.example:80")[0] == 403
    status, state = studio("GET", "/api/state")
    assert status == 200 and "identity" in state["contract"]["operations"]


def test_full_authoring_round_trip(studio):
    status, source = studio("POST", "/api/sources", {
        "name": "synthetic_orders", "kind": "jsonl", "fixture": str(ROOT / "examples/orders.jsonl"), "schema": "dbo", "table": "Orders",
        "date_column": "CreatedAt", "settings": {"end": "2026-09-01T00:00:00Z", "years": 3, "order_by": ["CreatedAt", "OrderId"]}})
    assert status == 200 and [c["name"] for c in source["metadata"]][:2] == ["OrderId", "CreatedAt"]
    status, uploaded = studio("POST", "/api/upload?kind=target&name=orders.xsd", raw=(ROOT / "examples/targets/orders.xsd").read_bytes())
    assert status == 200 and len(uploaded["target"]["spec"]["columns"]) == 7
    target = uploaded["target"]
    spec = target["spec"] | {"file": target["spec"]["file"] | {"pattern": "{mapping_id}_{start:%Y%m}", "delimiter": ","}}
    assert studio("POST", "/api/targets", {"spec": spec})[0] == 200
    status, skeleton = studio("POST", "/api/automap", {"source_id": source["id"], "target_id": target["id"], "mapping_id": "orders"})
    assert [c["source"] for c in skeleton["columns"]][-1] is None

    status, invalid = studio("POST", "/api/validate", {"model": skeleton})
    assert status == 400 and "Columns row 8" in invalid["error"]
    model = load_mapping(ROOT / "examples/mapping.xlsx")["model"]
    status, checked = studio("POST", "/api/validate", {"model": model, "source_id": source["id"], "target_id": target["id"]})
    assert status == 200 and checked["issues"] == []
    status, preview = studio("POST", "/api/preview", {"model": model, "source_id": source["id"]})
    assert status == 200 and "JOSÉ" in [r[2] for r in preview["rows"]] and preview["header"][-1] == "source_system"

    status, saved = studio("POST", "/api/mappings", {"model": model, "source_id": source["id"], "target_id": target["id"]})
    assert status == 200 and saved["status"] == "draft"
    status, dry = studio("POST", f"/api/mappings/{saved['id']}/dry-run", {})
    assert dry["windows"] == 36 and dry["files"][0] == "orders_202309.txt"
    assert studio("POST", f"/api/mappings/{saved['id']}/export", {})[0] == 400
    status, published = studio("POST", f"/api/mappings/{saved['id']}/approve", {})
    assert status == 200 and published["config_path"].endswith("orders-1.0.0.json")
    status, export = studio("POST", f"/api/mappings/{saved['id']}/export", {})
    assert status == 200
    for _ in range(100):
        rows = studio("GET", "/api/exports")[1]
        if rows[0]["status"] != "running":
            break
        time.sleep(0.05)
    assert rows[0]["status"] == "completed" and rows[0]["rows"] == 5 and rows[0]["windows_written"] == 36
    data = (studio.root / "exports/orders-1.0.0/orders_202309.txt").read_text(encoding="utf-8")
    assert data.startswith("order_id,created_at,customer,amount,status,note,source_system\n")
    status, xlsx = studio("GET", f"/api/mappings/{saved['id']}/download.xlsx")
    path = studio.root / "roundtrip.xlsx"
    path.write_bytes(xlsx)
    assert load_mapping(path)["fingerprint"] == saved["fingerprint"]
    status, again = studio("POST", "/api/mappings", {"model": model, "source_id": source["id"], "target_id": target["id"]})
    assert status == 400 and "immutable" in again["error"]


def test_uploads_and_errors_are_value_free(studio):
    status, result = studio("POST", "/api/upload?kind=mapping&name=../../evil.md", raw=b"# not a contract mapping")
    assert status == 400 and "Devin" in result["error"]
    assert not (studio.root.parent / "evil.md").exists()
    status, result = studio("POST", "/api/upload?kind=document&name=../notes.md", raw=b"Customer is upper case")
    assert status == 200 and (studio.root / "uploads" / result["document"]).is_file()
    status, result = studio("POST", "/api/jobs", {"provider": "devin", "document": "../control.db"})
    assert status == 400
    status, result = studio("POST", "/api/describe", {"kind": "table", "connection_id": 99, "schema": "dbo", "table": "x"})
    assert status == 400 and result["error"] == "Unknown connection"
