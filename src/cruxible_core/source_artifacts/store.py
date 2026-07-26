"""Source artifact store protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod

from cruxible_core.source_artifacts.types import (
    SourceArtifactChunk,
    SourceArtifactRecord,
)


class SourceArtifactStoreProtocol(ABC):
    """Interface for source artifact manifests and optional archived content."""

    @abstractmethod
    def save_artifact(
        self,
        record: SourceArtifactRecord,
        chunks: list[SourceArtifactChunk],
        *,
        archive_content: bytes | None = None,
        archive_media_type: str = "text/markdown",
    ) -> str:
        """Insert one immutable artifact revision, superseding the current one."""

    @abstractmethod
    def get_artifact(self, source_artifact_id: str) -> SourceArtifactRecord | None:
        """Return the current (non-superseded) revision of a logical artifact."""

    @abstractmethod
    def get_artifact_revision(self, artifact_revision_id: str) -> SourceArtifactRecord | None:
        """Return one specific revision, superseded or not."""

    @abstractmethod
    def list_artifact_revisions(self, source_artifact_id: str) -> list[SourceArtifactRecord]:
        """Return every revision of a logical artifact, oldest first."""

    @abstractmethod
    def record_content_drift(
        self,
        artifact_revision_id: str,
        *,
        observed_hash: str | None,
        observed_at: str | None,
    ) -> bool:
        """Persist (or clear) the last observed local-content drift for a revision."""

    @abstractmethod
    def list_artifacts(self) -> list[SourceArtifactRecord]: ...
    @abstractmethod
    def list_chunks(self, source_artifact_id: str) -> list[SourceArtifactChunk]: ...
    @abstractmethod
    def list_revision_chunks(self, artifact_revision_id: str) -> list[SourceArtifactChunk]: ...
    @abstractmethod
    def get_chunk(
        self,
        source_artifact_id: str,
        chunk_id: str,
    ) -> SourceArtifactChunk | None: ...
    @abstractmethod
    def find_chunks(
        self,
        source_artifact_id: str,
        *,
        heading_path: list[str],
        block_selector: str,
    ) -> list[SourceArtifactChunk]: ...
    @abstractmethod
    def get_archive_content(self, content_hash: str) -> bytes | None: ...
    @abstractmethod
    def close(self) -> None: ...
