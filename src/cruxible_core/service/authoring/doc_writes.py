"""Admit workspace edits and retain overwritten subtrees; never open workspace paths."""

import base64
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.declared_blocks import (
    declares_projection_block,
    stamped_projection_windows,
)
from cruxible_client.contracts.doc_sections import SectionPath, section_digest
from cruxible_client.contracts.errors import ReadRefusalError, WriteRefusalError
from cruxible_client.contracts.get_reads import (
    GET_BODY_DEFAULT_MAX_BYTES,
    GET_HISTORY_DEFAULT_LIMIT,
    ByteRange,
    GetBody,
    GetHistory,
    GetRequest,
    GetResult,
    GetRevision,
)
from cruxible_client.contracts.operational_reads import LiveHead, LiveView
from cruxible_client.contracts.proposal_models import AuthenticatedActor
from cruxible_client.contracts.read_values import ShownObject
from cruxible_client.contracts.temporal import format_datetime
from cruxible_client.contracts.write import (
    DocConfirmationOutcome,
    DocEditObservation,
    DocOp,
    WriteOutcome,
    WriteRefusal,
    WriteRequest,
)
from cruxible_core.authoring.registrations import source_projection_blocks
from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.exhaust.records import (
    DOC_EDIT_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    parse_journal_payload,
    payload_digest,
)
from cruxible_core.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import compact_coordinate
from cruxible_core.service.list_pages import (
    ListCursorMismatch,
    decode_list_cursor,
    encode_list_cursor,
)
from cruxible_core.service.read_refusals import resolve_read_coordinate
from cruxible_core.storage.cas import BodyAccessContext

_FENCE = "doc-edit-local-v1"
_ACCESS = BodyAccessContext(principal_id="doc-history", can_read_body=True)
_CONFIRMATION_WINDOW_SECONDS = 24 * 60 * 60
_EXPIRED_CLEANUP_LIMIT = 500


