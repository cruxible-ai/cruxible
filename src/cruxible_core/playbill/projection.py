"""Backend-neutral contracts for deterministic immutable Playbill projections."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.candidates import SemanticCandidate, candidate_digest
from cruxible_core.playbill.canonical import (
    CandidateDigest,
    GenerationRoot,
    LogicalDigest,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    canonical_digest,
)
from cruxible_core.playbill.projection_tree import TreeReadLimits
from cruxible_core.playbill.types import CompilerCoordinate, GitObjectFormat

PROJECTION_SCHEMA_VERSION = 1

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PIECE_RE = re.compile(r"^piece-[0-9a-f]{64}-[0-9]{4}\.sqlite$")


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


class ProvisionalProjectionCoordinate(_StrictProjectionModel):
    """A candidate overlay labeled with both canonical and provisional identity."""

    tag: Literal["playbill-provisional-projection-coordinate-v1"] = (
        "playbill-provisional-projection-coordinate-v1"
    )
    canonical: AcceptedProjectionCoordinate
    candidate: SemanticCandidate
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


class AssemblerRequest(_StrictProjectionModel):
    """Serializable v1 input contract for the Python reference assembler."""

    tag: Literal["playbill-assembler-request-v1"] = "playbill-assembler-request-v1"
    contract_version: Literal[1] = 1
    instance_id: str = Field(min_length=1, max_length=256)
    repository_path: str
    git_object_format: GitObjectFormat
    git_oid: str
    semantic_root: str
    generation_root: str
    compiler_digest: str
    schema_version: Literal[1] = 1
    output_staging_directory: str
    limits: TreeReadLimits = TreeReadLimits()

    @field_validator("repository_path", "output_staging_directory")
    @classmethod
    def _path(cls, value: str, info: object) -> str:
        label = cast(str, getattr(info, "field_name", "path"))
        return _absolute_path(value, label=label)

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

    @field_validator("compiler_digest")
    @classmethod
    def _compiler_digest(cls, value: str) -> str:
        return _tagged_sha256(value, label="compiler_digest")

    @model_validator(mode="after")
    def _oid_matches_format(self) -> "AssemblerRequest":
        expected = 40 if self.git_object_format == "sha1" else 64
        if len(self.git_oid) != expected:
            raise ValueError("git_oid length does not match git_object_format")
        return self


class ProjectionPiece(_StrictProjectionModel):
    ordinal: int = Field(ge=0)
    name: str
    format: Literal["sqlite-v1"] = "sqlite-v1"
    byte_length: int = Field(ge=1)
    physical_digest: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _PIECE_RE.fullmatch(value):
            raise ValueError("projection piece name is not canonical")
        return value

    @field_validator("physical_digest")
    @classmethod
    def _physical_digest(cls, value: str) -> str:
        return _tagged_sha256(value, label="physical_digest")


class ProjectionManifest(_StrictProjectionModel):
    """Atomic serving contract. It is a binding record, never ledger authority."""

    tag: Literal["playbill-projection-manifest-v1"] = "playbill-projection-manifest-v1"
    manifest_version: Literal[1] = 1
    instance_id: str
    git_object_format: GitObjectFormat
    git_oid: str
    semantic_root: str
    generation_root: str
    compiler_digest: str
    schema_version: Literal[1] = 1
    pieces: tuple[ProjectionPiece, ...]
    logical_digest: str
    row_counts: dict[str, int]

    @field_validator("git_oid")
    @classmethod
    def _git_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("manifest git_oid is malformed")
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

    @field_validator("logical_digest")
    @classmethod
    def _logical_digest(cls, value: str) -> str:
        LogicalDigest.from_tagged(value)
        return value

    @field_validator("row_counts")
    @classmethod
    def _row_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(count < 0 for count in value.values()):
            raise ValueError("row counts cannot be negative")
        if list(value) != sorted(value, key=lambda item: item.encode("utf-8")):
            raise ValueError("row counts must be sorted by table name")
        return value

    @model_validator(mode="after")
    def _piece_order(self) -> "ProjectionManifest":
        if not self.pieces:
            raise ValueError("projection manifest must contain at least one piece")
        if [piece.ordinal for piece in self.pieces] != list(range(len(self.pieces))):
            raise ValueError("projection pieces must be ordered contiguously")
        expected = 40 if self.git_object_format == "sha1" else 64
        if len(self.git_oid) != expected:
            raise ValueError("manifest git_oid length does not match object format")
        return self


class BuildInstrumentation(_StrictProjectionModel):
    phase_nanoseconds: dict[str, int]
    high_water_memory_bytes: int | None = Field(default=None, ge=0)


class AssemblerResult(_StrictProjectionModel):
    """Serializable v1 output contract, including operational measurements."""

    tag: Literal["playbill-assembler-result-v1"] = "playbill-assembler-result-v1"
    contract_version: Literal[1] = 1
    manifest_path: str
    manifest: ProjectionManifest
    git_oid: str
    semantic_root: str
    generation_root: str
    logical_digest: str
    row_counts: dict[str, int]
    instrumentation: BuildInstrumentation

    @field_validator("manifest_path")
    @classmethod
    def _manifest_path(cls, value: str) -> str:
        return _absolute_path(value, label="manifest_path")


OrphanKind = Literal[
    "staging-build",
    "unreferenced-piece",
    "malformed-manifest",
    "missing-piece",
]


class ProjectionOrphan(_StrictProjectionModel):
    kind: OrphanKind
    path: str
    detail: str


def projection_coordinate_key(request: AssemblerRequest | ProjectionManifest) -> str:
    return canonical_digest(
        "playbill-projection-coordinate-v1",
        {
            "instance_id": request.instance_id,
            "git_object_format": request.git_object_format,
            "git_oid": request.git_oid,
            "semantic_root": request.semantic_root,
            "generation_root": request.generation_root,
            "compiler_digest": request.compiler_digest,
            "schema_version": request.schema_version,
        },
    )


def projection_manifest_name(request: AssemblerRequest | ProjectionManifest) -> str:
    return f"projection-{projection_coordinate_key(request)}.json"


def projection_piece_name(request: AssemblerRequest, ordinal: int = 0) -> str:
    return f"piece-{projection_coordinate_key(request)}-{ordinal:04d}.sqlite"


def render_projection_manifest(manifest: ProjectionManifest) -> bytes:
    return canonical_bytes(manifest.model_dump(mode="json")) + b"\n"


__all__ = [
    "AcceptedProjectionCoordinate",
    "AssemblerRequest",
    "AssemblerResult",
    "BuildInstrumentation",
    "PROJECTION_SCHEMA_VERSION",
    "ProjectionManifest",
    "ProjectionOrphan",
    "ProjectionPiece",
    "ProvisionalProjectionCoordinate",
    "projection_coordinate_key",
    "projection_manifest_name",
    "projection_piece_name",
    "render_projection_manifest",
]
