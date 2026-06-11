"""Normalized actor context for hosted governed runtime writes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
)

from cruxible_core.errors import ConfigError
from cruxible_core.temporal import ensure_utc, format_datetime

ActorType = Literal["human_user", "service_account", "system"]


class GovernedActorContext(BaseModel):
    """Product actor context supplied by Cloud/API for hosted governed writes."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    actor_type: ActorType
    actor_id: str = Field(min_length=1)
    org_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    timestamp: datetime
    request_id: str | None = None

    @field_validator("actor_id", "org_id", "operation_id", "request_id")
    @classmethod
    def _nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("timestamp")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("timestamp", when_used="json")
    def _serialize_timestamp(self, value: datetime) -> str | None:
        return format_datetime(value)


def dump_actor_context(actor: GovernedActorContext | None) -> dict[str, Any] | None:
    """Return the JSON-ready actor context shape."""
    if actor is None:
        return None
    return actor.model_dump(mode="json", exclude_none=True)


def load_actor_context(value: Any) -> GovernedActorContext | None:
    """Parse persisted actor context, returning None for absent values."""
    if value is None:
        return None
    if isinstance(value, GovernedActorContext):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict):
        return None
    try:
        return GovernedActorContext.model_validate(value)
    except ValidationError:
        return None


def require_hosted_actor_context(value: Any) -> GovernedActorContext:
    """Return a validated hosted actor context or raise a safe config error."""
    if isinstance(value, GovernedActorContext):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    try:
        return GovernedActorContext.model_validate(value)
    except ValidationError as exc:
        raise ConfigError("hosted governed actor context is required") from exc


__all__ = [
    "ActorType",
    "GovernedActorContext",
    "dump_actor_context",
    "load_actor_context",
    "require_hosted_actor_context",
]
