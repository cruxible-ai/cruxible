"""Governed multi-source acquisition policy and deterministic selection planner."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    Sha256Value,
    artifact_bytes_for_path,
    artifact_path_matches,
    canonical_bytes,
    normalize_canonical,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.capture_journal import CaptureLandingEventV1
from cruxible_client.contracts.captures import (
    PLAYBILL_CAPTURE_COMPONENTS,
    CanonicalDurationV1,
    CaptureEnvelopeV1,
    CaptureSelectionBudgetV1,
    capture_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.semantic import SemanticAddress

AcquisitionFailureBehaviorV1 = Literal["refuse", "omit_optional", "declared_conservative_default"]
_INPUT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_POLICY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class SourceAcquisitionPolicyError(PlaybillFormatError):
    """A SourceAcquisitionPolicy artifact or acceptance transition is invalid."""


class _StrictAcquisitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if values != tuple(sorted(set(values), key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{label} must be sorted and unique")
    return values


class InputAcquisitionRuleV1(_StrictAcquisitionModel):
    tag: Literal["playbill-input-acquisition-rule-v1"] = "playbill-input-acquisition-rule-v1"
    input_name: str
    requirement: Literal["required", "optional", "conservative_default"]
    permitted_replayability: tuple[Literal["exact", "attested_only"], ...]
    max_age: CanonicalDurationV1 | None = None
    correlation_keys: tuple[str, ...] = ()
    join_semantics_digest: str | None = None
    on_unavailable: AcquisitionFailureBehaviorV1
    on_stale: AcquisitionFailureBehaviorV1
    on_oversized: AcquisitionFailureBehaviorV1
    on_conflict: Literal["preserve", "refuse"]
    conservative_default: object | None = None

    @field_validator("input_name")
    @classmethod
    def _input_name(cls, value: str) -> str:
        if not _INPUT_RE.fullmatch(value):
            raise ValueError("acquisition input_name must be canonical")
        return value

    @field_validator("permitted_replayability")
    @classmethod
    def _replayability(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("permitted replayability must not be empty")
        return _sorted_unique(value, label="permitted replayability")

    @field_validator("correlation_keys")
    @classmethod
    def _correlation(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="correlation keys")

    @field_validator("join_semantics_digest")
    @classmethod
    def _join_digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @field_validator("conservative_default", mode="before")
    @classmethod
    def _default(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @model_validator(mode="after")
    def _shape(self) -> "InputAcquisitionRuleV1":
        behaviors = (self.on_unavailable, self.on_stale, self.on_oversized)
        if self.requirement == "required" and any(item != "refuse" for item in behaviors):
            raise ValueError("required inputs must refuse on unavailable, stale, or oversized")
        if self.requirement == "optional" and self.conservative_default is not None:
            raise ValueError("optional inputs cannot carry a conservative default")
        if self.requirement == "conservative_default":
            if self.conservative_default is None:
                raise ValueError("conservative-default inputs require an exact default value")
            if "declared_conservative_default" not in behaviors:
                raise ValueError("conservative-default input must declare when its default applies")
        elif "declared_conservative_default" in behaviors:
            raise ValueError("only conservative-default inputs may select a default")
        if self.correlation_keys and self.join_semantics_digest is None:
            raise ValueError("correlated acquisition requires exact join semantics")
        return self


class IndependentCoherenceV1(_StrictAcquisitionModel):
    tag: Literal["playbill-independent-coherence-v1"] = "playbill-independent-coherence-v1"
    kind: Literal["independent"] = "independent"


class BoundedWindowCoherenceV1(_StrictAcquisitionModel):
    tag: Literal["playbill-bounded-window-coherence-v1"] = "playbill-bounded-window-coherence-v1"
    kind: Literal["bounded_window"] = "bounded_window"
    max_cross_source_skew: CanonicalDurationV1


class DeclaredSnapshotGroupCoherenceV1(_StrictAcquisitionModel):
    tag: Literal["playbill-declared-snapshot-group-coherence-v1"] = (
        "playbill-declared-snapshot-group-coherence-v1"
    )
    kind: Literal["declared_snapshot_group"] = "declared_snapshot_group"
    coordinate_grammar_digest: str
    proof_adapter_digest: str

    @field_validator("coordinate_grammar_digest", "proof_adapter_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


SourceCoherenceV1 = Annotated[
    IndependentCoherenceV1 | BoundedWindowCoherenceV1 | DeclaredSnapshotGroupCoherenceV1,
    Field(discriminator="kind"),
]


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes]:
    return pin.role.encode("utf-8"), pin.target.qualified.encode("utf-8")


class SourceAcquisitionPolicyV1(_StrictAcquisitionModel):
    artifact_format: Literal["playbill-source-acquisition-policy-v1"] = (
        "playbill-source-acquisition-policy-v1"
    )
    identity: ArtifactIdentity
    inputs: tuple[InputAcquisitionRuleV1, ...]
    coherence: SourceCoherenceV1
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("inputs")
    @classmethod
    def _inputs(
        cls, value: tuple[InputAcquisitionRuleV1, ...]
    ) -> tuple[InputAcquisitionRuleV1, ...]:
        names = tuple(item.input_name for item in value)
        if not value or names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("acquisition inputs must be nonempty, sorted, and unique")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("acquisition policy pins must be sorted")
        return value

    @model_validator(mode="after")
    def _identity(self) -> "SourceAcquisitionPolicyV1":
        if self.identity.kind != "SourceAcquisitionPolicy" or not _POLICY_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("acquisition policy identity kind is invalid")
        return self


def acquisition_policy_digest(policy: SourceAcquisitionPolicyV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        policy.model_dump(mode="json"),
    )


def acquisition_policy_path(name: str) -> str:
    if not _POLICY_NAME_RE.fullmatch(name):
        raise SourceAcquisitionPolicyError("SourceAcquisitionPolicy is not path-addressable")
    return f"source-acquisition-policies/{name}.json"


def render_acquisition_policy(policy: SourceAcquisitionPolicyV1) -> bytes:
    return pretty_canonical_bytes(policy.model_dump(mode="json"))


def parse_acquisition_policy(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> SourceAcquisitionPolicyV1:
    try:
        policy = SourceAcquisitionPolicyV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceAcquisitionPolicyError(
            "SourceAcquisitionPolicy failed strict v1 validation"
        ) from exc
    if not artifact_path_matches(acquisition_policy_path(policy.identity.name), path, codec=codec):
        raise SourceAcquisitionPolicyError("SourceAcquisitionPolicy identity/path disagreement")
    if artifact_bytes_for_path(render_acquisition_policy(policy), path, codec=codec) != content:
        raise SourceAcquisitionPolicyError("SourceAcquisitionPolicy is not canonical")
    return policy


class AcceptedSourceAcquisitionPolicyV1(_StrictAcquisitionModel):
    path: str
    policy: SourceAcquisitionPolicyV1
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedSourceAcquisitionPolicyV1":
        if self.path != acquisition_policy_path(self.policy.identity.name) or (
            self.artifact_digest != acquisition_policy_digest(self.policy).tagged
        ):
            raise ValueError("accepted SourceAcquisitionPolicy does not reproduce")
        return self


class SourceAcquisitionPolicyLawResultV1(_StrictAcquisitionModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _law_refusal(
    code: str,
    message: str,
    *,
    path: str,
) -> SourceAcquisitionPolicyLawResultV1:
    return SourceAcquisitionPolicyLawResultV1(
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


def evaluate_acquisition_policy_law(
    policy: SourceAcquisitionPolicyV1,
    *,
    path: str,
    predecessor: AcceptedSourceAcquisitionPolicyV1 | None,
) -> SourceAcquisitionPolicyLawResultV1:
    if path != acquisition_policy_path(policy.identity.name):
        return _law_refusal(
            "playbill.acquisition_policy.path_mismatch",
            "SourceAcquisitionPolicy identity/path disagreement.",
            path=path,
        )
    if predecessor is None and policy.lifecycle.predecessor_digest is not None:
        return _law_refusal(
            "playbill.acquisition_policy.predecessor_missing",
            "A new SourceAcquisitionPolicy cannot name a predecessor.",
            path=path,
        )
    if predecessor is not None:
        if policy.identity != predecessor.policy.identity or (
            policy.lifecycle.predecessor_digest != predecessor.artifact_digest
        ):
            return _law_refusal(
                "playbill.acquisition_policy.predecessor_mismatch",
                "SourceAcquisitionPolicy successor identity or predecessor differs.",
                path=path,
            )
    if isinstance(policy.coherence, DeclaredSnapshotGroupCoherenceV1):
        required = (
            ("coordinate-grammar", policy.coherence.coordinate_grammar_digest),
            ("proof-adapter", policy.coherence.proof_adapter_digest),
        )
        resolved = all(
            any(
                pin.role == role
                and pin.artifact_digest == digest
                and PLAYBILL_CAPTURE_COMPONENTS.resolves(pin)
                for pin in policy.pins
            )
            for role, digest in required
        )
        if not resolved:
            return _law_refusal(
                "playbill.acquisition_policy.snapshot_registry_unresolved",
                "Declared snapshot coherence requires registered exact grammar and "
                "proof-adapter pins.",
                path=path,
            )
    return SourceAcquisitionPolicyLawResultV1(
        verdict="accepted",
        artifact_digest=acquisition_policy_digest(policy).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


class AcquisitionCandidateV1(_StrictAcquisitionModel):
    tag: Literal["playbill-acquisition-candidate-v1"] = "playbill-acquisition-candidate-v1"
    input_name: str
    envelope: CaptureEnvelopeV1
    capture_digest: str
    landing_event: CaptureLandingEventV1
    current_replay_available: bool
    selection_budget: CaptureSelectionBudgetV1
    selected_bytes: int = Field(ge=0)
    selected_rows: int = Field(ge=0)
    selected_items: int = Field(ge=0)
    conflict_key: str | None = None
    snapshot_group: str | None = None
    snapshot_proof_digest: str | None = None

    @field_validator("capture_digest", "snapshot_proof_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _bindings(self) -> "AcquisitionCandidateV1":
        if capture_digest(self.envelope).tagged != self.capture_digest:
            raise ValueError("acquisition candidate Capture digest does not reproduce")
        if self.landing_event.capture_digest != self.capture_digest:
            raise ValueError("acquisition candidate differs from its landing event")
        return self


class AcquisitionInputDecisionV1(_StrictAcquisitionModel):
    input_name: str
    disposition: Literal["selected", "omitted", "defaulted", "refused"]
    considered_capture_digests: tuple[str, ...] = ()
    selected_capture_digests: tuple[str, ...] = ()
    selected_cursors: tuple[str, ...] = ()
    default_value: object | None = None
    reason_codes: tuple[str, ...] = ()

    @field_validator("default_value", mode="before")
    @classmethod
    def _default(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)


class SourceSelectionReceiptV1(_StrictAcquisitionModel):
    tag: Literal["playbill-source-selection-receipt-v1"] = "playbill-source-selection-receipt-v1"
    policy_digest: str
    anchor_cursor: str
    evaluation_time: datetime
    verdict: Literal["selected", "refused"]
    decisions: tuple[AcquisitionInputDecisionV1, ...]
    coordinate_time_vector: tuple[dict[str, object], ...]
    coherence_proof_digest: str | None = None

    @field_validator("policy_digest", "coherence_proof_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selection receipt evaluation time must be timezone-aware")
        return value

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        return typed_digest(
            Sha256Value,
            "playbill-source-selection-receipt-v1",
            payload,
        ).tagged


def _behavior_decision(
    rule: InputAcquisitionRuleV1,
    behavior: AcquisitionFailureBehaviorV1,
    *,
    considered: tuple[str, ...],
    reason: str,
    default_authorized: bool,
) -> AcquisitionInputDecisionV1:
    if behavior == "omit_optional" and rule.requirement == "optional":
        return AcquisitionInputDecisionV1(
            input_name=rule.input_name,
            disposition="omitted",
            considered_capture_digests=considered,
            reason_codes=(reason,),
        )
    if behavior == "declared_conservative_default" and default_authorized:
        return AcquisitionInputDecisionV1(
            input_name=rule.input_name,
            disposition="defaulted",
            considered_capture_digests=considered,
            default_value=rule.conservative_default,
            reason_codes=(reason,),
        )
    return AcquisitionInputDecisionV1(
        input_name=rule.input_name,
        disposition="refused",
        considered_capture_digests=considered,
        reason_codes=(reason,),
    )


def select_sources(
    policy: SourceAcquisitionPolicyV1,
    candidates: tuple[AcquisitionCandidateV1, ...],
    *,
    anchor: CaptureLandingEventV1,
    evaluation_time: datetime,
    default_authorizations: tuple[str, ...] = (),
) -> SourceSelectionReceiptV1:
    """Select one dependency vector without inventing cross-source total order."""

    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise ValueError("source selection evaluation time must be timezone-aware")
    if default_authorizations != tuple(sorted(set(default_authorizations))):
        raise ValueError("default authorizations must be sorted and unique")
    grouped: dict[str, list[AcquisitionCandidateV1]] = {
        rule.input_name: [] for rule in policy.inputs
    }
    for candidate in candidates:
        if candidate.input_name not in grouped:
            raise ValueError("candidate names an undeclared acquisition input")
        grouped[candidate.input_name].append(candidate)
    decisions: list[AcquisitionInputDecisionV1] = []
    selected: list[AcquisitionCandidateV1] = []
    for rule in policy.inputs:
        contenders = tuple(
            sorted(
                grouped[rule.input_name],
                key=lambda item: (
                    item.landing_event.partition_id,
                    item.landing_event.sequence,
                    item.capture_digest,
                ),
            )
        )
        considered = tuple(item.capture_digest for item in contenders)
        if not contenders:
            decisions.append(
                _behavior_decision(
                    rule,
                    rule.on_unavailable,
                    considered=considered,
                    reason="playbill.acquisition.unavailable",
                    default_authorized=rule.input_name in default_authorizations,
                )
            )
            continue
        eligible: list[AcquisitionCandidateV1] = []
        failure: tuple[AcquisitionFailureBehaviorV1, str] | None = None
        for candidate in contenders:
            replayability = getattr(candidate.envelope.source, "replayability", "exact")
            if replayability not in rule.permitted_replayability or (
                replayability == "exact" and not candidate.current_replay_available
            ):
                failure = (rule.on_unavailable, "playbill.acquisition.replay_unavailable")
                continue
            if rule.max_age is not None and evaluation_time - candidate.envelope.observed_at > (
                timedelta(microseconds=rule.max_age.microseconds)
            ):
                failure = (rule.on_stale, "playbill.acquisition.stale")
                continue
            if (
                candidate.selected_bytes > candidate.selection_budget.max_bytes
                or candidate.selected_rows > candidate.selection_budget.max_rows
                or candidate.selected_items > candidate.selection_budget.max_items
            ):
                failure = (rule.on_oversized, "playbill.acquisition.oversized")
                continue
            eligible.append(candidate)
        if not eligible:
            behavior, reason = failure or (
                rule.on_unavailable,
                "playbill.acquisition.unavailable",
            )
            decisions.append(
                _behavior_decision(
                    rule,
                    behavior,
                    considered=considered,
                    reason=reason,
                    default_authorized=rule.input_name in default_authorizations,
                )
            )
            continue
        conflict = (
            len(eligible) > 1
            and len({item.conflict_key or item.capture_digest for item in eligible}) > 1
        )
        if conflict and rule.on_conflict == "refuse":
            decisions.append(
                AcquisitionInputDecisionV1(
                    input_name=rule.input_name,
                    disposition="refused",
                    considered_capture_digests=considered,
                    reason_codes=("playbill.acquisition.conflict",),
                )
            )
            continue
        chosen = tuple(eligible) if conflict else (eligible[-1],)
        selected.extend(chosen)
        decisions.append(
            AcquisitionInputDecisionV1(
                input_name=rule.input_name,
                disposition="selected",
                considered_capture_digests=considered,
                selected_capture_digests=tuple(item.capture_digest for item in chosen),
                selected_cursors=tuple(item.landing_event.cursor for item in chosen),
                reason_codes=("playbill.acquisition.conflict_preserved",) if conflict else (),
            )
        )
    coherence_proof: str | None = None
    selected_times = [item.envelope.observed_at for item in selected]
    if isinstance(policy.coherence, BoundedWindowCoherenceV1) and selected_times:
        skew = max(selected_times) - min(selected_times)
        if skew > timedelta(microseconds=policy.coherence.max_cross_source_skew.microseconds):
            decisions.append(
                AcquisitionInputDecisionV1(
                    input_name="coherence",
                    disposition="refused",
                    reason_codes=("playbill.acquisition.cross_source_skew",),
                )
            )
    if isinstance(policy.coherence, DeclaredSnapshotGroupCoherenceV1) and selected:
        groups = {item.snapshot_group for item in selected}
        proofs = {item.snapshot_proof_digest for item in selected}
        if None in groups or len(groups) != 1 or None in proofs:
            decisions.append(
                AcquisitionInputDecisionV1(
                    input_name="coherence",
                    disposition="refused",
                    reason_codes=("playbill.acquisition.snapshot_group_unproved",),
                )
            )
        else:
            coherence_proof = typed_digest(
                Sha256Value,
                "playbill-declared-snapshot-group-proof-v1",
                {
                    "coordinate_grammar_digest": policy.coherence.coordinate_grammar_digest,
                    "proof_adapter_digest": policy.coherence.proof_adapter_digest,
                    "snapshot_group": next(iter(groups)),
                    "proofs": sorted(item for item in proofs if item is not None),
                },
            ).tagged
    ordered_decisions = tuple(sorted(decisions, key=lambda item: item.input_name.encode("utf-8")))
    refused = any(item.disposition == "refused" for item in ordered_decisions)
    vector: tuple[dict[str, object], ...] = tuple(
        {
            "capture_digest": item.capture_digest,
            "input_name": item.input_name,
            "landed_at": item.landing_event.landed_at,
            "native_source": item.envelope.source.model_dump(mode="json"),
            "observed_at": item.envelope.observed_at,
            "source_effective_time": (
                None
                if item.envelope.source_effective_time is None
                else item.envelope.source_effective_time.model_dump(mode="json")
            ),
        }
        for item in sorted(
            selected,
            key=lambda candidate: canonical_bytes(candidate.model_dump(mode="json")),
        )
    )
    return SourceSelectionReceiptV1(
        policy_digest=acquisition_policy_digest(policy).tagged,
        anchor_cursor=anchor.cursor,
        evaluation_time=evaluation_time,
        verdict="refused" if refused else "selected",
        decisions=ordered_decisions,
        coordinate_time_vector=vector,
        coherence_proof_digest=coherence_proof,
    )


__all__ = [
    "AcceptedSourceAcquisitionPolicyV1",
    "AcquisitionCandidateV1",
    "AcquisitionFailureBehaviorV1",
    "AcquisitionInputDecisionV1",
    "BoundedWindowCoherenceV1",
    "DeclaredSnapshotGroupCoherenceV1",
    "IndependentCoherenceV1",
    "InputAcquisitionRuleV1",
    "SourceAcquisitionPolicyV1",
    "SourceAcquisitionPolicyError",
    "SourceAcquisitionPolicyLawResultV1",
    "SourceSelectionReceiptV1",
    "acquisition_policy_digest",
    "acquisition_policy_path",
    "evaluate_acquisition_policy_law",
    "parse_acquisition_policy",
    "render_acquisition_policy",
    "select_sources",
]
