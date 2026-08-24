"""Shared UTC datetime helpers."""

from __future__ import annotations

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


def describe_rejected_datetime(value: object) -> str:
    """Render a bounded echo of the value a datetime check rejected."""
    text = str(value)
    if len(text) > _MAX_ECHOED_VALUE_CHARS:
        text = f"{text[:_MAX_ECHOED_VALUE_CHARS]}..."
    return repr(text)


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


__all__ = [
    "ISO_8601_FORMAT_HINT",
    "describe_rejected_datetime",
    "ensure_utc",
    "format_datetime",
    "is_effective",
    "is_expired",
    "parse_datetime",
    "utc_now",
]
