"""Operational reads: Lines, Captures, predictions, mandates and Procedure runs.

``get`` resolves each of these by a stable reference -- ``Line:<name>`` (or the
Line identity digest ``next`` names a due Line by), ``CAP-<12+ hex>`` or
``Capture:<digest>``, ``ResolutionContract:<name>``, ``Mandate:<name>`` (or the
``ProcedureMandate:<name>`` form ``next`` emits) and ``ProcedureRun:<run_id>``
-- and answers a values-first card. ``orient(section=...)`` pages the same
families as compact rows.

An accepted artifact (a Line, a ResolutionContract, a mandate) is read at the
requested coordinate. What happens to it operationally -- arms, pending
occurrences, runs, bound prediction windows, capture availability -- has no
history: it is read as of now at the current head, whatever coordinate the read
names, and the answer says so with ``live`` (``LiveView``), which
names that head and the fields read live. No answer mixes the two unannounced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.line_dispatch import LineTriggerVersion
from cruxible_client.contracts.procedures.results import (
    ProcedureTerminal,
    ProcedureTerminalEgress,
)
from cruxible_client.contracts.procedures.windows import TriggerEventReference

#: A Capture handle is ``CAP-`` plus at least this many hex digits of its digest.
CAPTURE_HANDLE_HEX = 12
#: Arms, occurrences, runs, windows, citing Claims and per-node rows a card lists at most.
OPERATIONAL_CARD_LIST_LIMIT = 10
#: Arms and recent runs a Line card lists at most.
LINE_CARD_ARMS = 5
LINE_CARD_RUNS = 5

RunStatus: TypeAlias = Literal[
    "running",
    "succeeded",
    "admission_refused",
    "node_refused",
    "operational_failed",
    "internal_failed",
    "halted",
]
#: What a Line arm's automation is doing, as the Line consumer reports it.
LineEnablementState: TypeAlias = Literal["running", "stalled", "stopped", "disabled"]
#: Why a Line's Triggers run nothing: a Trigger aimed at a Line that is not
#: enabled does nothing.
LineTriggersInactive: TypeAlias = Literal["not enabled"]
MandateState: TypeAlias = Literal[
    "active", "expiring", "expired", "not_yet_valid", "suspended", "retired"
]
PredictionWindowStatus: TypeAlias = Literal["open", "settleable", "resolved"]


def _omit_none(value: object) -> bool:
    return value is None


def _omit_empty(value: object) -> bool:
    return not value


class _StrictOperationalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def capture_handle(digest: str) -> str:
    """``CAP-<12 hex>``: the short handle a card prints for a Capture digest."""

    return "CAP-" + digest.removeprefix("sha256:")[:CAPTURE_HANDLE_HEX]


class LiveHead(_StrictOperationalModel):
    """The accepted head a live read was taken at: its git oid's 12-hex prefix and sequence."""

    git_oid: str = Field(pattern=r"^[0-9a-f]{12}$")
    generation: int = Field(ge=0)


class LiveView(_StrictOperationalModel):
    """Marks the parts of an answer read live: operational state has no history.

    ``as_of`` is the current head those parts were read at, which differs from
    the answer's own coordinate when the read named an older ``at``; ``fields``
    names what was read live (``card`` when the whole card is).
    """

    tag: Literal["playbill-live-view-v1"] = "playbill-live-view-v1"
    as_of: LiveHead
    fields: tuple[str, ...]


class RunRow(_StrictOperationalModel):
    """One Procedure run as a list row, newest admission first; ``status`` never orders."""

    run: str
    procedure: str
    status: RunStatus
    # The run's admission: its evaluation instant.
    started_at: datetime
    line: str | None = Field(default=None, exclude_if=_omit_none)
    nodes_done: int = Field(ge=0)


#: Who armed a Line, as a card shows it.
EnablementPrincipalKind: TypeAlias = Literal[
    "runtime_credential", "principal_claim", "local_operator"
]


