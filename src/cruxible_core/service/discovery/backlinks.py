"""Backlink reads: one service function per target kind, over the backlink index.

- ``service_subject_backlinks``: the notes about a Subject, the Documents
  linked ``about`` it, and the workspace sections its live Claims cite;
- ``service_definition_guidance``: the governed Documents that ``guides`` a
  ClaimType or Procedure (review guidance);
- ``service_section_backlinks``: the live Claims resting on one workspace section.

Every read is a keyset range of ``backlinks`` (indexes/evidence/backlinks.py)
bounded by ``limit``: a page costs its rows plus one, a title or predicate per
row, and one latest-observation probe per distinct section on the page --
never the target's whole link set. A section row reads ``changed_since_cited``
when accepted state holds a later observation of that section with other
bytes; whether the workspace file moved is the writer-side adapter's to say.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts import AcceptedCoordinate
from cruxible_client.contracts.backlinks import (
    Backlink,
    BacklinksRequest,
    BacklinksResult,
)
from cruxible_client.contracts.capture_reads import BodyRestricted
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import ReadRefusalError
from cruxible_client.contracts.get_reads import GetCoordinate, GetNoteLink
from cruxible_client.contracts.proposal_summary import ReviewGuidance
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.indexes.evidence.backlinks import section_target
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.list_pages import (
    ListCursorMismatch,
    decode_list_cursor,
    encode_list_cursor,
)
from cruxible_core.service.read_refusals import resolve_read_coordinate

_LIST = "backlinks"
_KEYSET = "keyset"
_COLUMNS = (
    "relation,source_kind,source_identity,link_key,source_id,section,cited_digest,role,"
    "claim_subject,capture_digest"
)


def _read_coordinate(
    instance: PlaybillInstance, request: BacklinksRequest
) -> AcceptedProjectionCoordinate:
    """The coordinate this page reads: the cursor's pinned one, else ``at`` (default head).

    A continuation resolves its target here, at the coordinate its first page
    read, so a key, alias or name that moved since then still continues the
    same listing. An explicit ``at`` must agree with the cursor.
    """

    if request.cursor is None:
        return resolve_read_coordinate(instance, request.at)
    continuation = decode_list_cursor(request.cursor, list_name=_LIST, selection=None)
    pinned = AcceptedCoordinate.model_validate(continuation.coordinate)
    coordinate = resolve_read_coordinate(instance, pinned)
    if request.at is not None and AcceptedCoordinate.from_internal(
        resolve_read_coordinate(instance, request.at)
    ) != AcceptedCoordinate.from_internal(coordinate):
        raise ListCursorMismatch(
            f"{ListCursorMismatch.error_code}: the cursor continues a different coordinate; "
            "omit at to continue it"
        )
    return coordinate


def _continuation(
    instance: PlaybillInstance, request: BacklinksRequest, selection: dict[str, Any]
) -> tuple[tuple[str, ...], AcceptedProjectionCoordinate]:
    """The key after which this page starts, and the coordinate the listing is read at."""

    coordinate = _read_coordinate(instance, request)
    if request.cursor is None:
        return (), coordinate
    continuation = decode_list_cursor(request.cursor, list_name=_LIST, selection=selection)
    if continuation.snapshot != _KEYSET or len(continuation.last_key) != 3:
        raise ListCursorMismatch(
            f"{ListCursorMismatch.error_code}: the cursor does not continue this listing; "
            "read backlinks again without a cursor"
        )
    return continuation.last_key, coordinate


def _subject_ref(identity: str) -> str:
    return identity.removeprefix("Subject:")


def _page(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    target: str,
    selection: dict[str, Any],
    rows: Sequence[sqlite3.Row | tuple[Any, ...]],
    limit: int,
    connection: sqlite3.Connection,
    access: BodyAccessContext | None = None,
) -> BacklinksResult:
    """One page; a row whose Capture is withheld from ``access`` loses its section (C14).

    Access is decided per row from the cited Capture's exact kind version
    (maintained metadata, one batched resolution per page) before anything of
    the Capture is read; the row keeps its relation, source and Subject.
    """

    from cruxible_core.service.evidence.body_access import (
        NO_RESTRICTED_ACCESS,
        capture_access_resolver,
    )

    truncated = len(rows) > limit
    page = list(rows[:limit])
    withheld, notice = capture_access_resolver(instance, coordinate).withheld(
        sorted({str(row[9]) for row in page if row[9] is not None}),
        access or NO_RESTRICTED_ACCESS,
    )
    claims = [str(row[2]) for row in page if row[1] == "Claim"]
    documents = [str(row[2]) for row in page if row[1] == "Document"]
    predicates = dict(
        connection.execute(
            "SELECT identity,predicate FROM claims WHERE identity IN ("
            + ",".join("?" * len(claims))
            + ")",
            claims,
        ).fetchall()
        if claims
        else ()
    )
    titles = dict(
        connection.execute(
            "SELECT identity,title FROM documents WHERE identity IN ("
            + ",".join("?" * len(documents))
            + ")",
            documents,
        ).fetchall()
        if documents
        else ()
    )
    latest: dict[tuple[str, str], str | None] = {}
    for row in page:
        if row[4] is not None and (row[4], row[5]) not in latest:
            found = connection.execute(
                "SELECT cited_digest FROM backlinks INDEXED BY backlinks_by_section "
                "WHERE source_id=? AND section=? AND observed_at_us IS NOT NULL "
                "ORDER BY observed_at_us DESC LIMIT 1",
                (row[4], row[5]),
            ).fetchone()
            latest[(row[4], row[5])] = None if found is None else str(found[0])
    links = []
    for relation, kind, identity, _key, source_id, section, digest, role, subject, capture in page:
        if capture is not None and capture in withheld:
            source_id, section, digest = None, None, None
        newest = None if source_id is None else latest.get((source_id, section))
        links.append(
            Backlink(
                relation=relation,
                source_kind=kind,
                source=str(identity).split(":", 1)[1] if kind != "Document" else str(identity),
                title=titles.get(identity),
                subject=None if subject is None else _subject_ref(str(subject)),
                field=predicates.get(identity),
                role=role,
                file=source_id,
                section=None if section is None else tuple(json.loads(section)),
                cited_digest=digest,
                drift=None
                if source_id is None
                else "changed_since_cited"
                if newest is not None and newest != digest
                else "unknown",
            )
        )
    served = AcceptedCoordinate.from_internal(coordinate)
    with instance.accepted_history_reader(at=served) as history:
        generation = int(history.sequence)
    next_cursor = (
        encode_list_cursor(
            list_name=_LIST,
            coordinate=served.model_dump(mode="json"),
            selection=selection,
            snapshot=_KEYSET,
            last_key=(str(page[-1][0]), str(page[-1][2]), str(page[-1][3])),
        )
        if truncated and page
        else None
    )
    return BacklinksResult(
        target=target,
        coordinate=GetCoordinate(git_oid=served.git_oid[:12], generation=generation),
        links=tuple(links),
        truncated=truncated,
        next_cursor=next_cursor,
        withheld=notice,
    )


def service_subject_backlinks(
    instance: PlaybillInstance,
    *,
    subject: str,
    request: BacklinksRequest,
    access: BodyAccessContext | None = None,
) -> BacklinksResult:
    """Notes and Documents about a Subject, then the sections its live Claims cite.

    Two keyset ranges -- ``about`` rows by target, ``cites`` rows by the citing
    Claim's Subject -- walked in that order, each bounded by the page.
    """

    selection = {"subject": subject}
    after, coordinate = _continuation(instance, request, selection)
    target = f"Subject:{subject}"
    limit = request.limit
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        rows: list[Any] = []
        if not after or after[0] == "about":
            rows.extend(
                connection.execute(
                    f"SELECT {_COLUMNS} FROM backlinks WHERE target=? AND relation='about' "
                    "AND (source_identity,link_key)>(?,?) "
                    "ORDER BY source_identity,link_key LIMIT ?",
                    (target, *(after[1:] if after else ("", "")), limit + 1),
                ).fetchall()
            )
        if len(rows) <= limit:
            resume = after[1:] if after and after[0] == "cites" else ("", "")
            rows.extend(
                connection.execute(
                    f"SELECT {_COLUMNS} FROM backlinks INDEXED BY backlinks_by_claim_subject "
                    "WHERE claim_subject=? AND (source_identity,link_key)>(?,?) "
                    "ORDER BY source_identity,link_key LIMIT ?",
                    (target, *resume, limit + 1 - len(rows)),
                ).fetchall()
            )
        return _page(
            instance,
            coordinate,
            target=subject,
            selection=selection,
            rows=rows,
            limit=limit,
            connection=connection,
            access=access,
        )


#: The ``about`` rows of notes are the keyset range ``[Note:, Note;)`` of the
#: primary key under one target: ``;`` sorts right after ``:``.
_NOTE_LOW = "Note:"
_NOTE_HIGH = "Note;"
_NOTE_COLUMNS = "source_identity,link_key,source_id,section,cited_digest,capture_digest"


def _note_links(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    connection: sqlite3.Connection,
    rows: Sequence[sqlite3.Row | tuple[Any, ...]],
    *,
    access: BodyAccessContext | None,
) -> tuple[tuple[GetNoteLink, ...], BodyRestricted | None]:
    """Section pointers for note rows; a withheld section Capture drops the section (C14).

    One batched access resolution, and one latest-observation probe per row
    (a note is one section): the rows given, never the Subject's link set.
    """

    from cruxible_core.service.evidence.body_access import (
        NO_RESTRICTED_ACCESS,
        capture_access_resolver,
    )

    withheld, notice = capture_access_resolver(instance, coordinate).withheld(
        sorted({str(row[5]) for row in rows if row[5] is not None}),
        access or NO_RESTRICTED_ACCESS,
    )
    links = []
    for identity, _key, source_id, section, digest, capture in rows:
        note = str(identity).split(":", 1)[1]
        if capture is not None and capture in withheld:
            links.append(GetNoteLink(note=note))
            continue
        newest = connection.execute(
            "SELECT cited_digest FROM backlinks INDEXED BY backlinks_by_section "
            "WHERE source_id=? AND section=? AND observed_at_us IS NOT NULL "
            "ORDER BY observed_at_us DESC LIMIT 1",
            (source_id, section),
        ).fetchone()
        links.append(
            GetNoteLink(
                note=note,
                file=source_id,
                section=tuple(json.loads(section)),
                cited_digest=digest,
                drift="changed_since_cited"
                if newest is not None and str(newest[0]) != digest
                else "unknown",
            )
        )
    return tuple(links), notice


def card_notes(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    connection: sqlite3.Connection,
    subject: str,
    *,
    limit: int,
    access: BodyAccessContext | None = None,
) -> tuple[tuple[GetNoteLink, ...], int | None]:
    """The first ``limit`` live notes about a Subject, as pointers, and the total if cut.

    One keyset range of the backlink primary key (target, relation, the note
    identities) bounded by ``limit + 1``; only a cut list counts the rest of
    this one target's note rows.
    """

    target = f"Subject:{subject}"
    rows = connection.execute(
        f"SELECT {_NOTE_COLUMNS} FROM backlinks WHERE target=? AND relation='about' "
        "AND source_identity>=? AND source_identity<? "
        "ORDER BY source_identity,link_key LIMIT ?",
        (target, _NOTE_LOW, _NOTE_HIGH, limit + 1),
    ).fetchall()
    total = None
    if len(rows) > limit:
        total = int(
            connection.execute(
                "SELECT count(*) FROM backlinks WHERE target=? AND relation='about' "
                "AND source_identity>=? AND source_identity<?",
                (target, _NOTE_LOW, _NOTE_HIGH),
            ).fetchone()[0]
        )
    links, _notice = _note_links(instance, coordinate, connection, rows[:limit], access=access)
    return links, total


@dataclass(frozen=True)
class NotesPage:
    """One page of the notes about a Subject: ``get`` with ``detail="notes"``."""

    target: str
    notes: tuple[GetNoteLink, ...]
    withheld: BodyRestricted | None
    truncated: bool
    next_cursor: str | None
    coordinate: GetCoordinate


def service_subject_notes(
    instance: PlaybillInstance,
    *,
    subject: str,
    request: BacklinksRequest,
    access: BodyAccessContext | None = None,
) -> NotesPage:
    """The live notes about a Subject, one keyset page of their ``about`` rows.

    The cursor is the backlinks listing's own, with a selection of its own,
    so a backlinks cursor never continues a notes page or the other way.
    """

    selection = {"subject": subject, "notes": True}
    after, coordinate = _continuation(instance, request, selection)
    target = f"Subject:{subject}"
    limit = request.limit
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        rows = connection.execute(
            f"SELECT {_NOTE_COLUMNS} FROM backlinks WHERE target=? AND relation='about' "
            "AND source_identity>=? AND source_identity<? AND (source_identity,link_key)>(?,?) "
            "ORDER BY source_identity,link_key LIMIT ?",
            (target, _NOTE_LOW, _NOTE_HIGH, *(after[1:] if after else ("", "")), limit + 1),
        ).fetchall()
        truncated = len(rows) > limit
        page = rows[:limit]
        links, notice = _note_links(instance, coordinate, connection, page, access=access)
    served = AcceptedCoordinate.from_internal(coordinate)
    with instance.accepted_history_reader(at=served) as history:
        generation = int(history.sequence)
    next_cursor = (
        encode_list_cursor(
            list_name=_LIST,
            coordinate=served.model_dump(mode="json"),
            selection=selection,
            snapshot=_KEYSET,
            last_key=("about", str(page[-1][0]), str(page[-1][1])),
        )
        if truncated and page
        else None
    )
    return NotesPage(
        target=subject,
        notes=links,
        withheld=notice,
        truncated=truncated,
        next_cursor=next_cursor,
        coordinate=GetCoordinate(git_oid=served.git_oid[:12], generation=generation),
    )


def subject_has_backlinks(connection: Any, subject: str) -> bool:
    """Whether ``backlinks`` lists anything for a Subject: two point seeks, no page.

    A note or Document about it (the primary key's ``target, relation`` prefix),
    or a section one of its live Claims cites (``backlinks_by_claim_subject``).
    """

    target = f"Subject:{subject}"
    return (
        connection.execute(
            "SELECT 1 FROM backlinks WHERE target=? AND relation='about' LIMIT 1", (target,)
        ).fetchone()
        is not None
        or connection.execute(
            "SELECT 1 FROM backlinks INDEXED BY backlinks_by_claim_subject "
            "WHERE claim_subject=? LIMIT 1",
            (target,),
        ).fetchone()
        is not None
    )


def service_definition_guidance(
    instance: PlaybillInstance,
    *,
    definition: str,
    request: BacklinksRequest,
    access: BodyAccessContext | None = None,
) -> BacklinksResult:
    """The governed Documents that ``guides`` one ClaimType or Procedure."""

    selection = {"definition": definition}
    after, coordinate = _continuation(instance, request, selection)
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM backlinks WHERE target=? AND relation='guides' "
            "AND (source_identity,link_key)>(?,?) ORDER BY source_identity,link_key LIMIT ?",
            (definition, *(after[1:] if after else ("", "")), request.limit + 1),
        ).fetchall()
        return _page(
            instance,
            coordinate,
            target=definition,
            selection=selection,
            rows=rows,
            limit=request.limit,
            connection=connection,
            access=access,
        )


def service_section_backlinks(
    instance: PlaybillInstance,
    *,
    source_id: str,
    section: tuple[str, ...],
    request: BacklinksRequest,
    access: BodyAccessContext | None = None,
) -> BacklinksResult:
    """The live Claims resting on one workspace section (its catalogued source + path)."""

    selection = {"file": source_id, "section": list(section)}
    after, coordinate = _continuation(instance, request, selection)
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM backlinks WHERE target=? AND relation='cites' "
            "AND (source_identity,link_key)>(?,?) ORDER BY source_identity,link_key LIMIT ?",
            (
                section_target(source_id, section),
                *(after[1:] if after else ("", "")),
                request.limit + 1,
            ),
        ).fetchall()
        return _page(
            instance,
            coordinate,
            target=f"{source_id}#{'/'.join(section)}",
            selection=selection,
            rows=rows,
            limit=request.limit,
            connection=connection,
            access=access,
        )


def card_guidance(
    connection: sqlite3.Connection, definition: str, *, limit: int
) -> tuple[tuple[tuple[str, str | None], ...], int | None]:
    """The first ``limit`` Documents that ``guides`` one definition, and the total if cut.

    One range of the backlink primary key (target, relation) bounded by
    ``limit + 1`` rows, each Document's title by its key; only a cut list
    counts the rest, over the same range of this one target.
    """

    rows = connection.execute(
        "SELECT b.source_identity,d.title FROM backlinks b "
        "LEFT JOIN documents d ON d.identity=b.source_identity "
        "WHERE b.target=? AND b.relation='guides' ORDER BY b.source_identity LIMIT ?",
        (definition, limit + 1),
    ).fetchall()
    total = None
    if len(rows) > limit:
        total = int(
            connection.execute(
                "SELECT count(*) FROM backlinks WHERE target=? AND relation='guides'",
                (definition,),
            ).fetchone()[0]
        )
    shown = tuple(
        (str(document), None if title is None else str(title)) for document, title in rows[:limit]
    )
    return shown, total


#: Guided definitions one review shows, and Documents shown per definition.
REVIEW_GUIDANCE_MAX_DEFINITIONS = 20
REVIEW_GUIDANCE_PER_DEFINITION = 5


def review_guidance(
    instance: PlaybillInstance,
    definitions: Iterable[str],
    *,
    line: str | None = None,
) -> tuple[ReviewGuidance, ...]:
    """The Documents that ``guides`` each definition a proposal touches, at the head.

    ``definitions`` are every ClaimType (and Procedure) identity the change
    set touches; ``line`` the originating Line, whose Procedure is asked too.
    Each is asked, in identity order, until ``REVIEW_GUIDANCE_MAX_DEFINITIONS``
    of them have guidance: the cap counts guided definitions, never the ones
    asked, so which lines a review shows depends on the definitions and not on
    the order of the change set's members (Claim paths are content ids, which
    differ between two submissions of the same change). One indexed range of
    ``REVIEW_GUIDANCE_PER_DEFINITION`` rows per definition asked: work follows
    the change set's distinct definitions, never the instance.
    """

    wanted = sorted(set(definitions), key=lambda item: item.encode("utf-8"))
    if not wanted and line is None:
        return ()
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        connection = projection.typed.connection
        if line is not None:
            found = connection.execute(
                "SELECT procedure_identity FROM lines WHERE identity=?", (line,)
            ).fetchone()
            if found is not None:
                wanted.insert(0, str(found[0]))
        guidance: list[ReviewGuidance] = []
        guided = 0
        for definition in wanted:
            if guided == REVIEW_GUIDANCE_MAX_DEFINITIONS:
                break
            shown = len(guidance)
            for document, title in connection.execute(
                "SELECT b.source_identity,d.title FROM backlinks b "
                "LEFT JOIN documents d ON d.identity=b.source_identity "
                "WHERE b.target=? AND b.relation='guides' ORDER BY b.source_identity LIMIT ?",
                (definition, REVIEW_GUIDANCE_PER_DEFINITION),
            ):
                guidance.append(
                    ReviewGuidance(
                        definition=definition,
                        document=str(document),
                        title=None if title is None else str(title),
                    )
                )
            guided += len(guidance) > shown
    return tuple(guidance)


def claim_type_identity(artifact: Mapping[str, Any] | None, artifact_kind: str) -> str | None:
    """The ClaimType a reviewed Claim or ClaimType member is about, from its own bytes."""

    if artifact is None:
        return None
    if artifact_kind == "claim":
        statement = artifact.get("statement")
        predicate = statement.get("predicate") if isinstance(statement, Mapping) else None
    elif artifact_kind == "claim-type":
        predicate = artifact.get("predicate")
    else:
        return None
    return f"ClaimType:{predicate}" if isinstance(predicate, str) else None


def _not_backlinkable(ref: str, kind: str) -> ReadRefusalError:
    return ReadRefusalError(
        "cruxible.backlinks.target_unsupported",
        f"{ref!r} is a {kind}; backlinks point into a Subject, a ClaimType, a Procedure "
        "or a workspace section",
        candidates=(),
        repair=RepairOperation(operation="cruxible.get", arguments={"ref": ref}),
        repair_line="Name a Subject (kind/id), ClaimType:<predicate>, Procedure:<name>, "
        "or file and section",
        context={"ref": ref},
    )


def resolve_backlink_target(
    instance: PlaybillInstance,
    request: BacklinksRequest,
    *,
    access: BodyAccessContext | None = None,
) -> tuple[Any, AcceptedProjectionCoordinate]:
    """Resolve ``request.ref`` the way ``get`` resolves a reference, at the page's coordinate."""

    from cruxible_core.service.discovery.get import resolve_get_ref
    from cruxible_core.service.evidence.body_access import NO_RESTRICTED_ACCESS

    assert request.ref is not None
    reference = request.ref.strip()
    if reference.startswith(("CAP-", "Capture:", "sha256:")):
        # Capture references are never backlink targets: refused before any
        # resolution, so no Capture is verified (or read) for this reader (C14).
        raise _not_backlinkable(request.ref, "capture")
    coordinate = _read_coordinate(instance, request)
    resolved = resolve_get_ref(
        instance,
        coordinate,
        request.ref,
        surface=request.surface,
        access=access or NO_RESTRICTED_ACCESS,
    )
    return resolved, coordinate


