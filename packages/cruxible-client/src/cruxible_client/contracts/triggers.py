"""Governed Trigger artifact: one schedule aimed at a Line or an internal action.

Every trigger has one governed home. A Trigger holds when something happens
(a cadence, a cron calendar, a generation acceptance, a Capture landing, or an
observation window closing) and what it sets off: a Line, named by identity so
an ordinary Line successor never strands it,
or one internal action from the code's action registry. Triggers are changed
and retired through ordinary proposals; a Line no longer embeds its own.

What a Trigger may fire on is decided by what its target needs: a Line that
binds its triggering Capture, and an internal action that declares a Capture
input, each need a schedule that fires on that event; a target that needs no
event takes any schedule the target admits. In v1 an internal action admits time
schedules (cadence, cron) and generation acceptance; Capture schedules come later, by
admitting them here and in the trigger journal, with the input rule unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
    ArtifactRef,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.cron import CRON_UTC_HINT, CronExpressionError, parse_cron
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelector,
    CaptureEventWindow,
    ObservationWindow,
)
from cruxible_client.contracts.semantic import SemanticAddress

_TRIGGER_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")

#: The role a Trigger's Line reference carries.
TRIGGER_LINE_REF_ROLE: Final = "line"
#: The role of the exact CaptureContract pin an event schedule carries.
TRIGGER_CAPTURE_CONTRACT_PIN_ROLE: Final = "trigger-capture-contract"


class TriggerFormatError(FormatError):
    """A Trigger artifact or its successor transition is invalid."""


class _StrictTriggerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class CadenceSchedule(_StrictTriggerModel):
    """Fire one interval after the last fire; a new schedule fires on its first tick."""

    kind: Literal["cadence"] = "cadence"
    interval_seconds: int = Field(gt=0, description="Reads VALIDITY WINDOW.")


class CronSchedule(_StrictTriggerModel):
    """Fire at each instant a standard five-field cron expression names, in UTC.

    ``minute hour day-of-month month day-of-week``, always read as UTC: no host
    timezone database enters an instant, and a schedule names no timezone. The
    Trigger law refuses an expression the grammar does not admit; see
    ``cruxible_client.contracts.cron``.
    """

    kind: Literal["cron"] = "cron"
    expression: str = Field(
        min_length=1,
        max_length=128,
        examples=["0 14 * * 1-5"],
        description=(
            "Five fields, minute hour day-of-month month day-of-week, evaluated in UTC. "
            + CRON_UTC_HINT
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _utc_only(cls, data: object) -> object:
        if isinstance(data, dict):
            for field in ("timezone", "time_zone", "tz", "zone", "utc_offset"):
                if field in data:
                    raise PydanticCustomError(
                        "cron_utc_only",
                        "A cron schedule names no {field}: it is evaluated in UTC. {hint}",
                        {"field": field, "hint": CRON_UTC_HINT},
                    )
        return data


class CaptureLandingSchedule(_StrictTriggerModel):
    """Fire once for each retained Capture landing under one exact CaptureContract."""

    kind: Literal["capture_landing"] = "capture_landing"
    event: CaptureEventSelector


class WindowCloseSchedule(_StrictTriggerModel):
    """Fire when a fixed window, or a window anchored on a Capture landing, closes."""

    kind: Literal["window_close"] = "window_close"
    window: ObservationWindow


class GenerationAcceptedSchedule(_StrictTriggerModel):
    """Fire once at the latest accepted head after it moves, coalescing a burst."""

    kind: Literal["generation_accepted"] = "generation_accepted"


TriggerSchedule: TypeAlias = Annotated[
    CadenceSchedule
    | CronSchedule
    | CaptureLandingSchedule
    | WindowCloseSchedule
    | GenerationAcceptedSchedule,
    Field(discriminator="kind"),
]


def schedule_is_timed(schedule: TriggerSchedule) -> bool:
    """Whether a schedule fires on time alone rather than on a Capture or window.

    Every kind is named: a kind added later fails here until it is classified,
    never falling through as one or the other.
    """

    if isinstance(schedule, CadenceSchedule | CronSchedule):
        return True
    if isinstance(
        schedule, CaptureLandingSchedule | WindowCloseSchedule | GenerationAcceptedSchedule
    ):
        return False
    raise TriggerFormatError(f"unsupported Trigger schedule kind {schedule.kind!r}")


class NoTriggerInput(_StrictTriggerModel):
    """The target needs no event, so every schedule satisfies it."""

    kind: Literal["none"] = "none"


class CaptureEventInput(_StrictTriggerModel):
    """The target needs a Capture event: its Trigger's schedule must fire on one.

    ``event`` names the exact event when only that one is acceptable, as for a
    Line that binds its triggering Capture to a Source input.
    """

    kind: Literal["capture_event"] = "capture_event"
    event: CaptureEventSelector | None = None


TriggerInputRecord: TypeAlias = Annotated[
    NoTriggerInput | CaptureEventInput,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class InternalActionSpec:
    """One internal action a Trigger may fire, and the worker that performs it.

    Adding an action is one entry here and the consumer part that follows its
    fires in the trigger journal; the Trigger law, authoring and the next
    status read this registry, never a list of names of their own.
    """

    name: str
    #: The event a fire must carry; the Trigger law checks the schedule supplies it.
    input: TriggerInputRecord
    #: What performing the action may change, never governed state.
    effect: Literal["findings", "workspace_output"]
    #: The consumer kind and part that follow this action's fires.
    consumer: str
    part: str


INTERNAL_ACTIONS: Final[Mapping[str, InternalActionSpec]] = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            InternalActionSpec(
                name="floor.refresh",
                input=NoTriggerInput(),
                effect="workspace_output",
                consumer="floor",
                part="deliver",
            ),
            InternalActionSpec(
                name="evidence.sweep",
                input=NoTriggerInput(),
                effect="findings",
                consumer="next",
                part="evidence",
            ),
            InternalActionSpec(
                name="prediction.anchor_retry",
                input=NoTriggerInput(),
                effect="findings",
                consumer="next",
                part="prediction",
            ),
            InternalActionSpec(
                name="curation.detect",
                input=NoTriggerInput(),
                effect="findings",
                consumer="next",
                part="curation",
            ),
        )
    }
)

#: An internal action named by a Trigger. Its shape is checked here; whether it
#: is registered is the Trigger law's to judge, so an unknown name is a typed
#: refusal at acceptance rather than an opaque format failure.
InternalActionName: TypeAlias = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$",
        max_length=128,
        description="A registered internal action: " + ", ".join(INTERNAL_ACTIONS) + ".",
        examples=list(INTERNAL_ACTIONS),
    ),
]


class LineTarget(_StrictTriggerModel):
    """Run one Line, whatever version of it is accepted when the Trigger fires."""

    kind: Literal["line"] = "line"
    line: ArtifactRef

    @model_validator(mode="after")
    def _line(self) -> LineTarget:
        if self.line.role != TRIGGER_LINE_REF_ROLE or self.line.target.kind != "Line":
            raise ValueError("a Line target is a role=line reference to a Line identity")
        return self


class ActionTarget(_StrictTriggerModel):
    """Fire one internal action; the worker that owns it follows its fires."""

    kind: Literal["action"] = "action"
    action: InternalActionName


TriggerTarget: TypeAlias = Annotated[
    LineTarget | ActionTarget,
    Field(discriminator="kind"),
]


def schedule_capture_selector(schedule: TriggerSchedule) -> CaptureEventSelector | None:
    """The exact Capture event a schedule fires on, if it fires on one."""

    if isinstance(schedule, CaptureLandingSchedule):
        return schedule.event
    if isinstance(schedule, WindowCloseSchedule):
        window = schedule.window
        return window.event if isinstance(window, CaptureEventWindow) else None
    if schedule_is_timed(schedule) or isinstance(schedule, GenerationAcceptedSchedule):
        return None
    raise TriggerFormatError(f"unsupported Trigger schedule kind {schedule.kind!r}")


def schedule_satisfies_input(schedule: TriggerSchedule, required: TriggerInputRecord) -> bool:
    """Whether every fire of ``schedule`` carries the event ``required`` names.

    The one rule for every target: a Line's accepted event and an internal
    action's declared input are both judged here.
    """

    if isinstance(required, NoTriggerInput):
        return True
    selector = schedule_capture_selector(schedule)
    return selector is not None and (required.event is None or selector == required.event)


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


def trigger_schedule_pins(schedule: TriggerSchedule) -> tuple[ArtifactPin, ...]:
    """The exact pins a schedule requires: the CaptureContract an event names."""

    selector = schedule_capture_selector(schedule)
    if selector is None:
        return ()
    return (
        ArtifactPin(
            role=TRIGGER_CAPTURE_CONTRACT_PIN_ROLE,
            target=selector.capture_contract_identity,
            artifact_digest=selector.capture_contract_digest,
        ),
    )


class Trigger(_StrictTriggerModel):
    """One schedule and the one Line or internal action it sets off."""

    artifact_format: Literal["playbill-trigger-v1"] = "playbill-trigger-v1"
    identity: ArtifactIdentity
    schedule: TriggerSchedule
    target: TriggerTarget
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @model_validator(mode="after")
    def _shape(self) -> Trigger:
        if self.identity.kind != "Trigger" or not _TRIGGER_NAME_RE.fullmatch(self.identity.name):
            raise ValueError("Trigger identity must be path-addressable and kind Trigger")
        # The pins are exactly what the schedule names, so closure moves a
        # Trigger with the CaptureContract version its event matches.
        if self.pins != tuple(sorted(trigger_schedule_pins(self.schedule), key=_pin_key)):
            raise ValueError("Trigger pins must be exactly the CaptureContract its schedule names")
        return self

    @property
    def line(self) -> ArtifactIdentity | None:
        return self.target.line.target if isinstance(self.target, LineTarget) else None

    @property
    def action(self) -> str | None:
        return self.target.action if isinstance(self.target, ActionTarget) else None


def trigger_path(name: str) -> str:
    if not _TRIGGER_NAME_RE.fullmatch(name):
        raise TriggerFormatError("Trigger identity is not path-addressable")
    return f"triggers/{name}.json"


def render_trigger(trigger: Trigger) -> bytes:
    return pretty_canonical_bytes(trigger.model_dump(mode="json"))


def trigger_digest(trigger: Trigger) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest, "playbill-trigger-artifact-v1", trigger.model_dump(mode="json")
    )


def parse_trigger(
    content: bytes, *, path: str, codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC
) -> Trigger:
    try:
        trigger = Trigger.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TriggerFormatError("Trigger failed strict validation") from exc
    if not artifact_path_matches(trigger_path(trigger.identity.name), path, codec=codec):
        raise TriggerFormatError("Trigger identity/path disagreement")
    if artifact_bytes_for_path(render_trigger(trigger), path, codec=codec) != content:
        raise TriggerFormatError("Trigger is not in canonical wire form")
    return trigger


class AcceptedTrigger(_StrictTriggerModel):
    path: str
    trigger: Trigger
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> AcceptedTrigger:
        if self.path != trigger_path(self.trigger.identity.name):
            raise ValueError("accepted Trigger path does not reproduce")
        if self.artifact_digest != trigger_digest(self.trigger).tagged:
            raise ValueError("accepted Trigger digest does not reproduce")
        return self


class TriggerLawResult(_StrictTriggerModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @field_validator("artifact_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value


def _refusal(code: str, message: str, *, path: str) -> TriggerLawResult:
    return TriggerLawResult(
        verdict="refused",
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity="error",
                message=message,
                subject=SemanticAddress.whole_artifact(path),
            ),
        ),
    )


def evaluate_trigger_law(
    trigger: Trigger,
    *,
    path: str,
    predecessor: AcceptedTrigger | None,
    target_line_live: bool | None = None,
    target_line_input: TriggerInputRecord | None = None,
    actions: Mapping[str, InternalActionSpec] = INTERNAL_ACTIONS,
) -> TriggerLawResult:
    """Judge one Trigger against its predecessor and what it aims at.

    ``target_line_live`` is whether the named Line is live in the final
    candidate (None when the target is an internal action), and
    ``target_line_input`` the event that Line accepts: a Line that binds its
    triggering Capture to a Source input accepts exactly its declared event.
    An internal action must be registered in ``actions`` and takes the input its
    entry declares. Either way the schedule must supply the target's input. A
    retiring Trigger is judged only for its succession: it sets off nothing, so
    what it named no longer matters.
    """

    if path != trigger_path(trigger.identity.name):
        return _refusal(
            "cruxible.trigger.path_mismatch", "Trigger identity/path disagreement.", path=path
        )
    if predecessor is None:
        if trigger.lifecycle.predecessor_digest is not None:
            return _refusal(
                "cruxible.trigger.invalid_genesis",
                "A new Trigger cannot name a predecessor.",
                path=path,
            )
    else:
        if trigger.identity != predecessor.trigger.identity:
            return _refusal(
                "cruxible.trigger.stable_identity_changed",
                "A Trigger successor must retain its stable identity.",
                path=path,
            )
        if trigger.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "cruxible.trigger.predecessor_mismatch",
                "A Trigger successor does not pin its exact predecessor.",
                path=path,
            )
        if trigger.lifecycle.state == "live" and predecessor.trigger.lifecycle.state == "retired":
            return _refusal(
                "cruxible.trigger.revival_refused",
                "A retired Trigger cannot be revived; propose a new Trigger identity instead.",
                path=path,
            )
    if trigger.lifecycle.state == "live":
        if isinstance(trigger.schedule, CronSchedule):
            try:
                parse_cron(trigger.schedule.expression)
            except CronExpressionError as exc:
                return _refusal(
                    "cruxible.trigger.cron_invalid",
                    f"Cron schedule is not valid: {exc}. Use five UTC fields (minute hour "
                    "day-of-month month day-of-week) of numbers, ranges, steps or lists.",
                    path=path,
                )
        if isinstance(trigger.target, ActionTarget):
            spec = actions.get(trigger.target.action)
            if spec is None:
                return _refusal(
                    "cruxible.trigger.action_unknown",
                    f"Internal action {trigger.target.action!r} is not registered; a Trigger "
                    "may fire one of: " + ", ".join(sorted(actions)) + ".",
                    path=path,
                )
            if not (
                schedule_is_timed(trigger.schedule)
                or isinstance(trigger.schedule, GenerationAcceptedSchedule)
            ):
                return _refusal(
                    "cruxible.trigger.schedule_unsupported_for_action",
                    f"Internal action {spec.name!r} takes cadence, cron or generation_accepted "
                    "in this "
                    f"version; a {trigger.schedule.kind} schedule for an internal action is "
                    "not supported yet.",
                    path=path,
                )
            target, required = f"Internal action {spec.name!r}", spec.input
        else:
            if not target_line_live:
                return _refusal(
                    "cruxible.trigger.target_line_unavailable",
                    f"Trigger target {trigger.target.line.target.qualified!r} is not a live "
                    "accepted Line in this candidate; accept the Line first, or in this "
                    "ChangeSet.",
                    path=path,
                )
            target = f"Line {trigger.target.line.target.qualified!r}"
            required = target_line_input or NoTriggerInput()
        if not schedule_satisfies_input(trigger.schedule, required):
            assert isinstance(required, CaptureEventInput)
            needed = (
                "its declared trigger_event exactly"
                if required.event is not None
                else "a Capture event"
            )
            return _refusal(
                "cruxible.trigger.event_not_accepted",
                f"{target} needs {needed} as its input; this Trigger's schedule does not "
                "fire on it.",
                path=path,
            )
    return TriggerLawResult(
        verdict="accepted",
        artifact_digest=trigger_digest(trigger).tagged,
        required_tier="governed_write",
    )


__all__ = [
    "INTERNAL_ACTIONS",
    "TRIGGER_CAPTURE_CONTRACT_PIN_ROLE",
    "TRIGGER_LINE_REF_ROLE",
    "AcceptedTrigger",
    "ActionTarget",
    "CadenceSchedule",
    "CaptureEventInput",
    "CaptureLandingSchedule",
    "CronSchedule",
    "InternalActionName",
    "InternalActionSpec",
    "LineTarget",
    "NoTriggerInput",
    "TriggerFormatError",
    "TriggerInputRecord",
    "TriggerLawResult",
    "TriggerSchedule",
    "TriggerTarget",
    "Trigger",
    "WindowCloseSchedule",
    "evaluate_trigger_law",
    "parse_trigger",
    "render_trigger",
    "schedule_capture_selector",
    "GenerationAcceptedSchedule",
    "schedule_is_timed",
    "schedule_satisfies_input",
    "trigger_digest",
    "trigger_path",
    "trigger_schedule_pins",
]
