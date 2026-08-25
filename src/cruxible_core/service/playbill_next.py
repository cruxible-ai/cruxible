"""Deterministic repair queue derived from one accepted Playbill coordinate."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    CanonicalDurationV1,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_verdicts import ClaimVerdictResultV2
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimCitationV1,
    LiteralClaimObject,
    claim_citation_references,
)
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_BLOCKS_PER_SOURCE,
    MAX_PROJECTION_CARDS_PER_SOURCE,
    MAX_PROJECTION_SOURCE_BYTES,
    PlaybillPresentationPolicyV1,
    ProjectionClaimBackingV1,
    ProjectionMarkerSummaryV1,
    ProjectionQueryBackingV1,
    projection_query_semantic_result_digest,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.query.definitions import QueryEvaluationPolicyV1
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claim_slots import classify_claim_slot
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.coverage.indexes import WorkingOccurrenceV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.query.backends import claim_row_visibility
from cruxible_core.playbill.query.engine import evaluate_claim_query
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import accepted_query_definition
from cruxible_core.service.playbill_claims import _claim_from_view, service_list_playbill_claims
from cruxible_core.service.playbill_evidence import service_evaluate_playbill_claim_verdict
from cruxible_core.service.playbill_query import build_accepted_query_facts

NEXT_ITEM_ID_DOMAIN = "playbill-next-item-v1"
NEXT_RESULT_DIGEST_DOMAIN = "playbill-next-result-v1"
DEFAULT_EXPIRING_WITHIN_MICROSECONDS = 604_800_000_000

NextDomain = Literal["accepted_state", "workspace_floor", "workspace_sources"]
NextSeverity = Literal["blocking", "repair", "warning"]
NextReason = Literal[
    "claim_conflicted",
    "claim_uncovered",
    "claim_stale_evidence",
    "citation_drifted",
    "citation_source_unobserved",
    "evidence_expiring",
    "floor_missing",
    "floor_stale",
    "floor_invalid",
    "projection_dirty",
    "projection_backing_stale",
    "self_published_source_stale",
]
NextRepairOperation = Literal[
    "playbill.authoring.create",
    "playbill.authoring.bind",
    "playbill.claim_type.migrate",
    "playbill.floor.export",
    "playbill.block.repin",
]

_SEVERITY_RANK: dict[NextSeverity, int] = {"blocking": 0, "repair": 1, "warning": 2}
_ALL_DOMAINS: tuple[NextDomain, ...] = (
    "accepted_state",
    "workspace_floor",
    "workspace_sources",
)
_PROJECTION_VISIBILITY_POLICY = QueryEvaluationPolicyV1(
    visible_verdicts=("contradicted", "stale", "supported", "uncovered", "unresolved"),
    visible_currency=("current", "not_applicable", "stale"),
    conflict_behavior="surface_conflicts",
)


class _StrictNextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillNextError(PlaybillError):
    code = "playbill.next.refused"


class PlaybillNextAccessProfileInvalid(PlaybillNextError):
    code = "playbill.next.access_profile_invalid"


class PlaybillNextWorkspaceObservationInvalid(PlaybillNextError):
    code = "playbill.next.workspace_observation_invalid"


class PlaybillNextCoordinateNotAccepted(PlaybillNextError):
    code = "playbill.next.coordinate_not_accepted"


class PlaybillNextAcceptedStateInvalid(PlaybillNextError):
    code = "playbill.next.accepted_state_invalid"


class PlaybillNextDriftObservationV1(_StrictNextModel):
    citation_id: str
    expected_commitment_digest: str
    observed_commitment_digest: str

    @field_validator("citation_id", "expected_commitment_digest", "observed_commitment_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class PlaybillNextSourceObservationV1(_StrictNextModel):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    observed_source_digest: str

    @field_validator("observed_source_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class PlaybillNextSourceObservationV2(_StrictNextModel):
    tag: Literal["playbill-next-source-observation-v2"]
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    observed_source_digest: str
    byte_length: int = Field(ge=0, le=MAX_PROJECTION_SOURCE_BYTES)
    marker_summaries: tuple[ProjectionMarkerSummaryV1, ...] = Field(
        max_length=MAX_PROJECTION_BLOCKS_PER_SOURCE
    )
    occurrences: tuple[WorkingOccurrenceV1, ...] = Field(max_length=MAX_PROJECTION_CARDS_PER_SOURCE)
    scanned_commitment_digests: tuple[str, ...]
    scan_complete: bool
    scan_notes: tuple[str, ...]
    marker_notes: tuple[str, ...]

    @field_validator("observed_source_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("scanned_commitment_digests")
    @classmethod
    def _commitments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for digest in value:
            Sha256Value.from_tagged(digest)
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("next scanned commitment digests must be sorted and unique")
        return value

    @field_validator("scan_notes", "marker_notes")
    @classmethod
    def _notes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next observation notes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _source_shape(self) -> "PlaybillNextSourceObservationV2":
        ids = tuple(marker.stamp.block_id for marker in self.marker_summaries)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next marker summaries must be sorted and unique by block ID")
        previous_end = -1
        for marker in sorted(self.marker_summaries, key=lambda item: item.start_byte):
            if marker.stamp.source_id != self.source_id:
                raise ValueError("next marker summary names a different logical source")
            if marker.start_byte < previous_end or marker.end_byte > self.byte_length:
                raise ValueError("next marker summary windows overlap or escape the source")
            previous_end = marker.end_byte
        keys = tuple(occurrence.sort_key for occurrence in self.occurrences)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("next source occurrences must be sorted and unique")
        for occurrence in self.occurrences:
            if (
                occurrence.source.plane != "external"
                or occurrence.source.identity != self.source_id
            ):
                raise ValueError("next occurrence names a different logical source")
            if occurrence.line_overlay.end_byte > self.byte_length:
                raise ValueError("next occurrence presentation window escapes the source")
        if not self.scan_complete and (self.occurrences or self.scanned_commitment_digests):
            raise ValueError("an incomplete next scan cannot assert occurrences or scanned digests")
        return self


class PlaybillNextWorkspaceObservationV1(_StrictNextModel):
    tag: Literal["playbill-next-workspace-observation-v1"] = (
        "playbill-next-workspace-observation-v1"
    )
    floor_status: Literal["not_configured", "missing", "current", "stale", "invalid"] | None = None
    installed_coordinate: AcceptedCoordinate | None = None
    drift_observations: tuple[PlaybillNextDriftObservationV1, ...] | None = None
    source_observations: (
        tuple[PlaybillNextSourceObservationV1 | PlaybillNextSourceObservationV2, ...] | None
    ) = None
    presentation_policy: PlaybillPresentationPolicyV1 | None = None

    @field_validator("drift_observations")
    @classmethod
    def _drift(
        cls,
        value: tuple[PlaybillNextDriftObservationV1, ...] | None,
    ) -> tuple[PlaybillNextDriftObservationV1, ...] | None:
        if value is None:
            return None
        ids = tuple(item.citation_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("ascii"))):
            raise ValueError("next drift observations must be sorted and unique by citation_id")
        return value

    @field_validator("source_observations")
    @classmethod
    def _sources(
        cls,
        value: tuple[PlaybillNextSourceObservationV1 | PlaybillNextSourceObservationV2, ...] | None,
    ) -> tuple[PlaybillNextSourceObservationV1 | PlaybillNextSourceObservationV2, ...] | None:
        if value is None:
            return None
        ids = tuple(item.source_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next source observations must be sorted and unique by source_id")
        return value

    @model_validator(mode="after")
    def _floor_shape(self) -> "PlaybillNextWorkspaceObservationV1":
        if self.floor_status == "current" and self.installed_coordinate is None:
            raise ValueError("a current floor observation requires its installed coordinate")
        return self


class PlaybillNextRequestV1(_StrictNextModel):
    tag: Literal["playbill-next-request-v1"] = "playbill-next-request-v1"
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime
    access_profile: CoverageAccessProfileV1
    expiring_within: CanonicalDurationV1 = CanonicalDurationV1(
        microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS
    )
    workspace_observation: PlaybillNextWorkspaceObservationV1 | None = None

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def validate_playbill_next_request(
    value: PlaybillNextRequestV1 | Mapping[str, object],
) -> PlaybillNextRequestV1:
    if isinstance(value, PlaybillNextRequestV1):
        return value
    try:
        return PlaybillNextRequestV1.model_validate(value)
    except ValidationError as exc:
        roots = {str(item["loc"][0]) for item in exc.errors() if item["loc"]}
        if "access_profile" in roots:
            error: type[PlaybillNextError] = PlaybillNextAccessProfileInvalid
        elif "workspace_observation" in roots:
            error = PlaybillNextWorkspaceObservationInvalid
        else:
            error = PlaybillNextAcceptedStateInvalid
        raise error(f"{error.code}: {exc}") from exc


class PlaybillNextRepairV1(_StrictNextModel):
    operation: NextRepairOperation
    target: str
    required_change: str
    arguments: object = Field(default_factory=dict)

    @field_validator("arguments", mode="before")
    @classmethod
    def _arguments(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)


class PlaybillNextItemV1(_StrictNextModel):
    tag: Literal["playbill-next-item-v1"] = "playbill-next-item-v1"
    item_id: str
    severity: NextSeverity
    reason: NextReason
    subject_identity: str
    related_identities: tuple[str, ...] = ()
    detail: object = Field(default_factory=dict)
    repair: PlaybillNextRepairV1

    @field_validator("item_id")
    @classmethod
    def _item_id(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("related_identities")
    @classmethod
    def _related(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next related identities must be byte-sorted and unique")
        return value

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _identity(self) -> "PlaybillNextItemV1":
        if self.item_id != playbill_next_item_id(self):
            raise ValueError("next item ID does not reproduce")
        return self


class PlaybillNextResultV1(_StrictNextModel):
    tag: Literal["playbill-next-result-v1"] = "playbill-next-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    observed_domains: tuple[NextDomain, ...]
    unobserved_domains: tuple[NextDomain, ...]
    items: tuple[PlaybillNextItemV1, ...]
    result_digest: str

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> "PlaybillNextResultV1":
        if set(self.observed_domains).intersection(self.unobserved_domains):
            raise ValueError("next observed and unobserved domains overlap")
        if set((*self.observed_domains, *self.unobserved_domains)) != set(_ALL_DOMAINS):
            raise ValueError("next result must account for every observation domain")
        if self.items != tuple(sorted(self.items, key=_item_sort_key)):
            raise ValueError("next items do not follow the deterministic order")
        if self.result_digest != playbill_next_result_digest(self):
            raise ValueError("next result digest does not reproduce")
        return self


def playbill_next_item_id(item: PlaybillNextItemV1) -> str:
    payload = item.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("item_id")
    return typed_digest(Sha256Value, NEXT_ITEM_ID_DOMAIN, payload).tagged


def _item(
    *,
    severity: NextSeverity,
    reason: NextReason,
    subject_identity: str,
    related_identities: tuple[str, ...] = (),
    detail: object,
    repair: PlaybillNextRepairV1,
) -> PlaybillNextItemV1:
    values = {
        "severity": severity,
        "reason": reason,
        "subject_identity": subject_identity,
        "related_identities": related_identities,
        "detail": detail,
        "repair": repair,
    }
    provisional = PlaybillNextItemV1.model_construct(
        _fields_set=None,
        item_id="sha256:" + "0" * 64,
        severity=severity,
        reason=reason,
        subject_identity=subject_identity,
        related_identities=related_identities,
        detail=detail,
        repair=repair,
    )
    return PlaybillNextItemV1.model_validate(
        {**values, "item_id": playbill_next_item_id(provisional)}
    )


def _item_sort_key(item: PlaybillNextItemV1) -> tuple[int, bytes, bytes, bytes]:
    return (
        _SEVERITY_RANK[item.severity],
        item.subject_identity.encode("utf-8"),
        item.reason.encode("utf-8"),
        item.item_id.encode("ascii"),
    )


def playbill_next_result_digest(result: PlaybillNextResultV1) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("result_digest")
    return typed_digest(Sha256Value, NEXT_RESULT_DIGEST_DOMAIN, payload).tagged


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: AcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    try:
        return instance.resolve_accepted_coordinate(
            git_oid=at.git_oid,
            semantic_root=at.semantic_root,
            generation_root=at.generation_root,
            compiler_digest=at.compiler_digest,
        )
    except ValueError as exc:
        raise PlaybillNextCoordinateNotAccepted(
            f"{PlaybillNextCoordinateNotAccepted.code}: coordinate is not accepted"
        ) from exc


def _claim_items(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
    expiring_within: CanonicalDurationV1,
) -> tuple[PlaybillNextItemV1, ...]:
    listed = service_list_playbill_claims(instance, at=coordinate)
    claims = tuple(
        claim
        for claim in (_claim_from_view(view) for view in listed.claims)
        if claim.lifecycle.state == "live"
    )
    groups: dict[bytes, list[ClaimArtifactAny]] = defaultdict(list)
    for claim in claims:
        groups[
            canonical_bytes(
                {
                    "predicate": claim.statement.predicate,
                    "qualifier": claim.statement.qualifier,
                    "subject": claim.statement.subject.model_dump(mode="json"),
                }
            )
        ].append(claim)
    items: list[PlaybillNextItemV1] = []
    for group in groups.values():
        slot = classify_claim_slot(group)
        subject = group[0].statement.subject.artifact_path
        identities = tuple(
            sorted((claim.identity.qualified for claim in group), key=lambda item: item.encode())
        )
        if slot.resolution == "unresolved":
            discriminator = _qualifier_discriminator(group)
            detail: dict[str, object] = {
                "contender_count": slot.contender_count,
                "predicate": group[0].statement.predicate,
                "qualifier": group[0].statement.qualifier,
            }
            arguments: dict[str, object] = {"claim_ids": list(identities)}
            if discriminator is not None:
                detail["suggested_qualifier_field"] = discriminator
                arguments["qualifier_field"] = discriminator
            items.append(
                _item(
                    severity="blocking",
                    reason="claim_conflicted",
                    subject_identity=subject,
                    related_identities=identities,
                    detail=detail,
                    repair=PlaybillNextRepairV1(
                        operation="playbill.authoring.create",
                        target=subject,
                        required_change="revise_claims_into_distinct_qualifiers",
                        arguments=arguments,
                    ),
                )
            )
            continue
        for claim in group:
            verdict = service_evaluate_playbill_claim_verdict(
                instance,
                claim_identity=claim.identity.qualified,
                evaluation_time=evaluation_time,
                at=coordinate,
            ).verdict
            if verdict.verdict == "stale_evidence":
                expirations = (
                    verdict.freshness_expirations
                    if isinstance(verdict, ClaimVerdictResultV2)
                    else ()
                )
                items.append(
                    _item(
                        severity="repair",
                        reason="claim_stale_evidence",
                        subject_identity=claim.identity.qualified,
                        related_identities=(subject,),
                        detail={
                            "expired_capture_digests": [
                                item.capture_digest
                                for item in expirations
                                if evaluation_time >= item.expires_at
                            ],
                            "predicate": claim.statement.predicate,
                            "verdict": verdict.verdict,
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.authoring.bind",
                            target=claim.identity.qualified,
                            required_change="recapture_expired_evidence",
                            arguments={"claim_id": claim.identity.name},
                        ),
                    )
                )
                continue
            if isinstance(verdict, ClaimVerdictResultV2) and verdict.verdict in {
                "supported",
                "contradicted",
                "unresolved",
            }:
                lead_end = evaluation_time + timedelta(microseconds=expiring_within.microseconds)
                expiring = tuple(
                    item
                    for item in verdict.freshness_expirations
                    if evaluation_time < item.expires_at <= lead_end
                )
                if expiring:
                    items.append(
                        _item(
                            severity="warning",
                            reason="evidence_expiring",
                            subject_identity=claim.identity.qualified,
                            related_identities=(subject,),
                            detail={
                                "expirations": [item.model_dump(mode="json") for item in expiring],
                                "predicate": claim.statement.predicate,
                            },
                            repair=PlaybillNextRepairV1(
                                operation="playbill.authoring.bind",
                                target=claim.identity.qualified,
                                required_change="recapture_expiring_evidence",
                                arguments={"claim_id": claim.identity.name},
                            ),
                        )
                    )
            if verdict.verdict != "uncovered":
                continue
            items.append(
                _item(
                    severity="repair",
                    reason="claim_uncovered",
                    subject_identity=claim.identity.qualified,
                    related_identities=(subject,),
                    detail={
                        "currency": verdict.currency,
                        "predicate": claim.statement.predicate,
                        "verdict": verdict.verdict,
                    },
                    repair=PlaybillNextRepairV1(
                        operation="playbill.authoring.bind",
                        target=claim.identity.qualified,
                        required_change="add_admissible_evidence",
                        arguments={"claim_id": claim.identity.name},
                    ),
                )
            )
    return tuple(items)


def _qualifier_discriminator(claims: list[ClaimArtifactAny]) -> str | None:
    """Name the first field whose scalar values separate all semantic contenders."""

    contender_values: dict[bytes, Mapping[str, object]] = {}
    for claim in claims:
        if not isinstance(claim.statement.object, LiteralClaimObject):
            return None
        value = claim.statement.object.value
        if not isinstance(value, Mapping):
            return None
        contender_values.setdefault(
            canonical_bytes(claim.statement.object.model_dump(mode="json")), value
        )
    common = set.intersection(*(set(value) for value in contender_values.values()))
    ordered_fields = sorted(common, key=lambda item: item.encode("utf-8"))
    for field in ordered_fields:
        values = tuple(value[field] for value in contender_values.values())
        if not all(item is None or isinstance(item, (bool, int, str)) for item in values):
            continue
        if len({canonical_bytes(item) for item in values}) == len(values):
            return field
    return None


@dataclass(frozen=True)
class _CitationCommitment:
    commitment_digest: str
    claim_identity: str
    source_id: str | None
    source_digest: str | None
    whole_source: bool = False


@dataclass(frozen=True)
class _SourceAssociation:
    citation_id: str
    claim_identity: str
    commitment_digest: str
    source_id: str
    qualifying_publication: bool
    stale_publication: bool


def _whole_source_selection(envelope: object) -> bool:
    source = getattr(envelope, "source", None)
    if not isinstance(source, ExternalSourceReferenceV1):
        return False
    coordinate = source.coordinate
    selector = source.selector
    if not isinstance(coordinate, Mapping) or not isinstance(selector, Mapping):
        return False
    length = coordinate.get("source_byte_length")
    window = selector.get("working_selection", selector)
    if not isinstance(window, Mapping) or not isinstance(length, int) or isinstance(length, bool):
        return False
    return (
        window.get("start_byte") == 0
        and window.get("end_byte") == length
        and getattr(getattr(envelope, "commitment", None), "byte_length", None) == length
    )


def _citation_commitments(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
) -> dict[str, _CitationCommitment]:
    listed = service_list_playbill_claims(instance, at=coordinate)
    store = instance.body_store()
    access = BodyAccessContext(principal_id="playbill-next", can_read_body=True)
    result: dict[str, _CitationCommitment] = {}
    try:
        for view in listed.claims:
            claim = _claim_from_view(view)
            if claim.lifecycle.state != "live":
                continue
            for citation in claim_citation_references(claim):
                envelope = parse_capture_envelope(
                    store.read(citation.capture_digest, access=access)
                )
                source_id: str | None = None
                source_digest: str | None = None
                if (
                    isinstance(envelope.source, ExternalSourceReferenceV1)
                    and envelope.source.coordinate_type == FOREIGN_SOURCE_COORDINATE_TYPE
                    and isinstance(envelope.source.coordinate, Mapping)
                ):
                    observed_digest = envelope.source.coordinate.get("source_content_digest")
                    if isinstance(observed_digest, str):
                        try:
                            Sha256Value.from_tagged(observed_digest)
                        except ValueError:
                            pass
                        else:
                            source_id = envelope.source.source_identity
                            source_digest = observed_digest
                result[citation.citation_id] = _CitationCommitment(
                    commitment_digest=envelope.commitment.digest,
                    claim_identity=claim.identity.qualified,
                    source_id=source_id,
                    source_digest=source_digest,
                    whole_source=_whole_source_selection(envelope),
                )
    except Exception as exc:
        raise PlaybillNextAcceptedStateInvalid(
            f"{PlaybillNextAcceptedStateInvalid.code}: citation inventory is invalid"
        ) from exc
    return result


def _source_associations(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
) -> tuple[_SourceAssociation, ...]:
    """Fold historical citation pins without relying on the live-only coverage index."""

    listed = service_list_playbill_claims(instance, at=coordinate, include_retired=True)
    store = instance.body_store()
    access = BodyAccessContext(principal_id="playbill-next", can_read_body=True)
    associations: list[_SourceAssociation] = []
    verdicts: dict[str, str] = {}
    try:
        for view in listed.claims:
            claim = _claim_from_view(view)
            for reference in claim_citation_references(claim):
                envelope = parse_capture_envelope(
                    store.read(reference.capture_digest, access=access)
                )
                source = envelope.source
                if (
                    not isinstance(source, ExternalSourceReferenceV1)
                    or source.coordinate_type != FOREIGN_SOURCE_COORDINATE_TYPE
                ):
                    continue
                qualifying = (
                    isinstance(reference, ClaimCitationV1)
                    and reference.role == "copy"
                    and reference.origin == "self_published"
                )
                stale = False
                if qualifying:
                    if claim.lifecycle.state == "retired":
                        stale = True
                    else:
                        verdict = verdicts.get(claim.identity.qualified)
                        if verdict is None:
                            verdict = service_evaluate_playbill_claim_verdict(
                                instance,
                                claim_identity=claim.identity.qualified,
                                evaluation_time=evaluation_time,
                                at=coordinate,
                            ).verdict.verdict
                            verdicts[claim.identity.qualified] = verdict
                        stale = verdict == "contradicted"
                associations.append(
                    _SourceAssociation(
                        citation_id=reference.citation_id,
                        claim_identity=claim.identity.qualified,
                        commitment_digest=envelope.commitment.digest,
                        source_id=source.source_identity,
                        qualifying_publication=qualifying,
                        stale_publication=stale,
                    )
                )
    except Exception as exc:
        raise PlaybillNextAcceptedStateInvalid(
            f"{PlaybillNextAcceptedStateInvalid.code}: publication association fold is invalid"
        ) from exc
    return tuple(
        sorted(
            associations,
            key=lambda item: (
                item.source_id.encode("utf-8"),
                item.commitment_digest.encode("ascii"),
                item.claim_identity.encode("utf-8"),
                item.citation_id.encode("ascii"),
            ),
        )
    )


def _self_published_source_items(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> tuple[PlaybillNextItemV1, ...]:
    if (
        observation is None
        or observation.source_observations is None
        or not access_profile.permits("instance")
    ):
        return ()
    policy = observation.presentation_policy or PlaybillPresentationPolicyV1()
    archival = set(policy.archival_source_ids)
    observed = {
        item.source_id: item
        for item in observation.source_observations
        if isinstance(item, PlaybillNextSourceObservationV2)
        and item.scan_complete
        and not item.scan_notes
        and not item.marker_notes
    }
    associations = _source_associations(
        instance,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
    )
    grouped: dict[tuple[str, str], list[_SourceAssociation]] = defaultdict(list)
    for association in associations:
        grouped[(association.source_id, association.commitment_digest)].append(association)

    items: list[PlaybillNextItemV1] = []
    for (source_id, commitment_digest), group in sorted(
        grouped.items(), key=lambda item: (item[0][0].encode("utf-8"), item[0][1].encode("ascii"))
    ):
        source = observed.get(source_id)
        if source is None or source_id in archival:
            continue
        occurrences = tuple(
            item
            for item in source.occurrences
            if item.observed_commitment_digest == commitment_digest
        )
        if len(occurrences) != 1:
            continue
        occurrence = occurrences[0]
        if any(
            occurrence.line_overlay.start_byte < marker.end_byte
            and occurrence.line_overlay.end_byte > marker.start_byte
            for marker in source.marker_summaries
        ):
            continue
        if any(not item.qualifying_publication for item in group):
            continue
        if any(item.qualifying_publication and not item.stale_publication for item in group):
            continue
        stale = tuple(
            sorted(
                {item.claim_identity for item in group if item.stale_publication},
                key=lambda item: item.encode("utf-8"),
            )
        )
        if not stale:
            continue
        items.append(
            _item(
                severity="warning",
                reason="self_published_source_stale",
                subject_identity=source_id,
                related_identities=stale,
                detail={
                    "source_id": source_id,
                    "commitment_digest": commitment_digest,
                    "occurrence_identity_digest": occurrence.identity_digest,
                    "stale_claim_identities": list(stale),
                },
                repair=PlaybillNextRepairV1(
                    operation="playbill.authoring.create",
                    target=source_id,
                    required_change="review_self_published_passage",
                    arguments={
                        "source_id": source_id,
                        "occurrence_identity_digest": occurrence.identity_digest,
                    },
                ),
            )
        )
    return tuple(items)


def _source_citation_item(
    *,
    citation_id: str,
    commitment: _CitationCommitment,
    observed: PlaybillNextSourceObservationV1 | PlaybillNextSourceObservationV2 | None,
) -> PlaybillNextItemV1 | None:
    source_id = commitment.source_id
    captured_source_digest = commitment.source_digest
    assert source_id is not None and captured_source_digest is not None
    claim_identity = commitment.claim_identity
    unobserved = observed is None or (
        isinstance(observed, PlaybillNextSourceObservationV2) and not observed.scan_complete
    )
    if isinstance(observed, PlaybillNextSourceObservationV2) and observed.scan_complete:
        matched = any(
            item.observed_commitment_digest == commitment.commitment_digest
            for item in observed.occurrences
        )
        if commitment.whole_source:
            if observed.observed_source_digest == captured_source_digest:
                return None
        elif matched:
            return None
        elif commitment.commitment_digest not in observed.scanned_commitment_digests:
            unobserved = True
    elif observed is not None and observed.observed_source_digest == captured_source_digest:
        return None

    arguments = {
        "claim_id": claim_identity.removeprefix("Claim:"),
        "citation_id": citation_id,
        "source_id": source_id,
    }
    if unobserved:
        return _item(
            severity="warning",
            reason="citation_source_unobserved",
            subject_identity=claim_identity,
            related_identities=(citation_id,),
            detail={
                "citation_id": citation_id,
                "source_id": source_id,
                "expected_source_digest": captured_source_digest,
            },
            repair=PlaybillNextRepairV1(
                operation="playbill.authoring.bind",
                target=claim_identity,
                required_change="observe_cited_source",
                arguments=arguments,
            ),
        )
    assert observed is not None
    return _item(
        severity="repair",
        reason="citation_drifted",
        subject_identity=claim_identity,
        related_identities=(citation_id,),
        detail={
            "citation_id": citation_id,
            "source_id": source_id,
            "expected_source_digest": captured_source_digest,
            "observed_source_digest": observed.observed_source_digest,
        },
        repair=PlaybillNextRepairV1(
            operation="playbill.authoring.bind",
            target=claim_identity,
            required_change="recapture_or_revise_citation",
            arguments=arguments,
        ),
    )


def _workspace_items(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> tuple[tuple[NextDomain, ...], tuple[PlaybillNextItemV1, ...]]:
    if observation is None:
        return (), ()
    domains: list[NextDomain] = []
    items: list[PlaybillNextItemV1] = []
    if observation.floor_status is not None or observation.installed_coordinate is not None:
        domains.append("workspace_floor")
        status = observation.floor_status
        reason: NextReason | None
        if status in {"not_configured", "missing"}:
            reason = "floor_missing"
        elif status == "invalid":
            reason = "floor_invalid"
        elif observation.installed_coordinate is not None and (
            observation.installed_coordinate
            != AcceptedCoordinate.model_validate(coordinate.model_dump(mode="json"))
        ):
            reason = "floor_stale"
        elif status == "stale" and observation.installed_coordinate is None:
            reason = "floor_stale"
        else:
            reason = None
        if reason is not None:
            items.append(
                _item(
                    severity="warning" if reason != "floor_invalid" else "blocking",
                    reason=reason,
                    subject_identity=coordinate.git_oid,
                    detail={
                        "installed_coordinate": (
                            None
                            if observation.installed_coordinate is None
                            else observation.installed_coordinate.model_dump(mode="json")
                        ),
                        "reported_status": status,
                    },
                    repair=PlaybillNextRepairV1(
                        operation="playbill.floor.export",
                        target=instance.descriptor.instance_id,
                        required_change="replace_installed_floor",
                        arguments={},
                    ),
                )
            )
    if observation.drift_observations is not None:
        domains.append("workspace_sources")
        commitments = _citation_commitments(instance, coordinate=coordinate)
        for drift in observation.drift_observations:
            expected = commitments.get(drift.citation_id)
            if expected is None or expected.commitment_digest != drift.expected_commitment_digest:
                raise PlaybillNextWorkspaceObservationInvalid(
                    f"{PlaybillNextWorkspaceObservationInvalid.code}: "
                    f"citation {drift.citation_id} does not match accepted state"
                )
            if drift.observed_commitment_digest == drift.expected_commitment_digest:
                continue
            claim_identity = expected.claim_identity
            items.append(
                _item(
                    severity="repair",
                    reason="citation_drifted",
                    subject_identity=claim_identity,
                    related_identities=(drift.citation_id,),
                    detail={
                        "citation_id": drift.citation_id,
                        "expected_commitment_digest": drift.expected_commitment_digest,
                        "observed_commitment_digest": drift.observed_commitment_digest,
                    },
                    repair=PlaybillNextRepairV1(
                        operation="playbill.authoring.bind",
                        target=claim_identity,
                        required_change="recapture_or_revise_citation",
                        arguments={"citation_id": drift.citation_id},
                    ),
                )
            )
    elif observation.source_observations is not None:
        domains.append("workspace_sources")
        observed = {source.source_id: source for source in observation.source_observations}
        commitments = _citation_commitments(instance, coordinate=coordinate)
        for citation_id in sorted(commitments, key=lambda item: item.encode("ascii")):
            commitment = commitments[citation_id]
            if commitment.source_id is None or commitment.source_digest is None:
                continue
            item = _source_citation_item(
                citation_id=citation_id,
                commitment=commitment,
                observed=observed.get(commitment.source_id),
            )
            if item is not None:
                items.append(item)
    return tuple(domains), tuple(items)


def _projection_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Evaluate locally declared blocks only after every backing is visible."""

    if (
        observation is None
        or observation.source_observations is None
        or not access_profile.permits("instance")
    ):
        return ()
    sources = tuple(
        source
        for source in observation.source_observations
        if isinstance(source, PlaybillNextSourceObservationV2)
        and source.scan_complete
        and not source.marker_notes
        and source.marker_summaries
    )
    if not sources:
        return ()

    facts = build_accepted_query_facts(instance, coordinate=coordinate)
    subjects = {subject.path: subject for subject in facts.subjects}
    providers = {provider.identity.qualified: provider for provider in facts.providers}
    claims = {row.accepted.claim.identity.qualified: row for row in facts.claims}
    items: list[PlaybillNextItemV1] = []
    for source in sources:
        for marker in source.marker_summaries:
            visible = True
            stale: list[str] = []
            for backing in marker.stamp.backing:
                if isinstance(backing, ProjectionClaimBackingV1):
                    claim = claims.get(backing.identity.qualified)
                    if claim is None or (
                        claim_row_visibility(
                            claim,
                            subject=subjects.get(claim.subject_path),
                            providers=providers,
                            policy=_PROJECTION_VISIBILITY_POLICY,
                            evaluation_time=evaluation_time,
                        )
                        is None
                    ):
                        visible = False
                        break
                    if claim.accepted.statement_digest != backing.statement_digest:
                        stale.append(backing.identity.qualified)
                elif isinstance(backing, ProjectionQueryBackingV1):
                    try:
                        definition = accepted_query_definition(
                            instance,
                            name=backing.identity.name,
                            coordinate=coordinate,
                        )
                        result = evaluate_claim_query(
                            definition,
                            facts=facts,
                            coordinate=coordinate,
                            evaluation_time=evaluation_time,
                            parameters={
                                item.name: item.value
                                for item in backing.resolved_parameter_bindings
                            },
                        )
                    except (PlaybillError, ValueError):
                        visible = False
                        break
                    if result.verdict != "completed" or result.truncation.clipped_budgets:
                        visible = False
                        break
                    if projection_query_semantic_result_digest(result) != (
                        backing.semantic_result_digest
                    ):
                        stale.append(backing.identity.qualified)

            if not visible:
                continue
            target = f"{source.source_id}#{marker.stamp.block_id}"
            identities = tuple(
                sorted(
                    (backing.identity.qualified for backing in marker.stamp.backing),
                    key=lambda value: value.encode("utf-8"),
                )
            )
            arguments = {"source_id": source.source_id, "block_id": marker.stamp.block_id}
            if marker.observed_body_digest != marker.stamp.body_digest:
                items.append(
                    _item(
                        severity="repair",
                        reason="projection_dirty",
                        subject_identity=target,
                        related_identities=identities,
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                            "expected_body_digest": marker.stamp.body_digest,
                            "observed_body_digest": marker.observed_body_digest,
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.block.repin",
                            target=target,
                            required_change="verify_alignment_then_repin_or_edit",
                            arguments=arguments,
                        ),
                    )
                )
            if stale:
                related = tuple(sorted(stale, key=lambda value: value.encode("utf-8")))
                items.append(
                    _item(
                        severity="repair",
                        reason="projection_backing_stale",
                        subject_identity=target,
                        related_identities=related,
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                            "stale_backings": list(related),
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.block.repin",
                            target=target,
                            required_change="review_block_supersede_prose_then_repin",
                            arguments=arguments,
                        ),
                    )
                )
    return tuple(items)


