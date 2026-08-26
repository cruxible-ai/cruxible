"""G9 curation-list foundation and attributed block observations."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, parse_artifact_identity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.declared_blocks import ProjectionMarkerSummaryV1
from cruxible_client.contracts.documents import document_path, parse_document
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.service.playbill_next import (
    PlaybillNextSourceObservationV3,
    PlaybillNextWorkspaceObservationV1,
)

BLOCK_OBSERVATION_ID_DOMAIN = "playbill-block-observation-v1"


class PlaybillCurationError(PlaybillError):
    code = "playbill.curation.refused"


class PlaybillCurationCoordinateNotAccepted(PlaybillCurationError):
    code = "playbill.curation.coordinate_not_accepted"


class _StrictCurationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillCurationListRequestV1(_StrictCurationModel):
    tag: Literal["playbill-curation-list-request-v1"] = "playbill-curation-list-request-v1"
    workspace_observation: PlaybillNextWorkspaceObservationV1 | None = None


class BlockObservationV1(_StrictCurationModel):
    tag: Literal["playbill-block-observation-v1"] = "playbill-block-observation-v1"
    event_id: str
    observation_id: str
    observation_basis: Literal["client_observed"] = "client_observed"
    document_identity: ArtifactIdentity
    source_id: str
    block_id: str
    marker_summary: ProjectionMarkerSummaryV1
    request_source_digest: str
    scan_coordinate: AcceptedCoordinate
    scan_generation: int = Field(ge=0)
    actor_principal_id: str

    @field_validator("event_id", "observation_id", "request_source_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reproduces(self) -> BlockObservationV1:
        if self.event_id != self.observation_id:
            raise ValueError("block event and observation identities differ")
        if self.source_id != self.marker_summary.stamp.source_id:
            raise ValueError("block observation source differs from its marker")
        if self.block_id != self.marker_summary.stamp.block_id:
            raise ValueError("block observation identity differs from its marker")
        if self.observation_id != block_observation_id(self):
            raise ValueError("block observation ID does not reproduce")
        return self


class PlaybillCurationCoverageCountV1(_StrictCurationModel):
    reason: str
    count: int = Field(ge=0)


class PlaybillCurationObservationCoverageV1(_StrictCurationModel):
    tag: Literal["playbill-curation-observation-coverage-v1"] = (
        "playbill-curation-observation-coverage-v1"
    )
    source_count: int = Field(ge=0)
    observed_block_count: int = Field(ge=0)
    omitted_source_count: int = Field(ge=0)
    omissions: tuple[PlaybillCurationCoverageCountV1, ...]


class PlaybillCurationListResultV1(_StrictCurationModel):
    tag: Literal["playbill-curation-list-result-v1"] = "playbill-curation-list-result-v1"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    operational_head_digest: str
    items: tuple[object, ...] = ()
    observation_coverage: PlaybillCurationObservationCoverageV1

    @field_validator("operational_head_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


def block_observation_id(observation: BlockObservationV1) -> str:
    return typed_digest(
        Sha256Value,
        BLOCK_OBSERVATION_ID_DOMAIN,
        {
            "document_identity": observation.document_identity.model_dump(mode="json"),
            "source_id": observation.source_id,
            "block_id": observation.block_id,
            "marker_summary": observation.marker_summary.model_dump(mode="json"),
            "request_source_digest": observation.request_source_digest,
            "scan_coordinate": observation.scan_coordinate.model_dump(mode="json"),
            "scan_generation": observation.scan_generation,
            "actor_principal_id": observation.actor_principal_id,
        },
    ).tagged


def build_block_observation(
    *,
    document_identity: ArtifactIdentity,
    source: PlaybillNextSourceObservationV3,
    marker: ProjectionMarkerSummaryV1,
    scan_coordinate: AcceptedCoordinate,
    scan_generation: int,
    actor_context: GovernedActorContext,
) -> BlockObservationV1:
    placeholder = "sha256:" + "0" * 64
    draft = BlockObservationV1.model_construct(
        tag="playbill-block-observation-v1",
        event_id=placeholder,
        observation_id=placeholder,
        observation_basis="client_observed",
        document_identity=document_identity,
        source_id=source.source_id,
        block_id=marker.stamp.block_id,
        marker_summary=marker,
        request_source_digest=source.observed_source_digest,
        scan_coordinate=scan_coordinate,
        scan_generation=scan_generation,
        actor_principal_id=actor_context.actor_id,
    )
    identity = block_observation_id(draft)
    return BlockObservationV1(
        event_id=identity,
        observation_id=identity,
        document_identity=document_identity,
        source_id=source.source_id,
        block_id=marker.stamp.block_id,
        marker_summary=marker,
        request_source_digest=source.observed_source_digest,
        scan_coordinate=scan_coordinate,
        scan_generation=scan_generation,
        actor_principal_id=actor_context.actor_id,
    )


def _generation(instance: PlaybillInstance, coordinate: AcceptedCoordinate) -> int:
    matches = tuple(
        item.sequence for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if len(matches) != 1:
        raise PlaybillCurationCoordinateNotAccepted(
            "curation requires the current replay-verified accepted coordinate"
        )
    return matches[0]


def _valid_document_identity(tree: dict[str, bytes], document_id: str) -> ArtifactIdentity | None:
    path = document_path(document_id)
    content = tree.get(path)
    if content is None:
        return None
    document = parse_document(content, path=path)
    identity = parse_artifact_identity(document.identity)
    if identity.name != document_id:
        return None
    return identity


def service_list_playbill_curation(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationListRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationListResultV1:
    """Append explicit client block observations and return the empty G9a queue."""

    internal_coordinate = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(internal_coordinate)
    generation = _generation(instance, coordinate)
    tree = instance.tree_at(coordinate.git_oid)
    counts: Counter[str] = Counter()
    source_count = 0
    observed = 0
    observation = request.workspace_observation
    sources = () if observation is None else observation.source_observations
    if sources is not None:
        for source in sources:
            source_count += 1
            if not isinstance(source, PlaybillNextSourceObservationV3):
                counts["source_observation_not_v3"] += 1
                continue
            if not source.scan_complete:
                counts["source_scan_incomplete"] += 1
                continue
            if source.document_id is None:
                counts["block_subject_unresolved"] += 1
                continue
            document_identity = _valid_document_identity(tree, source.document_id)
            if document_identity is None:
                counts["block_subject_unresolved"] += 1
                continue
            for note in source.marker_notes:
                if note == "projection_block_unstamped":
                    counts["projection_block_unstamped"] += 1
                elif note == "projection_marker_invalid":
                    counts["projection_marker_invalid"] += 1
            for marker in source.marker_summaries:
                try:
                    instance.resolve_accepted_coordinate(
                        git_oid=marker.stamp.declared_coordinate.git_oid,
                        semantic_root=marker.stamp.declared_coordinate.semantic_root,
                        generation_root=marker.stamp.declared_coordinate.generation_root,
                        compiler_digest=marker.stamp.declared_coordinate.compiler_digest,
                    )
                except PlaybillError:
                    counts["marker_coordinate_unaccepted"] += 1
                    continue
                block = build_block_observation(
                    document_identity=document_identity,
                    source=source,
                    marker=marker,
                    scan_coordinate=coordinate,
                    scan_generation=generation,
                    actor_context=actor_context,
                )
                instance.review_operational_store().append(
                    family="block_observation",
                    partition_id=(
                        f"{document_identity.qualified}/{source.source_id}/{marker.stamp.block_id}"
                    ),
                    event_id=block.observation_id,
                    payload=block,
                    coordinate=coordinate,
                    generation=generation,
                    actor_context=actor_context,
                    recorded_at=actor_context.timestamp,
                )
                observed += 1

    head = instance.review_operational_store().head()
    return PlaybillCurationListResultV1(
        coordinate=coordinate,
        generation=generation,
        operational_head_digest=head.head_digest,
        items=(),
        observation_coverage=PlaybillCurationObservationCoverageV1(
            source_count=source_count,
            observed_block_count=observed,
            omitted_source_count=sum(counts.values()),
            omissions=tuple(
                PlaybillCurationCoverageCountV1(reason=reason, count=counts[reason])
                for reason in sorted(counts, key=lambda item: item.encode("utf-8"))
            ),
        ),
    )


__all__ = [
    "BLOCK_OBSERVATION_ID_DOMAIN",
    "BlockObservationV1",
    "PlaybillCurationCoverageCountV1",
    "PlaybillCurationError",
    "PlaybillCurationListRequestV1",
    "PlaybillCurationListResultV1",
    "PlaybillCurationObservationCoverageV1",
    "block_observation_id",
    "build_block_observation",
    "service_list_playbill_curation",
]
