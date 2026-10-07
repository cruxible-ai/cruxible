"""Line v6 and Trigger fixtures: a Line runs on the Triggers aimed at it."""

from __future__ import annotations

from typing import Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactRef
from cruxible_client.contracts.triggers import (
    TRIGGER_LINE_REF_ROLE,
    ActionTarget,
    CadenceSchedule,
    LineTarget,
    Trigger,
    TriggerSchedule,
    render_trigger,
    trigger_digest,
    trigger_path,
    trigger_schedule_pins,
)


def line_trigger(
    name: str,
    *,
    line: str,
    schedule: TriggerSchedule,
    lifecycle: ArtifactLifecycle | None = None,
) -> Trigger:
    """A Trigger that runs the named Line on `schedule`."""

    return Trigger(
        identity=ArtifactIdentity(kind="Trigger", name=name),
        schedule=schedule,
        target=LineTarget(
            line=ArtifactRef(
                role=TRIGGER_LINE_REF_ROLE,
                target=ArtifactIdentity(kind="Line", name=line.removeprefix("Line:")),
            )
        ),
        pins=trigger_schedule_pins(schedule),
        lifecycle=lifecycle or ArtifactLifecycle(),
    )


def action_trigger(
    name: str,
    *,
    action: str,
    interval_seconds: int | None = None,
    schedule: TriggerSchedule | None = None,
    lifecycle: ArtifactLifecycle | None = None,
) -> Trigger:
    """A Trigger that fires one internal action, on a cadence unless given a schedule."""

    if schedule is None:
        assert interval_seconds is not None
        schedule = CadenceSchedule(interval_seconds=interval_seconds)
    return Trigger(
        identity=ArtifactIdentity(kind="Trigger", name=name),
        schedule=schedule,
        target=ActionTarget(action=action),
        pins=trigger_schedule_pins(schedule),
        lifecycle=lifecycle or ArtifactLifecycle(),
    )


def successor(
    trigger: Trigger,
    *,
    schedule: TriggerSchedule | None = None,
    target: LineTarget | ActionTarget | None = None,
    state: Literal["live", "retired"] = "live",
) -> Trigger:
    """The next version of a Trigger, pinning its exact predecessor."""

    schedule = schedule or trigger.schedule
    return Trigger(
        identity=trigger.identity,
        schedule=schedule,
        target=target or trigger.target,
        pins=trigger_schedule_pins(schedule),
        lifecycle=ArtifactLifecycle(state=state, predecessor_digest=trigger_digest(trigger).tagged),
    )


def trigger_members(*triggers: Trigger) -> dict[str, bytes]:
    """Tree members for Triggers, keyed by their ledger paths."""

    return {trigger_path(item.identity.name): render_trigger(item) for item in triggers}


def line_enablement(instance, line: str, *, now=None):  # type: ignore[no-untyped-def]
    """The Line's latest enablement as ``get Line:<name>`` shows it (the folded line status).

    ``now`` is the read's evaluation time (it decides ``stalled``); the default
    is an hour after the shared fixtures' read time.
    """

    from datetime import UTC, datetime

    from cruxible_client.contracts.cas_contracts import BodyAccessContext
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_client.contracts.operational_reads import GetLineCard
    from cruxible_core.service.discovery.get import service_playbill_get

    card = service_playbill_get(
        instance,
        request=GetRequest(
            ref=f"Line:{line.removeprefix('Line:')}",
            evaluation_time=now or datetime(2026, 8, 24, 17, tzinfo=UTC),
        ),
        access=BodyAccessContext(principal_id="reader", can_read_body=True),
    ).card
    assert isinstance(card, GetLineCard) and card.enablements, card
    return card.enablements[0]
