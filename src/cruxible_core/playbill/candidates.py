"""Frozen semantic-candidate preimage and immutable PB-C candidate records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.canonical import (
    AcceptanceLawDigest,
    ArtifactDigest,
    CandidateDigest,
    GenerationRoot,
    SemanticDiffDigest,
    SemanticManifestRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    normalize_manifest_paths,
    typed_digest,
)
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.governance import (
    ActivationPolicy,
    ApprovalRequirement,
    MutationDisposition,
    PermissionTier,
    governance_identifier,
)


class _StrictCandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_candidate_timestamp(value: datetime) -> str:
    """Render the frozen six-fraction UTC timestamp spelling used by C_s."""

    if value.tzinfo is None:
        raise ValueError("candidate timestamp must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_candidate_timestamp(value: str) -> str:
    """Refuse timestamp spellings outside the frozen candidate wire format."""

    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(
            "candidate timestamp must use UTC with exactly six fractional digits and Z"
        ) from exc
    if canonical_candidate_timestamp(parsed) != value:
        raise ValueError("candidate timestamp is not canonical")
    return value


class SemanticCandidate(_StrictCandidateModel):
    """The complete locator-free object signed by reviewers in PB-D."""

    tag: Literal["playbill-candidate-v1"] = "playbill-candidate-v1"
    parent_semantic_root: str
    candidate_manifest_root: str
    semantic_diff_digest: str
    scope: tuple[str, ...]
    timestamp: str

    @field_validator("parent_semantic_root")
    @classmethod
    def _parent_semantic_root(cls, value: str) -> str:
        SemanticRoot.from_tagged(value)
        return value

    @field_validator("candidate_manifest_root")
    @classmethod
    def _candidate_manifest_root(cls, value: str) -> str:
        SemanticManifestRoot.from_tagged(value)
        return value

    @field_validator("semantic_diff_digest")
    @classmethod
    def _semantic_diff_digest(cls, value: str) -> str:
        SemanticDiffDigest.from_tagged(value)
        return value

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("candidate scope must not be empty")
        normalized = tuple(normalize_manifest_paths(value))
        if value != normalized:
            raise ValueError("candidate scope must be normalized, sorted, and unique")
        return value

    @field_validator("timestamp")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_candidate_timestamp(value)


def candidate_digest(candidate: SemanticCandidate) -> CandidateDigest:
    """Hash exactly the five C_s fields under the frozen candidate domain."""

    payload = candidate.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(CandidateDigest, "playbill-candidate-v1", payload)


class CandidateRecord(_StrictCandidateModel):
    """Family-neutral validated candidate and its complete law/closure evidence."""

    tag: Literal["playbill-validated-candidate-v1"] = "playbill-validated-candidate-v1"
    candidate: SemanticCandidate
    candidate_digest: str
    required_tier: PermissionTier
    approval_requirements: tuple[ApprovalRequirement, ...]
    activation_policy: ActivationPolicy
    closure_paths: tuple[str, ...]
    members: tuple["CandidateMemberEvidence", ...]
    law_digests: dict[str, str]
    compiler_digest: str

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @field_validator("approval_requirements")
    @classmethod
    def _approval_requirements(
        cls, value: tuple[ApprovalRequirement, ...]
    ) -> tuple[ApprovalRequirement, ...]:
        roles = [requirement.role for requirement in value]
        if roles != sorted(set(roles), key=lambda item: item.encode("utf-8")):
            raise ValueError("approval requirements must be sorted and unique by role")
        return value

    @field_validator("law_digests")
    @classmethod
    def _law_digests(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("validated candidates require at least one acceptance law")
        if list(value) != sorted(value, key=lambda item: item.encode("utf-8")):
            raise ValueError("acceptance-law mapping must be sorted by identifier")
        for identifier, digest in value.items():
            governance_identifier(identifier, label="acceptance-law identifier")
            AcceptanceLawDigest.from_tagged(digest)
        return value

    @field_validator("compiler_digest")
    @classmethod
    def _compiler_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _complete_binding(self) -> "CandidateRecord":
        if candidate_digest(self.candidate).tagged != self.candidate_digest:
            raise ValueError("candidate record digest does not reproduce from its complete C_s")
        if self.closure_paths != self.candidate.scope:
            raise ValueError("candidate closure must equal the complete candidate scope")
        member_paths = tuple(member.path for member in self.members)
        if member_paths != self.closure_paths:
            raise ValueError("candidate members must enumerate the complete ordered closure")
        used_laws = {member.law_identifier for member in self.members}
        if used_laws != set(self.law_digests):
            raise ValueError("candidate members and acceptance-law mapping differ")
        return self


class CandidateMemberEvidence(_StrictCandidateModel):
    """Per-member evidence interpreted by one digest-pinned acceptance law."""

    path: str
    artifact_kind: str
    artifact_digest: str
    disposition: MutationDisposition
    law_identifier: str
    governance_operation: str | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        normalized = tuple(normalize_manifest_paths((value,)))
        if normalized != (value,):
            raise ValueError("candidate member path must be canonical")
        return value

    @field_validator("artifact_kind")
    @classmethod
    def _artifact_kind(cls, value: str) -> str:
        return governance_identifier(value, label="artifact kind")

    @field_validator("artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @field_validator("law_identifier")
    @classmethod
    def _law_identifier(cls, value: str) -> str:
        return governance_identifier(value, label="acceptance-law identifier")

    @field_validator("governance_operation")
    @classmethod
    def _governance_operation(cls, value: str | None) -> str | None:
        if value is not None:
            return governance_identifier(value, label="governance operation")
        return value


class DependencyProofReferenceV1(_StrictCandidateModel):
    """Exact artifact dependency edge read while evaluating one member."""

    tag: Literal["playbill-dependency-proof-ref-v1"] = (
        "playbill-dependency-proof-ref-v1"
    )
    source_path: str
    source_artifact_digest: str
    target_path: str
    target_artifact_digest: str
    pin_role: str

    @field_validator("source_path", "target_path")
    @classmethod
    def _path(cls, value: str) -> str:
        normalized = tuple(normalize_manifest_paths((value,)))
        if normalized != (value,):
            raise ValueError("dependency proof path must be canonical")
        return value

    @field_validator("source_artifact_digest", "target_artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @field_validator("pin_role")
    @classmethod
    def _pin_role(cls, value: str) -> str:
        return governance_identifier(value, label="dependency proof pin role")


class LawEvaluationCoordinateV1(_StrictCandidateModel):
    """Path-free accepted coordinate at which a member law was evaluated."""

    tag: Literal["playbill-law-evaluation-coordinate-v1"] = (
        "playbill-law-evaluation-coordinate-v1"
    )
    git_oid: str
    semantic_root: str
    generation_root: str
    compiler_digest: str

    @field_validator("git_oid")
    @classmethod
    def _git_oid(cls, value: str) -> str:
        malformed = any(character not in "0123456789abcdef" for character in value)
        if len(value) not in {40, 64} or malformed:
            raise ValueError("law-evaluation Git OID is malformed")
        return value

    @field_validator("semantic_root")
    @classmethod
    def _semantic_root(cls, value: str) -> str:
        SemanticRoot.from_tagged(value)
        return value

    @field_validator("generation_root")
    @classmethod
    def _generation_root(cls, value: str) -> str:
        GenerationRoot.from_tagged(value)
        return value

    @field_validator("compiler_digest")
    @classmethod
    def _compiler_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class MemberLawEvaluationV2(_StrictCandidateModel):
    """Structured output whose digest is recorded beside one changed member."""

    tag: Literal["playbill-member-law-evaluation-v2"] = (
        "playbill-member-law-evaluation-v2"
    )
    path: str
    law_identifier: str
    law_digest: str
    evaluation_time: str
    evaluation_coordinate: LawEvaluationCoordinateV1
    dependency_proof_refs: tuple[DependencyProofReferenceV1, ...] = ()
    policy_digests: tuple[str, ...] = ()
    query_receipt_digests: tuple[str, ...] = ()
    result: dict[str, object]
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if tuple(normalize_manifest_paths((value,))) != (value,):
            raise ValueError("member law-evidence path must be canonical")
        return value

    @field_validator("law_identifier")
    @classmethod
    def _law_identifier(cls, value: str) -> str:
        return governance_identifier(value, label="member law-evidence identifier")

    @field_validator("law_digest")
    @classmethod
    def _law_digest(cls, value: str) -> str:
        AcceptanceLawDigest.from_tagged(value)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: str) -> str:
        return validate_candidate_timestamp(value)

    @field_validator("dependency_proof_refs")
    @classmethod
    def _proofs(
        cls, value: tuple[DependencyProofReferenceV1, ...]
    ) -> tuple[DependencyProofReferenceV1, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("dependency proof refs must be canonically sorted and unique")
        return value

    @field_validator("policy_digests", "query_receipt_digests")
    @classmethod
    def _digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("law-evidence digests must be sorted and unique")
        for item in value:
            Sha256Value.from_tagged(item)
        return value

    @field_validator("result")
    @classmethod
    def _result(cls, value: dict[str, object]) -> dict[str, object]:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):  # pragma: no cover - field type proves this
            raise ValueError("member law result must be a canonical object")
        return {str(key): item for key, item in normalized.items()}


def member_law_evidence_digest(evidence: MemberLawEvaluationV2) -> str:
    payload = evidence.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-member-law-evaluation-v2",
        payload,
    ).tagged


class CandidateMemberLawEvidenceV2(_StrictCandidateModel):
    tag: Literal["playbill-candidate-member-law-evidence-v2"] = (
        "playbill-candidate-member-law-evidence-v2"
    )
    path: str
    artifact_kind: str
    disposition: Literal["create", "replace", "retire", "delete"]
    predecessor_artifact_digest: str | None
    candidate_artifact_digest: str | None
    law_identifier: str
    law_digest: str
    law_evidence_digest: str
    closure_role: Literal["authored", "generated_successor", "invalidation"]
    dependency_proof_refs: tuple[DependencyProofReferenceV1, ...] = ()

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if tuple(normalize_manifest_paths((value,))) != (value,):
            raise ValueError("candidate member path must be canonical")
        return value

    @field_validator("artifact_kind", "law_identifier")
    @classmethod
    def _identifier(cls, value: str, info: object) -> str:
        return governance_identifier(value, label=str(getattr(info, "field_name", "identifier")))

    @field_validator("predecessor_artifact_digest", "candidate_artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @field_validator("law_digest")
    @classmethod
    def _law_digest(cls, value: str) -> str:
        AcceptanceLawDigest.from_tagged(value)
        return value

    @field_validator("law_evidence_digest")
    @classmethod
    def _law_evidence_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("dependency_proof_refs")
    @classmethod
    def _proofs(
        cls, value: tuple[DependencyProofReferenceV1, ...]
    ) -> tuple[DependencyProofReferenceV1, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("member dependency proofs must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _digest_shape(self) -> "CandidateMemberLawEvidenceV2":
        if self.disposition == "create":
            valid = (
                self.predecessor_artifact_digest is None
                and self.candidate_artifact_digest is not None
            )
        elif self.disposition == "delete":
            valid = (
                self.predecessor_artifact_digest is not None
                and self.candidate_artifact_digest is None
            )
        else:
            valid = (
                self.predecessor_artifact_digest is not None
                and self.candidate_artifact_digest is not None
            )
        if not valid:
            raise ValueError("candidate member disposition and before/after digests disagree")
        return self


class ClosureProofV2(_StrictCandidateModel):
    tag: Literal["playbill-closure-proof-v2"] = "playbill-closure-proof-v2"
    strategy: Literal["dependency-closure-v2"] = "dependency-closure-v2"
    paths: tuple[str, ...]
    dependency_graph_digest: str
    member_evidence_digest: str

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or tuple(normalize_manifest_paths(value)) != value:
            raise ValueError("closure proof paths must be nonempty, sorted, and unique")
        return value

    @field_validator("dependency_graph_digest", "member_evidence_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class CandidateRecordV2(_StrictCandidateModel):
    """Validated multi-member candidate without changing the frozen C_s."""

    tag: Literal["playbill-validated-candidate-v2"] = "playbill-validated-candidate-v2"
    candidate: SemanticCandidate
    candidate_digest: str
    required_tier: PermissionTier
    approval_requirements: tuple[ApprovalRequirement, ...]
    activation_policy: ActivationPolicy
    closure_proof: ClosureProofV2
    members: tuple[CandidateMemberLawEvidenceV2, ...]
    law_evidence: tuple[MemberLawEvaluationV2, ...]
    law_digests: dict[str, str]
    compiler_digest: str

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @field_validator("approval_requirements")
    @classmethod
    def _approval_requirements(
        cls, value: tuple[ApprovalRequirement, ...]
    ) -> tuple[ApprovalRequirement, ...]:
        identities = tuple((item.role, item.minimum_distinct_signers) for item in value)
        if identities != tuple(sorted(set(identities), key=lambda item: item[0].encode("utf-8"))):
            raise ValueError("v2 approval requirements must be sorted and unique")
        return value

    @field_validator("law_digests")
    @classmethod
    def _law_digests(cls, value: dict[str, str]) -> dict[str, str]:
        if not value or list(value) != sorted(value, key=lambda item: item.encode("utf-8")):
            raise ValueError("v2 acceptance-law mapping must be nonempty and sorted")
        for identifier, digest in value.items():
            governance_identifier(identifier, label="v2 acceptance-law identifier")
            AcceptanceLawDigest.from_tagged(digest)
        return value

    @field_validator("compiler_digest")
    @classmethod
    def _compiler_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _complete_binding(self) -> "CandidateRecordV2":
        if candidate_digest(self.candidate).tagged != self.candidate_digest:
            raise ValueError("v2 candidate digest does not reproduce")
        paths = tuple(member.path for member in self.members)
        if paths != self.candidate.scope or self.closure_proof.paths != self.candidate.scope:
            raise ValueError("v2 member/closure paths differ from C_s.scope")
        evidence_paths = tuple(item.path for item in self.law_evidence)
        if evidence_paths != paths:
            raise ValueError("v2 structured law evidence differs from member paths")
        if {item.law_identifier for item in self.members} != set(self.law_digests):
            raise ValueError("v2 members and law mapping differ")
        for member, evidence in zip(self.members, self.law_evidence, strict=True):
            if (
                member.law_identifier != evidence.law_identifier
                or member.law_digest != evidence.law_digest
                or self.law_digests[member.law_identifier] != member.law_digest
                or member.dependency_proof_refs != evidence.dependency_proof_refs
                or member.law_evidence_digest != member_law_evidence_digest(evidence)
            ):
                raise ValueError("v2 member does not reproduce its structured law evidence")
        member_payload = [item.model_dump(mode="json") for item in self.members]
        expected_member_digest = typed_digest(
            Sha256Value,
            "playbill-candidate-member-evidence-v2",
            {"members": member_payload},
        ).tagged
        if expected_member_digest != self.closure_proof.member_evidence_digest:
            raise ValueError("v2 closure member-evidence digest does not reproduce")
        return self


CandidateRecordLike = CandidateRecord | CandidateRecordV2


def render_candidate_record(record: CandidateRecord | CandidateRecordV2) -> bytes:
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


__all__ = [
    "CandidateMemberLawEvidenceV2",
    "CandidateRecord",
    "CandidateRecordLike",
    "CandidateRecordV2",
    "CandidateMemberEvidence",
    "ClosureProofV2",
    "DependencyProofReferenceV1",
    "LawEvaluationCoordinateV1",
    "MemberLawEvaluationV2",
    "SemanticCandidate",
    "candidate_digest",
    "canonical_candidate_timestamp",
    "render_candidate_record",
    "member_law_evidence_digest",
    "validate_candidate_timestamp",
]
