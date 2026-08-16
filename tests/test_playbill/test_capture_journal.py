from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cruxible_core.playbill.capture_journal import (
    CaptureCursorV1,
    InMemoryCaptureLandingJournal,
    capture_landing_idempotency_key,
)
from cruxible_core.playbill.captures import build_cas_capture
from tests.test_playbill._pc_c_support import (
    NOW,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)


def _capture(tmp_path: Path, run_id: str = "provider-run-1"):
    contract = capture_contract()
    provider_artifact = provider(contract)
    result = build_cas_capture(
        store=body_store(tmp_path),
        contract=contract,
        source_body=b'{"order_id":"ord-482"}',
        run_coordinate=provider_run(provider_artifact, run_id=run_id),
        run_receipt_digest=digest("test-receipt", run_id),
        producer=provider_artifact.identity,
        producer_binding_digest=digest("test-binding", "orders"),
        observed_at=NOW,
    )
    return result.envelope


def test_landing_retry_returns_same_event_and_cursor() -> None:
    from pathlib import Path
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        envelope = _capture(Path(directory))
        journal = InMemoryCaptureLandingJournal()
        key = capture_landing_idempotency_key(
            instance_id="inst-journal",
            envelope=envelope,
        )
        first = journal.append(
            instance_id="inst-journal",
            envelope=envelope,
            landed_at=NOW,
            idempotency_key=key,
        )
        retry = journal.append(
            instance_id="inst-journal",
            envelope=envelope,
            landed_at=NOW + timedelta(seconds=5),
            idempotency_key=key,
        )
        assert retry == first
        assert CaptureCursorV1.parse(first.cursor).render() == first.cursor
        journal.verify()


def test_partitions_have_independent_sequences(tmp_path: Path) -> None:
    first_envelope = _capture(tmp_path / "one", run_id="run-one")
    second_envelope = first_envelope.model_copy(
        update={"producer_binding_digest": digest("test-binding", "other")}
    )
    journal = InMemoryCaptureLandingJournal()
    events = []
    for envelope in (first_envelope, second_envelope):
        events.append(
            journal.append(
                instance_id="inst-journal",
                envelope=envelope,
                landed_at=NOW,
                idempotency_key=capture_landing_idempotency_key(
                    instance_id="inst-journal",
                    envelope=envelope,
                ),
            )
        )
    assert [event.sequence for event in events] == [0, 0]
    assert events[0].partition_id != events[1].partition_id
    assert len(journal.vector_cursor()) == 2
