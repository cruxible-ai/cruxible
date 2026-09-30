"""Governed Trigger artifact: one schedule aimed at a Line or an internal action.

Every trigger has one governed home. A Trigger holds when something happens
(a cadence, a Capture landing, an observation window closing) and what it sets
off: a Line, named by identity so an ordinary Line successor never strands it,
or one internal action from a closed registry. Triggers are changed and
retired through ordinary proposals; a Line no longer embeds its own.

An internal action takes a cadence only: the daemon's own maintenance runs on
time, never on a Capture it would then have to interpret.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelectorV1,
    CaptureEventWindowV1,
    ObservationWindowV1,
)
from cruxible_client.contracts.semantic import SemanticAddress

_TRIGGER_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")

#: The internal actions a Trigger may aim at. Closed: a worker follows fires
#: of one of these actions, and an unknown action would fire into nothing.
InternalAction: TypeAlias = Literal["evidence.sweep", "prediction.anchor_retry"]
INTERNAL_ACTIONS: Final[tuple[InternalAction, ...]] = ("evidence.sweep", "prediction.anchor_retry")

#: The role a Trigger's Line reference carries.
TRIGGER_LINE_REF_ROLE: Final = "line"
#: The role of the exact CaptureContract pin an event schedule carries.
TRIGGER_CAPTURE_CONTRACT_PIN_ROLE: Final = "trigger-capture-contract"


class TriggerFormatError(PlaybillFormatError):
    """A Trigger artifact or its successor transition is invalid."""


class _StrictTriggerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class CadenceScheduleV1(_StrictTriggerModel):
    """Fire one interval after the last fire; a new schedule fires on its first tick."""

    kind: Literal["cadence"] = "cadence"
    interval_seconds: int = Field(gt=0, description="Reads VALIDITY WINDOW.")


class CaptureLandingScheduleV1(_StrictTriggerModel):
    """Fire once for each retained Capture landing under one exact CaptureContract."""

    kind: Literal["capture_landing"] = "capture_landing"
    event: CaptureEventSelectorV1


class WindowCloseScheduleV1(_StrictTriggerModel):
    """Fire when a fixed window, or a window anchored on a Capture landing, closes."""

    kind: Literal["window_close"] = "window_close"
    window: ObservationWindowV1


TriggerScheduleV1: TypeAlias = Annotated[
    CadenceScheduleV1 | CaptureLandingScheduleV1 | WindowCloseScheduleV1,
    Field(discriminator="kind"),
]


class LineTargetV1(_StrictTriggerModel):
    """Run one Line, whatever version of it is accepted when the Trigger fires."""

    kind: Literal["line"] = "line"
    line: ArtifactRef

    @model_validator(mode="after")
    def _line(self) -> LineTargetV1:
        if self.line.role != TRIGGER_LINE_REF_ROLE or self.line.target.kind != "Line":
            raise ValueError("a Line target is a role=line reference to a Line identity")
        return self


class ActionTargetV1(_StrictTriggerModel):
    """Fire one internal action; the worker that owns it follows its fires."""

    kind: Literal["action"] = "action"
    action: InternalAction


TriggerTargetV1: TypeAlias = Annotated[
    LineTargetV1 | ActionTargetV1,
    Field(discriminator="kind"),
]


def schedule_capture_selector(schedule: TriggerScheduleV1) -> CaptureEventSelectorV1 | None:
    """The exact Capture event a schedule fires on, if it fires on one."""

    if isinstance(schedule, CaptureLandingScheduleV1):
        return schedule.event
    if isinstance(schedule, WindowCloseScheduleV1) and isinstance(
        schedule.window, CaptureEventWindowV1
    ):
        return schedule.window.event
    return None


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


def trigger_schedule_pins(schedule: TriggerScheduleV1) -> tuple[ArtifactPin, ...]:
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


class TriggerV1(_StrictTriggerModel):
    """One schedule and the one Line or internal action it sets off."""

    artifact_format: Literal["playbill-trigger-v1"] = "playbill-trigger-v1"
    identity: ArtifactIdentity
    schedule: TriggerScheduleV1
    target: TriggerTargetV1
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @model_validator(mode="after")
    def _shape(self) -> TriggerV1:
        if self.identity.kind != "Trigger" or not _TRIGGER_NAME_RE.fullmatch(self.identity.name):
            raise ValueError("Trigger identity must be path-addressable and kind Trigger")
        # The pins are exactly what the schedule names, so closure moves a
        # Trigger with the CaptureContract version its event matches.
        if self.pins != tuple(sorted(trigger_schedule_pins(self.schedule), key=_pin_key)):
            raise ValueError("Trigger pins must be exactly the CaptureContract its schedule names")
        return self

    @property
    def line(self) -> ArtifactIdentity | None:
        return self.target.line.target if isinstance(self.target, LineTargetV1) else None

    @property
    def action(self) -> InternalAction | None:
        return self.target.action if isinstance(self.target, ActionTargetV1) else None


def trigger_path(name: str) -> str:
    if not _TRIGGER_NAME_RE.fullmatch(name):
        raise TriggerFormatError("Trigger identity is not path-addressable")
    return f"triggers/{name}.json"


def render_trigger(trigger: TriggerV1) -> bytes:
    return pretty_canonical_bytes(trigger.model_dump(mode="json"))


def trigger_digest(trigger: TriggerV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest, "playbill-trigger-artifact-v1", trigger.model_dump(mode="json")
    )


def parse_trigger(
    content: bytes, *, path: str, codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC
) -> TriggerV1:
    try:
        trigger = TriggerV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TriggerFormatError("Trigger failed strict validation") from exc
    if not artifact_path_matches(trigger_path(trigger.identity.name), path, codec=codec):
        raise TriggerFormatError("Trigger identity/path disagreement")
    if artifact_bytes_for_path(render_trigger(trigger), path, codec=codec) != content:
        raise TriggerFormatError("Trigger is not in canonical wire form")
    return trigger


class AcceptedTriggerV1(_StrictTriggerModel):
    path: str
    trigger: TriggerV1
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> AcceptedTriggerV1:
        if self.path != trigger_path(self.trigger.identity.name):
            raise ValueError("accepted Trigger path does not reproduce")
        if self.artifact_digest != trigger_digest(self.trigger).tagged:
            raise ValueError("accepted Trigger digest does not reproduce")
        return self


class TriggerLawResultV1(_StrictTriggerModel):
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


def _refusal(code: str, message: str, *, path: str) -> TriggerLawResultV1:
    return TriggerLawResultV1(
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
    trigger: TriggerV1,
    *,
    path: str,
    predecessor: AcceptedTriggerV1 | None,
    target_line_live: bool | None = None,
    accepted_event: CaptureEventSelectorV1 | None = None,
    target_line_binds_event: bool = False,
) -> TriggerLawResultV1:
    """Judge one Trigger against its predecessor and the Line it aims at.

    ``target_line_live`` is whether the named Line is live in the final
    candidate (None when the target is an internal action). A Line that binds
    its triggering Capture to a Source input (``target_line_binds_event``)
    accepts exactly ``accepted_event``; a Trigger aimed at it must fire on that
    event. A retiring Trigger is judged only for its succession: it sets off
    nothing, so what it named no longer matters.
    """

    if path != trigger_path(trigger.identity.name):
        return _refusal(
            "playbill.trigger.path_mismatch", "Trigger identity/path disagreement.", path=path
        )
    if predecessor is None:
        if trigger.lifecycle.predecessor_digest is not None:
            return _refusal(
                "playbill.trigger.invalid_genesis",
                "A new Trigger cannot name a predecessor.",
                path=path,
            )
    else:
        if trigger.identity != predecessor.trigger.identity:
            return _refusal(
                "playbill.trigger.stable_identity_changed",
                "A Trigger successor must retain its stable identity.",
                path=path,
            )
        if trigger.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "playbill.trigger.predecessor_mismatch",
                "A Trigger successor does not pin its exact predecessor.",
                path=path,
            )
        if trigger.lifecycle.state == "live" and predecessor.trigger.lifecycle.state == "retired":
            return _refusal(
                "playbill.trigger.revival_refused",
                "A retired Trigger cannot be revived; propose a new Trigger identity instead.",
                path=path,
            )
    if trigger.lifecycle.state == "live":
        if isinstance(trigger.target, ActionTargetV1):
            if not isinstance(trigger.schedule, CadenceScheduleV1):
                return _refusal(
                    "playbill.trigger.action_schedule_unsupported",
                    f"Internal action {trigger.target.action!r} runs on a cadence schedule only.",
                    path=path,
                )
        else:
            if not target_line_live:
                return _refusal(
                    "playbill.trigger.target_line_unavailable",
                    f"Trigger target {trigger.target.line.target.qualified!r} is not a live "
                    "accepted Line in this candidate; accept the Line first, or in this "
                    "ChangeSet.",
                    path=path,
                )
            if target_line_binds_event and schedule_capture_selector(trigger.schedule) != (
                accepted_event
            ):
                return _refusal(
                    "playbill.trigger.event_not_accepted",
                    f"Line {trigger.target.line.target.qualified!r} binds its triggering "
                    "Capture to a Source input and accepts only its declared trigger_event; "
                    "this Trigger's schedule does not fire on that exact event.",
                    path=path,
                )
    return TriggerLawResultV1(
        verdict="accepted",
        artifact_digest=trigger_digest(trigger).tagged,
        required_tier="governed_write",
    )


__all__ = [
    "INTERNAL_ACTIONS",
    "TRIGGER_CAPTURE_CONTRACT_PIN_ROLE",
    "TRIGGER_LINE_REF_ROLE",
    "AcceptedTriggerV1",
    "ActionTargetV1",
    "CadenceScheduleV1",
    "CaptureLandingScheduleV1",
    "InternalAction",
    "LineTargetV1",
    "TriggerFormatError",
    "TriggerLawResultV1",
    "TriggerScheduleV1",
    "TriggerTargetV1",
    "TriggerV1",
    "WindowCloseScheduleV1",
    "evaluate_trigger_law",
    "parse_trigger",
    "render_trigger",
    "schedule_capture_selector",
    "trigger_digest",
    "trigger_path",
    "trigger_schedule_pins",
]
