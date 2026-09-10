import hashlib
import json
import re
from pathlib import Path


class ExportError(ValueError):
    """Actionable error safe to display without source row values or credentials."""


def require(condition, message):
    if not condition:
        raise ExportError(message)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value),
            "Identifiers must be ASCII letters/digits/underscores, start with a letter or underscore, and have at most 128 characters")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def invalid(_):
        raise ExportError("Non-finite JSON numbers are unsupported")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExportError("Expected valid JSON; free-text rules are not executable") from exc

