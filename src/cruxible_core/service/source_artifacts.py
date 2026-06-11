"""Service functions for source artifact registration and dereference."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import (
    GovernedActorContext,
    dump_actor_context,
)
from cruxible_core.graph.evidence import EvidenceRef, merge_evidence_ref_objects
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.primitives import new_id
from cruxible_core.source_artifacts.markdown import parse_markdown_chunks
from cruxible_core.source_artifacts.store import SourceArtifactStoreProtocol
from cruxible_core.source_artifacts.types import (
    MARKDOWN_CHUNKS_V1,
    DereferenceSourceEvidenceResult,
    RegisterSourceArtifactResult,
    SourceArtifactChunk,
    SourceArtifactRecord,
    SourceEvidenceInput,
    SourceKind,
    SourceRetention,
)
from cruxible_core.temporal import format_datetime, utc_now


def service_register_source_artifact(
    instance: InstanceProtocol,
    *,
    source_path: str,
    source_kind: SourceKind = "markdown",
    source_retention: SourceRetention = "manifest_only",
    original_uri: str | None = None,
    label: str | None = None,
    parser_version: str = MARKDOWN_CHUNKS_V1,
    actor_context: GovernedActorContext | None = None,
) -> RegisterSourceArtifactResult:
    """Register a local source document as proposal evidence."""
    if source_kind != "markdown":
        raise ConfigError(f"Unsupported source_kind '{source_kind}'")
    if source_retention not in ("manifest_only", "archive"):
        raise ConfigError(f"Unsupported source_retention '{source_retention}'")
    if parser_version != MARKDOWN_CHUNKS_V1:
        raise ConfigError(f"Unsupported parser_version '{parser_version}'")

    path = _resolve_source_path(instance, source_path)
    if not path.is_file():
        raise ConfigError(f"Source artifact path is not a file: {source_path}")
    content = path.read_bytes()
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("Only UTF-8 Markdown source artifacts are supported") from exc
    content_hash = _sha256_bytes(content)
    source_artifact_id = new_id("SRC")
    chunks = parse_markdown_chunks(
        source_artifact_id=source_artifact_id,
        content=content,
        parser_version=parser_version,
    )
    if not chunks:
        raise ConfigError("Source artifact did not produce any addressable chunks")

    archived = source_retention == "archive"
    created_at = format_datetime(utc_now())
    assert created_at is not None
    record = SourceArtifactRecord(
        source_artifact_id=source_artifact_id,
        source_kind=source_kind,
        source_retention=source_retention,
        original_uri=_default_original_uri(instance, path, original_uri),
        label=label,
        parser_version=parser_version,
        content_hash=content_hash,
        byte_count=len(content),
        local_path=str(path),
        archived=archived,
        archive_content_hash=content_hash if archived else None,
        created_at=created_at,
        registered_actor_context=actor_context,
    )
    with instance.write_transaction() as uow:
        uow.source_artifacts.save_artifact(
            record,
            chunks,
            archive_content=content if archived else None,
        )

    return RegisterSourceArtifactResult(
        source_artifact_id=source_artifact_id,
        source_kind=source_kind,
        source_retention=source_retention,
        original_uri=record.original_uri,
        label=label,
        content_hash=content_hash,
        byte_count=len(content),
        parser_version=parser_version,
        archived=archived,
        archive_content_hash=record.archive_content_hash,
        chunks=chunks,
    )


def service_dereference_source_evidence(
    instance: InstanceProtocol,
    *,
    source_artifact_id: str,
    chunk_id: str | None = None,
    heading_path: list[str] | None = None,
    block_selector: str | None = None,
    expected_content_hash: str | None = None,
) -> DereferenceSourceEvidenceResult:
    """Resolve a source-evidence locator and return drift-aware source text."""
    source_evidence = SourceEvidenceInput(
        source_artifact_id=source_artifact_id,
        chunk_id=chunk_id,
        heading_path=heading_path,
        block_selector=block_selector,
        expected_content_hash=expected_content_hash,
    )
    store = instance.get_source_artifact_store()
    try:
        artifact = store.get_artifact(source_evidence.source_artifact_id)
        if artifact is None:
            raise ConfigError(f"Source artifact '{source_evidence.source_artifact_id}' not found")
        chunk = _resolve_chunk(store, source_evidence)
        if (
            source_evidence.expected_content_hash is not None
            and source_evidence.expected_content_hash != chunk.content_hash
        ):
            return DereferenceSourceEvidenceResult(
                status="drifted",
                source_artifact_id=artifact.source_artifact_id,
                chunk_id=chunk.chunk_id,
                content_hash=chunk.content_hash,
                expected_artifact_hash=artifact.content_hash,
                reason="expected_content_hash does not match registered chunk",
                chunk=chunk,
            )

        if artifact.archive_content_hash is not None:
            archived = store.get_archive_content(artifact.archive_content_hash)
            if archived is not None:
                archived_hash = _sha256_bytes(archived)
                if archived_hash != artifact.content_hash:
                    return _unavailable_result(
                        artifact,
                        chunk,
                        "archived source content hash does not match manifest",
                    )
                return DereferenceSourceEvidenceResult(
                    status="available",
                    source_artifact_id=artifact.source_artifact_id,
                    chunk_id=chunk.chunk_id,
                    content_hash=chunk.content_hash,
                    expected_artifact_hash=artifact.content_hash,
                    current_artifact_hash=archived_hash,
                    body_origin="archive",
                    body=_chunk_body(archived, chunk),
                    chunk=chunk,
                )

        if artifact.local_path is None:
            return _unavailable_result(artifact, chunk, "source artifact has no local path")
        path = Path(artifact.local_path)
        if not path.is_file():
            return _unavailable_result(artifact, chunk, "local source path is unavailable")

        content = path.read_bytes()
        current_hash = _sha256_bytes(content)
        if current_hash != artifact.content_hash:
            return DereferenceSourceEvidenceResult(
                status="drifted",
                source_artifact_id=artifact.source_artifact_id,
                chunk_id=chunk.chunk_id,
                content_hash=chunk.content_hash,
                expected_artifact_hash=artifact.content_hash,
                current_artifact_hash=current_hash,
                reason="local source content hash does not match registered manifest",
                chunk=chunk,
            )
        return DereferenceSourceEvidenceResult(
            status="available",
            source_artifact_id=artifact.source_artifact_id,
            chunk_id=chunk.chunk_id,
            content_hash=chunk.content_hash,
            expected_artifact_hash=artifact.content_hash,
            current_artifact_hash=current_hash,
            body_origin="local_path",
            body=_chunk_body(content, chunk),
            chunk=chunk,
        )
    finally:
        store.close()


def resolve_source_evidence_refs(
    instance: InstanceProtocol,
    source_evidence: Sequence[SourceEvidenceInput | Mapping[str, Any]],
    *,
    actor_context: GovernedActorContext | None = None,
) -> list[EvidenceRef]:
    """Resolve source-evidence locators to existing compact evidence refs."""
    if not source_evidence:
        return []
    store = instance.get_source_artifact_store()
    try:
        refs: list[EvidenceRef] = []
        for item in source_evidence:
            locator = (
                item
                if isinstance(item, SourceEvidenceInput)
                else SourceEvidenceInput.model_validate(item)
            )
            artifact = store.get_artifact(locator.source_artifact_id)
            if artifact is None:
                raise ConfigError(f"Source artifact '{locator.source_artifact_id}' not found")
            chunk = _resolve_chunk(store, locator)
            if (
                locator.expected_content_hash is not None
                and locator.expected_content_hash != chunk.content_hash
            ):
                raise ConfigError(
                    "source_evidence expected_content_hash does not match registered chunk"
                )
            refs.append(
                EvidenceRef(
                    source="source_artifact",
                    source_record_id=chunk.chunk_id,
                    artifact_id=artifact.source_artifact_id,
                    label=locator.label or chunk.label or artifact.label,
                    metadata={
                        "chunk_id": chunk.chunk_id,
                        "content_hash": chunk.content_hash,
                        "artifact_content_hash": artifact.content_hash,
                        "source_kind": artifact.source_kind,
                        "parser_version": artifact.parser_version,
                        "heading_path": chunk.heading_path,
                        "block_selector": chunk.block_selector,
                        "block_type": chunk.block_type,
                        "line_start": chunk.line_start,
                        "line_end": chunk.line_end,
                        "source_retention": artifact.source_retention,
                        **(
                            {
                                "actor_context": dump_actor_context(actor_context),
                                "operation_id": actor_context.operation_id,
                            }
                            if actor_context is not None
                            else {}
                        ),
                    },
                )
            )
        return merge_evidence_ref_objects(refs)
    finally:
        store.close()


def _resolve_chunk(
    store: SourceArtifactStoreProtocol,
    locator: SourceEvidenceInput,
) -> SourceArtifactChunk:
    if locator.chunk_id is not None:
        chunk = store.get_chunk(locator.source_artifact_id, locator.chunk_id)
        if chunk is None:
            raise ConfigError(
                f"Source artifact chunk '{locator.chunk_id}' not found "
                f"for '{locator.source_artifact_id}'"
            )
        return chunk
    assert locator.heading_path is not None
    assert locator.block_selector is not None
    matches = store.find_chunks(
        locator.source_artifact_id,
        heading_path=locator.heading_path,
        block_selector=locator.block_selector,
    )
    if not matches:
        raise ConfigError(
            "Source evidence locator did not match any registered chunk: "
            f"{locator.source_artifact_id} {locator.heading_path} "
            f"{locator.block_selector}"
        )
    if len(matches) > 1:
        raise ConfigError("Source evidence locator matched multiple chunks; use chunk_id instead")
    return matches[0]


def _resolve_source_path(instance: InstanceProtocol, source_path: str) -> Path:
    raw_path = Path(source_path).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return (instance.get_root_path() / raw_path).resolve()


def _default_original_uri(
    instance: InstanceProtocol,
    path: Path,
    original_uri: str | None,
) -> str:
    if original_uri is not None:
        return original_uri
    try:
        return path.relative_to(instance.get_root_path()).as_posix()
    except ValueError:
        return path.name


def _chunk_body(content: bytes, chunk: SourceArtifactChunk) -> str:
    text = content.decode("utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    return "\n".join(lines[max(chunk.line_start - 1, 0) : max(chunk.line_end, 0)])


def _unavailable_result(
    artifact: SourceArtifactRecord,
    chunk: SourceArtifactChunk,
    reason: str,
) -> DereferenceSourceEvidenceResult:
    return DereferenceSourceEvidenceResult(
        status="unavailable",
        source_artifact_id=artifact.source_artifact_id,
        chunk_id=chunk.chunk_id,
        content_hash=chunk.content_hash,
        expected_artifact_hash=artifact.content_hash,
        reason=reason,
        chunk=chunk,
    )


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
