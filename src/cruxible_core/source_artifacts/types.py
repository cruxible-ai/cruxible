"""Types for local source artifacts used as governed proposal evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_core.governance.actors import GovernedActorContext

SourceKind = Literal["markdown"]
SourceRetention = Literal["manifest_only", "archive"]
DereferenceStatus = Literal["available", "drifted", "unavailable"]
DereferenceBodyOrigin = Literal["archive", "local_path"]

MARKDOWN_CHUNKS_V1 = "markdown_chunks_v1"


class SourceArtifactChunk(BaseModel):
    """A deterministic parsed source block that can be cited by proposals."""

    chunk_id: str
    heading_path: list[str] = Field(default_factory=list)
    block_selector: str
    block_type: str
    content_hash: str
    line_start: int
    line_end: int
    preview: str | None = None
    label: str | None = None

    model_config = ConfigDict(extra="forbid")


class SourceArtifactRecord(BaseModel):
    """Persisted source artifact metadata and local dereference information."""

    source_artifact_id: str
    source_kind: SourceKind
    source_retention: SourceRetention
    original_uri: str | None = None
    label: str | None = None
    parser_version: str
    content_hash: str
    byte_count: int
    local_path: str | None = None
    archived: bool = False
    archive_content_hash: str | None = None
    created_at: str
    registered_actor_context: GovernedActorContext | None = None

    model_config = ConfigDict(extra="forbid")


class RegisterSourceArtifactResult(BaseModel):
    """Public result returned after registering a local evidence source."""

    source_artifact_id: str
    source_kind: SourceKind
    source_retention: SourceRetention
    original_uri: str | None = None
    label: str | None = None
    content_hash: str
    byte_count: int
    parser_version: str
    archived: bool = False
    archive_content_hash: str | None = None
    chunks: list[SourceArtifactChunk] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class SourceEvidenceInput(BaseModel):
    """Unresolved source locator supplied by an agent or user."""

    source_artifact_id: str
    chunk_id: str | None = None
    heading_path: list[str] | None = None
    block_selector: str | None = None
    label: str | None = None
    expected_content_hash: str | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_locator(self) -> SourceEvidenceInput:
        if not self.source_artifact_id.strip():
            raise ValueError("source_artifact_id is required")
        if self.chunk_id is not None:
            if not self.chunk_id.strip():
                raise ValueError("chunk_id must be non-empty when provided")
            return self
        if not self.heading_path or self.block_selector is None:
            raise ValueError(
                "source evidence requires chunk_id or heading_path plus block_selector"
            )
        if not self.block_selector.strip():
            raise ValueError("block_selector must be non-empty when provided")
        return self


class DereferenceSourceEvidenceResult(BaseModel):
    """Result of resolving a persisted source citation back to readable source text."""

    status: DereferenceStatus
    source_artifact_id: str
    chunk_id: str
    content_hash: str
    expected_artifact_hash: str
    current_artifact_hash: str | None = None
    body_origin: DereferenceBodyOrigin | None = None
    body: str | None = None
    reason: str | None = None
    chunk: SourceArtifactChunk | None = None

    model_config = ConfigDict(extra="forbid")
