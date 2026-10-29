"""Frozen semantic-candidate preimage and immutable PB-C candidate records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.canonical import (
    AcceptanceLawDigest,
    ArtifactDigest,
    CandidateDigest,
    DependencyEdgeRoot,
    GenerationRoot,
    SemanticDiffDigest,
    SemanticMerkleRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    canonical_list_digest,
    model_canonical_bytes,
    normalize_canonical,
    normalize_manifest_paths,
    typed_digest,
)
from cruxible_client.contracts.content_memo import seal, sealed_memo
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.governance import (
    ActivationPolicy,
    ApprovalRequirement,
    PermissionTier,
    governance_identifier,
    validate_approval_requirements,
)
from cruxible_client.contracts.primitives import canonical_json


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
    """The complete locator-free object signed by reviewers, with a merkle manifest root.

    The validator asks for exactly this root type and nothing else parses: a
    flat manifest root is refused here, so the structure of a candidate's
    commitment can never be ambiguous.
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


@sealed_memo
def candidate_digest(candidate: SemanticCandidate) -> CandidateDigest:
    """Hash exactly the five C_s fields under the candidate's own tag domain."""

    payload = candidate.model_dump(mode="json")
    domain = str(payload.pop("tag"))
    return typed_digest(CandidateDigest, domain, payload)


class DependencyProofReference(_StrictCandidateModel):
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


@sealed_memo
def dependency_proof_order(reference: DependencyProofReference) -> bytes:
    """A proof reference's canonical bytes: the order proof sets are sorted and unique in.

    Every field is a validated string, so when all of them are ASCII (paths,
    digests and pin roles always are in practice) the canonical encoding is
    exactly the compact sorted-key JSON of the dump: an ASCII string is its own
    NFC form and holds no surrogate, and ASCII sorts the same as its UTF-8. That
    JSON is written directly; any other reference takes the full canonical path.
    """

    payload = reference.model_dump(mode="json")
    if all(type(value) is str and value.isascii() for value in payload.values()):
        return canonical_json(payload).encode("ascii")
    return canonical_bytes(payload)


def _sorted_unique_proofs(
    value: tuple[DependencyProofReference, ...], *, message: str
) -> tuple[DependencyProofReference, ...]:
    encoded = tuple(dependency_proof_order(item) for item in value)
    if encoded != tuple(sorted(set(encoded))):
        raise ValueError(message)
    return value


class LawEvaluationCoordinate(_StrictCandidateModel):
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


class MemberLawEvaluation(_StrictCandidateModel):
    """Structured output whose digest is recorded beside one changed member."""

    tag: Literal["playbill-member-law-evaluation-v2"] = "playbill-member-law-evaluation-v2"
    path: str
    law_identifier: str
    law_digest: str
    evaluation_time: str
    evaluation_coordinate: LawEvaluationCoordinate
    dependency_proof_refs: tuple[DependencyProofReference, ...] = ()
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
        cls, value: tuple[DependencyProofReference, ...]
    ) -> tuple[DependencyProofReference, ...]:
        return _sorted_unique_proofs(
            value, message="dependency proof refs must be canonically sorted and unique"
        )

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

    @model_validator(mode="after")
    def _sealed(self) -> "MemberLawEvaluation":
        # A validated member entry is final, so it is sealed at once (see
        # ``content_memo``): one parse of a candidate or receipt checks its law
        # evidence digest, the closure digest, the receipt digest and the
        # canonical form, and each reads this entry's one remembered encoding.
        # Copies (``model_copy``, ``deepcopy``) are never sealed.
        seal(self)
        return self


@sealed_memo
def member_law_evidence_digest(evidence: MemberLawEvaluation) -> str:
    payload = evidence.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-member-law-evaluation-v2",
        payload,
    ).tagged


class CandidateMemberLawEvidence(_StrictCandidateModel):
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
    dependency_proof_refs: tuple[DependencyProofReference, ...] = ()

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
        cls, value: tuple[DependencyProofReference, ...]
    ) -> tuple[DependencyProofReference, ...]:
        return _sorted_unique_proofs(
            value, message="member dependency proofs must be sorted and unique"
        )

    @model_validator(mode="after")
    def _digest_shape(self) -> "CandidateMemberLawEvidence":
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

    @model_validator(mode="after")
    def _sealed(self) -> "CandidateMemberLawEvidence":
        seal(self)  # final once validated, as MemberLawEvaluation._sealed
        return self


def _validated_closure_proof_paths(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value or tuple(normalize_manifest_paths(value)) != value:
        raise ValueError("closure proof paths must be nonempty, sorted, and unique")
    return value


class ClosureProof(_StrictCandidateModel):
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


def candidate_member_evidence_digest(
    members: tuple[CandidateMemberLawEvidence, ...],
) -> str:
    """Hash the ordered member evidence under its own unchanged frozen domain.

    The preimage is composed from each member's own canonical bytes, which a
    sealed member keeps: re-verifying a candidate built from sealed members
    re-encodes none of them.
    """

    return Sha256Value(
        canonical_list_digest(
            "playbill-candidate-member-evidence-v2",
            "members",
            [model_canonical_bytes(item) for item in members],
        )
    ).tagged


def _verify_multi_member_binding(
    *,
    candidate: SemanticCandidate,
    candidate_digest_value: str,
    closure_proof: ClosureProof,
    members: tuple[CandidateMemberLawEvidence, ...],
    law_evidence: tuple[MemberLawEvaluation, ...],
    law_digests: dict[str, str],
    label: str,
) -> None:
    """Check the closed candidate/member/law correspondence of one candidate record."""

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
    return validate_approval_requirements(value, label=label)


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


class CandidateRecord(_StrictCandidateModel):
    """The one validated candidate: a v2 C_s with its v3 closure proof.

    Every accepted generation is judged against one of these; settlement and
    replay know no other candidate shape.
    """

    tag: Literal["playbill-validated-candidate-v3"] = "playbill-validated-candidate-v3"
    candidate: SemanticCandidate
    candidate_digest: str
    required_tier: PermissionTier
    approval_requirements: tuple[ApprovalRequirement, ...]
    activation_policy: ActivationPolicy
    closure_proof: ClosureProof
    members: tuple[CandidateMemberLawEvidence, ...]
    law_evidence: tuple[MemberLawEvaluation, ...]
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
    def _complete_binding(self) -> "CandidateRecord":
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


def render_candidate_record(record: CandidateRecord) -> bytes:
    return model_canonical_bytes(record) + b"\n"


__all__ = [
    "CandidateMemberLawEvidence",
    "CandidateRecord",
    "ClosureProof",
    "DependencyProofReference",
    "LawEvaluationCoordinate",
    "MemberLawEvaluation",
    "SemanticCandidate",
    "candidate_digest",
    "candidate_member_evidence_digest",
    "canonical_candidate_timestamp",
    "dependency_proof_order",
    "render_candidate_record",
    "member_law_evidence_digest",
    "validate_candidate_timestamp",
]