class GetLineEnablement(_StrictOperationalModel):
    """One enablement of a Line: who enabled it, and what its automation is doing."""

    enablement: str
    state: LineEnablementState
    principal_kind: EnablementPrincipalKind
    # Who enabled it: the local operator, a claimed principal, or a runtime
    # credential's label and id. A runtime credential's are shown only to an
    # admin, that credential, or another credential bound to its principal;
    # anyone else reads ``enabled_by_withheld``.
    enabled_by: str | None = Field(default=None, exclude_if=_omit_none)
    credential: str | None = Field(default=None, exclude_if=_omit_none)
    enabled_by_withheld: bool = Field(default=False, exclude_if=lambda value: not value)
    enabled_at: datetime
    #: The Line version and the exact Trigger versions this enablement is pinned
    #: to; any change to them stops it until the Line is enabled again.
    line_artifact_digest: str
    triggers: tuple[LineTriggerVersion, ...] = ()
    #: The instant through which the daemon matched this enablement's Triggers.
    evaluated_until: datetime
    stopped_at: datetime | None = Field(default=None, exclude_if=_omit_none)
    stop_reason: str | None = Field(default=None, exclude_if=_omit_none)
    detail: str | None = Field(default=None, exclude_if=_omit_none)
    pending_automatic: int = Field(default=0, ge=0)
    pending_explicit: int = Field(default=0, ge=0)


class GetLineOccurrence(_StrictOperationalModel):
    """One pending occurrence: ``due`` once its window has closed, else ``waiting``."""

    occurrence: str
    eligible_at: datetime
    state: Literal["due", "waiting"]


class GetLineTrigger(_StrictOperationalModel):
    """One live Trigger aimed at a Line: its name, accepted version and when it fires."""

    trigger: str
    version: int = Field(ge=1)
    fires: str


class GetLineCard(_StrictOperationalModel):
    line: str
    identity_digest: str
    lifecycle: str
    procedure: str
    authority: Literal["observe", "propose", "settle"]
    #: The kinds of schedule that set this Line off (``manual`` with none).
    trigger: str
    #: The first live Triggers aimed at this Line, in identity order;
    #: ``get Trigger:<name>`` reads one, ``query Trigger --where target=...`` lists all.
    triggers: tuple[GetLineTrigger, ...] = ()
    triggers_total: int = Field(default=0, ge=0)
    #: Set while Triggers aim at this live Line but it is not enabled.
    triggers_inactive: LineTriggersInactive | None = Field(default=None, exclude_if=_omit_none)
    occurrence_epoch: int = Field(ge=1)
    enablements: tuple[GetLineEnablement, ...] = ()
    enablements_total: int = Field(default=0, ge=0)
    due: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)
    occurrences: tuple[GetLineOccurrence, ...] = ()
    recent_runs: tuple[RunRow, ...] = ()
    runs_total: int = Field(default=0, ge=0)
    next: tuple[str, ...] = ()


class GetCaptureCard(_StrictOperationalModel):
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


class GetPredictionWindow(_StrictOperationalModel):
    """One bound window of a ResolutionContract, as the settlement worker holds it."""

    window: str
    starts_at: datetime
    ends_at: datetime
    status: PredictionWindowStatus


class GetResolutionContractCard(_StrictOperationalModel):
    contract: str
    lifecycle: str
    hypothesis: str
    hypothesis_value: Any = None
    window: str
    rule: str
    state: Literal["open", "settleable", "resolved", "unbound", "not_observed"]
    windows: tuple[GetPredictionWindow, ...] = ()
    windows_total: int = Field(default=0, ge=0)
    next: tuple[str, ...] = ()


class GetMandateCard(_StrictOperationalModel):
    mandate: str
    procedure: str
    grants: Literal["propose", "settle"]
    lifecycle: str
    state: MandateState
    valid_from: datetime
    expires_at: datetime
    namespace: tuple[str, ...] = ()
    next: tuple[str, ...] = ()


class GetRunNode(_StrictOperationalModel):
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


class GetRunCurrentNode(_StrictOperationalModel):
    node: str
    kind: str
    # The journal time of the event before it: the run's evaluation instant.
    started_at: datetime


class GetRunTrigger(_StrictOperationalModel):
    """What admitted a Line run: the Line, its occurrence, and the enablement that dispatched it."""

    line: str
    occurrence: str | None = Field(default=None, exclude_if=_omit_none)
    enablement: str | None = Field(default=None, exclude_if=_omit_none)
    principal_kind: EnablementPrincipalKind | None = Field(default=None, exclude_if=_omit_none)
    # Withheld, as on a Line card, unless the reader is the enabling credential or an admin.
    enabled_by: str | None = Field(default=None, exclude_if=_omit_none)
    enabled_by_withheld: bool = Field(default=False, exclude_if=lambda value: not value)


