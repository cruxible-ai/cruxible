"""Canonical curation identities, operational events, and replay projection.

Curation records are mechanical observations about accepted state.  They live
only in the review operational store and never become accepted artifacts or
inputs to semantic evaluation.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.curation_calibration import (
    ADMISSION_FAILURE_MINIMUM_DISTINCT_DURABLE_ATTEMPTS,
    BLOCK_CHURN_ACCEPTED_GENERATION_WINDOW,
    BLOCK_CHURN_MINIMUM_DISTINCT_BODY_DIGESTS,
    BLOCK_CHURN_MINIMUM_OBSERVED_GENERATIONS,
    DEAD_VOCABULARY_MINIMUM_ZERO_TOUCH_GENERATIONS,
    DUPLICATE_STATEMENT_MINIMUM_LIVE_CLAIM_IDENTITIES,
    FRESHNESS_MINIMUM_CHANGED_COMMITMENT_INTERVALS,
    FRESHNESS_RATIO_LOWER,
    FRESHNESS_RATIO_UPPER,
    LITERAL_SUBJECT_REFERENCE_DETECTOR_ENABLED,
    PROVENANCE_CONCENTRATED_CONTROL_COMPONENT_COUNT,
    PROVENANCE_MINIMUM_ACTIVE_WRITING_PRINCIPALS,
    PROVENANCE_MINIMUM_LIVE_SUPPORTED_CLAIMS,
    QUALIFIER_MINIMUM_DISTINCT_SUBJECT_ADDRESSES,
    RECURRING_CONFLICT_MINIMUM_UNRESOLVED_SLOTS,
)
from cruxible_core.playbill.review_operational import PlaybillReviewOperationalEventV1

CURATION_PATTERN_ID_DOMAIN = "playbill-curation-pattern-v1"
CURATION_ITEM_ID_DOMAIN = "playbill-curation-item-v1"
CURATION_OBSERVATION_ID_DOMAIN = "playbill-curation-observation-v1"
CURATION_DETECTION_EVIDENCE_DIGEST_DOMAIN = "playbill-curation-detection-evidence-v1"
CURATION_DETECTOR_LAW_DIGEST_DOMAIN = "playbill-curation-detector-law-v1"

CurationPatternKind: TypeAlias = Literal[
    "playbill.curation.recurring_conflict_per_type.v1",
    "playbill.curation.admission_failure_cluster.v1",
    "playbill.curation.freshness_drift_calibration.v1",
    "playbill.curation.provenance_concentration.v1",
    "playbill.curation.duplicate_statement_lineages.v1",
    "playbill.curation.qualifier_crystallization.v1",
    "playbill.curation.block_churn.v1",
    "playbill.curation.dead_vocabulary.v1",
    "playbill.curation.literal_subject_reference.v1",
]

CurationEvidenceKind: TypeAlias = Literal[
    "accepted_artifact",
    "accepted_member",
    "authoring_attempt",
    "block_observation",
    "capture_transition",
    "consumption_aggregate",
    "control_component",
    "proposal_attempt",
    "slot",
]

CurationCoverageOmissionReason: TypeAlias = Literal[
    "admission_record_missing",
    "admission_subject_unresolved",
    "admission_tree_unavailable",
    "block_document_association_unavailable",
    "block_observation_invalid",
    "capture_contract_identity_unresolved",
    "consumption_epoch_uninitialized",
    "drift_series_unavailable",
]

CURATION_PATTERN_KINDS: tuple[CurationPatternKind, ...] = (
    "playbill.curation.recurring_conflict_per_type.v1",
    "playbill.curation.admission_failure_cluster.v1",
    "playbill.curation.freshness_drift_calibration.v1",
    "playbill.curation.provenance_concentration.v1",
    "playbill.curation.duplicate_statement_lineages.v1",
    "playbill.curation.qualifier_crystallization.v1",
    "playbill.curation.block_churn.v1",
    "playbill.curation.dead_vocabulary.v1",
    "playbill.curation.literal_subject_reference.v1",
)

_DETECTOR_LAWS: dict[CurationPatternKind, dict[str, object]] = {
    "playbill.curation.recurring_conflict_per_type.v1": {
        "cardinality": "one",
        "slot_partition": "subject+predicate+qualifier",
        "minimum_unresolved_slots": RECURRING_CONFLICT_MINIMUM_UNRESOLVED_SLOTS,
    },
    "playbill.curation.admission_failure_cluster.v1": {
        "minimum_distinct_durable_attempts": (ADMISSION_FAILURE_MINIMUM_DISTINCT_DURABLE_ATTEMPTS),
        "discriminators": ["diagnostic_code", "refusal_direction"],
    },
    "playbill.curation.freshness_drift_calibration.v1": {
        "minimum_changed_commitment_intervals": FRESHNESS_MINIMUM_CHANGED_COMMITMENT_INTERVALS,
        "inclusive_ratio_lower_numerator": FRESHNESS_RATIO_LOWER.numerator,
        "inclusive_ratio_lower_denominator": FRESHNESS_RATIO_LOWER.denominator,
        "inclusive_ratio_upper_numerator": FRESHNESS_RATIO_UPPER.numerator,
        "inclusive_ratio_upper_denominator": FRESHNESS_RATIO_UPPER.denominator,
    },
    "playbill.curation.provenance_concentration.v1": {
        "minimum_live_supported_claims": PROVENANCE_MINIMUM_LIVE_SUPPORTED_CLAIMS,
        "minimum_active_writing_principals": PROVENANCE_MINIMUM_ACTIVE_WRITING_PRINCIPALS,
        "effective_supporting_control_components": (
            PROVENANCE_CONCENTRATED_CONTROL_COMPONENT_COUNT
        ),
    },
    "playbill.curation.duplicate_statement_lineages.v1": {
        "minimum_distinct_claim_identities": DUPLICATE_STATEMENT_MINIMUM_LIVE_CLAIM_IDENTITIES,
        "comparison": "exact_claim_statement_digest_across_live_claims",
    },
    "playbill.curation.qualifier_crystallization.v1": {
        "minimum_distinct_subject_addresses": QUALIFIER_MINIMUM_DISTINCT_SUBJECT_ADDRESSES,
        "comparison": "exact_non_null_qualifier",
    },
    "playbill.curation.block_churn.v1": {
        "minimum_distinct_observed_body_digests": BLOCK_CHURN_MINIMUM_DISTINCT_BODY_DIGESTS,
        "minimum_observed_generations": BLOCK_CHURN_MINIMUM_OBSERVED_GENERATIONS,
        "accepted_generation_window": BLOCK_CHURN_ACCEPTED_GENERATION_WINDOW,
    },
    "playbill.curation.dead_vocabulary.v1": {
        "minimum_zero_touch_generations": DEAD_VOCABULARY_MINIMUM_ZERO_TOUCH_GENERATIONS,
        "artifact_families": ["ClaimType", "Procedure", "QueryDefinition", "Subject"],
    },
    "playbill.curation.literal_subject_reference.v1": {
        "enabled": LITERAL_SUBJECT_REFERENCE_DETECTOR_ENABLED,
        "matching": "exact_live_subject_id_equality",
        "object_kind": "literal",
        "retired_subjects": "excluded",
    },
}


class _StrictCurationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CurationCoverageOmissionV1(_StrictCurationModel):
    reason: CurationCoverageOmissionReason
    count: int = Field(ge=1)


class CurationDetectorCoverageV1(_StrictCurationModel):
    tag: Literal["playbill-curation-detector-coverage-v1"] = (
        "playbill-curation-detector-coverage-v1"
    )
    pattern_kind: CurationPatternKind
    status: Literal["complete", "partial"]
    evaluated_fact_count: int = Field(ge=0)
    omissions: tuple[CurationCoverageOmissionV1, ...] = ()

    @field_validator("omissions")
    @classmethod
    def _omissions(
        cls, value: tuple[CurationCoverageOmissionV1, ...]
    ) -> tuple[CurationCoverageOmissionV1, ...]:
        keys = tuple(item.reason for item in value)
        if keys != tuple(sorted(set(keys), key=lambda item: item.encode("utf-8"))):
            raise ValueError("curation coverage omissions must be byte-sorted and unique")
        return value

    @model_validator(mode="after")
    def _status(self) -> CurationDetectorCoverageV1:
        if (self.status == "partial") != bool(self.omissions):
            raise ValueError("curation detector coverage status disagrees with omissions")
        return self


class CurationEvidenceRefV1(_StrictCurationModel):
    """One exact mechanically relevant record or artifact reference."""

    kind: CurationEvidenceKind
    identity: str = Field(min_length=1)
    path: str | None = None
    generation: int | None = Field(default=None, ge=0)
    artifact_digest: str | None = None
    statement_digest: str | None = None
    event_digest: str | None = None
    facts: dict[str, object] = Field(default_factory=dict)

    @field_validator("artifact_digest", "statement_digest", "event_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("facts", mode="before")
    @classmethod
    def _facts(cls, value: object) -> dict[str, object]:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):
            raise ValueError("curation evidence facts must be a canonical object")
        return {str(key): item for key, item in normalized.items()}


def _canonical_detail(value: object) -> dict[str, object]:
    normalized = normalize_canonical(value)
    if not isinstance(normalized, dict):
        raise ValueError("curation pattern detail must be a canonical object")
    return {str(key): item for key, item in normalized.items()}


def _validate_pattern_detail(pattern_kind: CurationPatternKind, detail: dict[str, object]) -> None:
    keys = set(detail)
    if pattern_kind == "playbill.curation.recurring_conflict_per_type.v1":
        if detail != {
            "cardinality": "one",
            "slot_partition": "subject+predicate+qualifier",
        }:
            raise ValueError("recurring-conflict pattern detail differs from the frozen preimage")
    elif pattern_kind == "playbill.curation.admission_failure_cluster.v1":
        if (
            keys != {"diagnostic_code", "refusal_direction"}
            or not isinstance(detail["diagnostic_code"], str)
            or detail["refusal_direction"] not in {"payload_side", "schema_side", "unclassified"}
        ):
            raise ValueError(
                "admission-failure pattern detail requires diagnostic_code and refusal_direction"
            )
    elif pattern_kind == "playbill.curation.freshness_drift_calibration.v1":
        expected = {"capture_contract_identity", "external_source_identity", "selector_type"}
        if keys != expected or not all(isinstance(detail[key], str) for key in expected):
            raise ValueError("freshness-calibration pattern detail has the wrong shape")
    elif pattern_kind == "playbill.curation.provenance_concentration.v1":
        if detail != {"basis": "effective_supporting_control_components"}:
            raise ValueError("provenance-concentration detail differs from the frozen preimage")
    elif pattern_kind == "playbill.curation.duplicate_statement_lineages.v1":
        if keys != {"statement_digest"} or not isinstance(detail["statement_digest"], str):
            raise ValueError("duplicate-statement pattern detail requires statement_digest")
        Sha256Value.from_tagged(detail["statement_digest"])
    elif pattern_kind == "playbill.curation.qualifier_crystallization.v1":
        if keys != {"qualifier"} or not isinstance(detail["qualifier"], str):
            raise ValueError("qualifier-crystallization detail requires an exact qualifier")
    elif pattern_kind == "playbill.curation.block_churn.v1":
        if keys != {"block_id", "source_id"} or not all(
            isinstance(detail[key], str) for key in keys
        ):
            raise ValueError("block-churn pattern detail has the wrong shape")
    elif pattern_kind == "playbill.curation.dead_vocabulary.v1":
        if keys != {"artifact_family"} or detail["artifact_family"] not in {
            "Subject",
            "ClaimType",
            "QueryDefinition",
            "Procedure",
        }:
            raise ValueError("dead-vocabulary pattern detail has the wrong artifact family")
    elif pattern_kind == "playbill.curation.literal_subject_reference.v1":
        expected = {"literal_value", "matching_subject_kinds", "message"}
        kinds = detail.get("matching_subject_kinds")
        if (
            keys != expected
            or not isinstance(detail["literal_value"], str)
            or not isinstance(kinds, list)
            or not kinds
            or not all(isinstance(item, str) and item for item in kinds)
            or kinds != sorted(set(kinds), key=lambda item: item.encode("utf-8"))
            or detail["message"]
            != "literal looks like a subject reference; consider a subject-valued object"
        ):
            raise ValueError("literal-subject-reference pattern detail has the wrong shape")
    else:
        raise ValueError(f"unknown curation pattern kind: {pattern_kind}")


def detector_law_digest(pattern_kind: CurationPatternKind) -> str:
    return typed_digest(
        Sha256Value,
        CURATION_DETECTOR_LAW_DIGEST_DOMAIN,
        {"pattern_kind": pattern_kind, "law": _DETECTOR_LAWS[pattern_kind]},
    ).tagged


def curation_pattern_id(
    *,
    pattern_kind: CurationPatternKind,
    subject: ArtifactIdentity,
    detail: dict[str, object],
) -> str:
    normalized = _canonical_detail(detail)
    _validate_pattern_detail(pattern_kind, normalized)
    return _curation_pattern_id_from_normalized(
        pattern_kind=pattern_kind,
        subject=subject,
        detail=normalized,
    )


def _curation_pattern_id_from_normalized(
    *,
    pattern_kind: CurationPatternKind,
    subject: ArtifactIdentity,
    detail: dict[str, object],
) -> str:
    """Reproduce an already-canonical historical preimage without current-law policy."""

    return typed_digest(
        Sha256Value,
        CURATION_PATTERN_ID_DOMAIN,
        {
            "pattern_kind": pattern_kind,
            "subject": subject.model_dump(mode="json"),
            "detail": detail,
        },
    ).tagged


def curation_item_id(*, pattern_id: str, predecessor_item_id: str | None) -> str:
    Sha256Value.from_tagged(pattern_id)
    if predecessor_item_id is not None:
        Sha256Value.from_tagged(predecessor_item_id)
    return typed_digest(
        Sha256Value,
        CURATION_ITEM_ID_DOMAIN,
        {"pattern_id": pattern_id, "predecessor_item_id": predecessor_item_id},
    ).tagged


def curation_detection_evidence_digest(
    *,
    pattern_id: str,
    detector_law_digest_value: str,
    coverage: CurationDetectorCoverageV1,
    evidence_refs: tuple[CurationEvidenceRefV1, ...],
) -> str:
    return typed_digest(
        Sha256Value,
        CURATION_DETECTION_EVIDENCE_DIGEST_DOMAIN,
        {
            "pattern_id": pattern_id,
            "detector_law_digest": detector_law_digest_value,
            "coverage": coverage.model_dump(mode="json"),
            "evidence_refs": [item.model_dump(mode="json") for item in evidence_refs],
        },
    ).tagged


def curation_observation_id(
    *, item_id: str, accepted_generation: int, detection_evidence_digest: str
) -> str:
    return typed_digest(
        Sha256Value,
        CURATION_OBSERVATION_ID_DOMAIN,
        {
            "item_id": item_id,
            "accepted_generation": accepted_generation,
            "detection_evidence_digest": detection_evidence_digest,
        },
    ).tagged


class CurationDetectionV1(_StrictCurationModel):
    pattern_kind: CurationPatternKind
    subject: ArtifactIdentity
    detail: dict[str, object]
    pattern_id: str
    detector_law_digest: str
    coverage: CurationDetectorCoverageV1
    evidence_refs: tuple[CurationEvidenceRefV1, ...]
    detection_evidence_digest: str

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> dict[str, object]:
        return _canonical_detail(value)

    @field_validator("pattern_id", "detector_law_digest", "detection_evidence_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _evidence(
        cls, value: tuple[CurationEvidenceRefV1, ...]
    ) -> tuple[CurationEvidenceRefV1, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("curation evidence references must be byte-sorted and unique")
        return value

    @model_validator(mode="after")
    def _reproduces(self) -> CurationDetectionV1:
        _validate_pattern_detail(self.pattern_kind, self.detail)
        if self.coverage.pattern_kind != self.pattern_kind:
            raise ValueError("curation detection coverage names another detector")
        if self.pattern_id != curation_pattern_id(
            pattern_kind=self.pattern_kind,
            subject=self.subject,
            detail=self.detail,
        ):
            raise ValueError("curation pattern ID does not reproduce")
        if self.detector_law_digest != detector_law_digest(self.pattern_kind):
            raise ValueError("curation detector law digest does not reproduce")
        if self.detection_evidence_digest != curation_detection_evidence_digest(
            pattern_id=self.pattern_id,
            detector_law_digest_value=self.detector_law_digest,
            coverage=self.coverage,
            evidence_refs=self.evidence_refs,
        ):
            raise ValueError("curation detection evidence digest does not reproduce")
        return self


def build_curation_detection(
    *,
    pattern_kind: CurationPatternKind,
    subject: ArtifactIdentity,
    detail: dict[str, object],
    coverage: CurationDetectorCoverageV1,
    evidence_refs: tuple[CurationEvidenceRefV1, ...],
) -> CurationDetectionV1:
    by_bytes = {canonical_bytes(item.model_dump(mode="json")): item for item in evidence_refs}
    ordered = tuple(by_bytes[key] for key in sorted(by_bytes))
    pattern_id = curation_pattern_id(
        pattern_kind=pattern_kind,
        subject=subject,
        detail=detail,
    )
    law_digest = detector_law_digest(pattern_kind)
    evidence_digest = curation_detection_evidence_digest(
        pattern_id=pattern_id,
        detector_law_digest_value=law_digest,
        coverage=coverage,
        evidence_refs=ordered,
    )
    return CurationDetectionV1(
        pattern_kind=pattern_kind,
        subject=subject,
        detail=detail,
        pattern_id=pattern_id,
        detector_law_digest=law_digest,
        coverage=coverage,
        evidence_refs=ordered,
        detection_evidence_digest=evidence_digest,
    )


class CurationPatternObservedV1(_StrictCurationModel):
    tag: Literal["playbill-curation-pattern-observed-v1"] = "playbill-curation-pattern-observed-v1"
    event_id: str
    observation_id: str
    item_id: str
    predecessor_item_id: str | None
    pattern_id: str
    pattern_kind: CurationPatternKind
    subject: ArtifactIdentity
    detail: dict[str, object]
    detector_law_digest: str
    detection_evidence_digest: str
    evidence_refs: tuple[CurationEvidenceRefV1, ...]
    coverage: CurationDetectorCoverageV1
    accepted_generation: int = Field(ge=0)

    @field_validator(
        "event_id",
        "observation_id",
        "item_id",
        "predecessor_item_id",
        "pattern_id",
        "detector_law_digest",
        "detection_evidence_digest",
    )
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> dict[str, object]:
        return _canonical_detail(value)

    @model_validator(mode="after")
    def _reproduces(self) -> CurationPatternObservedV1:
        detection = CurationDetectionV1(
            pattern_kind=self.pattern_kind,
            subject=self.subject,
            detail=self.detail,
            pattern_id=self.pattern_id,
            detector_law_digest=self.detector_law_digest,
            coverage=self.coverage,
            evidence_refs=self.evidence_refs,
            detection_evidence_digest=self.detection_evidence_digest,
        )
        if self.item_id != curation_item_id(
            pattern_id=detection.pattern_id,
            predecessor_item_id=self.predecessor_item_id,
        ):
            raise ValueError("curation item ID does not reproduce")
        expected = curation_observation_id(
            item_id=self.item_id,
            accepted_generation=self.accepted_generation,
            detection_evidence_digest=self.detection_evidence_digest,
        )
        if self.event_id != expected or self.observation_id != expected:
            raise ValueError("curation observation ID does not reproduce")
        return self


class _RecordedCurationPatternObservedV1(_StrictCurationModel):
    """Historical observation whose recorded law may predate the running detector."""

    tag: Literal["playbill-curation-pattern-observed-v1"]
    event_id: str
    observation_id: str
    item_id: str
    predecessor_item_id: str | None
    pattern_id: str
    pattern_kind: CurationPatternKind
    subject: ArtifactIdentity
    detail: dict[str, object]
    detector_law_digest: str
    detection_evidence_digest: str
    evidence_refs: tuple[CurationEvidenceRefV1, ...]
    coverage: CurationDetectorCoverageV1
    accepted_generation: int = Field(ge=0)

    @field_validator(
        "event_id",
        "observation_id",
        "item_id",
        "predecessor_item_id",
        "pattern_id",
        "detector_law_digest",
        "detection_evidence_digest",
    )
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> dict[str, object]:
        return _canonical_detail(value)

    @field_validator("evidence_refs")
    @classmethod
    def _evidence(
        cls, value: tuple[CurationEvidenceRefV1, ...]
    ) -> tuple[CurationEvidenceRefV1, ...]:
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("curation evidence references must be byte-sorted and unique")
        return value

    @model_validator(mode="after")
    def _reproduces_recorded_bytes(self) -> _RecordedCurationPatternObservedV1:
        if self.coverage.pattern_kind != self.pattern_kind:
            raise ValueError("curation detection coverage names another detector")
        if self.pattern_id != _curation_pattern_id_from_normalized(
            pattern_kind=self.pattern_kind,
            subject=self.subject,
            detail=self.detail,
        ):
            raise ValueError("recorded curation pattern ID does not reproduce")
        if self.detection_evidence_digest != curation_detection_evidence_digest(
            pattern_id=self.pattern_id,
            detector_law_digest_value=self.detector_law_digest,
            coverage=self.coverage,
            evidence_refs=self.evidence_refs,
        ):
            raise ValueError("recorded curation detection evidence digest does not reproduce")
        if self.item_id != curation_item_id(
            pattern_id=self.pattern_id,
            predecessor_item_id=self.predecessor_item_id,
        ):
            raise ValueError("recorded curation item ID does not reproduce")
        expected = curation_observation_id(
            item_id=self.item_id,
            accepted_generation=self.accepted_generation,
            detection_evidence_digest=self.detection_evidence_digest,
        )
        if self.event_id != expected or self.observation_id != expected:
            raise ValueError("recorded curation observation ID does not reproduce")
        return self


def _parse_pattern_observation(
    payload: dict[str, object],
) -> CurationPatternObservedV1:
    try:
        return CurationPatternObservedV1.model_validate(payload)
    except ValueError as current_error:
        try:
            recorded = _RecordedCurationPatternObservedV1.model_validate(payload)
        except ValueError:
            raise current_error
        if recorded.detector_law_digest == detector_law_digest(recorded.pattern_kind):
            raise current_error
        return CurationPatternObservedV1.model_construct(
            **{
                field_name: getattr(recorded, field_name)
                for field_name in CurationPatternObservedV1.model_fields
            }
        )


def build_pattern_observation(
    *,
    detection: CurationDetectionV1,
    predecessor_item_id: str | None,
    accepted_generation: int,
) -> CurationPatternObservedV1:
    item_id = curation_item_id(
        pattern_id=detection.pattern_id,
        predecessor_item_id=predecessor_item_id,
    )
    observation_id = curation_observation_id(
        item_id=item_id,
        accepted_generation=accepted_generation,
        detection_evidence_digest=detection.detection_evidence_digest,
    )
    return CurationPatternObservedV1(
        event_id=observation_id,
        observation_id=observation_id,
        item_id=item_id,
        predecessor_item_id=predecessor_item_id,
        pattern_id=detection.pattern_id,
        pattern_kind=detection.pattern_kind,
        subject=detection.subject,
        detail=detection.detail,
        detector_law_digest=detection.detector_law_digest,
        detection_evidence_digest=detection.detection_evidence_digest,
        evidence_refs=detection.evidence_refs,
        coverage=detection.coverage,
        accepted_generation=accepted_generation,
    )


class CurationAffectedMemberV1(_StrictCurationModel):
    path: str
    disposition: Literal["create", "replace", "retire", "delete"]
    predecessor_artifact_digest: str | None = None
    candidate_artifact_digest: str | None = None

    @field_validator("predecessor_artifact_digest", "candidate_artifact_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value


class _CurationLifecycleEvent(_StrictCurationModel):
    event_id: str
    item_id: str
    expected_latest_event_digest: str
    actor_principal_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    attribution_refs: tuple[str, ...] = ()

    @field_validator("event_id", "item_id", "expected_latest_event_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("attribution_refs")
    @classmethod
    def _refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("curation attribution refs must be byte-sorted and unique")
        return value


def _lifecycle_event_id(domain: str, event: BaseModel) -> str:
    payload = event.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("event_id")
    return typed_digest(Sha256Value, domain, payload).tagged


class CurationOverruledV1(_CurationLifecycleEvent):
    tag: Literal["playbill-curation-overruled-v1"] = "playbill-curation-overruled-v1"

    @model_validator(mode="after")
    def _reproduces(self) -> CurationOverruledV1:
        if self.event_id != _lifecycle_event_id(self.tag, self):
            raise ValueError("curation overrule event ID does not reproduce")
        return self


class CurationAcceptedFixedV1(_CurationLifecycleEvent):
    tag: Literal["playbill-curation-accepted-fixed-v1"] = "playbill-curation-accepted-fixed-v1"
    accepted_proposal_id: str
    accepted_changeset_digest: str
    resolved_generation: int = Field(ge=1)
    affected_members: tuple[CurationAffectedMemberV1, ...]

    @field_validator("accepted_proposal_id", "accepted_changeset_digest")
    @classmethod
    def _accepted_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("affected_members")
    @classmethod
    def _members(
        cls, value: tuple[CurationAffectedMemberV1, ...]
    ) -> tuple[CurationAffectedMemberV1, ...]:
        paths = tuple(item.path for item in value)
        if not paths or paths != tuple(sorted(set(paths), key=lambda item: item.encode("utf-8"))):
            raise ValueError("curation affected members must be nonempty, sorted, and unique")
        return value

    @model_validator(mode="after")
    def _reproduces(self) -> CurationAcceptedFixedV1:
        if self.event_id != _lifecycle_event_id(self.tag, self):
            raise ValueError("curation accepted-fixed event ID does not reproduce")
        return self


class CurationSuppressedV1(_CurationLifecycleEvent):
    tag: Literal["playbill-curation-suppressed-v1"] = "playbill-curation-suppressed-v1"
    scope: Literal["item", "pattern", "instance"]
    until_generation: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _reproduces(self) -> CurationSuppressedV1:
        if self.event_id != _lifecycle_event_id(self.tag, self):
            raise ValueError("curation suppression event ID does not reproduce")
        return self


def build_curation_overruled(
    *,
    item_id: str,
    expected_latest_event_digest: str,
    actor_principal_id: str,
    reason: str,
    attribution_refs: tuple[str, ...] = (),
) -> CurationOverruledV1:
    draft = CurationOverruledV1.model_construct(
        tag="playbill-curation-overruled-v1",
        event_id="sha256:" + "0" * 64,
        item_id=item_id,
        expected_latest_event_digest=expected_latest_event_digest,
        actor_principal_id=actor_principal_id,
        reason=reason,
        attribution_refs=attribution_refs,
    )
    return CurationOverruledV1(
        event_id=_lifecycle_event_id(draft.tag, draft),
        item_id=item_id,
        expected_latest_event_digest=expected_latest_event_digest,
        actor_principal_id=actor_principal_id,
        reason=reason,
        attribution_refs=attribution_refs,
    )


def build_curation_accepted_fixed(
    *,
    item_id: str,
    expected_latest_event_digest: str,
    actor_principal_id: str,
    reason: str,
    accepted_proposal_id: str,
    accepted_changeset_digest: str,
    resolved_generation: int,
    affected_members: tuple[CurationAffectedMemberV1, ...],
    attribution_refs: tuple[str, ...] = (),
) -> CurationAcceptedFixedV1:
    draft = CurationAcceptedFixedV1.model_construct(
        tag="playbill-curation-accepted-fixed-v1",
        event_id="sha256:" + "0" * 64,
        item_id=item_id,
        expected_latest_event_digest=expected_latest_event_digest,
        actor_principal_id=actor_principal_id,
        reason=reason,
        accepted_proposal_id=accepted_proposal_id,
        accepted_changeset_digest=accepted_changeset_digest,
        resolved_generation=resolved_generation,
        affected_members=affected_members,
        attribution_refs=attribution_refs,
    )
    return CurationAcceptedFixedV1(
        event_id=_lifecycle_event_id(draft.tag, draft),
        item_id=item_id,
        expected_latest_event_digest=expected_latest_event_digest,
        actor_principal_id=actor_principal_id,
        reason=reason,
        accepted_proposal_id=accepted_proposal_id,
        accepted_changeset_digest=accepted_changeset_digest,
        resolved_generation=resolved_generation,
        affected_members=affected_members,
        attribution_refs=attribution_refs,
    )


def build_curation_suppressed(
    *,
    item_id: str,
    expected_latest_event_digest: str,
    actor_principal_id: str,
    reason: str,
    scope: Literal["item", "pattern", "instance"],
    until_generation: int | None,
    attribution_refs: tuple[str, ...] = (),
) -> CurationSuppressedV1:
    draft = CurationSuppressedV1.model_construct(
        tag="playbill-curation-suppressed-v1",
        event_id="sha256:" + "0" * 64,
        item_id=item_id,
        expected_latest_event_digest=expected_latest_event_digest,
        actor_principal_id=actor_principal_id,
        reason=reason,
        attribution_refs=attribution_refs,
        scope=scope,
        until_generation=until_generation,
    )
    return CurationSuppressedV1(
        event_id=_lifecycle_event_id(draft.tag, draft),
        item_id=item_id,
        expected_latest_event_digest=expected_latest_event_digest,
        actor_principal_id=actor_principal_id,
        reason=reason,
        attribution_refs=attribution_refs,
        scope=scope,
        until_generation=until_generation,
    )


CurationOperationalPayload = Annotated[
    CurationPatternObservedV1
    | CurationOverruledV1
    | CurationAcceptedFixedV1
    | CurationSuppressedV1,
    Field(discriminator="tag"),
]
_CURATION_PAYLOAD_ADAPTER: TypeAdapter[CurationOperationalPayload] = TypeAdapter(
    CurationOperationalPayload
)


class CurationSuppressionV1(_StrictCurationModel):
    event_id: str
    scope: Literal["item", "pattern", "instance"]
    until_generation: int | None = Field(default=None, ge=0)
    reason: str
    actor_principal_id: str


class CurationItemV1(_StrictCurationModel):
    tag: Literal["playbill-curation-item-v1"] = "playbill-curation-item-v1"
    item_id: str
    predecessor_item_id: str | None
    pattern_id: str
    pattern_kind: CurationPatternKind
    subject: ArtifactIdentity
    detail: dict[str, object]
    detector_law_digest: str
    status: Literal["open", "quarantined", "overruled", "accepted_fixed"]
    first_proposed_generation: int = Field(ge=0)
    last_observed_generation: int = Field(ge=0)
    resolved_at_generation: int | None = Field(default=None, ge=0)
    observation_count: int = Field(ge=1)
    latest_detection_evidence_digest: str
    latest_evidence_refs: tuple[CurationEvidenceRefV1, ...]
    latest_coverage: CurationDetectorCoverageV1
    latest_event_digest: str
    suppressions: tuple[CurationSuppressionV1, ...] = ()
    accepted_proposal_id: str | None = None
    accepted_changeset_digest: str | None = None
    quarantine_reason: Literal["detector_law_unreproducible"] | None = None
    current_detector_law_digest: str | None = None

    @field_validator(
        "item_id",
        "predecessor_item_id",
        "pattern_id",
        "detector_law_digest",
        "latest_detection_evidence_digest",
        "latest_event_digest",
        "accepted_proposal_id",
        "accepted_changeset_digest",
        "current_detector_law_digest",
    )
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _quarantine_shape(self) -> CurationItemV1:
        quarantined_law = self.quarantine_reason is not None
        if quarantined_law != (self.current_detector_law_digest is not None):
            raise ValueError("curation quarantine fields must appear together")
        if self.status == "quarantined" and not quarantined_law:
            raise ValueError("quarantined curation item must name its detector-law mismatch")
        if quarantined_law and self.current_detector_law_digest == self.detector_law_digest:
            raise ValueError("curation quarantine requires distinct recorded and current laws")
        return self

    def suppressed_at(self, generation: int, *, all_items: tuple[CurationItemV1, ...]) -> bool:
        for owner in all_items:
            for suppression in owner.suppressions:
                if (
                    suppression.until_generation is not None
                    and generation > suppression.until_generation
                ):
                    continue
                if suppression.scope == "instance":
                    return True
                if suppression.scope == "pattern" and owner.pattern_id == self.pattern_id:
                    return True
                if suppression.scope == "item" and owner.item_id == self.item_id:
                    return True
        return False


def parse_curation_payload(payload: dict[str, object]) -> CurationOperationalPayload:
    if payload.get("tag") == "playbill-curation-pattern-observed-v1":
        return _parse_pattern_observation(payload)
    return _CURATION_PAYLOAD_ADAPTER.validate_python(payload)


def replay_curation_items(
    events: tuple[tuple[PlaybillReviewOperationalEventV1, dict[str, object]], ...],
) -> tuple[CurationItemV1, ...]:
    grouped: dict[
        str, list[tuple[PlaybillReviewOperationalEventV1, CurationOperationalPayload]]
    ] = defaultdict(list)
    for event, raw in events:
        payload = parse_curation_payload(raw)
        grouped[payload.item_id].append((event, payload))

    projected: list[CurationItemV1] = []
    for item_id in sorted(grouped, key=lambda item: item.encode("ascii")):
        rows = sorted(grouped[item_id], key=lambda item: item[0].sequence)
        if tuple(row[0].sequence for row in rows) != tuple(range(len(rows))):
            raise ValueError("curation item event sequence is not contiguous")
        first_payload = rows[0][1]
        if not isinstance(first_payload, CurationPatternObservedV1):
            raise ValueError("curation item must begin with a detector observation")
        current_law_digest = detector_law_digest(first_payload.pattern_kind)
        quarantined_law = first_payload.detector_law_digest != current_law_digest
        observations = 0
        last_observation = first_payload
        status: Literal["open", "quarantined", "overruled", "accepted_fixed"] = (
            "quarantined" if quarantined_law else "open"
        )
        resolved_at: int | None = None
        suppressions: list[CurationSuppressionV1] = []
        accepted_proposal_id: str | None = None
        accepted_changeset_digest: str | None = None
        for event, payload in rows:
            if payload.item_id != item_id:
                raise ValueError("curation payload names another partition item")
            if isinstance(payload, CurationPatternObservedV1):
                if status not in {"open", "quarantined"}:
                    raise ValueError("resolved curation item received another observation")
                if (
                    payload.pattern_id != first_payload.pattern_id
                    or payload.pattern_kind != first_payload.pattern_kind
                    or payload.subject != first_payload.subject
                    or payload.detail != first_payload.detail
                    or payload.predecessor_item_id != first_payload.predecessor_item_id
                    or payload.detector_law_digest != first_payload.detector_law_digest
                ):
                    raise ValueError("curation item observation changed stable identity")
                observations += 1
                last_observation = payload
            elif isinstance(payload, CurationSuppressedV1):
                if status not in {"open", "quarantined"}:
                    raise ValueError("resolved curation item cannot be suppressed")
                suppressions.append(
                    CurationSuppressionV1(
                        event_id=payload.event_id,
                        scope=payload.scope,
                        until_generation=payload.until_generation,
                        reason=payload.reason,
                        actor_principal_id=payload.actor_principal_id,
                    )
                )
            elif isinstance(payload, CurationOverruledV1):
                if status not in {"open", "quarantined"}:
                    raise ValueError("curation item was resolved more than once")
                status = "overruled"
                resolved_at = event.accepted_generation
            else:
                if status not in {"open", "quarantined"}:
                    raise ValueError("curation item was resolved more than once")
                status = "accepted_fixed"
                resolved_at = payload.resolved_generation
                accepted_proposal_id = payload.accepted_proposal_id
                accepted_changeset_digest = payload.accepted_changeset_digest
        projected.append(
            CurationItemV1(
                item_id=item_id,
                predecessor_item_id=first_payload.predecessor_item_id,
                pattern_id=first_payload.pattern_id,
                pattern_kind=first_payload.pattern_kind,
                subject=first_payload.subject,
                detail=first_payload.detail,
                detector_law_digest=first_payload.detector_law_digest,
                status=status,
                first_proposed_generation=first_payload.accepted_generation,
                last_observed_generation=last_observation.accepted_generation,
                resolved_at_generation=resolved_at,
                observation_count=observations,
                latest_detection_evidence_digest=last_observation.detection_evidence_digest,
                latest_evidence_refs=last_observation.evidence_refs,
                latest_coverage=last_observation.coverage,
                latest_event_digest=rows[-1][0].event_digest,
                suppressions=tuple(suppressions),
                accepted_proposal_id=accepted_proposal_id,
                accepted_changeset_digest=accepted_changeset_digest,
                quarantine_reason=("detector_law_unreproducible" if quarantined_law else None),
                current_detector_law_digest=(current_law_digest if quarantined_law else None),
            )
        )

    by_id = {item.item_id: item for item in projected}
    for item in projected:
        if item.predecessor_item_id is None:
            continue
        predecessor = by_id.get(item.predecessor_item_id)
        if (
            predecessor is None
            or predecessor.pattern_id != item.pattern_id
            or predecessor.status not in {"accepted_fixed", "overruled", "quarantined"}
        ):
            raise ValueError("curation recurrence predecessor is invalid")
    return tuple(projected)


__all__ = [
    "CURATION_DETECTION_EVIDENCE_DIGEST_DOMAIN",
    "CURATION_DETECTOR_LAW_DIGEST_DOMAIN",
    "CURATION_ITEM_ID_DOMAIN",
    "CURATION_OBSERVATION_ID_DOMAIN",
    "CURATION_PATTERN_ID_DOMAIN",
    "CURATION_PATTERN_KINDS",
    "CurationCoverageOmissionReason",
    "CurationAcceptedFixedV1",
    "CurationAffectedMemberV1",
    "CurationDetectionV1",
    "CurationDetectorCoverageV1",
    "CurationEvidenceRefV1",
    "CurationEvidenceKind",
    "CurationItemV1",
    "CurationOperationalPayload",
    "CurationOverruledV1",
    "CurationPatternKind",
    "CurationPatternObservedV1",
    "CurationSuppressedV1",
    "build_curation_detection",
    "build_curation_accepted_fixed",
    "build_curation_overruled",
    "build_curation_suppressed",
    "build_pattern_observation",
    "curation_detection_evidence_digest",
    "curation_item_id",
    "curation_observation_id",
    "curation_pattern_id",
    "detector_law_digest",
    "parse_curation_payload",
    "replay_curation_items",
]
