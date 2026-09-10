from datetime import date, datetime, timedelta, timezone

from .common import require

UTC = timezone.utc


def timestamp(value):
    """Naive values are UTC by contract; normalize aware timestamps to UTC."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    else:
        require(isinstance(value, str), "Expected an ISO date or timestamp")
        # Reject silently truncated sub-microsecond precision.
        import re
        require(not re.search(r"[.,]\d{7,}", value), "Timestamp precision exceeds six fractional digits")
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            from .common import ExportError
            raise ExportError("Expected an ISO date or timestamp") from exc
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def resolve_range(start=None, end=None, years=3, now=None):
    end = timestamp(end) if end else (now or datetime.now(UTC))
    if start:
        start = timestamp(start)
    else:
        require(end.year > years, "Rolling range underflows the supported calendar")
        try:
            start = end.replace(year=end.year - years)
        except ValueError:  # Feb 29 anniversary clamps to Feb 28.
            start = end.replace(year=end.year - years, day=28)
    require(start < end, "Range start must precede end")
    return start, end


def windows(start, end, granularity="month"):
    require(granularity in {"day", "month", "quarter", "year"}, "Unsupported granularity")
    current = start
    while current < end:
        floor = current.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            if granularity == "day":
                boundary = floor + timedelta(days=1)
            else:
                step = {"month": 1, "quarter": 3, "year": 12}[granularity]
                month = ((current.month - 1) // step + 1) * step
                boundary = floor.replace(year=current.year + month // 12, month=month % 12 + 1, day=1)
        except (ValueError, OverflowError):
            boundary = end
        stop = min(boundary, end)
        yield current, stop
        current = stop


def window_name(start, end):
    return start.strftime("%Y%m%dT%H%M%S%fZ") + "_" + end.strftime("%Y%m%dT%H%M%S%fZ")
