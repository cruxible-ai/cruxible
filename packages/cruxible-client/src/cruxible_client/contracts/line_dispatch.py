"""Line trigger discovery. Checks describe evidence; they never admit work."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalV1,
    ProcedureNodeRefusalV1,
)
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
    dispatch_status: Literal["pending", "admitted", "rejected", "superseded", "lapsed"] | None = (
        None
    )


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
    retry: bool = Field(
        default=False,
        description=(
            "Explicitly retry one closed or blocked occurrence against "
            "the current Line version in the same epoch."
        ),
    )

    @model_validator(mode="after")
    def _retry_target(self) -> LineDispatchRequestV1:
        if self.retry and (self.occurrence_id is None or self.limit != 1):
            raise ValueError("retry requires one explicit occurrence_id and limit=1")
        return self


#: Why an arm stopped admitting work on its own. Every reason but `disarmed`
#: is the daemon noticing that the authority or Line the arm was bound to no
#: longer holds; rearming is the explicit way back.
LineArmStopReasonV1 = Literal[
    "disarmed",
    "line_changed",
    "epoch_changed",
    "credential_revoked",
    "credential_scope_changed",
    "permission_insufficient",
    "authentication_changed",
]


class LineArmPrincipalV1(BaseModel):
    """Who armed a Line: the credential rechecked before every automatic admission.

    Only the credential's identifier is retained, never a token.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["runtime_credential", "local_operator"]
    credential_id: str | None = None
    label: str

    @model_validator(mode="after")
    def _credential(self) -> LineArmPrincipalV1:
        if (self.kind == "runtime_credential") != (self.credential_id is not None):
            raise ValueError("exactly a runtime-credential arm names its credential")
        return self


class LineArmV1(BaseModel):
    """One Line's automatic dispatch: armed forward-only, or why it stopped.

    An armed Line admits the occurrences its daemon matched since it was armed
    or last restarted, under the pinned Line version and the arming credential.
    Occurrences matched before a restart, or by explicit evaluation, stay
    pending for explicit dispatch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    arm_id: str
    line: str
    line_artifact_digest: str
    occurrence_epoch: int
    state: Literal["armed", "stopped"]
    armed_at: datetime = Field(description="Reads VALIDITY WINDOW.")
    armed_by: LineArmPrincipalV1
    evaluated_until: datetime = Field(description="Reads VALIDITY WINDOW.")
    stopped_at: datetime | None = Field(default=None, description="Reads VALIDITY WINDOW.")
    stop_reason: LineArmStopReasonV1 | None = None
    detail: str | None = None
    pending_automatic: int = Field(default=0, ge=0)
    pending_explicit: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _state(self) -> LineArmV1:
        if (self.state == "stopped") != (self.stop_reason is not None):
            raise ValueError("exactly a stopped arm names why it stopped")
        if (self.state == "stopped") != (self.stopped_at is not None):
            raise ValueError("exactly a stopped arm names when it stopped")
        return self


class LineDispatchItemV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurrence_id: str
    status: Literal["admitted", "pending", "blocked", "rejected", "superseded"]
    run_id: str | None = None
    detail: str | None = None
    refusal: ProcedureAdmissionRefusalV1 | ProcedureNodeRefusalV1 | None = None


class LineDispatchResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[LineDispatchItemV1, ...] = ()
