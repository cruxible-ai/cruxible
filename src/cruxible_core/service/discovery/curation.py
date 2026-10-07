"""The curation queue: detection as an internal action, a pure list, rulings, block scans."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts import (
    CURATION_LIST_DEFAULT_LIMIT,
    CURATION_LIST_MAX_LIMIT,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, parse_artifact_identity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.change_control import DryRun, PreviewAt
from cruxible_client.contracts.declared_blocks import ProjectionMarkerSummary
from cruxible_client.contracts.documents import document_path, parse_document
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_client.contracts.validation_messages import validation_summary
from cruxible_core.claims.closure import dependency_artifacts, parse_dependency_artifact
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.curation.curation import (
    CurationAffectedMemberV1,
    CurationDetectorCoverageV1,
    CurationItemV1,
    CurationPatternKind,
    CurationSuppressionScope,
    build_curation_accepted_fixed,
    build_curation_overruled,
    build_curation_suppressed,
    build_curation_unsuppressed,
    build_pattern_observation,
    replay_curation_items,
)
from cruxible_core.curation.curation_detectors import run_curation_detectors
from cruxible_core.curation.review_operational import (
    ReviewOperationalConcurrentChangeError,
    ReviewOperationalStoreError,
)
from cruxible_core.exhaust.consumption import (
    consumption_receipts_enabled,
    ensure_consumption_epoch,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.proposals.settlement import ChangeSetRecordAnyVersion
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import change_entry, change_scope
from cruxible_core.service.discovery.next import (
    NextAccessProfileInvalid,
    NextSourceObservationV3,
    NextWorkspaceObservation,
    NextWorkspaceObservationInvalid,
)
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
from cruxible_core.service.list_pages import (
    ListCursorStale,
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)

BLOCK_OBSERVATION_ID_DOMAIN = "playbill-block-observation-v1"
BLOCK_SCAN_ID_DOMAIN = "playbill-block-scan-v1"
CURATION_RESULT_DIGEST_DOMAIN = "playbill-curation-list-result-v1"

PlaybillCurationObservationOmissionReason: TypeAlias = Literal[
    "block_subject_unresolved",
    "marker_coordinate_unaccepted",
    "projection_block_unstamped",
    "projection_marker_invalid",
    "source_observation_not_v3",
    "source_scan_incomplete",
]


class CurationError(CruxibleError):
    code = "cruxible.curation.refused"

    @property
    def error_code(self) -> str:
        return self.code


class CurationCoordinateNotAccepted(CurationError):
    code = "cruxible.curation.coordinate_not_accepted"


class CurationItemNotFound(CurationError):
    code = "cruxible.curation.item_not_found"


class CurationItemAlreadyResolved(CurationError):
    code = "cruxible.curation.item_already_resolved"


class CurationSuppressionInvalid(CurationError):
    code = "cruxible.curation.suppression_invalid"


class CurationResolvingProposalInvalid(CurationError):
    code = "cruxible.curation.resolving_proposal_invalid"


class CurationResolvingChangeUnrelated(CurationError):
    code = "cruxible.curation.resolving_change_unrelated"


class _StrictCurationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillCurationListRequestV1(_StrictCurationModel):
    """One page of the curation queue; a pure read of what detection recorded."""

    tag: Literal["playbill-curation-list-request-v1"] = "playbill-curation-list-request-v1"
    access_profile: CoverageAccessProfile
    limit: int = Field(default=CURATION_LIST_DEFAULT_LIMIT, ge=1, le=CURATION_LIST_MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=4096)


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
            raise NextAccessProfileInvalid(
                f"{NextAccessProfileInvalid.code}: {validation_summary(exc)}"
            ) from exc
        raise CurationError(f"{CurationError.code}: {validation_summary(exc)}") from exc


class PlaybillCurationObserveRequestV1(_StrictCurationModel):
    """Record the declared blocks a client scan of its workspace saw, for block churn."""

    tag: Literal["playbill-curation-observe-request-v1"] = "playbill-curation-observe-request-v1"
    workspace_observation: NextWorkspaceObservation
    dry_run: DryRun = None
    at: PreviewAt = None


def validate_playbill_curation_observe_request(
    value: PlaybillCurationObserveRequestV1 | Mapping[str, object],
) -> PlaybillCurationObserveRequestV1:
    if isinstance(value, PlaybillCurationObserveRequestV1):
        return value
    try:
        return PlaybillCurationObserveRequestV1.model_validate(value)
    except ValidationError as exc:
        roots = {str(item["loc"][0]) for item in exc.errors() if item["loc"]}
        if "workspace_observation" in roots:
            raise NextWorkspaceObservationInvalid(
                f"{NextWorkspaceObservationInvalid.code}: {validation_summary(exc)}"
            ) from exc
        raise CurationError(f"{CurationError.code}: {validation_summary(exc)}") from exc


class PlaybillCurationOverruleRequestV1(_StrictCurationModel):
    tag: Literal["playbill-curation-overrule-request-v1"] = "playbill-curation-overrule-request-v1"
    item_id: str
    expected_latest_event_digest: str
    reason: str = Field(min_length=1)
    attribution_refs: tuple[str, ...] = ()
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("item_id", "expected_latest_event_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class PlaybillCurationAcceptFixedRequestV1(_StrictCurationModel):
    """Link an item to the accepted change that fixed it.

    Name the change by ``accepted_proposal_id`` (with ``accepted_changeset_digest``
    to pin it exactly) or by ``accepted_generation``; the daemon resolves the other.
    """

    tag: Literal["playbill-curation-accept-fixed-request-v1"] = (
        "playbill-curation-accept-fixed-request-v1"
    )
    item_id: str
    expected_latest_event_digest: str
    reason: str = Field(min_length=1)
    accepted_proposal_id: str | None = None
    accepted_changeset_digest: str | None = None
    accepted_generation: int | None = Field(default=None, ge=1)
    attribution_refs: tuple[str, ...] = ()
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("item_id", "expected_latest_event_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("accepted_proposal_id", "accepted_changeset_digest")
    @classmethod
    def _optional_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _one_change(self) -> PlaybillCurationAcceptFixedRequestV1:
        if (self.accepted_proposal_id is None) == (self.accepted_generation is None):
            raise ValueError(
                "name the fixing change by accepted_proposal_id or accepted_generation"
            )
        if self.accepted_changeset_digest is not None and self.accepted_proposal_id is None:
            raise ValueError("accepted_changeset_digest pins an accepted_proposal_id")
        return self


class PlaybillCurationSuppressRequestV1(_StrictCurationModel):
    tag: Literal["playbill-curation-suppress-request-v1"] = "playbill-curation-suppress-request-v1"
    item_id: str
    expected_latest_event_digest: str
    reason: str = Field(min_length=1)
    #: ``item`` hides this item; ``lineage`` also hides every successor the same
    #: pattern opens after it is fixed.
    scope: CurationSuppressionScope
    until_generation: int | None = Field(default=None, ge=0)
    attribution_refs: tuple[str, ...] = ()
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("item_id", "expected_latest_event_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class PlaybillCurationUnsuppressRequestV1(_StrictCurationModel):
    """Lift one suppression recorded on an item; with one in force it need not be named."""

    tag: Literal["playbill-curation-unsuppress-request-v1"] = (
        "playbill-curation-unsuppress-request-v1"
    )
    item_id: str
    expected_latest_event_digest: str
    reason: str = Field(min_length=1)
    suppression_event_id: str | None = None
    attribution_refs: tuple[str, ...] = ()
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("item_id", "expected_latest_event_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("suppression_event_id")
    @classmethod
    def _suppression(cls, value: str | None) -> str | None:
        if value is not None:
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


class BlockScanV1(_StrictCurationModel):
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


PlaybillCurationInactiveReason: TypeAlias = Literal[
    "consumption_receipts_off",
    "no_block_observations",
]


class PlaybillCurationInactiveDetectorV1(_StrictCurationModel):
    """A detector that cannot find anything here, and why."""

    pattern_kind: CurationPatternKind
    reason: PlaybillCurationInactiveReason


class PlaybillCurationDetectionV1(_StrictCurationModel):
    """When detection last ran, and whether it has caught up with the accepted head.

    Detection runs as the ``curation.detect`` internal action, fired by a live
    Trigger on accepted generations; ``trigger`` says whether one is live.
    """

    state: Literal["current", "behind", "never_run"]
    trigger: Literal["live", "missing"]
    detected_through_generation: int | None = Field(default=None, ge=0)
    detected_at: datetime | None = None

    @field_validator("detected_at")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class PlaybillCurationListResultV1(_StrictCurationModel):
    tag: Literal["playbill-curation-list-result-v1"] = "playbill-curation-list-result-v1"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    operational_head_digest: str
    items: tuple[CurationItemV1, ...] = ()
    detection: PlaybillCurationDetectionV1
    detector_coverage: tuple[CurationDetectorCoverageV1, ...]
    inactive_detectors: tuple[PlaybillCurationInactiveDetectorV1, ...] = ()
    #: The latest recorded workspace scan's accounting; None before any scan.
    observation_coverage: PlaybillCurationObservationCoverageV1 | None = None
    truncated: bool = False
    next_cursor: str | None = None
    result_digest: str

    @field_validator("operational_head_digest", "result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reproduces(self) -> PlaybillCurationListResultV1:
        if self.result_digest != curation_list_result_digest(self):
            raise ValueError("curation list result digest does not reproduce")
        return self


class PlaybillCurationObserveResultV1(_StrictCurationModel):
    tag: Literal["playbill-curation-observe-result-v1"] = "playbill-curation-observe-result-v1"
    #: ``would_record`` answers a preview: the scan was checked, nothing appended.
    status: Literal["recorded", "would_record"] = "recorded"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    observation_coverage: PlaybillCurationObservationCoverageV1


class PlaybillCurationActionResultV1(_StrictCurationModel):
    tag: Literal["playbill-curation-action-result-v1"] = "playbill-curation-action-result-v1"
    #: ``would_record`` answers a preview: every check ran and nothing was
    #: appended, so ``item`` is the item as it stands (R12).
    status: Literal["recorded", "would_record"] = "recorded"
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
    source: NextSourceObservationV3,
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
    matches = tuple(
        item.sequence for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if len(matches) != 1:
        raise CurationCoordinateNotAccepted(
            "curation requires the current replay-verified accepted coordinate"
        )
    return matches[0]


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
    observation: NextWorkspaceObservation,
    actor_context: GovernedActorContext,
    previewing: bool = False,
) -> PlaybillCurationObservationCoverageV1:
    accepted = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(accepted)
    generation = _generation(instance, coordinate)
    tree = ClaimVerdictReadContext(instance, accepted).tree
    counts: Counter[PlaybillCurationObservationOmissionReason] = Counter()
    source_count = 0
    observed = 0
    sources = observation.source_observations
    if sources is not None:
        for source in sources:
            source_count += 1
            if not isinstance(source, NextSourceObservationV3):
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
    evidence = instance.proposal_evidence()
    assert evidence.index is not None
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
                {
                    row["proposal_id"]
                    # Admission commits publication; an evaluation surviving an
                    # interrupted submission never names a resolving proposal.
                    for row in evidence.index.rows(
                        evidence,
                        "candidate_digest=? AND admission_path IS NOT NULL",
                        (accepted.record.candidate_digest,),
                    )
                },
                key=lambda value: value.encode("ascii"),
            )
        )
        if not proposal_ids:
            # Imported ledgers can preserve the accepted receipt without local
            # proposal exhaust.  Do not invent a resolving proposal identity.
            continue
        paths = tuple(member.path for member in accepted.record.members)
        parent_tree = instance.blobs_at(history[index - 1].oid, paths)
        candidate_tree = instance.blobs_at(accepted.oid, paths)
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


def _latest_block_scan(instance: PlaybillInstance) -> BlockScanV1 | None:
    scans = instance.review_operational_store().events(family="block_scan")
    for _event, payload in reversed(scans):
        try:
            return BlockScanV1.model_validate(payload)
        except ValueError:
            continue
    return None


def service_observe_playbill_curation_blocks(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationObserveRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationObserveResultV1:
    """Record the declared blocks one client workspace scan saw, and the scan's accounting.

    Block churn is the one detector that needs the workspace, which the daemon
    never reads, so the client's scan is recorded here and detection reads it
    when it next runs.
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
            operation="cruxible.curation.observe",
            describe="recording a workspace block scan",
        ) as mode:
            if mode.previewing:
                coverage = _record_block_observations(
                    instance,
                    observation=request.workspace_observation,
                    actor_context=actor_context,
                    previewing=True,
                )
                return PlaybillCurationObserveResultV1(
                    status="would_record",
                    coordinate=coordinate,
                    generation=generation,
                    observation_coverage=coverage,
                )
            with mode.committing():
                coverage = _record_block_observations(
                    instance,
                    observation=request.workspace_observation,
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
        return PlaybillCurationObserveResultV1(
            coordinate=coordinate, generation=generation, observation_coverage=coverage
        )


def run_playbill_curation_detection(
    instance: PlaybillInstance,
    *,
    evaluation_time: datetime,
    actor_context: GovernedActorContext,
) -> tuple[int, tuple[CurationDetectorCoverageV1, ...]]:
    """Run every detector at the accepted head and record what it found.

    The ``curation.detect`` internal action runs this when its Trigger fires;
    ``evaluation_time`` is the fire's instant, never a reader's clock. Each
    detection lands on its pattern's item (a successor after a fix), an
    overruled pattern is never observed again, and dead-vocabulary items whose
    artifact was retired are resolved. Returns the generation detected through
    and each detector's coverage.
    """

    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    generation = _generation(instance, coordinate)
    store = instance.review_operational_store()
    if consumption_receipts_enabled():
        ensure_consumption_epoch(
            instance,
            coordinate=coordinate,
            generation=generation,
            actor_context=actor_context,
        )
    scan = _latest_block_scan(instance)
    block_association_omissions = (
        0
        if scan is None
        else next(
            (
                item.count
                for item in scan.coverage.omissions
                if item.reason == "block_subject_unresolved"
            ),
            0,
        )
    )
    detected = run_curation_detectors(
        instance,
        coordinate=coordinate,
        generation=generation,
        evaluation_time=evaluation_time,
        operational_head_digest=store.head().head_digest,
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
        if current is not None and current.status == "overruled":
            continue
        starts_successor = current is not None and current.status in {
            "accepted_fixed",
            "quarantined",
        }
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
                    recorded_at=evaluation_time,
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
                if current is not None and current.status == "overruled":
                    break
                starts_successor = current is not None and current.status in {
                    "accepted_fixed",
                    "quarantined",
                }
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
        recorded_at=evaluation_time,
    )
    return generation, detected.coverage


def _detection_trigger_live(instance: PlaybillInstance) -> bool:
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        row = projection.typed.connection.execute(
            "SELECT 1 FROM triggers WHERE target_kind='action' AND lifecycle='live' "
            "AND target=? LIMIT 1",
            ("curation.detect",),
        ).fetchone()
    return row is not None


def _inactive_detectors(
    instance: PlaybillInstance, scan: BlockScanV1 | None
) -> tuple[PlaybillCurationInactiveDetectorV1, ...]:
    inactive: list[PlaybillCurationInactiveDetectorV1] = []
    if scan is None or scan.coverage.observed_block_count == 0:
        if not instance.review_operational_store().events(family="block_observation"):
            inactive.append(
                PlaybillCurationInactiveDetectorV1(
                    pattern_kind="playbill.curation.block_churn.v1",
                    reason="no_block_observations",
                )
            )
    if not consumption_receipts_enabled():
        inactive.append(
            PlaybillCurationInactiveDetectorV1(
                pattern_kind="playbill.curation.dead_vocabulary.v1",
                reason="consumption_receipts_off",
            )
        )
    return tuple(sorted(inactive, key=lambda item: item.pattern_kind.encode("ascii")))


def service_list_playbill_curation(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationListRequestV1,
) -> PlaybillCurationListResultV1:
    """One page of the visible curation queue, as detection last recorded it.

    A pure read: detection runs on its own (the ``curation.detect`` action), so
    the list writes nothing and reads no clock. A cursor continues its first
    page only while the accepted coordinate and the queue are unchanged.
    """

    from cruxible_core.consumers.next.curation import curation_detection

    internal_coordinate = instance.accepted_coordinate()
    coordinate = AcceptedCoordinate.from_internal(internal_coordinate)
    selection = {"access_profile": request.access_profile.model_dump(mode="json")}
    continuation = (
        None
        if request.cursor is None
        else decode_list_cursor(request.cursor, list_name=_CURATION_LIST, selection=selection)
    )
    if continuation is not None and continuation.coordinate != coordinate.model_dump(mode="json"):
        raise ListCursorStale(
            f"{ListCursorStale.error_code}: accepted state moved since the "
            "cursor's first page; list the curation queue again without a cursor"
        )
    generation = _generation(instance, coordinate)
    store = instance.review_operational_store()
    recorded = curation_detection(instance)
    detection = PlaybillCurationDetectionV1(
        state=(
            "never_run"
            if recorded is None
            else "current"
            if recorded.generation >= generation
            else "behind"
        ),
        trigger="live" if _detection_trigger_live(instance) else "missing",
        detected_through_generation=None if recorded is None else recorded.generation,
        detected_at=None if recorded is None else recorded.detected_at,
    )
    # G9 visibility note: all present curation facts are instance-class.  Until
    # sub-instance ACLs exist the access decision is intentionally binary and
    # per-item filtering would be vacuous rather than an additional guarantee.
    permitted = request.access_profile.permits("instance")
    scan = _latest_block_scan(instance) if permitted else None
    all_items = _replay_items(instance) if permitted else ()
    items = tuple(
        sorted(
            (
                item
                for item in all_items
                if item.status in {"open", "quarantined"}
                and not item.suppressed_at(generation, all_items=all_items)
            ),
            key=_curation_sort_key,
        )
    )
    snapshot = list_snapshot([[item.item_id, item.status] for item in items])
    page, truncated = page_after_boundary(
        items,
        keys=tuple(_curation_key(item) for item in items),
        snapshot=snapshot,
        continuation=continuation,
        limit=request.limit,
        list_name=_CURATION_LIST,
    )
    next_cursor = (
        encode_list_cursor(
            list_name=_CURATION_LIST,
            coordinate=coordinate.model_dump(mode="json"),
            selection=selection,
            snapshot=snapshot,
            last_key=_curation_key(page[-1]),
        )
        if truncated and page
        else None
    )
    values: dict[str, object] = {
        "coordinate": coordinate,
        "generation": generation,
        "operational_head_digest": store.head().head_digest,
        "items": page,
        "detection": detection,
        "detector_coverage": recorded.coverage if (permitted and recorded is not None) else (),
        "inactive_detectors": _inactive_detectors(instance, scan) if permitted else (),
        "observation_coverage": None if scan is None else scan.coverage,
        "truncated": truncated,
        "next_cursor": next_cursor,
    }
    provisional = PlaybillCurationListResultV1.model_construct(
        tag="playbill-curation-list-result-v1",
        result_digest="sha256:" + "0" * 64,
        **values,  # type: ignore[arg-type]
    )
    return PlaybillCurationListResultV1(
        result_digest=curation_list_result_digest(provisional),
        **values,  # type: ignore[arg-type]
    )


_CURATION_LIST = "curation"


def _curation_sort_key(item: CurationItemV1) -> tuple[bytes, bytes, bytes]:
    return (
        item.pattern_kind.encode("ascii"),
        item.subject.qualified.encode("utf-8"),
        item.item_id.encode("ascii"),
    )


def _curation_key(item: CurationItemV1) -> tuple[str, str, str]:
    return (item.pattern_kind, item.subject.qualified, item.item_id)


def _open_item(
    instance: PlaybillInstance,
    item_id: str,
    *,
    allow_quarantined: bool = False,
) -> CurationItemV1:
    item = next((item for item in _replay_items(instance) if item.item_id == item_id), None)
    if item is None:
        raise CurationItemNotFound(f"curation item does not exist: {item_id}")
    if item.status != "open" and not (allow_quarantined and item.status == "quarantined"):
        raise CurationItemAlreadyResolved(f"curation item is already {item.status}: {item_id}")
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


def _record_ruling(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationOverruleRequestV1
    | PlaybillCurationSuppressRequestV1
    | PlaybillCurationUnsuppressRequestV1
    | PlaybillCurationAcceptFixedRequestV1,
    item_id: str,
    payload: BaseModel,
    coordinate: AcceptedCoordinate,
    generation: int,
    actor_context: GovernedActorContext,
    operation: str,
) -> PlaybillCurationActionResultV1:
    """Append one ruling, or (previewing) run every check the append runs (R12)."""

    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="direct",
        operation=operation,
        describe=f"recording a curation ruling on {item_id}",
    ) as mode:
        store = instance.review_operational_store()
        event_id = cast(str, getattr(payload, "event_id"))
        if mode.previewing:
            store.check_append(
                family="curation",
                partition_id=item_id,
                event_id=event_id,
                payload=payload,
                expected_latest_event_digest=request.expected_latest_event_digest,
            )
            return _action_result(instance, item_id).model_copy(update={"status": "would_record"})
        # A pinned ruling confirms the live accepted head and appends while
        # holding it still (R12).
        with mode.committing():
            store.append(
                family="curation",
                partition_id=item_id,
                event_id=event_id,
                payload=payload,
                coordinate=coordinate,
                generation=generation,
                actor_context=actor_context,
                recorded_at=actor_context.timestamp,
                expected_latest_event_digest=request.expected_latest_event_digest,
            )
        return _action_result(instance, item_id)


