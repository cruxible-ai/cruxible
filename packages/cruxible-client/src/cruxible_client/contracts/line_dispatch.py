"""Line enablement, evaluation and dispatch: evaluation records work, it never admits it."""

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


class LineEvaluateRequest(BaseModel):
    """Evaluate a Line's Triggers over a range: enqueue what they make eligible, or preview it.

    Evaluation records pending occurrences for explicit dispatch and never runs
    anything. ``dry_run`` only reads: it reports the occurrences without
    enqueueing them, so it needs no range and no governed write.
    """

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
    dry_run: bool = Field(
        default=False,
        description="Report what the range makes eligible without enqueueing it (read-only).",
    )

    @field_validator("since", "until")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("trigger range requires timezone-aware timestamps")
            return ensure_utc(value)
        return value

    @model_validator(mode="after")
    def _range(self) -> LineEvaluateRequest:
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("trigger range must be increasing")
        if not self.dry_run and (self.since is None or self.until is None):
            raise ValueError(
                "evaluation that enqueues requires an explicit since and until; "
                "dry_run reads without them"
            )
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


class LineEvaluateResult(BaseModel):
    """What one evaluation found; ``pending`` marks what it enqueued (never on a dry run)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    line: str
    line_identity_digest: str
    line_artifact_digest: str
    occurrence_epoch: int
    #: The live Triggers aimed at the Line that this evaluation read.
    triggers: tuple[LineTriggerVersion, ...] = ()
    coordinate: AcceptedCoordinate
    status: Literal["met", "not_met", "incomplete"]
    occurrences: tuple[LineTriggerOccurrence, ...] = ()
    checked_since: datetime | None = Field(default=None, description="Reads VALIDITY WINDOW.")
    checked_until: datetime = Field(description="Reads VALIDITY WINDOW.")
    cursor: str | None = None
    detail: str | None = None


class LineDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurrence_id: str | None = None
    limit: int = Field(default=100, ge=1, le=100)
    cursor: str | None = Field(
        default=None,
        description=(
            "Continue after the last occurrence a previous page attempted (its `cursor`), "
            "so occurrences that stayed blocked are not attempted again."
        ),
    )
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
        if self.retry and self.occurrence_id is None:
            raise ValueError("retry requires one explicit occurrence_id")
        return self


#: Why an enablement stopped admitting work on its own. Every reason but
#: `disabled` is the daemon noticing that the authority, Line or Triggers the
#: enablement was bound to no longer hold; enabling again is the way back.
LineEnablementStopReason = Literal[
    "disabled",
    "line_changed",
    "line_retired",
    "trigger_changed",
    "epoch_changed",
    "credential_revoked",
    "credential_unbound",
    "principal_inactive",
    "credential_scope_changed",
    "permission_insufficient",
    "authentication_changed",
]


#: What one enable or disable call did. Enabling a Line already enabled with
#: the same credential, Line version and epoch, or disabling a stopped
#: enablement, changes nothing and says so.
LineEnablementOutcome = Literal[
    "enabled",
    "reenabled",
    "already_enabled",
    "disabled",
    "already_disabled",
    "would_enable",
    "would_reenable",
    "would_disable",
]


class LineEnablementPrincipal(BaseModel):
    """Who enabled a Line: the authority rechecked before every automatic admission.

    ``runtime_credential`` retains only the credential's identifier, never a
    token. On an auth-off daemon, ``principal_claim`` is an enablement made under
    a configured principal ID (``label``), whose accepted standing is rechecked
    before every admission; ``local_operator`` is the implicit local operator
    that claimed no principal, and never resolves to a registered principal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    # The dispatch store's record format (an internal name).
    tag: Literal["line-arm-principal-v2"] = "line-arm-principal-v2"
    kind: Literal["runtime_credential", "principal_claim", "local_operator"]
    credential_id: str | None = None
    label: str

    @model_validator(mode="after")
    def _credential(self) -> LineEnablementPrincipal:
        if (self.kind == "runtime_credential") != (self.credential_id is not None):
            raise ValueError("exactly a runtime-credential enablement names its credential")
        return self


class LineEnablement(BaseModel):
    """One Line's automatic dispatch: enabled forward-only, or why it stopped.

    An enabled Line admits the occurrences its daemon matched since it was
    enabled or last restarted, under the pinned Line version, the exact Trigger
    versions aimed at it when it was enabled, and the enabling credential. A
    Trigger aimed at a Line does nothing until the Line is enabled. Occurrences
    matched before a restart, or by explicit evaluation, stay pending for
    explicit dispatch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    enablement_id: str
    line: str
    line_artifact_digest: str
    occurrence_epoch: int
    #: The Trigger versions the enablement matches; any change to the Triggers
    #: aimed at the Line stops it (`trigger_changed`) until it is enabled again.
    triggers: tuple[LineTriggerVersion, ...] = ()
    state: Literal["enabled", "stopped"]
    enabled_at: datetime = Field(description="Reads VALIDITY WINDOW.")
    enabled_by: LineEnablementPrincipal
    evaluated_until: datetime = Field(description="Reads VALIDITY WINDOW.")
    stopped_at: datetime | None = Field(default=None, description="Reads VALIDITY WINDOW.")
    stop_reason: LineEnablementStopReason | None = None
    detail: str | None = None
    pending_automatic: int = Field(default=0, ge=0)
    pending_explicit: int = Field(default=0, ge=0)
    outcome: LineEnablementOutcome | None = Field(
        default=None,
        description=(
            "What this enable or disable call did. "
            "`already_enabled` and `already_disabled` changed nothing."
        ),
    )
    coordinate: AcceptedCoordinate | None = Field(
        default=None,
        description=(
            "The accepted coordinate this enable or disable call evaluated the Line at. "
            "Commit a preview with at=<its git_oid>."
        ),
    )

    @model_validator(mode="after")
    def _state(self) -> LineEnablement:
        if (self.state == "stopped") != (self.stop_reason is not None):
            raise ValueError("exactly a stopped enablement names why it stopped")
        if (self.state == "stopped") != (self.stopped_at is not None):
            raise ValueError("exactly a stopped enablement names when it stopped")
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
    #: Set when the page was full: pass it back to continue past every
    #: occurrence this page attempted, including those that stayed blocked.
    cursor: str | None = None
