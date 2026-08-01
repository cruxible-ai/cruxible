"""Service functions for source artifact registration and dereference."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cruxible_core.errors import (
    CitationHandleResolutionError,
    ConfigError,
    SourceArtifactNotFoundError,
)
from cruxible_core.governance.actors import (
    GovernedActorContext,
    dump_actor_context,
)
from cruxible_core.graph.evidence import EvidenceRef, merge_evidence_ref_objects
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.primitives import new_id
from cruxible_core.service.mutation_receipts import mutation_receipt
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
_REVISION_HANDLE_PREFIX = "src1_"
_CHUNK_HANDLE_PREFIX = "cite1_"
_HANDLE_DIGEST_LENGTH = 20


@dataclass(frozen=True)
class _SourceContentResolution:
    status: DereferenceStatus
    content: bytes | None = None
    body_origin: DereferenceBodyOrigin | None = None
    current_artifact_hash: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _CitationHandleTarget:
    artifact: SourceArtifactRecord
    chunk: SourceArtifactChunk | None = None


def source_artifact_revision_handle(artifact: SourceArtifactRecord) -> str:
    """Return the stable, revision-pinned handle for a registered artifact."""
    return _digest_handle(
        _REVISION_HANDLE_PREFIX,
        artifact.artifact_revision_id,
        artifact.content_hash,
    )


def source_artifact_chunk_handle(
    artifact: SourceArtifactRecord,
    chunk: SourceArtifactChunk,
) -> str:
    """Return the stable handle for one chunk of one registered revision."""
    return _digest_handle(
        _CHUNK_HANDLE_PREFIX,
        artifact.artifact_revision_id,
        artifact.content_hash,
        chunk.chunk_id,
        chunk.content_hash,
    )


def _digest_handle(prefix: str, *parts: str) -> str:
    seed = "\x00".join(("cruxible-source-citation-handle-v1", *parts))
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:_HANDLE_DIGEST_LENGTH]
    return f"{prefix}{digest}"


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
        # Re-read after resolution so a drift observation persisted by this read
        # is reported by this read, not only by the next one.
        observed = store.get_artifact_revision(artifact.artifact_revision_id) or artifact
        return SourceArtifactReadResult(
            **_artifact_list_item(artifact, chunk_count=len(chunks)).model_dump(mode="python"),
            parser_version=artifact.parser_version,
            archived=artifact.archived,
            archive_content_hash=artifact.archive_content_hash,
            content_available=content_available,
            content_unavailable_reason=None if content_available else content.reason,
            body_origin=content.body_origin,
            current_artifact_hash=content.current_artifact_hash,
            drift_observed_hash=observed.drift_observed_hash,
            drift_observed_at=observed.drift_observed_at,
            first_drift_observed_hash=observed.first_drift_observed_hash,
            first_drift_observed_at=observed.first_drift_observed_at,
            chunks=[
                _read_chunk(
                    chunk,
                    content.content if content_available else None,
                    artifact=artifact,
                )
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

    Registration is insert-only. Re-registering an existing id with identical
    content is a no-op that returns the stored revision unchanged; with changed
    content it writes a NEW revision and marks the previous one superseded, so
    the manifest that existing evidence refs pinned their content hash against
    is never rewritten underneath them.
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
    resolved_original_uri = (
        _default_original_uri(instance, path, original_uri) if path is not None else original_uri
    )

    def _build_record(revision: int) -> SourceArtifactRecord:
        return SourceArtifactRecord(
            source_artifact_id=source_artifact_id,
            revision=revision,
            source_kind=source_kind,
            source_retention=source_retention,
            original_uri=resolved_original_uri,
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

    if not persist:
        # Dry run: report the revision this registration would take without
        # opening a write boundary, so a preview never mints a receipt.
        preview_store = instance.get_source_artifact_store()
        try:
            head = preview_store.get_artifact(source_artifact_id)
        finally:
            preview_store.close()
        if head is not None and head.content_hash == content_hash:
            return _registration_result(head, chunks, already_registered=True)
        return _registration_result(
            _build_record(1 if head is None else head.revision + 1),
            chunks,
            supersedes=head.artifact_revision_id if head is not None else None,
        )

    with mutation_receipt(
        instance,
        "source_artifact_register",
        {
            "source_artifact_id": source_artifact_id,
            "source_kind": source_kind,
            "source_retention": source_retention,
            "content_hash": content_hash,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        store = ctx.uow.source_artifacts
        # The duplicate check runs on the unit-of-work connection that also
        # performs the insert. Checking on a separate connection first left a
        # window in which two registrations of the same id both saw "absent"
        # and both wrote.
        head = store.get_artifact(source_artifact_id)
        if head is not None and head.content_hash == content_hash:
            ctx.builder.record_validation(
                True,
                detail={
                    "source_artifact_id": source_artifact_id,
                    "artifact_revision_id": head.artifact_revision_id,
                    "revision": head.revision,
                    "content_hash": content_hash,
                    "outcome": "already_registered",
                    "reason": "content hash matches the current revision; nothing written",
                },
            )
            ctx.set_result(_registration_result(head, chunks, already_registered=True))
        else:
            record = _build_record(1 if head is None else head.revision + 1)
            store.save_artifact(
                record,
                chunks,
                archive_content=content if archived else None,
            )
            ctx.builder.record_validation(
                True,
                detail={
                    "source_artifact_id": source_artifact_id,
                    "artifact_revision_id": record.artifact_revision_id,
                    "revision": record.revision,
                    "content_hash": content_hash,
                    "byte_count": len(content),
                    "chunk_count": len(chunks),
                    "archived": archived,
                    "outcome": "registered",
                    "supersedes": head.artifact_revision_id if head is not None else None,
                    "superseded_content_hash": head.content_hash if head is not None else None,
                },
            )
            ctx.set_result(
                _registration_result(
                    record,
                    chunks,
                    supersedes=head.artifact_revision_id if head is not None else None,
                )
            )

    result = ctx.result
    assert isinstance(result, RegisterSourceArtifactResult)
    return result


def service_dereference_source_evidence(
    instance: InstanceProtocol,
    *,
    source_artifact_id: str,
    artifact_revision_id: str | None = None,
    chunk_id: str | None = None,
    heading_path: list[str] | None = None,
    block_selector: str | None = None,
    expected_content_hash: str | None = None,
) -> DereferenceSourceEvidenceResult:
    """Resolve a source-evidence locator and return drift-aware source text.

    ``artifact_revision_id`` PINS the read to one immutable revision. Without it
    the read resolves against whatever revision is current, which meant a
    citation made against revision 1 could not retrieve the content it was made
    against once revision 2 existed — even though revision 1's chunks, manifest,
    and archived bytes were all still stored. Unpinned reads still work (old refs
    carry no revision) but say so via ``revision_unpinned``: falling back is
    acceptable, doing it silently is not.
    """
    source_evidence = SourceEvidenceInput(
        source_artifact_id=source_artifact_id,
        artifact_revision_id=artifact_revision_id,
        chunk_id=chunk_id,
        heading_path=heading_path,
        block_selector=block_selector,
        expected_content_hash=expected_content_hash,
    )
    store = instance.get_source_artifact_store()
    try:
        artifact = _resolve_artifact_revision(store, source_evidence)
        unpinned = source_evidence.artifact_revision_id is None
        chunk = _resolve_chunk(store, source_evidence, artifact=artifact)
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
                artifact_revision_id=artifact.artifact_revision_id,
                revision_unpinned=unpinned,
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
                artifact_revision_id=artifact.artifact_revision_id,
                revision_unpinned=unpinned,
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
            artifact_revision_id=artifact.artifact_revision_id,
            revision_unpinned=unpinned,
        )
    finally:
        store.close()


def _resolve_artifact_revision(
    store: SourceArtifactStoreProtocol,
    locator: SourceEvidenceInput,
) -> SourceArtifactRecord:
    """Return the pinned revision, or the current one when nothing is pinned."""
    if locator.artifact_revision_id is None:
        artifact = store.get_artifact(locator.source_artifact_id)
        if artifact is None:
            raise ConfigError(f"Source artifact '{locator.source_artifact_id}' not found")
        return artifact
    revision = store.get_artifact_revision(locator.artifact_revision_id)
    if revision is None:
        raise ConfigError(f"Source artifact revision '{locator.artifact_revision_id}' not found")
    if revision.source_artifact_id != locator.source_artifact_id:
        # A pin naming a different logical artifact is a corrupt citation, not a
        # stale one: silently honoring either half would return content from a
        # document the citation never referred to.
        raise ConfigError(
            f"Source artifact revision '{locator.artifact_revision_id}' does not belong "
            f"to '{locator.source_artifact_id}'"
        )
    return revision


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
        return _resolve_source_evidence_refs_with_store(
            store,
            source_evidence,
            actor_context=actor_context,
        )
    finally:
        store.close()


def resolve_citation_handle_refs(
    instance: InstanceProtocol,
    citation_handles: Sequence[str],
    *,
    actor_context: GovernedActorContext | None = None,
) -> list[EvidenceRef]:
    """Resolve server-minted handles through the canonical source-evidence path.

    A ``cite1_`` handle identifies exactly one chunk. A ``src1_`` revision
    handle identifies the whole immutable revision and lowers to one canonical
    source-evidence locator per registered chunk. There is deliberately no
    floating logical-artifact handle: every accepted token is revision-pinned.
    """
    if not citation_handles:
        return []
    store = instance.get_source_artifact_store()
    try:
        locators: list[SourceEvidenceInput] = []
        targets = _resolve_citation_handle_targets(store, citation_handles)
        for handle in citation_handles:
            target = targets[handle]
            chunks = (
                [target.chunk]
                if target.chunk is not None
                else store.list_revision_chunks(target.artifact.artifact_revision_id)
            )
            for chunk in chunks:
                assert chunk is not None
                locators.append(
                    SourceEvidenceInput(
                        source_artifact_id=target.artifact.source_artifact_id,
                        artifact_revision_id=target.artifact.artifact_revision_id,
                        chunk_id=chunk.chunk_id,
                    )
                )
        return _resolve_source_evidence_refs_with_store(
            store,
            locators,
            actor_context=actor_context,
        )
    finally:
        store.close()


def _resolve_citation_handle_targets(
    store: SourceArtifactStoreProtocol,
    handles: Sequence[str],
) -> dict[str, _CitationHandleTarget]:
    requested = dict.fromkeys(handles)
    matches: dict[str, list[_CitationHandleTarget]] = {
        handle: [] for handle in requested
    }
    for head in store.list_artifacts():
        for artifact in store.list_artifact_revisions(head.source_artifact_id):
            revision_handle = source_artifact_revision_handle(artifact)
            if revision_handle in requested:
                matches[revision_handle].append(_CitationHandleTarget(artifact=artifact))
            for chunk in store.list_revision_chunks(artifact.artifact_revision_id):
                chunk_handle = source_artifact_chunk_handle(artifact, chunk)
                if chunk_handle in requested:
                    matches[chunk_handle].append(
                        _CitationHandleTarget(artifact=artifact, chunk=chunk)
                    )

    resolved: dict[str, _CitationHandleTarget] = {}
    for handle in requested:
        handle_matches = matches[handle]
        if not handle_matches:
            raise CitationHandleResolutionError(
                handle,
                "unknown",
                detail=(
                    "the token matches no registered source-artifact revision or chunk; "
                    "use a handle returned by source-artifact register, list, or get"
                ),
            )
        if len(handle_matches) > 1:
            raise CitationHandleResolutionError(
                handle,
                "ambiguous",
                detail=(
                    f"the token matches {len(handle_matches)} registered targets; Cruxible "
                    "refuses to guess, so cite with an explicit revision-pinned "
                    "source_evidence locator"
                ),
            )

        target = handle_matches[0]
        current = store.get_artifact(target.artifact.source_artifact_id)
        if current is None or current.artifact_revision_id != target.artifact.artifact_revision_id:
            current_revision = current.artifact_revision_id if current is not None else "none"
            raise CitationHandleResolutionError(
                handle,
                "stale",
                detail=(
                    f"the token targets superseded revision "
                    f"'{target.artifact.artifact_revision_id}' (current revision: "
                    f"'{current_revision}'); fetch the current source-artifact handles"
                ),
            )
        resolved[handle] = target
    return resolved


def _resolve_source_evidence_refs_with_store(
    store: SourceArtifactStoreProtocol,
    source_evidence: Sequence[SourceEvidenceInput | Mapping[str, Any]],
    *,
    actor_context: GovernedActorContext | None,
) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    for item in source_evidence:
        locator = (
            item
            if isinstance(item, SourceEvidenceInput)
            else SourceEvidenceInput.model_validate(item)
        )
        artifact = _resolve_artifact_revision(store, locator)
        chunk = _resolve_chunk(store, locator, artifact=artifact)
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
                artifact_revision_id=artifact.artifact_revision_id,
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


def _registration_result(
    record: SourceArtifactRecord,
    chunks: list[SourceArtifactChunk],
    *,
    supersedes: str | None = None,
    already_registered: bool = False,
) -> RegisterSourceArtifactResult:
    return RegisterSourceArtifactResult(
        source_artifact_id=record.source_artifact_id,
        artifact_revision_id=record.artifact_revision_id,
        revision_handle=source_artifact_revision_handle(record),
        revision=record.revision,
        source_kind=record.source_kind,
        source_retention=record.source_retention,
        original_uri=record.original_uri,
        label=record.label,
        content_hash=record.content_hash,
        byte_count=record.byte_count,
        parser_version=record.parser_version,
        archived=record.archived,
        archive_content_hash=record.archive_content_hash,
        chunks=[
            chunk.model_copy(
                update={"citation_handle": source_artifact_chunk_handle(record, chunk)}
            )
            for chunk in chunks
        ],
        supersedes=supersedes,
        already_registered=already_registered,
    )


def _artifact_list_item(
    artifact: SourceArtifactRecord,
    *,
    chunk_count: int,
) -> SourceArtifactListItem:
    return SourceArtifactListItem(
        source_artifact_id=artifact.source_artifact_id,
        artifact_revision_id=artifact.artifact_revision_id,
        revision=artifact.revision,
        revision_handle=source_artifact_revision_handle(artifact),
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
    *,
    artifact: SourceArtifactRecord,
) -> SourceArtifactReadChunk:
    return SourceArtifactReadChunk(
        chunk_id=chunk.chunk_id,
        heading_path=chunk.heading_path,
        block_selector=chunk.block_selector,
        block_type=chunk.block_type,
        line_start=chunk.line_start,
        line_end=chunk.line_end,
        content_hash=chunk.content_hash,
        citation_handle=source_artifact_chunk_handle(artifact, chunk),
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

    if artifact.superseded_by is not None:
        # A SUPERSEDED revision's bytes only survive under archive retention;
        # the archive branch above is the only honest way to serve them. Under
        # the default manifest_only retention the local path now holds the
        # CURRENT revision's bytes, so hashing it compares this revision's
        # manifest against a different revision's content. The mismatch is
        # guaranteed and means nothing -- yet the fallthrough below reported it
        # as "drifted" and called ``record_content_drift``, permanently
        # stamping the sticky first_drift_observed_hash/_at tamper finding on a
        # revision nobody ever touched. Replaying a pinned citation is a read;
        # it must never manufacture a tamper record.
        return _SourceContentResolution(
            status="revision_bytes_not_retained",
            reason=(
                "superseded revision bytes are not retained (no archived copy); "
                "the local path holds a newer revision"
            ),
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
        # Persist the finding. Detecting drift and returning it only to the
        # immediate caller meant the instance rediscovered — and forgot — that
        # its evidence base had drifted on every single read. The store write
        # is idempotent, so a repeatedly-read drifted artifact costs one write
        # for the first observation and none afterwards; the read path stays a
        # read once the observation is on record.
        store.record_content_drift(
            artifact.artifact_revision_id,
            observed_hash=current_hash,
            observed_at=format_datetime(utc_now()),
        )
        return _SourceContentResolution(
            status="drifted",
            current_artifact_hash=current_hash,
            reason="local source content hash does not match registered manifest",
        )
    # Symmetric clear: leaving a stale marker on a restored file would misreport
    # the current state of the evidence base. Also idempotent.
    store.record_content_drift(
        artifact.artifact_revision_id,
        observed_hash=None,
        observed_at=None,
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
    *,
    artifact: SourceArtifactRecord | None = None,
) -> SourceArtifactChunk:
    """Resolve the locator's chunk, scoped to ``artifact``'s revision when pinned.

    ``get_chunk`` / ``find_chunks`` join on ``superseded_by IS NULL``, i.e. they
    only ever see the CURRENT revision — so a pinned read has to go through the
    revision-scoped chunk listing instead, or the pin would select the right
    manifest and the wrong chunks.
    """
    if artifact is not None and locator.artifact_revision_id is not None:
        return _resolve_revision_chunk(store, locator, artifact)
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


def _resolve_revision_chunk(
    store: SourceArtifactStoreProtocol,
    locator: SourceEvidenceInput,
    artifact: SourceArtifactRecord,
) -> SourceArtifactChunk:
    chunks = store.list_revision_chunks(artifact.artifact_revision_id)
    if locator.chunk_id is not None:
        for chunk in chunks:
            if chunk.chunk_id == locator.chunk_id:
                return chunk
        raise ConfigError(
            f"Source artifact chunk '{locator.chunk_id}' not found "
            f"in revision '{artifact.artifact_revision_id}'"
        )
    assert locator.heading_path is not None
    assert locator.block_selector is not None
    matches = [
        chunk
        for chunk in chunks
        if chunk.heading_path == locator.heading_path
        and chunk.block_selector == locator.block_selector
    ]
    if not matches:
        raise ConfigError(
            "Source evidence locator did not match any chunk in revision "
            f"'{artifact.artifact_revision_id}': {locator.heading_path} "
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


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
