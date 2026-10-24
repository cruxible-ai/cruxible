"""Governed source age defaults, with frozen v1 readers."""

from __future__ import annotations

import json
import re
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
    normalize_canonical,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    CAPTURE_COMPONENTS,
    CanonicalDuration,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import parse_iso_duration_microseconds

AcquisitionFailureBehavior = Literal["refuse", "omit_optional", "declared_conservative_default"]
_INPUT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_POLICY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")

#: The one pin role under which any artifact binds its acquisition policy. A
#: LineSpec carries it on the Line; a Procedure envelope carries it for every
#: direct run of that Procedure. One spelling, so one grep finds every binder.
ACQUISITION_POLICY_PIN_ROLE = "acquisition-policy"


class SourceAcquisitionPolicyError(FormatError):
    """A SourceAcquisitionPolicy artifact or acceptance transition is invalid."""


class _StrictAcquisitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if values != tuple(sorted(set(values), key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{label} must be sorted and unique")
    return values


class InputAcquisitionRule(_StrictAcquisitionModel):
    tag: Literal["playbill-input-acquisition-rule-v1"] = "playbill-input-acquisition-rule-v1"
    input_name: str
    requirement: Literal["required", "optional", "conservative_default"]
    permitted_replayability: tuple[Literal["exact", "attested_only"], ...]
    max_age: CanonicalDuration | None = None
    correlation_keys: tuple[str, ...] = ()
    join_semantics_digest: str | None = None
    on_unavailable: AcquisitionFailureBehavior
    on_stale: AcquisitionFailureBehavior
    on_oversized: AcquisitionFailureBehavior
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
    def _shape(self) -> "InputAcquisitionRule":
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


class IndependentCoherence(_StrictAcquisitionModel):
    tag: Literal["playbill-independent-coherence-v1"] = "playbill-independent-coherence-v1"
    kind: Literal["independent"] = "independent"


class BoundedWindowCoherence(_StrictAcquisitionModel):
    tag: Literal["playbill-bounded-window-coherence-v1"] = "playbill-bounded-window-coherence-v1"
    kind: Literal["bounded_window"] = "bounded_window"
    max_cross_source_skew: CanonicalDuration


class DeclaredSnapshotGroupCoherence(_StrictAcquisitionModel):
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


SourceCoherence = Annotated[
    IndependentCoherence | BoundedWindowCoherence | DeclaredSnapshotGroupCoherence,
    Field(discriminator="kind"),
]


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes]:
    return pin.role.encode("utf-8"), pin.target.qualified.encode("utf-8")


class SourceAcquisitionPolicyV1(_StrictAcquisitionModel):
    artifact_format: Literal["playbill-source-acquisition-policy-v1"] = (
        "playbill-source-acquisition-policy-v1"
    )
    identity: ArtifactIdentity
    inputs: tuple[InputAcquisitionRule, ...]
    coherence: SourceCoherence
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, value: tuple[InputAcquisitionRule, ...]) -> tuple[InputAcquisitionRule, ...]:
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


DEFAULT_ACQUISITION_POLICY_NAME = "default"


class AcquisitionRule(_StrictAcquisitionModel):
    """Engine age bound for one Source alias; no authored failure choices."""

    input_name: str
    max_age: CanonicalDuration | None = None


class SourceAcquisitionPolicy(_StrictAcquisitionModel):
    """One instance-wide age bound and only the aliases that differ."""

    artifact_format: Literal["playbill-source-acquisition-policy-v2"] = (
        "playbill-source-acquisition-policy-v2"
    )
    identity: ArtifactIdentity
    max_age: CanonicalDuration | None = None
    overrides: dict[str, CanonicalDuration | None] = Field(default_factory=dict)
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("max_age", mode="before")
    @classmethod
    def _duration(cls, value: object) -> object:
        if isinstance(value, str):
            return CanonicalDuration(microseconds=parse_iso_duration_microseconds(value))
        return value

    @field_validator("overrides", mode="before")
    @classmethod
    def _overrides(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if any(not isinstance(name, str) or not _INPUT_RE.fullmatch(name) for name in value):
            raise ValueError("acquisition overrides must name canonical Source aliases")
        return {name: cls._duration(age) for name, age in value.items()}

    @model_validator(mode="after")
    def _identity(self) -> "SourceAcquisitionPolicy":
        if self.identity.kind != "SourceAcquisitionPolicy" or not _POLICY_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("acquisition policy identity kind is invalid")
        return self


SourceAcquisitionPolicyAny = SourceAcquisitionPolicy | SourceAcquisitionPolicyV1
AcquisitionRuleAny = AcquisitionRule | InputAcquisitionRule


def acquisition_rule(policy: SourceAcquisitionPolicyAny, alias: str) -> AcquisitionRuleAny | None:
    """Resolve just this alias, without expanding defaults into stored rules."""
    if isinstance(policy, SourceAcquisitionPolicy):
        return AcquisitionRule(
            input_name=alias, max_age=policy.overrides.get(alias, policy.max_age)
        )
    return next((rule for rule in policy.inputs if rule.input_name == alias), None)


def acquisition_policy_digest(policy: SourceAcquisitionPolicyAny) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        policy.model_dump(mode="json"),
    )


