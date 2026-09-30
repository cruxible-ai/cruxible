"""Operational reads: Lines, Captures, predictions, mandates and Procedure runs.

``get`` resolves each of these by a stable reference -- ``Line:<name>`` (or the
Line identity digest ``next`` names a due Line by), ``CAP-<12+ hex>`` or
``Capture:<digest>``, ``ResolutionContract:<name>``, ``Mandate:<name>`` (or the
``ProcedureMandate:<name>`` form ``next`` emits) and ``ProcedureRun:<run_id>``
-- and answers a values-first card. ``orient(section=...)`` pages the same
families as compact rows.

An accepted artifact (a Line, a ResolutionContract, a mandate) is read at the
requested coordinate. What happens to it operationally -- arms, pending
occurrences, runs, bound prediction windows -- is operational state, read as of
now and only at the current head; a card read at an older coordinate says so in
``note`` instead of mixing generations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

#: A Capture handle is ``CAP-`` plus at least this many hex digits of its digest.
CAPTURE_HANDLE_HEX = 12
#: Arms, occurrences, runs, windows, citing Claims and per-node rows a card lists at most.
OPERATIONAL_CARD_LIST_LIMIT = 10
#: Arms and recent runs a Line card lists at most.
LINE_CARD_ARMS = 5
LINE_CARD_RUNS = 5

PlaybillRunStatus: TypeAlias = Literal[
    "running",
    "succeeded",
    "admission_refused",
    "node_refused",
    "operational_failed",
    "internal_failed",
    "halted",
]
#: What a Line arm's automation is doing, as the Line consumer reports it.
PlaybillLineArmState: TypeAlias = Literal["running", "stalled", "stopped", "disarmed"]
PlaybillMandateState: TypeAlias = Literal[
    "active", "expiring", "expired", "not_yet_valid", "suspended", "retired"
]
PlaybillPredictionWindowStatus: TypeAlias = Literal["open", "settleable", "resolved"]


def _omit_none(value: object) -> bool:
    return value is None


def _omit_empty(value: object) -> bool:
    return not value


class _StrictOperationalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def capture_handle(digest: str) -> str:
    """``CAP-<12 hex>``: the short handle a card prints for a Capture digest."""

    return "CAP-" + digest.removeprefix("sha256:")[:CAPTURE_HANDLE_HEX]


class PlaybillRunRowV1(_StrictOperationalModel):
    """One Procedure run as a list row, newest admission first; ``status`` never orders."""

    run: str
    procedure: str
    status: PlaybillRunStatus
    # The run's admission: its evaluation instant.
    started_at: datetime
    line: str | None = Field(default=None, exclude_if=_omit_none)
    nodes_done: int = Field(ge=0)


class PlaybillGetLineArmV1(_StrictOperationalModel):
    """One arm of a Line: who armed it, and what its automation is doing."""

    arm: str
    state: PlaybillLineArmState
    principal_kind: Literal["runtime_credential", "local_operator"]
    # Who armed it: the local operator, or a runtime credential's label and id.
    # A runtime credential's are shown only to that credential or an admin;
    # anyone else reads ``armed_by_withheld``.
    armed_by: str | None = Field(default=None, exclude_if=_omit_none)
    credential: str | None = Field(default=None, exclude_if=_omit_none)
    armed_by_withheld: bool = Field(default=False, exclude_if=lambda value: not value)
    armed_at: datetime
    stopped_at: datetime | None = Field(default=None, exclude_if=_omit_none)
    stop_reason: str | None = Field(default=None, exclude_if=_omit_none)
    detail: str | None = Field(default=None, exclude_if=_omit_none)
    pending_automatic: int = Field(default=0, ge=0)
    pending_explicit: int = Field(default=0, ge=0)


class PlaybillGetLineOccurrenceV1(_StrictOperationalModel):
    """One pending occurrence: ``due`` once its window has closed, else ``waiting``."""

    occurrence: str
    eligible_at: datetime
    state: Literal["due", "waiting"]


class PlaybillGetLineCardV1(_StrictOperationalModel):
    line: str
    identity_digest: str
    lifecycle: str
    procedure: str
    authority: Literal["observe", "propose", "settle"]
    trigger: str
    trigger_detail: str | None = Field(default=None, exclude_if=_omit_none)
    occurrence_epoch: int = Field(ge=1)
    arms: tuple[PlaybillGetLineArmV1, ...] = ()
    arms_total: int = Field(default=0, ge=0)
    due: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)
    occurrences: tuple[PlaybillGetLineOccurrenceV1, ...] = ()
    recent_runs: tuple[PlaybillRunRowV1, ...] = ()
    runs_total: int = Field(default=0, ge=0)
    note: str | None = Field(default=None, exclude_if=_omit_none)
    next: tuple[str, ...] = ()


class PlaybillGetCaptureCardV1(_StrictOperationalModel):
    """A retained Capture: its contract, when it was observed, and who cites it.

    ``status`` is whether the store can still produce it, as the evidence
    worker last saw (``status_detail`` names the defect); ``read_capture``
    verifies and reads its material, behind body-read permission.
    """

    capture: str
    digest: str
    contract: str
    version: int | None = Field(default=None, exclude_if=_omit_none)
    source: str | None = Field(default=None, exclude_if=_omit_none)
    observed_at: datetime
    size: int | None = Field(default=None, exclude_if=_omit_none)
    access: str | None = Field(default=None, exclude_if=_omit_none)
    status: Literal["available", "unavailable"]
    status_detail: str | None = Field(default=None, exclude_if=_omit_none)
    subjects: tuple[str, ...] = ()
    citing: tuple[str, ...] = ()
    citing_total: int = Field(default=0, ge=0)
    next: tuple[str, ...] = ()


class PlaybillGetPredictionWindowV1(_StrictOperationalModel):
    """One bound window of a ResolutionContract, as the settlement worker holds it."""

    window: str
    starts_at: datetime
    ends_at: datetime
    status: PlaybillPredictionWindowStatus


class PlaybillGetResolutionContractCardV1(_StrictOperationalModel):
    contract: str
    lifecycle: str
    hypothesis: str
    hypothesis_value: Any = None
    window: str
    rule: str
    state: Literal["open", "settleable", "resolved", "unbound", "not_observed"]
    windows: tuple[PlaybillGetPredictionWindowV1, ...] = ()
    windows_total: int = Field(default=0, ge=0)
    note: str | None = Field(default=None, exclude_if=_omit_none)
    next: tuple[str, ...] = ()


class PlaybillGetMandateCardV1(_StrictOperationalModel):
    mandate: str
    procedure: str
    grants: Literal["propose", "settle"]
    lifecycle: str
    state: PlaybillMandateState
    valid_from: datetime
    expires_at: datetime
    namespace: tuple[str, ...] = ()
    next: tuple[str, ...] = ()


class PlaybillGetRunNodeV1(_StrictOperationalModel):
    """One node that finished, in journal order.

    ``duration_us`` is absent: the journal records every event of a run at the
    run's evaluation instant (the deterministic clock), so per-node durations
    are not derivable from it.
    """

    node: str
    kind: str | None = Field(default=None, exclude_if=_omit_none)
    verdict: str | None = Field(default=None, exclude_if=_omit_none)
    sequence: int = Field(ge=1)
    duration_us: int | None = Field(default=None, ge=0, exclude_if=_omit_none)


class PlaybillGetRunCurrentNodeV1(_StrictOperationalModel):
    node: str
    kind: str
    # The journal time of the event before it: the run's evaluation instant.
    started_at: datetime


class PlaybillGetRunTriggerV1(_StrictOperationalModel):
    """What admitted a Line run: the Line, its occurrence, and the arm that dispatched it."""

    line: str
    occurrence: str | None = Field(default=None, exclude_if=_omit_none)
    arm: str | None = Field(default=None, exclude_if=_omit_none)
    principal_kind: Literal["runtime_credential", "local_operator"] | None = Field(
        default=None, exclude_if=_omit_none
    )
    # Withheld, as on a Line card, unless the reader is the arming credential or an admin.
    armed_by: str | None = Field(default=None, exclude_if=_omit_none)
    armed_by_withheld: bool = Field(default=False, exclude_if=lambda value: not value)


class PlaybillGetPendingInputV1(_StrictOperationalModel):
    name: str
    waiting_since: datetime


class PlaybillGetProcedureRunCardV1(_StrictOperationalModel):
    run: str
    procedure: str
    status: PlaybillRunStatus
    nodes_done: int = Field(ge=0)
    nodes_total: int = Field(ge=0)
    current_node: PlaybillGetRunCurrentNodeV1 | None = Field(default=None, exclude_if=_omit_none)
    # The run's admission: its evaluation instant.
    started_at: datetime
    elapsed_us: int | None = Field(default=None, ge=0, exclude_if=_omit_none)
    elapsed_basis: Literal["read_time", "measured_wall_clock"] | None = Field(
        default=None, exclude_if=_omit_none
    )
    nodes: tuple[PlaybillGetRunNodeV1, ...] = ()
    pending_inputs: tuple[PlaybillGetPendingInputV1, ...] = ()
    triggered_by: PlaybillGetRunTriggerV1 | None = Field(default=None, exclude_if=_omit_none)
    actor: str | None = Field(default=None, exclude_if=_omit_none)
    receipt_digest: str | None = Field(default=None, exclude_if=_omit_none)
    terminal: str | None = Field(default=None, exclude_if=_omit_none)
    next: tuple[str, ...] = ()


# -- orient section rows -----------------------------------------------------------


class PlaybillOrientLineV1(_StrictOperationalModel):
    line: str
    lifecycle: Literal["live", "retired"]
    procedure: str
    authority: Literal["observe", "propose", "settle"]
    trigger: str
    arm: PlaybillLineArmState | None = Field(default=None, exclude_if=_omit_none)
    due: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)


class PlaybillOrientCaptureV1(_StrictOperationalModel):
    capture: str
    contract: str
    observed_at: datetime
    citing: int = Field(ge=0)


class PlaybillOrientCaptureContractV1(_StrictOperationalModel):
    contract: str
    version: int = Field(ge=1)
    lifecycle: str
    grade: str
    admitted_by: int = Field(ge=0)


class PlaybillOrientPredictionV1(_StrictOperationalModel):
    contract: str
    hypothesis: str
    window: str
    open: int = Field(default=0, ge=0)
    settleable: int = Field(default=0, ge=0)
    resolved: int = Field(default=0, ge=0)
    next_close: datetime | None = Field(default=None, exclude_if=_omit_none)


class PlaybillOrientMandateV1(_StrictOperationalModel):
    mandate: str
    procedure: str
    grants: Literal["propose", "settle"]
    state: PlaybillMandateState
    expires_at: datetime


__all__ = [
    "CAPTURE_HANDLE_HEX",
    "LINE_CARD_ARMS",
    "LINE_CARD_RUNS",
    "OPERATIONAL_CARD_LIST_LIMIT",
    "PlaybillGetCaptureCardV1",
    "PlaybillGetLineArmV1",
    "PlaybillGetLineCardV1",
    "PlaybillGetLineOccurrenceV1",
    "PlaybillGetMandateCardV1",
    "PlaybillGetPendingInputV1",
    "PlaybillGetPredictionWindowV1",
    "PlaybillGetProcedureRunCardV1",
    "PlaybillGetResolutionContractCardV1",
    "PlaybillGetRunCurrentNodeV1",
    "PlaybillGetRunNodeV1",
    "PlaybillGetRunTriggerV1",
    "PlaybillLineArmState",
    "PlaybillMandateState",
    "PlaybillOrientCaptureContractV1",
    "PlaybillOrientCaptureV1",
    "PlaybillOrientLineV1",
    "PlaybillOrientMandateV1",
    "PlaybillOrientPredictionV1",
    "PlaybillPredictionWindowStatus",
    "PlaybillRunRowV1",
    "PlaybillRunStatus",
    "capture_handle",
]
