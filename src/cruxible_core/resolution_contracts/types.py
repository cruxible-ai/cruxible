"""Immutable resolution-contract, activation, resolution, and disposition types.

A **resolution contract** declares, at acceptance time and in advance, what
observable result counts as success for one governed subject, how it will be
measured, when it should first be checked, and when it expires unresolved. A
**resolution** is the append-only record of what reality said.

Contracts are answered, never edited: a wrong declaration is resolved
``indeterminate`` with a note and a new contract is opened.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.primitives import canonical_json, new_id
from cruxible_core.temporal import ensure_utc, utc_now

MeasurementKind = Literal["query", "attestation"]
ResolutionVerdict = Literal["satisfied", "contradicted", "indeterminate"]
ResolutionDispositionVerdict = Literal["upheld", "overturned"]
ContractStatus = Literal["prepared", "open", "resolved"]
ConditionScope = Literal["any", "all"]
ContractQueue = Literal["due", "overdue", "contradicted"]

CONTRACT_QUEUES: tuple[ContractQueue, ...] = ("due", "overdue", "contradicted")


class MeasurementExpectation(BaseModel):
    """Count grammar plus an optional property-equality condition.

    Reuses the ``named_query_result_count`` guard count grammar. ``condition``
    is a property -> value equality map applied to entity-shaped result rows;
    ``condition_scope`` states explicitly whether ANY row or ALL rows must
    match. Every surface here is point-in-time: durability windows ("healthy
    for 7 days") are inexpressible until the temporal vocabulary lands.
    """

    min_count: int | None = Field(default=None, ge=0)
    max_count: int | None = Field(default=None, ge=0)
    condition: dict[str, str | int | float | bool] | None = None
    condition_scope: ConditionScope = "all"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_shape(self) -> MeasurementExpectation:
        if self.min_count is None and self.max_count is None and self.condition is None:
            raise ValueError(
                "measurement expect requires min_count, max_count, or condition; "
                "an expectation that constrains nothing cannot be contradicted"
            )
        if (
            self.min_count is not None
            and self.max_count is not None
            and self.min_count > self.max_count
        ):
            raise ValueError("measurement expect min_count must be <= max_count")
        if self.condition is not None and not self.condition:
            raise ValueError("measurement expect condition requires at least one property=value")
        return self


class QueryMeasurement(BaseModel):
    """Measure the outcome by re-running one named query and reading its result."""

    kind: Literal["query"] = "query"
    query_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    expect: MeasurementExpectation
    query_definition_digest: str | None = Field(
        default=None,
        description=(
            "Digest of the named query definition pinned when the contract was "
            "opened. A resolution recorded after the definition drifts may only "
            "be indeterminate: the measurement no longer means what was declared."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("query_name")
    @classmethod
    def require_query_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query measurement query_name must be non-empty")
        return value


class AttestationMeasurement(BaseModel):
    """Measure the outcome by observation against one relationship claim tuple."""

    kind: Literal["attestation"] = "attestation"
    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str

    model_config = ConfigDict(extra="forbid")

    @field_validator("relationship_type", "from_type", "from_id", "to_type", "to_id")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("attestation measurement claim coordinates must be non-empty")
        return value

    def claim_key(self) -> tuple[str, str, str, str, str]:
        """Return the stable tuple-first claim identity being measured."""
        return (
            self.relationship_type,
            self.from_type,
            self.from_id,
            self.to_type,
            self.to_id,
        )


ContractMeasurement = Annotated[
    QueryMeasurement | AttestationMeasurement,
    Field(discriminator="kind"),
]


class ContractDeclaration(BaseModel):
    """What success means, how it is measured, when it is checked and expires."""

    description: str
    check_at: datetime
    expires_at: datetime
    measurement: ContractMeasurement

    model_config = ConfigDict(extra="forbid")

    @field_validator("check_at", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("description")
    @classmethod
    def require_description(cls, value: str) -> str:
        # Free text survives mechanical rot: the query may be renamed, the claim
        # retracted; the stated success criterion still says what was meant.
        if not value.strip():
            raise ValueError("contract description is required and must be non-empty")
        return value

    @model_validator(mode="after")
    def validate_clock(self) -> ContractDeclaration:
        if self.check_at >= self.expires_at:
            raise ValueError("contract check_at must be strictly before expires_at")
        return self


class ResolutionContract(BaseModel):
    """One prepared or activated commitment attached to one governed subject."""

    contract_id: str = Field(default_factory=lambda: new_id("RSC"))
    entity_type: str
    entity_id: str
    subject_content_digest: str
    declaration: ContractDeclaration
    opened_at: datetime = Field(default_factory=utc_now)
    actor_context: GovernedActorContext
    idempotency_key: str | None = None
    receipt_id: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("opened_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("entity_type", "entity_id", "subject_content_digest")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("contract subject coordinates and digest must be non-empty")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("idempotency_key must be non-empty when provided")
        return value

    def subject_key(self) -> tuple[str, str]:
        """Return the subject entity coordinates."""
        return (self.entity_type, self.entity_id)


class ContractActivation(BaseModel):
    """The durable contract-to-acceptance join, written by the accepting write.

    ``subject_content_digest`` here is the digest of the content that was
    ACCEPTED (the post-transition entity), which is deliberately a different
    coordinate from :attr:`ResolutionContract.subject_content_digest` — the
    pre-transition content the contract committed to. Both are kept so an
    episode export can state what was promised and what was ratified.
    """

    activation_id: str = Field(default_factory=lambda: new_id("RSA"))
    contract_id: str
    acceptance_receipt_id: str | None = None
    subject_content_digest: str
    activated_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(extra="forbid")

    @field_validator("activated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ContractResolution(BaseModel):
    """One immutable answer to one activated contract."""

    resolution_id: str = Field(default_factory=lambda: new_id("RSR"))
    contract_id: str
    sequence: int = Field(ge=1)
    verdict: ResolutionVerdict
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    observed_at: datetime
    recorded_at: datetime = Field(default_factory=utc_now)
    actor_context: GovernedActorContext
    note: str | None = None
    resolving_query_receipt_id: str | None = None
    resolving_attestation_ids: list[str] = Field(default_factory=list)
    receipt_id: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("observed_at", "recorded_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def validate_observation(self) -> ContractResolution:
        if self.verdict in {"satisfied", "contradicted"} and not self.evidence_refs:
            raise ValueError(f"verdict '{self.verdict}' requires at least one evidence ref")
        if self.verdict == "contradicted" and not (self.note or "").strip():
            # The note is the corpus this feature exists to build.
            raise ValueError("verdict 'contradicted' requires a note")
        if self.observed_at > self.recorded_at:
            raise ValueError("observed_at must be less than or equal to recorded_at")
        return self


class ResolutionDisposition(BaseModel):
    """One immutable reviewer answer to a resolution."""

    disposition_id: str = Field(default_factory=lambda: new_id("RSD"))
    resolution_id: str
    verdict: ResolutionDispositionVerdict
    reviewer_actor_context: GovernedActorContext
    note: str | None = None
    receipt_id: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(extra="forbid")

    @field_validator("resolution_id")
    @classmethod
    def require_resolution_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("resolution_id must be non-empty")
        return value

    @field_validator("recorded_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ContractOpenResult(BaseModel):
    """Receipted result of opening one resolution contract."""

    contract: ResolutionContract
    idempotent_replay: bool = False
    receipt_id: str | None = None


class ContractResolveResult(BaseModel):
    """Receipted result of recording one resolution."""

    resolution: ContractResolution
    receipt_id: str | None = None


class ContractDispositionResult(BaseModel):
    """Receipted result of appending one reviewer disposition."""

    disposition: ResolutionDisposition
    receipt_id: str | None = None


class ContractListItem(BaseModel):
    """One stored contract plus derived, read-time lifecycle markers."""

    contract: ResolutionContract
    status: ContractStatus
    activation: ContractActivation | None = None
    latest_resolution: ContractResolution | None = None
    latest_disposition: ResolutionDisposition | None = None
    expired: bool = False
    subject_present: bool = False
    subject_content_drifted: bool = False


class ContractQueueEntry(BaseModel):
    """One queued contract aggregated under its live subject."""

    contract_id: str
    entity_type: str
    entity_id: str
    description: str
    check_at: datetime
    expires_at: datetime
    overdue: bool = False
    measurement_kind: MeasurementKind
    latest_resolution: ContractResolution | None = None


def compute_entity_content_digest(
    entity_type: str,
    entity_id: str,
    properties: dict[str, Any],
) -> str:
    """Return the canonical digest for one entity's coordinates plus properties."""
    payload = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "properties": properties,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def compute_query_definition_digest(definition: Any) -> str:
    """Return the canonical digest for one named query definition.

    ``definition`` is a ``NamedQuerySchema`` (or any pydantic model / mapping).
    The digest is what makes definition drift detectable at resolution time
    without re-reading the config that was in force at open.
    """
    payload = (
        definition.model_dump(mode="json", exclude_none=True)
        if isinstance(definition, BaseModel)
        else definition
    )
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "CONTRACT_QUEUES",
    "AttestationMeasurement",
    "ConditionScope",
    "ContractActivation",
    "ContractDeclaration",
    "ContractDispositionResult",
    "ContractListItem",
    "ContractMeasurement",
    "ContractOpenResult",
    "ContractQueue",
    "ContractQueueEntry",
    "ContractResolution",
    "ContractResolveResult",
    "ContractStatus",
    "MeasurementExpectation",
    "MeasurementKind",
    "QueryMeasurement",
    "ResolutionContract",
    "ResolutionDisposition",
    "ResolutionDispositionVerdict",
    "ResolutionVerdict",
    "compute_entity_content_digest",
    "compute_query_definition_digest",
]
