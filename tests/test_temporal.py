"""Tests for shared temporal helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cruxible_core.temporal import (
    ensure_utc,
    format_datetime,
    is_effective,
    is_expired,
    parse_datetime,
    utc_now,
)


def test_utc_now_returns_aware_utc() -> None:
    now = utc_now()

    assert now.tzinfo is timezone.utc
    assert now.utcoffset() == timedelta(0)


def test_ensure_utc_handles_aware_utc_non_utc_and_naive() -> None:
    aware_utc = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    aware_non_utc = datetime(
        2026,
        5,
        17,
        8,
        0,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    naive = datetime(2026, 5, 17, 12, 0)

    assert ensure_utc(aware_utc) == aware_utc
    assert ensure_utc(aware_non_utc) == aware_utc
    assert ensure_utc(naive) == aware_utc


def test_parse_datetime_accepts_supported_inputs() -> None:
    expected = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)

    assert parse_datetime(None) is None
    assert parse_datetime(expected) == expected
    assert parse_datetime("2026-05-17T12:00:00Z") == expected
    assert parse_datetime("2026-05-17T12:00:00+00:00") == expected
    assert parse_datetime("2026-05-17T08:00:00-04:00") == expected
    assert parse_datetime("2026-05-17T12:00:00") == expected


def test_parse_datetime_invalid_string_raises() -> None:
    with pytest.raises(ValueError):
        parse_datetime("not-a-datetime")


def test_format_datetime_emits_plus_zero_offset_not_z() -> None:
    value = datetime(2026, 5, 17, 8, 0, tzinfo=timezone(timedelta(hours=-4)))

    formatted = format_datetime(value)

    assert formatted == "2026-05-17T12:00:00+00:00"
    assert formatted is not None
    assert not formatted.endswith("Z")


def test_is_expired_handles_none_invalid_past_and_future() -> None:
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)

    assert is_expired(None, now=now) is False
    assert is_expired("not-a-datetime", now=now) is False
    assert is_expired("2026-05-17T11:59:59+00:00", now=now) is True
    assert is_expired("2026-05-17T12:00:01+00:00", now=now) is False


def test_is_effective_handles_open_and_bounded_windows() -> None:
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)

    assert is_effective(now=now) is True
    assert is_effective(effective_from="2026-05-17T12:00:01Z", now=now) is False
    assert is_effective(effective_until="2026-05-17T12:00:00+00:00", now=now) is False
    assert (
        is_effective(
            effective_from="2026-05-17T11:00:00+00:00",
            effective_until="2026-05-17T13:00:00+00:00",
            now=now,
        )
        is True
    )
    assert (
        is_effective(
            effective_from=datetime(2026, 5, 17, 11, 0),
            effective_until=datetime(
                2026,
                5,
                17,
                9,
                0,
                tzinfo=timezone(timedelta(hours=-4)),
            ),
            now=now,
        )
        is True
    )


def test_is_effective_invalid_string_raises() -> None:
    with pytest.raises(ValueError):
        is_effective(effective_from="not-a-datetime")
