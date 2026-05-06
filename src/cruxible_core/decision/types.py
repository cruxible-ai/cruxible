"""Decision record types for auditable agent and human decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from cruxible_core.primitives import new_id

DecisionStatus = Literal["open", "finalized", "abandoned"]
DecisionClass = Literal["recommended", "rejected", "deferred", "escalated"]
DecisionEventStatus = Literal["success", "error"]


class DecisionRecord(BaseModel):
    """A durable record scoped to one decision or inquiry."""

    decision_record_id: str = Field(default_factory=lambda: new_id("DR"))
    question: str
    subject_type: str | None = None
    subject_id: str | None = None
    status: DecisionStatus = "open"
    opened_by: Literal["human", "agent", "service"] = "human"
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finalized_at: datetime | None = None
    final_decision: str | None = None
    decision_class: DecisionClass | None = None
    rationale: str = ""
    abandoned_reason: str = ""

    @model_validator(mode="after")
    def validate_terminal_state(self) -> DecisionRecord:
        if self.status == "finalized" and self.decision_class is None:
            msg = "finalized decision records require decision_class"
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
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
