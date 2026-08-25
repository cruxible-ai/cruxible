"""Deterministic repair queue derived from one accepted Playbill coordinate."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
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
    LiteralClaimObject,
    claim_citation_references,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claim_slots import classify_claim_slot
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import _claim_from_view, service_list_playbill_claims
from cruxible_core.service.playbill_evidence import service_evaluate_playbill_claim_verdict

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
]
NextRepairOperation = Literal[
    "playbill.authoring.create",
    "playbill.authoring.bind",
    "playbill.claim_type.migrate",
    "playbill.floor.export",
]

_SEVERITY_RANK: dict[NextSeverity, int] = {"blocking": 0, "repair": 1, "warning": 2}
_ALL_DOMAINS: tuple[NextDomain, ...] = (
    "accepted_state",
    "workspace_floor",
    "workspace_sources",
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


class PlaybillNextWorkspaceObservationV1(_StrictNextModel):
    tag: Literal["playbill-next-workspace-observation-v1"] = (
        "playbill-next-workspace-observation-v1"
    )
    floor_status: Literal["not_configured", "missing", "current", "stale", "invalid"] | None = None
    installed_coordinate: AcceptedCoordinate | None = None
    drift_observations: tuple[PlaybillNextDriftObservationV1, ...] | None = None
    source_observations: tuple[PlaybillNextSourceObservationV1, ...] | None = None

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
        value: tuple[PlaybillNextSourceObservationV1, ...] | None,
    ) -> tuple[PlaybillNextSourceObservationV1, ...] | None:
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


def _citation_commitments(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
) -> dict[str, tuple[str, str, str | None, str | None]]:
    listed = service_list_playbill_claims(instance, at=coordinate)
    store = instance.body_store()
    access = BodyAccessContext(principal_id="playbill-next", can_read_body=True)
    result: dict[str, tuple[str, str, str | None, str | None]] = {}
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
                result[citation.citation_id] = (
                    envelope.commitment.digest,
                    claim.identity.qualified,
                    source_id,
                    source_digest,
                )
    except Exception as exc:
        raise PlaybillNextAcceptedStateInvalid(
            f"{PlaybillNextAcceptedStateInvalid.code}: citation inventory is invalid"
        ) from exc
    return result


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
            if expected is None or expected[0] != drift.expected_commitment_digest:
                raise PlaybillNextWorkspaceObservationInvalid(
                    f"{PlaybillNextWorkspaceObservationInvalid.code}: "
                    f"citation {drift.citation_id} does not match accepted state"
                )
            if drift.observed_commitment_digest == drift.expected_commitment_digest:
                continue
            claim_identity = expected[1]
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
        observed = {
            source.source_id: source.observed_source_digest
            for source in observation.source_observations
        }
        commitments = _citation_commitments(instance, coordinate=coordinate)
        for citation_id in sorted(commitments, key=lambda item: item.encode("ascii")):
            _commitment, claim_identity, source_id, captured_source_digest = commitments[
                citation_id
            ]
            if source_id is None or captured_source_digest is None:
                continue
            observed_source_digest = observed.get(source_id)
            if observed_source_digest is None:
                items.append(
                    _item(
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
                            arguments={
                                "claim_id": claim_identity.removeprefix("Claim:"),
                                "citation_id": citation_id,
                                "source_id": source_id,
                            },
                        ),
                    )
                )
            elif observed_source_digest != captured_source_digest:
                items.append(
                    _item(
                        severity="repair",
                        reason="citation_drifted",
                        subject_identity=claim_identity,
                        related_identities=(citation_id,),
                        detail={
                            "citation_id": citation_id,
                            "source_id": source_id,
                            "expected_source_digest": captured_source_digest,
                            "observed_source_digest": observed_source_digest,
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.authoring.bind",
                            target=claim_identity,
                            required_change="recapture_or_revise_citation",
                            arguments={
                                "claim_id": claim_identity.removeprefix("Claim:"),
                                "citation_id": citation_id,
                                "source_id": source_id,
                            },
                        ),
                    )
                )
    return tuple(domains), tuple(items)


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
    "PlaybillNextWorkspaceObservationInvalid",
    "PlaybillNextWorkspaceObservationV1",
    "playbill_next_item_id",
    "playbill_next_result_digest",
    "service_playbill_next",
    "validate_playbill_next_request",
]