def backlinks_of_resolved(
    instance: PlaybillInstance,
    resolved: Any,
    *,
    request: BacklinksRequest,
    access: BodyAccessContext | None = None,
) -> BacklinksResult:
    """One backlinks page of a resolved Subject, ClaimType or Procedure reference."""

    if resolved.kind == "subject":
        return service_subject_backlinks(
            instance,
            subject=resolved.identity.removeprefix("Subject:"),
            request=request,
            access=access,
        )
    if resolved.kind in ("claim_type", "procedure"):
        return service_definition_guidance(
            instance, definition=resolved.identity, request=request, access=access
        )
    raise _not_backlinkable(str(request.ref), resolved.kind)


def service_playbill_backlinks(
    instance: PlaybillInstance,
    *,
    request: BacklinksRequest,
    access: BodyAccessContext | None = None,
) -> BacklinksResult:
    """Resolve the target the way ``get`` resolves a reference, then read its backlinks."""

    if request.file is not None:
        assert request.section is not None
        return service_section_backlinks(
            instance,
            source_id=request.file,
            section=request.section,
            request=request,
            access=access,
        )
    resolved, _coordinate = resolve_backlink_target(instance, request, access=access)
    return backlinks_of_resolved(instance, resolved, request=request, access=access)


__all__ = [
    "NotesPage",
    "backlinks_of_resolved",
    "card_guidance",
    "card_notes",
    "claim_type_identity",
    "resolve_backlink_target",
    "review_guidance",
    "service_definition_guidance",
    "service_playbill_backlinks",
    "service_section_backlinks",
    "service_subject_backlinks",
    "service_subject_notes",
]
