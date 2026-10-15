"""Target file specs: required columns plus file layout. Imported from XSD, Excel, or a sample file."""
import re
from pathlib import Path

from .common import ExportError, file_hash, identifier, require, strict_json
from .mapping import _read, _rows
from .transforms import TYPES

XS = "{http://www.w3.org/2001/XMLSchema}"
FILE_DEFAULTS = {"pattern": None, "extension": ".txt", "delimiter": "|", "header": True,
                 "line_ending": "\n", "encoding": "utf-8", "null_token": "\\N"}
XSD_TYPES = {"string": "string", "normalizedString": "string", "token": "string", "anyURI": "string",
             "integer": "integer", "int": "integer", "long": "integer", "short": "integer", "byte": "integer",
             "nonNegativeInteger": "integer", "positiveInteger": "integer", "negativeInteger": "integer",
             "nonPositiveInteger": "integer", "unsignedInt": "integer", "unsignedShort": "integer",
             "unsignedByte": "integer", "unsignedLong": "integer", "decimal": "decimal", "date": "date",
             "dateTime": "datetime", "boolean": "boolean"}
COLUMN_KEYS = ["ordinal", "name", "type", "nullable", "scale"]


def normalize_target(spec):
    require(isinstance(spec, dict) and set(spec) == {"target_version", "name", "columns", "file"}, "Target spec requires target_version, name, columns, file")
    require(spec["target_version"] == 1 and type(spec["target_version"]) is int, "Only target_version 1 is supported")
    identifier(spec["name"])
    require(isinstance(spec["columns"], list) and 1 <= len(spec["columns"]) <= 1024, "Target spec requires 1 to 1024 columns")
    columns = []
    for index, column in enumerate(spec["columns"], 1):
        require(isinstance(column, dict) and set(column) == set(COLUMN_KEYS), "Target columns require ordinal, name, type, nullable, scale")
        try:
            require(column["ordinal"] == index and type(column["ordinal"]) is int, "ordinals must be contiguous from 1")
            identifier(column["name"])
            require(column["type"] in TYPES, "Unsupported target type")
            require(type(column["nullable"]) is bool, "nullable must be true or false")
            if column["type"] == "decimal":
                require(type(column["scale"]) is int and 0 <= column["scale"] <= 18, "decimal scale must be 0-18")
            else:
                require(column["scale"] is None, "scale is only supported for decimal")
        except ExportError as exc:
            raise ExportError(f"Target column {index}: {exc}") from exc
        columns.append(dict(column))
    require(len({c["name"].lower() for c in columns}) == len(columns), "Duplicate target column names")
    require(isinstance(spec["file"], dict) and set(spec["file"]) <= set(FILE_DEFAULTS), "Unknown target file setting")
    file = FILE_DEFAULTS | spec["file"]
    # Reuse the engine's config validation so a spec can never publish an unrunnable config.
    from .config import check_config
    probe = {"source": "jsonl", "source_id": "probe", "schema": "probe", "table": "probe", "date_column": "probe",
             "fixture": "probe", "mapping": "probe", "output_dir": "probe"} | file_settings({"file": file})
    check_config(probe, ".")
    return {"target_version": 1, "name": spec["name"], "columns": columns, "file": file}


def file_settings(spec):
    file = FILE_DEFAULTS | spec["file"]
    return {"file_pattern": file["pattern"], "file_extension": file["extension"], "delimiter": file["delimiter"],
            "header": file["header"], "line_ending": file["line_ending"], "encoding": file["encoding"],
            "null_token": file["null_token"]}


def spec_from_columns(name, columns, file=None):
    return normalize_target({"target_version": 1, "name": name, "file": file or {}, "columns": [
        {"ordinal": i, "name": c["name"], "type": c["type"], "nullable": c["nullable"], "scale": c.get("scale")}
        for i, c in enumerate(columns, 1)]})


def safe_name(text):
    name = re.sub(r"[^A-Za-z0-9_]", "_", text or "")[:128] or "target"
    return ("_" + name)[:128] if name[0].isdigit() else name


def _local(name):
    return name.split(":")[-1] if name else name


