import csv
import io

import pytest

from sql_exporter.common import ExportError
from sql_exporter.serialization import record


def test_roundtrip_special_characters_and_null():
    original = ["José 東京", "a|b", 'say "hello"', "one\ntwo", "one\rtwo", "\r\n", "", None, "end"]
    data = record(original)
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b'"say ""hello"""' in data
    parsed = next(csv.reader(io.StringIO(data.decode("utf-8"), newline=""), delimiter="|"))
    assert [None if v == "\\N" else v for v in parsed] == original


def test_null_collision_is_rejected():
    with pytest.raises(ExportError, match="collides"):
        record(["\\N"])
    assert record([None, ""]) == b'\\N|""\n'


def test_single_empty_column_and_cr_quote():
    assert record([""]) == b'""\n'
    assert record(["a\rb"]) == b'"a\rb"\n'

