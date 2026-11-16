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