def from_xsd(path):
    try:
        from defusedxml import ElementTree
    except ImportError as exc:
        raise ExportError("XSD import needs defusedxml; reinstall the package") from exc
    path = Path(path)
    require(path.stat().st_size <= 10 * 1024 * 1024, "XSD exceeds 10 MiB")
    try:
        root = ElementTree.parse(str(path)).getroot()
    except Exception as exc:
        raise ExportError("XSD is not well-formed or uses forbidden entities/DTDs") from exc
    require(root.tag == XS + "schema", "Expected an xs:schema document")
    simple = {e.get("name"): e for e in root.findall(XS + "simpleType")}
    complex_types = {e.get("name") for e in root.findall(XS + "complexType")}
    parents = {child: parent for parent in root.iter() for child in parent}

    def restricted(node, depth):
        restriction = node.find(XS + "restriction")
        require(restriction is not None and depth < 16, "Only xs:restriction simple types are supported")
        facets = {f.tag.replace(XS, ""): f.get("value") for f in restriction}
        base = _local(restriction.get("base"))
        if base in simple:
            inner, more = restricted(simple[base], depth + 1)
            return inner, more | facets
        return base, facets

    def resolve(element):
        inline = element.find(XS + "simpleType")
        if inline is not None:
            return restricted(inline, 0)
        name = _local(element.get("type"))
        return restricted(simple[name], 0) if name in simple else (name, {})

    def flat(sequence):
        children = [c for c in sequence if c.tag == XS + "element"]
        return children and len(children) == len(list(sequence)) and all(
            c.find(XS + "complexType") is None and _local(c.get("type")) not in complex_types
            and (c.find(XS + "simpleType") is not None or c.get("type")) for c in children)

    sequences = [s for s in root.iter(XS + "sequence") if flat(s)]
    require(len(sequences) == 1, "XSD must define exactly one flat record sequence of simple-typed elements")
    sequence = sequences[0]
    owner = parents.get(parents.get(sequence))
    name = (owner.get("name") if owner is not None and owner.get("name") else parents[sequence].get("name")) or path.stem
    columns = []
    for element in sequence:
        label = element.get("name")
        require(label and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", label), "XSD element names must be identifiers; rename elements with dashes or dots")
        base, facets = resolve(element)
        require(base in XSD_TYPES, f"Element {label}: xs:{base} is unsupported" + ("; binary floats are rejected, use xs:decimal with fractionDigits" if base in {"float", "double"} else ""))
        kind = XSD_TYPES[base]
        scale = None
        if kind == "decimal":
            require("fractionDigits" in facets, f"Element {label}: xs:decimal needs a fractionDigits facet for its scale")
            scale = int(facets["fractionDigits"])
        nullable = element.get("minOccurs") == "0" or element.get("nillable") in {"true", "1"}
        columns.append({"name": label, "type": kind, "nullable": nullable, "scale": scale})
    return spec_from_columns(safe_name(name), columns), []


def from_excel(path):
    sheets = _read(Path(path))
    require("Target" in sheets, "Target workbook needs a Target sheet with ordinal, name, type, nullable, scale")
    rows = list(_rows(sheets["Target"]))
    require(rows and [str(v).strip().lower() for v in rows[0]] == COLUMN_KEYS, "Target sheet headers must be: " + ", ".join(COLUMN_KEYS))
    columns = []
    for row in rows[1:]:
        column = dict(zip(COLUMN_KEYS, row + [None] * (5 - len(row))))
        if type(column["scale"]) is float and column["scale"] == int(column["scale"]):
            column["scale"] = int(column["scale"])
        columns.append(column)
    file = {}
    for row in list(_rows(sheets.get("File", [])))[1:]:
        require(len(row) == 2 and row[0] in FILE_DEFAULTS, "File sheet rows must be key,value for: " + ", ".join(FILE_DEFAULTS))
        file[row[0]] = {"\\n": "\n", "\\r\\n": "\r\n", "LF": "\n", "CRLF": "\r\n"}.get(row[1], row[1]) if row[0] == "line_ending" else row[1]
    return spec_from_columns(safe_name(Path(path).stem.split(".")[0]), columns, file), []


def from_sample(path):
    path = Path(path)
    with path.open("rb") as stream:
        head = stream.readline(1024 * 1024).decode("utf-8-sig")
    line = head.rstrip("\r\n")
    delimiter = max("|,\t;", key=line.count)
    require(line.strip(), "Sample file needs a header row")
    names = [n.strip().strip('"') for n in line.split(delimiter)]
    columns = [{"name": n, "type": "string", "nullable": True} for n in names]
    file = {"delimiter": delimiter, "line_ending": "\r\n" if head.endswith("\r\n") else "\n",
            "extension": path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", path.suffix) and path.suffix.lower() != ".json" else ".txt"}
    return spec_from_columns(safe_name(path.stem), columns, file), [
        "Sample headers give names only: every column is string and nullable; review types before approving"]


def import_target(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".xsd":
        spec, warnings = from_xsd(path)
    elif suffix in {".xlsx", ".xls"}:
        spec, warnings = from_excel(path)
    elif suffix == ".json":
        spec, warnings = normalize_target(strict_json(path.read_text(encoding="utf-8"))), []
    else:
        spec, warnings = from_sample(path)
    return {"spec": spec, "warnings": warnings, "origin": path.name, "origin_sha256": file_hash(path)}


def check_against_target(model, spec):
    """Issues as value-free strings; empty means the mapping produces exactly the required file layout."""
    issues = []
    mapped = {c["target"].lower(): c for c in model["columns"]}
    wanted = {c["name"].lower(): c for c in spec["columns"]}
    for name in wanted.keys() - mapped.keys():
        issues.append(f"Target column {wanted[name]['name']} is not mapped")
    for name in mapped.keys() - wanted.keys():
        issues.append(f"Mapping column {mapped[name]['target']} is not in the target spec")
    if not issues and [c["target"].lower() for c in model["columns"]] != [c["name"].lower() for c in spec["columns"]]:
        issues.append("Mapping column order differs from the target spec")
    for name in mapped.keys() & wanted.keys():
        have, need = mapped[name], wanted[name]
        if have["target"] != need["name"]:
            issues.append(f"Column {have['target']} must be spelled {need['name']}")
        if have["type"] != need["type"]:
            issues.append(f"Column {need['name']} must be {need['type']}, mapped as {have['type']}")
        elif have["scale"] != need["scale"]:
            issues.append(f"Column {need['name']} must have scale {need['scale']}")
        if have["nullable"] and not need["nullable"]:
            issues.append(f"Column {need['name']} is required (not nullable) in the target")
    return sorted(issues)


def automap(spec, source_columns, mapping_id=None):
    """Skeleton mapping: one column per target, source matched by name ignoring case and underscores."""
    def key(name):
        return name.replace("_", "").lower()
    sources = {}
    for name in source_columns:
        sources.setdefault(key(name), name)
    columns = []
    for column in spec["columns"]:
        columns.append({"ordinal": column["ordinal"], "source": sources.get(key(column["name"])), "target": column["name"],
                        "type": column["type"], "nullable": column["nullable"], "rules": [], "scale": column["scale"]})
    return {"metadata": {"contract_version": 1, "mapping_id": mapping_id or spec["name"], "mapping_version": "1.0.0"}, "columns": columns}
