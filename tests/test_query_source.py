import json
import sys
from types import SimpleNamespace

import pytest

from sql_exporter.common import ExportError
from sql_exporter.config import load_config, validate_query
from sql_exporter.sources import SqlServerSource, select_sql
from test_sources_cli import Connection, Cursor

QUERY = "SELECT o.OrderId, o.CreatedAt, c.Name AS Customer, o.Amount, o.Status, o.Note FROM dbo.Orders o JOIN dbo.Customers c ON c.Id = o.CustomerId"


def query_config(config):
    config = {k: v for k, v in config.items() if k not in {"schema", "table", "fixture"}}
    return config | {"source": "sqlserver", "connection_env": "TEST_CONNECTION", "query": QUERY}


@pytest.mark.parametrize("text", [
    QUERY,
    "select [OrderId], [CreatedAt] from dbo.Orders where Note <> 'drop; --'",
    "SELECT a.x FROM (SELECT x FROM t ORDER BY x OFFSET 0 ROWS) a",
    "SELECT [delete], [Set] FROM dbo.T",
])
def test_query_accepts_read_only_select(text):
    assert validate_query(text) == text.strip()


@pytest.mark.parametrize("text", [
    "", "WITH x AS (SELECT 1 a) SELECT a FROM x", "SELECT 1; DROP TABLE t", "SELECT 1 -- c",
    "SELECT 1 /* c */", "SELECT * INTO t2 FROM t", "SELECT * FROM t ORDER BY a",
    "SELECT * FROM OPENROWSET('x', 'y', 'z')", "SELECT 1 FROM t WHERE x IN (SELECT 1)) ",
    "SELECT name FROM sys.tables; EXEC xp_cmdshell 'dir'", "SELECT 'unterminated FROM t",
    "UPDATE t SET a = 1", "SELECT * FROM t WAITFOR DELAY '00:01'", "SELECT sp_who FROM t",
])
def test_query_rejects_unsafe_or_ambiguous_sql(text):
    with pytest.raises(ExportError):
        validate_query(text)


def test_query_config_rules(config, tmp_path):
    path = tmp_path / "query.json"
    path.write_text(json.dumps(query_config(config)))
    loaded = load_config(path)
    assert loaded["query"] == QUERY and "table" not in loaded
    (tmp_path / "source.sql").write_text(QUERY + "\n")
    path.write_text(json.dumps({k: v for k, v in query_config(config).items() if k != "query"} | {"query_file": "source.sql"}))
    loaded = load_config(path)
    assert loaded["query"] == QUERY and "query_file" not in loaded
    for bad in ({"table": "Orders"}, {"source": "jsonl"}):
        path.write_text(json.dumps(query_config(config) | bad))
        with pytest.raises(ExportError):
            load_config(path)


def test_query_is_wrapped_with_window_filter_outside(config, mapping):
    sql = select_sql(query_config(config), mapping)
    assert sql.startswith("SELECT [OrderId], [CreatedAt], [Customer], [Amount], [Status], [Note] FROM (" + QUERY + ") AS [__source]")
    assert sql.endswith("WHERE [CreatedAt] >= ? AND [CreatedAt] < ? ORDER BY [CreatedAt], [OrderId]")


def test_query_preflight_uses_describe_without_execution(config, mapping, monkeypatch):
    class MetadataCursor(Cursor):
        def fetchone(self):
            return ("TEST_SERVER", "TEST_DATABASE")

        def fetchall(self):
            return [("OrderId", "bigint", 8, 19, 0, False), ("CreatedAt", "datetime2", 8, 27, 6, False),
                    ("Customer", "nvarchar", 200, 0, 0, True), ("Amount", "decimal", 17, 38, 3, False),
                    ("Status", "nvarchar", 2, 0, 0, False), ("Note", "nvarchar", -1, 0, 0, True)]

    cursor = MetadataCursor()
    connection = Connection(cursor)
    connection.close = lambda: None
    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(connect=lambda *a, **kw: connection))
    monkeypatch.setenv("TEST_CONNECTION", "secret")
    source = SqlServerSource(query_config(config), mapping)
    describe_sql, statement = cursor.calls[1]
    assert "dm_exec_describe_first_result_set(?" in describe_sql
    assert statement == "SELECT * FROM (" + QUERY + ") AS [__source]"
    assert len(source.identity["query_sha256"]) == 64
