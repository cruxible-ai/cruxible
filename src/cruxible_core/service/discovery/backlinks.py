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
from typing import Any

from cruxible_client.contracts import AcceptedCoordinate
from cruxible_client.contracts.backlinks import (
    Backlink,
    BacklinksRequest,
    BacklinksResult,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import ReadRefusalError
from cruxible_client.contracts.get_reads import GetCoordinate
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


#: Definitions one review asks for guidance about, and Documents shown per definition.
REVIEW_GUIDANCE_MAX_DEFINITIONS = 20
REVIEW_GUIDANCE_PER_DEFINITION = 5


def review_guidance(
    instance: PlaybillInstance,
    definitions: Iterable[str],
    *,
    line: str | None = None,
) -> tuple[ReviewGuidance, ...]:
    """The Documents that ``guides`` each definition a proposal touches, at the head.

    ``definitions`` are ClaimType (and Procedure) identities; ``line`` the
    originating Line, whose Procedure is asked too. Bounded: at most
    ``REVIEW_GUIDANCE_MAX_DEFINITIONS`` definitions, each one indexed range of
    ``REVIEW_GUIDANCE_PER_DEFINITION`` rows.
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
        for definition in wanted[:REVIEW_GUIDANCE_MAX_DEFINITIONS]:
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
    "backlinks_of_resolved",
    "claim_type_identity",
    "resolve_backlink_target",
    "review_guidance",
    "service_definition_guidance",
    "service_playbill_backlinks",
    "service_section_backlinks",
    "service_subject_backlinks",
]