class _EditRecord(BaseModel):
    """A confirmed overwrite whose workspace file CAS succeeded.

    The prior body is one CAS blob, never a copy of the page or workspace.
    This record attributes the replacing write, not authorship of the prior prose.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    before_section: SectionPath
    section: SectionPath
    before_digest: str
    after_digest: str
    status: str
    proposal: str | None
    because: str
    write_id: str | None = None


class _PendingEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    edit: _EditRecord
    actor: GovernedActorContext
    coordinate: AcceptedCoordinate
    # None is the compact confirmation receipt; unconfirmed bytes never land in CAS.
    before_content_base64: str | None


def _pending_root(instance: PlaybillInstance) -> Path:
    return instance.root / instance.descriptor.storage.exhaust / "doc-edits" / "pending"


def _store_pending(connection: sqlite3.Connection, token: str, pending: _PendingEdit) -> None:
    unconfirmed = pending.before_content_base64 is not None
    expires = (
        pending.actor.timestamp.timestamp() + _CONFIRMATION_WINDOW_SECONDS if unconfirmed else None
    )
    connection.execute(
        "INSERT INTO pending_edits(token,source_id,unconfirmed,payload,expires_at) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET "
        "unconfirmed=excluded.unconfirmed,payload=excluded.payload,expires_at=excluded.expires_at",
        (token, pending.edit.source_id, int(unconfirmed), pending.model_dump_json(), expires),
    )


@contextmanager
def _pending_store(root: Path) -> Iterator[sqlite3.Connection]:
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root.parent / "pending.sqlite3"
    connection = sqlite3.connect(path, timeout=30)
    path.chmod(0o600)
    try:
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS pending_edits ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE, "
            "source_id TEXT NOT NULL, unconfirmed INTEGER NOT NULL, payload TEXT NOT NULL, "
            "expires_at REAL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS pending_by_source "
            "ON pending_edits(source_id,unconfirmed,sequence DESC)"
        )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            # One cold migration of the old count-bounded store. Reconstruct
            # deadlines from issuance, never from this migration's wall clock.
            connection.execute("ALTER TABLE pending_edits ADD COLUMN expires_at REAL")
            for sequence, issued_at in connection.execute(
                "SELECT sequence,json_extract(payload,'$.actor.timestamp') "
                "FROM pending_edits WHERE unconfirmed=1"
            ):
                connection.execute(
                    "UPDATE pending_edits SET expires_at=? WHERE sequence=?",
                    (
                        datetime.fromisoformat(issued_at).timestamp()
                        + _CONFIRMATION_WINDOW_SECONDS,
                        sequence,
                    ),
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS pending_expiry "
            "ON pending_edits(source_id,unconfirmed,expires_at)"
        )
        if version == 0:
            # One cold format migration: visit legacy receipts once. Normal
            # writes never scan the pending namespace.
            for legacy in sorted(root.glob("DCE-*.json"), key=lambda p: p.stat().st_mtime_ns):
                _store_pending(
                    connection, legacy.stem, _PendingEdit.model_validate_json(legacy.read_bytes())
                )
            connection.execute("PRAGMA user_version=2")
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
        if version == 1:
            connection.execute("PRAGMA user_version=2")
        if root.exists():
            # Only after the import commits; retry cleanup after an interrupted migration.
            for legacy in root.glob("DCE-*.json"):
                legacy.unlink()
            root.rmdir()
        yield connection
        connection.commit()
    finally:
        connection.close()


def _save_pending(path: Path, pending: _PendingEdit) -> None:
    with _pending_store(path.parent) as connection:
        _store_pending(connection, path.stem, pending)


def _load_pending(path: Path) -> _PendingEdit:
    with _pending_store(path.parent) as connection:
        row = connection.execute(
            "SELECT payload FROM pending_edits WHERE token=?", (path.stem,)
        ).fetchone()
    if row is None:
        raise FileNotFoundError(path)
    return _PendingEdit.model_validate_json(row[0])


def _journal(
    instance: PlaybillInstance, *, create: bool = False
) -> tuple[LocalJournalBackend | None, JournalStreamIdentityV1]:
    root = instance.root / instance.descriptor.storage.exhaust / "doc-edits"
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=DOC_EDIT_JOURNAL_FAMILY,
        stream_id="doc-edits",
    )
    return (LocalJournalBackend(root) if root.exists() else None), stream


def prepare_doc_edits(
    instance: PlaybillInstance,
    *,
    request: WriteRequest,
    outcome: WriteOutcome,
    actor: AuthenticatedActor,
) -> dict[int, str]:
    """Keep pending edits outside history and CAS until the writer confirms its CAS.

    Each immutable token has a 24-hour confirmation window, independent of other
    writers or request size. An indexed, bounded cleanup visits only expired
    payloads of sources in this request. Confirmed receipts stay compact.
    """
    confirmations: dict[int, str] = {}
    edits = [
        (index, edit)
        for index, edit in enumerate(request.docs)
        if isinstance(edit, DocEditObservation)
        and edit.op == "replace"
        and edit.before_digest != edit.after_digest
    ]
    if not edits:
        return confirmations
    now = instance.recording_clock()
    sources: set[str] = set()
    with _pending_store(_pending_root(instance)) as connection:
        for index, edit in edits:
            assert edit.before_content_base64 is not None and edit.before_section is not None
            assert edit.before_digest is not None
            token = "DCE-" + uuid4().hex
            pending = _PendingEdit(
                edit=_EditRecord(
                    source_id=edit.source_id,
                    before_section=edit.before_section,
                    section=edit.section,
                    before_digest=edit.before_digest,
                    after_digest=edit.after_digest,
                    status=outcome.status,
                    proposal=None if outcome.proposal is None else outcome.proposal.proposal_id,
                    because=request.because,
                    write_id=token,
                ),
                coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
                actor=GovernedActorContext(
                    actor_type="service_account",
                    actor_id=actor.actor_id,
                    org_id=instance.descriptor.instance_id,
                    operation_id=token,
                    timestamp=now,
                ),
                before_content_base64=edit.before_content_base64,
            )
            _store_pending(connection, token, pending)
            sources.add(edit.source_id)
            confirmations[index] = token
        # Work is capped per changed source, never proportional to all pending
        # edits. Nothing issued by this request can already be expired.
        for source in sources:
            connection.execute(
                "DELETE FROM pending_edits WHERE token IN ("
                "SELECT token FROM pending_edits WHERE source_id=? AND unconfirmed=1 "
                "AND expires_at<=? ORDER BY expires_at LIMIT ?)",
                (source, now.timestamp(), _EXPIRED_CLEANUP_LIMIT),
            )
    return confirmations


def confirm_doc_edits(
    instance: PlaybillInstance,
    *,
    tokens: tuple[str, ...],
    actor: AuthenticatedActor,
) -> WriteOutcome:
    """Publish only successful file edits; retries are indexed and idempotent."""
    from cruxible_core.service.authoring.write_verbs import _refusal

    results = []
    for token in tokens:
        try:
            _confirm_doc_edit(instance, token=token, actor=actor)
        except WriteRefusalError as error:
            results.append(DocConfirmationOutcome(confirmation=token, refusal=_refusal(error)))
        else:
            results.append(DocConfirmationOutcome(confirmation=token))
    failed = any(result.refusal is not None for result in results)
    return WriteOutcome(
        status="refused" if failed else "accepted",
        coordinate=compact_coordinate(instance, instance.accepted_coordinate()),
        doc_confirmations=tuple(results),
        refusal=(
            WriteRefusal(
                code="cruxible.doc.confirmation_incomplete",
                message="Some edits could not be confirmed; other edits were recorded",
                repair=(
                    "Inspect each doc_confirmations result; do not resend unknown or expired tokens"
                ),
            )
            if failed
            else None
        ),
    )


def _confirm_doc_edit(instance: PlaybillInstance, *, token: str, actor: AuthenticatedActor) -> None:
    from cruxible_core.service.authoring.write_verbs import _refuse

    if re.fullmatch(r"DCE-[0-9a-f]{32}", token) is None:
        raise _refuse("cruxible.doc.confirmation_invalid", "Invalid document confirmation")
    path = _pending_root(instance) / f"{token}.json"
    try:
        pending = _load_pending(path)
    except FileNotFoundError as exc:
        raise _refuse(
            "cruxible.doc.confirmation_unknown", "No admitted edit has this confirmation"
        ) from exc
    if pending.actor.actor_id != actor.actor_id:
        raise _refuse(
            "cruxible.doc.confirmation_author", "Only the admitted edit's author can confirm it"
        )
    if pending.before_content_base64 is None:
        return
    previous = base64.b64decode(pending.before_content_base64, validate=True)
    if section_digest(previous) != pending.edit.before_digest:
        raise _refuse("cruxible.doc.previous_bytes_mismatch", "Previous subtree digest differs")
    bodies = instance.body_store()
    journal, stream = _journal(instance, create=True)
    assert journal is not None
    partition = section_digest(pending.edit.source_id.encode()).removeprefix("sha256:")
    payload = pending.edit.model_dump(mode="json")
    # Recover a crash after journal append but before compacting the pending
    # bytes. This probe is one indexed key, never the source's whole history.
    if journal.select_records(
        stream,
        partition_id=partition,
        event_kind="doc_edit",
        payload_digest=payload_digest(payload),
        limit=1,
    ):
        _save_pending(path, pending.model_copy(update={"before_content_base64": None}))
        return
    if instance.recording_clock().timestamp() >= (
        pending.actor.timestamp.timestamp() + _CONFIRMATION_WINDOW_SECONDS
    ):
        raise _refuse(
            "cruxible.doc.confirmation_expired",
            "The document confirmation window has expired",
            repair="Do not repeat this token; its history can no longer be confirmed",
        )
    bodies.store(previous)
    head = journal.read_head(stream, partition)
    journal.activate_writer(stream, partition, fencing_token=_FENCE, expected_head=head)
    ProcedureExhaustWriter(journal=journal, bodies=bodies, fencing_token=_FENCE).append(
        stream=stream,
        partition_id=partition,
        event_kind="doc_edit",
        accepted_coordinate=pending.coordinate,
        definition_digest=stream.identity_digest,
        actor_context=pending.actor,
        recorded_at=instance.recording_clock(),
        expected_head=head,
        payload=payload,
    )
    _save_pending(path, pending.model_copy(update={"before_content_base64": None}))


def read_doc_edits(
    instance: PlaybillInstance,
    *,
    request: GetRequest,
    access: BodyAccessContext,
) -> GetResult:
    """A file's overwrite history, paged by its own journal sequence.

    ``file:source@N`` reads the prior subtree retained by overwrite N. History
    returns metadata only; recovery reads authorize bytes like Document bodies.
    Only edits confirmed after a successful workspace file CAS are visible.
    """
    match = re.fullmatch(r"file:([a-z][a-z0-9_.-]{0,255})(?:@([1-9][0-9]*))?", request.ref)
    if match is None:
        raise ReadRefusalError(
            "cruxible.get.invalid_ref", "Use file:<catalog-name>, or file:<catalog-name>@<revision>"
        )
    source, revision = match.groups()
    if request.detail not in ("history", "body") or request.field is not None:
        raise ReadRefusalError(
            "cruxible.get.detail_unsupported", "Workspace files support history and body only"
        )
    if (revision is None) != (request.detail == "history"):
        raise ReadRefusalError(
            "cruxible.get.invalid_ref",
            "History takes file:<name>; body takes file:<name>@<revision>",
        )
    coordinate = resolve_read_coordinate(instance, request.at)
    journal, stream = _journal(instance)
    partition = section_digest(source.encode()).removeprefix("sha256:")
    last = None if revision is None else int(revision)
    selection = {"ref": request.ref}
    if request.cursor is not None:
        cursor = decode_list_cursor(request.cursor, list_name="doc history", selection=selection)
        if (
            cursor.snapshot != "keyset"
            or len(cursor.last_key) != 1
            or not cursor.last_key[0].isdigit()
        ):
            raise ListCursorMismatch("Invalid document history cursor; read again without cursor")
        last = int(cursor.last_key[0]) - 1
    limit = request.limit or GET_HISTORY_DEFAULT_LIMIT
    rows = (
        ()
        if journal is None
        else journal.select_records(
            stream,
            partition_id=partition,
            last_sequence=last,
            first_sequence=None if revision is None else int(revision),
            descending=True,
            limit=limit + 1 if revision is None else 1,
        )
    )
    fields: dict[str, Any] = {}
    if request.detail == "body":
        if not rows:
            raise ReadRefusalError(
                "cruxible.get.not_found", "No retained overwrite at this revision"
            )
        edit = _EditRecord.model_validate(
            parse_journal_payload(
                instance.body_store().read(rows[0].record.payload_digest, access=_ACCESS)
            )
        )
        content = instance.body_store().read(edit.before_digest, access=access)
        size = len(content)
        if request.range is None and size > GET_BODY_DEFAULT_MAX_BYTES:
            raise ReadRefusalError(
                "cruxible.get.body_too_large", "Pass a byte range to recover this subtree"
            )
        window = request.range or (ByteRange(start=0, end=size) if size else None)
        if window is not None and window.start >= size and window.start != 0:
            raise ReadRefusalError(
                "cruxible.get.range_out_of_bounds", "Pass a range inside the retained subtree"
            )
        if window is not None:
            window = ByteRange(start=window.start, end=min(window.end, size)) if size else None
        chunk = content if window is None else content[window.start : window.end]
        fields["body"] = GetBody(
            document=source,
            media_type="text/markdown",
            size=size,
            body_digest=edit.before_digest,
            range=window,
            content_base64=base64.b64encode(chunk).decode(),
        )
    else:
        revisions = []
        for row in rows[:limit]:
            record = row.record
            edit = _EditRecord.model_validate(
                parse_journal_payload(
                    instance.body_store().read(record.payload_digest, access=_ACCESS)
                )
            )
            ref = f"file:{source}@{record.sequence}"
            step = (
                f"cruxible get {ref} --detail body"
                if request.surface == "cli"
                else f'get(ref="{ref}", detail="body")'
            )
            revisions.append(
                GetRevision(
                    revision=record.sequence,
                    sequence=compact_coordinate(
                        instance, instance.coordinate_for_oid(record.accepted_coordinate.git_oid)
                    ).generation,
                    git_oid=record.accepted_coordinate.git_oid[:12],
                    accepted=format_datetime(record.recorded_at) or "",
                    actor=record.actor_context.actor_id,
                    digest=edit.before_digest,
                    value=ShownObject(object=edit.model_dump(mode="json", exclude={"because"})),
                    next=(step,),
                )
            )
        fields["history"] = GetHistory(revisions=tuple(revisions))
        fields["truncated"] = len(rows) > limit
        fields["next_cursor"] = (
            None
            if len(rows) <= limit
            else encode_list_cursor(
                list_name="doc history",
                coordinate=AcceptedCoordinate.from_internal(coordinate).model_dump(mode="json"),
                selection=selection,
                snapshot="keyset",
                last_key=(str(rows[limit - 1].record.sequence),),
            )
        )
    if request.full_coordinate:
        fields["accepted_coordinate"] = AcceptedCoordinate.from_internal(coordinate)
    return GetResult(
        ref=request.ref,
        kind="file",
        detail=request.detail,
        coordinate=compact_coordinate(instance, coordinate),
        evaluation_time=instance.recording_clock(),
        live=LiveView(
            as_of=LiveHead(
                **compact_coordinate(instance, instance.accepted_coordinate()).model_dump()
            ),
            fields=(request.detail,),
        ),
        **fields,
    )


def validate_doc_edits(
    instance: PlaybillInstance, docs: tuple[DocOp | DocEditObservation, ...]
) -> None:
    # Per request: only supplied edits and blocks of their logical source. No
    # instance Claims/citations are inventoried (S4 is deferred).
    from cruxible_core.service.authoring.write_verbs import _refuse

    for index, edit in enumerate(docs):
        if not isinstance(edit, DocEditObservation):
            raise _refuse(
                "cruxible.write.doc_unobserved",
                "Document operations must be observed by the writer.",
                field_path=f"docs[{index}]",
                repair="Use the CLI, SDK or MCP write adapter beside the workspace",
            )
        if edit.op == "replace":
            try:
                previous = base64.b64decode(edit.before_content_base64 or "", validate=True)
            except ValueError as exc:
                raise _refuse(
                    "cruxible.doc.previous_bytes_mismatch",
                    "Previous subtree is not valid base64",
                    field_path=f"docs[{index}]",
                ) from exc
            if edit.before_content_base64 is None or section_digest(previous) != edit.before_digest:
                raise _refuse(
                    "cruxible.doc.previous_bytes_mismatch",
                    "Previous subtree digest differs",
                    field_path=f"docs[{index}]",
                )
        blocks = source_projection_blocks(instance, edit.source_id)
        page = (
            None
            if edit.source_content_base64 is None
            else base64.b64decode(edit.source_content_base64)
        )
        windows = () if page is None else stamped_projection_windows(page)
        if blocks is None or (
            blocks and (page is None or set(blocks) - {w.block_id for w in windows})
        ):
            raise _refuse(
                "cruxible.projection.window_unverifiable",
                "The edited source's projection windows cannot be verified.",
                field_path=f"docs[{index}]",
                repair="Present the original page with all registered projection markers",
            )
        if declares_projection_block(edit.content) or any(
            (edit.start_byte < w.end_byte and edit.end_byte > w.start_byte)
            or (edit.start_byte == edit.end_byte and w.start_byte <= edit.start_byte < w.end_byte)
            for w in windows
        ):
            raise _refuse(
                "cruxible.doc.projection_edit",
                "Document operations cannot edit projection blocks.",
                field_path=f"docs[{index}]",
                repair="Choose prose outside the block; use block repin to update it",
            )