class GetPendingInput(_StrictOperationalModel):
    name: str
    waiting_since: datetime


class GetRunOutcome(_StrictOperationalModel):
    """One durable outcome a finished run recorded."""

    sequence: int = Field(ge=1)
    event_kind: str
    node_id: str | None = Field(default=None, exclude_if=_omit_none)
    payload_digest: str
    capture_event: TriggerEventReference | None = Field(default=None, exclude_if=_omit_none)


class GetProcedureRunCard(_StrictOperationalModel):
    run: str
    procedure: str
    status: RunStatus
    nodes_done: int = Field(ge=0)
    nodes_total: int = Field(ge=0)
    current_node: GetRunCurrentNode | None = Field(default=None, exclude_if=_omit_none)
    # The run's admission: its evaluation instant.
    started_at: datetime
    elapsed_us: int | None = Field(default=None, ge=0, exclude_if=_omit_none)
    elapsed_basis: Literal["read_time", "measured_wall_clock"] | None = Field(
        default=None, exclude_if=_omit_none
    )
    nodes: tuple[GetRunNode, ...] = ()
    pending_inputs: tuple[GetPendingInput, ...] = ()
    triggered_by: GetRunTrigger | None = Field(default=None, exclude_if=_omit_none)
    actor: str | None = Field(default=None, exclude_if=_omit_none)
    receipt_digest: str | None = Field(default=None, exclude_if=_omit_none)
    #: A finished run's typed result value.
    result: Any = Field(default=None, exclude_if=_omit_none)
    outcomes: tuple[GetRunOutcome, ...] = ()
    #: Why a run stopped short of a result: code, message, details and repair.
    terminal: ProcedureTerminal | None = Field(default=None, exclude_if=_omit_none)
    #: What each terminal node did (the proposal it opened, the Capture it emitted).
    terminal_egress: tuple[ProcedureTerminalEgress, ...] = ()
    next: tuple[str, ...] = ()


# -- orient section rows -----------------------------------------------------------


class OrientLine(_StrictOperationalModel):
    line: str
    lifecycle: Literal["live", "retired"]
    procedure: str
    authority: Literal["observe", "propose", "settle"]
    trigger: str
    enablement: LineEnablementState | None = Field(default=None, exclude_if=_omit_none)
    triggers_inactive: LineTriggersInactive | None = Field(default=None, exclude_if=_omit_none)
    due: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)


class OrientCapture(_StrictOperationalModel):
    capture: str
    contract: str
    observed_at: datetime
    citing: int = Field(ge=0)


class OrientCaptureContract(_StrictOperationalModel):
    contract: str
    version: int = Field(ge=1)
    lifecycle: str
    grade: str
    admitted_by: int = Field(ge=0)


class OrientPrediction(_StrictOperationalModel):
    contract: str
    hypothesis: str
    window: str
    open: int = Field(default=0, ge=0)
    settleable: int = Field(default=0, ge=0)
    resolved: int = Field(default=0, ge=0)
    next_close: datetime | None = Field(default=None, exclude_if=_omit_none)


class OrientMandate(_StrictOperationalModel):
    mandate: str
    procedure: str
    grants: Literal["propose", "settle"]
    state: MandateState
    expires_at: datetime


__all__ = [
    "CAPTURE_HANDLE_HEX",
    "LINE_CARD_ARMS",
    "LINE_CARD_RUNS",
    "OPERATIONAL_CARD_LIST_LIMIT",
    "EnablementPrincipalKind",
    "GetCaptureCard",
    "GetLineEnablement",
    "GetLineCard",
    "GetLineOccurrence",
    "GetLineTrigger",
    "GetMandateCard",
    "GetPendingInput",
    "GetPredictionWindow",
    "GetProcedureRunCard",
    "GetResolutionContractCard",
    "GetRunCurrentNode",
    "GetRunNode",
    "GetRunTrigger",
    "LineEnablementState",
    "LineTriggersInactive",
    "LiveHead",
    "LiveView",
    "MandateState",
    "OrientCaptureContract",
    "OrientCapture",
    "OrientLine",
    "OrientMandate",
    "OrientPrediction",
    "PredictionWindowStatus",
    "RunRow",
    "RunStatus",
    "capture_handle",
]