def service_overrule_playbill_curation(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationOverruleRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationActionResultV1:
    instance.require_writable()
    # The whole ruling runs behind a preview's guards: reading the accepted
    # history and proposal evidence may catch derived indexes up on disk.
    with change_entry(request.dry_run, "direct"):
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
        return _record_ruling(
            instance,
            request=request,
            item_id=item.item_id,
            payload=payload,
            coordinate=coordinate,
            generation=generation,
            actor_context=actor_context,
            operation="cruxible.curation.overrule",
        )


def service_suppress_playbill_curation(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationSuppressRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationActionResultV1:
    instance.require_writable()
    # The whole ruling runs behind a preview's guards: reading the accepted
    # history and proposal evidence may catch derived indexes up on disk.
    with change_entry(request.dry_run, "direct"):
        item = _open_item(instance, request.item_id, allow_quarantined=True)
        coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        generation = _generation(instance, coordinate)
        if request.until_generation is not None and request.until_generation < generation:
            raise CurationSuppressionInvalid(
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
        return _record_ruling(
            instance,
            request=request,
            item_id=item.item_id,
            payload=payload,
            coordinate=coordinate,
            generation=generation,
            actor_context=actor_context,
            operation="cruxible.curation.suppress",
        )


def service_unsuppress_playbill_curation(
    instance: PlaybillInstance,
    *,
    request: PlaybillCurationUnsuppressRequestV1,
    actor_context: GovernedActorContext,
) -> PlaybillCurationActionResultV1:
    """Lift one suppression recorded on an item, so what it hid is listed again."""

    instance.require_writable()
    with change_entry(request.dry_run, "direct"):
        item = _open_item(instance, request.item_id, allow_quarantined=True)
        if request.suppression_event_id is None:
            if len(item.suppressions) != 1:
                raise CurationSuppressionInvalid(
                    f"curation item {item.item_id} carries {len(item.suppressions)} "
                    "suppressions; name the one to lift with suppression_event_id"
                )
            suppression_event_id = item.suppressions[0].event_id
        else:
            suppression_event_id = request.suppression_event_id
            if suppression_event_id not in {entry.event_id for entry in item.suppressions}:
                raise CurationSuppressionInvalid(
                    f"curation item {item.item_id} carries no suppression {suppression_event_id}"
                )
        coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        generation = _generation(instance, coordinate)
        payload = build_curation_unsuppressed(
            item_id=item.item_id,
            expected_latest_event_digest=request.expected_latest_event_digest,
            actor_principal_id=actor_context.actor_id,
            reason=request.reason,
            suppression_event_id=suppression_event_id,
            attribution_refs=request.attribution_refs,
        )
        return _record_ruling(
            instance,
            request=request,
            item_id=item.item_id,
            payload=payload,
            coordinate=coordinate,
            generation=generation,
            actor_context=actor_context,
            operation="cruxible.curation.unsuppress",
        )


def _proposals_for_candidate(instance: PlaybillInstance, candidate_digest: str) -> list[str]:
    evidence = instance.proposal_evidence()
    assert evidence.index is not None
    with evidence.index.read(evidence, review_context=True) as connection:
        rows = connection.execute(
            "SELECT proposal_id FROM proposals WHERE candidate_digest=? ORDER BY proposal_id",
            (candidate_digest,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _resolve_fixing_change(
    instance: PlaybillInstance, request: PlaybillCurationAcceptFixedRequestV1
) -> tuple[str, str]:
    """The (proposal, ChangeSet) pair a request names by proposal or by generation."""

    history = instance.accepted_history()
    if request.accepted_generation is not None:
        located = next(
            (item for item in history if item.sequence == request.accepted_generation), None
        )
        if located is None or located.record is None:
            raise CurationResolvingProposalInvalid(
                f"accepted generation {request.accepted_generation} carries no ChangeSet"
            )
        proposals = _proposals_for_candidate(instance, located.record.candidate_digest)
        if len(proposals) != 1:
            raise CurationResolvingProposalInvalid(
                f"accepted generation {request.accepted_generation} maps to {len(proposals)} "
                "proposals; name the fixing proposal with accepted_proposal_id"
            )
        return proposals[0], located.record.changeset_digest
    assert request.accepted_proposal_id is not None
    if request.accepted_changeset_digest is not None:
        return request.accepted_proposal_id, request.accepted_changeset_digest
    try:
        evaluation = instance.proposal_evidence().read_evaluation(request.accepted_proposal_id)
    except CruxibleError as exc:
        raise CurationResolvingProposalInvalid(
            "curation resolving proposal has no unique durable evaluation"
        ) from exc
    accepted = tuple(
        item
        for item in history
        if item.record is not None
        and evaluation.candidate_digest is not None
        and item.record.candidate_digest == evaluation.candidate_digest
    )
    if len(accepted) != 1:
        raise CurationResolvingProposalInvalid(
            "curation resolving proposal was not accepted as exactly one generation"
        )
    record = accepted[0].record
    assert record is not None
    return request.accepted_proposal_id, record.changeset_digest


def _accepted_change(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    changeset_digest: str,
) -> tuple[int, ChangeSetRecordAnyVersion, dict[str, bytes], dict[str, bytes]]:
    try:
        evaluation = instance.proposal_evidence().read_evaluation(proposal_id)
    except CruxibleError as exc:
        raise CurationResolvingProposalInvalid(
            "curation resolving proposal has no unique durable evaluation"
        ) from exc
    if evaluation.verdict != "candidate" or evaluation.candidate_digest is None:
        raise CurationResolvingProposalInvalid(
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
        raise CurationResolvingProposalInvalid(
            "curation resolving proposal/ChangeSet is not one accepted generation"
        )
    index, generation = matches[0]
    assert generation.record is not None
    paths = tuple(member.path for member in generation.record.members)
    parent_tree = instance.blobs_at(history[index - 1].oid, paths)
    candidate_tree = instance.blobs_at(generation.oid, paths)
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
    # Only changed members can intersect this resolution; unchanged owner paths
    # cannot make an unrelated ChangeSet resolve the item.
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
    instance.require_writable()
    # The whole ruling runs behind a preview's guards: reading the accepted
    # history and proposal evidence may catch derived indexes up on disk.
    with change_entry(request.dry_run, "direct"):
        item = _open_item(instance, request.item_id)
        proposal_id, changeset_digest = _resolve_fixing_change(instance, request)
        resolved_generation, record, parent_tree, candidate_tree = _accepted_change(
            instance,
            proposal_id=proposal_id,
            changeset_digest=changeset_digest,
        )
        # The item is proposed only after its accepted coordinate is observed; a
        # resolving ChangeSet must therefore postdate, not merely equal, that generation.
        if resolved_generation <= item.first_proposed_generation:
            raise CurationResolvingProposalInvalid(
                "curation resolving generation does not postdate the item"
            )
        affected = _affected_members(record, parent_tree=parent_tree, candidate_tree=candidate_tree)
        related = _related_paths(item, tree=parent_tree) | _related_paths(item, tree=candidate_tree)
        if not any(member.path in related for member in affected):
            raise CurationResolvingChangeUnrelated(
                "accepted ChangeSet does not intersect the curation subject or evidence"
            )
        coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        generation = _generation(instance, coordinate)
        payload = build_curation_accepted_fixed(
            item_id=item.item_id,
            expected_latest_event_digest=request.expected_latest_event_digest,
            actor_principal_id=actor_context.actor_id,
            reason=request.reason,
            accepted_proposal_id=proposal_id,
            accepted_changeset_digest=changeset_digest,
            resolved_generation=resolved_generation,
            affected_members=affected,
            attribution_refs=request.attribution_refs,
        )
        return _record_ruling(
            instance,
            request=request,
            item_id=item.item_id,
            payload=payload,
            coordinate=coordinate,
            generation=generation,
            actor_context=actor_context,
            operation="cruxible.curation.accept-fixed",
        )


__all__ = [
    "BLOCK_OBSERVATION_ID_DOMAIN",
    "BlockObservationV1",
    "PlaybillCurationCoverageCountV1",
    "PlaybillCurationObservationOmissionReason",
    "PlaybillCurationAcceptFixedRequestV1",
    "PlaybillCurationActionResultV1",
    "CurationError",
    "CurationItemAlreadyResolved",
    "CurationItemNotFound",
    "PlaybillCurationListRequestV1",
    "PlaybillCurationListResultV1",
    "PlaybillCurationObservationCoverageV1",
    "PlaybillCurationOverruleRequestV1",
    "CurationResolvingChangeUnrelated",
    "CurationResolvingProposalInvalid",
    "PlaybillCurationSuppressRequestV1",
    "PlaybillCurationUnsuppressRequestV1",
    "PlaybillCurationObserveRequestV1",
    "PlaybillCurationObserveResultV1",
    "PlaybillCurationDetectionV1",
    "PlaybillCurationInactiveDetectorV1",
    "BlockScanV1",
    "CurationSuppressionInvalid",
    "block_observation_id",
    "build_block_observation",
    "curation_list_result_digest",
    "service_accept_fixed_playbill_curation",
    "service_list_playbill_curation",
    "service_overrule_playbill_curation",
    "service_suppress_playbill_curation",
    "service_unsuppress_playbill_curation",
    "service_observe_playbill_curation_blocks",
    "run_playbill_curation_detection",
    "validate_playbill_curation_list_request",
    "validate_playbill_curation_observe_request",
]
