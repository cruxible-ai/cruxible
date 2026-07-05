"""Service functions for source artifact registration and dereference."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cruxible_core.errors import ConfigError, SourceArtifactNotFoundError
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
    DereferenceBodyOrigin,
    DereferenceSourceEvidenceResult,
    DereferenceStatus,
    RegisterSourceArtifactResult,
    SourceArtifactChunk,
    SourceArtifactListItem,
    SourceArtifactListResult,
    SourceArtifactReadChunk,
    SourceArtifactReadResult,
    SourceArtifactRecord,
    SourceEvidenceInput,
    SourceKind,
    SourceRetention,
)
from cruxible_core.temporal import format_datetime, utc_now

_SOURCE_ARTIFACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")


@dataclass(frozen=True)
class _SourceContentResolution:
    status: DereferenceStatus
    content: bytes | None = None
    body_origin: DereferenceBodyOrigin | None = None
    current_artifact_hash: str | None = None
    reason: str | None = None


def service_list_source_artifacts(
    instance: InstanceProtocol,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> SourceArtifactListResult:
    """List registered source artifacts with deterministic id ordering."""
    if offset < 0:
        raise ConfigError("offset must be >= 0")
    if limit is not None and limit < 0:
        raise ConfigError("limit must be >= 0")

    store = instance.get_source_artifact_store()
    try:
        artifacts = sorted(
            store.list_artifacts(),
            key=lambda artifact: artifact.source_artifact_id,
        )
        total = len(artifacts)
        end = None if limit is None else offset + limit
        page = artifacts[offset:end]
        items = [
            _artifact_list_item(
                artifact, chunk_count=len(store.list_chunks(artifact.source_artifact_id))
            )
            for artifact in page
        ]
        return SourceArtifactListResult(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            truncated=offset + len(items) < total,
        )
    finally:
        store.close()


def service_get_source_artifact(
    instance: InstanceProtocol,
    *,
    source_artifact_id: str,
) -> SourceArtifactReadResult:
    """Return artifact metadata and ordered chunks, with text when retained content resolves."""
    store = instance.get_source_artifact_store()
    try:
        artifact = store.get_artifact(source_artifact_id)
        if artifact is None:
            raise SourceArtifactNotFoundError(source_artifact_id)
        chunks = store.list_chunks(source_artifact_id)
        content = _resolve_artifact_content(store, artifact)
        content_available = content.status == "available" and content.content is not None
        return SourceArtifactReadResult(
            **_artifact_list_item(artifact, chunk_count=len(chunks)).model_dump(mode="python"),
            parser_version=artifact.parser_version,
            archived=artifact.archived,
            archive_content_hash=artifact.archive_content_hash,
            content_available=content_available,
            content_unavailable_reason=None if content_available else content.reason,
            body_origin=content.body_origin,
            current_artifact_hash=content.current_artifact_hash,
            chunks=[
                _read_chunk(chunk, content.content if content_available else None)
                for chunk in chunks
            ],
        )
    finally:
        store.close()


def service_register_source_artifact(
    instance: InstanceProtocol,
    *,
    source_path: str | None = None,
    source_content: str | bytes | None = None,
    source_kind: SourceKind = "markdown",
    source_retention: SourceRetention = "manifest_only",
    original_uri: str | None = None,
    label: str | None = None,
    parser_version: str = MARKDOWN_CHUNKS_V1,
    actor_context: GovernedActorContext | None = None,
    allowed_source_roots: Sequence[str | Path] | None = None,
    source_artifact_id: str | None = None,
    persist: bool = True,
) -> RegisterSourceArtifactResult:
    """Register a local or already-loaded source document as proposal evidence.

    When ``source_path`` is used, the resolved source path must stay within one
    of *allowed_source_roots* (defaulting to the instance root) and any roots
    configured via ``CRUXIBLE_ALLOWED_ROOTS``. Containment is default-deny: an
    absolute ``source_path`` that escapes the allowed roots is rejected even
    when ``CRUXIBLE_ALLOWED_ROOTS`` is unset.

    ``source_content`` is for callers that already hold source bytes in trusted
    workflow/service memory; it never resolves a path or reads local files.
    """
    if source_kind != "markdown":
        raise ConfigError(f"Unsupported source_kind '{source_kind}'")
    if source_retention not in ("manifest_only", "archive"):
        raise ConfigError(f"Unsupported source_retention '{source_retention}'")
    if parser_version != MARKDOWN_CHUNKS_V1:
        raise ConfigError(f"Unsupported parser_version '{parser_version}'")

    path: Path | None = None
    if (source_path is None) == (source_content is None):
        raise ConfigError("Exactly one of source_path or source_content is required")
    if source_content is None:
        assert source_path is not None
        path = _resolve_source_path(
            instance,
            source_path,
            allowed_source_roots=allowed_source_roots,
        )
        if not path.is_file():
            raise ConfigError(f"Source artifact path is not a file: {source_path}")
        content = path.read_bytes()
    elif isinstance(source_content, str):
        content = source_content.encode("utf-8")
    else:
        content = source_content
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("Only UTF-8 Markdown source artifacts are supported") from exc
    content_hash = _sha256_bytes(content)
    if source_artifact_id is not None:
        # Caller-supplied ids let digest-pinned seed evidence reference
        # artifacts deterministically (e.g. opinion_text_op_loper_bright).
        if not _SOURCE_ARTIFACT_ID_RE.fullmatch(source_artifact_id):
            raise ConfigError(
                "source_artifact_id must be 3-64 chars of [A-Za-z0-9._-] "
                "starting with an alphanumeric"
            )
        store = instance.get_source_artifact_store()
        try:
            existing_artifact = store.get_artifact(source_artifact_id)
        finally:
            store.close()
        if existing_artifact is not None:
            raise ConfigError(f"Source artifact '{source_artifact_id}' is already registered")
    else:
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
        original_uri=(
            _default_original_uri(instance, path, original_uri)
            if path is not None
            else original_uri
        ),
        label=label,
        parser_version=parser_version,
        content_hash=content_hash,
        byte_count=len(content),
        local_path=str(path) if path is not None else None,
        archived=archived,
        archive_content_hash=content_hash if archived else None,
        created_at=created_at,
        registered_actor_context=actor_context,
    )
    if persist:
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

        content = _resolve_artifact_content(store, artifact)
        if content.status != "available" or content.content is None:
            return DereferenceSourceEvidenceResult(
                status=content.status,
                source_artifact_id=artifact.source_artifact_id,
                chunk_id=chunk.chunk_id,
                content_hash=chunk.content_hash,
                expected_artifact_hash=artifact.content_hash,
                current_artifact_hash=content.current_artifact_hash,
                reason=content.reason,
                chunk=chunk,
            )
        return DereferenceSourceEvidenceResult(
            status="available",
            source_artifact_id=artifact.source_artifact_id,
            chunk_id=chunk.chunk_id,
            content_hash=chunk.content_hash,
            expected_artifact_hash=artifact.content_hash,
            current_artifact_hash=content.current_artifact_hash,
            body_origin=content.body_origin,
            body=_chunk_body(content.content, chunk),
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


def _artifact_list_item(
    artifact: SourceArtifactRecord,
    *,
    chunk_count: int,
) -> SourceArtifactListItem:
    return SourceArtifactListItem(
        source_artifact_id=artifact.source_artifact_id,
        kind=artifact.source_kind,
        retention=artifact.source_retention,
        original_uri=artifact.original_uri,
        label=artifact.label,
        content_hash=artifact.content_hash,
        registered_at=artifact.created_at,
        chunk_count=chunk_count,
        byte_count=artifact.byte_count,
    )


def _read_chunk(
    chunk: SourceArtifactChunk,
    content: bytes | None,
) -> SourceArtifactReadChunk:
    return SourceArtifactReadChunk(
        chunk_id=chunk.chunk_id,
        heading_path=chunk.heading_path,
        block_selector=chunk.block_selector,
        block_type=chunk.block_type,
        line_start=chunk.line_start,
        line_end=chunk.line_end,
        content_hash=chunk.content_hash,
        text=_chunk_body(content, chunk) if content is not None else None,
    )


def _resolve_artifact_content(
    store: SourceArtifactStoreProtocol,
    artifact: SourceArtifactRecord,
) -> _SourceContentResolution:
    if artifact.archive_content_hash is not None:
        archived = store.get_archive_content(artifact.archive_content_hash)
        if archived is not None:
            archived_hash = _sha256_bytes(archived)
            if archived_hash != artifact.content_hash:
                return _SourceContentResolution(
                    status="unavailable",
                    reason="archived source content hash does not match manifest",
                )
            return _SourceContentResolution(
                status="available",
                content=archived,
                body_origin="archive",
                current_artifact_hash=archived_hash,
            )

    if artifact.local_path is None:
        return _SourceContentResolution(
            status="unavailable",
            reason="source artifact has no local path",
        )
    path = Path(artifact.local_path)
    if not path.is_file():
        return _SourceContentResolution(
            status="unavailable",
            reason="local source path is unavailable",
        )

    content = path.read_bytes()
    current_hash = _sha256_bytes(content)
    if current_hash != artifact.content_hash:
        return _SourceContentResolution(
            status="drifted",
            current_artifact_hash=current_hash,
            reason="local source content hash does not match registered manifest",
        )
    return _SourceContentResolution(
        status="available",
        content=content,
        body_origin="local_path",
        current_artifact_hash=current_hash,
    )


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


def _resolve_source_path(
    instance: InstanceProtocol,
    source_path: str,
    *,
    allowed_source_roots: Sequence[str | Path] | None = None,
) -> Path:
    """Resolve *source_path* and enforce default-deny workspace containment.

    Both relative and absolute source paths must resolve (after expanding the
    user home directory and following symlinks) to a location under one of the
    allowed roots. The allowed roots are *allowed_source_roots* (defaulting to
    the instance root) plus any roots configured via ``CRUXIBLE_ALLOWED_ROOTS``.
    """
    if allowed_source_roots is None:
        allowed_source_roots = [instance.get_root_path()]
    return resolve_contained_source_path(source_path, allowed_source_roots=allowed_source_roots)


def resolve_contained_source_path(
    source_path: str,
    *,
    allowed_source_roots: Sequence[str | Path],
) -> Path:
    """Resolve *source_path* under default-deny containment.

    *source_path* may be absolute or relative. Relative paths are resolved
    against the first allowed root. The result is resolved (user home expanded,
    symlinks followed, ``..`` collapsed) and then required to equal or be nested
    under one of the allowed roots — *allowed_source_roots* plus any
    ``CRUXIBLE_ALLOWED_ROOTS``. Raises :class:`ConfigError` on escape.
    """
    allowed = _allowed_source_roots(allowed_source_roots)
    base = allowed[0]

    raw_path = Path(source_path).expanduser()
    candidate = raw_path if raw_path.is_absolute() else (base / raw_path)
    resolved = candidate.resolve()

    if not _is_within_allowed_roots(resolved, allowed):
        raise ConfigError("source_path must stay within the registered workspace")
    return resolved


def _allowed_source_roots(roots: Sequence[str | Path]) -> list[Path]:
    """Resolve the configured allowed roots, augmenting with allowed-roots env."""
    resolved: list[Path] = [Path(root).expanduser().resolve() for root in roots]
    for env_root in _env_allowed_roots():
        if env_root not in resolved:
            resolved.append(env_root)
    if not resolved:
        # Defensive: never fall back to allowing the entire filesystem.
        raise ConfigError("No allowed source roots configured for path containment")
    return resolved


def _is_within_allowed_roots(resolved: Path, allowed: Sequence[Path]) -> bool:
    """Return whether *resolved* equals or is nested under an allowed root.

    Comparison is performed on already-resolved paths so that ``..`` traversal,
    symlink escapes, and prefix-matching siblings (``/srv/data-evil`` vs
    ``/srv/data``) cannot bypass containment.
    """
    return any(root == resolved or root in resolved.parents for root in allowed)


def _env_allowed_roots() -> list[Path]:
    """Parse ``CRUXIBLE_ALLOWED_ROOTS`` into resolved absolute roots."""
    raw = os.environ.get("CRUXIBLE_ALLOWED_ROOTS")
    if raw is None:
        return []
    roots: list[Path] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        path = Path(entry)
        if not path.is_absolute():
            raise ConfigError(f"CRUXIBLE_ALLOWED_ROOTS contains relative path: '{entry}'")
        roots.append(path.resolve())
    return roots


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
