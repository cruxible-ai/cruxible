"""Final policy-bearing ClaimType v1 artifact and acceptance law."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Callable, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_core.playbill.claim_type_structure import ClaimTypeStructure
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.governance import PermissionTier
from cruxible_core.playbill.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_core.playbill.principals import PrincipalRegistrySnapshot

_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})+$")


class ClaimTypeFormatError(PlaybillFormatError):
    """The ClaimType envelope or its canonical path is invalid."""


class _StrictClaimTypeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimSubjectScopeV1(_StrictClaimTypeModel):
    tag: Literal["playbill-claim-subject-scope-v1"] = "playbill-claim-subject-scope-v1"
    kind: Literal["any_existing_subject"] = "any_existing_subject"


class ClaimSlotPolicyV1(_StrictClaimTypeModel):
    tag: Literal["playbill-claim-slot-policy-v1"] = "playbill-claim-slot-policy-v1"
    kind: Literal["literal_field_digest"] = "literal_field_digest"
    json_pointer: Literal["/purpose"] = "/purpose"
    digest_domain: Literal["playbill-knowledge-brief-purpose-v1"] = (
        "playbill-knowledge-brief-purpose-v1"
    )
    per_slot_cardinality: Literal[1] = 1


class ClaimType(_StrictClaimTypeModel):
    artifact_format: Literal["playbill-claim-type-v1", "playbill-claim-type-v2"] = (
        "playbill-claim-type-v1"
    )
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
    subject_scope: ClaimSubjectScopeV1 | None = None
    slot_policy: ClaimSlotPolicyV1 | None = None

    @model_serializer(mode="wrap")
    def _versioned_wire(self, handler: Any) -> dict[str, object]:
        payload = cast(dict[str, object], handler(self))
        if self.artifact_format == "playbill-claim-type-v1":
            payload.pop("subject_scope", None)
            payload.pop("slot_policy", None)
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
            if self.subject_scope is not None or self.slot_policy is not None:
                raise ValueError("ClaimType v1 cannot carry v2 subject or slot policy")
            structural_subject_kinds = self.allowed_subject_kinds
        else:
            if self.subject_scope is None or self.slot_policy is None:
                raise ValueError("ClaimType v2 requires subject-scope and slot-policy contracts")
            if self.allowed_subject_kinds:
                raise ValueError("a v2 any-subject scope replaces finite allowed_subject_kinds")
            if self.cardinality != "many":
                raise ValueError("a slotted ClaimType v2 has many slots")
            structural_subject_kinds = ("semantic.subject",)
        ClaimTypeStructure(
            predicate=self.predicate,
            allowed_subject_kinds=structural_subject_kinds,
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
        subject_kinds = (
            ("semantic.subject",)
            if self.artifact_format == "playbill-claim-type-v2"
            else self.allowed_subject_kinds
        )
        return ClaimTypeStructure(
            predicate=self.predicate,
            allowed_subject_kinds=subject_kinds,
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
    if claim_type.artifact_format == "playbill-claim-type-v1":
        payload.pop("subject_scope", None)
        payload.pop("slot_policy", None)
    return canonical_bytes(payload) + b"\n"


def parse_claim_type(content: bytes, *, path: str) -> ClaimType:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ClaimTypeFormatError("ClaimType is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("artifact_format") not in {
        "playbill-claim-type-v1",
        "playbill-claim-type-v2",
    }:
        declared = payload.get("artifact_format") if isinstance(payload, dict) else None
        raise ClaimTypeFormatError(f"unsupported ClaimType artifact format: {declared!r}")
    try:
        claim_type = ClaimType.model_validate(payload)
    except ValidationError as exc:
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


def _claim_type_digest_v2(claim_type: ClaimType) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        claim_type.model_dump(mode="json"),
    )


CLAIM_TYPE_DIGEST_FUNCTIONS: dict[str, Callable[[ClaimType], ArtifactDigest]] = {
    "playbill-claim-type-v1": _claim_type_digest_v1,
    "playbill-claim-type-v2": _claim_type_digest_v2,
}


def claim_type_digest(claim_type: ClaimType) -> ArtifactDigest:
    return CLAIM_TYPE_DIGEST_FUNCTIONS[claim_type.artifact_format](claim_type)


def claim_type_accepts_subject(claim_type: ClaimType, subject_kind: str) -> bool:
    if claim_type.artifact_format == "playbill-claim-type-v2":
        return (
            claim_type.subject_scope is not None
            and claim_type.subject_scope.kind == "any_existing_subject"
        )
    return subject_kind in claim_type.allowed_subject_kinds


def claim_type_projection_structure(claim_type: ClaimType) -> dict[str, object]:
    """Project v2 structure without treating JSON-Schema `$` keys as fact wrappers."""

    structure = claim_type.structure.model_dump(mode="json")
    if claim_type.artifact_format == "playbill-claim-type-v1":
        return structure
    structure["literal_schema"] = canonical_bytes(claim_type.literal_schema or {}).decode("utf-8")
    structure["subject_scope"] = (
        None
        if claim_type.subject_scope is None
        else claim_type.subject_scope.model_dump(mode="json")
    )
    structure["slot_policy"] = (
        None if claim_type.slot_policy is None else claim_type.slot_policy.model_dump(mode="json")
    )
    return structure


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
            if (
                self.artifact_digest is None
                or self.required_tier is None
                or not self.approval_scope
            ):
                raise ValueError("accepted ClaimType law result is incomplete")
            if self.diagnostics:
                raise ValueError("accepted ClaimType law result cannot carry diagnostics")
        elif self.artifact_digest is not None or self.required_tier is not None:
            raise ValueError("refused ClaimType law result cannot carry acceptance fields")
        return self


def _diagnostic(code: str, message: str, *, path: str) -> CompilerDiagnostic:
    from cruxible_core.playbill.semantic import SemanticAddress

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

    if claim_type.artifact_format == "playbill-claim-type-v2":
        from cruxible_core.playbill.knowledge_briefs import KNOWLEDGE_BRIEF_CLAIM_TYPE

        if claim_type != KNOWLEDGE_BRIEF_CLAIM_TYPE:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.v2_profile_unregistered",
                        "ClaimType v2 is reserved for the exact built-in knowledge.brief profile.",
                        path=path,
                    ),
                ),
            )

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
    roles = set(_actor_roles(principals, actor_id))
    digest = claim_type_digest(claim_type).tagged
    if predecessor is None:
        root_authority = ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",))
        if claim_type.authority != root_authority:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.namespace_authority_mismatch",
                        "A new ClaimType must materialize accepted root namespace authority.",
                        path=path,
                    ),
                ),
            )
        if not roles.intersection(root_authority.propose_roles):
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.actor_unauthorized",
                        "The request actor lacks accepted namespace creation authority.",
                        path=path,
                    ),
                ),
            )
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
        approval_scope = root_authority.approve_roles
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
        if claim_type.authority != previous.authority:
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.authority_change_unsupported",
                        "ClaimType succession cannot rewrite accepted authority in v1.",
                        path=path,
                    ),
                ),
            )
        if not roles.intersection(previous.authority.propose_roles):
            return ClaimTypeLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.claim_type.actor_unauthorized",
                        "The request actor lacks predecessor proposal authority.",
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
        approval_scope = previous.authority.approve_roles
    return ClaimTypeLawResult(
        verdict="accepted",
        artifact_digest=digest,
        required_tier="governed_write",
        approval_scope=approval_scope,
    )


__all__ = [
    "AcceptedClaimType",
    "CLAIM_TYPE_DIGEST_FUNCTIONS",
    "ClaimType",
    "ClaimSlotPolicyV1",
    "ClaimSubjectScopeV1",
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
