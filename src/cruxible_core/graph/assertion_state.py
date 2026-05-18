"""Typed helpers for relationship assertion state.

Provenance explains where an edge came from. Assertion state explains how
Cruxible should treat the edge in live graph semantics.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_serializer

RelationshipReviewStatus = Literal[
    "unreviewed",
    "pending",
    "approved",
    "rejected",
]

RelationshipReviewSource = Literal["system", "human", "agent", "group"]

RelationshipLifecycleStatus = Literal[
    "active",
    "inactive",
    "superseded",
    "retracted",
]

ASSERTION_PROPERTY = "_assertion"
LEGACY_REVIEW_STATUS_PROPERTY = "review_status"


class RelationshipReviewState(BaseModel):
    """Review/adjudication state for a relationship assertion."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    status: RelationshipReviewStatus = "unreviewed"
    source: RelationshipReviewSource = "system"
    updated_at: datetime | None = None
    updated_by: str | None = None

    @field_serializer("updated_at", when_used="json")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None


class RelationshipLifecycleState(BaseModel):
    """Lifecycle/actuality state for a relationship assertion."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    status: RelationshipLifecycleStatus = "active"
    reason: str | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    closed_at: datetime | None = None
    closed_by: str | None = None
    supersedes: dict[str, Any] | None = None
    superseded_by: dict[str, Any] | None = None

    @field_serializer(
        "effective_from",
        "effective_until",
        "closed_at",
        when_used="json",
    )
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None


class RelationshipAssertionState(BaseModel):
    """Coupled review and lifecycle state stored under relationship ``_assertion``."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    review: RelationshipReviewState = Field(default_factory=RelationshipReviewState)
    lifecycle: RelationshipLifecycleState = Field(
        default_factory=RelationshipLifecycleState
    )


def legacy_review_status_to_review_state(
    value: str | None,
) -> RelationshipReviewState:
    """Map legacy ``review_status`` values into typed review state."""
    if value == "human_approved":
        return RelationshipReviewState(status="approved", source="human")
    if value == "agent_approved":
        return RelationshipReviewState(status="approved", source="agent")
    if value == "human_rejected":
        return RelationshipReviewState(status="rejected", source="human")
    if value == "agent_rejected":
        return RelationshipReviewState(status="rejected", source="agent")
    if value == "pending_review":
        return RelationshipReviewState(status="pending")
    return RelationshipReviewState()


def review_state_to_legacy_review_status(
    review: RelationshipReviewState,
) -> str | None:
    """Map typed review state back to the legacy compatibility property."""
    if review.status == "approved":
        if review.source == "human":
            return "human_approved"
        if review.source in {"agent", "group"}:
            return "agent_approved"
        return None
    if review.status == "rejected":
        if review.source == "human":
            return "human_rejected"
        if review.source in {"agent", "group"}:
            return "agent_rejected"
        return None
    if review.status == "pending":
        return "pending_review"
    return None


def _load_stored_assertion(value: Any) -> RelationshipAssertionState | None:
    if isinstance(value, RelationshipAssertionState):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return RelationshipAssertionState.model_validate(value)
    except ValidationError:
        return None


def load_assertion_state(
    properties: Mapping[str, Any] | None,
) -> RelationshipAssertionState:
    """Load relationship assertion state with legacy compatibility fallback."""
    props = properties or {}
    assertion = _load_stored_assertion(props.get(ASSERTION_PROPERTY))
    if assertion is not None:
        return assertion

    legacy_value = props.get(LEGACY_REVIEW_STATUS_PROPERTY)
    legacy_review_status = legacy_value if isinstance(legacy_value, str) else None
    return RelationshipAssertionState(
        review=legacy_review_status_to_review_state(legacy_review_status)
    )


def dump_assertion_state(state: RelationshipAssertionState) -> dict[str, Any]:
    """Return the JSON-ready dict shape stored in graph properties."""
    return state.model_dump(mode="json", exclude_none=True)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def relationship_is_live(
    properties: Mapping[str, Any],
    *,
    require_approved: bool = False,
) -> bool:
    """Return whether a relationship participates in live graph semantics."""
    state = load_assertion_state(properties)
    if state.lifecycle.status != "active":
        return False

    now = datetime.now(timezone.utc)
    if (
        state.lifecycle.effective_from is not None
        and _to_utc(state.lifecycle.effective_from) > now
    ):
        return False
    if (
        state.lifecycle.effective_until is not None
        and _to_utc(state.lifecycle.effective_until) <= now
    ):
        return False

    if state.review.status in {"pending", "rejected"}:
        return False
    if require_approved and state.review.status != "approved":
        return False
    return True


__all__ = [
    "ASSERTION_PROPERTY",
    "LEGACY_REVIEW_STATUS_PROPERTY",
    "RelationshipAssertionState",
    "RelationshipLifecycleState",
    "RelationshipLifecycleStatus",
    "RelationshipReviewSource",
    "RelationshipReviewState",
    "RelationshipReviewStatus",
    "dump_assertion_state",
    "legacy_review_status_to_review_state",
    "load_assertion_state",
    "relationship_is_live",
    "review_state_to_legacy_review_status",
]
