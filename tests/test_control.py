import pytest

from sql_exporter import engine
from sql_exporter.common import ExportError
from sql_exporter.config import load_config
from sql_exporter.control import Control
from sql_exporter.mapping import load_mapping
from sql_exporter.sources import JsonlSource
from sql_exporter.targets import import_target
from conftest import ROOT

METADATA = [{"name": n, "type": "nvarchar"} for n in ["OrderId", "CreatedAt", "Customer", "Amount", "Status", "Note"]]


@pytest.fixture
def control(tmp_path):
    return Control(tmp_path / "ws", create=True)


def seed(control):
    source = control.save_source("synthetic_orders", "jsonl", "CreatedAt", schema="dbo", table="Orders",
                                 fixture=str(ROOT / "examples/orders.jsonl"), metadata=METADATA,
                                 settings={"end": "2026-09-01T00:00:00Z", "years": 3, "order_by": ["CreatedAt", "OrderId"],
                                           "source_revision": "fixture_v1"})
    spec = import_target(ROOT / "examples/targets/orders.xsd")["spec"]
    target = control.save_target(spec | {"file": {"pattern": "{mapping_id}_{start:%Y%m}"}})
    return source, target


def test_workspace_requires_init(tmp_path):
    with pytest.raises(ExportError, match="studio init"):
        Control(tmp_path / "missing")


def test_draft_approve_publish_export(control):
    source, target = seed(control)
    model = load_mapping(ROOT / "examples/mapping.xlsx")["model"]
    saved = control.save_mapping(model, source, target, actor="devin")
    assert saved["issues"] == [] and saved["status"] == "draft"
    row = control.mapping(saved["id"])
    assert row["created_by"] == "devin" and row["lineage"][2]["inputs"] == ["Customer"]
    with pytest.raises(ExportError, match="person"):
        control.approve(saved["id"], actor="mcp")
    published = control.approve(saved["id"])
    config = load_config(published["config_path"])
    assert config["file_pattern"] == "{mapping_id}_{start:%Y%m}" and config["source"] == "jsonl"
    mapping = load_mapping(config["mapping"])
    assert mapping["fingerprint"] == saved["fingerprint"]
    result = engine.export(config, mapping, JsonlSource(config, mapping))
    assert result["rows"] == 5 and result["windows_written"] == 36
    with pytest.raises(ExportError, match="immutable"):
        control.save_mapping(model, source, target)
    model["metadata"]["mapping_version"] = "1.1.0"
    assert control.save_mapping(model, source, target)["id"] != saved["id"]
    assert [a["action"] for a in control.audit_log()][:3] == ["save", "publish", "approve"]


def test_review_blocks_approval(control):
    source, target = seed(control)
    model = load_mapping(ROOT / "examples/mapping.xlsx")["model"]
    model["columns"][2]["source"] = "CustomerName"
    saved = control.save_mapping(model, source, target)
    assert saved["issues"] == ["Source column CustomerName is not in the captured source metadata"]
    with pytest.raises(ExportError, match="Resolve review issues"):
        control.approve(saved["id"])


def test_invalid_model_and_secrets_never_stored(control):
    with pytest.raises(ExportError):
        control.save_mapping({"metadata": {}, "columns": []})
    with pytest.raises(ExportError):
        control.save_connection("warehouse", "Driver={ODBC};PWD=x")
    pk = control.save_connection("warehouse", "WAREHOUSE_ODBC")
    query = control.save_source("orders_q", "query", "CreatedAt", connection_id=pk, query="SELECT OrderId, CreatedAt FROM dbo.Orders")
    assert control.source(query)["query"].startswith("SELECT")
    with pytest.raises(ExportError):
        control.save_source("bad_q", "query", "CreatedAt", connection_id=pk, query="SELECT 1; DROP TABLE x")
    assert "PWD" not in (control.root / "control.db").read_bytes().decode("latin-1")
