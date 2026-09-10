import os
from contextlib import contextmanager
from pathlib import Path

from .common import ExportError, file_hash, fingerprint, identifier, require, strict_json
from .windows import timestamp
from .transforms import rule_sources


def quote(name):
    return "[" + identifier(name) + "]"


def source_columns(mapping):
    names = []
    for column in mapping["model"]["columns"]:
        if column["source"]:
            names.append(column["source"])
        names.extend(rule_sources(column["rules"]))
    return list(dict.fromkeys(names))


def select_sql(config, mapping):
    names = source_columns(mapping)
    projection = ", ".join(map(quote, names)) if names else "1 AS [__constant]"
    table = quote(config["schema"]) + "." + quote(config["table"])
    date = quote(config["date_column"])
    order = ", ".join(map(quote, config["order_by"]))
    return f"SELECT {projection} FROM {table} WHERE {date} >= ? AND {date} < ?" + (f" ORDER BY {order}" if order else "")


class JsonlSource:
    """Offline fixture adapter; rescans its input per window, never materializes it."""

    def __init__(self, config, mapping):
        self.config, self.mapping = config, mapping
        self.path = Path(config["fixture"])
        self.identity = {"kind": "jsonl", "sha256": file_hash(self.path)}

    def rows(self, start, end):
        required = set(source_columns(self.mapping)) | {self.config["date_column"]}
        with self.path.open("rb") as stream:
            for number, raw in enumerate(iter(lambda: stream.readline(16 * 1024 * 1024 + 1), b""), 1):
                require(len(raw) <= 16 * 1024 * 1024, f"Fixture line {number} exceeds 16 MiB")
                try:
                    line = raw.decode("utf-8")
                except UnicodeError as exc:
                    raise ExportError(f"Fixture line {number} is not valid UTF-8") from exc
                if not line.strip():
                    continue
                try:
                    row = strict_json(line)
                    require(isinstance(row, dict) and required <= set(row), "Missing fixture columns")
                    value = row[self.config["date_column"]]
                    if value is not None and start <= timestamp(value) < end:
                        yield row
                except (ExportError, ValueError, TypeError) as exc:
                    raise ExportError(f"Invalid fixture row {number}; check columns, JSON, and timestamp") from exc

    def close(self):
        pass


class SqlServerSource:
    def __init__(self, config, mapping):
        self.config, self.mapping = config, mapping
        self.connection = None
        try:
            import pyodbc
        except ImportError as exc:
            raise ExportError("Cannot load pyodbc; install the sqlserver extra and the system ODBC manager/SQL Server driver (see README)") from exc
        connection_string = os.environ.get(config["connection_env"])
        require(connection_string, f"Set environment variable {config['connection_env']} to an ODBC connection string")
        try:
            self.connection = pyodbc.connect(connection_string, timeout=config["connect_timeout"], autocommit=True)
            self.connection.timeout = config["query_timeout"]
            cursor = self.connection.cursor()
            try:
                identity = cursor.execute("SELECT CAST(SERVERPROPERTY('ServerName') AS nvarchar(128)), DB_NAME()").fetchone()
                catalog = cursor.execute(
                    "SELECT c.name, TYPE_NAME(c.system_type_id), c.max_length, c.precision, c.scale, c.is_nullable "
                    "FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id "
                    "JOIN sys.schemas s ON s.schema_id = t.schema_id "
                    "WHERE s.name = ? AND t.name = ? ORDER BY c.column_id",
                    config["schema"], config["table"]).fetchall()
                columns = {r[0]: r[1] for r in catalog}
                required = set(source_columns(mapping)) | {config["date_column"]} | set(config["order_by"])
                require(required <= set(columns), "SQL table is missing required columns (or metadata is not visible); column names are case-sensitive in this contract")
                require(columns[config["date_column"]] in {"date", "datetime", "datetime2", "smalldatetime"},
                        "date_column must be date/datetime/datetime2/smalldatetime storing UTC; datetimeoffset is not supported")
                details = {r[0]: r for r in catalog}
                for name in source_columns(mapping):
                    require(columns[name] not in {"datetimeoffset", "sql_variant", "geography", "geometry", "hierarchyid"},
                            "A selected SQL column has an unsupported driver type")
                    require(columns[name] != "datetime2" or details[name][4] <= 6,
                            "Selected datetime2 columns must have scale <= 6 to avoid Python datetime precision loss; provide an approved staging column")
                self.identity = {"kind": "sqlserver", "server": identity[0], "database": identity[1],
                                 "schema_sha256": fingerprint([list(r) for r in catalog])}
                isolation = "SNAPSHOT" if config["isolation"] == "snapshot" else "READ COMMITTED"
                cursor.execute("SET TRANSACTION ISOLATION LEVEL " + isolation)
            finally:
                cursor.close()
            if config["isolation"] == "snapshot":
                self.connection.autocommit = False
        except ExportError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise ExportError("SQL connection or metadata validation failed; check ODBC driver, credentials, permissions, and table configuration") from exc

    def rows(self, start, end):
        cursor = self.connection.cursor()
        cursor.arraysize = self.config["batch_size"]
        try:
            # ODBC parameters are UTC-naive datetime objects. No literals interpolated into SQL.
            cursor.execute(select_sql(self.config, self.mapping), start.replace(tzinfo=None), end.replace(tzinfo=None))
            names = [c[0] for c in cursor.description]
            while True:
                batch = cursor.fetchmany(self.config["batch_size"])
                if not batch:
                    break
                for row in batch:
                    yield dict(zip(names, row))
            if self.config["isolation"] == "snapshot":
                self.connection.commit()
        except Exception as exc:
            if self.config["isolation"] == "snapshot":
                self.connection.rollback()
            raise ExportError("SQL extraction failed; check server availability, permissions, timeout, and supported data types") from exc
        finally:
            cursor.close()

    def close(self):
        if self.connection is not None:
            self.connection.close()


@contextmanager
def open_source(config, mapping):
    source = JsonlSource(config, mapping) if config["source"] == "jsonl" else SqlServerSource(config, mapping)
    try:
        yield source
    finally:
        source.close()
