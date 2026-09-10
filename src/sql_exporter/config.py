from pathlib import Path

from .common import identifier, require, strict_json
from .windows import resolve_range, timestamp

DEFAULTS = {"config_version": 1, "years": 3, "start": None, "end": None,
            "granularity": "month", "batch_size": 5000, "header": True,
            "null_token": "\\N", "timezone": "UTC", "query_timeout": 300,
            "connect_timeout": 30, "order_by": [], "isolation": "read_committed"}
REQUIRED = {"source", "source_id", "schema", "table", "date_column", "mapping", "output_dir"}
OPTIONAL = {"connection_env", "fixture", "source_revision"}


def load_config(path):
    path = Path(path).resolve()
    raw = strict_json(path.read_text(encoding="utf-8"))
    require(isinstance(raw, dict), "Config must be a JSON object")
    require(REQUIRED <= set(raw), "Config is missing required fields: " + ", ".join(sorted(REQUIRED - set(raw))))
    require(set(raw) <= REQUIRED | OPTIONAL | set(DEFAULTS), "Unknown config field")
    config = DEFAULTS | raw
    require(type(config["config_version"]) is int and config["config_version"] == 1, "Only config_version 1 is supported")
    require(config["source"] in {"sqlserver", "jsonl"}, "source must be sqlserver or jsonl")
    for key in ("source_id", "schema", "table", "date_column"):
        identifier(config[key])
    require(config["timezone"] == "UTC", "Version 1 requires UTC timestamps")
    require(config["granularity"] in {"day", "month", "quarter", "year"}, "granularity must be day, month, quarter, or year")
    require(config["isolation"] in {"read_committed", "snapshot"}, "Unsupported isolation")
    for key, upper in (("years", 100), ("batch_size", 100000), ("query_timeout", 86400), ("connect_timeout", 600)):
        require(type(config[key]) is int and 1 <= config[key] <= upper, f"{key} must be an integer between 1 and {upper}")
    require(type(config["header"]) is bool, "header must be true or false")
    require(isinstance(config["null_token"], str) and 1 <= len(config["null_token"]) <= 32
            and not any(c in config["null_token"] for c in '|"\r\n'), "null_token must be 1-32 characters without pipe, quote, CR or LF")
    require(isinstance(config["order_by"], list), "order_by must be an array of column identifiers")
    for name in config["order_by"]:
        identifier(name)
    require(len(set(config["order_by"])) == len(config["order_by"]), "Duplicate order_by columns")
    for key in ("start", "end"):
        require(config[key] is None or isinstance(config[key], str), f"{key} must be an ISO string or null")
        if config[key] is not None:
            timestamp(config[key])
    if config["start"] and config["end"]:
        resolve_range(config["start"], config["end"], config["years"])
    if "source_revision" in config:
        require(isinstance(config["source_revision"], str) and 0 < len(config["source_revision"]) <= 200, "source_revision must be nonempty text up to 200 characters")
    if config["source"] == "sqlserver":
        require("fixture" not in config, "fixture is only valid for jsonl")
        config.setdefault("connection_env", "SQL_EXPORTER_CONNECTION_STRING")
        identifier(config["connection_env"])
    else:
        require("fixture" in config and "connection_env" not in config, "jsonl requires fixture and forbids connection_env")
        require(config["isolation"] == "read_committed", "snapshot is only supported by sqlserver")
    for key in ("mapping", "output_dir", "fixture"):
        if key in config:
            require(isinstance(config[key], str) and config[key].strip(), f"{key} must be a nonempty path")
            config[key] = str((path.parent / config[key]).resolve())
    return config


def requested_settings(config):
    # Paths can change when copying an unchanged workbook. Its logical fingerprint is authoritative.
    return {k: v for k, v in config.items() if k not in {"mapping", "output_dir"}}

