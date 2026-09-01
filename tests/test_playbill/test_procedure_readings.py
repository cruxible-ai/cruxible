"""Exact ProcedureReading grain and contract-grade laws."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
)
from cruxible_core.playbill.procedures.readings import (
    append_procedure_reading,
    build_procedure_reading,
    evaluate_procedure_reading,
    procedure_reading_partition_id,
)
from cruxible_core.playbill.procedures.resolution import (
    ProcedureProofReferenceV1,
    ProcedureResolutionBook,
    append_procedure_resolution,
    append_resolution_disposition,
    build_procedure_resolution,
    build_resolution_disposition,
    derive_resolution_activations,
    resolution_contract_partition_id,
)
from tests.test_playbill.test_resolution_contracts import (
    NOW,
    _accepted,
    _accepted_v4,
    _actor,
    _coordinate,
    _digest,
)


def test_graph_v4_reading_uses_v4_grain_digests() -> None:
    accepted = _accepted_v4()
    reading = build_procedure_reading(
        accepted,
        accepted_coordinate=_coordinate(),
        subject_grain="node",
        node_id="hot",
        grade="observation",
        verdict="satisfied",
        observed_at=NOW,
        recorded_at=NOW,
        actor_context=_actor(),
        value={"healthy": True},
    )

    assert reading.definition_digest == accepted.procedure.definition_digest
    assert reading.node_id == "hot"
    assert reading.node_local_digest is not None


def _writer(tmp_path, *, partition_id: str):
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    journal = LocalJournalBackend(journal_root)
    bodies = ContentAddressedBodyStore(cas_root)
    stream = JournalStreamIdentityV1(
        instance_id="instance-a",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )
    journal.activate_writer(
        stream,
        partition_id,
        fencing_token="writer",
        expected_head=journal.read_head(stream, partition_id),
    )
    writer = ProcedureExhaustWriter(
        journal=journal,
        bodies=bodies,
        fencing_token="writer",
    )
    return journal, bodies, stream, writer


def _resolved(tmp_path):
    accepted = _accepted()
    activation = derive_resolution_activations(
        accepted,
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )[0]
    resolution = build_procedure_resolution(
        activation,
        sequence=1,
        verdict="satisfied",
        value={"healthy": True},
        evidence_refs=(
            ProcedureProofReferenceV1(
                kind="query_receipt",
                digest=_digest("reading-query-receipt"),
            ),
        ),
        observed_at=NOW + timedelta(seconds=2),
        recorded_at=NOW + timedelta(seconds=3),
        actor_context=_actor(),
    )
    partition_id = resolution_contract_partition_id(activation)
    journal, bodies, stream, writer = _writer(tmp_path, partition_id=partition_id)
    append_procedure_resolution(
        writer,
        activation=activation,
        resolution=resolution,
        stream=stream,
    )
    book = ProcedureResolutionBook((activation,))
    book.replay(journal.all_records(stream, partition_id), bodies=bodies)
    return accepted, activation, resolution, book, journal, bodies, stream, writer


def test_contract_grade_requires_exact_current_resolution_and_semantic_grain(tmp_path) -> None:
    accepted, activation, resolution, book, *_rest = _resolved(tmp_path)
    reading = build_procedure_reading(
        accepted,
        accepted_coordinate=_coordinate(),
        subject_grain="arm",
        node_id="hot",
        from_node_id="gate",
        arm_label="on_true",
        grade="contract",
        activation=activation,
        resolution_id=resolution.resolution_id,
        verdict="satisfied",
        value={"healthy": True},
        observed_at=resolution.observed_at,
        recorded_at=NOW + timedelta(seconds=4),
        actor_context=_actor(),
    )

    law = evaluate_procedure_reading(
        reading,
        accepted=accepted,
        accepted_coordinate=_coordinate(),
        activations=(activation,),
        resolution_book=book,
    )
    assert law.verdict == "accepted"

    stale = reading.model_copy(update={"resolution_id": "RSR-" + "0" * 32})
    refused = evaluate_procedure_reading(
        stale,
        accepted=accepted,
        accepted_coordinate=_coordinate(),
        activations=(activation,),
        resolution_book=book,
    )
    assert refused.verdict == "refused"
    assert refused.refusal_code == "reading.current_resolution_missing"
    assert stale.grade == "contract"


def test_overturned_resolution_cannot_mint_contract_grade(tmp_path) -> None:
    accepted, activation, resolution, book, journal, bodies, stream, writer = _resolved(tmp_path)
    disposition = build_resolution_disposition(
        resolution,
        sequence=1,
        verdict="overturned",
        reviewer_actor_context=_actor("reviewer"),
        recorded_at=NOW + timedelta(seconds=4),
    )
    append_resolution_disposition(
        writer,
        activation=activation,
        resolution=resolution,
        disposition=disposition,
        stream=stream,
    )
    book.replay(
        journal.all_records(stream, resolution_contract_partition_id(activation)),
        bodies=bodies,
    )
    reading = build_procedure_reading(
        accepted,
        accepted_coordinate=_coordinate(),
        subject_grain="arm",
        node_id="hot",
        from_node_id="gate",
        arm_label="on_true",
        grade="contract",
        activation=activation,
        resolution_id=resolution.resolution_id,
        verdict="satisfied",
        value=resolution.value,
        observed_at=resolution.observed_at,
        recorded_at=NOW + timedelta(seconds=5),
        actor_context=_actor(),
    )
    law = evaluate_procedure_reading(
        reading,
        accepted=accepted,
        accepted_coordinate=_coordinate(),
        activations=(activation,),
        resolution_book=book,
    )
    assert law.verdict == "refused"
    assert law.refusal_code == "reading.current_resolution_missing"


def test_observation_grade_is_exact_but_needs_no_contract(tmp_path) -> None:
    accepted = _accepted()
    reading = build_procedure_reading(
        accepted,
        accepted_coordinate=_coordinate(),
        subject_grain="node",
        node_id="hot",
        grade="observation",
        verdict="contradicted",
        value={"reason": "wrong-arm"},
        observed_at=NOW,
        recorded_at=NOW,
        actor_context=_actor(),
        claim_attestation_digests=(_digest("attestation"),),
    )
    law = evaluate_procedure_reading(
        reading,
        accepted=accepted,
        accepted_coordinate=_coordinate(),
    )
    assert law.verdict == "accepted"
    assert reading.measurement_name is None


def test_reading_idempotency_domain_replays_and_refuses_payload_drift(tmp_path) -> None:
    accepted = _accepted()
    partition_id = procedure_reading_partition_id(accepted)
    _journal, bodies, stream, writer = _writer(tmp_path, partition_id=partition_id)
    common = dict(
        accepted_coordinate=_coordinate(),
        subject_grain="procedure_unit",
        grade="observation",
        verdict="satisfied",
        observed_at=NOW,
        recorded_at=NOW,
        actor_context=_actor(),
        idempotency_key="reading-1",
    )
    first = build_procedure_reading(accepted, value={"count": 1}, **common)
    stored = append_procedure_reading(
        writer,
        reading=first,
        accepted=accepted,
        accepted_coordinate=_coordinate(),
        stream=stream,
        bodies=bodies,
    )
    replay = append_procedure_reading(
        writer,
        reading=first,
        accepted=accepted,
        accepted_coordinate=_coordinate(),
        stream=stream,
        bodies=bodies,
    )
    assert replay == stored

    changed = build_procedure_reading(accepted, value={"count": 2}, **common)
    assert changed.reading_id == first.reading_id
    with pytest.raises(PlaybillExecutionError, match="different payload"):
        append_procedure_reading(
            writer,
            reading=changed,
            accepted=accepted,
            accepted_coordinate=_coordinate(),
            stream=stream,
            bodies=bodies,
        )
