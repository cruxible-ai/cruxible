"""Settled-only immutable Procedure calibration production laws."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillCasError, PlaybillExecutionError
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.procedures.calibration import (
    ProcedureCalibrationCohortMembershipWitnessV1,
    ProcedureCalibrationReadingV1,
    ProcedureCalibrationRelationCohortWitnessV1,
    ProcedureCalibrationWitnessError,
    build_procedure_calibration_cohort,
    load_procedure_calibration_reading,
    procedure_calibration_cohort_membership_witness_digest,
    procedure_calibration_reading_digest,
    produce_procedure_calibration_reading,
    store_procedure_calibration_reading,
)
from cruxible_core.playbill.procedures.resolution import (
    append_procedure_resolution,
    resolution_contract_partition_id,
)
from cruxible_core.playbill.procedures.settled_outcomes import (
    SettledOutcomesAccessProfileV1,
    SettledOutcomesQueryRequestV1,
    query_settled_outcomes,
)
from tests.test_playbill.test_resolution_contracts import NOW, _coordinate, _digest
from tests.test_playbill.test_settled_outcomes import (
    _activation_v2,
    _resolution_v2,
    _store,
)


def _query(tmp_path, *, with_settlements: bool = True):
    activations = tuple(
        sorted(
            (_activation_v2("calibration-a"), _activation_v2("calibration-b")),
            key=lambda item: item.contract_id.encode("utf-8"),
        )
    )
    journal, bodies, stream, writer = _store(tmp_path, activations)
    resolutions = tuple(
        _resolution_v2(activation, label, outcome=outcome)
        for activation, label, outcome in zip(
            activations,
            ("calibration-first", "calibration-second"),
            (True, False),
            strict=True,
        )
    )
    if with_settlements:
        for activation, resolution in zip(activations, resolutions, strict=True):
            append_procedure_resolution(
                writer,
                activation=activation,
                resolution=resolution,
                stream=stream,
            )
    records = {
        resolution_contract_partition_id(activation): journal.all_records(
            stream,
            resolution_contract_partition_id(activation),
        )
        for activation in activations
    }
    proof_digests = tuple(sorted(resolution.evidence_refs[0].digest for resolution in resolutions))
    request = SettledOutcomesQueryRequestV1(
        accepted_coordinate=_coordinate("calibration-query"),
        evaluation_time=NOW + timedelta(seconds=4),
        access_profile=SettledOutcomesAccessProfileV1(
            profile_id="calibration-production",
            can_read_resolution_bodies=True,
            visible_proof_digests=proof_digests,
        ),
    )
    result, receipt = query_settled_outcomes(
        request,
        activations=activations,
        records_by_partition=records,
        bodies=bodies,
    )
    return activations, bodies, result, receipt


def _cohort(procedure_digest: str, *implementations: str):
    return build_procedure_calibration_cohort(
        procedure_artifact_digest=procedure_digest,
        provider_implementation_digests=tuple(sorted(implementations)),
    )


def _witness(result, cohort):
    relations = tuple(
        sorted(
            (
                ProcedureCalibrationRelationCohortWitnessV1(
                    relation_digest=row.relation_digest,
                    procedure_artifact_digest=cohort.procedure_artifact_digest,
                    provider_implementation_digests=(cohort.provider_implementation_digests),
                    run_receipt_digests=(_digest(f"run-receipt-{row.relation_digest}"),),
                )
                for row in result.rows
                if row.relation.activation.procedure_artifact_digest
                == cohort.procedure_artifact_digest
            ),
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )
    return ProcedureCalibrationCohortMembershipWitnessV1(relations=relations)


def test_reading_scores_both_settled_outcomes_and_ignores_verdict_fields(tmp_path) -> None:
    activations, _bodies, result, receipt = _query(tmp_path)
    cohort = _cohort(
        activations[0].procedure_artifact_digest,
        _digest("provider-implementation-a"),
    )

    witness = _witness(result, cohort)
    reading = produce_procedure_calibration_reading(
        result=result,
        receipt=receipt,
        cohort=cohort,
        cohort_membership_witness=witness,
    )

    assert reading is not None
    assert reading.score.settled_count == 2
    assert reading.score.settled_true_count == 1
    assert reading.score.settled_false_count == 1
    assert reading.selected_relation_digests == tuple(
        sorted(row.relation_digest for row in result.rows)
    )
    assert reading.cohort_membership_witness_digest == (
        procedure_calibration_cohort_membership_witness_digest(witness)
    )
    reading_fields = reading.model_dump(mode="json")
    assert "verdict" not in reading_fields
    assert "claim_attestation_digests" not in reading_fields


def test_empty_settled_selection_is_honest_cold_start(tmp_path) -> None:
    activations, _bodies, result, receipt = _query(tmp_path, with_settlements=False)
    cohort = _cohort(activations[0].procedure_artifact_digest)

    assert result.rows == ()
    assert (
        produce_procedure_calibration_reading(
            result=result,
            receipt=receipt,
            cohort=cohort,
            cohort_membership_witness=_witness(result, cohort),
        )
        is None
    )


def test_reading_is_exactly_pinned_to_immutable_cas_bytes(tmp_path) -> None:
    activations, bodies, result, receipt = _query(tmp_path)
    cohort = _cohort(
        activations[0].procedure_artifact_digest,
        _digest("provider-implementation-a"),
    )
    reading = produce_procedure_calibration_reading(
        result=result,
        receipt=receipt,
        cohort=cohort,
        cohort_membership_witness=_witness(result, cohort),
    )
    assert reading is not None

    artifact = store_procedure_calibration_reading(bodies, reading)
    repeated = store_procedure_calibration_reading(bodies, reading)
    loaded = load_procedure_calibration_reading(
        bodies,
        artifact,
        access=BodyAccessContext(principal_id="calibration-test", can_read_body=True),
        expected_cohort_key=cohort.cohort_key,
    )

    assert repeated == artifact
    assert loaded == reading
    assert artifact.pin.artifact_digest == procedure_calibration_reading_digest(reading)
    assert artifact.pin.target.name == reading.reading_id

    payload = reading.model_dump(mode="python")
    payload["selected_relation_digests"] = tuple(reversed(reading.selected_relation_digests))
    with pytest.raises(ValueError, match="byte-sorted"):
        ProcedureCalibrationReadingV1.model_validate(payload)


def test_g2_cohort_changes_for_provider_successor_and_never_implicitly_carries(tmp_path) -> None:
    activations, bodies, result, receipt = _query(tmp_path)
    procedure_digest = activations[0].procedure_artifact_digest
    first = _cohort(procedure_digest, _digest("provider-implementation-a"))
    successor = _cohort(procedure_digest, _digest("provider-implementation-b"))
    first_reading = produce_procedure_calibration_reading(
        result=result,
        receipt=receipt,
        cohort=first,
        cohort_membership_witness=_witness(result, first),
    )
    assert first_reading is not None

    assert first.cohort_key != successor.cohort_key
    with pytest.raises(ProcedureCalibrationWitnessError) as refused:
        produce_procedure_calibration_reading(
            result=result,
            receipt=receipt,
            cohort=successor,
            cohort_membership_witness=_witness(result, first),
        )
    assert refused.value.code == "calibration.cohort_witness_implementation_mismatch"

    artifact = store_procedure_calibration_reading(bodies, first_reading)
    with pytest.raises(PlaybillExecutionError, match="another implementation cohort"):
        load_procedure_calibration_reading(
            bodies,
            artifact,
            access=BodyAccessContext(principal_id="calibration-test", can_read_body=True),
            expected_cohort_key=successor.cohort_key,
        )


def test_reading_refuses_a_substituted_query_receipt(tmp_path) -> None:
    activations, _bodies, result, receipt = _query(tmp_path)
    cohort = _cohort(activations[0].procedure_artifact_digest)
    substituted = receipt.model_copy(update={"visible_row_count": 1})

    with pytest.raises(PlaybillExecutionError, match="exact query result"):
        produce_procedure_calibration_reading(
            result=result,
            receipt=substituted,
            cohort=cohort,
            cohort_membership_witness=_witness(result, cohort),
        )


def test_reading_refuses_missing_or_incomplete_cohort_witness(tmp_path) -> None:
    activations, _bodies, result, receipt = _query(tmp_path)
    cohort = _cohort(
        activations[0].procedure_artifact_digest,
        _digest("provider-implementation-a"),
    )

    with pytest.raises(ProcedureCalibrationWitnessError) as missing:
        produce_procedure_calibration_reading(
            result=result,
            receipt=receipt,
            cohort=cohort,
        )
    assert missing.value.code == "calibration.cohort_witness_missing"

    complete = _witness(result, cohort)
    incomplete = complete.model_copy(update={"relations": complete.relations[:1]})
    with pytest.raises(ProcedureCalibrationWitnessError) as mismatch:
        produce_procedure_calibration_reading(
            result=result,
            receipt=receipt,
            cohort=cohort,
            cohort_membership_witness=incomplete,
        )
    assert mismatch.value.code == "calibration.cohort_witness_relation_mismatch"


def test_reading_load_refuses_access_missing_body_invalid_body_and_pin_tamper(tmp_path) -> None:
    activations, bodies, result, receipt = _query(tmp_path)
    cohort = _cohort(activations[0].procedure_artifact_digest)
    reading = produce_procedure_calibration_reading(
        result=result,
        receipt=receipt,
        cohort=cohort,
        cohort_membership_witness=_witness(result, cohort),
    )
    assert reading is not None
    artifact = store_procedure_calibration_reading(bodies, reading)

    with pytest.raises(PlaybillCasError, match="access is denied"):
        load_procedure_calibration_reading(
            bodies,
            artifact,
            access=BodyAccessContext(principal_id="denied", can_read_body=False),
            expected_cohort_key=cohort.cohort_key,
        )

    with pytest.raises(PlaybillCasError, match="object is missing"):
        load_procedure_calibration_reading(
            bodies,
            artifact.model_copy(update={"body_digest": _digest("missing-reading-body")}),
            access=BodyAccessContext(principal_id="reader", can_read_body=True),
            expected_cohort_key=cohort.cohort_key,
        )

    invalid_body = bodies.store(b"{}")
    with pytest.raises(PlaybillExecutionError, match="artifact is invalid"):
        load_procedure_calibration_reading(
            bodies,
            artifact.model_copy(update={"body_digest": invalid_body.digest}),
            access=BodyAccessContext(principal_id="reader", can_read_body=True),
            expected_cohort_key=cohort.cohort_key,
        )

    with pytest.raises(PlaybillExecutionError, match="does not reproduce its pin"):
        load_procedure_calibration_reading(
            bodies,
            artifact.model_copy(
                update={
                    "pin": artifact.pin.model_copy(
                        update={"artifact_digest": _digest("substituted-reading")}
                    )
                }
            ),
            access=BodyAccessContext(principal_id="reader", can_read_body=True),
            expected_cohort_key=cohort.cohort_key,
        )


def test_g2_provider_digest_vector_must_be_sorted_and_unique() -> None:
    first = _digest("provider-a")
    second = _digest("provider-b")
    values = (first, second) if first > second else (second, first)
    with pytest.raises(ValueError, match="byte-sorted"):
        build_procedure_calibration_cohort(
            procedure_artifact_digest=_digest("procedure"),
            provider_implementation_digests=values,
        )
