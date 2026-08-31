"""Typed service operations for identity-only governed Subjects."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.errors import ProposalIntegrityError, SubjectNotFoundError
from cruxible_client.contracts.subjects import (
    parse_subject,
    subject_digest,
    subject_path,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.projection_subjects import SubjectProjectionView
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
)


class _StrictSubjectServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillSubjectView(_StrictSubjectServiceModel):
    tag: Literal["playbill-subject-read-v1"] = "playbill-subject-read-v1"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, object]
    facts: tuple[dict[str, object], ...]


class PlaybillSubjectList(_StrictSubjectServiceModel):
    tag: Literal["playbill-subject-list-v1"] = "playbill-subject-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    subjects: tuple[PlaybillSubjectView, ...]


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


def _public_subject(view: SubjectProjectionView) -> PlaybillSubjectView:
    if view.coordinate_kind != "canonical" or not isinstance(
        view.coordinate,
        AcceptedProjectionCoordinate,
    ):
        raise ProposalIntegrityError("canonical Subject service received a provisional view")
    return PlaybillSubjectView(
        coordinate=PlaybillAcceptedCoordinate.from_internal(view.coordinate),
        envelope=view.envelope.model_dump(mode="json"),
        facts=tuple(fact.model_dump(mode="json") for fact in view.facts),
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
    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.sequence == 0:
        raise SubjectNotFoundError(identity)
    with instance.bind_accepted_projection(coordinate) as projection:
        subject = projection.subject(identity)
    if subject is None:
        raise SubjectNotFoundError(identity)
    return _public_subject(subject)


def service_list_playbill_subjects(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillSubjectList:
    coordinate = _resolve_coordinate(instance, at)
    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if generation.sequence == 0:
        subjects: tuple[PlaybillSubjectView, ...] = ()
    else:
        with instance.bind_accepted_projection(coordinate) as projection:
            subjects = tuple(_public_subject(item) for item in projection.list_subjects())
    return PlaybillSubjectList(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        subjects=subjects,
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
    for generation in instance.accepted_history()[1:]:
        record = generation.record
        if record is None or not any(member.path == path for member in record.members):
            continue
        content = instance.tree_at(generation.oid).get(path)
        if content is None:
            continue
        shell = parse_subject(content, path=path)
        entries.append(
            PlaybillSubjectHistoryEntry(
                sequence=generation.sequence,
                coordinate=PlaybillAcceptedCoordinate.from_internal(
                    instance.coordinate_for_oid(generation.oid)
                ),
                artifact_digest=subject_digest(shell).tagged,
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
    "PlaybillSubjectList",
    "PlaybillSubjectView",
    "service_get_playbill_subject",
    "service_list_playbill_subjects",
    "service_playbill_subject_history",
]
