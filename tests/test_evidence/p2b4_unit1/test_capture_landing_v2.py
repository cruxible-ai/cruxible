"""Self-attacks for mixed-version Capture landing and replay."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cruxible_client.contracts.capture_journal import (
    CaptureLandingEvent,
    CaptureLandingEventV1,
    InMemoryCaptureLandingJournal,
    capture_landing_idempotency_key,
)
from cruxible_client.contracts.captures import (
    CaptureEnvelope,
    CaptureEnvelopeV1,
    CaptureRunCoordinateV1,
    build_provider_external_capture_v2,
)
from tests.core_support._pc_c_support import NOW
from tests.core_support.p2b4_unit1._support import provider_capture_fixture


def test_mixed_v1_v2_replay_from_genesis_preserves_every_event_object(
    tmp_path: Path,
) -> None:
    fixture = provider_capture_fixture(tmp_path)
    built_v2 = build_provider_external_capture_v2(
        store=fixture.store,
        contract=fixture.contract,
        result=fixture.result,
        receipt=fixture.receipt,
        occurrence=fixture.occurrence,
        producer=fixture.producer,
        bound_generation=fixture.bound_generation,
    )
    envelope_v2 = built_v2.envelope
    assert isinstance(envelope_v2, CaptureEnvelope)
    envelope_v1 = CaptureEnvelopeV1(
        capture_contract_digest=envelope_v2.capture_contract_digest,
        source=envelope_v2.source,
        commitment=envelope_v2.commitment,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind=envelope_v2.run_coordinate.run_kind,
            run_id="run-b4-source",
            bound_generation=envelope_v2.run_coordinate.bound_generation,
            executable_identity=envelope_v2.run_coordinate.executable_identity,
            executable_digest=envelope_v2.run_coordinate.executable_digest,
        ),
        run_receipt_digest=envelope_v2.producer_receipt_digest,
        producer=envelope_v2.producer,
        producer_binding_digest=envelope_v2.producer_binding_digest,
        observed_at=envelope_v2.observed_at,
        source_effective_time=envelope_v2.source_effective_time,
    )
    journal = InMemoryCaptureLandingJournal()
    first = journal.append(
        instance_id="inst-b4",
        envelope=envelope_v1,
        landed_at=NOW,
        idempotency_key=capture_landing_idempotency_key(
            instance_id="inst-b4",
            envelope=envelope_v1,
        ),
    )
    second = journal.append(
        instance_id="inst-b4",
        envelope=envelope_v2,
        landed_at=NOW + timedelta(seconds=1),
        idempotency_key=capture_landing_idempotency_key(
            instance_id="inst-b4",
            envelope=envelope_v2,
        ),
    )

    assert isinstance(first, CaptureLandingEventV1)
    assert isinstance(second, CaptureLandingEvent)
    assert first.idempotency_key == (
        "e44868c72ea643c28b86ac66246be064baac3bf7c9d7be22c0525dd96a4ec9d7"
    )
    assert first.event_id == "eef54c8b396814fdb8125b31885e4e9ef54068e5ed02f0542cb2ac693b6f1252"
    assert first.partition_id == second.partition_id
    assert second.sequence == 1
    assert second.previous_event_digest == first.event_id
    original = journal.events_after()
    replayed = InMemoryCaptureLandingJournal.replay_from_genesis(
        original,
        envelopes={
            first.capture_digest: envelope_v1,
            second.capture_digest: envelope_v2,
        },
    )

    assert replayed.events_after() == original
    assert replayed.events_after()[0].model_dump(mode="json") == first.model_dump(mode="json")
    replayed.verify()
