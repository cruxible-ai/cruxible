"""G9 curation-list foundation and attributed block observations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, parse_artifact_identity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.declared_blocks import ProjectionMarkerSummaryV1
from cruxible_client.contracts.documents import document_path, parse_document
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.closure import dependency_artifacts, parse_dependency_artifact
from cruxible_core.playbill.consumption import ensure_consumption_epoch
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.curation import (
    CurationAffectedMemberV1,
    CurationDetectorCoverageV1,
    CurationItemV1,
    build_curation_accepted_fixed,
    build_curation_overruled,
    build_curation_suppressed,
    build_pattern_observation,
    replay_curation_items,
)
from cruxible_core.playbill.curation_detectors import run_curation_detectors
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.review_operational import (
    ReviewOperationalConcurrentChangeError,
    ReviewOperationalStoreError,
)
from cruxible_core.playbill.settlement import ChangeSetRecordAnyVersion
from cruxible_core.service.playbill_next import (
    PlaybillNextAccessProfileInvalid,
    PlaybillNextSourceObservationV3,
    PlaybillNextWorkspaceObservationInvalid,
    PlaybillNextWorkspaceObservationV1,
)

BLOCK_OBSERVATION_ID_DOMAIN = "playbill-block-observation-v1"
CURATION_RESULT_DIGEST_DOMAIN = "playbill-curation-list-result-v1"

PlaybillCurationObservationOmissionReason: TypeAlias = Literal[
    "block_subject_unresolved",
    "marker_coordinate_unaccepted",
    "projection_block_unstamped",
    "projection_marker_invalid",
    "source_observation_not_v3",
    "source_scan_incomplete",
]


class PlaybillCurationError(PlaybillError):
    code = "playbill.curation.refused"

    @property
    def error_code(self) -> str:
        return self.code


class PlaybillCurationCoordinateNotAccepted(PlaybillCurationError):
    code = "playbill.curation.coordinate_not_accepted"


class PlaybillCurationItemNotFound(PlaybillCurationError):
    code = "playbill.curation.item_not_found"


class PlaybillCurationItemAlreadyResolved(PlaybillCurationError):
    code = "playbill.curation.item_already_resolved"


class PlaybillCurationSuppressionInvalid(PlaybillCurationError):
    code = "playbill.curation.suppression_invalid"


class PlaybillCurationResolvingProposalInvalid(PlaybillCurationError):
    code = "playbill.curation.resolving_proposal_invalid"


class PlaybillCurationResolvingChangeUnrelated(PlaybillCurationError):
    code = "playbill.curation.resolving_change_unrelated"


class _StrictCurationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillCurationListRequestV1(_StrictCurationModel):
    tag: Literal["playbill-curation-list-request-v1"] = "playbill-curation-list-request-v1"
    evaluation_time: datetime
    access_profile: CoverageAccessProfileV1
    workspace_observation: PlaybillNextWorkspaceObservationV1 | None = None

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def validate_playbill_curation_list_request(
    value: PlaybillCurationListRequestV1 | Mapping[str, object],
) -> PlaybillCurationListRequestV1:
    if isinstance(value, PlaybillCurationListRequestV1):
        return value
    try:
        return PlaybillCurationListRequestV1.model_validate(value)
    except ValidationError as exc:
        roots = {str(item["loc"][0]) for item in exc.errors() if item["loc"]}
        if "access_profile" in roots:
            raise PlaybillNextAccessProfileInvalid(
                f"{PlaybillNextAccessProfileInvalid.code}: {exc}"
            ) from exc
        if "workspace_observation" in roots:
            raise PlaybillNextWorkspaceObservationInvalid(
                f"{PlaybillNextWorkspaceObservationInvalid.code}: {exc}"
            ) from exc
        raise PlaybillCurationError(f"{PlaybillCurationError.code}: {exc}") from exc


class PlaybillCurationOverruleRequestV1(_StrictCurationModel):
    tag: Literal["playbill-curation-overrule-request-v1"] = "playbill-curation-overrule-request-v1"
    item_id: str
    expected_latest_event_digest: str
    reason: str = Field(min_length=1)
    attribution_refs: tuple[str, ...] = ()

    @field_validator("item_id", "expected_latest_event_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class PlaybillCurationAcceptFixedRequestV1(_StrictCurationModel):
    tag: Literal["playbill-curation-accept-fixed-request-v1"] = (
        "playbill-curation-accept-fixed-request-v1"
    )
    item_id: str
    expected_latest_event_digest: str
    reason: str = Field(min_length=1)
    accepted_proposal_id: str
    accepted_changeset_digest: str
    attribution_refs: tuple[str, ...] = ()

    @field_validator(
        "item_id",
        "expected_latest_event_digest",
        "accepted_proposal_id",
        "accepted_changeset_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class PlaybillCurationSuppressRequestV1(_StrictCurationModel):
    tag: Literal["playbill-curation-suppress-request-v1"] = "playbill-curation-suppress-request-v1"
    item_id: str
    expected_latest_event_digest: str
    reason: str = Field(min_length=1)
    scope: Literal["item", "pattern", "instance"]
    until_generation: int | None = Field(default=None, ge=0)
    attribution_refs: tuple[str, ...] = ()

    @field_validator("item_id", "expected_latest_event_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


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
    reason: PlaybillCurationObservationOmissionReason
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
    evaluation_time: datetime
    operational_head_digest: str
    items: tuple[CurationItemV1, ...] = ()
    detector_coverage: tuple[CurationDetectorCoverageV1, ...]
    observation_coverage: PlaybillCurationObservationCoverageV1
    result_digest: str

    @field_validator("operational_head_digest", "result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _reproduces(self) -> PlaybillCurationListResultV1:
        if self.result_digest != curation_list_result_digest(self):
            raise ValueError("curation list result digest does not reproduce")
        return self


class PlaybillCurationActionResultV1(_StrictCurationModel):
    tag: Literal["playbill-curation-action-result-v1"] = "playbill-curation-action-result-v1"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    operational_head_digest: str
    item: CurationItemV1

    @field_validator("operational_head_digest")
    @classmethod
    def _head_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


def curation_list_result_digest(result: PlaybillCurationListResultV1) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("result_digest")
    return typed_digest(Sha256Value, CURATION_RESULT_DIGEST_DOMAIN, payload).tagged


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


def _record_block_observations(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationListRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationObservationCoverageV1:
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    generation = _generation(instance, coordinate)
    tree = instance.tree_at(coordinate.git_oid)
    counts: Counter[PlaybillCurationObservationOmissionReason] = Counter()
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

    return PlaybillCurationObservationCoverageV1(
        source_count=source_count,
        observed_block_count=observed,
        omitted_source_count=sum(counts.values()),
        omissions=tuple(
            PlaybillCurationCoverageCountV1(reason=reason, count=counts[reason])
            for reason in sorted(counts, key=lambda item: item.encode("utf-8"))
        ),
    )


def _replay_items(instance: PlaybillInstance) -> tuple[CurationItemV1, ...]:
    try:
        return replay_curation_items(instance.review_operational_store().events(family="curation"))
    except ValueError as exc:
        raise ReviewOperationalStoreError("curation event replay is invalid") from exc


def _accepted_retirements_for_items(
    instance: PlaybillInstance,
    items: tuple[CurationItemV1, ...],
) -> dict[
    str,
    tuple[int, str, ChangeSetRecordAnyVersion, tuple[CurationAffectedMemberV1, ...]],
]:
    """Find first exact retirements in one bounded history/tree traversal."""

    history = instance.accepted_history()
    evaluations_by_candidate: dict[str, list[str]] = {}
    for evaluation in instance.proposal_evidence().list_evaluations():
        if evaluation.verdict == "candidate" and evaluation.candidate_digest is not None:
            evaluations_by_candidate.setdefault(evaluation.candidate_digest, []).append(
                evaluation.proposal_id
            )
    unresolved = {item.item_id: item for item in items}
    resolved: dict[
        str,
        tuple[int, str, ChangeSetRecordAnyVersion, tuple[CurationAffectedMemberV1, ...]],
    ] = {}
    for index, accepted in enumerate(history[1:], start=1):
        if not unresolved or accepted.record is None:
            continue
        eligible = tuple(
            item
            for item in unresolved.values()
            # An operational item is created after its accepted coordinate is
            # observed, so a same-generation change cannot have fixed it.
            if accepted.sequence > item.first_proposed_generation
        )
        if not eligible:
            continue
        proposal_ids = tuple(
            sorted(
                set(evaluations_by_candidate.get(accepted.record.candidate_digest, ())),
                key=lambda value: value.encode("ascii"),
            )
        )
        if not proposal_ids:
            # Imported ledgers can preserve the accepted receipt without local
            # proposal exhaust.  Do not invent a resolving proposal identity.
            continue
        parent_tree = instance.tree_at(history[index - 1].oid)
        candidate_tree = instance.tree_at(accepted.oid)
        affected = _affected_members(
            accepted.record,
            parent_tree=parent_tree,
            candidate_tree=candidate_tree,
        )
        retired_paths = {member.path for member in affected if member.disposition == "retire"}
        if not retired_paths:
            continue
        parent_paths: dict[ArtifactIdentity, set[str]] = {}
        candidate_paths: dict[ArtifactIdentity, set[str]] = {}
        for state in dependency_artifacts(parent_tree):
            parent_paths.setdefault(state.identity, set()).add(state.path)
        for state in dependency_artifacts(candidate_tree):
            candidate_paths.setdefault(state.identity, set()).add(state.path)
        for item in eligible:
            related = {ref.path for ref in item.latest_evidence_refs if ref.path is not None}
            related.update(parent_paths.get(item.subject, ()))
            related.update(candidate_paths.get(item.subject, ()))
            if retired_paths.isdisjoint(related):
                continue
            resolved[item.item_id] = (
                accepted.sequence,
                proposal_ids[0],
                accepted.record,
                affected,
            )
            unresolved.pop(item.item_id)
    return resolved


def _auto_resolve_retired_dead_vocabulary(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
    generation: int,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
) -> None:
    """Close dead-vocabulary rows whose exact artifact was retired by succession."""

    store = instance.review_operational_store()
    candidates = tuple(
        item
        for item in _replay_items(instance)
        if item.status == "open" and item.pattern_kind == "playbill.curation.dead_vocabulary.v1"
    )
    resolutions = _accepted_retirements_for_items(instance, candidates)
    for candidate in candidates:
        resolution = resolutions.get(candidate.item_id)
        if resolution is None:
            continue
        resolved_generation, proposal_id, record, affected = resolution
        current = candidate
        for attempt in range(2):
            payload = build_curation_accepted_fixed(
                item_id=current.item_id,
                expected_latest_event_digest=current.latest_event_digest,
                actor_principal_id=record.actor_binding.actor_id,
                reason="accepted change retired the artifact",
                accepted_proposal_id=proposal_id,
                accepted_changeset_digest=record.changeset_digest,
                resolved_generation=resolved_generation,
                affected_members=affected,
            )
            try:
                store.append(
                    family="curation",
                    partition_id=current.item_id,
                    event_id=payload.event_id,
                    payload=payload,
                    coordinate=coordinate,
                    generation=generation,
                    actor_context=actor_context,
                    recorded_at=recorded_at,
                    expected_latest_event_digest=current.latest_event_digest,
                )
                break
            except ReviewOperationalConcurrentChangeError:
                if attempt == 1:
                    raise
                refreshed = next(
                    (item for item in _replay_items(instance) if item.item_id == current.item_id),
                    None,
                )
                if refreshed is None or refreshed.status != "open":
                    break
                current = refreshed


def service_list_playbill_curation(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationListRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationListResultV1:
    """Refresh mechanical detections and return the visible current queue."""

    internal_coordinate = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(internal_coordinate)
    generation = _generation(instance, coordinate)
    store = instance.review_operational_store()
    # G9 visibility note: all present curation facts are instance-class.  Until
    # sub-instance ACLs exist the access decision is intentionally binary and
    # per-item filtering would be vacuous rather than an additional guarantee.
    if not request.access_profile.permits("instance"):
        head = store.head()
        observation_coverage = PlaybillCurationObservationCoverageV1(
            source_count=0,
            observed_block_count=0,
            omitted_source_count=0,
            omissions=(),
        )
        provisional = PlaybillCurationListResultV1.model_construct(
            tag="playbill-curation-list-result-v1",
            coordinate=coordinate,
            generation=generation,
            evaluation_time=request.evaluation_time,
            operational_head_digest=head.head_digest,
            items=(),
            detector_coverage=(),
            observation_coverage=observation_coverage,
            result_digest="sha256:" + "0" * 64,
        )
        return PlaybillCurationListResultV1(
            coordinate=coordinate,
            generation=generation,
            evaluation_time=request.evaluation_time,
            operational_head_digest=head.head_digest,
            items=(),
            detector_coverage=(),
            observation_coverage=observation_coverage,
            result_digest=curation_list_result_digest(provisional),
        )
    observation_coverage = _record_block_observations(
        instance, request=request, actor_context=actor_context
    )
    ensure_consumption_epoch(
        instance,
        coordinate=coordinate,
        generation=generation,
        actor_context=actor_context,
    )
    detector_input_head = store.head().head_digest
    block_association_omissions = next(
        (
            item.count
            for item in observation_coverage.omissions
            if item.reason == "block_subject_unresolved"
        ),
        0,
    )
    detected = run_curation_detectors(
        instance,
        coordinate=coordinate,
        generation=generation,
        evaluation_time=request.evaluation_time,
        operational_head_digest=detector_input_head,
        block_document_association_omissions=block_association_omissions,
    )
    existing = _replay_items(instance)
    by_pattern: dict[str, list[CurationItemV1]] = {}
    for item in existing:
        by_pattern.setdefault(item.pattern_id, []).append(item)
    for detection in detected.detections:
        lineage = sorted(
            by_pattern.get(detection.pattern_id, []),
            key=lambda item: (item.first_proposed_generation, item.item_id),
        )
        current = None if not lineage else lineage[-1]
        starts_successor = current is not None and current.status != "open"
        predecessor = (
            current.item_id
            if current is not None and starts_successor
            else (None if current is None else current.predecessor_item_id)
        )
        observation = build_pattern_observation(
            detection=detection,
            predecessor_item_id=predecessor,
            accepted_generation=generation,
        )
        expected = None if current is None or starts_successor else (current.latest_event_digest)
        for attempt in range(2):
            try:
                store.append(
                    family="curation",
                    partition_id=observation.item_id,
                    event_id=observation.event_id,
                    payload=observation,
                    coordinate=coordinate,
                    generation=generation,
                    actor_context=actor_context,
                    recorded_at=request.evaluation_time,
                    expected_latest_event_digest=expected,
                )
                break
            except ReviewOperationalConcurrentChangeError:
                if attempt == 1:
                    raise
                refreshed = tuple(
                    sorted(
                        (
                            item
                            for item in _replay_items(instance)
                            if item.pattern_id == detection.pattern_id
                        ),
                        key=lambda item: (item.first_proposed_generation, item.item_id),
                    )
                )
                current = None if not refreshed else refreshed[-1]
                starts_successor = current is not None and current.status != "open"
                predecessor = (
                    current.item_id
                    if current is not None and starts_successor
                    else (None if current is None else current.predecessor_item_id)
                )
                observation = build_pattern_observation(
                    detection=detection,
                    predecessor_item_id=predecessor,
                    accepted_generation=generation,
                )
                expected = (
                    None if current is None or starts_successor else current.latest_event_digest
                )
    _auto_resolve_retired_dead_vocabulary(
        instance,
        coordinate=coordinate,
        generation=generation,
        actor_context=actor_context,
        recorded_at=request.evaluation_time,
    )
    all_items = _replay_items(instance)
    items = tuple(
        sorted(
            (
                item
                for item in all_items
                if item.status in {"open", "quarantined"}
                and not item.suppressed_at(generation, all_items=all_items)
            ),
            key=lambda item: (
                item.pattern_kind.encode("ascii"),
                item.subject.qualified.encode("utf-8"),
                item.item_id.encode("ascii"),
            ),
        )
    )
    head = store.head()
    provisional = PlaybillCurationListResultV1.model_construct(
        tag="playbill-curation-list-result-v1",
        coordinate=coordinate,
        generation=generation,
        evaluation_time=request.evaluation_time,
        operational_head_digest=head.head_digest,
        items=items,
        detector_coverage=detected.coverage,
        observation_coverage=observation_coverage,
        result_digest="sha256:" + "0" * 64,
    )
    return PlaybillCurationListResultV1(
        coordinate=coordinate,
        generation=generation,
        evaluation_time=request.evaluation_time,
        operational_head_digest=head.head_digest,
        items=items,
        detector_coverage=detected.coverage,
        observation_coverage=observation_coverage,
        result_digest=curation_list_result_digest(provisional),
    )


def _open_item(
    instance: PlaybillInstance,
    item_id: str,
    *,
    allow_quarantined: bool = False,
) -> CurationItemV1:
    item = next((item for item in _replay_items(instance) if item.item_id == item_id), None)
    if item is None:
        raise PlaybillCurationItemNotFound(f"curation item does not exist: {item_id}")
    if item.status != "open" and not (allow_quarantined and item.status == "quarantined"):
        raise PlaybillCurationItemAlreadyResolved(
            f"curation item is already {item.status}: {item_id}"
        )
    return item


def _action_result(instance: PlaybillInstance, item_id: str) -> PlaybillCurationActionResultV1:
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    generation = _generation(instance, coordinate)
    item = next(item for item in _replay_items(instance) if item.item_id == item_id)
    return PlaybillCurationActionResultV1(
        coordinate=coordinate,
        generation=generation,
        operational_head_digest=instance.review_operational_store().head().head_digest,
        item=item,
    )


def service_overrule_playbill_curation(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationOverruleRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationActionResultV1:
    item = _open_item(instance, request.item_id, allow_quarantined=True)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    generation = _generation(instance, coordinate)
    payload = build_curation_overruled(
        item_id=item.item_id,
        expected_latest_event_digest=request.expected_latest_event_digest,
        actor_principal_id=actor_context.actor_id,
        reason=request.reason,
        attribution_refs=request.attribution_refs,
    )
    instance.review_operational_store().append(
        family="curation",
        partition_id=item.item_id,
        event_id=payload.event_id,
        payload=payload,
        coordinate=coordinate,
        generation=generation,
        actor_context=actor_context,
        recorded_at=actor_context.timestamp,
        expected_latest_event_digest=request.expected_latest_event_digest,
    )
    return _action_result(instance, item.item_id)


def service_suppress_playbill_curation(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationSuppressRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationActionResultV1:
    item = _open_item(instance, request.item_id, allow_quarantined=True)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    generation = _generation(instance, coordinate)
    if request.until_generation is not None and request.until_generation < generation:
        raise PlaybillCurationSuppressionInvalid(
            "curation suppression until_generation is already expired"
        )
    payload = build_curation_suppressed(
        item_id=item.item_id,
        expected_latest_event_digest=request.expected_latest_event_digest,
        actor_principal_id=actor_context.actor_id,
        reason=request.reason,
        scope=request.scope,
        until_generation=request.until_generation,
        attribution_refs=request.attribution_refs,
    )
    instance.review_operational_store().append(
        family="curation",
        partition_id=item.item_id,
        event_id=payload.event_id,
        payload=payload,
        coordinate=coordinate,
        generation=generation,
        actor_context=actor_context,
        recorded_at=actor_context.timestamp,
        expected_latest_event_digest=request.expected_latest_event_digest,
    )
    return _action_result(instance, item.item_id)


def _accepted_change(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    changeset_digest: str,
) -> tuple[int, ChangeSetRecordAnyVersion, dict[str, bytes], dict[str, bytes]]:
    try:
        evaluation = instance.proposal_evidence().read_evaluation(proposal_id)
    except PlaybillError as exc:
        raise PlaybillCurationResolvingProposalInvalid(
            "curation resolving proposal has no unique durable evaluation"
        ) from exc
    if evaluation.verdict != "candidate" or evaluation.candidate_digest is None:
        raise PlaybillCurationResolvingProposalInvalid(
            "curation resolving proposal did not produce a candidate"
        )
    history = instance.accepted_history()
    matches = tuple(
        (index, generation)
        for index, generation in enumerate(history)
        if generation.record is not None
        and generation.record.candidate_digest == evaluation.candidate_digest
        and generation.record.changeset_digest == changeset_digest
    )
    if len(matches) != 1:
        raise PlaybillCurationResolvingProposalInvalid(
            "curation resolving proposal/ChangeSet is not one accepted generation"
        )
    index, generation = matches[0]
    assert generation.record is not None
    parent_tree = instance.tree_at(history[index - 1].oid)
    candidate_tree = instance.tree_at(generation.oid)
    return generation.sequence, generation.record, parent_tree, candidate_tree


def _affected_members(
    record: ChangeSetRecordAnyVersion,
    *,
    parent_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
) -> tuple[CurationAffectedMemberV1, ...]:
    members = getattr(record, "members")
    result: list[CurationAffectedMemberV1] = []
    for member in members:
        path = str(member.path)
        before = parent_tree.get(path)
        after = candidate_tree.get(path)
        before_state = None if before is None else parse_dependency_artifact(path, before)
        after_state = None if after is None else parse_dependency_artifact(path, after)
        if before is None:
            disposition: Literal["create", "replace", "retire", "delete"] = "create"
        elif after is None:
            disposition = "delete"
        elif (
            before_state is not None
            and after_state is not None
            and before_state.lifecycle.state == "live"
            and after_state.lifecycle.state == "retired"
        ):
            disposition = "retire"
        else:
            disposition = "replace"
        predecessor = None if before_state is None else before_state.artifact_digest
        candidate = None if after_state is None else after_state.artifact_digest
        if predecessor is None:
            predecessor = getattr(member, "predecessor_artifact_digest", None)
        if candidate is None:
            candidate = getattr(
                member,
                "candidate_artifact_digest",
                getattr(member, "artifact_digest", None),
            )
        result.append(
            CurationAffectedMemberV1(
                path=path,
                disposition=disposition,
                predecessor_artifact_digest=predecessor,
                candidate_artifact_digest=candidate,
            )
        )
    return tuple(sorted(result, key=lambda item: item.path.encode("utf-8")))


def _related_paths(
    item: CurationItemV1,
    *,
    tree: Mapping[str, bytes],
) -> set[str]:
    paths = {ref.path for ref in item.latest_evidence_refs if ref.path is not None}
    paths.update(
        state.path for state in dependency_artifacts(tree) if state.identity == item.subject
    )
    return paths


def service_accept_fixed_playbill_curation(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationAcceptFixedRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationActionResultV1:
    item = _open_item(instance, request.item_id)
    resolved_generation, record, parent_tree, candidate_tree = _accepted_change(
        instance,
        proposal_id=request.accepted_proposal_id,
        changeset_digest=request.accepted_changeset_digest,
    )
    # The item is proposed only after its accepted coordinate is observed; a
    # resolving ChangeSet must therefore postdate, not merely equal, that generation.
    if resolved_generation <= item.first_proposed_generation:
        raise PlaybillCurationResolvingProposalInvalid(
            "curation resolving generation does not postdate the item"
        )
    affected = _affected_members(record, parent_tree=parent_tree, candidate_tree=candidate_tree)
    related = _related_paths(item, tree=parent_tree) | _related_paths(item, tree=candidate_tree)
    if not any(member.path in related for member in affected):
        raise PlaybillCurationResolvingChangeUnrelated(
            "accepted ChangeSet does not intersect the curation subject or evidence"
        )
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    generation = _generation(instance, coordinate)
    payload = build_curation_accepted_fixed(
        item_id=item.item_id,
        expected_latest_event_digest=request.expected_latest_event_digest,
        actor_principal_id=actor_context.actor_id,
        reason=request.reason,
        accepted_proposal_id=request.accepted_proposal_id,
        accepted_changeset_digest=request.accepted_changeset_digest,
        resolved_generation=resolved_generation,
        affected_members=affected,
        attribution_refs=request.attribution_refs,
    )
    instance.review_operational_store().append(
        family="curation",
        partition_id=item.item_id,
        event_id=payload.event_id,
        payload=payload,
        coordinate=coordinate,
        generation=generation,
        actor_context=actor_context,
        recorded_at=actor_context.timestamp,
        expected_latest_event_digest=request.expected_latest_event_digest,
    )
    return _action_result(instance, item.item_id)


__all__ = [
    "BLOCK_OBSERVATION_ID_DOMAIN",
    "BlockObservationV1",
    "PlaybillCurationCoverageCountV1",
    "PlaybillCurationObservationOmissionReason",
    "PlaybillCurationAcceptFixedRequestV1",
    "PlaybillCurationActionResultV1",
    "PlaybillCurationError",
    "PlaybillCurationItemAlreadyResolved",
    "PlaybillCurationItemNotFound",
    "PlaybillCurationListRequestV1",
    "PlaybillCurationListResultV1",
    "PlaybillCurationObservationCoverageV1",
    "PlaybillCurationOverruleRequestV1",
    "PlaybillCurationResolvingChangeUnrelated",
    "PlaybillCurationResolvingProposalInvalid",
    "PlaybillCurationSuppressRequestV1",
    "PlaybillCurationSuppressionInvalid",
    "block_observation_id",
    "build_block_observation",
    "curation_list_result_digest",
    "service_accept_fixed_playbill_curation",
    "service_list_playbill_curation",
    "service_overrule_playbill_curation",
    "service_suppress_playbill_curation",
    "validate_playbill_curation_list_request",
]
