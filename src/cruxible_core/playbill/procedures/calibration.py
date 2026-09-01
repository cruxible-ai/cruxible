"""Immutable settled-only Procedure calibration readings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    Sha256Value,
    canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedures.results import (
    ProcedureRunReceiptV2,
    ProcedureRunReceiptV3,
    ProcedureRunReceiptV4,
    ProcedureRunReceiptV5,
    ProcedureRunReceiptV6,
)
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.procedures.settled_outcomes import (
    SettledOutcomeRowV1,
    SettledOutcomesQueryReceiptV1,
    SettledOutcomesQueryResultV1,
    settled_outcomes_query_receipt_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate


class _StrictCalibrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


CalibrationWitnessRefusalCode = Literal[
    "calibration.cohort_witness_missing",
    "calibration.cohort_witness_relation_mismatch",
    "calibration.cohort_witness_procedure_mismatch",
    "calibration.cohort_witness_implementation_mismatch",
]


class ProcedureCalibrationWitnessError(PlaybillExecutionError):
    """Typed refusal for absent or non-reproducing G2 membership evidence."""

    def __init__(self, code: CalibrationWitnessRefusalCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _digest(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a tagged lowercase SHA-256 digest") from exc
    return value


class ProcedureCalibrationCohortV1(_StrictCalibrationModel):
    """Implementation-local G2 cohort; successors never inherit prior readings."""

    tag: Literal["playbill-procedure-calibration-cohort-v1"] = (
        "playbill-procedure-calibration-cohort-v1"
    )
    procedure_artifact_digest: str
    provider_implementation_digests: tuple[str, ...]
    cohort_key: str

    @field_validator(
        "procedure_artifact_digest",
        "provider_implementation_digests",
        "cohort_key",
    )
    @classmethod
    def _digests(cls, value: str | tuple[str, ...], info: object) -> str | tuple[str, ...]:
        if isinstance(value, tuple):
            for digest in value:
                _digest(digest, label="Provider implementation digest")
            if value != tuple(sorted(set(value))):
                raise ValueError("Provider implementation digests must be byte-sorted and unique")
            return value
        return _digest(value, label=str(getattr(info, "field_name", "calibration digest")))

    @model_validator(mode="after")
    def _cohort_key(self) -> "ProcedureCalibrationCohortV1":
        if self.cohort_key != procedure_calibration_cohort_key(
            self.procedure_artifact_digest,
            self.provider_implementation_digests,
        ):
            raise ValueError("Procedure calibration cohort key does not reproduce")
        return self


def procedure_calibration_cohort_key(
    procedure_artifact_digest: str,
    provider_implementation_digests: tuple[str, ...],
) -> str:
    _digest(procedure_artifact_digest, label="Procedure artifact digest")
    for digest in provider_implementation_digests:
        _digest(digest, label="Provider implementation digest")
    if provider_implementation_digests != tuple(sorted(set(provider_implementation_digests))):
        raise ValueError("Provider implementation digests must be byte-sorted and unique")
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-calibration-cohort-v1",
        {
            "procedure_artifact_digest": procedure_artifact_digest,
            "provider_implementation_digests": list(provider_implementation_digests),
        },
    ).tagged


def build_procedure_calibration_cohort(
    *,
    procedure_artifact_digest: str,
    provider_implementation_digests: tuple[str, ...],
) -> ProcedureCalibrationCohortV1:
    return ProcedureCalibrationCohortV1(
        procedure_artifact_digest=procedure_artifact_digest,
        provider_implementation_digests=provider_implementation_digests,
        cohort_key=procedure_calibration_cohort_key(
            procedure_artifact_digest,
            provider_implementation_digests,
        ),
    )


class ProcedureCalibrationRelationCohortWitnessV1(_StrictCalibrationModel):
    """Receipt-derived implementation membership for one selected relation."""

    tag: Literal["playbill-procedure-calibration-relation-cohort-witness-v1"] = (
        "playbill-procedure-calibration-relation-cohort-witness-v1"
    )
    relation_digest: str
    procedure_artifact_digest: str
    provider_implementation_digests: tuple[str, ...]
    run_receipt_digests: tuple[str, ...]

    @field_validator("relation_digest", "procedure_artifact_digest")
    @classmethod
    def _single_digest(cls, value: str, info: object) -> str:
        return _digest(value, label=str(getattr(info, "field_name", "witness digest")))

    @field_validator("provider_implementation_digests", "run_receipt_digests")
    @classmethod
    def _digest_vector(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        field_name = str(getattr(info, "field_name", "witness digest vector"))
        for digest in value:
            _digest(digest, label=field_name)
        if value != tuple(sorted(set(value))):
            raise ValueError(f"{field_name} must be byte-sorted and unique")
        if field_name == "run_receipt_digests" and not value:
            raise ValueError("run_receipt_digests must name receipt provenance")
        return value


class ProcedureCalibrationCohortMembershipWitnessV1(_StrictCalibrationModel):
    """Complete per-relation G2 membership witness for one reading production."""

    tag: Literal["playbill-procedure-calibration-cohort-membership-witness-v1"] = (
        "playbill-procedure-calibration-cohort-membership-witness-v1"
    )
    relations: tuple[ProcedureCalibrationRelationCohortWitnessV1, ...] = ()

    @field_validator("relations")
    @classmethod
    def _relations(
        cls,
        value: tuple[ProcedureCalibrationRelationCohortWitnessV1, ...],
    ) -> tuple[ProcedureCalibrationRelationCohortWitnessV1, ...]:
        keys = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        relation_digests = tuple(item.relation_digest for item in value)
        if keys != tuple(sorted(set(keys))) or len(set(relation_digests)) != len(value):
            raise ValueError("cohort witness relations must be canonically sorted and unique")
        return value


def procedure_calibration_cohort_membership_witness_digest(
    witness: ProcedureCalibrationCohortMembershipWitnessV1,
) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-calibration-cohort-membership-witness-v1",
        {"witness": witness.model_dump(mode="json")},
    ).tagged


ProcedureCalibrationRunReceiptV1 = (
    ProcedureRunReceiptV2
    | ProcedureRunReceiptV3
    | ProcedureRunReceiptV4
    | ProcedureRunReceiptV5
    | ProcedureRunReceiptV6
)


@dataclass(frozen=True)
class VerifiedProcedureCalibrationRunReceiptV1:
    """One service-verified public run receipt plus its Procedure association."""

    procedure_artifact_digest: str
    receipt_digest: str
    receipt: ProcedureCalibrationRunReceiptV1


def _public_run_receipt_digest(receipt: ProcedureCalibrationRunReceiptV1) -> str:
    domains = {
        "playbill-procedure-run-receipt-v2": "playbill-procedure-run-receipt-v2",
        "playbill-procedure-run-receipt-v3": "playbill-procedure-run-receipt-v3",
        "playbill-procedure-run-receipt-v4": "playbill-procedure-run-receipt-v4",
        "playbill-procedure-run-receipt-v5": "playbill-procedure-run-receipt-v5",
        "playbill-procedure-run-receipt-v6": "playbill-procedure-run-receipt-v6",
    }
    return typed_digest(
        Sha256Value,
        domains[receipt.tag],
        {"receipt": receipt.model_dump(mode="json")},
    ).tagged


def build_procedure_calibration_membership_witness(
    *,
    result: SettledOutcomesQueryResultV1,
    cohort: ProcedureCalibrationCohortV1,
    verified_run_receipts: Mapping[str, VerifiedProcedureCalibrationRunReceiptV1],
) -> ProcedureCalibrationCohortMembershipWitnessV1:
    """Construct membership only from exact receipts named by each settled relation."""

    relations: list[ProcedureCalibrationRelationCohortWitnessV1] = []
    rows = select_settled_outcomes_for_calibration(
        result,
        procedure_artifact_digest=cohort.procedure_artifact_digest,
    )
    for row in rows:
        run_receipt_digests = tuple(
            sorted(
                {
                    proof.digest
                    for proof in row.relation.resolution.evidence_refs
                    if proof.kind == "run_receipt"
                },
                key=lambda item: item.encode("ascii"),
            )
        )
        if not run_receipt_digests:
            raise ProcedureCalibrationWitnessError(
                "calibration.cohort_witness_missing",
                "selected relation has no run-receipt proof",
            )
        implementation_digests: set[str] = set()
        for digest in run_receipt_digests:
            evidence = verified_run_receipts.get(digest)
            if evidence is None or evidence.receipt_digest != digest:
                raise ProcedureCalibrationWitnessError(
                    "calibration.cohort_witness_missing",
                    f"run receipt {digest} is not available as verified evidence",
                )
            if _public_run_receipt_digest(evidence.receipt) != digest:
                raise ProcedureCalibrationWitnessError(
                    "calibration.cohort_witness_relation_mismatch",
                    f"run receipt {digest} bytes do not reproduce",
                )
            if evidence.procedure_artifact_digest != cohort.procedure_artifact_digest:
                raise ProcedureCalibrationWitnessError(
                    "calibration.cohort_witness_procedure_mismatch",
                    "run receipt names another Procedure artifact",
                )
            if not isinstance(evidence.receipt, ProcedureRunReceiptV4):
                raise ProcedureCalibrationWitnessError(
                    "calibration.cohort_witness_implementation_mismatch",
                    "run receipt predates implementation-bound Line receipts",
                )
            if evidence.receipt.status != "succeeded":
                raise ProcedureCalibrationWitnessError(
                    "calibration.cohort_witness_relation_mismatch",
                    "only succeeded run receipts can witness calibration membership",
                )
            implementation_digests.update(
                binding.implementation_digest
                for binding in evidence.receipt.resolved_provider_bindings
            )
        sorted_implementations = tuple(
            sorted(implementation_digests, key=lambda item: item.encode("ascii"))
        )
        if sorted_implementations != cohort.provider_implementation_digests:
            raise ProcedureCalibrationWitnessError(
                "calibration.cohort_witness_implementation_mismatch",
                "verified run receipts do not reproduce the selected implementation cohort",
            )
        relations.append(
            ProcedureCalibrationRelationCohortWitnessV1(
                relation_digest=row.relation_digest,
                procedure_artifact_digest=cohort.procedure_artifact_digest,
                provider_implementation_digests=sorted_implementations,
                run_receipt_digests=run_receipt_digests,
            )
        )
    return ProcedureCalibrationCohortMembershipWitnessV1(
        relations=tuple(
            sorted(
                relations,
                key=lambda item: canonical_bytes(item.model_dump(mode="json")),
            )
        )
    )


class ProcedureCalibrationScoreV1(_StrictCalibrationModel):
    tag: Literal["playbill-procedure-calibration-score-v1"] = (
        "playbill-procedure-calibration-score-v1"
    )
    settled_count: int = Field(ge=1)
    settled_true_count: int = Field(ge=0)
    settled_false_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _counts(self) -> "ProcedureCalibrationScoreV1":
        if self.settled_true_count + self.settled_false_count != self.settled_count:
            raise ValueError("calibration true/false counts must equal settled_count")
        return self


class ProcedureCalibrationReadingV1(_StrictCalibrationModel):
    """One immutable score over exact settled relation digests."""

    tag: Literal["playbill-procedure-calibration-reading-v1"] = (
        "playbill-procedure-calibration-reading-v1"
    )
    reading_id: str
    cohort: ProcedureCalibrationCohortV1
    accepted_coordinate: AcceptedCoordinate
    query_request_digest: str
    query_result_digest: str
    query_receipt_digest: str
    cohort_membership_witness_digest: str
    selected_relation_digests: tuple[str, ...]
    score: ProcedureCalibrationScoreV1

    @field_validator(
        "query_request_digest",
        "query_result_digest",
        "query_receipt_digest",
        "cohort_membership_witness_digest",
    )
    @classmethod
    def _digests(cls, value: str, info: object) -> str:
        return _digest(value, label=str(getattr(info, "field_name", "calibration digest")))

    @field_validator("selected_relation_digests")
    @classmethod
    def _relations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for digest in value:
            _digest(digest, label="selected settled relation digest")
        if not value or value != tuple(sorted(set(value))):
            raise ValueError(
                "selected settled relation digests must be nonempty, byte-sorted, and unique"
            )
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureCalibrationReadingV1":
        if self.score.settled_count != len(self.selected_relation_digests):
            raise ValueError("calibration score count differs from selected relation digests")
        if self.reading_id != procedure_calibration_reading_id(self):
            raise ValueError("Procedure calibration reading_id does not reproduce")
        return self


def procedure_calibration_reading_id(reading: ProcedureCalibrationReadingV1) -> str:
    payload = reading.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("reading_id")
    digest = typed_digest(
        ArtifactDigest,
        "playbill-procedure-calibration-reading-identity-v1",
        {"reading": payload},
    ).value
    return f"PCR-{digest[:32]}"


def procedure_calibration_reading_digest(reading: ProcedureCalibrationReadingV1) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-calibration-reading-v1",
        {"reading": reading.model_dump(mode="json")},
    ).tagged


def select_settled_outcomes_for_calibration(
    result: SettledOutcomesQueryResultV1,
    *,
    procedure_artifact_digest: str,
) -> tuple[SettledOutcomeRowV1, ...]:
    """Select only typed settled rows for one exact Procedure artifact."""

    _digest(procedure_artifact_digest, label="Procedure artifact digest")
    selected = tuple(
        row
        for row in result.rows
        if row.relation.activation.procedure_artifact_digest == procedure_artifact_digest
    )
    if any(
        proof.kind == "claim_attestation"
        for row in selected
        for proof in row.relation.resolution.evidence_refs
    ):
        raise PlaybillExecutionError("Claim attestations cannot enter calibration selection")
    return selected


def produce_procedure_calibration_reading(
    *,
    result: SettledOutcomesQueryResultV1,
    receipt: SettledOutcomesQueryReceiptV1,
    cohort: ProcedureCalibrationCohortV1,
    cohort_membership_witness: ProcedureCalibrationCohortMembershipWitnessV1 | None = None,
) -> ProcedureCalibrationReadingV1 | None:
    """Produce an exact reading, or honest cold start when no settled rows exist."""

    if (
        receipt.request_digest != result.request_digest
        or receipt.result_digest != result.result_digest
        or receipt.accepted_coordinate != result.accepted_coordinate
        or receipt.evaluation_time != result.evaluation_time
        or receipt.visible_row_count != len(result.rows)
    ):
        raise PlaybillExecutionError(
            "settled-outcomes receipt does not reproduce its exact query result"
        )
    rows = select_settled_outcomes_for_calibration(
        result,
        procedure_artifact_digest=cohort.procedure_artifact_digest,
    )
    if cohort_membership_witness is None:
        raise ProcedureCalibrationWitnessError(
            "calibration.cohort_witness_missing",
            "calibration reading production requires receipt-derived membership for every row",
        )
    relation_digests = tuple(sorted(row.relation_digest for row in rows))
    witnessed_relation_digests = tuple(
        sorted(item.relation_digest for item in cohort_membership_witness.relations)
    )
    if witnessed_relation_digests != relation_digests:
        raise ProcedureCalibrationWitnessError(
            "calibration.cohort_witness_relation_mismatch",
            "cohort witness must cover exactly the selected settled relations",
        )
    if any(
        item.procedure_artifact_digest != cohort.procedure_artifact_digest
        for item in cohort_membership_witness.relations
    ):
        raise ProcedureCalibrationWitnessError(
            "calibration.cohort_witness_procedure_mismatch",
            "cohort witness names another Procedure artifact",
        )
    if any(
        item.provider_implementation_digests != cohort.provider_implementation_digests
        for item in cohort_membership_witness.relations
    ):
        raise ProcedureCalibrationWitnessError(
            "calibration.cohort_witness_implementation_mismatch",
            "cohort witness names another Provider implementation vector",
        )
    if not rows:
        return None

    true_count = sum(row.relation.resolution.settlement_outcome for row in rows)
    score = ProcedureCalibrationScoreV1(
        settled_count=len(rows),
        settled_true_count=true_count,
        settled_false_count=len(rows) - true_count,
    )
    values = {
        "cohort": cohort,
        "accepted_coordinate": result.accepted_coordinate,
        "query_request_digest": result.request_digest,
        "query_result_digest": result.result_digest,
        "query_receipt_digest": settled_outcomes_query_receipt_digest(receipt),
        "cohort_membership_witness_digest": (
            procedure_calibration_cohort_membership_witness_digest(cohort_membership_witness)
        ),
        "selected_relation_digests": relation_digests,
        "score": score,
    }
    provisional = ProcedureCalibrationReadingV1.model_construct(
        reading_id="",
        **cast(dict[str, Any], values),
    )
    return ProcedureCalibrationReadingV1.model_validate(
        {
            **values,
            "reading_id": procedure_calibration_reading_id(provisional),
        }
    )


class ProcedureCalibrationReadingArtifactV1(_StrictCalibrationModel):
    """CAS locator plus an ArtifactPin for one immutable reading envelope."""

    tag: Literal["playbill-procedure-calibration-reading-artifact-v1"] = (
        "playbill-procedure-calibration-reading-artifact-v1"
    )
    pin: ArtifactPin
    body_digest: str
    cohort_key: str

    @field_validator("body_digest", "cohort_key")
    @classmethod
    def _digests(cls, value: str, info: object) -> str:
        return _digest(value, label=str(getattr(info, "field_name", "reading artifact digest")))

    @model_validator(mode="after")
    def _pin_shape(self) -> "ProcedureCalibrationReadingArtifactV1":
        if self.pin.role != "calibration-reading":
            raise ValueError("calibration reading pin has the wrong role")
        if self.pin.target.kind != "ProcedureCalibrationReading":
            raise ValueError("calibration reading pin has the wrong artifact kind")
        return self


def procedure_calibration_reading_pin(reading: ProcedureCalibrationReadingV1) -> ArtifactPin:
    return ArtifactPin(
        role="calibration-reading",
        target=ArtifactIdentity(
            kind="ProcedureCalibrationReading",
            name=reading.reading_id,
        ),
        artifact_digest=procedure_calibration_reading_digest(reading),
    )


def store_procedure_calibration_reading(
    bodies: ContentAddressedBodyStore,
    reading: ProcedureCalibrationReadingV1,
) -> ProcedureCalibrationReadingArtifactV1:
    content = canonical_bytes(reading.model_dump(mode="json"))
    metadata = bodies.store(content)
    return ProcedureCalibrationReadingArtifactV1(
        pin=procedure_calibration_reading_pin(reading),
        body_digest=metadata.digest,
        cohort_key=reading.cohort.cohort_key,
    )


def load_procedure_calibration_reading(
    bodies: ContentAddressedBodyStore,
    artifact: ProcedureCalibrationReadingArtifactV1,
    *,
    access: BodyAccessContext,
    expected_cohort_key: str,
) -> ProcedureCalibrationReadingV1:
    """Resolve only the exact pinned bytes in the caller's current G2 cohort."""

    _digest(expected_cohort_key, label="expected calibration cohort key")
    if artifact.cohort_key != expected_cohort_key:
        raise PlaybillExecutionError("calibration reading belongs to another implementation cohort")
    content = bodies.read(artifact.body_digest, access=access)
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlaybillExecutionError("calibration reading artifact is malformed") from exc
    if not isinstance(raw, dict) or canonical_bytes(raw) != content:
        raise PlaybillExecutionError("calibration reading artifact is not exact canonical bytes")
    try:
        reading = ProcedureCalibrationReadingV1.model_validate(raw)
    except ValueError as exc:
        raise PlaybillExecutionError("calibration reading artifact is invalid") from exc
    if (
        reading.reading_id != artifact.pin.target.name
        or procedure_calibration_reading_digest(reading) != artifact.pin.artifact_digest
        or reading.cohort.cohort_key != artifact.cohort_key
    ):
        raise PlaybillExecutionError("calibration reading artifact does not reproduce its pin")
    return reading


__all__ = [
    "CalibrationWitnessRefusalCode",
    "ProcedureCalibrationCohortV1",
    "ProcedureCalibrationCohortMembershipWitnessV1",
    "ProcedureCalibrationReadingArtifactV1",
    "ProcedureCalibrationReadingV1",
    "ProcedureCalibrationRelationCohortWitnessV1",
    "ProcedureCalibrationScoreV1",
    "ProcedureCalibrationWitnessError",
    "ProcedureCalibrationRunReceiptV1",
    "VerifiedProcedureCalibrationRunReceiptV1",
    "build_procedure_calibration_cohort",
    "build_procedure_calibration_membership_witness",
    "load_procedure_calibration_reading",
    "procedure_calibration_cohort_key",
    "procedure_calibration_cohort_membership_witness_digest",
    "procedure_calibration_reading_digest",
    "procedure_calibration_reading_id",
    "procedure_calibration_reading_pin",
    "produce_procedure_calibration_reading",
    "select_settled_outcomes_for_calibration",
    "store_procedure_calibration_reading",
]
