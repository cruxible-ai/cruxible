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
    ) -> str: ...

    @abstractmethod
    def get_artifact(self, source_artifact_id: str) -> SourceArtifactRecord | None: ...
    @abstractmethod
    def list_chunks(self, source_artifact_id: str) -> list[SourceArtifactChunk]: ...
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
