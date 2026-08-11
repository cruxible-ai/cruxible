"""Frozen semantic-candidate preimage and immutable PB-C candidate records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.canonical import (
    CandidateDigest,
    SemanticDiffDigest,
    SemanticManifestRoot,
    SemanticRoot,
    canonical_bytes,
    normalize_manifest_paths,
    typed_digest,
)
from cruxible_core.playbill.documents import (
    DocumentActivationPolicy,
    DocumentApprovalRole,
    PermissionTier,
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
    """Immutable proposal evidence containing the complete reviewed C_s body."""

    tag: Literal["playbill-candidate-record-v1"] = "playbill-candidate-record-v1"
    candidate: SemanticCandidate
    candidate_digest: str
    required_tier: PermissionTier
    approval_scope: tuple[DocumentApprovalRole, ...]
    activation_policy: DocumentActivationPolicy
    closure_paths: tuple[str, ...]

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _complete_binding(self) -> "CandidateRecord":
        if candidate_digest(self.candidate).tagged != self.candidate_digest:
            raise ValueError("candidate record digest does not reproduce from its complete C_s")
        if self.closure_paths != self.candidate.scope:
            raise ValueError("PB-C singleton closure must equal the complete candidate scope")
        return self


def render_candidate_record(record: CandidateRecord) -> bytes:
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


__all__ = [
    "CandidateRecord",
    "SemanticCandidate",
    "candidate_digest",
    "canonical_candidate_timestamp",
    "render_candidate_record",
    "validate_candidate_timestamp",
]