def service_playbill_next(
    instance: PlaybillInstance,
    *,
    request: PlaybillNextRequestV1,
) -> PlaybillNextResultV1:
    """Fold accepted state and explicit client observations into one repair queue."""

    coordinate = _resolve_coordinate(instance, request.at)
    public_coordinate = PlaybillAcceptedCoordinate.from_internal(coordinate)
    workspace_domains, workspace_items = _workspace_items(
        instance,
        coordinate=public_coordinate,
        observation=request.workspace_observation,
    )
    observed = tuple(
        domain
        for domain in _ALL_DOMAINS
        if domain == "accepted_state" or domain in workspace_domains
    )
    unobserved = tuple(domain for domain in _ALL_DOMAINS if domain not in observed)
    items = tuple(
        sorted(
            (
                *_claim_items(
                    instance,
                    coordinate=public_coordinate,
                    evaluation_time=request.evaluation_time,
                    expiring_within=request.expiring_within,
                ),
                *workspace_items,
                *_projection_items(
                    instance,
                    coordinate=coordinate,
                    evaluation_time=request.evaluation_time,
                    access_profile=request.access_profile,
                    observation=request.workspace_observation,
                ),
                *_self_published_source_items(
                    instance,
                    coordinate=public_coordinate,
                    evaluation_time=request.evaluation_time,
                    access_profile=request.access_profile,
                    observation=request.workspace_observation,
                ),
            ),
            key=_item_sort_key,
        )
    )
    values = {
        "coordinate": public_coordinate,
        "evaluation_time": request.evaluation_time,
        "observed_domains": observed,
        "unobserved_domains": unobserved,
        "items": items,
    }
    provisional = PlaybillNextResultV1.model_construct(
        _fields_set=None,
        result_digest="sha256:" + "0" * 64,
        coordinate=public_coordinate,
        evaluation_time=request.evaluation_time,
        observed_domains=observed,
        unobserved_domains=unobserved,
        items=items,
    )
    return PlaybillNextResultV1.model_validate(
        {**values, "result_digest": playbill_next_result_digest(provisional)}
    )


__all__ = [
    "DEFAULT_EXPIRING_WITHIN_MICROSECONDS",
    "NEXT_ITEM_ID_DOMAIN",
    "NEXT_RESULT_DIGEST_DOMAIN",
    "PlaybillNextAccessProfileInvalid",
    "PlaybillNextAcceptedStateInvalid",
    "PlaybillNextCoordinateNotAccepted",
    "PlaybillNextDriftObservationV1",
    "PlaybillNextItemV1",
    "PlaybillNextRequestV1",
    "PlaybillNextResultV1",
    "PlaybillNextSourceObservationV1",
    "PlaybillNextSourceObservationV2",
    "PlaybillNextWorkspaceObservationInvalid",
    "PlaybillNextWorkspaceObservationV1",
    "playbill_next_item_id",
    "playbill_next_result_digest",
    "service_playbill_next",
    "validate_playbill_next_request",
]
