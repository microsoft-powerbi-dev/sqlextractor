import asyncio
import sys
from types import SimpleNamespace

import pytest

from sql_exporter.common import ExportError
from sql_exporter.control import Control
from sql_exporter.mapping import load_mapping
from sql_exporter.mcp_server import NAMES, Tools, bearer, build_server, safe
from conftest import ROOT

COLUMNS = [("OrderId", "bigint", 8, 19, 0, False), ("CreatedAt", "datetime2", 8, 27, 6, False),
           ("Customer", "nvarchar", 200, 0, 0, True), ("Amount", "decimal", 17, 18, 2, False),
           ("Status", "nvarchar", 2, 0, 0, False), ("Note", "nvarchar", -1, 0, 0, True)]


class Cursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, *params):
        self.log.append((sql, params))
        self.sql = sql
        return self

    def fetchall(self):
        if "sys.objects" in self.sql and "TOP 5000" in self.sql:
            return [("dbo", "Orders", "U "), ("dbo", "OrdersView", "V ")]
        return COLUMNS

    def close(self):
        pass


@pytest.fixture
def tools(tmp_path, monkeypatch):
    log = []
    connection = SimpleNamespace(cursor=lambda: Cursor(log), close=lambda: None)
    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(connect=lambda *a, **kw: connection))
    monkeypatch.setenv("WAREHOUSE_ODBC", "Driver=x;PWD=secret")
    control = Control(tmp_path / "ws", create=True)
    control.save_connection("warehouse", "WAREHOUSE_ODBC")
    (control.root / "uploads").mkdir(exist_ok=True)
    (control.root / "uploads/orders.xsd").write_bytes((ROOT / "examples/targets/orders.xsd").read_bytes())
    (control.root / "uploads/notes.md").write_text("Customer names are upper-cased.")
    result = Tools(control.root)
    result.log = log
    return result


def test_metadata_tools_use_bound_parameters(tools):
    assert tools.list_connections() == [{"name": "warehouse", "connection_env": "WAREHOUSE_ODBC"}]
    assert tools.list_tables("warehouse", "dbo")[1] == {"schema": "dbo", "table": "OrdersView", "kind": "view"}
    columns = tools.describe_table("warehouse", "dbo", "Orders")
    assert columns[3]["suggested"] == {"type": "decimal", "scale": 2} and columns[0]["suggested"]["type"] == "integer"
    assert tools.log[-1][1] == ("dbo", "Orders")
    tools.describe_query("warehouse", "SELECT * FROM dbo.Orders")
    assert "dm_exec_describe_first_result_set" in tools.log[-1][0]
    with pytest.raises(ExportError):
        tools.describe_query("warehouse", "SELECT 1; DROP TABLE dbo.Orders")
    with pytest.raises(ExportError):
        tools.describe_table("warehouse", "dbo", "Orders]; --")


def test_authoring_flow_saves_drafts_only(tools):
    source = tools.capture_source("orders", "warehouse", "CreatedAt", schema="dbo", table="Orders")["source_id"]
    target = tools.import_target_spec("uploads/orders.xsd")["target_id"]
    assert "upper-cased" in tools.read_document("uploads/notes.md")["text"]
    skeleton = tools.suggest_mapping(source, target, "orders")
    assert [c["source"] for c in skeleton["columns"]][:2] == ["OrderId", "CreatedAt"]
    model = load_mapping(ROOT / "examples/mapping.xlsx")["model"]
    assert tools.validate_mapping(model, source, target)["issues"] == []
    saved = tools.save_mapping_draft(model, source, target)
    assert tools.get_mapping(saved["id"])["created_by"] == "mcp" and tools.get_mapping(saved["id"])["status"] == "draft"
    plan = tools.dry_run(saved["id"])
    assert plan["sql"].startswith("SELECT [OrderId]") and "FROM [dbo].[Orders]" in plan["sql"]
    assert not hasattr(tools, "approve") and not hasattr(tools, "export")


def test_paths_are_confined_to_workspace(tools, tmp_path):
    (tmp_path / "outside.md").write_text("secret")
    for path in ("../outside.md", str(tmp_path / "outside.md"), "control.db/../../outside.md"):
        with pytest.raises(ExportError, match="inside the workspace"):
            tools.read_document(path)


def test_safe_hides_unexpected_errors():
    def boom():
        raise RuntimeError("PWD=secret")
    with pytest.raises(ExportError) as caught:
        safe(boom)()
    assert "secret" not in str(caught.value)


def test_bearer_guard():
    calls = []

    async def inner(scope, receive, send):
        calls.append(scope["path"])

    async def run(headers):
        sent = []

        async def send(message):
            sent.append(message)
        await bearer(inner, "t" * 32)({"type": "http", "path": "/mcp", "headers": headers}, None, send)
        return sent

    assert asyncio.run(run([]))[0]["status"] == 401
    assert asyncio.run(run([(b"authorization", b"Bearer wrong")]))[0]["status"] == 401
    assert asyncio.run(run([(b"authorization", ("Bearer " + "t" * 32).encode())])) == [] and calls == ["/mcp"]


def test_server_registers_tools(tmp_path):
    pytest.importorskip("mcp")
    Control(tmp_path, create=True)
    server = build_server(tmp_path)
    assert sorted(t.name for t in asyncio.run(server.list_tools())) == sorted(NAMES)
