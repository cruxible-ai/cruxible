"""Frozen semantic-candidate preimage and immutable PB-C candidate records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.canonical import (
    AcceptanceLawDigest,
    ArtifactDigest,
    CandidateDigest,
    DependencyEdgeRoot,
    GenerationRoot,
    SemanticDiffDigest,
    SemanticManifestRoot,
    SemanticMerkleRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    normalize_manifest_paths,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.governance import (
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


def _validated_candidate_scope(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        raise ValueError("candidate scope must not be empty")
    normalized = tuple(normalize_manifest_paths(value))
    if value != normalized:
        raise ValueError("candidate scope must be normalized, sorted, and unique")
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
        return _validated_candidate_scope(value)

    @field_validator("timestamp")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_candidate_timestamp(value)


class SemanticCandidateV2(_StrictCandidateModel):
    """The same five C_s fields, with the manifest root carried as a merkle root.

    The merkle root *replaces* the flat root; a v2 candidate never carries both,
    and the two spellings are disjoint, so the version of a candidate and the
    structure of the commitment it signs can never disagree. A flat root is
    refused here exactly as a merkle root is refused by v1: the validator asks
    for its own root type and nothing else parses.

    Nothing produces a v2 candidate yet. The format and its verifier land first.
    """

    tag: Literal["playbill-candidate-v2"] = "playbill-candidate-v2"
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
        SemanticMerkleRoot.from_tagged(value)
        return value

    @field_validator("semantic_diff_digest")
    @classmethod
    def _semantic_diff_digest(cls, value: str) -> str:
        SemanticDiffDigest.from_tagged(value)
        return value

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_candidate_scope(value)

    @field_validator("timestamp")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_candidate_timestamp(value)


SemanticCandidateLike = SemanticCandidate | SemanticCandidateV2


def candidate_digest(candidate: SemanticCandidateLike) -> CandidateDigest:
    """Hash exactly the five C_s fields under the candidate's own version domain.

    The domain is read off the object's own frozen `tag`, so v1 hashes under
    `playbill-candidate-v1` byte-for-byte as it always has and a v2 candidate can
    never collide with the v1 candidate that carries the same five values.
    """

    payload = candidate.model_dump(mode="json")
    domain = str(payload.pop("tag"))
    return typed_digest(CandidateDigest, domain, payload)


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

    tag: Literal["playbill-dependency-proof-ref-v1"] = "playbill-dependency-proof-ref-v1"
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

    tag: Literal["playbill-law-evaluation-coordinate-v1"] = "playbill-law-evaluation-coordinate-v1"
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

    tag: Literal["playbill-member-law-evaluation-v2"] = "playbill-member-law-evaluation-v2"
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


def _validated_closure_proof_paths(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value or tuple(normalize_manifest_paths(value)) != value:
        raise ValueError("closure proof paths must be nonempty, sorted, and unique")
    return value


class ClosureProofV2(_StrictCandidateModel):
    tag: Literal["playbill-closure-proof-v2"] = "playbill-closure-proof-v2"
    strategy: Literal["dependency-closure-v2"] = "dependency-closure-v2"
    paths: tuple[str, ...]
    dependency_graph_digest: str
    member_evidence_digest: str

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_closure_proof_paths(value)

    @field_validator("dependency_graph_digest", "member_evidence_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ClosureProofV3(_StrictCandidateModel):
    """The v2 closure proof with an incrementally maintainable edge commitment.

    `dependency_graph_digest` hashed the full edge lists of both trees, so it
    could only ever be recomputed in full. `dependency_edge_root` commits to the
    same candidate-tree edges through a per-source-member merkle trie, so a
    change touching a handful of members re-hashes a handful of edge sets. The
    parent tree's edge root is not restated here: the parent is byte-determined
    by `C_s.parent_semantic_root`, so its root is derived, never asserted.

    Everything else -- the closure paths and the member-evidence digest, which
    still covers the unchanged `playbill-candidate-member-evidence-v2` member
    shape -- is the v2 proof unchanged.
    """

    tag: Literal["playbill-closure-proof-v3"] = "playbill-closure-proof-v3"
    strategy: Literal["dependency-closure-v3"] = "dependency-closure-v3"
    paths: tuple[str, ...]
    dependency_edge_root: str
    member_evidence_digest: str

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_closure_proof_paths(value)

    @field_validator("dependency_edge_root")
    @classmethod
    def _edge_root(cls, value: str) -> str:
        DependencyEdgeRoot.from_tagged(value)
        return value

    @field_validator("member_evidence_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


ClosureProofLike = ClosureProofV2 | ClosureProofV3


def candidate_member_evidence_digest(
    members: tuple[CandidateMemberLawEvidenceV2, ...],
) -> str:
    """Hash the ordered member evidence under its own unchanged frozen domain."""

    return typed_digest(
        Sha256Value,
        "playbill-candidate-member-evidence-v2",
        {"members": [item.model_dump(mode="json") for item in members]},
    ).tagged


def _verify_multi_member_binding(
    *,
    candidate: SemanticCandidateLike,
    candidate_digest_value: str,
    closure_proof: ClosureProofLike,
    members: tuple[CandidateMemberLawEvidenceV2, ...],
    law_evidence: tuple[MemberLawEvaluationV2, ...],
    law_digests: dict[str, str],
    label: str,
) -> None:
    """Check the closed candidate/member/law correspondence shared by v2 and v3.

    The two record versions differ only in which candidate and closure-proof
    versions they embed; the correspondence they must both close is one rule, so
    it is written once and reported under the caller's own version label.
    """

    if candidate_digest(candidate).tagged != candidate_digest_value:
        raise ValueError(f"{label} candidate digest does not reproduce")
    paths = tuple(member.path for member in members)
    if paths != candidate.scope or closure_proof.paths != candidate.scope:
        raise ValueError(f"{label} member/closure paths differ from C_s.scope")
    if tuple(item.path for item in law_evidence) != paths:
        raise ValueError(f"{label} structured law evidence differs from member paths")
    if {item.law_identifier for item in members} != set(law_digests):
        raise ValueError(f"{label} members and law mapping differ")
    for member, evidence in zip(members, law_evidence, strict=True):
        if (
            member.law_identifier != evidence.law_identifier
            or member.law_digest != evidence.law_digest
            or law_digests[member.law_identifier] != member.law_digest
            or member.dependency_proof_refs != evidence.dependency_proof_refs
            or member.law_evidence_digest != member_law_evidence_digest(evidence)
        ):
            raise ValueError(f"{label} member does not reproduce its structured law evidence")
    if candidate_member_evidence_digest(members) != closure_proof.member_evidence_digest:
        raise ValueError(f"{label} closure member-evidence digest does not reproduce")


def _validated_multi_member_approval_requirements(
    value: tuple[ApprovalRequirement, ...],
    *,
    label: str,
) -> tuple[ApprovalRequirement, ...]:
    identities = tuple((item.role, item.minimum_distinct_signers) for item in value)
    if identities != tuple(sorted(set(identities), key=lambda item: item[0].encode("utf-8"))):
        raise ValueError(f"{label} approval requirements must be sorted and unique")
    return value


def _validated_multi_member_law_digests(
    value: dict[str, str],
    *,
    label: str,
) -> dict[str, str]:
    if not value or list(value) != sorted(value, key=lambda item: item.encode("utf-8")):
        raise ValueError(f"{label} acceptance-law mapping must be nonempty and sorted")
    for identifier, digest in value.items():
        governance_identifier(identifier, label=f"{label} acceptance-law identifier")
        AcceptanceLawDigest.from_tagged(digest)
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
        return _validated_multi_member_approval_requirements(value, label="v2")

    @field_validator("law_digests")
    @classmethod
    def _law_digests(cls, value: dict[str, str]) -> dict[str, str]:
        return _validated_multi_member_law_digests(value, label="v2")

    @field_validator("compiler_digest")
    @classmethod
    def _compiler_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _complete_binding(self) -> "CandidateRecordV2":
        _verify_multi_member_binding(
            candidate=self.candidate,
            candidate_digest_value=self.candidate_digest,
            closure_proof=self.closure_proof,
            members=self.members,
            law_evidence=self.law_evidence,
            law_digests=self.law_digests,
            label="v2",
        )
        return self


class CandidateRecordV3(_StrictCandidateModel):
    """The v2 validated candidate carrying a v2 C_s and a v3 closure proof.

    Only the two embedded wire versions move. Member evidence, structured law
    evidence, tier, approval requirements, and activation policy are the v2
    shapes unchanged, and the correspondence between them is the same rule.

    Nothing produces one of these: settlement, acceptance, and replay refuse it.
    """

    tag: Literal["playbill-validated-candidate-v3"] = "playbill-validated-candidate-v3"
    candidate: SemanticCandidateV2
    candidate_digest: str
    required_tier: PermissionTier
    approval_requirements: tuple[ApprovalRequirement, ...]
    activation_policy: ActivationPolicy
    closure_proof: ClosureProofV3
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
        return _validated_multi_member_approval_requirements(value, label="v3")

    @field_validator("law_digests")
    @classmethod
    def _law_digests(cls, value: dict[str, str]) -> dict[str, str]:
        return _validated_multi_member_law_digests(value, label="v3")

    @field_validator("compiler_digest")
    @classmethod
    def _compiler_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _complete_binding(self) -> "CandidateRecordV3":
        _verify_multi_member_binding(
            candidate=self.candidate,
            candidate_digest_value=self.candidate_digest,
            closure_proof=self.closure_proof,
            members=self.members,
            law_evidence=self.law_evidence,
            law_digests=self.law_digests,
            label="v3",
        )
        return self


CandidateRecordLike = CandidateRecord | CandidateRecordV2
CandidateRecordAnyVersion = CandidateRecord | CandidateRecordV2 | CandidateRecordV3

CandidateWireVersion = Literal[
    "playbill-validated-candidate-v1",
    "playbill-validated-candidate-v2",
    "playbill-validated-candidate-v3",
]
"""Which validated-candidate shape an evaluation is asked to produce.

