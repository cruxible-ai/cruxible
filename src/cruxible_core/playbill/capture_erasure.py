"""Authorized evidence-body erasure without rewriting immutable Capture envelopes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.captures import (
    CaptureContractV1,
    CaptureEnvelopeV1,
    CaptureObjectStoreProtocol,
    capture_contract_digest,
    verify_capture,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.governance import governance_identifier
from cruxible_core.playbill.source_references import CasSourceReferenceV1

_SIGNATURE_RE = re.compile(r"^[0-9a-f]{128}$")
_PUBLIC_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class CaptureErasureError(PlaybillFormatError):
    """Evidence-body erasure is unauthorized, premature, or unverifiable."""


class _StrictErasureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptureErasureStatementV1(_StrictErasureModel):
    tag: Literal["playbill-capture-erasure-statement-v1"] = "playbill-capture-erasure-statement-v1"
    instance_id: str
    capture_digest: str
    capture_contract_digest: str
    body_digest: str
    erased_at: datetime
    authorized_by: str
    erasure_rule_digest: str
    authorization_proof_digest: str
    signing_key_id: str

    @field_validator(
        "capture_digest",
        "capture_contract_digest",
        "body_digest",
        "erasure_rule_digest",
        "authorization_proof_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("authorized_by")
    @classmethod
    def _actor(cls, value: str) -> str:
        return governance_identifier(value, label="Capture erasure actor")

    @field_validator("erased_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Capture erasure time must be timezone-aware")
        return value


class CaptureErasureReceiptV1(CaptureErasureStatementV1):
    algorithm: Literal["ed25519"] = "ed25519"
    signature: str

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        if not _SIGNATURE_RE.fullmatch(value):
            raise ValueError("erasure receipt signature must contain 64 bytes of lowercase hex")
        return value

    @property
    def statement(self) -> CaptureErasureStatementV1:
        payload = self.model_dump(mode="json")
        payload.pop("algorithm")
        payload.pop("signature")
        return CaptureErasureStatementV1.model_validate(payload)

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        return typed_digest(
            Sha256Value,
            "playbill-capture-erasure-receipt-v1",
            payload,
        ).tagged


def capture_erasure_statement_bytes(statement: CaptureErasureStatementV1) -> bytes:
    return canonical_bytes(
        {
            "algorithm": "ed25519",
            "domain": "playbill-capture-erasure-v1",
            "statement": statement.model_dump(mode="json"),
        }
    )


@runtime_checkable
class CaptureErasureSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def sign_erasure(self, statement: CaptureErasureStatementV1) -> str: ...


@runtime_checkable
class ErasableCaptureObjectStoreProtocol(CaptureObjectStoreProtocol, Protocol):
    def erase(self, digest: str) -> bool: ...


def verify_erasure_receipt(receipt: CaptureErasureReceiptV1, *, public_key: str) -> None:
    if not _PUBLIC_KEY_RE.fullmatch(public_key):
        raise CaptureErasureError("erasure receipt public key is malformed")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(receipt.signature),
            capture_erasure_statement_bytes(receipt.statement),
        )
    except (InvalidSignature, ValueError) as exc:
        raise CaptureErasureError("erasure receipt signature does not verify") from exc


def erase_capture_body(
    *,
    instance_id: str,
    capture_digest: str,
    contract: CaptureContractV1,
    store: ErasableCaptureObjectStoreProtocol,
    erased_at: datetime,
    authorized_by: str,
    authorization_proof_digest: str,
    signer: CaptureErasureSigner,
) -> CaptureErasureReceiptV1:
    """Erase only retained source bytes after exact policy and retention checks."""

    envelope: CaptureEnvelopeV1 = verify_capture(
        capture_digest,
        store=store,
        contract=contract,
    )
    policy = contract.retention_erasure_policy
    if policy.erasure != "authorized_by_rule" or policy.erasure_rule_digest is None:
        raise CaptureErasureError("CaptureContract prohibits evidence-body erasure")
    if policy.minimum_retention is not None and erased_at < envelope.observed_at + timedelta(
        microseconds=policy.minimum_retention.microseconds
    ):
        raise CaptureErasureError("Capture body is still inside its minimum retention interval")
    if not isinstance(envelope.source, CasSourceReferenceV1):
        raise CaptureErasureError("Capture has no independently retained CAS source body")
    body_digest = envelope.source.content_digest
    # Verify before deletion so the receipt cannot launder missing/corrupt material.
    store.read(
        body_digest,
        access=BodyAccessContext(principal_id="playbill-erasure", can_read_body=True),
    )
    statement = CaptureErasureStatementV1(
        instance_id=instance_id,
        capture_digest=capture_digest,
        capture_contract_digest=capture_contract_digest(contract).tagged,
        body_digest=body_digest,
        erased_at=erased_at,
        authorized_by=authorized_by,
        erasure_rule_digest=policy.erasure_rule_digest,
        authorization_proof_digest=authorization_proof_digest,
        signing_key_id=signer.key_id,
    )
    signature = signer.sign_erasure(statement)
    receipt = CaptureErasureReceiptV1(
        **statement.model_dump(mode="json"),
        signature=signature,
    )
    if not store.erase(body_digest):
        raise CaptureErasureError("Capture body disappeared before authorized erasure")
    return receipt


class EffectiveCaptureAvailabilityV1(_StrictErasureModel):
    tag: Literal["playbill-effective-capture-availability-v1"] = (
        "playbill-effective-capture-availability-v1"
    )
    capture_digest: str
    body_status: Literal["available", "body_unavailable_erased", "unavailable"]
    effective_materialization: Literal["ledger", "cas", "external", "none"]
    effective_replayability: Literal["exact", "attested_only"]
    erasure_receipt_digest: str | None = None


__all__ = [
    "CaptureErasureError",
    "CaptureErasureReceiptV1",
    "CaptureErasureSigner",
    "CaptureErasureStatementV1",
    "EffectiveCaptureAvailabilityV1",
    "ErasableCaptureObjectStoreProtocol",
    "capture_erasure_statement_bytes",
    "erase_capture_body",
    "verify_erasure_receipt",
]
