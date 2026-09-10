from copy import deepcopy
import zipfile
import xml.etree.ElementTree as ET

import pytest
import xlwt

from sql_exporter.common import ExportError, fingerprint
from sql_exporter.mapping import HEADERS, _read, load_mapping, parse_mapping
from conftest import ROOT


def sheets():
    return {"Metadata": [["key", "value"], ["contract_version", 1], ["mapping_id", "demo"], ["mapping_version", "1.0.0"]],
            "Columns": [HEADERS, [1, "Id", "id", "integer", False, "[]", None]]}


def test_sample_workbook_import(mapping):
    assert mapping["model"]["metadata"]["mapping_version"] == "1.0.0"
    assert len(mapping["model"]["columns"]) == 7
    assert mapping["model"]["columns"][2]["rules"][0]["op"] == "default"


@pytest.mark.parametrize("mutate", [
    lambda s: s["Metadata"].append(["mapping_version", "2.0.0"]),
    lambda s: s["Metadata"][1].__setitem__(1, 2),
    lambda s: s["Columns"][1].__setitem__(0, 2),
    lambda s: s["Columns"][1].__setitem__(1, "Id; DROP TABLE Orders"),
    lambda s: s["Columns"][1].__setitem__(2, "bad|target"),
    lambda s: s["Columns"][1].__setitem__(3, "money"),
    lambda s: s["Columns"][1].__setitem__(4, "false"),
    lambda s: s["Columns"][1].__setitem__(5, "Uppercase customer"),
    lambda s: s["Columns"][1].__setitem__(6, 2),
    lambda s: s["Columns"].append([2, "Id", "ID", "integer", False, "[]", None]),
    lambda s: s.__setitem__("Notes", [["unused"]]),
])
def test_reject_malformed_workbook_contract(mutate):
    value = sheets()
    mutate(value)
    with pytest.raises(ExportError):
        parse_mapping(value)


def test_fingerprint_tracks_rules_and_version_not_row_order():
    original = sheets()
    original["Columns"].append([2, None, "tag", "string", False, '[{"op":"constant","value":"A"}]', None])
    changed = deepcopy(original)
    changed["Columns"][1:] = reversed(changed["Columns"][1:])
    assert fingerprint(parse_mapping(original)) == fingerprint(parse_mapping(changed))
    changed["Metadata"][-1][1] = "1.0.1"
    assert fingerprint(parse_mapping(original)) != fingerprint(parse_mapping(changed))


def test_real_legacy_xls_reader_and_formula_rejection(tmp_path):
    # Test-only BIFF fixtures exercise the legacy reader; no Excel install required.
    for formula in (False, True):
        book = xlwt.Workbook()
        for name, rows in sheets().items():
            sheet = book.add_sheet(name)
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    if value is not None:
                        if formula and name == "Columns" and i == 1 and j == 5:
                            value = xlwt.Formula('"cached"')
                        sheet.write(i, j, value)
        path = tmp_path / "mapping.xls"
        book.save(str(path))
        if formula:
            with pytest.raises(ExportError, match="formulas"):
                load_mapping(path)
        else:
            assert load_mapping(path)["model"] == parse_mapping(sheets())


def test_xlsx_formula_rejected_even_with_cached_value(tmp_path):
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    path = tmp_path / "formula.xlsx"
    with zipfile.ZipFile(ROOT / "examples/mapping.xlsx") as source, zipfile.ZipFile(path, "w") as target:
        for entry in source.infolist():
            data = source.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(data)
                cell = root.find(f".//{ns}c[@r='F2']")
                cell.clear()
                cell.set("r", "F2")
                ET.SubElement(cell, ns + "f").text = "1+1"
                ET.SubElement(cell, ns + "v").text = "2"
                data = ET.tostring(root)
            target.writestr(entry, data)
    with pytest.raises(ExportError, match="formulas"):
        load_mapping(path)

