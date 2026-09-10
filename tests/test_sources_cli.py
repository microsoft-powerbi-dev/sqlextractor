from datetime import datetime
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from sql_exporter.cli import main
from sql_exporter.common import ExportError
from sql_exporter.config import load_config
from sql_exporter.sources import SqlServerSource, select_sql
from sql_exporter.windows import timestamp
from conftest import ROOT


class Cursor:
    description = [("OrderId",)]

    def __init__(self):
        self.calls, self.sizes = [], []
        self.closed = False

    def execute(self, *args):
        self.calls.append(args)
        return self

    def fetchmany(self, size):
        self.sizes.append(size)
        return [(1,), (2,)] if len(self.sizes) == 1 else []

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, cursor):
        self.c = cursor
        self.committed = self.rolled_back = False

    def cursor(self):
        return self.c

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_sql_projection_and_bound_parameters_streaming(config, mapping):
    cursor = Cursor()
    source = SqlServerSource.__new__(SqlServerSource)
    source.config, source.mapping = config | {"batch_size": 2, "isolation": "snapshot"}, mapping
    source.connection = Connection(cursor)
    rows = list(source.rows(timestamp("2023-09-01"), timestamp("2023-10-01")))
    assert rows == [{"OrderId": 1}, {"OrderId": 2}]
    sql, start, end = cursor.calls[0]
    assert "WHERE [CreatedAt] >= ? AND [CreatedAt] < ?" in sql
    assert "SELECT *" not in sql and "2023" not in sql
    assert "ORDER BY [CreatedAt], [OrderId]" in sql
    assert isinstance(start, datetime) and start.tzinfo is None and end.tzinfo is None
    assert cursor.sizes == [2, 2] and cursor.closed and source.connection.committed


def test_sql_failure_closes_cursor_and_rolls_back(config, mapping):
    cursor = Cursor()
    cursor.fetchmany = lambda _: (_ for _ in ()).throw(RuntimeError("credentials"))
    source = SqlServerSource.__new__(SqlServerSource)
    source.config, source.mapping = config | {"isolation": "snapshot"}, mapping
    source.connection = Connection(cursor)
    with pytest.raises(ExportError) as caught:
        list(source.rows(timestamp("2023-09-01"), timestamp("2023-10-01")))
    assert "credentials" not in str(caught.value)
    assert cursor.closed and source.connection.rolled_back


def test_identifier_injection_rejected(config, mapping):
    with pytest.raises(ExportError):
        select_sql(config | {"table": "Orders]; DELETE FROM Orders; --"}, mapping)


def test_cli_validate_dry_run_export_resume(config, tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert main(["validate", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert main(["dry-run", "--config", str(path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["plan"]["windows"]) == 36
    assert not Path(config["output_dir"]).exists()
    assert main(["export", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 5
    assert main(["export", "--config", str(path), "--resume"]) == 0
    assert json.loads(capsys.readouterr().out)["windows_skipped"] == 36


@pytest.mark.parametrize("change", [{"batch_size": 0}, {"header": "false"}, {"unknown": 1}, {"timezone": "America/Chicago"}, {"null_token": "a|b"}, {"start": 4}])
def test_config_validation(config, tmp_path, change):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(config | change))
    with pytest.raises(ExportError):
        load_config(path)


def test_cli_error_code(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{}")
    assert main(["validate", "--config", str(path)]) == 2
    assert "required fields" in capsys.readouterr().err


@pytest.mark.parametrize("precision,missing", [(6, False), (7, False), (6, True)])
def test_sql_metadata_preflight(config, mapping, monkeypatch, precision, missing):
    class MetadataCursor(Cursor):
        def fetchone(self):
            return ("TEST_SERVER", "TEST_DATABASE")

        def fetchall(self):
            if missing:
                return []
            return [("OrderId", "bigint", 8, 19, 0, False),
                    ("CreatedAt", "datetime2", 8, 27, precision, False),
                    ("Customer", "nvarchar", 200, 0, 0, True),
                    ("Amount", "decimal", 17, 38, 3, False),
                    ("Status", "nvarchar", 2, 0, 0, False),
                    ("Note", "nvarchar", -1, 0, 0, True)]

    cursor = MetadataCursor()
    connection = Connection(cursor)
    connection.close = lambda: None
    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(connect=lambda *a, **kw: connection))
    monkeypatch.setenv("TEST_CONNECTION", "secret")
    config = config | {"source": "sqlserver", "connection_env": "TEST_CONNECTION"}
    if precision == 7 or missing:
        with pytest.raises(ExportError, match="scale <= 6" if precision == 7 else "missing required"):
            SqlServerSource(config, mapping)
    else:
        source = SqlServerSource(config, mapping)
        assert source.identity["server"] == "TEST_SERVER"
        assert cursor.calls[1][1:] == ("dbo", "Orders")
        assert connection.timeout == 300
        assert cursor.closed
