"""Runbook artifact: a flat, ordered list of Trigger targets (D-R36).

A Runbook names, in order, the same targets a Trigger can aim at: a Line
(``LineTarget``) or a registered internal action (``ActionTarget``). It has no
branching, no parameters of its own and no action kinds of its own: each Line
step runs as that Line's ordinary manual occurrence, on the Line's own
parameters, budgets, authority and mandate, and each action step fires the
registered action exactly as ``action run`` does.

A run goes step by step and stops at the first step that fails or whose
writes go to review (a proposal): a later step would otherwise read state the
review has not decided. The run's record lists every step's outcome and why
it stopped.

A Runbook's preview is static: it is composed from accepted definitions only
(the Lines, their Procedures, Mandates, Queries and Triggers), never executes a
step and never predicts an outcome. It says what each step CAN read, write,
call, settle or propose, which Triggers its writes can set off, and where the
order reads what only a later step, or nothing, writes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, ClassVar, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.mandate_scope import MandateBasis, MandateChangeKind, SettleScopeGap
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_client.contracts.triggers import (
    INTERNAL_ACTIONS,
    ActionTarget,
    InternalActionSpec,
    LineTarget,
)

RUNBOOK_ARTIFACT_FORMAT: Final = "cruxible-runbook-v1"
#: Steps one Runbook may list: a bound on its run and on its preview.
RUNBOOK_MAX_STEPS: Final = 64
_RUNBOOK_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
#: The one path a Runbook lives at; any other path under ``runbooks/`` is no Runbook.
RUNBOOK_PATH_RE = re.compile(r"^runbooks/[a-z][a-z0-9_.-]{0,255}\.json$")


class RunbookFormatError(FormatError):
    """A Runbook artifact or its successor transition is invalid."""


class _StrictRunbookModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


#: One step: exactly a Trigger's Line or internal-action target.
RunbookStep: TypeAlias = Annotated[LineTarget | ActionTarget, Field(discriminator="kind")]


class Runbook(_StrictRunbookModel):
    """An ordered list of Lines and internal actions, run in order, stopping on review."""

    artifact_format: Literal["cruxible-runbook-v1"] = RUNBOOK_ARTIFACT_FORMAT
    identity: ArtifactIdentity
    description: str | None = Field(
        default=None, max_length=1024, exclude_if=lambda value: value is None
    )
    steps: tuple[RunbookStep, ...] = Field(min_length=1, max_length=RUNBOOK_MAX_STEPS)
    #: A Runbook names its Lines by identity, so a Line successor never strands it.
    pins: ClassVar[tuple[ArtifactPin, ...]] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @model_validator(mode="after")
    def _shape(self) -> Runbook:
        if self.identity.kind != "Runbook" or not _RUNBOOK_NAME_RE.fullmatch(self.identity.name):
            raise ValueError("Runbook identity must be path-addressable and kind Runbook")
        return self

    @property
    def lines(self) -> tuple[ArtifactIdentity, ...]:
        """Every Line the steps name, in first-step order, once each."""

        return tuple(
            dict.fromkeys(step.line.target for step in self.steps if isinstance(step, LineTarget))
        )


def runbook_path(name: str) -> str:
    if not _RUNBOOK_NAME_RE.fullmatch(name):
        raise RunbookFormatError("Runbook identity is not path-addressable")
    return f"runbooks/{name}.json"


def render_runbook(runbook: Runbook) -> bytes:
    return pretty_canonical_bytes(runbook.model_dump(mode="json"))


def runbook_digest(runbook: Runbook) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest, "playbill-runbook-artifact-v1", runbook.model_dump(mode="json")
    )


def parse_runbook(content: bytes, *, path: str) -> Runbook:
    try:
        runbook = Runbook.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RunbookFormatError("Runbook failed strict validation") from exc
    if runbook_path(runbook.identity.name) != path:
        raise RunbookFormatError("Runbook identity/path disagreement")
    if render_runbook(runbook) != content:
        raise RunbookFormatError("Runbook is not in canonical wire form")
    return runbook


class AcceptedRunbook(_StrictRunbookModel):
    path: str
    runbook: Runbook
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> AcceptedRunbook:
        if self.path != runbook_path(self.runbook.identity.name):
            raise ValueError("accepted Runbook path does not reproduce")
        if self.artifact_digest != runbook_digest(self.runbook).tagged:
            raise ValueError("accepted Runbook digest does not reproduce")
        return self


@dataclass(frozen=True)
class RunbookLineState:
    """What the Runbook law reads of one named Line in the final candidate."""

    live: bool
    #: The Line binds its triggering Capture to a Source input: a manual
    #: occurrence of it needs an event, which a Runbook step never supplies.
    takes_event: bool = False


class RunbookLawResult(_StrictRunbookModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> RunbookLawResult:
    return RunbookLawResult(
        verdict="refused",
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity="error",
                message=message,
                subject=SemanticAddress.whole_artifact(path),
            ),
        ),
    )


def evaluate_runbook_law(
    runbook: Runbook,
    *,
    path: str,
    predecessor: AcceptedRunbook | None,
    lines: Mapping[str, RunbookLineState],
    actions: Mapping[str, InternalActionSpec] = INTERNAL_ACTIONS,
) -> RunbookLawResult:
    """Judge one Runbook against its predecessor and the targets it names.

    ``lines`` holds, by qualified identity, the state of each Line the steps
    name in the final candidate; a Line it lacks is absent. A live Runbook's
    every Line must be live and run without an event, and every action must
    be registered. A retiring Runbook is judged only for its succession.
    O(steps): one lookup per step.
    """

    if path != runbook_path(runbook.identity.name):
        return _refusal(
            "cruxible.runbook.path_mismatch", "Runbook identity/path disagreement.", path=path
        )
    if predecessor is None:
        if runbook.lifecycle.predecessor_digest is not None:
            return _refusal(
                "cruxible.runbook.invalid_genesis",
                "A new Runbook cannot name a predecessor.",
                path=path,
            )
    else:
        if runbook.identity != predecessor.runbook.identity:
            return _refusal(
                "cruxible.runbook.stable_identity_changed",
                "A Runbook successor must retain its stable identity.",
                path=path,
            )
        if runbook.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "cruxible.runbook.predecessor_mismatch",
                "A Runbook successor does not pin its exact predecessor.",
                path=path,
            )
        if runbook.lifecycle.state == "live" and predecessor.runbook.lifecycle.state == "retired":
            return _refusal(
                "cruxible.runbook.revival_refused",
                "A retired Runbook cannot be revived; propose a new Runbook identity instead.",
                path=path,
            )
    if runbook.lifecycle.state == "live":
        for index, step in enumerate(runbook.steps, start=1):
            if isinstance(step, ActionTarget):
                if step.action not in actions:
                    return _refusal(
                        "cruxible.runbook.action_unknown",
                        f"Step {index}: internal action {step.action!r} is not registered; a "
                        "Runbook step may fire one of: " + ", ".join(sorted(actions)) + ".",
                        path=path,
                    )
                continue
            qualified = step.line.target.qualified
            state = lines.get(qualified)
            if state is None or not state.live:
                return _refusal(
                    "cruxible.runbook.line_unavailable",
                    f"Step {index}: {qualified!r} is not a live accepted Line in this "
                    "candidate; accept the Line first.",
                    path=path,
                )
            if state.takes_event:
                return _refusal(
                    "cruxible.runbook.line_takes_event",
                    f"Step {index}: {qualified!r} binds its triggering Capture to a Source "
                    "input, and a Runbook step runs without an event; run it from its own "
                    "capture_landing Trigger instead.",
                    path=path,
                )
    return RunbookLawResult(
        verdict="accepted",
        artifact_digest=runbook_digest(runbook).tagged,
        required_tier="governed_write",
    )


# --- Running ---------------------------------------------------------------

RunbookStepStatus: TypeAlias = Literal[
    # The Line run succeeded and nothing it wrote waits for review.
    "succeeded",
    # The Line run succeeded and sent writes to review: the run stops here.
    "proposed",
    # The Line run refused or failed: the run stops here.
    "failed",
    # A Trigger-fired run: the Line is not enabled, so nothing may run it.
    "not_enabled",
    # The internal action was fired into the trigger journal.
    "fired",
    # An earlier step stopped the run.
    "not_run",
]
RunbookStopReason: TypeAlias = Literal["step_failed", "step_proposed", "line_not_enabled"]


class RunbookStepOutcome(_StrictRunbookModel):
    """What one step did: its Line run (or action fire) and how it stood."""

    tag: Literal["cruxible-runbook-step-outcome-v1"] = "cruxible-runbook-step-outcome-v1"
    step: int = Field(ge=1)
    target: RunbookStep
    status: RunbookStepStatus
    #: The Line run this step admitted, readable with ``get`` like any run.
    run_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    run_status: str | None = Field(default=None, exclude_if=lambda value: value is None)
    #: The proposals the step's writes went to, or the open one it kept.
    proposal_ids: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    #: The trigger journal event an action step fired.
    event_id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    #: The refusal or failure code a failed step stopped on.
    code: str | None = Field(default=None, exclude_if=lambda value: value is None)
    message: str | None = Field(default=None, exclude_if=lambda value: value is None)


class RunbookRunResult(_StrictRunbookModel):
    """One run of a Runbook: every step's outcome, in order, and why it stopped."""

    tag: Literal["cruxible-runbook-run-v1"] = "cruxible-runbook-run-v1"
    run_id: str
    runbook: str
    runbook_artifact_digest: str
    mode: Literal["manual", "trigger"]
    trigger: str | None = Field(default=None, exclude_if=lambda value: value is None)
    started_at: datetime
    finished_at: datetime
    status: Literal["completed", "stopped"]
    stop_reason: RunbookStopReason | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    stopped_at_step: int | None = Field(default=None, exclude_if=lambda value: value is None)
    steps: tuple[RunbookStepOutcome, ...]

    _times = field_validator("started_at", "finished_at")(ensure_utc)

    @model_validator(mode="after")
    def _stop(self) -> RunbookRunResult:
        if (self.status == "stopped") != (self.stop_reason is not None):
            raise ValueError("a stopped run names its stop reason, and only a stopped run")
        if (self.stop_reason is None) != (self.stopped_at_step is None):
            raise ValueError("a stop reason names the step it stopped at")
        return self


