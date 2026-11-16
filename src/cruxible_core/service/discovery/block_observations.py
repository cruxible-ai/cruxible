"""Declared projection blocks a client read on the pages it touched (`block sync`, `repin`).

The daemon never reads the workspace, so a client sends what it read of each
page; the observations and each scan's accounting are retained in the
review-operational store. The work is the pages sent, never the workspace.
Split out of the curation service so served code imports no curation surface.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, parse_artifact_identity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.change_control import DryRun, PreviewAt
from cruxible_client.contracts.declared_blocks import (
    DeclaredBlockPageObservation,
    ProjectionMarkerSummary,
)
from cruxible_client.contracts.documents import document_path, parse_document
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.validation_messages import validation_summary
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import change_entry, change_scope
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext


class _StrictBlockModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


BLOCK_OBSERVATION_ID_DOMAIN = "playbill-block-observation-v1"


BLOCK_SCAN_ID_DOMAIN = "playbill-block-scan-v1"


PlaybillCurationObservationOmissionReason: TypeAlias = Literal[
    "block_subject_unresolved",
    "marker_coordinate_unaccepted",
    "projection_block_unstamped",
    "projection_marker_invalid",
]


class BlockObservationInvalid(CruxibleError):
    code = "cruxible.block.observation_invalid"

    @property
    def error_code(self) -> str:
        return self.code


#: The most pages one block observation records; `block sync --all` sends its pages in
#: batches of this size.
BLOCK_OBSERVE_MAX_PAGES = 256


class PlaybillBlockObserveRequestV1(_StrictBlockModel):
    """Record the declared blocks a client read on the pages it touched, for block churn."""

    tag: Literal["playbill-block-observe-request-v1"] = "playbill-block-observe-request-v1"
    pages: tuple[DeclaredBlockPageObservation, ...] = Field(
        min_length=1, max_length=BLOCK_OBSERVE_MAX_PAGES
    )
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("pages")
    @classmethod
    def _pages(
        cls, value: tuple[DeclaredBlockPageObservation, ...]
    ) -> tuple[DeclaredBlockPageObservation, ...]:
        ids = tuple(page.source_id for page in value)
        if len(set(ids)) != len(ids):
            raise ValueError("a block observation names each page once")
        return value


def validate_playbill_block_observe_request(
    value: PlaybillBlockObserveRequestV1 | Mapping[str, object],
) -> PlaybillBlockObserveRequestV1:
    if isinstance(value, PlaybillBlockObserveRequestV1):
        return value
    try:
        return PlaybillBlockObserveRequestV1.model_validate(value)
    except ValidationError as exc:
        raise BlockObservationInvalid(
            f"{BlockObservationInvalid.code}: {validation_summary(exc)}"
        ) from exc


class BlockObservationV1(_StrictBlockModel):
    tag: Literal["playbill-block-observation-v1"] = "playbill-block-observation-v1"
    event_id: str
    observation_id: str
    observation_basis: Literal["client_observed"] = "client_observed"
    document_identity: ArtifactIdentity
    source_id: str
    block_id: str
    marker_summary: ProjectionMarkerSummary
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


class PlaybillCurationCoverageCountV1(_StrictBlockModel):
    reason: PlaybillCurationObservationOmissionReason
    count: int = Field(ge=0)


class PlaybillCurationObservationCoverageV1(_StrictBlockModel):
    tag: Literal["playbill-curation-observation-coverage-v1"] = (
        "playbill-curation-observation-coverage-v1"
    )
    source_count: int = Field(ge=0)
    observed_block_count: int = Field(ge=0)
    omitted_source_count: int = Field(ge=0)
    omissions: tuple[PlaybillCurationCoverageCountV1, ...]


class BlockScanV1(_StrictBlockModel):
    """One client workspace scan's accounting, kept beside the block observations it made.

    Block-churn detection reads the latest scan's unresolved-association count,
    since a block it could not tie to a Document is a churn it cannot see.
    """

    tag: Literal["playbill-block-scan-v1"] = "playbill-block-scan-v1"
    event_id: str
    scan_coordinate: AcceptedCoordinate
    scan_generation: int = Field(ge=0)
    coverage: PlaybillCurationObservationCoverageV1
    actor_principal_id: str

    @model_validator(mode="after")
    def _reproduces(self) -> BlockScanV1:
        if self.event_id != _block_scan_id(self):
            raise ValueError("block scan event ID does not reproduce")
        return self


def _block_scan_id(scan: BlockScanV1) -> str:
    payload = scan.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("event_id")
    return typed_digest(Sha256Value, BLOCK_SCAN_ID_DOMAIN, payload).tagged


class PlaybillBlockObserveResultV1(_StrictBlockModel):
    tag: Literal["playbill-block-observe-result-v1"] = "playbill-block-observe-result-v1"
    #: ``would_record`` answers a preview: the pages were checked, nothing appended.
    status: Literal["recorded", "would_record"] = "recorded"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    observation_coverage: PlaybillCurationObservationCoverageV1


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
    source: DeclaredBlockPageObservation,
    marker: ProjectionMarkerSummary,
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
    # One indexed lookup, not a walk of the whole accepted history.
    with instance.accepted_history_reader() as history:
        location = history.generation_for_oid(coordinate.git_oid)
    if location is None:
        raise BlockObservationInvalid(
            f"{BlockObservationInvalid.code}: block observations need the current "
            "replay-verified accepted coordinate"
        )
    return location.sequence


def _valid_document_identity(
    tree: Mapping[str, bytes], document_id: str
) -> ArtifactIdentity | None:
    path = document_path(document_id)
    content = tree.get(path)
    if content is None:
        return None
    document = parse_document(content, path=path)
    identity = parse_artifact_identity(document.identity)
    if identity.name != document_id:
        return None
    return identity


def _record_block_observations(
    instance: PlaybillInstance,
    *,
    pages: tuple[DeclaredBlockPageObservation, ...],
    actor_context: GovernedActorContext,
    previewing: bool = False,
) -> PlaybillCurationObservationCoverageV1:
    accepted = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(accepted)
    generation = _generation(instance, coordinate)
    tree = ClaimVerdictReadContext(instance, accepted).tree
    counts: Counter[PlaybillCurationObservationOmissionReason] = Counter()
    observed = 0
    for source in pages:
        if source.document_id is None:
            counts["block_subject_unresolved"] += 1
            continue
        document_identity = _valid_document_identity(tree, source.document_id)
        if document_identity is None:
            counts["block_subject_unresolved"] += 1
            continue
        for note in source.marker_notes:
            counts[note] += 1
        for marker in source.marker_summaries:
            try:
                instance.resolve_accepted_coordinate(
                    git_oid=marker.stamp.declared_coordinate.git_oid,
                    semantic_root=marker.stamp.declared_coordinate.semantic_root,
                    generation_root=marker.stamp.declared_coordinate.generation_root,
                    compiler_digest=marker.stamp.declared_coordinate.compiler_digest,
                )
            except CruxibleError:
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
            if previewing:
                observed += 1
                continue
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

    return PlaybillCurationObservationCoverageV1(
        source_count=len(pages),
        observed_block_count=observed,
        omitted_source_count=sum(counts.values()),
        omissions=tuple(
            PlaybillCurationCoverageCountV1(reason=reason, count=counts[reason])
            for reason in sorted(counts, key=lambda item: item.encode("utf-8"))
        ),
    )


def service_observe_playbill_blocks(
    instance: PlaybillInstance,
    *,
    request: PlaybillBlockObserveRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillBlockObserveResultV1:
    """Record the declared blocks a client read on the pages it touched, and their accounting.

    Block churn is the one detector that needs the workspace, which the daemon
    never reads, so `block sync` and `block repin` send what they read of each
    page they touched; detection reads it when it next runs. The work is the
    pages sent, never the workspace.
    """

    instance.require_writable()
    with change_entry(request.dry_run, "direct"):
        coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        generation = _generation(instance, coordinate)
        with change_scope(
            instance,
            dry_run=request.dry_run,
            at=request.at,
            kind="direct",
            operation="cruxible.block.observe",
            describe="recording the declared blocks of the pages a client read",
        ) as mode:
            if mode.previewing:
                coverage = _record_block_observations(
                    instance,
                    pages=request.pages,
                    actor_context=actor_context,
                    previewing=True,
                )
                return PlaybillBlockObserveResultV1(
                    status="would_record",
                    coordinate=coordinate,
                    generation=generation,
                    observation_coverage=coverage,
                )
            with mode.committing():
                coverage = _record_block_observations(
                    instance,
                    pages=request.pages,
                    actor_context=actor_context,
                )
                placeholder = BlockScanV1.model_construct(
                    tag="playbill-block-scan-v1",
                    event_id="sha256:" + "0" * 64,
                    scan_coordinate=coordinate,
                    scan_generation=generation,
                    coverage=coverage,
                    actor_principal_id=actor_context.actor_id,
                )
                scan = BlockScanV1(
                    event_id=_block_scan_id(placeholder),
                    scan_coordinate=coordinate,
                    scan_generation=generation,
                    coverage=coverage,
                    actor_principal_id=actor_context.actor_id,
                )
                instance.review_operational_store().append(
                    family="block_scan",
                    partition_id="workspace",
                    event_id=scan.event_id,
                    payload=scan,
                    coordinate=coordinate,
                    generation=generation,
                    actor_context=actor_context,
                    recorded_at=actor_context.timestamp,
                )
        return PlaybillBlockObserveResultV1(
            coordinate=coordinate, generation=generation, observation_coverage=coverage
        )
