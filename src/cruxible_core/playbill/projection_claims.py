"""Coordinate-labeled Claim reads over canonical and provisional projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.errors import ProjectionCoordinateError
from cruxible_client.contracts.projection_extensions import (
    ProjectionExtensionRegistry,
    ProjectionFact,
)
from cruxible_core.playbill.compiler import (
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


class _StrictClaimProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectedClaimEnvelope(_StrictClaimProjectionModel):
    identity: str
    kind: Literal["claim"]
    format_tag: str
    path: str
    artifact_digest: str
    predecessor_digest: str | None
    revision: int


class ProjectedClaimFact(_StrictClaimProjectionModel):
    schema_id: str
    schema_version: int
    fact_key: str
    value: object


class ClaimProjectionView(_StrictClaimProjectionModel):
    tag: Literal["playbill-claim-projection-v1"] = "playbill-claim-projection-v1"
    coordinate_kind: Literal["canonical", "provisional"]
    coordinate: AcceptedProjectionCoordinate | ProvisionalProjectionCoordinate
    envelope: ProjectedClaimEnvelope
    facts: tuple[ProjectedClaimFact, ...]


def claim_projection_view(
    envelope: ArtifactEnvelopeRow,
    facts: Iterable[ProjectionFact],
    *,
    coordinate: AcceptedProjectionCoordinate | ProvisionalProjectionCoordinate,
) -> ClaimProjectionView:
    if envelope.kind != "claim":
        raise ProjectionCoordinateError("Claim query received a non-Claim envelope")
    return ClaimProjectionView(
        coordinate_kind=(
            "provisional"
            if isinstance(coordinate, ProvisionalProjectionCoordinate)
            else "canonical"
        ),
        coordinate=coordinate,
        envelope=ProjectedClaimEnvelope(
            identity=envelope.identity,
            kind="claim",
            format_tag=envelope.format_tag,
            path=envelope.path,
            artifact_digest=envelope.artifact_digest,
            predecessor_digest=envelope.predecessor_digest,
            revision=envelope.revision,
        ),
        facts=tuple(
            ProjectedClaimFact(
                schema_id=fact.schema_id,
                schema_version=fact.schema_version,
                fact_key=fact.fact_key,
                value=fact.value,
            )
            for fact in facts
        ),
    )


class ProvisionalClaimProjection:
    def __init__(
        self,
        *,
        coordinate: ProvisionalProjectionCoordinate,
        parsed: ParsedProjectionTree,
    ) -> None:
        self.coordinate = coordinate
        self._parsed = parsed

    def claim(self, identity: str) -> ClaimProjectionView | None:
        envelope = next(
            (
                row
                for row in self._parsed.envelopes
                if row.kind == "claim" and row.identity == identity
            ),
            None,
        )
        if envelope is None:
            return None
        facts = tuple(
            fact for fact in self._parsed.semantic_facts if fact.subject_identity == identity
        )
        return claim_projection_view(envelope, facts, coordinate=self.coordinate)

    def list_claims(self) -> tuple[ClaimProjectionView, ...]:
        return tuple(
            view
            for identity in sorted(
                (row.identity for row in self._parsed.envelopes if row.kind == "claim"),
                key=lambda value: value.encode("utf-8"),
            )
            if (view := self.claim(identity)) is not None
        )


def compile_provisional_claim_projection(
    tree: Mapping[str, bytes],
    *,
    coordinate: ProvisionalProjectionCoordinate,
    registry: ProjectionExtensionRegistry | None = None,
) -> ProvisionalClaimProjection:
    verify_provisional_tree(tree, coordinate=coordinate)
    parsed = parse_projection_tree(
        dict(tree),
        registry=registry or projection_registry_for_compiler(coordinate.canonical.compiler),
        artifact_kinds=artifact_kinds_for_compiler(coordinate.canonical.compiler),
    )
    return ProvisionalClaimProjection(coordinate=coordinate, parsed=parsed)


__all__ = [
    "ClaimProjectionView",
    "ProjectedClaimEnvelope",
    "ProjectedClaimFact",
    "ProvisionalClaimProjection",
    "claim_projection_view",
    "compile_provisional_claim_projection",
]
