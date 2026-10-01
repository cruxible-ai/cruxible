"""Cron schedules: one five-field grammar, wall-clock instants, no back-fill."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.cron import CronExpressionError, parse_cron
from cruxible_client.contracts.triggers import CronScheduleV1
from cruxible_core.triggers.cadence import cron_due
from cruxible_core.triggers.journal import InternalTrigger, evaluate_triggers

MONDAY = datetime(2026, 9, 28, tzinfo=UTC)


def test_the_grammar_is_five_numeric_fields_with_ranges_steps_and_lists() -> None:
    spec = parse_cron("*/15 9-17 * * 1-5")
    assert spec.minutes == (0, 15, 30, 45) and spec.hours == tuple(range(9, 18))
    # Sunday is both 0 and 7.
    assert parse_cron("0 0 * * 7").weekdays == parse_cron("0 0 * * 0").weekdays == {0}
    assert parse_cron("5,10-12/2 * * * *").minutes == (5, 10, 12)
    for expression, reason in (
        ("* * * *", "five"),
        ("*  * * * *", "five"),
        ("60 * * * *", "outside 0-59"),
        ("* 24 * * *", "outside 0-23"),
        ("* * 0 * *", "outside 1-31"),
        ("* * * 13 *", "outside 1-12"),
        ("* * * * 8", "outside 0-7"),
        ("*/0 * * * *", "positive integer"),
        ("5-1 * * * *", "runs backwards"),
        ("* * * JAN *", "number, range or"),
        ("@daily * * * *", "number, range or"),
        ("0 0 30 2 *", "never fires"),
    ):
        with pytest.raises(CronExpressionError, match=reason):
            parse_cron(expression)


def test_instants_respect_boundaries_both_ways() -> None:
    spec = parse_cron("0 9 * * 1-5")
    # Strictly after: an instant exactly at the boundary is not "after" itself.
    nine = MONDAY.replace(hour=9)
    assert spec.next_after(nine - timedelta(microseconds=1)) == nine
    assert spec.next_after(nine) == nine + timedelta(days=1)
    # Friday 09:00 is followed by Monday 09:00.
    friday = MONDAY.replace(day=2, month=10, hour=9)
    assert spec.next_after(friday) == friday + timedelta(days=3)
    assert spec.latest_at_or_before(nine) == nine
    assert spec.latest_at_or_before(nine - timedelta(seconds=1)) == nine - timedelta(days=3)
    # Both day fields restricted: a day matches if either does.
    either = parse_cron("0 0 1 * 1")
    assert either.next_after(datetime(2026, 9, 29, tzinfo=UTC)) == datetime(2026, 10, 1, tzinfo=UTC)
    assert either.next_after(datetime(2026, 10, 1, tzinfo=UTC)) == datetime(2026, 10, 5, tzinfo=UTC)


def test_cron_is_utc_and_never_reads_a_host_timezone_database(tmp_path: Path) -> None:
    import zoneinfo

    from pydantic import ValidationError
    from tests.support.lines import action_trigger

    from cruxible_client.contracts.triggers import evaluate_trigger_law, trigger_path

    calendar = action_trigger(
        "calendar", action="evidence.sweep", schedule=CronScheduleV1(expression="0 9 * * 1-5")
    )

    def judged() -> tuple[str, datetime | None]:
        parse_cron.cache_clear()
        law = evaluate_trigger_law(calendar, path=trigger_path("calendar"), predecessor=None)
        return law.verdict, parse_cron("0 9 * * 1-5").next_after(MONDAY)

    with_database = judged()
    original = zoneinfo.TZPATH
    try:
        zoneinfo.reset_tzpath([str(tmp_path)])
        zoneinfo.ZoneInfo.clear_cache()
        assert judged() == with_database == ("accepted", MONDAY.replace(hour=9))
    finally:
        zoneinfo.reset_tzpath(original)
        zoneinfo.ZoneInfo.clear_cache()
        parse_cron.cache_clear()
    # There is no timezone to name.
    with pytest.raises(ValidationError, match="timezone"):
        CronScheduleV1.model_validate({"expression": "0 9 * * *", "timezone": "UTC"})


def test_a_cron_schedule_is_due_once_after_downtime_and_never_before_its_floor() -> None:
    hourly = CronScheduleV1(expression="0 * * * *")
    noon = MONDAY.replace(hour=12)
    # Next instant after the last fire, while it is still ahead.
    assert cron_due(hourly, last=noon, now=noon + timedelta(minutes=30)) == noon + timedelta(
        hours=1
    )
    # Long overdue: the latest instant passed is due once; nothing in between.
    assert cron_due(hourly, last=noon, now=noon + timedelta(hours=5, minutes=1)) == (
        noon + timedelta(hours=5)
    )
    # Never fired: its most recent instant.
    assert cron_due(hourly, last=None, now=noon + timedelta(minutes=10)) == noon
    # A floor keeps a forward-only reader from any instant before it.
    assert cron_due(
        hourly, last=None, now=noon + timedelta(minutes=10), not_before=noon + timedelta(minutes=5)
    ) == noon + timedelta(hours=1)


def test_an_internal_cron_trigger_fires_at_its_instants_and_once_after_downtime(
    tmp_path: Path,
) -> None:
    world = SimpleNamespace(
        root=tmp_path, descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )
    nightly = (
        InternalTrigger(
            "Trigger:nightly-sweep",
            "evidence.sweep",
            CronScheduleV1(expression="0 0 * * *"),
        ),
    )
    (first,) = evaluate_triggers(world, now=MONDAY.replace(hour=1), triggers=nightly)
    assert first.due_at == MONDAY and first.event is None
    assert evaluate_triggers(world, now=MONDAY.replace(hour=23), triggers=nightly) == ()
    (second,) = evaluate_triggers(world, now=MONDAY + timedelta(days=1), triggers=nightly)
    assert second.due_at == MONDAY + timedelta(days=1)
    # A week down fires once, for the latest night only.
    (resumed,) = evaluate_triggers(world, now=MONDAY + timedelta(days=8, hours=3), triggers=nightly)
    assert resumed.due_at == MONDAY + timedelta(days=8)
    assert evaluate_triggers(world, now=MONDAY + timedelta(days=8, hours=4), triggers=nightly) == ()