class RunbookRunRequest(_StrictRunbookModel):
    """Run one accepted Runbook now, step by step, under the caller's authority."""

    tag: Literal["cruxible-runbook-run-request-v1"] = "cruxible-runbook-run-request-v1"
    runbook: str

    @field_validator("runbook")
    @classmethod
    def _name(cls, value: str) -> str:
        runbook_path(value.removeprefix("Runbook:"))
        return value.removeprefix("Runbook:")


# --- Preview ---------------------------------------------------------------


class RunbookPreviewRead(_StrictRunbookModel):
    """One read a step's Procedure declares, and the vocabulary behind it."""

    node_id: str
    query: str | None = Field(default=None, exclude_if=lambda value: value is None)
    #: The ClaimTypes the read reads: the Query's predicates, or the read's own.
    claim_types: tuple[str, ...]
    #: The Subject kinds those ClaimTypes are about, and each kind's key ClaimTypes.
    subject_kinds: tuple[str, ...] = ()
    key_claim_types: tuple[str, ...] = ()
    #: A read fed by a call or derive output: it runs mid-run (D-R38).
    late: bool = False


class RunbookPreviewWrite(_StrictRunbookModel):
    """One claims node a step's Procedure declares: the ClaimTypes it can write."""

    node_id: str
    subject_kind: str
    #: The node may create Subjects of ``subject_kind`` (``create: if_absent``).
    creates_subjects: bool = False
    claim_types: tuple[str, ...]
    #: Subject kinds its values may create.
    creates_object_kinds: tuple[str, ...] = ()
    #: The node judges absence (``expect``): it can retire held Claims.
    judges_absence: bool = False


