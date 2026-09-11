"""Read-only external acquisition seam and deterministic versioned test adapter."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    CaptureContractV1,
    CaptureEnvelopeAny,
    CaptureEnvelopeV1,
    CaptureEnvelopeV2,
    CaptureObjectStoreProtocol,
    CaptureRunCoordinateV1,
    CaptureSelectionBudgetV1,
    SourceEffectiveTimeV1,
    capture_contract_digest,
    capture_digest,
    render_capture_envelope,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.providers import ProviderV1
from cruxible_client.contracts.source_references import (
    EvidenceCommitmentV1,
    ExternalSourceReferenceV1,
)


class ExternalSourceError(PlaybillFormatError):
    """An external source could not satisfy its accepted acquisition contract."""


class _StrictSourceReaderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProducerBindingV1(_StrictSourceReaderModel):
    """Locator-free public description of a secret/runtime binding."""

    tag: Literal["playbill-producer-binding-v1"] = "playbill-producer-binding-v1"
    provider: ArtifactIdentity
    logical_source_identity: str
    adapter_digest: str
    binding_epoch: int = 0

    @field_validator("adapter_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ProducerBindingV1":
        if self.provider.kind != "Provider":
            raise ValueError("producer binding must name a Provider")
        # Reuse the locator/secret-free source identity validation.
        ExternalSourceReferenceV1(
            source_identity=self.logical_source_identity,
            producer_binding_digest="sha256:" + "00" * 32,
            coordinate_type="binding-check-v1",
            coordinate={},
            selector_type="binding-check-v1",
            selector={},
            replayability="attested_only",
        )
        if self.binding_epoch < 0:
            raise ValueError("producer binding epoch must be nonnegative")
        return self

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        return typed_digest(
            Sha256Value,
            "playbill-producer-binding-v1",
            payload,
        ).tagged


class ExternalSourceReadRequestV1(_StrictSourceReaderModel):
    tag: Literal["playbill-external-source-read-v1"] = "playbill-external-source-read-v1"
    contract: CaptureContractV1
    provider: ProviderV1
    binding: ProducerBindingV1
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    materialization: Literal["external", "none", "cas"]
    run_coordinate: CaptureRunCoordinateV1
    observed_at: datetime
    resource_budget: CaptureSelectionBudgetV1

    @field_validator("coordinate", "selector", mode="before")
    @classmethod
    def _canonical(cls, value: object) -> object:
        return normalize_canonical(value)

    @field_validator("observed_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("external acquisition time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _binding(self) -> "ExternalSourceReadRequestV1":
        contract_digest = capture_contract_digest(self.contract).tagged
        if self.binding.provider != self.provider.identity:
            raise ValueError("producer binding names a different Provider")
        if self.binding.logical_source_identity not in self.contract.logical_source_identities:
            raise ValueError("producer binding source is absent from its CaptureContract")
        if contract_digest not in self.provider.capture_contract_digests:
            raise ValueError("Provider does not declare the exact CaptureContract")
        if self.materialization not in self.contract.allowed_materialization_modes:
            raise ValueError("requested materialization is absent from the CaptureContract")
        if "external" not in self.contract.allowed_source_kinds:
            raise ValueError("CaptureContract does not permit external sources")
        if self.run_coordinate.executable_identity != self.provider.identity:
            raise ValueError("external acquisition run must name its Provider")
        if self.run_coordinate.executable_digest != self.provider_digest:
            raise ValueError("external acquisition run names a different Provider digest")
        if self.resource_budget.max_bytes > self.contract.selection_budget.max_bytes or (
            self.resource_budget.max_rows > self.contract.selection_budget.max_rows
            or self.resource_budget.max_items > self.contract.selection_budget.max_items
        ):
            raise ValueError("runtime resource budget cannot widen the CaptureContract")
        coordinate_types = {pin.target.name for pin in self.contract.coordinate_schema_pins}
        selector_types = {pin.target.name for pin in self.contract.selector_schema_pins}
        if self.coordinate_type not in coordinate_types:
            raise ValueError("external coordinate type is not pinned by the CaptureContract")
        if self.selector_type not in selector_types:
            raise ValueError("external selector type is not pinned by the CaptureContract")
        ExternalSourceReferenceV1(
            source_identity=self.binding.logical_source_identity,
            producer_binding_digest=self.binding.digest,
            coordinate_type=self.coordinate_type,
            coordinate=self.coordinate,
            selector_type=self.selector_type,
            selector=self.selector,
            replayability=("attested_only" if self.materialization == "none" else "exact"),
        )
        privacy = self.contract.retention_erasure_policy.selector_privacy
        if privacy == "pseudonymous_required" and not self.selector_type.startswith(
            "pseudonymous-"
        ):
            raise ValueError("CaptureContract requires a pseudonymous selector schema")
        return self

    @property
    def provider_digest(self) -> str:
        from cruxible_client.contracts.providers import provider_digest

        return provider_digest(self.provider).tagged


class CaptureAcquisitionReceiptV1(_StrictSourceReaderModel):
    tag: Literal["playbill-capture-acquisition-receipt-v1"] = (
        "playbill-capture-acquisition-receipt-v1"
    )
    capture_contract_digest: str
    producer: ArtifactIdentity
    producer_binding_digest: str
    source_identity: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    commitment: EvidenceCommitmentV1
    observed_at: datetime = Field(description="Reads EVALUATION INSTANT.")
    replayability: Literal["exact", "attested_only"]
    source_effective_time: SourceEffectiveTimeV1 | None = None

    @field_validator("capture_contract_digest", "producer_binding_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        return typed_digest(
            Sha256Value,
            "playbill-capture-acquisition-receipt-v1",
            payload,
        ).tagged


class ExternalCaptureAcquisitionV1(_StrictSourceReaderModel):
    tag: Literal["playbill-external-capture-acquisition-v1"] = (
        "playbill-external-capture-acquisition-v1"
    )
    receipt: CaptureAcquisitionReceiptV1
    envelope: CaptureEnvelopeAny
    capture_digest: str
    epistemic_grade: Literal["observed", "derived", "predicted"]
    provenance_grade: Literal["self-asserted", "daemon-fetched", "provider-signed", "witnessed"]
    canonical_material: object | None = None

    @field_validator("capture_digest")
    @classmethod
    def _capture_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("canonical_material", mode="before")
    @classmethod
    def _material(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @model_validator(mode="after")
    def _correspondence(self) -> "ExternalCaptureAcquisitionV1":
        envelope = self.envelope
        if isinstance(envelope, CaptureEnvelopeV1):
            if envelope.run_receipt_digest != self.receipt.digest:
                raise ValueError("Capture envelope differs from its acquisition receipt")
        else:
            assert isinstance(envelope, CaptureEnvelopeV2)
            source = envelope.source
            if not isinstance(source, ExternalSourceReferenceV1) or (
                self.receipt.capture_contract_digest != envelope.capture_contract_digest
                or self.receipt.producer != envelope.producer
                or self.receipt.producer_binding_digest != envelope.producer_binding_digest
                or self.receipt.source_identity != source.source_identity
                or self.receipt.coordinate_type != source.coordinate_type
                or self.receipt.coordinate != source.coordinate
                or self.receipt.selector_type != source.selector_type
                or self.receipt.selector != source.selector
                or self.receipt.commitment != envelope.commitment
                or self.receipt.observed_at != envelope.observed_at
                or self.receipt.replayability != source.replayability
                or self.receipt.source_effective_time != envelope.source_effective_time
            ):
                raise ValueError("Capture v2 differs from its acquisition receipt")
        if capture_digest(self.envelope).tagged != self.capture_digest:
            raise ValueError("external Capture digest does not reproduce")
        return self


@runtime_checkable
class ExternalSourceReaderProtocol(Protocol):
    """The complete production-facing surface: observe and dereference only."""

    def acquire(
        self,
        request: ExternalSourceReadRequestV1,
        *,
        store: CaptureObjectStoreProtocol,
    ) -> ExternalCaptureAcquisitionV1: ...

    def replay_available(self, source: ExternalSourceReferenceV1) -> bool: ...


class _VersionedRecord(_StrictSourceReaderModel):
    source_identity: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    value: object
    replayability: Literal["exact", "attested_only"]
    source_effective_time: SourceEffectiveTimeV1 | None = None
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
        source_effective_time: SourceEffectiveTimeV1 | None = None,
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

    def prune_fixture_version(self, source: ExternalSourceReferenceV1) -> None:
        """Simulate WAL/version-retention loss in tests; not part of the reader protocol."""

        self._records.pop(
            _selection_key(
                source.source_identity,
                source.coordinate_type,
                source.coordinate,
                source.selector_type,
                source.selector,
            ),
            None,
        )

    def replay_available(self, source: ExternalSourceReferenceV1) -> bool:
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
        commitment = EvidenceCommitmentV1(
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
        source = ExternalSourceReferenceV1(
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


__all__ = [
    "CaptureAcquisitionReceiptV1",
    "ExternalCaptureAcquisitionV1",
    "ExternalSourceError",
    "ExternalSourceReadRequestV1",
    "ExternalSourceReaderProtocol",
    "FakeVersionedExternalSourceReader",
    "ProducerBindingV1",
]
