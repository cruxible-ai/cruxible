"""Governed LineSpec artifact: one stable instantiation of an accepted Procedure."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    normalize_canonical,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.closure import (
    LineSlotBindingV1,
    ProcedurePinClosureError,
    close_procedure_pin_slots,
)
from cruxible_client.contracts.procedures.models import ExhaustTapNodeV3, SourceNodeV3
from cruxible_client.contracts.procedures.pin_expectations import (
    TRIGGER_CADENCE_POLICY,
    TRIGGER_CAPTURE_CONTRACT,
    TRIGGER_LANDING_FILTER,
    TRIGGER_WINDOW_POLICY,
    PinExpectation,
    validate_exact_pin_expectation,
)
from cruxible_client.contracts.semantic import SemanticAddress

_LINE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class LineSpecFormatError(PlaybillFormatError):
    """A LineSpec artifact, closure, or successor transition is invalid."""


class _StrictLineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _artifact_digest(value: str) -> str:
    ArtifactDigest.from_tagged(value)
    return value


class CadenceTriggerPolicyV1(_StrictLineModel):
    tag: Literal["playbill-cadence-trigger-v1"] = "playbill-cadence-trigger-v1"
    kind: Literal["cadence"] = "cadence"
    cadence_policy_digest: str

    _digest = field_validator("cadence_policy_digest")(_artifact_digest)


class CaptureLandingTriggerPolicyV1(_StrictLineModel):
    tag: Literal["playbill-capture-landing-trigger-v1"] = "playbill-capture-landing-trigger-v1"
    kind: Literal["capture_landing"] = "capture_landing"
    anchor_capture_contract_digest: str
    landing_filter_digest: str

    _digests = field_validator("anchor_capture_contract_digest", "landing_filter_digest")(
        _artifact_digest
    )


class WindowCloseTriggerPolicyV1(_StrictLineModel):
    tag: Literal["playbill-window-close-trigger-v1"] = "playbill-window-close-trigger-v1"
    kind: Literal["window_close"] = "window_close"
    window_policy_digest: str

    _digest = field_validator("window_policy_digest")(_artifact_digest)


class ManualTriggerPolicyV1(_StrictLineModel):
    tag: Literal["playbill-manual-trigger-v1"] = "playbill-manual-trigger-v1"
    kind: Literal["manual"] = "manual"


TriggerPolicyV1 = Annotated[
    CadenceTriggerPolicyV1
    | CaptureLandingTriggerPolicyV1
    | WindowCloseTriggerPolicyV1
    | ManualTriggerPolicyV1,
    Field(discriminator="kind"),
]


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


def _decimal_wrapper(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or tuple(value) != ("$decimal",):
        raise ValueError("LineSpec epsilon must be a canonical $decimal wrapper")
    spelling = value["$decimal"]
    if not isinstance(spelling, str):
        raise ValueError("LineSpec epsilon decimal must be text")
    try:
        decimal = Decimal(spelling)
    except InvalidOperation as exc:
        raise ValueError("LineSpec epsilon is not a decimal") from exc
    if not decimal.is_finite() or decimal < 0 or decimal > 1:
        raise ValueError("LineSpec epsilon must be in [0,1]")
    canonical = format(decimal, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if canonical in {"", "-0"}:
        canonical = "0"
    if spelling != canonical:
        raise ValueError("LineSpec epsilon decimal spelling is not canonical")
    return {"$decimal": spelling}


class LineSpecV1(_StrictLineModel):
    artifact_format: Literal["playbill-line-v1"] = "playbill-line-v1"
    identity: ArtifactIdentity
    occurrence_epoch: int = Field(ge=1, le=2**63 - 1)
    procedure: ArtifactPin
    parameters: object
    slot_bindings: tuple[LineSlotBindingV1, ...]
    trigger_policy: TriggerPolicyV1
    acquisition_policy: ArtifactPin | None = None
    requested_terminal_rung: Literal[1, 2, 3]
    budgets: object
    epsilon: object
    pins: tuple[ArtifactPin, ...]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("parameters", "budgets", mode="before")
    @classmethod
    def _canonical_objects(cls, value: object) -> object:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):
            raise ValueError("LineSpec parameters and budgets must be canonical objects")
        return normalized

    @field_validator("epsilon", mode="before")
    @classmethod
    def _epsilon(cls, value: object) -> object:
        return _decimal_wrapper(value)

    @field_validator("slot_bindings")
    @classmethod
    def _bindings(cls, value: tuple[LineSlotBindingV1, ...]) -> tuple[LineSlotBindingV1, ...]:
        names = tuple(item.slot_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("LineSpec slot bindings must be sorted and unique")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("LineSpec pins must be canonically sorted")
        keys = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(set(keys)) != len(keys):
            raise ValueError("LineSpec pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "LineSpecV1":
        if self.identity.kind != "Line" or not _LINE_NAME_RE.fullmatch(self.identity.name):
            raise ValueError("Line identity must be path-addressable and kind Line")
        if self.procedure.role != "procedure" or self.procedure.target.kind != "Procedure":
            raise ValueError("LineSpec procedure must be an exact role=procedure Procedure pin")
        if self.acquisition_policy is not None and (
            self.acquisition_policy.role != "acquisition-policy"
            or self.acquisition_policy.target.kind != "SourceAcquisitionPolicy"
        ):
            raise ValueError("LineSpec acquisition_policy pin has the wrong role or kind")
        required = {self.procedure}
        if self.acquisition_policy is not None:
            required.add(self.acquisition_policy)
        required.update(binding.artifact_pin for binding in self.slot_bindings)
        if not required.issubset(set(self.pins)):
            raise ValueError("LineSpec envelope pins do not contain its exact dependencies")
        for role, digest, expectation in _trigger_pin_requirements(self.trigger_policy):
            matches = tuple(
                pin for pin in self.pins if pin.role == role and pin.artifact_digest == digest
            )
            if len(matches) != 1:
                raise ValueError(
                    "LineSpec trigger digest lacks one exact role-named pin: "
                    f"role={role!r} digest={digest!r}"
                )
            validate_exact_pin_expectation(
                matches[0],
                expectation,
                location=f"LineSpec trigger pin {role!r}",
            )
        return self


def _trigger_pin_requirements(
    trigger: TriggerPolicyV1,
) -> tuple[tuple[str, str, PinExpectation], ...]:
    if isinstance(trigger, CadenceTriggerPolicyV1):
        return (
            (
                "trigger-cadence-policy",
                trigger.cadence_policy_digest,
                TRIGGER_CADENCE_POLICY,
            ),
        )
    if isinstance(trigger, CaptureLandingTriggerPolicyV1):
        return (
            (
                "trigger-capture-contract",
                trigger.anchor_capture_contract_digest,
                TRIGGER_CAPTURE_CONTRACT,
            ),
            (
                "trigger-landing-filter",
                trigger.landing_filter_digest,
                TRIGGER_LANDING_FILTER,
            ),
        )
    if isinstance(trigger, WindowCloseTriggerPolicyV1):
        return (
            (
                "trigger-window-policy",
                trigger.window_policy_digest,
                TRIGGER_WINDOW_POLICY,
            ),
        )
    return ()


def line_spec_path(name: str) -> str:
    if not _LINE_NAME_RE.fullmatch(name):
        raise LineSpecFormatError("Line identity is not path-addressable")
    return f"lines/{name}.json"


def render_line_spec(line: LineSpecV1) -> bytes:
    return pretty_canonical_bytes(line.model_dump(mode="json"))


def parse_line_spec(content: bytes, *, path: str) -> LineSpecV1:
    try:
        line = LineSpecV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LineSpecFormatError("LineSpec failed strict playbill-line-v1 validation") from exc
    if not artifact_path_matches(line_spec_path(line.identity.name), path):
        raise LineSpecFormatError("LineSpec identity/path disagreement")
    if artifact_bytes_for_path(render_line_spec(line), path) != content:
        raise LineSpecFormatError("LineSpec is not in canonical wire form")
    return line


def line_spec_digest(line: LineSpecV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        line.model_dump(mode="json"),
    )


class AcceptedLineSpecV1(_StrictLineModel):
    path: str
    line: LineSpecV1
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedLineSpecV1":
        if self.path != line_spec_path(self.line.identity.name):
            raise ValueError("accepted LineSpec path does not reproduce")
        if self.artifact_digest != line_spec_digest(self.line).tagged:
            raise ValueError("accepted LineSpec digest does not reproduce")
        return self


class LineSpecLawResultV1(_StrictLineModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> LineSpecLawResultV1:
    return LineSpecLawResultV1(
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


def _budget_int(budgets: object, key: str) -> int | None:
    if not isinstance(budgets, dict):
        return None
    value = budgets.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def evaluate_line_spec_law(
    line: LineSpecV1,
    *,
    path: str,
    procedure: AcceptedProcedureV1,
    interface_digests: dict[str, str],
    predecessor: AcceptedLineSpecV1 | None,
) -> LineSpecLawResultV1:
    if path != line_spec_path(line.identity.name):
        return _refusal(
            "playbill.line.path_mismatch", "Line identity/path disagreement.", path=path
        )
    if line.procedure.artifact_digest != procedure.artifact_digest:
        return _refusal(
            "playbill.line.procedure_pin_mismatch",
            "LineSpec does not pin the supplied accepted Procedure.",
            path=path,
        )
    try:
        close_procedure_pin_slots(
            procedure.procedure,
            bindings=line.slot_bindings,
            interface_digests=interface_digests,
        )
    except ProcedurePinClosureError as exc:
        return _refusal("playbill.line.slot_closure_failed", str(exc), path=path)
    definition = procedure.procedure.definition
    if line.requested_terminal_rung > definition.terminal_capability:
        return _refusal(
            "playbill.line.rung_exceeds_procedure_cap",
            "LineSpec requested terminal rung exceeds the Procedure hard cap.",
            path=path,
        )
    caps = definition.hard_caps
    limits = {
        "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
        "max_provider_calls": caps.max_provider_calls,
        "max_capture_bytes": caps.max_capture_bytes,
        "max_items": caps.max_items,
    }
    for key, hard_cap in limits.items():
        value = _budget_int(line.budgets, key)
        if value is None or value < 0 or value > hard_cap:
            return _refusal(
                "playbill.line.budget_exceeds_procedure_cap",
                f"LineSpec budget {key!r} is missing, invalid, or exceeds the Procedure cap.",
                path=path,
            )
    needs_acquisition = any(
        isinstance(node, SourceNodeV3 | ExhaustTapNodeV3) for node in definition.nodes
    )
    if needs_acquisition and line.acquisition_policy is None:
        return _refusal(
            "playbill.line.acquisition_policy_missing",
            "Source and exhaust paths require an exact SourceAcquisitionPolicy pin.",
            path=path,
        )
    if predecessor is None:
        if line.lifecycle.predecessor_digest is not None or line.occurrence_epoch != 1:
            return _refusal(
                "playbill.line.invalid_genesis",
                "Line genesis requires epoch 1 and no predecessor digest.",
                path=path,
            )
    else:
        if line.identity != predecessor.line.identity:
            return _refusal(
                "playbill.line.stable_identity_changed",
                "Line successor must retain stable identity.",
                path=path,
            )
        if line.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "playbill.line.predecessor_mismatch",
                "Line successor does not pin its exact predecessor.",
                path=path,
            )
        trigger_changed = line.trigger_policy != predecessor.line.trigger_policy
        expected_epoch = predecessor.line.occurrence_epoch + (1 if trigger_changed else 0)
        if line.occurrence_epoch != expected_epoch:
            return _refusal(
                "playbill.line.occurrence_epoch_mismatch",
                "Line occurrence epoch must advance exactly when trigger semantics change.",
                path=path,
            )
    return LineSpecLawResultV1(
        verdict="accepted",
        artifact_digest=line_spec_digest(line).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


__all__ = [
    "AcceptedLineSpecV1",
    "CadenceTriggerPolicyV1",
    "CaptureLandingTriggerPolicyV1",
    "LineSpecFormatError",
    "LineSpecLawResultV1",
    "LineSpecV1",
    "ManualTriggerPolicyV1",
    "TriggerPolicyV1",
    "WindowCloseTriggerPolicyV1",
    "evaluate_line_spec_law",
    "line_spec_digest",
    "line_spec_path",
    "parse_line_spec",
    "render_line_spec",
]
