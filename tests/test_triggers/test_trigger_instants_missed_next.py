"""`next` for an authored action Trigger that missed instants (ruling 7, follow-up 3).

An internal action runs on the CLI only: an MCP caller is not told it needs
another tool profile, and its row's command says it is a CLI command.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from tests.support.runbooks import author, runbook_world

from cruxible_client.contracts.authoring.inputs import TriggerInput
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.service.discovery.next import NextRequest, service_playbill_next
from cruxible_core.triggers.journal import evaluate_triggers, internal_triggers, trigger_events


def _next(instance, at, **caller):  # type: ignore[no-untyped-def]
    request = NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=at,
        access_profile=CoverageAccessProfile(
            profile_id="missed", permitted_access_classes=("instance", "public")
        ),
        expiring_within=CanonicalDuration(microseconds=604_800_000_000),
        **caller,
    )
    return [
        item
        for item in service_playbill_next(instance, request=request).items
        if item.reason == "trigger_instants_missed"
    ]


def test_an_action_rows_repair_is_a_cli_command_on_every_surface(tmp_path: Path) -> None:
    instance, _workspace = runbook_world(tmp_path)
    author(
        instance,
        TriggerInput.model_validate(
            {
                "kind": "trigger",
                "name": "sweep-hourly",
                "schedule": {"kind": "cadence", "interval_seconds": 3600},
                "action": "evidence.sweep",
            }
        ),
        name="trigger-sweep-hourly",
    )
    (item,) = (t for t in internal_triggers(instance) if t.trigger == "Trigger:sweep-hourly")
    evaluate_triggers(
        instance, now=item.accepted_at + timedelta(minutes=5), listening_since=item.accepted_at
    )
    restart = item.accepted_at + timedelta(hours=3, minutes=30)
    evaluate_triggers(instance, now=restart, listening_since=restart)

    (cli,) = _next(instance, restart)
    assert cli.subject_identity == "Trigger:sweep-hourly"
    assert cli.related_identities == ()
    assert (cli.detail["count"], cli.detail["target"]) == (3, "action evidence.sweep")
    assert cli.repair.operation == "cruxible.action.run"
    assert cli.repair.command == "cruxible action run evidence.sweep"

    (mcp,) = _next(
        instance, restart, caller_surface="mcp", caller_tools=("cruxible_next", "cruxible_get")
    )
    assert mcp.repair is not None and mcp.repair_requires is None
    assert mcp.repair.command == (
        "cruxible action run evidence.sweep  (on the CLI; the MCP has no form for "
        "cruxible.action.run)"
    )
    (sdk,) = _next(instance, restart, caller_surface="sdk")
    assert sdk.repair is not None and "(on the CLI;" in (sdk.repair.command or "")

    # A fire of the action after the restart (here by hand) clears the row.
    from cruxible_core.triggers.journal import fire_manual

    fire_manual(instance, action="evidence.sweep", principal="owner", now=restart)
    assert trigger_events(instance, action="evidence.sweep")
    assert _next(instance, restart) == []


def test_a_lines_outages_sum_into_one_row_per_trigger(tmp_path: Path) -> None:
    """Review finding 3: two outages under one cron Trigger are one row with the
    summed count; its repair evaluates the earliest uncovered range, then the next."""

    from datetime import datetime

    from tests.test_procedures import test_line_arming as arming
    from tests.test_procedures.test_line_triggers import line_world
    from tests.test_procedures.test_procedure_run_surface import READ_TIME

    from cruxible_client.contracts.line_dispatch import LineEvaluateRequest
    from cruxible_client.contracts.temporal import format_datetime
    from cruxible_client.contracts.triggers import CronSchedule
    from cruxible_core.service.procedures.line_dispatch import service_evaluate_line

    instance, line, _procedure = line_world(tmp_path, CronSchedule(expression="*/10 * * * *"))
    arming.service_enable_line(
        instance,
        line.identity.name,
        principal=arming.LOCAL,
        actor=arming._actor(instance),
        now=READ_TIME + timedelta(seconds=10),
        daemon_id="daemon",
    )
    arming._match(instance, READ_TIME + timedelta(minutes=1))
    arming._match(instance, READ_TIME + timedelta(minutes=31), daemon_id="second")
    arming._match(instance, READ_TIME + timedelta(minutes=32), daemon_id="second")
    restarted = READ_TIME + timedelta(minutes=61)
    arming._match(instance, restarted, daemon_id="third")

    (row,) = _next(instance, restarted)
    assert row.detail["count"] == 6
    assert [row.detail["since"], row.detail["until"]] == [
        format_datetime(READ_TIME + timedelta(minutes=10)),
        format_datetime(READ_TIME + timedelta(minutes=60)),
    ]
    first_since = datetime.fromisoformat(row.repair.arguments["since"])
    first_until = datetime.fromisoformat(row.repair.arguments["until"])
    assert first_until == READ_TIME + timedelta(minutes=31)

    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=first_since, until=first_until),
        actor=arming._actor(instance),
        now=restarted + timedelta(seconds=5),
    )
    (left,) = _next(instance, restarted)
    assert left.detail["count"] == 3
    assert datetime.fromisoformat(left.repair.arguments["until"]) == restarted
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(
            since=datetime.fromisoformat(left.repair.arguments["since"]), until=restarted
        ),
        actor=arming._actor(instance),
        now=restarted + timedelta(seconds=6),
    )
    assert _next(instance, restarted) == []


def test_a_recorded_outage_of_an_unserved_action_offers_no_repair(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A record left from before the unserved guard (or replayed history) is not
    shown: `action run curation.detect` would refuse, and nothing clears the row."""

    from types import SimpleNamespace

    from cruxible_core.triggers import journal
    from cruxible_core.triggers.journal import MissedInstants

    instance, _workspace = runbook_world(tmp_path)
    at = instance.accepted_evaluation_time(instance.accepted_coordinate().git_oid)
    trigger = SimpleNamespace(
        trigger="Trigger:curation-hourly", action="curation.detect", version=""
    )
    monkeypatch.setattr(journal, "internal_triggers", lambda _instance: (trigger,))
    monkeypatch.setattr(
        journal,
        "missed_trigger_instants",
        lambda _instance: (
            MissedInstants("Trigger:curation-hourly", "curation.detect", at, at, at, 3),
        ),
    )
    assert _next(instance, at) == []
