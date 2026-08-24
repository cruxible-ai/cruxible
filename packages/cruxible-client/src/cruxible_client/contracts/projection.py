"""Pure coordinate contracts for accepted and provisional Playbill state."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.candidates import (
    SemanticCandidateLike,
    SemanticCandidateV2,
    candidate_digest,
)
from cruxible_client.contracts.canonical import (
    CandidateDigest,
    GenerationRoot,
    SemanticRoot,
    Sha256Value,
    manifest_root,
    semantic_projection,
)
from cruxible_client.contracts.errors import ProjectionCoordinateError
from cruxible_client.contracts.merkle import merkle_manifest_root
from cruxible_client.contracts.types import CompilerCoordinate, GitObjectFormat

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class _StrictProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _tagged_sha256(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an algorithm-tagged SHA-256 value") from exc
    return value


def _absolute_path(value: str, *, label: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError(f"{label} must be a canonical absolute path")
    return value


class AcceptedProjectionCoordinate(_StrictProjectionModel):
    """A ledger coordinate supplied only after signature/root-chain verification."""

    instance_id: str = Field(min_length=1, max_length=256)
    repository_path: str
    git_object_format: GitObjectFormat
    git_oid: str
    semantic_root: str
    generation_root: str
    compiler: CompilerCoordinate

    @field_validator("repository_path")
    @classmethod
    def _repository_path(cls, value: str) -> str:
        return _absolute_path(value, label="repository_path")

    @field_validator("git_oid")
    @classmethod
    def _git_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("git_oid must be lowercase SHA-1 or SHA-256 hex")
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

    @model_validator(mode="after")
    def _oid_matches_format(self) -> "AcceptedProjectionCoordinate":
        expected = 40 if self.git_object_format == "sha1" else 64
        if len(self.git_oid) != expected:
            raise ValueError("git_oid length does not match git_object_format")
        return self


class AcceptedCoordinate(_StrictProjectionModel):
    """Path-free public handle for one fully verified accepted generation."""

    tag: Literal["playbill-accepted-coordinate-v1"] = "playbill-accepted-coordinate-v1"
    git_oid: str
    semantic_root: str
    generation_root: str
    compiler_digest: str

    @field_validator("git_oid")
    @classmethod
    def _git_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("accepted-coordinate Git OID is malformed")
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
        return _tagged_sha256(value, label="compiler_digest")

    @classmethod
    def from_internal(cls, coordinate: AcceptedProjectionCoordinate) -> AcceptedCoordinate:
        return cls(
            git_oid=coordinate.git_oid,
            semantic_root=coordinate.semantic_root,
            generation_root=coordinate.generation_root,
            compiler_digest=coordinate.compiler.rule_digest,
        )


class CandidateGenerationProjectionCoordinate(_StrictProjectionModel):
    """A verified, unaccepted generation eligible only for durable prebuild."""

    tag: Literal["playbill-candidate-generation-coordinate-v1"] = (
        "playbill-candidate-generation-coordinate-v1"
    )
    instance_id: str = Field(min_length=1, max_length=256)
    repository_path: str
    git_object_format: GitObjectFormat
    git_oid: str
    semantic_root: str
    generation_root: str
    compiler: CompilerCoordinate
    base_git_oid: str

    @field_validator("repository_path")
    @classmethod
    def _repository_path(cls, value: str) -> str:
        return _absolute_path(value, label="repository_path")

    @field_validator("git_oid", "base_git_oid")
    @classmethod
    def _git_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("candidate generation Git OID is malformed")
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

    @model_validator(mode="after")
    def _oid_matches_format(self) -> "CandidateGenerationProjectionCoordinate":
        expected = 40 if self.git_object_format == "sha1" else 64
        if len(self.git_oid) != expected or len(self.base_git_oid) != expected:
            raise ValueError("candidate generation OID length does not match object format")
        if self.git_oid == self.base_git_oid:
            raise ValueError("candidate generation must differ from its settlement base")
        return self


class ProvisionalProjectionCoordinate(_StrictProjectionModel):
    """A proposed-state read coordinate binding an accepted base to one exact candidate."""

    tag: Literal["playbill-provisional-projection-coordinate-v1"] = (
        "playbill-provisional-projection-coordinate-v1"
    )
    canonical: AcceptedProjectionCoordinate
    candidate: SemanticCandidateLike
    candidate_digest: str

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _coordinate_binding(self) -> "ProvisionalProjectionCoordinate":
        if self.candidate.parent_semantic_root != self.canonical.semantic_root:
            raise ValueError("provisional candidate is not parented by the canonical coordinate")
        if candidate_digest(self.candidate).tagged != self.candidate_digest:
            raise ValueError("provisional candidate digest does not reproduce from C_s")
        return self


def verify_provisional_tree(
    tree: Mapping[str, bytes],
    *,
    coordinate: ProvisionalProjectionCoordinate,
) -> None:
    """Refuse a provisional tree that is not the one the coordinate's candidate signs.

    The candidate names the structure of its own commitment: a v2 candidate signs
    a merkle manifest root and a v1 a flat one, and the two spellings are
    disjoint. The root is therefore recomputed in the candidate's own structure
    and compared, so a provisional read is bound to the exact tree under review
    on either side of the succession, and the three artifact-kind readers ask the
    question once rather than each in its own words.
    """

    projected = semantic_projection(tree)
    actual = (
        merkle_manifest_root(projected).tagged
        if isinstance(coordinate.candidate, SemanticCandidateV2)
        else manifest_root(projected).tagged
    )
    if actual != coordinate.candidate.candidate_manifest_root:
        raise ProjectionCoordinateError(
            "provisional tree differs from the candidate manifest coordinate"
        )


__all__ = [
    "AcceptedCoordinate",
    "AcceptedProjectionCoordinate",
    "CandidateGenerationProjectionCoordinate",
    "ProvisionalProjectionCoordinate",
    "verify_provisional_tree",
]
