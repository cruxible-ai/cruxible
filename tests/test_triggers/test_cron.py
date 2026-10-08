"""Cron schedules: one five-field UTC grammar, acceptance-forward, never retroactive."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.cron import CronExpressionError, parse_cron
from cruxible_client.contracts.triggers import CronSchedule
from cruxible_core.triggers.cadence import timer_due
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
        "calendar", action="evidence.sweep", schedule=CronSchedule(expression="0 9 * * 1-5")
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
        CronSchedule.model_validate({"expression": "0 9 * * *", "timezone": "UTC"})


@pytest.mark.parametrize("field", ["timezone", "tz", "time_zone"])
def test_an_authored_timezone_is_refused_with_the_utc_reason(field: str) -> None:
    from pydantic import ValidationError

    from cruxible_client.contracts.authoring.inputs import TriggerInput
    from cruxible_client.contracts.cron import CRON_UTC_HINT

    with pytest.raises(ValidationError) as caught:
        TriggerInput.model_validate(
            {
                "kind": "trigger",
                "name": "weekday-mornings",
                "schedule": {
                    "kind": "cron",
                    "expression": "0 9 * * 1-5",
                    field: "America/New_York",
                },
                "line_name": "triage",
            }
        )
    (error,) = caught.value.errors()
    # A typed refusal naming the rule and the repair, not a generic extra field.
    assert error["type"] == "cron_utc_only"
    assert (
        error["msg"] == f"A cron schedule names no {field}: it is evaluated in UTC. {CRON_UTC_HINT}"
    )


def test_the_trigger_example_says_cron_is_utc_and_how_to_convert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from click.testing import CliRunner

    from cruxible_client.authoring.examples import (
        authoring_example_note,
        trigger_example,
    )
    from cruxible_client.contracts.cron import CRON_UTC_HINT
    from cruxible_client.contracts.triggers import CronSchedule
    from cruxible_core.cli.main import cli
    from cruxible_core.mcp.handlers import handle_playbill_authoring_example

    assert "UTC" in CRON_UTC_HINT and "14:00 UTC" in CRON_UTC_HINT
    assert trigger_example.__doc__ is not None and CRON_UTC_HINT.split(";")[0] in " ".join(
        trigger_example.__doc__.split()
    )
    # The note leads with the UTC guidance, then names the generation floor refresh.
    note = authoring_example_note("trigger")
    assert note is not None and note.startswith(CRON_UTC_HINT)
    generation = note.removeprefix(CRON_UTC_HINT).strip()
    assert generation.startswith("Generation floor refresh: ")
    assert json.loads(generation.removeprefix("Generation floor refresh: ").rstrip(".")) == {
        "kind": "trigger",
        "name": "floor-refresh",
        "schedule": {"kind": "generation_accepted"},
        "action": "floor.refresh",
    }
    assert authoring_example_note("line") is None
    # The schema an agent reads says UTC on the expression itself.
    description = CronSchedule.model_json_schema()["properties"]["expression"]["description"]
    assert "evaluated in UTC" in description and CRON_UTC_HINT in description

    printed = CliRunner().invoke(cli, ["authoring", "example", "trigger"])
    assert printed.exit_code == 0, printed.output
    # stdout stays one JSON document; the hint rides beside it.
    assert json.loads(printed.stdout)["schedule"]["kind"] == "cron"
    assert printed.stderr.strip() == f"# {note}"
    assert handle_playbill_authoring_example("trigger").note == note


def test_a_cron_line_tick_follows_its_last_fire_and_never_precedes_its_floor() -> None:
    hourly = CronSchedule(expression="0 * * * *")
    noon = MONDAY.replace(hour=12)

    def due(last, not_before=None):  # type: ignore[no-untyped-def]
        return timer_due(hourly, accepted_at=noon, last=last, not_before=not_before)

    # The first instant after an acceptance or a fire, however long ago that was.
    assert due(noon + timedelta(minutes=30)) == noon + timedelta(hours=1)
    assert due(noon) == noon + timedelta(hours=1)
    # A floor (an arm's start) keeps a forward-only reader from any instant before it.
    assert due(noon, not_before=noon + timedelta(hours=5, minutes=1)) == noon + timedelta(hours=6)
    assert due(noon, not_before=noon + timedelta(hours=5)) == noon + timedelta(hours=5)


def _world(tmp_path: Path):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        root=tmp_path, descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )


def test_a_new_weekly_cron_fires_nothing_before_its_next_instant(tmp_path: Path) -> None:
    world = _world(tmp_path)
    # Accepted on a Wednesday noon; the Monday 09:00 of that week precedes it.
    wednesday = MONDAY + timedelta(days=2, hours=12)
    weekly = (
        InternalTrigger(
            "Trigger:weekly",
            "evidence.sweep",
            CronSchedule(expression="0 9 * * 1"),
            accepted_at=wednesday,
        ),
    )
    since = wednesday
    for at in (wednesday, wednesday + timedelta(days=4)):
        assert evaluate_triggers(world, now=at, listening_since=since, triggers=weekly) == ()
    next_monday = MONDAY + timedelta(days=7, hours=9)
    (fired,) = evaluate_triggers(world, now=next_monday, listening_since=since, triggers=weekly)
    assert fired.due_at == next_monday


def test_an_internal_cron_trigger_skips_the_instants_a_downtime_missed(tmp_path: Path) -> None:
    world = _world(tmp_path)
    nightly = (
        InternalTrigger(
            "Trigger:nightly-sweep",
            "evidence.sweep",
            CronSchedule(expression="0 0 * * *"),
            accepted_at=MONDAY - timedelta(hours=1),
        ),
    )

    def fire(at, since):  # type: ignore[no-untyped-def]
        return evaluate_triggers(world, now=at, listening_since=since, triggers=nightly)

    (first,) = fire(MONDAY + timedelta(minutes=1), MONDAY - timedelta(hours=1))
    assert first.due_at == MONDAY
    # A week down: the restarted daemon fires none of the nights it missed...
    restarted = MONDAY + timedelta(days=8, hours=3)
    assert fire(restarted, restarted) == ()
    # ...and the next night at its own instant.
    (resumed,) = fire(MONDAY + timedelta(days=9), restarted)
    assert resumed.due_at == MONDAY + timedelta(days=9)
