"""Line v6 and Trigger fixtures: a Line runs on the Triggers aimed at it."""

from __future__ import annotations

from typing import Any, Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactNameRef
from cruxible_client.contracts.procedures.line_specs import LineBudgets, LineBudgetsInput, LineSpec
from cruxible_client.contracts.triggers import (
    ActionTarget,
    CadenceSchedule,
    LineTarget,
    Trigger,
    TriggerSchedule,
    render_trigger,
    trigger_digest,
    trigger_path,
)


def line_budgets(**overrides: Any) -> LineBudgets:
    """Operating limits for a Line whose test does not exercise budgets."""

    from tests.support.procedures import RUN_WALL_CLOCK

    return LineBudgets(
        **{
            "max_wall_clock_microseconds": RUN_WALL_CLOCK.microseconds,
            "max_provider_calls": 0,
            "max_capture_bytes": 0,
            **overrides,
        }
    )


def line_budgets_input(**overrides: Any) -> LineBudgetsInput:
    """Authoring's partial budget, distinct from an accepted Line's filled budget."""

    return LineBudgetsInput(**overrides)


def line_spec(name: str = "fixture", **overrides: Any) -> LineSpec:
    """A typed Line; exact Procedure pins and test-specific fields override defaults."""

    from cruxible_client.contracts.artifacts import ArtifactPin
    from tests.support.procedures import fixture_digest

    target = ArtifactIdentity(kind="Procedure", name="fixture")
    return LineSpec(
        **{
            "identity": ArtifactIdentity(kind="Line", name=name),
            "occurrence_epoch": 1,
            "procedure": ArtifactPin(
                role="procedure", target=target, artifact_digest=fixture_digest(target)
            ),
            "parameters": {},
            "bindings": {},
            "acquisition": {},
            "max_authority": "observe",
            "budgets": line_budgets(),
            "epsilon": {"$decimal": "0"},
            **overrides,
        }
    )


def line_trigger(
    name: str,
    *,
    line: str,
    schedule: TriggerSchedule,
    lifecycle: ArtifactLifecycle | None = None,
) -> Trigger:
    """A Trigger that runs the named Line on its authored schedule."""
    return Trigger(
        identity=ArtifactIdentity(kind="Trigger", name=name),
        schedule=schedule,
        target=LineTarget(
            line=ArtifactNameRef(
                target=ArtifactIdentity(kind="Line", name=line.removeprefix("Line:"))
            )
        ),
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
    """A Trigger that fires one internal action."""
    if schedule is None:
        assert interval_seconds is not None
        schedule = CadenceSchedule(interval_seconds=interval_seconds)
    return Trigger(
        identity=ArtifactIdentity(kind="Trigger", name=name),
        schedule=schedule,
        target=ActionTarget(action=action),
        lifecycle=lifecycle or ArtifactLifecycle(),
    )


def successor(
    trigger: Trigger,
    *,
    schedule: TriggerSchedule | None = None,
    target: LineTarget | ActionTarget | None = None,
    state: Literal["live", "retired"] = "live",
) -> Trigger:
    """The next Trigger version, naming its exact predecessor."""
    return Trigger(
        identity=trigger.identity,
        schedule=schedule or trigger.schedule,
        target=target or trigger.target,
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
