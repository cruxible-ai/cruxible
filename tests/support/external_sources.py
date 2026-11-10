"""A deterministic versioned external source, for tests only.

It serves seeded, immutable source versions through the production
``ExternalSourceReaderProtocol`` and stores what it reads in the given CAS.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, normalize_canonical
from cruxible_client.contracts.captures import (
    CaptureEnvelopeV1,
    CaptureObjectStoreProtocol,
    SourceEffectiveTime,
    capture_contract_digest,
    capture_digest,
    render_capture_envelope,
)
from cruxible_client.contracts.source_references import (
    EvidenceCommitment,
    ExternalSourceReference,
)
from cruxible_core.evidence.source_readers import (
    CaptureAcquisitionReceiptV1,
    ExternalCaptureAcquisitionV1,
    ExternalSourceError,
    ExternalSourceReadRequestV1,
)


class _VersionedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_identity: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    value: object
    replayability: Literal["exact", "attested_only"]
    source_effective_time: SourceEffectiveTime | None = None
    provider_signature_verified: bool = False

    @field_validator("coordinate", "selector", "value", mode="before")
    @classmethod
    def _canonical(cls, value: object) -> object:
        return normalize_canonical(value)


def _selection_key(
    source_identity: str,
    coordinate_type: str,
    coordinate: object,
    selector_type: str,
    selector: object,
) -> bytes:
    return canonical_bytes(
        {
            "source_identity": source_identity,
            "coordinate_type": coordinate_type,
            "coordinate": coordinate,
            "selector_type": selector_type,
            "selector": selector,
        }
    )


class FakeVersionedExternalSourceReader:
    """Deterministic fixture adapter with no production write/DDL surface."""

    def __init__(self) -> None:
        self._records: dict[bytes, _VersionedRecord] = {}

    def seed(
        self,
        *,
        source_identity: str,
        coordinate_type: str,
        coordinate: object,
        selector_type: str,
        selector: object,
        value: object,
        replayability: Literal["exact", "attested_only"] = "exact",
        source_effective_time: SourceEffectiveTime | None = None,
        provider_signature_verified: bool = False,
    ) -> None:
        """Fixture-only setup; callers typed as the protocol cannot mutate a source."""

        record = _VersionedRecord(
            source_identity=source_identity,
            coordinate_type=coordinate_type,
            coordinate=coordinate,
            selector_type=selector_type,
            selector=selector,
            value=value,
            replayability=replayability,
            source_effective_time=source_effective_time,
            provider_signature_verified=provider_signature_verified,
        )
        key = _selection_key(
            source_identity,
            coordinate_type,
            record.coordinate,
            selector_type,
            record.selector,
        )
        if key in self._records:
            raise ExternalSourceError("fake source version is immutable once seeded")
        self._records[key] = record

    def replay_available(self, source: ExternalSourceReference) -> bool:
        if source.replayability != "exact":
            return False
        return (
            _selection_key(
                source.source_identity,
                source.coordinate_type,
                source.coordinate,
                source.selector_type,
                source.selector,
            )
            in self._records
        )

    def acquire(
        self,
        request: ExternalSourceReadRequestV1,
        *,
        store: CaptureObjectStoreProtocol,
    ) -> ExternalCaptureAcquisitionV1:
        if request.contract.epistemic_grade == "derived":
            raise ExternalSourceError("derived Captures require reducer receipt-set assembly")
        key = _selection_key(
            request.binding.logical_source_identity,
            request.coordinate_type,
            request.coordinate,
            request.selector_type,
            request.selector,
        )
        record = self._records.get(key)
        if record is None:
            raise ExternalSourceError("external source selection is unavailable")
        material = canonical_bytes(record.value)
        if len(material) > request.resource_budget.max_bytes:
            raise ExternalSourceError("external source selection exceeds the byte budget")
        item_count = len(record.value) if isinstance(record.value, list) else 1
        if item_count > request.resource_budget.max_items:
            raise ExternalSourceError("external source selection exceeds the item budget")
        if isinstance(record.value, list) and len(record.value) > request.resource_budget.max_rows:
            raise ExternalSourceError("external source selection exceeds the row budget")
        replayability = record.replayability
        if request.materialization == "external" and replayability != "exact":
            raise ExternalSourceError("external materialization requires an exact source version")
        if request.materialization == "none" and replayability != "attested_only":
            raise ExternalSourceError("unmaterialized evidence must be attested-only")
        digest = Sha256Value(hashlib.sha256(material).hexdigest()).tagged
        if request.materialization == "cas":
            stored = store.store(material)
            if stored.digest != digest:
                raise ExternalSourceError("bounded external material did not reproduce in CAS")
        commitment = EvidenceCommitment(
            digest_kind="canonical_value",
            digest=digest,
            materialization=request.materialization,
        )
        receipt = CaptureAcquisitionReceiptV1(
            capture_contract_digest=capture_contract_digest(request.contract).tagged,
            producer=request.provider.identity,
            producer_binding_digest=request.binding.digest,
            source_identity=request.binding.logical_source_identity,
            coordinate_type=request.coordinate_type,
            coordinate=request.coordinate,
            selector_type=request.selector_type,
            selector=request.selector,
            commitment=commitment,
            observed_at=request.observed_at,
            replayability=replayability,
            source_effective_time=record.source_effective_time,
        )
        source = ExternalSourceReference(
            source_identity=request.binding.logical_source_identity,
            producer_binding_digest=request.binding.digest,
            coordinate_type=request.coordinate_type,
            coordinate=request.coordinate,
            selector_type=request.selector_type,
            selector=request.selector,
            replayability=replayability,
        )
        envelope = CaptureEnvelopeV1(
            capture_contract_digest=capture_contract_digest(request.contract).tagged,
            source=source,
            commitment=commitment,
            run_coordinate=request.run_coordinate,
            run_receipt_digest=receipt.digest,
            producer=request.provider.identity,
            producer_binding_digest=request.binding.digest,
            observed_at=request.observed_at,
            source_effective_time=record.source_effective_time,
        )
        envelope_bytes = render_capture_envelope(envelope)
        stored_envelope = store.store(envelope_bytes)
        expected = capture_digest(envelope).tagged
        if stored_envelope.digest != expected:
            raise ExternalSourceError("external Capture envelope did not reproduce in CAS")
        provenance: Literal["daemon-fetched", "provider-signed"] = (
            "provider-signed" if record.provider_signature_verified else "daemon-fetched"
        )
        return ExternalCaptureAcquisitionV1(
            receipt=receipt,
            envelope=envelope,
            capture_digest=expected,
            epistemic_grade=request.contract.epistemic_grade,
            provenance_grade=provenance,
            canonical_material=record.value if request.materialization == "cas" else None,
        )


__all__ = ["FakeVersionedExternalSourceReader"]
