from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    """ISO-format a datetime, assuming naive values are UTC (MySQL drops tzinfo on round-trip).

    Always emits an explicit UTC offset so JS `new Date(...)` on the frontend doesn't
    misinterpret the string as local time.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
