"""Admit workspace edits and retain overwritten subtrees; never open workspace paths."""

import base64
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.declared_blocks import (
    declares_projection_block,
    stamped_projection_windows,
)
from cruxible_client.contracts.doc_sections import SectionPath, section_digest
from cruxible_client.contracts.errors import ReadRefusalError
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
    DocEditObservation,
    DocOp,
    WriteOutcome,
    WriteRequest,
)
from cruxible_core.authoring.registrations import source_projection_blocks
from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.exhaust.records import (
    DOC_EDIT_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    parse_journal_payload,
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


class _EditRecord(BaseModel):
    """An admitted overwrite; the workspace adapter still performs its file CAS.

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


def record_doc_edits(
    instance: PlaybillInstance,
    *,
    request: WriteRequest,
    outcome: WriteOutcome,
    actor: AuthenticatedActor,
) -> None:
    """Retain only changed replacement subtrees after admission, before local I/O."""
    for edit in request.docs:
        if (
            not isinstance(edit, DocEditObservation)
            or edit.op != "replace"
            or (edit.before_digest == edit.after_digest)
        ):
            continue
        assert edit.before_content_base64 is not None and edit.before_section is not None
        bodies = instance.body_store()
        previous = bodies.store(base64.b64decode(edit.before_content_base64))
        assert previous.digest == edit.before_digest
        journal, stream = _journal(instance, create=True)
        assert journal is not None
        partition = section_digest(edit.source_id.encode()).removeprefix("sha256:")
        head = journal.read_head(stream, partition)
        journal.activate_writer(stream, partition, fencing_token=_FENCE, expected_head=head)
        ProcedureExhaustWriter(journal=journal, bodies=bodies, fencing_token=_FENCE).append(
            stream=stream,
            partition_id=partition,
            event_kind="doc_edit",
            accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            definition_digest=stream.identity_digest,
            actor_context=GovernedActorContext(
                actor_type="service_account",
                actor_id=actor.actor_id,
                org_id=instance.descriptor.instance_id,
                operation_id="doc-edit",
                timestamp=instance.recording_clock(),
            ),
            recorded_at=instance.recording_clock(),
            expected_head=head,
            payload=_EditRecord(
                source_id=edit.source_id,
                before_section=edit.before_section,
                section=edit.section,
                before_digest=previous.digest,
                after_digest=edit.after_digest,
                status=outcome.status,
                proposal=None if outcome.proposal is None else outcome.proposal.proposal_id,
                because=request.because,
            ).model_dump(mode="json"),
        )


def read_doc_edits(
    instance: PlaybillInstance,
    *,
    request: GetRequest,
    access: BodyAccessContext,
) -> GetResult:
    """A file's overwrite history, paged by its own journal sequence.

    ``file:source@N`` reads the prior subtree retained by overwrite N. History
    returns metadata only; recovery reads authorize bytes like Document bodies.
    Operational admission records are live, including when a file CAS conflicts.
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
