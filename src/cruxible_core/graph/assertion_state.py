"""Typed helpers for relationship assertion state.

Provenance explains where an edge came from. Assertion state explains how
Cruxible should treat the edge in live graph semantics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from cruxible_core.temporal import ensure_utc, format_datetime, is_effective

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

class RelationshipReviewState(BaseModel):
    """Review/adjudication state for a relationship assertion."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    status: RelationshipReviewStatus = "unreviewed"
    source: RelationshipReviewSource = "system"
    updated_at: datetime | None = None
    updated_by: str | None = None

    @field_validator("updated_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_serializer("updated_at", when_used="json")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return format_datetime(value)


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

    @field_validator("effective_from", "effective_until", "closed_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_serializer(
        "effective_from",
        "effective_until",
        "closed_at",
        when_used="json",
    )
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return format_datetime(value)


class RelationshipAssertion(BaseModel):
    """Coupled review and lifecycle state for a relationship."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    review: RelationshipReviewState = Field(default_factory=RelationshipReviewState)
    lifecycle: RelationshipLifecycleState = Field(
        default_factory=RelationshipLifecycleState
    )


def dump_assertion(assertion: RelationshipAssertion) -> dict[str, Any]:
    """Return the JSON-ready relationship assertion shape."""
    return assertion.model_dump(mode="json", exclude_none=True)


def relationship_assertion_from_metadata(value: Any) -> RelationshipAssertion:
    """Load relationship assertion state from metadata-like input."""
    if value is None:
        return RelationshipAssertion()
    if isinstance(value, RelationshipAssertion):
        return value
    assertion = getattr(value, "assertion", None)
    if isinstance(assertion, RelationshipAssertion):
        return assertion
    if isinstance(value, dict):
        if not value:
            return RelationshipAssertion()
        if "assertion" in value:
            return RelationshipAssertion.model_validate(value.get("assertion") or {})
        if "review" in value or "lifecycle" in value:
            return RelationshipAssertion.model_validate(value)
    raise TypeError("relationship liveness requires a RelationshipAssertion or metadata")


def relationship_lifecycle_is_active(assertion_or_metadata: Any = None) -> bool:
    """Return whether relationship lifecycle permits current participation."""
    assertion = relationship_assertion_from_metadata(assertion_or_metadata)
    if assertion.lifecycle.status != "active":
        return False

    if not is_effective(
        effective_from=assertion.lifecycle.effective_from,
        effective_until=assertion.lifecycle.effective_until,
    ):
        return False
    return True


def relationship_is_live(
    assertion_or_metadata: Any = None,
    *,
    require_approved: bool = False,
) -> bool:
    """Return whether a relationship participates in live graph semantics."""
    assertion = relationship_assertion_from_metadata(assertion_or_metadata)
    if not relationship_lifecycle_is_active(assertion):
        return False

    if assertion.review.status in {"pending", "rejected"}:
        return False
    if require_approved and assertion.review.status != "approved":
        return False
    return True


__all__ = [
    "RelationshipAssertion",
    "RelationshipLifecycleState",
    "RelationshipLifecycleStatus",
    "RelationshipReviewSource",
    "RelationshipReviewState",
    "RelationshipReviewStatus",
    "dump_assertion",
    "relationship_assertion_from_metadata",
    "relationship_is_live",
    "relationship_lifecycle_is_active",
]
