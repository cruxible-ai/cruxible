"""Governed promotion of one exact verified exhaust range into accepted state."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CanonicalValue,
    CasDigest,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.errors import (
    PlaybillCasError,
    PlaybillFormatError,
    PlaybillJournalError,
)
from cruxible_client.contracts.governance import ActivationPolicy, PermissionTier
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.exhaust.records import (
    JournalRangeV1,
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
    verify_journal_range,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

_PROMOTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_STREAM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PARTITION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ExhaustPromotionError(PlaybillFormatError):
    """An ExhaustPromotion artifact or its exact operational basis is invalid."""


class _StrictPromotionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be tagged lowercase SHA-256") from exc
    return value


class ExhaustReceiptSetManifestV1(_StrictPromotionModel):
    """Exact ordered journal-record and CAS-object set consumed by a reducer."""

    tag: Literal["playbill-exhaust-receipt-set-v1"] = "playbill-exhaust-receipt-set-v1"
    stream_id: str
    partition_id: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    record_digests: tuple[str, ...]
    payload_digests: tuple[str, ...]

    @field_validator("stream_id", "partition_id")
    @classmethod
    def _coordinates(cls, value: str, info: object) -> str:
        pattern = (
            _STREAM_ID_RE if getattr(info, "field_name", None) == "stream_id" else _PARTITION_ID_RE
        )
        if not pattern.fullmatch(value):
            raise ValueError(
                f"{getattr(info, 'field_name', 'journal coordinate')} is not canonical"
            )
        return value

    @field_validator("record_digests", "payload_digests")
    @classmethod
    def _digests(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        if not value:
            raise ValueError(f"{getattr(info, 'field_name', 'manifest digests')} must be nonempty")
        if getattr(info, "field_name", None) == "record_digests" and len(value) != len(set(value)):
            raise ValueError("receipt-set record digests must be unique")
        for item in value:
            _digest(item, label=str(getattr(info, "field_name", "manifest digest")))
        return value

    @model_validator(mode="after")
    def _range(self) -> "ExhaustReceiptSetManifestV1":
        count = self.last_sequence - self.first_sequence + 1
        if count <= 0 or len(self.record_digests) != count or len(self.payload_digests) != count:
            raise ValueError("receipt-set manifest range and digest vectors disagree")
        return self


def exhaust_receipt_set_manifest_digest(manifest: ExhaustReceiptSetManifestV1) -> str:
    return CasDigest(
        hashlib.sha256(canonical_bytes(manifest.model_dump(mode="json"))).hexdigest()
    ).tagged


class VerifiedExhaustRecordV1(_StrictPromotionModel):
    """One verified record, carrying the coordinates the record itself declares.

    A reducer sees the record's own run/occurrence/implementation coordinates so
    a reduction can separate what a range actually contains.  These are the
    stored record's fields, not a caller's assertion, and they grant no
    authority beyond the range the promotion law already verified.
    """

    record_digest: str
    sequence: int
    event_kind: str
    generation_digest: str
    payload_digest: str
    payload: object
    procedure_artifact_digest: str
    definition_digest: str
    run_id: str | None = None
    occurrence_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    line_spec_digest: str | None = None

    @field_validator(
        "record_digest",
        "generation_digest",
        "payload_digest",
        "procedure_artifact_digest",
        "definition_digest",
    )
    @classmethod
    def _digests(cls, value: str, info: object) -> str:
        return _digest(value, label=str(getattr(info, "field_name", "exhaust digest")))

    @field_validator("line_spec_digest")
    @classmethod
    def _optional_digests(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _digest(value, label=str(getattr(info, "field_name", "exhaust digest")))

    @field_validator("payload", mode="before")
    @classmethod
    def _payload(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)


class ExhaustReducerProtocol(Protocol):
    @property
    def reducer_digest(self) -> str: ...

    def reduce(self, records: tuple[VerifiedExhaustRecordV1, ...]) -> object: ...


def exhaust_promotion_output_digest(output: object) -> str:
    return CasDigest(
        hashlib.sha256(canonical_bytes(normalize_canonical(output))).hexdigest()
    ).tagged


class ExhaustPromotionV1(_StrictPromotionModel):
    artifact_format: Literal["playbill-exhaust-promotion-v1"] = "playbill-exhaust-promotion-v1"
    identity: ArtifactIdentity
    stream_id: str
    partition_id: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    chain_head_digest: str
    receipt_set_manifest_digest: str
    reducer_digest: str
    output_digest: str
    bound_generation_digests: tuple[str, ...]
    pins: tuple[ArtifactPin, ...]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("stream_id", "partition_id")
    @classmethod
    def _coordinates(cls, value: str, info: object) -> str:
        pattern = (
            _STREAM_ID_RE if getattr(info, "field_name", None) == "stream_id" else _PARTITION_ID_RE
        )
        if not pattern.fullmatch(value):
            raise ValueError(
                f"{getattr(info, 'field_name', 'journal coordinate')} is not canonical"
            )
        return value

    @field_validator(
        "chain_head_digest",
        "receipt_set_manifest_digest",
        "reducer_digest",
        "output_digest",
    )
    @classmethod
    def _digests(cls, value: str, info: object) -> str:
        return _digest(value, label=str(getattr(info, "field_name", "promotion digest")))

    @field_validator("bound_generation_digests")
    @classmethod
    def _generations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _digest(item, label="bound generation digest")
        if not value or value != tuple(sorted(set(value))):
            raise ValueError("bound_generation_digests must be nonempty, sorted, and unique")
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        ordered = tuple(
            sorted(
                value,
                key=lambda pin: (
                    pin.role.encode(),
                    pin.target.qualified.encode(),
                    pin.artifact_digest.encode(),
                ),
            )
        )
        keys = tuple((pin.role, pin.target.qualified) for pin in value)
        if value != ordered or len(keys) != len(set(keys)):
            raise ValueError("ExhaustPromotion pins must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ExhaustPromotionV1":
        if self.identity.kind != "ExhaustPromotion" or not _PROMOTION_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("ExhaustPromotion identity is not path-addressable")
        if self.first_sequence > self.last_sequence:
            raise ValueError("ExhaustPromotion first_sequence must not exceed last_sequence")
        if not any(
            pin.role == "reducer"
            and pin.target.kind == "ExhaustReducer"
            and pin.artifact_digest == self.reducer_digest
            for pin in self.pins
        ):
            raise ValueError("ExhaustPromotion must exactly pin its reducer")
        if not any(
            pin.role == "receipt-set-manifest"
            and pin.target.kind == "ReceiptSetManifest"
            and pin.artifact_digest == self.receipt_set_manifest_digest
            for pin in self.pins
        ):
            raise ValueError("ExhaustPromotion must exactly pin its receipt-set manifest")
        if not any(pin.role == "procedure" and pin.target.kind == "Procedure" for pin in self.pins):
            raise ValueError("ExhaustPromotion must pin at least one governed Procedure")
        return self


def exhaust_promotion_path(name: str) -> str:
    if not _PROMOTION_NAME_RE.fullmatch(name):
        raise ExhaustPromotionError("ExhaustPromotion identity is not path-addressable")
    return f"exhaust-promotions/{name}.yaml"


def render_exhaust_promotion(promotion: ExhaustPromotionV1) -> bytes:
    return canonical_bytes(promotion.model_dump(mode="json")) + b"\n"


def parse_exhaust_promotion(content: bytes, *, path: str) -> ExhaustPromotionV1:
    try:
        promotion = ExhaustPromotionV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExhaustPromotionError("ExhaustPromotion failed strict v1 validation") from exc
    if path != exhaust_promotion_path(promotion.identity.name):
        raise ExhaustPromotionError("ExhaustPromotion identity/path disagreement")
    if render_exhaust_promotion(promotion) != content:
        raise ExhaustPromotionError("ExhaustPromotion is not in canonical wire form")
    return promotion


def exhaust_promotion_digest(promotion: ExhaustPromotionV1) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        promotion.model_dump(mode="json"),
    ).tagged


class AcceptedExhaustPromotionV1(_StrictPromotionModel):
    path: str
    promotion: ExhaustPromotionV1
    artifact_digest: str
    accepted_coordinate: AcceptedCoordinate

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedExhaustPromotionV1":
        if self.path != exhaust_promotion_path(self.promotion.identity.name):
            raise ValueError("accepted ExhaustPromotion path does not reproduce")
        if self.artifact_digest != exhaust_promotion_digest(self.promotion):
            raise ValueError("accepted ExhaustPromotion digest does not reproduce")
        return self


class ExhaustPromotionLawResultV1(_StrictPromotionModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    refusal_code: str | None = None
    message: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    activation_policy: ActivationPolicy | None = None


def _refused(code: str, message: str) -> ExhaustPromotionLawResultV1:
    return ExhaustPromotionLawResultV1(
        verdict="refused",
        refusal_code=code,
        message=message,
    )


def evaluate_exhaust_promotion_law(
    promotion: ExhaustPromotionV1,
    *,
    records: tuple[StoredProcedureJournalRecordV1, ...],
    bodies: ContentAddressedBodyStore,
    reducer: ExhaustReducerProtocol,
) -> ExhaustPromotionLawResultV1:
    """Verify exact chain/range/receipt/reducer/output correspondence."""

    if not records:
        return _refused("promotion.range_missing", "Promoted journal range is unavailable.")
    first = records[0].record
    if (
        first.stream.stream_id != promotion.stream_id
        or first.partition_id != promotion.partition_id
        or first.sequence != promotion.first_sequence
        or records[-1].record.sequence != promotion.last_sequence
    ):
        return _refused("promotion.range_mismatch", "Promoted journal coordinates differ.")
    try:
        verify_journal_range(
            JournalRangeV1(
                stream=first.stream,
                partition_id=first.partition_id,
                first_sequence=promotion.first_sequence,
                last_sequence=promotion.last_sequence,
                expected_previous_digest=first.previous_record_digest,
                expected_head_digest=promotion.chain_head_digest,
            ),
            records,
        )
    except (ValueError, PlaybillJournalError):
        return _refused("promotion.chain_invalid", "Promoted journal chain does not verify.")
    manifest = ExhaustReceiptSetManifestV1(
        stream_id=promotion.stream_id,
        partition_id=promotion.partition_id,
        first_sequence=promotion.first_sequence,
        last_sequence=promotion.last_sequence,
        record_digests=tuple(item.record_digest for item in records),
        payload_digests=tuple(item.record.payload_digest for item in records),
    )
    if exhaust_receipt_set_manifest_digest(manifest) != promotion.receipt_set_manifest_digest:
        return _refused(
            "promotion.receipt_set_mismatch",
            "Promotion receipt-set manifest does not reproduce.",
        )
    access = BodyAccessContext(principal_id="exhaust-promotion-law", can_read_body=True)
    try:
        stored_manifest = bodies.read(promotion.receipt_set_manifest_digest, access=access)
    except PlaybillCasError:
        return _refused(
            "promotion.receipt_set_missing",
            "Promotion receipt-set manifest CAS object is unavailable.",
        )
    if stored_manifest != canonical_bytes(manifest.model_dump(mode="json")):
        return _refused(
            "promotion.receipt_set_cas_mismatch",
            "Promotion receipt-set manifest CAS bytes differ.",
        )
    if reducer.reducer_digest != promotion.reducer_digest:
        return _refused("promotion.reducer_mismatch", "Promotion reducer digest differs.")
    observed_procedure_digests = tuple(
        sorted({stored.record.procedure_artifact for stored in records})
    )
    pinned_procedure_digests = tuple(
        sorted(
            {
                pin.artifact_digest
                for pin in promotion.pins
                if pin.role == "procedure" and pin.target.kind == "Procedure"
            }
        )
    )
    if observed_procedure_digests != pinned_procedure_digests:
        return _refused(
            "promotion.procedure_set_mismatch",
            "Promotion Procedure pins differ from the exact promoted record set.",
        )
    try:
        verified = tuple(
            VerifiedExhaustRecordV1(
                record_digest=stored.record_digest,
                sequence=stored.record.sequence,
                event_kind=stored.record.event_kind,
                generation_digest=stored.record.accepted_coordinate.generation_root,
                payload_digest=stored.record.payload_digest,
                payload=parse_journal_payload(
                    bodies.read(stored.record.payload_digest, access=access)
                ),
                procedure_artifact_digest=stored.record.procedure_artifact,
                definition_digest=stored.record.definition_digest,
                run_id=stored.record.run_id,
                occurrence_id=stored.record.occurrence_id,
                attempt=stored.record.attempt,
                line_spec_digest=stored.record.line_spec_digest,
            )
            for stored in records
        )
        output = normalize_canonical(reducer.reduce(verified))
    except Exception as exc:
        return _refused("promotion.reducer_failed", f"Promotion reducer refused: {exc}")
    if exhaust_promotion_output_digest(output) != promotion.output_digest:
        return _refused("promotion.output_mismatch", "Promotion output does not reproduce.")
    try:
        stored_output = bodies.read(promotion.output_digest, access=access)
    except PlaybillCasError:
        return _refused("promotion.output_missing", "Promotion output CAS object is unavailable.")
    if stored_output != canonical_bytes(output):
        return _refused("promotion.output_cas_mismatch", "Promotion output CAS bytes differ.")
    generations = tuple(sorted({item.generation_digest for item in verified}))
    if generations != promotion.bound_generation_digests:
        return _refused(
            "promotion.generation_set_mismatch",
            "Promotion bound-generation set does not reproduce.",
        )
    return ExhaustPromotionLawResultV1(
        verdict="accepted",
        artifact_digest=exhaust_promotion_digest(promotion),
        required_tier="governed_write",
        approval_scope=(),
        activation_policy="snapshot",
    )


def evaluate_exhaust_promotion_acceptance(
    promotion: ExhaustPromotionV1,
    *,
    path: str,
    predecessor: AcceptedExhaustPromotionV1 | None,
    operational_result: ExhaustPromotionLawResultV1,
) -> ExhaustPromotionLawResultV1:
    """Apply ordinary artifact lineage around exact-range verification."""

    if path != exhaust_promotion_path(promotion.identity.name):
        return _refused("promotion.path_mismatch", "Promotion identity/path disagreement.")
    if predecessor is None and promotion.lifecycle.predecessor_digest is not None:
        return _refused("promotion.predecessor_missing", "New promotion names a predecessor.")
    if predecessor is not None:
        if (
            predecessor.promotion.identity != promotion.identity
            or promotion.lifecycle.predecessor_digest != predecessor.artifact_digest
        ):
            return _refused(
                "promotion.predecessor_mismatch",
                "Promotion successor identity or predecessor differs.",
            )
    if operational_result.verdict != "accepted":
        return operational_result
    expected_digest = exhaust_promotion_digest(promotion)
    if operational_result.artifact_digest != expected_digest:
        return _refused(
            "promotion.verification_substitution",
            "Operational verification names another promotion artifact.",
        )
    return operational_result.model_copy(
        update={
            "required_tier": "governed_write",
            "approval_scope": (),
            "activation_policy": "snapshot",
        }
    )


def procedure_track_record_facts(
    accepted: AcceptedExhaustPromotionV1,
    *,
    output: CanonicalValue,
) -> tuple[ProjectionFact, ...]:
    """Only an accepted promotion can create canonical Procedure track-record facts."""

    promotion = accepted.promotion
    procedure_pins = tuple(pin for pin in promotion.pins if pin.role == "procedure")
    return tuple(
        ProjectionFact(
            schema_id="playbill.procedure.track_record",
            schema_version=1,
            subject_identity=pin.target.qualified,
            fact_key=promotion.identity.name,
            value={
                "accepted_coordinate": accepted.accepted_coordinate.model_dump(mode="json"),
                "bound_generation_digests": [
                    {"$digest": item} for item in promotion.bound_generation_digests
                ],
                "chain_head_digest": {"$digest": promotion.chain_head_digest},
                "first_sequence": promotion.first_sequence,
                "last_sequence": promotion.last_sequence,
                "output_digest": {"$digest": promotion.output_digest},
                "output": output,
                "promotion_digest": {"$digest": accepted.artifact_digest},
                "receipt_set_manifest_digest": {"$digest": promotion.receipt_set_manifest_digest},
                "reducer_digest": {"$digest": promotion.reducer_digest},
                "stream_id": promotion.stream_id,
                "partition_id": promotion.partition_id,
            },
        )
        for pin in procedure_pins
    )


__all__ = [
    "AcceptedExhaustPromotionV1",
    "ExhaustPromotionError",
    "ExhaustPromotionLawResultV1",
    "ExhaustPromotionV1",
    "ExhaustReceiptSetManifestV1",
    "ExhaustReducerProtocol",
    "VerifiedExhaustRecordV1",
    "evaluate_exhaust_promotion_law",
    "evaluate_exhaust_promotion_acceptance",
    "exhaust_promotion_digest",
    "exhaust_promotion_output_digest",
    "exhaust_promotion_path",
    "exhaust_receipt_set_manifest_digest",
    "parse_exhaust_promotion",
    "procedure_track_record_facts",
    "render_exhaust_promotion",
]
