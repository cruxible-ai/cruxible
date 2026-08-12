"""Frozen semantic-candidate preimage and immutable PB-C candidate records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.canonical import (
    AcceptanceLawDigest,
    CandidateDigest,
    SemanticDiffDigest,
    SemanticManifestRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    normalize_manifest_paths,
    typed_digest,
)
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
    disposition: MutationDisposition
    law_identifier: str

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

    @field_validator("law_identifier")
    @classmethod
    def _law_identifier(cls, value: str) -> str:
        return governance_identifier(value, label="acceptance-law identifier")


def render_candidate_record(record: CandidateRecord) -> bytes:
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


__all__ = [
    "CandidateRecord",
    "CandidateMemberEvidence",
    "SemanticCandidate",
    "candidate_digest",
    "canonical_candidate_timestamp",
    "render_candidate_record",
    "validate_candidate_timestamp",
]
