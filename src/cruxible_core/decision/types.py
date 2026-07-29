"""Decision record types and the payload digest shared by decision events."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from cruxible_core.deprecation import DECISION_OPENED_BY_INPUT, accept_deprecated_model_input
from cruxible_core.governance.actors import (
    DerivedActorKind,
    GovernedActorContext,
    derived_actor_kind,
)
from cruxible_core.primitives import new_id
from cruxible_core.temporal import utc_now

DecisionStatus = Literal["open", "finalized", "abandoned"]
DecisionClass = Literal["recommended", "rejected", "deferred", "escalated"]
DecisionEventStatus = Literal["success", "error"]

_SUMMARY_CHARS = 200


def digest_payload(payload: Any) -> tuple[str, str]:
    """Return deterministic digest and bounded summary for a JSON-like payload.

    Lives beside the types rather than in the service layer so the store can
    stamp its own lifecycle events with the identical digest shape without
    importing upward into ``cruxible_core.service``.
    """
    canonical = json.dumps(payload, sort_keys=True, default=str)
    digest = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    return digest, canonical[:_SUMMARY_CHARS]


class DecisionRecord(BaseModel):
    """A durable record scoped to one decision or inquiry."""

    decision_record_id: str = Field(default_factory=lambda: new_id("DR"))
    question: str
    subject_type: str | None = None
    subject_id: str | None = None
    status: DecisionStatus = "open"
    opened_actor_context: GovernedActorContext | None = None
    opened_at: datetime = Field(default_factory=utc_now)
    finalized_at: datetime | None = None
    finalized_actor_context: GovernedActorContext | None = None
    final_decision: str | None = None
    decision_class: DecisionClass | None = None
    rationale: str = ""
    abandoned_reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def accept_deprecated_opened_by(cls, value: Any) -> Any:
        """Accept and ignore the caller-declared opener compatibility input."""
        return accept_deprecated_model_input(
            value,
            field="opened_by",
            notice=DECISION_OPENED_BY_INPUT,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def opened_by(self) -> DerivedActorKind:
        """Deprecated: read-only projection of the opening actor's derived kind.

        The caller-declared ``opened_by`` axis is retired; this re-emits the old
        field name as a value DERIVED from ``opened_actor_context``, which is
        what the ``opened_by`` SQL column already stores. Never writable; removal
        follows 0.3. Read ``opened_actor_context`` instead.
        """
        return derived_actor_kind(self.opened_actor_context)

    @model_validator(mode="after")
    def validate_terminal_state(self) -> DecisionRecord:
        if self.status == "finalized":
            missing: list[str] = []
            if self.decision_class is None:
                missing.append("decision_class")
            if self.final_decision is None:
                missing.append("final_decision")
            if self.finalized_at is None:
                missing.append("finalized_at")
            if missing:
                joined = ", ".join(missing)
                msg = f"finalized decision records require {joined}"
                raise ValueError(msg)
        return self


class DecisionEvent(BaseModel):
    """Append-only event captured while supporting a decision."""

    decision_event_id: str = Field(default_factory=lambda: new_id("DE"))
    decision_record_id: str
    sequence: int = 0
    command: str
    status: DecisionEventStatus
    input_digest: str
    input_summary: str
    output_digest: str | None = None
    output_summary: str | None = None
    receipt_id: str | None = None
    trace_ids: list[str] = Field(default_factory=list)
    head_snapshot_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    surface: Literal["cli", "mcp", "http", "local"] | None = None
    request_id: str | None = None
    actor_context: GovernedActorContext | None = None
    started_at: datetime
    finished_at: datetime
