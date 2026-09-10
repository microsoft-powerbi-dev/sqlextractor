from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from string import Formatter
from uuid import NAMESPACE_URL, uuid5

from .common import ExportError, canonical, identifier, require
from .windows import iso, timestamp

TYPES = {"string", "integer", "decimal", "date", "datetime", "boolean"}
FIELDS = {
    "trim": set(), "upper": set(), "lower": set(),
    "replace": {"old", "new"}, "default": {"value"}, "constant": {"value"},
    "multiply": {"factor"}, "lookup": {"values", "on_missing"},
    "prefix": {"value"}, "suffix": {"value"}, "to_string": set(),
    "identity": {"namespace", "keys"}, "template": {"value"},
}


def template_fields(text):
    require(isinstance(text, str) and len(text) <= 4096, "template requires text up to 4096 characters")
    try:
        parts = list(Formatter().parse(text))
    except ValueError as exc:
        raise ExportError("Invalid template braces") from exc
    fields = []
    for _, name, spec, conversion in parts:
        if name is not None:
            identifier(name)
            require(not spec and conversion is None, "Templates allow only plain source column placeholders")
            fields.append(name)
    return fields


def rule_sources(rules):
    result = []
    for rule in rules:
        if rule["op"] == "identity":
            result.extend(rule["keys"])
        elif rule["op"] == "template":
            result.extend(template_fields(rule["value"]))
    return result


def identity_value(row, namespace, keys):
    # Include type tags and JSON framing so (1, 23) cannot collide with (12, 3),
    # and numeric 1 intentionally differs from text "1". Column names are not
    # included: the same logical foreign key may have a different source name.
    values = []
    for key in keys:
        value = row[key]
        require(type(value) in {str, int} and value != "", "identity keys must be nonempty text or integers; nulls/floats are forbidden")
        values.append(["integer" if type(value) is int else "string", value])
    return str(uuid5(uuid5(NAMESPACE_URL, namespace), canonical(values)))


def scalar(value):
    return value is None or type(value) in {str, int, float, bool}


def decimal(value):
    require(not isinstance(value, (bool, float)), "Decimal conversion requires exact integer, decimal, or text; floats are rejected")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExportError("Invalid decimal") from exc
    require(result.is_finite() and len(result.as_tuple().digits) <= 38 and abs(result.adjusted()) <= 38,
            "Decimal must be finite with at most 38 significant digits and magnitude exponent at most 38")
    return result


def validate_rules(rules):
    require(isinstance(rules, list) and len(rules) <= 32, "Rules must be a JSON array with at most 32 operations")
    for index, rule in enumerate(rules):
        require(isinstance(rule, dict) and isinstance(rule.get("op"), str) and rule["op"] in FIELDS,
                "Unsupported transformation; use a documented declarative op")
        op = rule["op"]
        require(set(rule) == FIELDS[op] | {"op"}, f"Incorrect parameters for {op}")
        if op in {"constant", "identity", "template"}:
            require(index == 0, f"{op} must be the first operation")
        if op in {"prefix", "suffix"}:
            require(isinstance(rule["value"], str) and len(rule["value"]) <= 4096, f"{op} requires text up to 4096 characters")
        if op == "template":
            template_fields(rule["value"])
        if op == "identity":
            require(isinstance(rule["namespace"], str) and 1 <= len(rule["namespace"]) <= 200, "identity requires a stable namespace of 1-200 characters")
            require(isinstance(rule["keys"], list) and 1 <= len(rule["keys"]) <= 16, "identity requires 1-16 source keys")
            for key in rule["keys"]:
                identifier(key)
            require(len(set(rule["keys"])) == len(rule["keys"]), "Duplicate identity keys")
        if op in {"constant", "default"}:
            require(scalar(rule["value"]), "Rule value must be a JSON scalar")
        if op == "multiply":
            decimal(rule["factor"])
        if op == "replace":
            require(isinstance(rule["old"], str) and isinstance(rule["new"], str), "replace requires text old/new")
        if op == "lookup":
            require(isinstance(rule["values"], dict) and all(scalar(v) for v in rule["values"].values()),
                    "lookup values must map strings to JSON scalars")
            require(rule["on_missing"] in ("error", "keep", "null"), "lookup on_missing must be error, keep, or null")
    return rules


def apply_rules(value, rules, row=None):
    for rule in rules:
        op = rule["op"]
        if op == "constant":
            value = rule["value"]
        elif op == "identity":
            require(row is not None, "identity requires source row context")
            value = identity_value(row, rule["namespace"], rule["keys"])
        elif op == "template":
            require(row is not None, "template requires source row context")
            values = {key: format_value(row[key], {"type": "string", "nullable": False})
                      for key in template_fields(rule["value"])}
            value = rule["value"].format_map(values)
        elif op == "default":
            if value is None:
                value = rule["value"]
        elif value is None:
            continue
        elif op == "to_string":
            value = format_value(value, {"type": "string", "nullable": False})
        elif op in {"prefix", "suffix"}:
            require(isinstance(value, str), f"{op} requires a string; use to_string first for numeric input")
            value = rule["value"] + value if op == "prefix" else value + rule["value"]
        elif op in {"trim", "upper", "lower", "replace", "lookup"}:
            require(isinstance(value, str), f"{op} requires a string input")
            if op == "replace":
                value = value.replace(rule["old"], rule["new"])
            elif op == "lookup":
                if value in rule["values"]:
                    value = rule["values"][value]
                else:
                    require(rule["on_missing"] != "error", "lookup key is unmapped")
                    value = None if rule["on_missing"] == "null" else value
            else:
                value = getattr(value, "strip" if op == "trim" else op)()
        elif op == "multiply":
            with localcontext() as context:
                context.prec = 80
                value = decimal(value) * decimal(rule["factor"])
    return value


def format_value(value, column):
    if value is None:
        require(column["nullable"], "NULL is forbidden for this target")
        return None
    kind = column["type"]
    if kind == "string":
        require(isinstance(value, (str, int, Decimal, bool, date)), "Unsupported value for string conversion")
        if isinstance(value, datetime):
            return iso(timestamp(value))
        if isinstance(value, date):
            return value.isoformat()
        return str(value)
    if kind == "integer":
        number = decimal(value)
        require(number == number.to_integral_value() and -(2**63) <= number < 2**63, "Expected an exact signed 64-bit integer")
        return str(int(number))
    if kind == "decimal":
        with localcontext() as context:
            context.prec = 80
            number = decimal(value).quantize(Decimal(1).scaleb(-column["scale"]), rounding=ROUND_HALF_UP)
        require(len(number.as_tuple().digits) <= 38, "Rounded decimal exceeds 38 digits")
        return format(number, "f")
    if kind == "boolean":
        require(type(value) in {str, int, bool} and value in (True, False, 0, 1, "true", "false", "0", "1"),
                "Boolean accepts only true/false or 0/1")
        return "true" if value in (True, 1, "true", "1") else "false"
    if kind == "date":
        return timestamp(value).date().isoformat()
    return iso(timestamp(value))


def transform(row, columns):
    result = []
    for column in columns:
        try:
            value = row[column["source"]] if column["source"] else None
            result.append(format_value(apply_rules(value, column["rules"], row), column))
        except (ExportError, KeyError, ValueError, TypeError, ArithmeticError) as exc:
            # Do not include source data in exceptions or failure manifests.
            raise ExportError(f"Transformation failed for target {column['target']}; check source type, nullability, and rule contract") from exc
    return result
