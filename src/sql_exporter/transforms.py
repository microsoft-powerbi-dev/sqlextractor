"""Declarative rule vocabulary, compiled once per export.

`compile_columns` turns a validated mapping into one closure per target column with every
rule-determined constant already resolved: namespace UUIDs, parsed template fields, decimal
factors and quantizers, lookup tables, and source keys. `transform`, `apply_rules` and
`format_value` remain as compatibility shims over the same compiler, so there is exactly one
implementation of every operation and no interpreter/compiler drift.
"""
from datetime import date, datetime
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_UP
from string import Formatter
from uuid import NAMESPACE_URL, uuid5

from .common import ExportError, canonical, identifier, require
from .windows import iso, timestamp

# Pinned so an ambient decimal context cannot change output bytes. prec 80 matches the
# previous per-value localcontext(); every other field keeps the DefaultContext value.
DECIMAL = Context(prec=80)
_NAMESPACES = {}
# Per-rule identity memo bound. Cleared wholesale on overflow to keep memory flat.
IDENTITY_CACHE = 4096

# LookupError rather than KeyError: positional source rows raise IndexError for a missing column.
FAILURES = (ExportError, LookupError, ValueError, TypeError, ArithmeticError)

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


def namespace_uuid(namespace):
    """uuid5(NAMESPACE_URL, namespace) depends only on the rule, so derive it once per namespace."""
    result = _NAMESPACES.get(namespace)
    if result is None:
        if len(_NAMESPACES) >= 1024:
            _NAMESPACES.clear()
        result = _NAMESPACES[namespace] = uuid5(NAMESPACE_URL, namespace)
    return result


def typed_values(values):
    # Include type tags and JSON framing so (1, 23) cannot collide with (12, 3),
    # and numeric 1 intentionally differs from text "1". Column names are not
    # included: the same logical foreign key may have a different source name.
    result = []
    for value in values:
        require(type(value) in {str, int} and value != "", "identity keys must be nonempty text or integers; nulls/floats are forbidden")
        result.append(["integer" if type(value) is int else "string", value])
    return result


def identity_value(row, namespace, keys):
    return str(uuid5(namespace_uuid(namespace), canonical(typed_values([row[key] for key in keys]))))


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


def text_value(value):
    """format_value(value, {"type": "string", "nullable": False}) without the per-call dict."""
    require(value is not None, "NULL is forbidden for this target")
    require(isinstance(value, (str, int, Decimal, bool, date)), "Unsupported value for string conversion")
    if isinstance(value, datetime):
        return iso(timestamp(value))
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


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


def compile_rule(rule, keys):
    """Build one step(value, row). Ops other than constant/identity/template/default pass null
    through unchanged, matching the interpreter's `elif value is None: continue`."""
    require(isinstance(rule, dict) and isinstance(rule.get("op"), str) and rule["op"] in FIELDS,
            "Unsupported transformation; use a documented declarative op")
    op = rule["op"]

    if op == "constant":
        constant = rule["value"]
        return lambda value, row: constant

    if op == "identity":
        base = namespace_uuid(rule["namespace"])
        names = tuple(keys.get(name, name) for name in rule["keys"])
        # Foreign-key identities repeat heavily across rows (a product id can recur thousands of
        # times), and the derivation is a pure function of the key values. Memoize it, bounded.
        # The cache key carries the value types because 1, True and "1" are equal-or-hashable
        # neighbours in Python but must never share an identity, and because a hit skips the
        # type validation that typed_values performs on a miss.
        cache = {}

        def step(value, row):
            raw = [row[name] for name in names]
            marker = (tuple(raw), tuple(map(type, raw)))
            found = cache.get(marker)
            if found is not None:
                return found
            result = str(uuid5(base, canonical(typed_values(raw))))
            if len(cache) >= IDENTITY_CACHE:
                cache.clear()
            cache[marker] = result
            return result
        return step

    if op == "template":
        text = rule["value"]
        fields = tuple((name, keys.get(name, name)) for name in template_fields(text))
        render = text.format_map
        return lambda value, row: render({name: text_value(row[key]) for name, key in fields})

    if op == "default":
        fallback = rule["value"]
        return lambda value, row: fallback if value is None else value

    if op == "to_string":
        return lambda value, row: None if value is None else text_value(value)

    if op in {"prefix", "suffix"}:
        affix = rule["value"]
        message = f"{op} requires a string; use to_string first for numeric input"
        if op == "prefix":
            def step(value, row):
                if value is None:
                    return None
                require(isinstance(value, str), message)
                return affix + value
        else:
            def step(value, row):
                if value is None:
                    return None
                require(isinstance(value, str), message)
                return value + affix
        return step

    if op in {"trim", "upper", "lower"}:
        method = "strip" if op == "trim" else op
        message = f"{op} requires a string input"

        def step(value, row):
            if value is None:
                return None
            require(isinstance(value, str), message)
            return getattr(value, method)()
        return step

    if op == "replace":
        old, new = rule["old"], rule["new"]

        def step(value, row):
            if value is None:
                return None
            require(isinstance(value, str), "replace requires a string input")
            return value.replace(old, new)
        return step

    if op == "lookup":
        table, on_missing = rule["values"], rule["on_missing"]

        def step(value, row):
            if value is None:
                return None
            require(isinstance(value, str), "lookup requires a string input")
            if value in table:
                return table[value]
            require(on_missing != "error", "lookup key is unmapped")
            return None if on_missing == "null" else value
        return step

    factor = decimal(rule["factor"])
    multiply = DECIMAL.multiply

    def step(value, row):
        if value is None:
            return None
        return multiply(decimal(value), factor)
    return step