class RunbookPreviewCall(_StrictRunbookModel):
    """One provider call: its slot's signature and the binding the Line pins."""

    node_id: str
    slot: str
    signature_in: str | None = None
    signature_out: str | None = None
    effect: str | None = None
    build: str | None = None
    operation: str | None = None
    #: The CaptureContracts the call lands Captures under.
    captures: tuple[str, ...] = ()


class RunbookSettleChange(_StrictRunbookModel):
    """One change kind a settle grant covers, and the evidence bases it settles on."""

    change: MandateChangeKind
    basis: tuple[MandateBasis, ...]


class RunbookSettleScope(_StrictRunbookModel):
    """One ClaimType a settle grant covers, with every restriction the grant states."""

    claim_type: str
    changes: tuple[RunbookSettleChange, ...]
    binding_subject_role: Literal["subject", "object"] = "subject"
    #: Enum/bool value restrictions: the held values a change may replace and write.
    value_from: tuple[str | bool, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    value_to: tuple[str | bool, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RunbookPreviewAuthority(_StrictRunbookModel):
    """What the step's Line may do with its writes, from its Mandate scope.

    Static: a write is shown as settling only when every restriction of the
    grant can be proven from definitions (ClaimType, change kind, the value's
    evidence basis, no value or Subject restriction). Anything that cannot be
    proven is listed in ``review_gaps`` and may go to review.
    """

    max_authority: Literal["observe", "propose", "settle"]
    mandates: tuple[str, ...] = ()
    grants: Literal["propose", "settle"] | None = None
    #: The settle grant's scope, restrictions included.
    settles: tuple[RunbookSettleScope, ...] = ()
    #: Subject kinds whose creation settles with the change.
    settles_creation: tuple[str, ...] = ()
    #: The settle grant's Subject fence: an explicit Subject scope (artifact
    #: paths), and/or ``expected`` (targets fenced by the claims ``within``).
    subject_scope: tuple[str, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    subjects: Literal["expected"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    #: The settle grant checks a condition Query (``condition_query``) and, when
    #: it does not hold, falls back as ``condition_fallback`` says.
    conditional: bool = False
    condition_query: str | None = Field(default=None, exclude_if=lambda value: value is None)
    condition_fallback: Literal["refuse", "propose"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    #: Why a write cannot be proven to settle: each (ClaimType, change, basis)
    #: and the scope term that leaves it out.
    review_gaps: tuple[SettleScopeGap, ...] = ()
    #: ClaimTypes the step can write (or retire) that may go to a proposal,
    #: which would stop the run.
    proposes: tuple[str, ...] = ()
    #: Subject kinds the step can create outside the grant's creation scope.
    proposes_creation: tuple[str, ...] = ()
    #: Whether any write can go to review (and so stop the run).
    may_stop_run: bool


class RunbookPreviewCascade(_StrictRunbookModel):
    """A Trigger the step's writes can set off, during or after the run."""

    trigger: str
    schedule: Literal["capture_landing", "generation_accepted"]
    target_kind: Literal["line", "action", "runbook"]
    target: str
    #: The CaptureContract whose landing fires it (capture_landing only).
    capture_contract: str | None = Field(default=None, exclude_if=lambda value: value is None)
    #: The target is a Line this Runbook also runs.
    in_runbook: bool = False


class RunbookPreviewStep(_StrictRunbookModel):
    step: int = Field(ge=1)
    target: RunbookStep
    #: The step's Line is not live: the run would stop here.
    unavailable: bool = Field(default=False, exclude_if=lambda value: not value)
    procedure: str | None = Field(default=None, exclude_if=lambda value: value is None)
    reads: tuple[RunbookPreviewRead, ...] = ()
    writes: tuple[RunbookPreviewWrite, ...] = ()
    calls: tuple[RunbookPreviewCall, ...] = ()
    authority: RunbookPreviewAuthority | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    #: The action's declared effect, for an action step.
    action_effect: str | None = Field(default=None, exclude_if=lambda value: value is None)
    cascades: tuple[RunbookPreviewCascade, ...] = ()


class RunbookOrderProblem(_StrictRunbookModel):
    """A read the order cannot serve: reported, never fixed."""

    kind: Literal["reads_later_write", "reads_unwritten"]
    step: int = Field(ge=1)
    claim_type: str
    #: The later steps that write it (``reads_later_write``).
    written_by_steps: tuple[int, ...] = ()
    #: Live Lines outside the Runbook that write it.
    outside_writers: tuple[str, ...] = ()


class RunbookPreview(_StrictRunbookModel):
    """A static composed graph of one Runbook, from accepted definitions only.

    It never executes a step, reads no Claims, Captures or journals, and
    predicts no outcome: no record counts, no value changes, no whether a
    proposal happens. It says what each step can do.
    """

    tag: Literal["cruxible-runbook-preview-v1"] = "cruxible-runbook-preview-v1"
    runbook: str
    runbook_artifact_digest: str
    description: str | None = Field(default=None, exclude_if=lambda value: value is None)
    generation_root: str
    steps: tuple[RunbookPreviewStep, ...]
    order_problems: tuple[RunbookOrderProblem, ...] = ()


__all__ = [
    "RUNBOOK_ARTIFACT_FORMAT",
    "RUNBOOK_MAX_STEPS",
    "RUNBOOK_PATH_RE",
    "AcceptedRunbook",
    "Runbook",
    "RunbookFormatError",
    "RunbookLawResult",
    "RunbookLineState",
    "RunbookOrderProblem",
    "RunbookPreview",
    "RunbookPreviewAuthority",
    "RunbookPreviewCall",
    "RunbookPreviewCascade",
    "RunbookPreviewRead",
    "RunbookPreviewStep",
    "RunbookPreviewWrite",
    "RunbookRunRequest",
    "RunbookRunResult",
    "RunbookSettleChange",
    "RunbookSettleScope",
    "RunbookStep",
    "RunbookStepOutcome",
    "RunbookStepStatus",
    "RunbookStopReason",
    "evaluate_runbook_law",
    "parse_runbook",
    "render_runbook",
    "runbook_digest",
    "runbook_path",
]
