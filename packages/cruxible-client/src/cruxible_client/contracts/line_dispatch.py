"""Line trigger discovery. Checks describe evidence; they never admit work."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.procedures.windows import LineTriggerBindingV1
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc


class LineTriggerCheckRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    since: datetime | None = Field(
        default=None, description="Reads VALIDITY WINDOW. inclusive eligibility bound."
    )
    until: datetime | None = Field(
        default=None,
        description="Reads VALIDITY WINDOW. exclusive eligibility bound, capped at daemon time.",
    )
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=256)

    @field_validator("since", "until")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("trigger range requires timezone-aware timestamps")
            return ensure_utc(value)
        return value

    @model_validator(mode="after")
    def _range(self) -> LineTriggerCheckRequestV1:
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("trigger range must be increasing")
        return self


class LineTriggerOccurrenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurrence_id: str
    binding: LineTriggerBindingV1 | None
    eligible_at: datetime = Field(description="Reads VALIDITY WINDOW.")
    admitted_run_id: str | None = None
    pending: bool = False


class LineTriggerCheckResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line: str
    line_identity_digest: str
    line_artifact_digest: str
    occurrence_epoch: int
    coordinate: AcceptedCoordinate
    status: Literal["met", "not_met", "incomplete"]
    occurrences: tuple[LineTriggerOccurrenceV1, ...] = ()
    checked_since: datetime | None = Field(default=None, description="Reads VALIDITY WINDOW.")
    checked_until: datetime = Field(description="Reads VALIDITY WINDOW.")
    cursor: str | None = None
    detail: str | None = None


class LineListenRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["start", "stop"]


class LineEvaluateRequestV1(LineTriggerCheckRequestV1):
    @model_validator(mode="after")
    def _explicit_range(self) -> LineEvaluateRequestV1:
        if self.since is None or self.until is None:
            raise ValueError("historical evaluation requires an explicit since and until")
        return self


class LineDispatchRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurrence_id: str | None = None
    limit: int = Field(default=1, ge=1, le=100)


class LineListeningSessionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    line: str
    occurrence_epoch: int
    starts_at: datetime = Field(description="Reads VALIDITY WINDOW.")
    stops_at: datetime | None = Field(default=None, description="Reads VALIDITY WINDOW.")
    evaluated_until: datetime = Field(description="Reads VALIDITY WINDOW.")
    detail: str | None = None


class LineDispatchItemV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurrence_id: str
    status: Literal["admitted", "pending", "blocked"]
    run_id: str | None = None
    detail: str | None = None


class LineDispatchResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[LineDispatchItemV1, ...] = ()
