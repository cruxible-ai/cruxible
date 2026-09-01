"""Coordinate-labeled Document reads over canonical and provisional projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.errors import ProjectionCoordinateError
from cruxible_client.contracts.projection_extensions import (
    ProjectionExtensionRegistry,
    ProjectionFact,
)
from cruxible_core.playbill.cas import BodyAccessContext, BodyProjectionProtocol
from cruxible_core.playbill.compiler import (
    artifact_codec_for_compiler,
    artifact_kinds_for_compiler,
    projection_registry_for_compiler,
)
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

_PROTECTED_DOCUMENT_SCHEMAS = frozenset({"playbill.document.source_mapping"})


class _StrictDocumentProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectedDocumentEnvelope(_StrictDocumentProjectionModel):
    identity: str
    kind: Literal["document"]
    format_tag: str
    path: str
    artifact_digest: str
    predecessor_digest: str | None
    revision: int


class ProjectedDocumentFact(_StrictDocumentProjectionModel):
    schema_id: str
    schema_version: int
    fact_key: str
    value: object


class DocumentProjectionView(_StrictDocumentProjectionModel):
    """A deterministic Document view that always discloses its read coordinate."""

    tag: Literal["playbill-document-projection-v1"] = "playbill-document-projection-v1"
    coordinate_kind: Literal["canonical", "provisional"]
    coordinate: AcceptedProjectionCoordinate | ProvisionalProjectionCoordinate
    envelope: ProjectedDocumentEnvelope
    facts: tuple[ProjectedDocumentFact, ...]


def document_projection_view(
    envelope: ArtifactEnvelopeRow,
    facts: Iterable[ProjectionFact],
    *,
    coordinate: AcceptedProjectionCoordinate | ProvisionalProjectionCoordinate,
    access: BodyAccessContext,
) -> DocumentProjectionView:
    """Apply the protected-body metadata boundary to normalized compiler facts."""

    if envelope.kind != "document":
        raise ProjectionCoordinateError("Document query received a non-Document envelope")
    visible = tuple(
        ProjectedDocumentFact(
            schema_id=fact.schema_id,
            schema_version=fact.schema_version,
            fact_key=fact.fact_key,
            value=fact.value,
        )
        for fact in facts
        if access.can_read_body or fact.schema_id not in _PROTECTED_DOCUMENT_SCHEMAS
    )
    return DocumentProjectionView(
        coordinate_kind=(
            "provisional"
            if isinstance(coordinate, ProvisionalProjectionCoordinate)
            else "canonical"
        ),
        coordinate=coordinate,
        envelope=ProjectedDocumentEnvelope(
            identity=envelope.identity,
            kind="document",
            format_tag=envelope.format_tag,
            path=envelope.path,
            artifact_digest=envelope.artifact_digest,
            predecessor_digest=envelope.predecessor_digest,
            revision=envelope.revision,
        ),
        facts=visible,
    )


class ProvisionalDocumentProjection:
    """In-memory candidate projection; it cannot be bound through a canonical API."""

    def __init__(
        self,
        *,
        coordinate: ProvisionalProjectionCoordinate,
        parsed: ParsedProjectionTree,
    ) -> None:
        self.coordinate = coordinate
        self._parsed = parsed

    def document(
        self,
        identity: str,
        *,
        access: BodyAccessContext,
    ) -> DocumentProjectionView | None:
        envelope = next(
            (
                row
                for row in self._parsed.envelopes
                if row.kind == "document" and row.identity == identity
            ),
            None,
        )
        if envelope is None:
            return None
        facts = tuple(
            fact for fact in self._parsed.semantic_facts if fact.subject_identity == identity
        )
        return document_projection_view(
            envelope,
            facts,
            coordinate=self.coordinate,
            access=access,
        )

    def list_documents(
        self,
        *,
        access: BodyAccessContext,
    ) -> tuple[DocumentProjectionView, ...]:
        return tuple(
            view
            for identity in sorted(
                (row.identity for row in self._parsed.envelopes if row.kind == "document"),
                key=lambda value: value.encode("utf-8"),
            )
            if (view := self.document(identity, access=access)) is not None
        )


def compile_provisional_document_projection(
    tree: Mapping[str, bytes],
    *,
    coordinate: ProvisionalProjectionCoordinate,
    bodies: BodyProjectionProtocol,
    registry: ProjectionExtensionRegistry | None = None,
) -> ProvisionalDocumentProjection:
    """Compile an exact candidate projection without publishing serving state."""

    verify_provisional_tree(tree, coordinate=coordinate)
    parsed = parse_projection_tree(
        dict(tree),
        registry=registry or projection_registry_for_compiler(coordinate.canonical.compiler),
        artifact_kinds=artifact_kinds_for_compiler(coordinate.canonical.compiler),
        artifact_codec=artifact_codec_for_compiler(coordinate.canonical.compiler),
        bodies=bodies,
    )
    return ProvisionalDocumentProjection(coordinate=coordinate, parsed=parsed)


__all__ = [
    "DocumentProjectionView",
    "ProjectedDocumentEnvelope",
    "ProjectedDocumentFact",
    "ProvisionalDocumentProjection",
    "compile_provisional_document_projection",
    "document_projection_view",
]
