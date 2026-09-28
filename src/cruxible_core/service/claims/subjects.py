"""Typed service operations for identity-only governed Subjects."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.candidates import CandidateMemberEvidence
from cruxible_client.contracts.canonical import file_digest
from cruxible_client.contracts.errors import (
    ProjectionIntegrityError,
    ProposalIntegrityError,
    SubjectNotFoundError,
)
from cruxible_client.contracts.subjects import (
    parse_subject,
    subject_digest,
    subject_path,
)
from cruxible_core.indexes.claims.projection_claims import ClaimProjectionView
from cruxible_core.indexes.claims.projection_subjects import SubjectProjectionView
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    PlaybillAcceptedCoordinate,
)
from cruxible_core.service.list_pages import (
    PlaybillListCursorMismatch,
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)


class _StrictSubjectServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillSubjectIncomingClaimV1(_StrictSubjectServiceModel):
    """One live Claim whose subject-valued object is the profiled Subject."""

    tag: Literal["playbill-subject-incoming-claim-v1"] = "playbill-subject-incoming-claim-v1"
    claim_identity: str
    subject_identity: str


class PlaybillSubjectIncomingGroupV1(_StrictSubjectServiceModel):
    """Every incoming edge that arrives on one governed predicate."""

    tag: Literal["playbill-subject-incoming-group-v1"] = "playbill-subject-incoming-group-v1"
    predicate: str
    claims: tuple[PlaybillSubjectIncomingClaimV1, ...]


class PlaybillSubjectView(_StrictSubjectServiceModel):
    tag: Literal["playbill-subject-read-v1"] = "playbill-subject-read-v1"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, object]
    facts: tuple[dict[str, object], ...]
    # Edges where this Subject is the OBJECT. A profile that lists only its own
    # facts cannot answer "what touches this package": the relation is stored
    # once, on the asserting Subject, and is invisible from the object side.
    # Empty on the list surface, which never resolves incoming edges.
    incoming: tuple[PlaybillSubjectIncomingGroupV1, ...] = ()


class PlaybillSubjectListRow(_StrictSubjectServiceModel):
    subject_kind: str
    subject_id: str
    lifecycle: Literal["live", "retired"]
    live_claims: int


class PlaybillSubjectList(_StrictSubjectServiceModel):
    tag: Literal["playbill-subject-list-v2"] = "playbill-subject-list-v2"
    coordinate: PlaybillAcceptedCoordinate
    subject_kind_filter: str | None = None
    subjects: tuple[PlaybillSubjectListRow, ...]
    truncated: bool = False
    next_cursor: str | None = None


class PlaybillSubjectIndexEntry(_StrictSubjectServiceModel):
    identity: str
    subject_kind: str
    subject_id: str
    lifecycle: Literal["live", "retired"]


class PlaybillSubjectIndex(_StrictSubjectServiceModel):
    tag: Literal["playbill-subject-index-v1"] = "playbill-subject-index-v1"
    coordinate: PlaybillAcceptedCoordinate
    subjects: tuple[PlaybillSubjectIndexEntry, ...]


class PlaybillSubjectHistoryEntry(_StrictSubjectServiceModel):
    sequence: int
    coordinate: PlaybillAcceptedCoordinate
    artifact_digest: str
    predecessor_digest: str | None
    lifecycle_state: Literal["live", "retired"]
    change_set_path: str
    changeset_digest: str
    candidate_digest: str


class PlaybillSubjectHistory(_StrictSubjectServiceModel):
    tag: Literal["playbill-subject-history-v1"] = "playbill-subject-history-v1"
    identity: str
    entries: tuple[PlaybillSubjectHistoryEntry, ...]


def _public_subject(
    view: SubjectProjectionView,
    *,
    incoming: tuple[PlaybillSubjectIncomingGroupV1, ...] = (),
) -> PlaybillSubjectView:
    if view.coordinate_kind != "canonical" or not isinstance(
        view.coordinate,
        AcceptedProjectionCoordinate,
    ):
        raise ProposalIntegrityError("canonical Subject service received a provisional view")
    return PlaybillSubjectView(
        coordinate=PlaybillAcceptedCoordinate.from_internal(view.coordinate),
        envelope=view.envelope.model_dump(mode="json"),
        facts=tuple(fact.model_dump(mode="json") for fact in view.facts),
        incoming=incoming,
    )


def _claim_statement(view: ClaimProjectionView) -> Mapping[str, object] | None:
    statement = next(
        (fact.value for fact in view.facts if fact.schema_id == "playbill.claim.statement"),
        None,
    )
    return statement if isinstance(statement, Mapping) else None


def _claim_is_live(view: ClaimProjectionView) -> bool:
    lifecycle = next(
        (fact.value for fact in view.facts if fact.schema_id == "playbill.claim.lifecycle"),
        None,
    )
    if not isinstance(lifecycle, Mapping):
        return False
    state = lifecycle.get("lifecycle")
    return isinstance(state, Mapping) and state.get("state") == "live"


def _incoming_groups(
    claims: Iterable[ClaimProjectionView],
    *,
    subject_path_value: str,
) -> tuple[PlaybillSubjectIncomingGroupV1, ...]:
    """Group live edges whose subject-valued object is this Subject, by predicate.

    Live only, and read at exactly the coordinate the profile was resolved at:
    an incoming row is accepted structure, never a verdict-relative claim about
    whether the relation currently holds.
    """

    grouped: dict[str, list[PlaybillSubjectIncomingClaimV1]] = {}
    for view in claims:
        if not _claim_is_live(view):
            continue
        statement = _claim_statement(view)
        if statement is None:
            continue
        obj = statement.get("object")
        subject = statement.get("subject")
        predicate = statement.get("predicate")
        if not (
            isinstance(obj, Mapping)
            and isinstance(subject, Mapping)
            and isinstance(predicate, str)
            and obj.get("kind") == "subject"
        ):
            continue
        address = obj.get("address")
        if not isinstance(address, Mapping) or address.get("artifact_path") != subject_path_value:
            continue
        asserting = subject.get("artifact_path")
        if not isinstance(asserting, str):
            continue
        grouped.setdefault(predicate, []).append(
            PlaybillSubjectIncomingClaimV1(
                claim_identity=view.envelope.identity,
                subject_identity=asserting,
            )
        )
    return tuple(
        PlaybillSubjectIncomingGroupV1(
            predicate=predicate,
            claims=tuple(
                sorted(
                    grouped[predicate],
                    key=lambda item: (
                        item.subject_identity.encode("utf-8"),
                        item.claim_identity.encode("utf-8"),
                    ),
                )
            ),
        )
        for predicate in sorted(grouped, key=lambda value: value.encode("utf-8"))
    )


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: PlaybillAcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def service_get_playbill_subject(
    instance: PlaybillInstance,
    *,
    identity: str,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillSubjectView:
    coordinate = _resolve_coordinate(instance, at)
    with instance.bind_accepted_projection(coordinate) as projection:
        subject = projection.subject(identity)
        if subject is None:
            raise SubjectNotFoundError(identity)
        incoming = _incoming_groups(
            projection.list_claims(),
            subject_path_value=subject.envelope.path,
        )
    return _public_subject(subject, incoming=incoming)


_SUBJECT_LIST = "subjects"


def service_list_playbill_subjects(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate | None = None,
    subject_kind: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> PlaybillSubjectList:
    """One page of compact Subject rows, in kind-qualified identity order.

    Rows name the Subject and count its live Claims; the envelope and facts
    stay on the Subject read. ``limit`` bounds the page (``None`` reads them
    all). A cursor continues its first page at that page's coordinate.
    """

    selection = {"subject_kind": subject_kind}
    continuation = (
        None
        if cursor is None
        else decode_list_cursor(cursor, list_name=_SUBJECT_LIST, selection=selection)
    )
    if continuation is not None:
        pinned = PlaybillAcceptedCoordinate.model_validate(continuation.coordinate)
        if at is not None and at != pinned:
            raise PlaybillListCursorMismatch(
                f"{PlaybillListCursorMismatch.error_code}: the cursor continues a different "
                "coordinate; list the subjects again without a cursor"
            )
        at = pinned
    coordinate = _resolve_coordinate(instance, at)
    served = PlaybillAcceptedCoordinate.from_internal(coordinate)
    with instance.bind_accepted_projection(coordinate) as projection:
        indexed = tuple(
            (kind, subject_id, lifecycle)
            for _identity, kind, subject_id, lifecycle in projection.subject_index()
            if subject_kind is None or kind == subject_kind
        )
        keys = tuple((kind, subject_id) for kind, subject_id, _lifecycle in indexed)
        snapshot = list_snapshot([list(key) for key in keys])
        page, truncated = page_after_boundary(
            indexed,
            keys=keys,
            snapshot=snapshot,
            continuation=continuation,
            limit=len(indexed) if limit is None else limit,
            list_name=_SUBJECT_LIST,
        )
        counts = _live_claim_counts(
            projection.typed.connection,
            tuple(subject_path(kind, subject_id) for kind, subject_id, _ in page),
        )
    rows = tuple(
        PlaybillSubjectListRow(
            subject_kind=kind,
            subject_id=subject_id,
            lifecycle="retired" if lifecycle == "retired" else "live",
            live_claims=counts.get(subject_path(kind, subject_id), 0),
        )
        for kind, subject_id, lifecycle in page
    )
    return PlaybillSubjectList(
        coordinate=served,
        subject_kind_filter=subject_kind,
        subjects=rows,
        truncated=truncated,
        next_cursor=(
            encode_list_cursor(
                list_name=_SUBJECT_LIST,
                coordinate=served.model_dump(mode="json"),
                selection=selection,
                snapshot=snapshot,
                last_key=(rows[-1].subject_kind, rows[-1].subject_id),
            )
            if truncated and rows
            else None
        ),
    )


def _live_claim_counts(connection: sqlite3.Connection, paths: tuple[str, ...]) -> dict[str, int]:
    """Live Claims per Subject path, for just the Subjects on one page."""

    if not paths:
        return {}
    placeholders = ",".join("?" for _ in paths)
    return {
        str(path): int(count)
        for path, count in connection.execute(
            "SELECT subject_path, count(*) FROM claims "
            f"WHERE lifecycle='live' AND subject_path IN ({placeholders}) "
            "GROUP BY subject_path",
            paths,
        )
    }


def service_list_playbill_subject_index(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillSubjectIndex:
    """Which Subjects exist at a coordinate, without compiling their facts."""

    coordinate = _resolve_coordinate(instance, at)
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = projection.subject_index()
    return PlaybillSubjectIndex(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        subjects=tuple(
            PlaybillSubjectIndexEntry(
                identity=identity,
                subject_kind=subject_kind,
                subject_id=subject_id,
                lifecycle="retired" if lifecycle == "retired" else "live",
            )
            for identity, subject_kind, subject_id, lifecycle in rows
        ),
    )


def _subject_path_from_identity(identity: str) -> str:
    parsed = parse_artifact_identity(identity)
    if parsed.kind != "Subject" or parsed.name.count("/") != 1:
        raise SubjectNotFoundError(identity)
    subject_kind, subject_id = parsed.name.split("/", 1)
    return subject_path(subject_kind, subject_id)


def service_playbill_subject_history(
    instance: PlaybillInstance,
    *,
    identity: str,
) -> PlaybillSubjectHistory:
    try:
        path = _subject_path_from_identity(identity)
    except ValueError as exc:
        raise SubjectNotFoundError(identity) from exc
    entries: list[PlaybillSubjectHistoryEntry] = []
    with instance.accepted_history_reader() as history:
        for location in history.member_history(path):
            generation = history.generation(location.sequence)
            record = history.read_member_record(location, instance.blob_at)
            member = record.members[location.member_ordinal]
            content = instance.blob_at(generation.git_oid, path)
            if member.disposition == "delete" and content is None:
                continue
            if content is None:
                raise ProjectionIntegrityError("Subject history source is unavailable")
            shell = parse_subject(content, path=path)
            digest = subject_digest(shell).tagged
            source_digest = (
                file_digest(content).tagged
                if isinstance(member, CandidateMemberEvidence)
                else digest
            )
            if source_digest != location.artifact_digest:
                raise ProjectionIntegrityError("Subject history source binding differs")
            entries.append(
                PlaybillSubjectHistoryEntry(
                    sequence=generation.sequence,
                    coordinate=PlaybillAcceptedCoordinate(
                        git_oid=generation.git_oid,
                        semantic_root=generation.semantic_root,
                        generation_root=generation.generation_root,
                        compiler_digest=generation.compiler_digest,
                    ),
                    artifact_digest=digest,
                    predecessor_digest=shell.lifecycle.predecessor_digest,
                    lifecycle_state=shell.lifecycle.state,
                    change_set_path=f"changesets/cs-{record.sequence:020d}.json",
                    changeset_digest=record.changeset_digest,
                    candidate_digest=record.candidate_digest,
                )
            )
    if not entries:
        raise SubjectNotFoundError(identity)
    return PlaybillSubjectHistory(identity=identity, entries=tuple(entries))


__all__ = [
    "PlaybillSubjectHistory",
    "PlaybillSubjectHistoryEntry",
    "PlaybillSubjectIncomingClaimV1",
    "PlaybillSubjectIncomingGroupV1",
    "PlaybillSubjectIndex",
    "PlaybillSubjectIndexEntry",
    "PlaybillSubjectList",
    "PlaybillSubjectListRow",
    "PlaybillSubjectView",
    "service_get_playbill_subject",
    "service_list_playbill_subject_index",
    "service_list_playbill_subjects",
    "service_playbill_subject_history",
]
