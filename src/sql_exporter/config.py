import re
from pathlib import Path

from .common import identifier, require, strict_json
from .windows import resolve_range, timestamp

DEFAULTS = {"config_version": 1, "years": 3, "start": None, "end": None,
            "granularity": "month", "batch_size": 5000, "header": True,
            "null_token": "\\N", "timezone": "UTC", "query_timeout": 300,
            "connect_timeout": 30, "order_by": [], "isolation": "read_committed",
            "delimiter": "|", "line_ending": "\n", "encoding": "utf-8",
            "file_pattern": None, "file_extension": ".txt"}
REQUIRED = {"source", "source_id", "date_column", "mapping", "output_dir"}
OPTIONAL = {"connection_env", "fixture", "source_revision", "schema", "table", "query", "query_file"}
PATTERN_FIELDS = {"mapping_id", "source_id", "start", "end"}


def check_file_pattern(pattern):
    from string import Formatter
    require(isinstance(pattern, str) and 0 < len(pattern) <= 200, "file_pattern must be text up to 200 characters")
    try:
        parts = list(Formatter().parse(pattern))
    except ValueError as exc:
        from .common import ExportError
        raise ExportError("file_pattern has invalid braces") from exc
    for literal, name, spec, conversion in parts:
        require(re.fullmatch(r"[A-Za-z0-9._-]*", literal), "file_pattern literals allow letters, digits, dot, dash, underscore")
        if name is not None:
            require(name in PATTERN_FIELDS and conversion is None, "file_pattern placeholders: {mapping_id}, {source_id}, {start:FORMAT}, {end:FORMAT}")
            # Only fixed-width, locale-free directives so names are portable and sortable.
            require(not spec or (name in {"start", "end"} and re.fullmatch(r"(%[YmdHMSfj]|[A-Za-z0-9_.-])+", spec)),
                    "file_pattern date formats allow %Y %m %d %H %M %S %f %j and literal letters/digits")
    require("{start" in pattern, "file_pattern must include {start} so every window gets its own file")
    return pattern


def validate_query(text):
    """One read-only SELECT, configured by the operator. Never sourced from workbooks or AI output."""
    require(isinstance(text, str) and 0 < len(text.encode("utf-8")) <= 65536, "query must be nonempty text up to 64 KB")
    require(re.match(r"\s*select\b", text, re.I), "query must be a single SELECT statement")
    # Strip quoted literals and bracketed identifiers before looking for statement-level syntax.
    bare = re.sub(r"'(?:[^']|'')*'|\[(?:[^\]]|\]\])*\]|\"(?:[^\"]|\"\")*\"", " ", text)
    require(not re.search(r"['\"\[\]]", bare), "query has unbalanced quotes or brackets")
    require(not any(t in bare for t in (";", "--", "/*", "*/")), "query must not contain ';', comments, or multiple statements")
    banned = r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|deny|into|openrowset|openquery|opendatasource|openxml|waitfor|shutdown|reconfigure|backup|restore|dbcc|bulk|use|declare|set|go)\b|\b(xp|sp)_\w*"
    require(not re.search(banned, bare, re.I), "query may only read data: DML, DDL, EXEC, INTO, variables, and system procedures are rejected; [bracket] identifiers that look like keywords")
    depth, top_order = 0, False
    for token in re.finditer(r"\(|\)|\border\s+by\b", bare, re.I):
        value = token.group(0)
        depth += 1 if value == "(" else -1 if value == ")" else 0
        require(depth >= 0, "query has unbalanced parentheses")
        top_order = top_order or (depth == 0 and value not in "()")
    require(depth == 0, "query has unbalanced parentheses")
    require(not top_order, "query must not end in ORDER BY; use the order_by config field")
    return text.strip()


def load_config(path):
    path = Path(path).resolve()
    raw = strict_json(path.read_text(encoding="utf-8"))
    require(isinstance(raw, dict), "Config must be a JSON object")
    require(REQUIRED <= set(raw), "Config is missing required fields: " + ", ".join(sorted(REQUIRED - set(raw))))
    require(set(raw) <= REQUIRED | OPTIONAL | set(DEFAULTS), "Unknown config field")
    return check_config(DEFAULTS | raw, path.parent)


def check_config(config, base):
    base = Path(base)
    require(type(config["config_version"]) is int and config["config_version"] == 1, "Only config_version 1 is supported")
    require(config["source"] in {"sqlserver", "jsonl"}, "source must be sqlserver or jsonl")
    if "query_file" in config:
        require("query" not in config and isinstance(config["query_file"], str) and config["query_file"].strip(), "Use either query or a nonempty query_file")
        query_path = base / config.pop("query_file")
        require(query_path.is_file() and query_path.stat().st_size <= 65536, "query_file must be a file up to 64 KB")
        config["query"] = query_path.read_text(encoding="utf-8")
    if "query" in config:
        require(config["source"] == "sqlserver", "query is only supported for sqlserver sources")
        require("schema" not in config and "table" not in config, "Use either schema/table or query, not both")
        config["query"] = validate_query(config["query"])
    else:
        require("schema" in config and "table" in config, "Config requires schema and table, or a query")
        identifier(config["schema"])
        identifier(config["table"])
    for key in ("source_id", "date_column"):
        identifier(config[key])
    require(config["timezone"] == "UTC", "Version 1 requires UTC timestamps")
    require(config["granularity"] in {"day", "month", "quarter", "year"}, "granularity must be day, month, quarter, or year")
    require(config["isolation"] in {"read_committed", "snapshot"}, "Unsupported isolation")
    for key, upper in (("years", 100), ("batch_size", 100000), ("query_timeout", 86400), ("connect_timeout", 600)):
        require(type(config[key]) is int and 1 <= config[key] <= upper, f"{key} must be an integer between 1 and {upper}")
    require(type(config["header"]) is bool, "header must be true or false")
    require(isinstance(config["delimiter"], str) and len(config["delimiter"]) == 1 and config["delimiter"] not in '"\r\n',
            "delimiter must be one character other than quote, CR or LF")
    require(config["line_ending"] in {"\n", "\r\n"}, "line_ending must be LF or CRLF")
    require(config["encoding"] in {"utf-8", "utf-8-sig"}, "encoding must be utf-8 or utf-8-sig")
    require(isinstance(config["file_extension"], str) and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", config["file_extension"])
            and config["file_extension"].lower() not in {".json", ".part", ".lock"}, "file_extension must be like .txt or .csv")
    if config["file_pattern"] is not None:
        check_file_pattern(config["file_pattern"])
    require(isinstance(config["null_token"], str) and 1 <= len(config["null_token"]) <= 32
            and not any(c in config["null_token"] for c in '"\r\n' + config["delimiter"]), "null_token must be 1-32 characters without the delimiter, quote, CR or LF")
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
            config[key] = str((base / config[key]).resolve())
    return config


def requested_settings(config):
    # Paths can change when copying an unchanged workbook. Its logical fingerprint is authoritative.
    return {k: v for k, v in config.items() if k not in {"mapping", "output_dir"}}
