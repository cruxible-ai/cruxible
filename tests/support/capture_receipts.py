"""A fixed Capture verification context for unit tests of ``verify_capture``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from cruxible_client.contracts.captures import (
    CaptureBuildResult,
    CaptureProductionRecord,
    ProcedureProducerReceiptProtocol,
    ProviderProducerReceiptResolution,
)

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
