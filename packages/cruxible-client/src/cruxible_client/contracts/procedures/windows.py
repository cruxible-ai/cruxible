"""Shared observation windows for contracts and Line trigger eligibility."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import ArtifactDigest, Sha256Value
from cruxible_client.contracts.temporal import ensure_utc, format_datetime


class _WindowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class CaptureEventSelectorV1(_WindowModel):
    """Match a durable produced-capture event, never an accepted-head change."""

    capture_contract_identity: ArtifactIdentity
    capture_contract_digest: str

    @model_validator(mode="after")
    def _kind(self) -> CaptureEventSelectorV1:
        if self.capture_contract_identity.kind != "CaptureContract":
            raise ValueError("capture event selector must name a CaptureContract")
        return self

    @field_validator("capture_contract_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class TriggerEventReferenceV1(_WindowModel):
    run_id: str = Field(min_length=1)
    partition_id: str = Field(min_length=1)
    sequence: int = Field(ge=1, description="Reads SETTLEMENT ORDER.")
    record_digest: str

    @field_validator("record_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class FixedWindowV1(_WindowModel):
    kind: Literal["fixed"] = "fixed"
    starts_at: datetime = Field(description="Reads VALIDITY WINDOW.")
    duration_seconds: int = Field(gt=0, description="Reads VALIDITY WINDOW.")

    @field_validator("starts_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("starts_at", when_used="json")
    def _serialize(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered


class CaptureEventWindowV1(_WindowModel):
    kind: Literal["capture_event"] = "capture_event"
    event: CaptureEventSelectorV1
    duration_seconds: int = Field(gt=0, description="Reads VALIDITY WINDOW.")


ObservationWindowV1 = Annotated[FixedWindowV1 | CaptureEventWindowV1, Field(discriminator="kind")]


class BoundObservationWindowV1(_WindowModel):
    """Exact boundaries retained at admission, including the verified event anchor."""

    starts_at: datetime = Field(description="Reads VALIDITY WINDOW.")
    ends_at: datetime = Field(description="Reads VALIDITY WINDOW.")
    event: TriggerEventReferenceV1 | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("starts_at", "ends_at", when_used="json")
    def _serialize(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @model_validator(mode="after")
    def _ordered(self) -> BoundObservationWindowV1:
        if self.ends_at <= self.starts_at:
            raise ValueError("observation window must be finite and increasing")
        return self


def bind_observation_window(
    policy: ObservationWindowV1,
    *,
    event: TriggerEventReferenceV1 | None = None,
    event_time: datetime | None = None,
) -> BoundObservationWindowV1:
    """The caller authenticates the event and selector before supplying its time."""
    if isinstance(policy, FixedWindowV1):
        if event is not None or event_time is not None:
            raise ValueError("fixed window cannot accept an event anchor")
        start = policy.starts_at
    else:
        if event is None or event_time is None:
            raise ValueError("event window is waiting for its exact retained event")
        start = ensure_utc(event_time)
    return BoundObservationWindowV1(
        starts_at=start,
        ends_at=start + timedelta(seconds=policy.duration_seconds),
        event=event,
    )


class LineTriggerBindingV1(_WindowModel):
    """Semantic cause of one occurrence, independent of its dispatch instant."""

    kind: Literal["capture_landing", "window_close"]
    event: TriggerEventReferenceV1 | None = None
    window: BoundObservationWindowV1 | None = None

    @model_validator(mode="after")
    def _shape(self) -> LineTriggerBindingV1:
        if self.kind == "capture_landing":
            if self.event is None or self.window is not None:
                raise ValueError("capture trigger must bind exactly one retained event")
        elif self.window is None or self.event != self.window.event:
            raise ValueError("window trigger must reproduce its event anchor")
        return self
