"""Line trigger discovery. Checks describe evidence; they never admit work."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusal,
    ProcedureNodeRefusal,
)
from cruxible_client.contracts.procedures.windows import LineTriggerBinding
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc


class LineTriggerCheckRequest(BaseModel):
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
    def _range(self) -> LineTriggerCheckRequest:
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("trigger range must be increasing")
        return self


class LineTriggerVersion(BaseModel):
    """One exact version of a live Trigger aimed at a Line."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    trigger: str
    artifact_digest: str


class LineTriggerOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurrence_id: str
    binding: LineTriggerBinding | None
    eligible_at: datetime = Field(description="Reads VALIDITY WINDOW.")
    admitted_run_id: str | None = None
    pending: bool = False
    dispatch_status: Literal["pending", "admitted", "rejected", "superseded", "lapsed"] | None = (
        None
    )


class LineTriggerCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line: str
    line_identity_digest: str
    line_artifact_digest: str
    occurrence_epoch: int
    #: The live Triggers aimed at the Line that this check evaluated.
    triggers: tuple[LineTriggerVersion, ...] = ()
    coordinate: AcceptedCoordinate
    status: Literal["met", "not_met", "incomplete"]
    occurrences: tuple[LineTriggerOccurrence, ...] = ()
    checked_since: datetime | None = Field(default=None, description="Reads VALIDITY WINDOW.")
    checked_until: datetime = Field(description="Reads VALIDITY WINDOW.")
    cursor: str | None = None
    detail: str | None = None


class LineEvaluateRequest(LineTriggerCheckRequest):
    @model_validator(mode="after")
    def _explicit_range(self) -> LineEvaluateRequest:
        if self.since is None or self.until is None:
            raise ValueError("historical evaluation requires an explicit since and until")
        return self


class LineDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurrence_id: str | None = None
    limit: int = Field(default=1, ge=1, le=100)
    retry: bool = Field(
        default=False,
        description=(
            "Explicitly retry one closed or blocked occurrence against "
            "the current Line version in the same epoch, while its Trigger still "
            "aims at the Line unchanged."
        ),
    )

    @model_validator(mode="after")
    def _retry_target(self) -> LineDispatchRequest:
        if self.retry and (self.occurrence_id is None or self.limit != 1):
            raise ValueError("retry requires one explicit occurrence_id and limit=1")
        return self


#: Why an arm stopped admitting work on its own. Every reason but `disarmed`
#: is the daemon noticing that the authority, Line or Triggers the arm was
#: bound to no longer hold; rearming is the explicit way back.
LineArmStopReason = Literal[
    "disarmed",
    "line_changed",
    "trigger_changed",
    "epoch_changed",
    "credential_revoked",
    "credential_unbound",
    "principal_inactive",
    "credential_scope_changed",
    "permission_insufficient",
    "authentication_changed",
]


#: What one arm or disarm call did. Arming an arm that already stands with the
#: same credential, Line version and epoch, or disarming a stopped arm, changes
#: nothing and says so.
LineArmOutcome = Literal[
    "armed",
    "rearmed",
    "already_armed",
    "disarmed",
    "already_disarmed",
    "would_arm",
    "would_rearm",
    "would_disarm",
]


class LineArmPrincipal(BaseModel):
    """Who armed a Line: the authority rechecked before every automatic admission.

    ``runtime_credential`` retains only the credential's identifier, never a
    token. On an auth-off daemon, ``principal_claim`` is an arm made under a
    configured principal ID (``label``), whose accepted standing is rechecked
    before every admission; ``local_operator`` is the implicit local operator
    that claimed no principal, and never resolves to a registered principal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["line-arm-principal-v2"] = "line-arm-principal-v2"
    kind: Literal["runtime_credential", "principal_claim", "local_operator"]
    credential_id: str | None = None
    label: str

    @model_validator(mode="after")
    def _credential(self) -> LineArmPrincipal:
        if (self.kind == "runtime_credential") != (self.credential_id is not None):
            raise ValueError("exactly a runtime-credential arm names its credential")
        return self


class LineArm(BaseModel):
    """One Line's automatic dispatch: armed forward-only, or why it stopped.

    An armed Line admits the occurrences its daemon matched since it was armed
    or last restarted, under the pinned Line version, the exact Trigger versions
    aimed at it when it was armed, and the arming credential. Occurrences
    matched before a restart, or by explicit evaluation, stay pending for
    explicit dispatch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    arm_id: str
    line: str
    line_artifact_digest: str
    occurrence_epoch: int
    #: The Trigger versions the arm matches; any change to the Triggers aimed
    #: at the Line stops it (`trigger_changed`).
    triggers: tuple[LineTriggerVersion, ...] = ()
    state: Literal["armed", "stopped"]
    armed_at: datetime = Field(description="Reads VALIDITY WINDOW.")
    armed_by: LineArmPrincipal
    evaluated_until: datetime = Field(description="Reads VALIDITY WINDOW.")
    stopped_at: datetime | None = Field(default=None, description="Reads VALIDITY WINDOW.")
    stop_reason: LineArmStopReason | None = None
    detail: str | None = None
    pending_automatic: int = Field(default=0, ge=0)
    pending_explicit: int = Field(default=0, ge=0)
    outcome: LineArmOutcome | None = Field(
        default=None,
        description=(
            "What this arm or disarm call did; absent on a status read. "
            "`already_armed` and `already_disarmed` changed nothing."
        ),
    )
    coordinate: AcceptedCoordinate | None = Field(
        default=None,
        description=(
            "The accepted coordinate this arm or disarm call evaluated the Line at; "
            "absent on a status read. Commit a preview with at=<its git_oid>."
        ),
    )

    @model_validator(mode="after")
    def _state(self) -> LineArm:
        if (self.state == "stopped") != (self.stop_reason is not None):
            raise ValueError("exactly a stopped arm names why it stopped")
        if (self.state == "stopped") != (self.stopped_at is not None):
            raise ValueError("exactly a stopped arm names when it stopped")
        return self


class LineDispatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurrence_id: str
    status: Literal["admitted", "pending", "blocked", "rejected", "superseded"]
    run_id: str | None = None
    detail: str | None = None
    refusal: ProcedureAdmissionRefusal | ProcedureNodeRefusal | None = None


class LineDispatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[LineDispatchItem, ...] = ()
