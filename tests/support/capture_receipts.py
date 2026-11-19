"""Capture verification contexts and journal records for tests of Capture evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from cruxible_client.contracts.captures import (
    CaptureBuildResult,
    CaptureProductionRecord,
    ProcedureProducerReceiptProtocol,
    ProviderProducerReceiptResolution,
    VerifiedCaptureMemo,
)

Resolution = ProviderProducerReceiptResolution | ProcedureProducerReceiptProtocol


@dataclass
class StaticReceipts:
    """Receipts by digest, and the production records a daemon journal would hold."""

    receipts: Mapping[str, Resolution] = field(default_factory=dict)
    produced: tuple[CaptureProductionRecord, ...] = ()
    verified: dict[str, VerifiedCaptureMemo] = field(default_factory=dict)

    def producer_receipt(self, digest: str, *, run_id: str) -> Resolution | None:
        return self.receipts.get(digest)

    def produced_capture(
        self, capture_digest: str, *, run_id: str
    ) -> tuple[CaptureProductionRecord, ...]:
        return tuple(record for record in self.produced if record.capture_digest == capture_digest)

    def recall_verified(self, capture_digest: str) -> VerifiedCaptureMemo | None:
        return self.verified.get(capture_digest)

    def remember_verified(self, memo: VerifiedCaptureMemo) -> None:
        self.verified[memo.capture_digest] = memo


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


def admit_as_before_production_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admit every Capture as the daemon did before production proof existed.

    Every journal context vouches that a run produced whatever Capture it is
    asked about, and v1 envelopes are judged the old way: what a proposal
    admitted by the vulnerable build looked like.
    """

    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_client.contracts.cas_contracts import BodyAccessContext
    from cruxible_core.exhaust.producer_receipts import JournalProducerReceiptResolver

    access = BodyAccessContext(principal_id="test-before-proof", can_read_body=True)

    def vouched(
        self: JournalProducerReceiptResolver, capture_digest: str, *, run_id: str
    ) -> tuple[CaptureProductionRecord, ...]:
        envelope = parse_capture_envelope(self._bodies.read(capture_digest, access=access))
        return (
            CaptureProductionRecord(
                capture_digest=capture_digest,
                capture_contract_digest=envelope.capture_contract_digest,
                run_id=run_id,
                receipt_digest=None,
            ),
        )

    monkeypatch.setattr(JournalProducerReceiptResolver, "produced_capture", vouched)
    admit_legacy_v1_citations(monkeypatch)
