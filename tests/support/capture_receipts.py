"""Capture verification contexts and journal records for tests of Capture evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from cruxible_client.contracts.captures import (
    CaptureBuildResult,
    CaptureProductionRecord,
    ProcedureProducerReceiptProtocol,
    ProviderProducerReceiptResolution,
)
from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.exhaust import LocalJournalBackend
from cruxible_core.exhaust.records import ProcedureJournalRecordDraftV1, journal_payload_bytes
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.procedures.execution import procedure_line_journal_stream
from cruxible_core.runtime.instance import PlaybillInstance

Resolution = ProviderProducerReceiptResolution | ProcedureProducerReceiptProtocol


@dataclass
class StaticReceipts:
    """Receipts by digest, and the production records a daemon journal would hold."""

    receipts: Mapping[str, Resolution] = field(default_factory=dict)
    produced: tuple[CaptureProductionRecord, ...] = ()

    def producer_receipt(self, digest: str, *, run_id: str) -> Resolution | None:
        return self.receipts.get(digest)

    def produced_capture(
        self, capture_digest: str, *, run_id: str
    ) -> tuple[CaptureProductionRecord, ...]:
        return tuple(record for record in self.produced if record.capture_digest == capture_digest)


def produced_by_run(built: CaptureBuildResult) -> CaptureProductionRecord:
    """The record a run's journal holds for a v2 Capture it built."""

    envelope = built.envelope
    return CaptureProductionRecord(
        capture_digest=built.capture_digest,
        capture_contract_digest=envelope.capture_contract_digest,
        run_id=envelope.run_coordinate.run_id,
        receipt_digest=getattr(envelope, "producer_receipt_digest", None),
    )


def admit_legacy_v1_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Judge v1 envelopes as the Claim law did before production proof existed.

    Version-1 envelopes under a daemon-fetched contract come only from the
    fixture source reader and from state accepted before producer receipts: no
    new citation of one is admitted. A test pinning how such accepted evidence
    is judged (mapping, admission, replay) admits it the old way.
    """

    monkeypatch.setattr(
        "cruxible_client.contracts.captures.capture_contract_is_self_asserted",
        lambda _contract: True,
    )


def journal_produced_capture(
    instance: PlaybillInstance,
    capture_digest: str,
    *,
    contract_digest: str,
    run_id: str,
    recorded_at: datetime,
) -> None:
    """Append the ``produced_capture`` record a run would have written for a Capture.

    For tests of what reads the journal's produced-Capture index (discovery by
    contract); the receipt chain such a record follows in a real run is absent.
    """

    journal_root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    journal_root.mkdir(mode=0o700, exist_ok=True)
    journal = LocalJournalBackend(journal_root)
    stream = procedure_line_journal_stream(instance.descriptor.instance_id)
    partition_id = f"test-produced-{run_id}"
    head = journal.read_head(stream, partition_id)
    if head.sequence == 0:
        journal.activate_writer(
            stream, partition_id, fencing_token="test-writer", expected_head=head
        )
    payload = instance.body_store().store(
        journal_payload_bytes(
            {
                "tag": "playbill-procedure-produced-capture-v1",
                "node_id": "source",
                "capture_digest": capture_digest,
                "capture_contract_digest": contract_digest,
                "observed_at": format_datetime(recorded_at),
            }
        )
    )
    accepted = instance.accepted_coordinate()
    journal.append(
        ProcedureJournalRecordDraftV1(
            stream=stream,
            partition_id=partition_id,
            event_kind="produced_capture",
            accepted_coordinate=AcceptedCoordinate.from_internal(accepted),
            procedure_artifact_digest=contract_digest,
            definition_digest=contract_digest,
            run_id=run_id,
            admission_binding_digest=contract_digest,
            payload_digest=payload.digest,
            actor_context=GovernedActorContext(
                actor_type="human_user",
                actor_id="owner",
                org_id="org-test",
                operation_id=f"op-{run_id}",
                timestamp=recorded_at,
            ),
            recorded_at=recorded_at,
        ),
        expected_head=head,
        fencing_token="test-writer",
    )
