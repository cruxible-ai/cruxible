"""Coordinate-labeled Subject reads over canonical and provisional projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_core.playbill.compiler import projection_registry_for_compiler
from cruxible_core.playbill.errors import ProjectionCoordinateError
from cruxible_core.playbill.projection import (
    AcceptedProjectionCoordinate,
    ProvisionalProjectionCoordinate,
    verify_provisional_tree,
)
from cruxible_core.playbill.projection_artifacts import (
    ArtifactEnvelopeRow,
    ParsedProjectionTree,
    parse_projection_tree,
)
from cruxible_core.playbill.projection_extensions import (
    ProjectionExtensionRegistry,
    ProjectionFact,
)


class _StrictSubjectProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectedSubjectEnvelope(_StrictSubjectProjectionModel):
    identity: str
    kind: Literal["subject"]
    format_tag: str
    path: str
    artifact_digest: str
    predecessor_digest: str | None
    revision: int


class ProjectedSubjectFact(_StrictSubjectProjectionModel):
    schema_id: str
    schema_version: int
    fact_key: str
    value: object


class SubjectProjectionView(_StrictSubjectProjectionModel):
    tag: Literal["playbill-subject-projection-v1"] = "playbill-subject-projection-v1"
    coordinate_kind: Literal["canonical", "provisional"]
    coordinate: AcceptedProjectionCoordinate | ProvisionalProjectionCoordinate
    envelope: ProjectedSubjectEnvelope
    facts: tuple[ProjectedSubjectFact, ...]


def subject_projection_view(
    envelope: ArtifactEnvelopeRow,
    facts: Iterable[ProjectionFact],
    *,
    coordinate: AcceptedProjectionCoordinate | ProvisionalProjectionCoordinate,
) -> SubjectProjectionView:
    if envelope.kind != "subject":
        raise ProjectionCoordinateError("Subject query received a non-Subject envelope")
    return SubjectProjectionView(
        coordinate_kind=(
            "provisional"
            if isinstance(coordinate, ProvisionalProjectionCoordinate)
            else "canonical"
        ),
        coordinate=coordinate,
        envelope=ProjectedSubjectEnvelope(
            identity=envelope.identity,
            kind="subject",
            format_tag=envelope.format_tag,
            path=envelope.path,
            artifact_digest=envelope.artifact_digest,
            predecessor_digest=envelope.predecessor_digest,
            revision=envelope.revision,
        ),
        facts=tuple(
            ProjectedSubjectFact(
                schema_id=fact.schema_id,
                schema_version=fact.schema_version,
                fact_key=fact.fact_key,
                value=fact.value,
            )
            for fact in facts
        ),
    )


class ProvisionalSubjectProjection:
    def __init__(
        self,
        *,
        coordinate: ProvisionalProjectionCoordinate,
        parsed: ParsedProjectionTree,
    ) -> None:
        self.coordinate = coordinate
        self._parsed = parsed

    def subject(self, identity: str) -> SubjectProjectionView | None:
        envelope = next(
            (
                row
                for row in self._parsed.envelopes
                if row.kind == "subject" and row.identity == identity
            ),
            None,
        )
        if envelope is None:
            return None
        facts = tuple(
            fact for fact in self._parsed.semantic_facts if fact.subject_identity == identity
        )
        return subject_projection_view(envelope, facts, coordinate=self.coordinate)

    def list_subjects(self) -> tuple[SubjectProjectionView, ...]:
        return tuple(
            view
            for identity in sorted(
                (row.identity for row in self._parsed.envelopes if row.kind == "subject"),
                key=lambda value: value.encode("utf-8"),
            )
            if (view := self.subject(identity)) is not None
        )


def compile_provisional_subject_projection(
    tree: Mapping[str, bytes],
    *,
    coordinate: ProvisionalProjectionCoordinate,
    registry: ProjectionExtensionRegistry | None = None,
) -> ProvisionalSubjectProjection:
    verify_provisional_tree(tree, coordinate=coordinate)
    parsed = parse_projection_tree(
        dict(tree),
        registry=registry or projection_registry_for_compiler(coordinate.canonical.compiler),
    )
    return ProvisionalSubjectProjection(coordinate=coordinate, parsed=parsed)


__all__ = [
    "ProjectedSubjectEnvelope",
    "ProjectedSubjectFact",
    "ProvisionalSubjectProjection",
    "SubjectProjectionView",
    "compile_provisional_subject_projection",
    "subject_projection_view",
]
