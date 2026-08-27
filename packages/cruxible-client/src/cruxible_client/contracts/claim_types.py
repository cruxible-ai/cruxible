"""Final policy-bearing ClaimType v1 artifact and acceptance law."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Callable, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from cruxible_client.contracts.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_type_structure import ClaimTypeStructure
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier, governance_identifier
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.principals import PrincipalRegistrySnapshot

_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})+$")


class ClaimTypeFormatError(PlaybillFormatError):
    """The ClaimType envelope or its canonical path is invalid."""


class ClaimTypeFreshnessHorizonInvalid(ClaimTypeFormatError):
    """A ClaimType v3 evidence-freshness horizon is malformed or non-positive."""


class _StrictClaimTypeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimFreshnessDurationV1(_StrictClaimTypeModel):
    tag: Literal["playbill-duration-v1"] = "playbill-duration-v1"
    microseconds: int = Field(ge=0)


class ClaimEvidenceFreshnessV1(_StrictClaimTypeModel):
    tag: Literal["playbill-claim-evidence-freshness-v1"] = "playbill-claim-evidence-freshness-v1"
    stale_after: ClaimFreshnessDurationV1

    @model_validator(mode="after")
    def _positive_horizon(self) -> "ClaimEvidenceFreshnessV1":
        if self.stale_after.microseconds <= 0:
            raise ValueError("evidence freshness stale_after must be positive")
        return self


class ClaimAttestationConsequenceRuleV1(_StrictClaimTypeModel):
    tag: Literal["playbill-claim-attestation-consequence-rule-v1"] = (
        "playbill-claim-attestation-consequence-rule-v1"
    )
    rule_id: str
    stance: Literal["unsure", "contradict"]
    minimum_independent_control_components: int = Field(ge=0)
    consequence: Literal["next_claim_attestation_threshold"] = "next_claim_attestation_threshold"
    require_current: Literal[True] = True

    @field_validator("rule_id")
    @classmethod
    def _rule_id(cls, value: str) -> str:
        return governance_identifier(value, label="attestation consequence rule_id")


class ClaimAttestationConsequencePolicyV1(_StrictClaimTypeModel):
    tag: Literal["playbill-claim-attestation-consequence-policy-v1"] = (
        "playbill-claim-attestation-consequence-policy-v1"
    )
    rules: tuple[ClaimAttestationConsequenceRuleV1, ...] = Field(min_length=1)

    @field_validator("rules")
    @classmethod
    def _rules(
        cls, value: tuple[ClaimAttestationConsequenceRuleV1, ...]
    ) -> tuple[ClaimAttestationConsequenceRuleV1, ...]:
        rule_ids = tuple(rule.rule_id for rule in value)
        if rule_ids != tuple(sorted(set(rule_ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("attestation consequence rules must be sorted and unique by rule_id")
        return value


class ClaimType(_StrictClaimTypeModel):
    artifact_format: Literal[
        "playbill-claim-type-v1",
        "playbill-claim-type-v3",
        "playbill-claim-type-v4",
    ] = "playbill-claim-type-v1"
    identity: ArtifactIdentity
    predicate: str
    allowed_subject_kinds: tuple[str, ...]
    object_kind: Literal["literal", "subject", "exact_content"]
    literal_schema: dict[str, object] | None = None
    allowed_object_subject_kinds: tuple[str, ...] = ()
    cardinality: Literal["one", "many"]
    permitted_roles: tuple[
        Literal["normative", "observation", "environment_binding", "derivation"], ...
    ]
    referent_sensitivity: Literal["identity", "shell"] = "identity"
    evidence_admission_policy: ClaimEvidenceAdmissionPolicyV1
    admission_policy: ClaimAdmissionPolicyV1
    resolution_policy: ClaimResolutionPolicyV1
    authority: ArtifactAuthority
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()
    # Existing v3 envelopes committed these null placeholders. They remain
    # null-only compatibility bytes, never supported authoring capabilities.
    subject_scope: None = None
    slot_policy: None = None
    evidence_freshness: ClaimEvidenceFreshnessV1 | None = None
    attestation_consequence_policy: ClaimAttestationConsequencePolicyV1 | None = None

    @model_serializer(mode="wrap")
    def _versioned_wire(self, handler: Any) -> dict[str, object]:
        payload = cast(dict[str, object], handler(self))
        if self.artifact_format in {
            "playbill-claim-type-v1",
            "playbill-claim-type-v3",
        }:
            payload.pop("attestation_consequence_policy", None)
        if self.artifact_format == "playbill-claim-type-v1":
            payload.pop("subject_scope", None)
            payload.pop("slot_policy", None)
            payload.pop("evidence_freshness", None)
        return payload

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, value: str) -> str:
        if not _PREDICATE_RE.fullmatch(value):
            raise ValueError("ClaimType predicate must be a canonical qualified identifier")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        def key(pin: ArtifactPin) -> tuple[bytes, bytes]:
            return pin.role.encode("utf-8"), pin.target.qualified.encode("utf-8")

        if value != tuple(sorted(value, key=key)):
            raise ValueError("ClaimType pins must be canonically sorted")
        identities = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(identities) != len(set(identities)):
            raise ValueError("ClaimType pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _complete_contract(self) -> "ClaimType":
        expected = ArtifactIdentity(kind="ClaimType", name=self.predicate)
        if self.identity != expected:
            raise ValueError("ClaimType identity must equal ClaimType:<predicate>")
        # Reuse the deliberately policy-free PC-A1 validator so the final wire
        # cannot drift from the reviewed structural surface.
        if self.artifact_format == "playbill-claim-type-v1":
            if self.evidence_freshness is not None:
                raise ValueError("ClaimType v1 cannot carry v3 evidence freshness")
            if self.attestation_consequence_policy is not None:
                raise ValueError("ClaimType v1 cannot carry v4 attestation consequences")
        elif self.artifact_format == "playbill-claim-type-v3":
            if self.evidence_freshness is None:
                raise ValueError("ClaimType v3 requires evidence freshness")
            if self.attestation_consequence_policy is not None:
                raise ValueError("ClaimType v3 cannot carry v4 attestation consequences")
        elif self.attestation_consequence_policy is None:
            raise ValueError("ClaimType v4 requires an attestation consequence policy")
        ClaimTypeStructure(
            predicate=self.predicate,
            allowed_subject_kinds=self.allowed_subject_kinds,
            object_kind=self.object_kind,
            literal_schema=self.literal_schema,
            allowed_object_subject_kinds=self.allowed_object_subject_kinds,
            cardinality=self.cardinality,
            permitted_roles=self.permitted_roles,
            referent_sensitivity=self.referent_sensitivity,
        )
        if self.resolution_policy.cardinality != self.cardinality:
            raise ValueError("ClaimType and resolution-policy cardinality must agree")
        return self

    @property
    def structure(self) -> ClaimTypeStructure:
        return ClaimTypeStructure(
            predicate=self.predicate,
            allowed_subject_kinds=self.allowed_subject_kinds,
            object_kind=self.object_kind,
            literal_schema=self.literal_schema,
            allowed_object_subject_kinds=self.allowed_object_subject_kinds,
            cardinality=self.cardinality,
            permitted_roles=self.permitted_roles,
            referent_sensitivity=self.referent_sensitivity,
        )


def claim_type_path(predicate: str) -> str:
    if not _PREDICATE_RE.fullmatch(predicate):
        raise ClaimTypeFormatError("ClaimType predicate is not path-addressable")
    namespace, _separator, name = predicate.rpartition(".")
    return f"claim-types/{namespace}/{name}.yaml"


def validate_claim_type_path(claim_type: ClaimType, path: str) -> str:
    expected = claim_type_path(claim_type.predicate)
    if path != expected:
        raise ClaimTypeFormatError(
            f"ClaimType identity/path disagreement: {claim_type.identity.qualified!r} "
            f"requires {expected!r}"
        )
    return path


def render_claim_type(claim_type: ClaimType) -> bytes:
    payload = claim_type.model_dump(mode="json")
    if claim_type.artifact_format in {
        "playbill-claim-type-v1",
        "playbill-claim-type-v3",
    }:
        payload.pop("attestation_consequence_policy", None)
    if claim_type.artifact_format == "playbill-claim-type-v1":
        payload.pop("subject_scope", None)
        payload.pop("slot_policy", None)
        payload.pop("evidence_freshness", None)
    return canonical_bytes(payload) + b"\n"


def parse_claim_type(content: bytes, *, path: str) -> ClaimType:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ClaimTypeFormatError("ClaimType is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("artifact_format") not in {
        "playbill-claim-type-v1",
        "playbill-claim-type-v3",
        "playbill-claim-type-v4",
    }:
        declared = payload.get("artifact_format") if isinstance(payload, dict) else None
        raise ClaimTypeFormatError(f"unsupported ClaimType artifact format: {declared!r}")
    try:
        claim_type = ClaimType.model_validate(payload)
    except ValidationError as exc:
        if payload.get("artifact_format") == "playbill-claim-type-v3" and any(
            tuple(error["loc"])[0:1] == ("evidence_freshness",) for error in exc.errors()
        ):
            raise ClaimTypeFreshnessHorizonInvalid(
                "ClaimType v3 evidence freshness horizon is malformed or non-positive"
            ) from exc
        raise ClaimTypeFormatError("ClaimType failed strict versioned validation") from exc
    if render_claim_type(claim_type) != content:
        raise ClaimTypeFormatError("ClaimType is not in canonical wire form")
    validate_claim_type_path(claim_type, path)
    return claim_type


def _claim_type_digest_v1(claim_type: ClaimType) -> ArtifactDigest:
    payload = claim_type.model_dump(mode="json")
    payload.pop("subject_scope", None)
    payload.pop("slot_policy", None)
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        payload,
    )


def _claim_type_digest_v3(claim_type: ClaimType) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        claim_type.model_dump(mode="json"),
    )


def _claim_type_digest_v4(claim_type: ClaimType) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        claim_type.model_dump(mode="json"),
    )


CLAIM_TYPE_DIGEST_FUNCTIONS: dict[str, Callable[[ClaimType], ArtifactDigest]] = {
    "playbill-claim-type-v1": _claim_type_digest_v1,
    "playbill-claim-type-v3": _claim_type_digest_v3,
    "playbill-claim-type-v4": _claim_type_digest_v4,
}


def claim_type_digest(claim_type: ClaimType) -> ArtifactDigest:
    return CLAIM_TYPE_DIGEST_FUNCTIONS[claim_type.artifact_format](claim_type)


def claim_type_accepts_subject(claim_type: ClaimType, subject_kind: str) -> bool:
    return subject_kind in claim_type.allowed_subject_kinds


def claim_type_projection_structure(claim_type: ClaimType) -> dict[str, object]:
    """Project the policy-free finite-subject structure shared by v1 and v3."""

    return claim_type.structure.model_dump(mode="json")


class AcceptedClaimType(_StrictClaimTypeModel):
    path: str
    claim_type: ClaimType
    artifact_digest: str

    @model_validator(mode="after")
    def _correspondence(self) -> "AcceptedClaimType":
        validate_claim_type_path(self.claim_type, self.path)
        if self.artifact_digest != claim_type_digest(self.claim_type).tagged:
            raise ValueError("accepted ClaimType digest differs from its exact envelope")
        return self


class ClaimTypeLawResult(_StrictClaimTypeModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "ClaimTypeLawResult":
        if self.verdict == "accepted":
            if self.artifact_digest is None or self.required_tier is None:
                raise ValueError("accepted ClaimType law result is incomplete")
            if self.diagnostics:
                raise ValueError("accepted ClaimType law result cannot carry diagnostics")
        elif self.artifact_digest is not None or self.required_tier is not None:
            raise ValueError("refused ClaimType law result cannot carry acceptance fields")
        return self


def _diagnostic(code: str, message: str, *, path: str) -> CompilerDiagnostic:
    from cruxible_client.contracts.semantic import SemanticAddress

    return CompilerDiagnostic(
        code=code,
        severity="error",
        message=message,
        subject=SemanticAddress.whole_artifact(path),
    )


def _actor_roles(principals: PrincipalRegistrySnapshot, actor_id: str | None) -> tuple[str, ...]:
    if actor_id is None:
        return ()
    try:
        return principals.require_active(actor_id).authority_roles
    except Exception:
        return ()


def evaluate_claim_type_law(
    claim_type: ClaimType,
    *,
    path: str,
    principals: PrincipalRegistrySnapshot,
    actor_id: str | None,
    predecessor: AcceptedClaimType | None,
    accepted_artifacts: Mapping[str, tuple[ArtifactIdentity, str]] | None = None,
) -> ClaimTypeLawResult:
    """Evaluate exact path, lifecycle, authority, and digest-pinned dependencies."""

    try:
        validate_claim_type_path(claim_type, path)
    except ClaimTypeFormatError as exc:
        return ClaimTypeLawResult(
            verdict="refused",
            diagnostics=(_diagnostic("playbill.claim_type.path_mismatch", str(exc), path=path),),
        )
    if accepted_artifacts is not None:
        for pin in claim_type.pins:
            accepted = accepted_artifacts.get(pin.target.qualified)
            if accepted is None or accepted[1] != pin.artifact_digest:
                return ClaimTypeLawResult(
                    verdict="refused",
                    diagnostics=(
                        _diagnostic(
                            "playbill.claim_type.pin_unresolved",
                            "A ClaimType pin does not resolve at the accepted parent coordinate.",
                            path=path,
                        ),
                    ),
                )
    digest = claim_type_digest(claim_type).tagged
    if predecessor is None:
        if claim_type.lifecycle.state != "live" or claim_type.lifecycle.predecessor_digest:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.unexpected_predecessor",
                        "A new ClaimType must begin live without a predecessor.",
                        path=path,
                    ),
                ),
            )
    else:
        previous = predecessor.claim_type
        if previous.identity != claim_type.identity or predecessor.path != path:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.predecessor_identity_mismatch",
                        "The live predecessor has a different ClaimType identity.",
                        path=path,
                    ),
                ),
            )
        if claim_type.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.stale_predecessor",
                        "The ClaimType does not name the exact live predecessor digest.",
                        path=path,
                    ),
                ),
            )
        if previous.lifecycle.state == "retired":
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.lifecycle_invalid",
                        "A retired ClaimType cannot be revived or revised.",
                        path=path,
                    ),
                ),
            )
        if digest == predecessor.artifact_digest:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.no_semantic_change",
                        "ClaimType succession must produce a new artifact digest.",
                        path=path,
                    ),
                ),
            )
    return ClaimTypeLawResult(
        verdict="accepted",
        artifact_digest=digest,
        required_tier="governed_write",
        approval_scope=(),
    )


__all__ = [
    "AcceptedClaimType",
    "CLAIM_TYPE_DIGEST_FUNCTIONS",
    "ClaimAttestationConsequencePolicyV1",
    "ClaimAttestationConsequenceRuleV1",
    "ClaimType",
    "ClaimEvidenceFreshnessV1",
    "ClaimFreshnessDurationV1",
    "ClaimTypeFreshnessHorizonInvalid",
    "ClaimTypeFormatError",
    "ClaimTypeLawResult",
    "claim_type_digest",
    "claim_type_accepts_subject",
    "claim_type_path",
    "claim_type_projection_structure",
    "evaluate_claim_type_law",
    "parse_claim_type",
    "render_claim_type",
    "validate_claim_type_path",
]