def acquisition_policy_path(name: str) -> str:
    if not _POLICY_NAME_RE.fullmatch(name):
        raise SourceAcquisitionPolicyError("SourceAcquisitionPolicy is not path-addressable")
    return f"source-acquisition-policies/{name}.json"


def render_acquisition_policy(policy: SourceAcquisitionPolicyAny) -> bytes:
    return pretty_canonical_bytes(policy.model_dump(mode="json"))


def parse_acquisition_policy(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> SourceAcquisitionPolicyAny:
    try:
        payload = json.loads(content)
        model = (
            SourceAcquisitionPolicyV1
            if payload.get("artifact_format") == "playbill-source-acquisition-policy-v1"
            else SourceAcquisitionPolicy
        )
        policy = model.model_validate(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceAcquisitionPolicyError(
            "SourceAcquisitionPolicy failed strict validation"
        ) from exc
    if not artifact_path_matches(acquisition_policy_path(policy.identity.name), path, codec=codec):
        raise SourceAcquisitionPolicyError("SourceAcquisitionPolicy identity/path disagreement")
    if artifact_bytes_for_path(render_acquisition_policy(policy), path, codec=codec) != content:
        raise SourceAcquisitionPolicyError("SourceAcquisitionPolicy is not canonical")
    return policy


class AcceptedSourceAcquisitionPolicy(_StrictAcquisitionModel):
    path: str
    policy: SourceAcquisitionPolicyAny
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedSourceAcquisitionPolicy":
        if self.path != acquisition_policy_path(self.policy.identity.name) or (
            self.artifact_digest != acquisition_policy_digest(self.policy).tagged
        ):
            raise ValueError("accepted SourceAcquisitionPolicy does not reproduce")
        return self


class SourceAcquisitionPolicyLawResult(_StrictAcquisitionModel):
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
) -> SourceAcquisitionPolicyLawResult:
    return SourceAcquisitionPolicyLawResult(
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
    policy: SourceAcquisitionPolicyAny,
    *,
    path: str,
    predecessor: AcceptedSourceAcquisitionPolicy | None,
) -> SourceAcquisitionPolicyLawResult:
    if path != acquisition_policy_path(policy.identity.name):
        return _law_refusal(
            "cruxible.acquisition_policy.path_mismatch",
            "SourceAcquisitionPolicy identity/path disagreement.",
            path=path,
        )
    if predecessor is None and policy.lifecycle.predecessor_digest is not None:
        return _law_refusal(
            "cruxible.acquisition_policy.predecessor_missing",
            "A new SourceAcquisitionPolicy cannot name a predecessor.",
            path=path,
        )
    if predecessor is not None:
        if policy.identity != predecessor.policy.identity or (
            policy.lifecycle.predecessor_digest != predecessor.artifact_digest
        ):
            return _law_refusal(
                "cruxible.acquisition_policy.predecessor_mismatch",
                "SourceAcquisitionPolicy successor identity or predecessor differs.",
                path=path,
            )
    if isinstance(policy, SourceAcquisitionPolicyV1) and isinstance(
        policy.coherence, DeclaredSnapshotGroupCoherence
    ):
        required = (
            ("coordinate-grammar", policy.coherence.coordinate_grammar_digest),
            ("proof-adapter", policy.coherence.proof_adapter_digest),
        )
        resolved = all(
            any(
                pin.role == role
                and pin.artifact_digest == digest
                and CAPTURE_COMPONENTS.resolves(pin)
                for pin in policy.pins
            )
            for role, digest in required
        )
        if not resolved:
            return _law_refusal(
                "cruxible.acquisition_policy.snapshot_registry_unresolved",
                "Declared snapshot coherence requires registered exact grammar and "
                "proof-adapter pins.",
                path=path,
            )
    return SourceAcquisitionPolicyLawResult(
        verdict="accepted",
        artifact_digest=acquisition_policy_digest(policy).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


class AcquisitionInputDecision(_StrictAcquisitionModel):
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


__all__ = [
    "ACQUISITION_POLICY_PIN_ROLE",
    "DEFAULT_ACQUISITION_POLICY_NAME",
    "AcceptedSourceAcquisitionPolicy",
    "AcquisitionFailureBehavior",
    "AcquisitionInputDecision",
    "AcquisitionRule",
    "AcquisitionRuleAny",
    "IndependentCoherence",
    "InputAcquisitionRule",
    "SourceAcquisitionPolicy",
    "SourceAcquisitionPolicyV1",
    "SourceAcquisitionPolicyAny",
    "SourceAcquisitionPolicyError",
    "SourceAcquisitionPolicyLawResult",
    "acquisition_rule",
    "acquisition_policy_digest",
    "acquisition_policy_path",
    "evaluate_acquisition_policy_law",
    "parse_acquisition_policy",
    "render_acquisition_policy",
]
