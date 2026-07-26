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


def artifact_revision_id(source_artifact_id: str, revision: int) -> str:
    """Physical id of one immutable revision of a logical source artifact.

    Registrations are insert-only, so the logical ``source_artifact_id`` alone
    cannot identify a row: an artifact re-registered with changed content keeps
    its logical id while the earlier manifest must stay addressable for the
    evidence refs already pinned to it.
    """
    return f"{source_artifact_id}@{revision}"


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
    """One immutable revision of a source artifact manifest.

    ``revision`` counts registrations of the same logical
    ``source_artifact_id``; ``superseded_by`` points forward to the revision
    that replaced this one as the current manifest, so a superseded manifest
    stays readable for evidence refs pinned to its ``content_hash``.
    """

    source_artifact_id: str
    artifact_revision_id: str = ""
    revision: int = 1
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
    superseded_by: str | None = None
    superseded_at: str | None = None
    drift_observed_hash: str | None = None
    drift_observed_at: str | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _derive_revision_id(self) -> SourceArtifactRecord:
        # Derived rather than required so every construction site cannot get
        # the physical id out of step with the (logical id, revision) pair.
        if not self.artifact_revision_id:
            object.__setattr__(
                self,
                "artifact_revision_id",
                artifact_revision_id(self.source_artifact_id, self.revision),
            )
        return self


class RegisterSourceArtifactResult(BaseModel):
    """Public result returned after registering a local evidence source."""

    source_artifact_id: str
    artifact_revision_id: str
    revision: int = 1
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
    supersedes: str | None = None
    already_registered: bool = False
    receipt_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class SourceEvidenceInput(BaseModel):
    """Unresolved source locator supplied by an agent or user."""

    source_artifact_id: str
    # Optional PIN to one immutable revision. Absent means "whatever revision is
    # current", which is the right default for a fresh locator and the wrong one
    # for replaying a citation made earlier.
    artifact_revision_id: str | None = None
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


class SourceArtifactListItem(BaseModel):
    """Metadata summary for a registered source artifact."""

    source_artifact_id: str
    kind: SourceKind
    retention: SourceRetention
    original_uri: str | None = None
    label: str | None = None
    content_hash: str
    registered_at: str
    chunk_count: int
    byte_count: int

    model_config = ConfigDict(extra="forbid")


class SourceArtifactReadChunk(BaseModel):
    """One addressable source artifact chunk, optionally including resolved text."""

    chunk_id: str
    heading_path: list[str] = Field(default_factory=list)
    block_selector: str
    block_type: str
    line_start: int
    line_end: int
    content_hash: str
    text: str | None = None

    model_config = ConfigDict(extra="forbid")


class SourceArtifactListResult(BaseModel):
    """Paginated source artifact listing."""

    items: list[SourceArtifactListItem] = Field(default_factory=list)
    total: int
    limit: int | None = None
    offset: int = 0
    truncated: bool = False

    model_config = ConfigDict(extra="forbid")


class SourceArtifactReadResult(SourceArtifactListItem):
    """Full source artifact read model with ordered chunks."""

    artifact_revision_id: str
    revision: int = 1
    parser_version: str
    archived: bool = False
    archive_content_hash: str | None = None
    content_available: bool
    content_unavailable_reason: str | None = None
    body_origin: DereferenceBodyOrigin | None = None
    current_artifact_hash: str | None = None
    drift_observed_hash: str | None = None
    drift_observed_at: str | None = None
    chunks: list[SourceArtifactReadChunk] = Field(default_factory=list)


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
    # The revision this read actually resolved against, always reported.
    artifact_revision_id: str | None = None
    # True when the caller pinned no revision and the read fell back to the
    # CURRENT one. An unpinned dereference is not wrong, but it is not a replay
    # of the citation either, and the difference must be visible rather than
    # inferred from a matching hash.
    revision_unpinned: bool = False

    model_config = ConfigDict(extra="forbid")
