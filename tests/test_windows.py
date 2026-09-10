import pytest

from sql_exporter.common import ExportError
from sql_exporter.windows import iso, resolve_range, timestamp, windows


def test_rolling_anniversary_and_leap_day():
    start, end = resolve_range(end="2024-02-29T12:34:56.123456Z")
    assert iso(start) == "2021-02-28T12:34:56.123456Z"
    assert iso(end) == "2024-02-29T12:34:56.123456Z"


@pytest.mark.parametrize("granularity,count", [("day", 62), ("month", 4), ("quarter", 2), ("year", 1)])
def test_windows_clip_without_gaps_or_overlaps(granularity, count):
    start, end = timestamp("2024-01-31T12:00Z"), timestamp("2024-04-01T06:00Z")
    result = list(windows(start, end, granularity))
    assert len(result) == count
    assert result[0][0] == start and result[-1][1] == end
    assert all(a < b for a, b in result)
    assert all(result[i][1] == result[i + 1][0] for i in range(len(result) - 1))


def test_month_boundaries_and_year_transition():
    result = list(windows(timestamp("2023-12-31T23:59:59Z"), timestamp("2024-03-01Z")))
    assert [iso(b) for _, b in result] == ["2024-01-01T00:00:00.000000Z", "2024-02-01T00:00:00.000000Z", "2024-03-01T00:00:00.000000Z"]


def test_utc_normalization_and_precision():
    assert timestamp("2024-01-01T02:00:00+02:00") == timestamp("2024-01-01")
    with pytest.raises(ExportError, match="precision"):
        timestamp("2024-01-01T00:00:00.1234567Z")


@pytest.mark.parametrize("start,end", [("2024-01-01", "2024-01-01"), ("2025-01-01", "2024-01-01")])
def test_bad_range(start, end):
    with pytest.raises(ExportError, match="precede"):
        resolve_range(start, end)
