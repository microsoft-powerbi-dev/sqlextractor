import openpyxl
import pytest

from sql_exporter.common import ExportError
from sql_exporter.mapping import load_mapping
from sql_exporter.targets import automap, check_against_target, file_settings, import_target, normalize_target
from conftest import ROOT

XSD = ROOT / "examples/targets/orders.xsd"


def write_xsd(tmp_path, body, extra=""):
    path = tmp_path / "t.xsd"
    path.write_text(f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">{extra}<xs:element name="rec"><xs:complexType>'
                    f'<xs:sequence>{body}</xs:sequence></xs:complexType></xs:element></xs:schema>')
    return path


def test_xsd_import_matches_demo_mapping():
    result = import_target(XSD)
    spec = result["spec"]
    assert [c["name"] for c in spec["columns"]] == ["order_id", "created_at", "customer", "amount", "status", "note", "source_system"]
    assert spec["columns"][3] == {"ordinal": 4, "name": "amount", "type": "decimal", "nullable": False, "scale": 2}
    assert spec["columns"][5]["nullable"] and len(result["origin_sha256"]) == 64
    assert check_against_target(load_mapping(ROOT / "examples/mapping.xlsx")["model"], spec) == []


@pytest.mark.parametrize("body,match", [
    ('<xs:element name="x" type="xs:double"/>', "binary floats"),
    ('<xs:element name="x" type="xs:decimal"/>', "fractionDigits"),
    ('<xs:element name="bad-name" type="xs:string"/>', "identifiers"),
    ('<xs:element name="x" type="xs:duration"/>', "unsupported"),
])
def test_xsd_rejections(tmp_path, body, match):
    with pytest.raises(ExportError, match=match):
        import_target(write_xsd(tmp_path, body))


def test_xsd_rejects_entities(tmp_path):
    path = tmp_path / "evil.xsd"
    path.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">&a;</xs:schema>')
    with pytest.raises(ExportError, match="forbidden"):
        import_target(path)


def test_excel_and_sample_targets(tmp_path):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Target"
    sheet.append(["ordinal", "name", "type", "nullable", "scale"])
    sheet.append([1, "id", "integer", False, None])
    sheet.append([2, "amount", "decimal", True, 2])
    file = book.create_sheet("File")
    file.append(["key", "value"])
    file.append(["delimiter", ","])
    file.append(["line_ending", "CRLF"])
    file.append(["pattern", "{mapping_id}_{start:%Y%m}"])
    book.save(tmp_path / "layout.xlsx")
    spec = import_target(tmp_path / "layout.xlsx")["spec"]
    assert spec["columns"][1]["scale"] == 2
    assert file_settings(spec) | {} == file_settings(spec) and spec["file"]["line_ending"] == "\r\n"
    (tmp_path / "sample.csv").write_text("id,amount,note\r\n1,2,x\r\n")
    result = import_target(tmp_path / "sample.csv")
    assert [c["name"] for c in result["spec"]["columns"]] == ["id", "amount", "note"]
    assert result["spec"]["file"]["delimiter"] == "," and result["spec"]["file"]["extension"] == ".csv" and result["warnings"]


def test_target_mismatch_issues():
    spec = import_target(XSD)["spec"]
    model = load_mapping(ROOT / "examples/mapping.xlsx")["model"]
    model["columns"][3] |= {"scale": 3}
    model["columns"][0] |= {"nullable": True}
    model["columns"] = model["columns"][:-1]
    issues = check_against_target(model, spec)
    assert "Target column source_system is not mapped" in issues
    assert "Column amount must have scale 2" in issues
    assert "Column order_id is required (not nullable) in the target" in issues


def test_spec_validation_and_automap():
    spec = import_target(XSD)["spec"]
    with pytest.raises(ExportError):
        normalize_target(spec | {"file": {"delimiter": "||"}})
    model = automap(spec, ["OrderId", "CreatedAt", "Customer", "Amount", "Status", "Note"], "orders")
    assert [c["source"] for c in model["columns"]] == ["OrderId", "CreatedAt", "Customer", "Amount", "Status", "Note", None]
