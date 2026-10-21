"""Shared UTC datetime helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone

ISO_8601_FORMAT_HINT = "expected ISO 8601 datetime or date, e.g. 2026-08-05T14:30:00Z or 2026-08-05"
"""Self-correcting format guidance echoed by every datetime rejection.

A rejection that only says the value was invalid teaches nothing: the caller
resubmits another guess in the same wrong shape. Every datetime validation
surface (HTTP request validation, runtime API argument checks) appends this
hint so one failed call carries both the accepted format and a copyable
example.
"""

_MAX_ECHOED_VALUE_CHARS = 60


def utc_now() -> datetime:
    """Return a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Normalize to timezone-aware UTC. Treat naive datetimes as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse datetime-like input and normalize to UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    normalized = value.replace("Z", "+00:00")
    return ensure_utc(datetime.fromisoformat(normalized))


def format_datetime(value: datetime | None) -> str | None:
    """Serialize as ISO-8601 UTC using +00:00."""
    if value is None:
        return None
    return ensure_utc(value).isoformat()


def is_expired(value: str | datetime | None, *, now: datetime | None = None) -> bool:
    """Return True if value is before now.

    Invalid strings are treated as not expired to preserve policy-expiry
    compatibility.
    """
    if value is None:
        return False
    try:
        expiry = parse_datetime(value)
    except ValueError:
        return False
    if expiry is None:
        return False
    return expiry < ensure_utc(now or utc_now())


def is_effective(
    *,
    effective_from: str | datetime | None = None,
    effective_until: str | datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True when now is within [effective_from, effective_until)."""
    current = ensure_utc(now or utc_now())
    start = parse_datetime(effective_from)
    end = parse_datetime(effective_until)
    if start is not None and start > current:
        return False
    if end is not None and end <= current:
        return False
    return True


_ISO_8601_DURATION = re.compile(
    r"P(?:(?P<weeks>[0-9]+)W|(?:(?P<days>[0-9]+)D)?"
    r"(?:T(?:(?P<hours>[0-9]+)H)?(?:(?P<minutes>[0-9]+)M)?"
    r"(?:(?P<seconds>[0-9]+)(?:\.(?P<fraction>[0-9]{1,6}))?S)?)?)"
)

ISO_8601_DURATION_HINT = (
    "use a nonnegative ISO-8601 duration with weeks, days, hours, minutes or seconds "
    "(for example P30D, P7D or PT12H)"
)


def parse_iso_duration_microseconds(value: str) -> int:
    """An ISO-8601 duration (``P30D``, ``PT12H``, ``P1W``) in whole microseconds.

    Weeks, days, hours, minutes and seconds (with up to six fractional digits)
    only: months and years have no fixed length. Raises ``ValueError`` naming
    the accepted shape.
    """

    match = _ISO_8601_DURATION.fullmatch(value)
    time_parts = ("hours", "minutes", "seconds")
    if match is None or not any(
        match.group(key) is not None for key in ("weeks", "days", *time_parts)
    ):
        raise ValueError(ISO_8601_DURATION_HINT)
    if "T" in value and not any(match.group(key) is not None for key in time_parts):
        raise ValueError("the ISO-8601 time section must contain a duration component")
    fraction = match.group("fraction") or ""
    return (
        int(match.group("weeks") or 0) * 604_800_000_000
        + int(match.group("days") or 0) * 86_400_000_000
        + int(match.group("hours") or 0) * 3_600_000_000
        + int(match.group("minutes") or 0) * 60_000_000
        + int(match.group("seconds") or 0) * 1_000_000
        + int(fraction.ljust(6, "0") or 0)
    )


__all__ = [
    "ISO_8601_DURATION_HINT",
    "ISO_8601_FORMAT_HINT",
    "ensure_utc",
    "format_datetime",
    "is_effective",
    "is_expired",
    "parse_datetime",
    "parse_iso_duration_microseconds",
    "utc_now",
]