def compile_format(column):
    """Build fmt(value) with type, nullability and scale resolved once."""
    kind = column["type"]
    if kind == "string":
        convert = text_value
    elif kind == "integer":
        def convert(value):
            number = decimal(value)
            require(number == number.to_integral_value() and -(2**63) <= number < 2**63, "Expected an exact signed 64-bit integer")
            return str(int(number))
    elif kind == "decimal":
        quantum = Decimal(1).scaleb(-column["scale"])

        def convert(value):
            number = decimal(value).quantize(quantum, rounding=ROUND_HALF_UP, context=DECIMAL)
            require(len(number.as_tuple().digits) <= 38, "Rounded decimal exceeds 38 digits")
            return format(number, "f")
    elif kind == "boolean":
        def convert(value):
            require(type(value) in {str, int, bool} and value in (True, False, 0, 1, "true", "false", "0", "1"),
                    "Boolean accepts only true/false or 0/1")
            return "true" if value in (True, 1, "true", "1") else "false"
    elif kind == "date":
        def convert(value):
            return timestamp(value).date().isoformat()
    else:
        def convert(value):
            return iso(timestamp(value))

    if column["nullable"]:
        return lambda value: None if value is None else convert(value)

    def fmt(value):
        require(value is not None, "NULL is forbidden for this target")
        return convert(value)
    return fmt


def compile_column(column, keys=None):
    """Build run(row) -> formatted text or None, including this column's error contract."""
    keys = keys or {}
    source = column["source"]
    # `is not None`, not truthiness: a positional key may legitimately be index 0.
    key = keys.get(source, source) if source else None
    steps = tuple(compile_rule(rule, keys) for rule in column["rules"])
    fmt = compile_format(column)
    message = f"Transformation failed for target {column['target']}; check source type, nullability, and rule contract"

    if not steps:
        def run(row):
            try:
                return fmt(row[key] if key is not None else None)
            except FAILURES as exc:
                # Do not include source data in exceptions or failure manifests.
                raise ExportError(message) from exc
        return run

    if len(steps) == 1:
        only = steps[0]

        def run(row):
            try:
                return fmt(only(row[key] if key is not None else None, row))
            except FAILURES as exc:
                raise ExportError(message) from exc
        return run

    def run(row):
        try:
            value = row[key] if key is not None else None
            for step in steps:
                value = step(value, row)
            return fmt(value)
        except FAILURES as exc:
            raise ExportError(message) from exc
    return run


def compile_columns(columns, keys=None):
    """Compile a whole mapping once per export. `keys` maps source names to positional
    indexes for sources that yield sequences; absent names stay string keys."""
    return [compile_column(column, keys) for column in columns]


def apply_rules(value, rules, row=None):
    """Compatibility shim. The engine compiles once per export instead of calling this."""
    if row is None:
        for rule in rules:
            require(rule.get("op") not in {"identity", "template"}, f"{rule.get('op')} requires source row context")
    for rule in rules:
        value = compile_rule(rule, {})(value, row)
    return value


def format_value(value, column):
    """Compatibility shim over compile_format for ad-hoc column dicts."""
    return compile_format(column)(value)


def transform(row, columns):
    """Compatibility shim. The engine compiles once per export instead of calling this."""
    return [run(row) for run in compile_columns(columns)]