Production only ever asks for the newest, and nothing chooses this by preference.
Re-verifying an accepted generation asks for the shape that generation actually
settled in, read off its own receipt, because a candidate is only reproduced in
order to be compared with the one accepted history recorded -- and a comparison
between two different wire shapes of the same judgement would be a change of
verification, not a succession of formats.
"""

PRODUCED_CANDIDATE_VERSION: Final[CandidateWireVersion] = "playbill-validated-candidate-v3"
"""The one candidate version this build produces for a new proposal."""


def render_candidate_record(record: CandidateRecordAnyVersion) -> bytes:
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


__all__ = [
    "PRODUCED_CANDIDATE_VERSION",
    "CandidateMemberLawEvidenceV2",
    "CandidateWireVersion",
    "CandidateRecord",
    "CandidateRecordAnyVersion",
    "CandidateRecordLike",
    "CandidateRecordV2",
    "CandidateRecordV3",
    "CandidateMemberEvidence",
    "ClosureProofLike",
    "ClosureProofV2",
    "ClosureProofV3",
    "DependencyProofReferenceV1",
    "LawEvaluationCoordinateV1",
    "MemberLawEvaluationV2",
    "SemanticCandidate",
    "SemanticCandidateLike",
    "SemanticCandidateV2",
    "candidate_digest",
    "candidate_member_evidence_digest",
    "canonical_candidate_timestamp",
    "render_candidate_record",
    "member_law_evidence_digest",
    "validate_candidate_timestamp",
]
