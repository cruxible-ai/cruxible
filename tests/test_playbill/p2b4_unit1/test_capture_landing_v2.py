"""Self-attacks for mixed-version Capture landing and replay."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cruxible_client.contracts.capture_journal import (
    CaptureLandingEventV1,
    CaptureLandingEventV2,
    InMemoryCaptureLandingJournal,
    capture_landing_idempotency_key,
)
from cruxible_client.contracts.captures import (
    CaptureEnvelopeV1,
    CaptureEnvelopeV2,
    build_provider_external_capture_v2,
)
from tests.test_playbill._pc_c_support import NOW
from tests.test_playbill.p2b4_unit1._support import provider_capture_fixture


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
    assert isinstance(envelope_v2, CaptureEnvelopeV2)
    envelope_v1 = CaptureEnvelopeV1(
        capture_contract_digest=envelope_v2.capture_contract_digest,
        source=envelope_v2.source,
        commitment=envelope_v2.commitment,
        run_coordinate=envelope_v2.run_coordinate,
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
    assert isinstance(second, CaptureLandingEventV2)
    assert first.idempotency_key == (
        "80b88d651981f494f86b4822579f06bcf3d3057db6877d6f7636d401e6ce7705"
    )
    assert first.event_id == "65f5f866cd86156e44c53ef79bf39384e139fd5091d765559e8608acf5926d08"
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
