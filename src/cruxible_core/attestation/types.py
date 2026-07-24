"""Immutable attestation, disposition, and derived corroboration types."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.primitives import canonical_json, new_id
from cruxible_core.temporal import ensure_utc, utc_now

AttestationStance = Literal["support", "contradict", "unsure"]
AttestationVerdict = Literal["upheld", "corrected", "invalidated"]
ClaimStateAtRecord = Literal[
    "live",
    "pending",
    "rejected",
    "inactive",
    "superseded",
    "retracted",
]
ClaimKey: TypeAlias = tuple[str, str, str, str, str]


class AttestationRecord(BaseModel):
    """One immutable observation against one relationship claim."""

    attestation_id: str = Field(default_factory=lambda: new_id("ATT"))
    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    claim_content_digest: str
    claim_state_at_record: ClaimStateAtRecord
    stance: AttestationStance
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    observed_at: datetime
    recorded_at: datetime = Field(default_factory=utc_now)
    actor_context: GovernedActorContext
    note: str | None = None
    idempotency_key: str | None = None
    receipt_id: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("observed_at", "recorded_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator(
        "relationship_type",
        "from_type",
        "from_id",
        "to_type",
        "to_id",
        "claim_content_digest",
    )
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("attestation claim coordinates and digest must be non-empty")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("idempotency_key must be non-empty when provided")
        return value

    @model_validator(mode="after")
    def validate_observation(self) -> AttestationRecord:
        if self.stance in {"support", "contradict"} and not self.evidence_refs:
            raise ValueError(f"stance '{self.stance}' requires at least one evidence ref")
        if self.observed_at > self.recorded_at:
            raise ValueError("observed_at must be less than or equal to recorded_at")
        return self

    def claim_key(self) -> ClaimKey:
        """Return the stable tuple-first claim identity."""
        return (
            self.relationship_type,
            self.from_type,
            self.from_id,
            self.to_type,
            self.to_id,
        )


class AttestationDisposition(BaseModel):
    """One immutable reviewer answer to an attestation."""

    disposition_id: str = Field(default_factory=lambda: new_id("ATD"))
    attestation_id: str
    verdict: AttestationVerdict
    reviewer_actor_context: GovernedActorContext
    note: str | None = None
    follow_up_receipt_id: str | None = None
    receipt_id: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(extra="forbid")

    @field_validator("attestation_id")
    @classmethod
    def require_attestation_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("attestation_id must be non-empty")
        return value

    @field_validator("recorded_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class AttestationRecordResult(BaseModel):
    """Receipted result of recording one observation."""

    attestation: AttestationRecord
    created_claim: bool = False
    idempotent_replay: bool = False
    warnings: list[str] = Field(default_factory=list)
    receipt_id: str | None = None


class AttestationDispositionResult(BaseModel):
    """Receipted result of appending one disposition."""

    disposition: AttestationDisposition
    receipt_id: str | None = None


class AttestationListItem(BaseModel):
    """One stored attestation plus tuple-first read-time resolution markers."""

    attestation: AttestationRecord
    latest_disposition: AttestationDisposition | None = None
    unresolved_target: bool = False
    edge_key_mismatch: bool = False
    stale_content: bool = False
    current_claim_state: ClaimStateAtRecord | None = None


class StaleContentSummary(BaseModel):
    """Counts for attestations that target a prior version of the claim tuple."""

    support_count: int = 0
    contradict_count: int = 0
    unsure_count: int = 0
    invalidated_count: int = 0


class CorroborationSummary(BaseModel):
    """Derived, zero-elided calibration summary for one current claim."""

    support_count: int = 0
    contradict_count: int = 0
    unsure_count: int = 0
    invalidated_count: int = 0
    last_supported_at: datetime | None = None
    last_contradicted_at: datetime | None = None
    distinct_actor_count: int = 0
    open_contradiction: bool = False
    stale_content: StaleContentSummary = Field(default_factory=StaleContentSummary)


class AttestationQueueEntry(BaseModel):
    """One live claim aggregated from its open current-content contradictions."""

    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    open_contradict_count: int
    distinct_contradicting_actor_count: int
    latest_observed_at: datetime


def compute_claim_content_digest(
    relationship_type: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    properties: dict[str, Any],
) -> str:
    """Return the canonical digest for one tuple plus its current properties."""
    payload = {
        "relationship_type": relationship_type,
        "from_type": from_type,
        "from_id": from_id,
        "to_type": to_type,
        "to_id": to_id,
        "properties": properties,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "AttestationDisposition",
    "AttestationDispositionResult",
    "AttestationListItem",
    "AttestationQueueEntry",
    "AttestationRecord",
    "AttestationRecordResult",
    "AttestationStance",
    "AttestationVerdict",
    "ClaimKey",
    "ClaimStateAtRecord",
    "CorroborationSummary",
    "StaleContentSummary",
    "compute_claim_content_digest",
]
