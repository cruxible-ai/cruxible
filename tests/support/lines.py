"""Line v6 and Trigger fixtures: a Line runs on the Triggers aimed at it."""

from __future__ import annotations

from typing import Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactRef
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.line_specs import (
    RUNG_AUTHORITY,
    LineSpec,
    LineSpecAny,
    line_requested_rung,
)
from cruxible_client.contracts.procedures.models import ProcedureDefinitionV4
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


def graph_v4(accepted: AcceptedProcedure) -> AcceptedProcedure:
    """The same Procedure as a graph-v4 definition, which a Line v6 instantiates."""

    base = accepted.procedure
    definition = ProcedureDefinitionV4.model_validate(
        {**base.definition.model_dump(mode="python"), "graph_format": 4}
    )
    procedure = ProcedureArtifact.model_validate(
        {
            **base.model_dump(mode="python"),
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
        }
    )
    return AcceptedProcedure(
        path=accepted.path,
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


#: Pin roles only an embedded trigger policy carried.
_EMBEDDED_TRIGGER_PIN_ROLES = frozenset(
    {
        "trigger-cadence-policy",
        "trigger-capture-contract",
        "trigger-landing-filter",
        "trigger-window-policy",
    }
)


def as_v6(line: LineSpecAny) -> LineSpec:
    """The same Line with its embedded trigger removed: what compiler revision 32 accepts.

    Its authority is the verb its rung meant; any trigger pins go with the trigger.
    """

    value = line.model_dump(mode="python")
    for field in ("artifact_format", "trigger_policy", "requested_terminal_rung", "trigger_input"):
        value.pop(field, None)
    value["max_authority"] = RUNG_AUTHORITY[line_requested_rung(line)]
    value.setdefault("provider_implementation_closures", ())
    value["pins"] = tuple(pin for pin in line.pins if pin.role not in _EMBEDDED_TRIGGER_PIN_ROLES)
    return LineSpec.model_validate(value)


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
