import io
import re
import struct
import zipfile
from pathlib import Path

from .common import ExportError, canonical, file_hash, fingerprint, identifier, require, strict_json
from .transforms import TYPES, validate_rules

HEADERS = ["ordinal", "source", "target", "type", "nullable", "rules", "scale"]


def _xls_formula_check(data):
    """xlrd returns formula caches. Reject BIFF formula records before using it."""
    import olefile
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        require(not any("VBA" in "/".join(p).upper() for p in ole.listdir()), "Macros are unsupported")
        name = "Workbook" if ole.exists("Workbook") else "Book"
        stream = ole.openstream(name).read()
    offset = 0
    while offset + 4 <= len(stream):
        opcode, size = struct.unpack_from("<HH", stream, offset)
        require(opcode not in {0x0006, 0x0206, 0x0406}, "Excel formulas are unsupported; use declarative JSON rules")
        require(opcode != 0x002F, "Encrypted workbooks are unsupported")
        offset += 4 + size


def _read(path):
    require(path.stat().st_size <= 10 * 1024 * 1024, "Mapping workbook exceeds 10 MiB")
    data = path.read_bytes()
    if path.suffix.lower() == ".xlsx":
        import openpyxl
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            require(sum(i.file_size for i in archive.infolist()) <= 50 * 1024 * 1024, "Expanded workbook exceeds 50 MiB")
            require(not any("externalLinks/" in n or "vbaProject" in n for n in archive.namelist()), "External links and macros are unsupported")
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        try:
            result = {}
            for sheet in book:
                # Dimensions are optional in OOXML and can also be stale after edits.
                # Ignore them so cells/formulas outside a declared range cannot be skipped.
                sheet.reset_dimensions()
                rows = []
                for index, row in enumerate(sheet.iter_rows(), 1):
                    require(index <= 4096 and len(row) <= 32, "Mapping sheets are limited to 4096 rows and 32 columns")
                    require(all(c.data_type not in {"f", "e"} for c in row), "Excel formulas and error cells are unsupported; use declarative JSON rules")
                    rows.append([c.value for c in row])
                result[sheet.title] = rows
            return result
        finally:
            book.close()
    require(path.suffix.lower() == ".xls", "Mapping must be .xls or .xlsx")
    import xlrd
    _xls_formula_check(data)
    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    try:
        result = {}
        for sheet in book.sheets():
            require(sheet.nrows <= 4096 and sheet.ncols <= 32, "Mapping sheets are limited to 4096 rows and 32 columns")
            rows = []
            for i in range(sheet.nrows):
                cells = sheet.row(i)
                require(all(c.ctype != xlrd.XL_CELL_ERROR for c in cells), "Excel error cells are unsupported")
                rows.append([bool(c.value) if c.ctype == xlrd.XL_CELL_BOOLEAN else c.value for c in cells])
            result[sheet.name] = rows
        return result
    finally:
        book.release_resources()


def _rows(rows):
    for row in rows:
        row = [None if v == "" else v for v in row]
        while row and row[-1] is None:
            row.pop()
        if row:
            yield row


def parse_mapping(sheets):
    require(set(sheets) == {"Metadata", "Columns"}, "Workbook must contain exactly Metadata and Columns sheets")
    metadata = list(_rows(sheets["Metadata"]))
    require(metadata and metadata[0] == ["key", "value"], "Metadata header must be key,value")
    meta = {}
    for row in metadata[1:]:
        require(len(row) == 2 and isinstance(row[0], str) and row[0] not in meta, "Invalid or duplicate Metadata key")
        meta[row[0]] = row[1]
    require(set(meta) == {"contract_version", "mapping_id", "mapping_version"}, "Metadata requires contract_version, mapping_id, mapping_version only")
    require(type(meta["contract_version"]) in {int, float} and meta["contract_version"] == 1, "Only contract_version 1 is supported")
    meta["contract_version"] = 1
    identifier(meta["mapping_id"])
    require(isinstance(meta["mapping_version"], str) and re.fullmatch(r"\d+\.\d+\.\d+", meta["mapping_version"]), "mapping_version must be text MAJOR.MINOR.PATCH")
    rows = list(_rows(sheets["Columns"]))
    require(rows and rows[0] == HEADERS, "Columns headers must be exactly: " + ", ".join(HEADERS))
    columns = []
    for index, row in enumerate(rows[1:], 2):
        require(len(row) <= len(HEADERS), f"Columns row {index}: extra cells")
        column = dict(zip(HEADERS, row + [None] * (len(HEADERS) - len(row))))
        try:
            ordinal = column["ordinal"]
            require(type(ordinal) in {int, float} and ordinal == int(ordinal), "ordinal must be an integer")
            column["ordinal"] = int(ordinal)
            identifier(column["target"])
            if column["source"] is not None:
                identifier(column["source"])
            require(column["type"] in TYPES, "Unsupported target type")
            require(type(column["nullable"]) is bool, "nullable must be an Excel TRUE/FALSE boolean")
            column["rules"] = validate_rules(strict_json(column["rules"]) if column["rules"] else [])
            require(column["source"] is not None or (column["rules"] and column["rules"][0]["op"] in {"constant", "identity", "template"}), "Blank source requires a constant, identity, or template operation")
            if column["type"] == "decimal":
                scale = column["scale"]
                require(type(scale) in {int, float} and scale == int(scale) and 0 <= scale <= 18, "decimal scale must be an integer from 0 to 18")
                column["scale"] = int(scale)
            else:
                require(column["scale"] is None, "scale is only supported for decimal")
        except (ExportError, TypeError, ValueError, OverflowError) as exc:
            raise ExportError(f"Columns row {index}: {exc}") from exc
        columns.append(column)
    require(columns and len(columns) <= 1024, "Mapping requires 1 to 1024 target columns")
    columns.sort(key=lambda c: c["ordinal"])
    require([c["ordinal"] for c in columns] == list(range(1, len(columns) + 1)), "Ordinals must be unique and contiguous from 1")
    require(len({c["target"].lower() for c in columns}) == len(columns), "Duplicate target names")
    return {"metadata": meta, "columns": columns}


def normalize_model(model):
    """Canonical JSON and Excel share exactly the same validation/semantics."""
    require(isinstance(model, dict) and set(model) == {"metadata", "columns"}, "Mapping JSON requires metadata and columns only")
    require(isinstance(model["metadata"], dict) and isinstance(model["columns"], list), "Invalid mapping JSON shape")
    rows = [HEADERS]
    for column in model["columns"]:
        require(isinstance(column, dict) and set(column) == set(HEADERS), "JSON column fields must match the workbook headers exactly")
        require(isinstance(column["rules"], list), "JSON rules must be an array")
        rows.append([canonical(column[k]) if k == "rules" else column[k] for k in HEADERS])
    return parse_mapping({"Metadata": [["key", "value"], *[list(pair) for pair in model["metadata"].items()]], "Columns": rows})


def load_mapping(path):
    path = Path(path)
    try:
        if path.suffix.lower() == ".json":
            require(path.stat().st_size <= 10 * 1024 * 1024, "Mapping JSON exceeds 10 MiB")
            model = normalize_model(strict_json(path.read_text(encoding="utf-8")))
        else:
            model = parse_mapping(_read(path))
        return {"model": model, "fingerprint": fingerprint(model), "workbook_sha256": file_hash(path)}
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError("Cannot read mapping workbook; check file format, contents, and reader dependencies") from exc
