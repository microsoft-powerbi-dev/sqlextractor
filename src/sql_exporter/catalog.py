"""Read-only source metadata for authoring. Fixed, parameterized catalog SQL; never reads rows."""
from pathlib import Path

from .common import ExportError, identifier, require, strict_json
from .config import validate_query
from .sources import QUERY_COLUMNS, TABLE_COLUMNS, connect

INTEGERS = {"bigint", "int", "smallint", "tinyint"}
DECIMALS = {"decimal", "numeric", "money", "smallmoney"}
DATETIMES = {"datetime", "datetime2", "smalldatetime"}
UNSUPPORTED = {"datetimeoffset", "sql_variant", "geography", "geometry", "hierarchyid"}
LIST_TABLES = ("SELECT TOP 5000 s.name, o.name, o.type FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
               "WHERE o.type IN ('U', 'V') AND (? IS NULL OR s.name = ?) ORDER BY s.name, o.name")


def suggest(sql_type, scale):
    """Mapping type a column most naturally exports as; reviewers may still choose string."""
    if sql_type in INTEGERS:
        return {"type": "integer", "scale": None}
    if sql_type in DECIMALS:
        return {"type": "decimal", "scale": min(int(scale or 0), 18) if sql_type in {"decimal", "numeric"} else 4}
    if sql_type == "date":
        return {"type": "date", "scale": None}
    if sql_type in DATETIMES:
        return {"type": "datetime", "scale": None}
    if sql_type == "bit":
        return {"type": "boolean", "scale": None}
    return {"type": "string", "scale": None}


def columns(rows):
    result = []
    for name, sql_type, max_length, precision, scale, nullable in rows:
        result.append({"name": name, "sql_type": sql_type, "max_length": max_length, "precision": precision, "scale": scale,
                       "nullable": bool(nullable), "supported": sql_type not in UNSUPPORTED} | {"suggested": suggest(sql_type, scale)})
    return result


def _run(connection_env, action):
    connection = None
    try:
        connection = connect(connection_env)
        cursor = connection.cursor()
        try:
            return action(cursor)
        finally:
            cursor.close()
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError("SQL metadata request failed; check ODBC driver, credentials, permissions, and object names") from exc
    finally:
        if connection is not None:
            connection.close()


def list_tables(connection_env, schema=None):
    if schema is not None:
        identifier(schema)
    rows = _run(connection_env, lambda c: c.execute(LIST_TABLES, schema, schema).fetchall())
    return [{"schema": s, "table": t, "kind": "view" if k.strip() == "V" else "table"} for s, t, k in rows]


def describe_table(connection_env, schema, table):
    identifier(schema)
    identifier(table)
    rows = _run(connection_env, lambda c: c.execute(TABLE_COLUMNS, schema, table).fetchall())
    require(rows, "Table or view not found, or its metadata is not visible to this login")
    return columns(rows)


def describe_query(connection_env, query):
    query = validate_query(query)
    rows = _run(connection_env, lambda c: c.execute(QUERY_COLUMNS, "SELECT * FROM (" + query + ") AS [__source]").fetchall())
    require(rows and all(r[0] for r in rows), "Query has no describable result set, or a column needs an alias")
    require(len({r[0] for r in rows}) == len(rows), "Query returns duplicate column names; alias them")
    return columns(rows)


def describe_fixture(path):
    """Offline JSONL fixtures: names from the first object; types are suggestions only."""
    with Path(path).open("rb") as stream:
        line = stream.readline(16 * 1024 * 1024).decode("utf-8")
    row = strict_json(line)
    require(isinstance(row, dict) and row, "Fixture first line must be a JSON object")
    result = []
    for name, value in row.items():
        identifier(name)
        kind = "integer" if type(value) is int else "boolean" if type(value) is bool else "string"
        result.append({"name": name, "sql_type": "json", "max_length": None, "precision": None, "scale": None,
                       "nullable": True, "supported": True, "suggested": {"type": kind, "scale": None}})
    return result
