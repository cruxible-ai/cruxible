"""Pure occurrence-backlog accounting and the fenced lapse receipt.

Backlog classification is a pure function of the pinned acquisition policy, the
occurrence windows derived from pinned trigger policy and journal facts, and one
explicit evaluation time.  It performs no source I/O, consults no wall clock,
and appends nothing.  Two backends holding the same verified head vector must
therefore render byte-identical backlog reports at the same evaluation time.

Two laws shape what the scan is allowed to know:

* ``on_unavailable`` is never applied here.  Whether a source is reachable right
  now is only learnable from a real typed acquisition attempt result, so the
  scan consumes only the pinned *replayability class* and *age* law.  A rule
  whose ``permitted_replayability`` excludes ``exact`` can never truthfully
  re-read a past window; a rule with ``max_age`` stops being truthful once that
  age elapses relative to the window it would fulfil.
* Only ``required`` inputs bound truthful fulfilment.  ``optional`` and
  ``conservative_default`` rules have already declared a truthful completion
  mode that needs no fresh exact read, so they cannot make an original window
  unfulfillable.  ``requirement`` is a structural declaration, not a live
  availability behaviour, so reading it does not smuggle ``on_unavailable`` in.

Lapse legality is receipt-independent: an occurrence is lapsed the instant its
deterministic ``lapse_effective_at`` passes, whether or not any writer was ever
online to memorialize it.  The receipt only records that a fenced writer noticed
an already-effective gap, which is why ``lapse_effective_at`` is derived from
the window and the policy while ``recorded_at`` and runner identity stay in the
operational audit envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_core.playbill.acquisition_policies import (
    SourceAcquisitionPolicyV1,
    acquisition_policy_digest,
)
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.capture_journal import (
    CaptureCursorV1,
    CaptureJournalError,
    CaptureLandingEventV1,
)
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.errors import PlaybillCasError, PlaybillJournalError
from cruxible_core.playbill.exhaust import (
    JournalHeadVectorV1,
    JournalPartitionHeadV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
)
from cruxible_core.playbill.line_scheduler import (
    LineAttemptStatusV1,
    LineOccurrenceIndex,
    ResolvedCadenceTriggerV1,
    ResolvedCaptureLandingTriggerV1,
    ResolvedWindowCloseTriggerV1,
    cadence_occurrence_time,
    derive_cadence_occurrences,
    derive_landing_occurrences,
    derive_window_close_occurrences,
)
from cruxible_core.playbill.lines import (
    LineDeploymentV1,
    LineLeaseV1,
    LineRuntimeRefusal,
    line_deployment_digest,
    line_partition_pairs,
    verify_line_lease,
)
from cruxible_core.playbill.occurrences import (
    LineOccurrenceV1,
    ManualOccurrenceV1,
    line_occurrence_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.temporal import ensure_utc, format_datetime

OccurrenceBacklogStateV1 = Literal["late_eligible", "lapsed"]
OperationalCoverageV1 = Literal["available", "unavailable"]

LAPSE_UNREPLAYABLE = "playbill.line.lapse.unreplayable_input"
LAPSE_OUT_OF_AGE = "playbill.line.lapse.out_of_age"

COVERAGE_MISSING = "playbill.line.coverage.journal_missing"
COVERAGE_TRUNCATED = "playbill.line.coverage.journal_truncated"
COVERAGE_FORKED = "playbill.line.coverage.journal_forked"
COVERAGE_UNVERIFIED = "playbill.line.coverage.journal_unverified"
COVERAGE_UNAUTHORIZED = "playbill.line.coverage.journal_unauthorized"
COVERAGE_HEAD_INCOMPLETE = "playbill.line.coverage.head_vector_incomplete"

_TERMINAL_DISPOSITIONS: frozenset[str] = frozenset({"succeeded", "refused"})


class _StrictBacklogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _tagged_digest(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


def _sorted_reason_codes(value: tuple[str, ...]) -> tuple[str, ...]:
    if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
        raise ValueError("reason codes must be sorted and unique")
    return value


# ---------------------------------------------------------------------------
# The pinned policy's lapse law
# ---------------------------------------------------------------------------


class AcquisitionLapseHorizonV1(_StrictBacklogModel):
    """The pinned policy's replayability/age law, reduced to one lapse horizon."""

    tag: Literal["playbill-acquisition-lapse-horizon-v1"] = "playbill-acquisition-lapse-horizon-v1"
    acquisition_policy_digest: str
    unreplayable_inputs: tuple[str, ...] = ()
    age_bound_microseconds: int | None = Field(default=None, ge=1)
    age_bound_inputs: tuple[str, ...] = ()

    _policy = field_validator("acquisition_policy_digest")(_tagged_digest)

    @field_validator("unreplayable_inputs", "age_bound_inputs")
    @classmethod
    def _inputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("lapse horizon input names must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _age_shape(self) -> "AcquisitionLapseHorizonV1":
        if (self.age_bound_microseconds is None) != (not self.age_bound_inputs):
            raise ValueError("an age bound and the inputs that impose it travel together")
        return self


def acquisition_lapse_horizon(policy: SourceAcquisitionPolicyV1) -> AcquisitionLapseHorizonV1:
    """Reduce a pinned policy to the replayability/age law a late read must satisfy."""

    unreplayable: list[str] = []
    age_bounded: list[str] = []
    bound: int | None = None
    for rule in policy.inputs:
        if rule.requirement != "required":
            # Optional and conservative-default inputs declare a truthful
            # completion mode that needs no fresh exact read of the window.
            continue
        if "exact" not in rule.permitted_replayability:
            unreplayable.append(rule.input_name)
        if rule.max_age is not None:
            age_bounded.append(rule.input_name)
            bound = (
                rule.max_age.microseconds
                if bound is None
                else min(bound, rule.max_age.microseconds)
            )
    return AcquisitionLapseHorizonV1(
        acquisition_policy_digest=acquisition_policy_digest(policy).tagged,
        unreplayable_inputs=tuple(sorted(unreplayable, key=lambda item: item.encode("utf-8"))),
        age_bound_microseconds=bound,
        age_bound_inputs=tuple(sorted(age_bounded, key=lambda item: item.encode("utf-8"))),
    )


def occurrence_lapse_effective_at(
    window_end: datetime,
    horizon: AcquisitionLapseHorizonV1,
) -> datetime | None:
    """Return the exact instant this window stops being truthfully fulfillable.

    The result depends only on the window and the pinned policy, so every runner
    and every backend derives the same instant, and ``None`` means the policy
    permits a truthful late read indefinitely.
    """

    end = ensure_utc(window_end)
    candidates: list[datetime] = []
    if horizon.unreplayable_inputs:
        # No exact replay is permitted, so the window was never fulfillable
        # after it closed.
        candidates.append(end)
    if horizon.age_bound_microseconds is not None:
        candidates.append(end + timedelta(microseconds=horizon.age_bound_microseconds))
    return min(candidates) if candidates else None


def occurrence_lapse_reason_codes(
    window_end: datetime,
    horizon: AcquisitionLapseHorizonV1,
    *,
    evaluation_time: datetime,
) -> tuple[str, ...]:
    """Name every pinned reason this window is lapsed at the evaluation time."""

    end = ensure_utc(window_end)
    evaluation = ensure_utc(evaluation_time)
    reasons: list[str] = []
    if horizon.unreplayable_inputs and evaluation >= end:
        reasons.append(LAPSE_UNREPLAYABLE)
    if horizon.age_bound_microseconds is not None and evaluation >= end + timedelta(
        microseconds=horizon.age_bound_microseconds
    ):
        reasons.append(LAPSE_OUT_OF_AGE)
    return tuple(sorted(set(reasons), key=lambda item: item.encode("utf-8")))


# ---------------------------------------------------------------------------
# Pure occurrence facts
# ---------------------------------------------------------------------------


def cadence_window_end(trigger: ResolvedCadenceTriggerV1, tick_index: int) -> datetime:
    """Close a cadence tick's window at the next tick; purely schedule-derived."""

    return cadence_occurrence_time(trigger, tick_index + 1)


class BacklogOccurrenceV1(_StrictBacklogModel):
    """One occurrence's window and attempt outcome, free of runner identity."""

    tag: Literal["playbill-backlog-occurrence-v1"] = "playbill-backlog-occurrence-v1"
    occurrence: LineOccurrenceV1
    occurrence_digest: str
    window_end: datetime
    attempt_count: int = Field(default=0, ge=0)
    latest_status: LineAttemptStatusV1 | None = None

    _digest = field_validator("occurrence_digest")(_tagged_digest)

    @field_validator("window_end")
    @classmethod
    def _window_end(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("window_end", when_used="json")
    def _serialize_window_end(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _shape(self) -> "BacklogOccurrenceV1":
        if line_occurrence_digest(self.occurrence) != self.occurrence_digest:
            raise ValueError("backlog occurrence digest does not reproduce")
        if isinstance(self.occurrence, ManualOccurrenceV1):
            raise ValueError("a manual occurrence has no window and cannot enter a backlog")
        if self.attempt_count == 0 and self.latest_status is not None:
            raise ValueError("an occurrence with no attempt carries no attempt status")
        return self

    @property
    def disposed(self) -> bool:
        """Report whether an attempt already reached a terminal disposition."""

        return self.latest_status in _TERMINAL_DISPOSITIONS


def build_backlog_occurrences(
    candidates: tuple[tuple[LineOccurrenceV1, datetime], ...],
    *,
    attempts: Mapping[str, tuple[int, LineAttemptStatusV1 | None]] | None = None,
) -> tuple[BacklogOccurrenceV1, ...]:
    """Join derived occurrence windows to journal-derived attempt state."""

    recorded: Mapping[str, tuple[int, LineAttemptStatusV1 | None]] = attempts or {}
    built: list[BacklogOccurrenceV1] = []
    seen: set[str] = set()
    for occurrence, window_end in candidates:
        if isinstance(occurrence, ManualOccurrenceV1):
            raise LineRuntimeRefusal(
                "playbill.line.backlog_manual_has_no_window",
                "A manual occurrence carries a request handle, not a window that can pass.",
            )
        digest = line_occurrence_digest(occurrence)
        if digest in seen:
            continue
        seen.add(digest)
        count, status = recorded.get(digest, (0, None))
        built.append(
            BacklogOccurrenceV1(
                occurrence=occurrence,
                occurrence_digest=digest,
                window_end=window_end,
                attempt_count=count,
                latest_status=status,
            )
        )
    return tuple(sorted(built, key=lambda item: item.occurrence_digest.encode("ascii")))


# ---------------------------------------------------------------------------
# Pure classification
# ---------------------------------------------------------------------------


class OccurrenceBacklogEntryV1(_StrictBacklogModel):
    """One passed, unfulfilled occurrence in exactly one disjoint backlog state."""

    tag: Literal["playbill-occurrence-backlog-entry-v1"] = "playbill-occurrence-backlog-entry-v1"
    occurrence_digest: str
    trigger_kind: Literal["cadence", "capture_landing", "window_close"]
    state: OccurrenceBacklogStateV1
    window_end: datetime
    lapse_effective_at: datetime | None = None
    reason_codes: tuple[str, ...] = ()

    _digest = field_validator("occurrence_digest")(_tagged_digest)
    _reasons = field_validator("reason_codes")(_sorted_reason_codes)

    @field_validator("window_end", "lapse_effective_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_serializer("window_end", "lapse_effective_at", when_used="json")
    def _serialize_times(self, value: datetime | None) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _states_are_disjoint(self) -> "OccurrenceBacklogEntryV1":
        if self.state == "lapsed":
            if self.lapse_effective_at is None or not self.reason_codes:
                raise ValueError("a lapsed occurrence carries an effective time and a reason")
        elif self.reason_codes:
            raise ValueError("a late-eligible occurrence carries no lapse reason")
        return self


class LineOccurrenceBacklogV1(_StrictBacklogModel):
    """Structured per-Line backlog facts; unavailable coverage never reads zero."""

    tag: Literal["playbill-line-occurrence-backlog-v1"] = "playbill-line-occurrence-backlog-v1"
    line_id: str
    occurrence_epoch: int = Field(ge=0)
    line_spec_digest: str
    acquisition_policy_digest: str
    head_vector_digest: str
    evaluation_time: datetime
    coverage: OperationalCoverageV1
    coverage_reason_codes: tuple[str, ...] = ()
    late_eligible: tuple[OccurrenceBacklogEntryV1, ...] | None = None
    lapsed: tuple[OccurrenceBacklogEntryV1, ...] | None = None

    _digests = field_validator(
        "line_spec_digest",
        "acquisition_policy_digest",
        "head_vector_digest",
    )(_tagged_digest)
    _reasons = field_validator("coverage_reason_codes")(_sorted_reason_codes)

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_evaluation_time(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _coverage_shape(self) -> "LineOccurrenceBacklogV1":
        populated = self.late_eligible is not None and self.lapsed is not None
        if self.coverage == "available":
            if not populated or self.coverage_reason_codes:
                raise ValueError("available coverage reports both backlog lists and no reason")
        else:
            if populated or self.late_eligible is not None or self.lapsed is not None:
                raise ValueError("unavailable coverage reports no counts, not zero counts")
            if not self.coverage_reason_codes:
                raise ValueError("unavailable coverage must name why it is unavailable")
        return self

    @property
    def late_eligible_count(self) -> int | None:
        return None if self.late_eligible is None else len(self.late_eligible)

    @property
    def lapsed_count(self) -> int | None:
        return None if self.lapsed is None else len(self.lapsed)


def classify_occurrence_backlog(
    *,
    line_id: str,
    occurrence_epoch: int,
    line_spec_digest: str,
    policy: SourceAcquisitionPolicyV1,
    occurrences: tuple[BacklogOccurrenceV1, ...],
    head_vector: JournalHeadVectorV1,
    evaluation_time: datetime,
) -> LineOccurrenceBacklogV1:
    """Classify every passed, unfulfilled occurrence without touching a source.

    The result is a pure function of the pinned policy, the supplied occurrence
    windows, the verified head vector, and the explicit evaluation time.
    """

    evaluation = ensure_utc(evaluation_time)
    horizon = acquisition_lapse_horizon(policy)
    late: list[OccurrenceBacklogEntryV1] = []
    lapsed: list[OccurrenceBacklogEntryV1] = []
    for item in occurrences:
        if item.occurrence.line_id != line_id or (
            item.occurrence.occurrence_epoch != occurrence_epoch
        ):
            raise LineRuntimeRefusal(
                "playbill.line.backlog_occurrence_foreign",
                "Backlog occurrence names another Line identity or occurrence epoch.",
            )
        if item.disposed or evaluation < item.window_end:
            continue
        if isinstance(
            item.occurrence, ManualOccurrenceV1
        ):  # pragma: no cover - refused by the model validator
            raise LineRuntimeRefusal(
                "playbill.line.backlog_manual_has_no_window",
                "A manual occurrence carries a request handle, not a window that can pass.",
            )
        trigger_kind = item.occurrence.trigger_kind
        effective = occurrence_lapse_effective_at(item.window_end, horizon)
        if effective is not None and evaluation >= effective:
            lapsed.append(
                OccurrenceBacklogEntryV1(
                    occurrence_digest=item.occurrence_digest,
                    trigger_kind=trigger_kind,
                    state="lapsed",
                    window_end=item.window_end,
                    lapse_effective_at=effective,
                    reason_codes=occurrence_lapse_reason_codes(
                        item.window_end,
                        horizon,
                        evaluation_time=evaluation,
                    ),
                )
            )
            continue
        late.append(
            OccurrenceBacklogEntryV1(
                occurrence_digest=item.occurrence_digest,
                trigger_kind=item.occurrence.trigger_kind,
                state="late_eligible",
                window_end=item.window_end,
                lapse_effective_at=effective,
            )
        )
    return LineOccurrenceBacklogV1(
        line_id=line_id,
        occurrence_epoch=occurrence_epoch,
        line_spec_digest=line_spec_digest,
        acquisition_policy_digest=horizon.acquisition_policy_digest,
        head_vector_digest=head_vector.vector_digest,
        evaluation_time=evaluation,
        coverage="available",
        late_eligible=tuple(sorted(late, key=lambda item: item.occurrence_digest.encode("ascii"))),
        lapsed=tuple(sorted(lapsed, key=lambda item: item.occurrence_digest.encode("ascii"))),
    )


def unavailable_occurrence_backlog(
    *,
    line_id: str,
    occurrence_epoch: int,
    line_spec_digest: str,
    policy: SourceAcquisitionPolicyV1,
    head_vector: JournalHeadVectorV1,
    evaluation_time: datetime,
    reason_codes: tuple[str, ...],
) -> LineOccurrenceBacklogV1:
    """Report honest unavailable coverage instead of a fabricated zero backlog."""

    return LineOccurrenceBacklogV1(
        line_id=line_id,
        occurrence_epoch=occurrence_epoch,
        line_spec_digest=line_spec_digest,
        acquisition_policy_digest=acquisition_policy_digest(policy).tagged,
        head_vector_digest=head_vector.vector_digest,
        evaluation_time=ensure_utc(evaluation_time),
        coverage="unavailable",
        coverage_reason_codes=tuple(
            sorted(set(reason_codes), key=lambda item: item.encode("utf-8"))
        ),
    )


def require_original_window_acquirable(
    occurrence: BacklogOccurrenceV1,
    *,
    policy: SourceAcquisitionPolicyV1,
    evaluation_time: datetime,
) -> None:
    """Refuse a claim on a lapsed occurrence's original window, receipt or not.

    The refusal derives from the same pure classification the read paths use, so
    it holds even when no writer was ever online to append the lapse receipt.
    """

    horizon = acquisition_lapse_horizon(policy)
    effective = occurrence_lapse_effective_at(occurrence.window_end, horizon)
    if effective is not None and ensure_utc(evaluation_time) >= effective:
        raise LineRuntimeRefusal(
            "playbill.line.occurrence_lapsed",
            "This occurrence's original window can no longer be truthfully fulfilled.",
        )


# ---------------------------------------------------------------------------
# Journal read boundaries: coverage law, never a write
# ---------------------------------------------------------------------------


@runtime_checkable
class LineLandingJournalProtocol(Protocol):
    """The landing-journal reads a Line watcher needs; PC-C's seam, read-only."""

    def events_after(self, cursor: str | None = None) -> tuple[CaptureLandingEventV1, ...]: ...

    def vector_cursor(self) -> tuple[CaptureCursorV1, ...]: ...


class LineControlCoverageV1(_StrictBacklogModel):
    """Journal-derived occurrence/attempt state, or an honest unavailable verdict."""

    tag: Literal["playbill-line-control-coverage-v1"] = "playbill-line-control-coverage-v1"
    coverage: OperationalCoverageV1
    reason_codes: tuple[str, ...] = ()
    attempts: tuple[tuple[str, int, LineAttemptStatusV1 | None], ...] = ()
    materialized: tuple[str, ...] = ()

    _reasons = field_validator("reason_codes")(_sorted_reason_codes)

    @model_validator(mode="after")
    def _shape(self) -> "LineControlCoverageV1":
        if self.coverage == "available":
            if self.reason_codes:
                raise ValueError("available control coverage names no unavailability reason")
        elif not self.reason_codes:
            raise ValueError("unavailable control coverage must name a reason")
        elif self.attempts or self.materialized:
            raise ValueError("unavailable control coverage reports no journal-derived state")
        return self

    @property
    def attempt_state(self) -> Mapping[str, tuple[int, LineAttemptStatusV1 | None]]:
        return {digest: (count, status) for digest, count, status in self.attempts}


def _partition_key(stream: JournalStreamIdentityV1, partition_id: str) -> tuple[str, str, str, str]:
    return (stream.instance_id, stream.journal_family, stream.stream_id, partition_id)


def verify_line_journal_coverage(
    journal: LocalJournalBackend,
    deployment: LineDeploymentV1,
    declared_head_vector: JournalHeadVectorV1,
) -> tuple[str, ...]:
    """Return every coverage reason this journal fails the declared head vector."""

    declared: dict[tuple[str, str, str, str], JournalPartitionHeadV1] = {
        _partition_key(head.stream, head.partition_id): head
        for head in declared_head_vector.partitions
    }
    reasons: list[str] = []
    for stream, partition_id in line_partition_pairs(deployment):
        head = declared.get(_partition_key(stream, partition_id))
        if head is None:
            reasons.append(COVERAGE_HEAD_INCOMPLETE)
            continue
        try:
            local = journal.read_head(stream, partition_id)
        except PlaybillJournalError:
            reasons.append(COVERAGE_UNVERIFIED)
            continue
        if head.sequence == 0:
            continue
        if local.sequence == 0:
            reasons.append(COVERAGE_MISSING)
            continue
        if local.sequence < head.sequence:
            reasons.append(COVERAGE_TRUNCATED)
            continue
        try:
            # read_exact_range verifies identity, continuity, and both boundaries.
            records = journal.read_exact_range(
                journal.range_from_sequences(
                    stream,
                    partition_id,
                    first_sequence=1,
                    last_sequence=head.sequence,
                )
            )
        except PlaybillJournalError:
            reasons.append(COVERAGE_UNVERIFIED)
            continue
        if records[-1].record_digest != head.record_digest:
            reasons.append(COVERAGE_FORKED)
    return tuple(sorted(set(reasons), key=lambda item: item.encode("utf-8")))


def read_line_control_state(
    *,
    journal: LocalJournalBackend,
    bodies: ContentAddressedBodyStore,
    index: LineOccurrenceIndex,
    deployment: LineDeploymentV1,
    declared_head_vector: JournalHeadVectorV1,
    access: BodyAccessContext,
) -> LineControlCoverageV1:
    """Rebuild journal-derived occurrence/attempt state under the coverage law.

    This is a read path: it refreshes the disposable index cache and appends
    nothing to any journal.  A missing, truncated, forked, unverifiable, or
    unauthorized journal reports unavailable coverage rather than zero rows.
    """

    if not access.can_read_body:
        return LineControlCoverageV1(
            coverage="unavailable",
            reason_codes=(COVERAGE_UNAUTHORIZED,),
        )
    failures = verify_line_journal_coverage(journal, deployment, declared_head_vector)
    if failures:
        return LineControlCoverageV1(coverage="unavailable", reason_codes=failures)
    binding = deployment.journal_binding
    try:
        records = journal.all_records(binding.logical_stream, binding.control_partition_id)
        index.rebuild(records, bodies=bodies)
    except PlaybillCasError:
        return LineControlCoverageV1(
            coverage="unavailable",
            reason_codes=(COVERAGE_UNAUTHORIZED,),
        )
    except (PlaybillJournalError, LineRuntimeRefusal):
        return LineControlCoverageV1(
            coverage="unavailable",
            reason_codes=(COVERAGE_UNVERIFIED,),
        )
    materialized = index.occurrences(
        line_id=deployment.line_id,
        occurrence_epoch=deployment.occurrence_epoch,
    )
    attempts: list[tuple[str, int, LineAttemptStatusV1 | None]] = []
    for item in materialized:
        recorded = index.attempts(item.occurrence_digest)
        if not recorded:
            continue
        attempts.append((item.occurrence_digest, len(recorded), recorded[-1].status))
    return LineControlCoverageV1(
        coverage="available",
        attempts=tuple(sorted(attempts, key=lambda row: row[0].encode("ascii"))),
        materialized=tuple(
            sorted(
                (item.occurrence_digest for item in materialized),
                key=lambda digest: digest.encode("ascii"),
            )
        ),
    )


class LineLandingCoverageV1(_StrictBacklogModel):
    """Landing-journal candidates and their runner-independent window ends."""

    tag: Literal["playbill-line-landing-coverage-v1"] = "playbill-line-landing-coverage-v1"
    coverage: OperationalCoverageV1
    reason_codes: tuple[str, ...] = ()
    candidates: tuple[tuple[LineOccurrenceV1, datetime], ...] = ()

    _reasons = field_validator("reason_codes")(_sorted_reason_codes)

    @field_validator("candidates")
    @classmethod
    def _candidates(
        cls, value: tuple[tuple[LineOccurrenceV1, datetime], ...]
    ) -> tuple[tuple[LineOccurrenceV1, datetime], ...]:
        return tuple((occurrence, ensure_utc(window_end)) for occurrence, window_end in value)

    @model_validator(mode="after")
    def _shape(self) -> "LineLandingCoverageV1":
        if self.coverage == "available":
            if self.reason_codes:
                raise ValueError("available landing coverage names no unavailability reason")
        elif not self.reason_codes:
            raise ValueError("unavailable landing coverage must name a reason")
        elif self.candidates:
            raise ValueError("unavailable landing coverage reports no candidates")
        return self


def read_landing_backlog_candidates(
    landing_journal: LineLandingJournalProtocol,
    *,
    line_id: str,
    occurrence_epoch: int,
    trigger: ResolvedCaptureLandingTriggerV1,
    cursor: str | None = None,
) -> LineLandingCoverageV1:
    """Derive landing occurrences from real journal reads under the coverage law.

    A landing occurrence's window closes when its anchor landed, a landing-journal
    fact that no Line runner's uptime can move.
    """

    try:
        events = landing_journal.events_after(cursor)
    except CaptureJournalError:
        return LineLandingCoverageV1(
            coverage="unavailable",
            reason_codes=(COVERAGE_UNVERIFIED,),
        )
    landed = {event.event_id: event.landed_at for event in events}
    occurrences = derive_landing_occurrences(
        line_id=line_id,
        occurrence_epoch=occurrence_epoch,
        trigger=trigger,
        events=events,
    )
    return LineLandingCoverageV1(
        coverage="available",
        candidates=tuple((occurrence, landed[occurrence.event_id]) for occurrence in occurrences),
    )


def read_window_close_backlog_candidates(
    landing_journal: LineLandingJournalProtocol,
    *,
    line_id: str,
    occurrence_epoch: int,
    trigger: ResolvedWindowCloseTriggerV1,
    from_cursors: tuple[CaptureCursorV1, ...] = (),
) -> LineLandingCoverageV1:
    """Close a window from real cursor reads; the last covered landing ends it."""

    try:
        events = landing_journal.events_after(None)
        to_cursors = landing_journal.vector_cursor()
    except CaptureJournalError:
        return LineLandingCoverageV1(
            coverage="unavailable",
            reason_codes=(COVERAGE_UNVERIFIED,),
        )
    try:
        occurrences = derive_window_close_occurrences(
            line_id=line_id,
            occurrence_epoch=occurrence_epoch,
            trigger=trigger,
            from_cursors=from_cursors,
            to_cursors=to_cursors,
        )
    except LineRuntimeRefusal:
        return LineLandingCoverageV1(
            coverage="unavailable",
            reason_codes=(COVERAGE_UNVERIFIED,),
        )
    if not occurrences:
        return LineLandingCoverageV1(coverage="available")
    covered = {(cursor.partition_id, cursor.sequence) for cursor in to_cursors}
    landings = tuple(
        event.landed_at for event in events if (event.partition_id, event.sequence) in covered
    )
    if not landings:
        return LineLandingCoverageV1(
            coverage="unavailable",
            reason_codes=(COVERAGE_MISSING,),
        )
    window_end = max(landings)
    return LineLandingCoverageV1(
        coverage="available",
        candidates=tuple((occurrence, window_end) for occurrence in occurrences),
    )


def cadence_backlog_candidates(
    *,
    line_id: str,
    occurrence_epoch: int,
    trigger: ResolvedCadenceTriggerV1,
    evaluation_time: datetime,
    last_tick_index: int | None = None,
) -> tuple[tuple[LineOccurrenceV1, datetime], ...]:
    """Derive due cadence ticks and close each tick's window purely from schedule."""

    occurrences = derive_cadence_occurrences(
        line_id=line_id,
        occurrence_epoch=occurrence_epoch,
        trigger=trigger,
        evaluation_time=evaluation_time,
        last_tick_index=last_tick_index,
    )
    return tuple(
        (occurrence, cadence_window_end(trigger, occurrence.tick_index))
        for occurrence in occurrences
    )


# ---------------------------------------------------------------------------
# The fenced lapse receipt
# ---------------------------------------------------------------------------


class OccurrenceLapseV1(_StrictBacklogModel):
    """The normative lapse payload: no runner, no backend, no operational time."""

    tag: Literal["playbill-occurrence-lapse-v1"] = "playbill-occurrence-lapse-v1"
    line_id: str
    occurrence_epoch: int = Field(ge=0)
    occurrence_digest: str
    acquisition_policy_digest: str
    reason_codes: tuple[str, ...]
    lapse_effective_at: datetime

    _digests = field_validator("occurrence_digest", "acquisition_policy_digest")(_tagged_digest)

    @field_validator("reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("a lapse record must name why the window cannot be fulfilled")
        return _sorted_reason_codes(value)

    @field_validator("lapse_effective_at")
    @classmethod
    def _effective_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("lapse_effective_at", when_used="json")
    def _serialize_effective_at(self, value: datetime) -> str | None:
        return format_datetime(value)


def occurrence_lapse_identity_digest(lapse: OccurrenceLapseV1) -> str:
    """Address a lapse by occurrence coordinate and pinned policy alone."""

    return typed_digest(
        Sha256Value,
        "playbill-occurrence-lapse-identity-v1",
        {
            "acquisition_policy_digest": lapse.acquisition_policy_digest,
            "line_id": lapse.line_id,
            "occurrence_digest": lapse.occurrence_digest,
            "occurrence_epoch": lapse.occurrence_epoch,
        },
    ).tagged


def build_occurrence_lapse(
    occurrence: BacklogOccurrenceV1,
    *,
    policy: SourceAcquisitionPolicyV1,
    evaluation_time: datetime,
) -> OccurrenceLapseV1:
    """Memorialize what the pure classification already decided; never decide it."""

    horizon = acquisition_lapse_horizon(policy)
    effective = occurrence_lapse_effective_at(occurrence.window_end, horizon)
    reasons = occurrence_lapse_reason_codes(
        occurrence.window_end,
        horizon,
        evaluation_time=evaluation_time,
    )
    if effective is None or not reasons:
        raise LineRuntimeRefusal(
            "playbill.line.lapse_not_effective",
            "This occurrence is not lapsed under its pinned policy at this time.",
        )
    return OccurrenceLapseV1(
        line_id=occurrence.occurrence.line_id,
        occurrence_epoch=occurrence.occurrence.occurrence_epoch,
        occurrence_digest=occurrence.occurrence_digest,
        acquisition_policy_digest=horizon.acquisition_policy_digest,
        reason_codes=reasons,
        lapse_effective_at=effective,
    )


def lapse_journal_payload(
    lapse: OccurrenceLapseV1,
    *,
    deployment: LineDeploymentV1,
    recorded_at: datetime,
) -> dict[str, object]:
    """Separate the normative lapse from the operational audit envelope."""

    return {
        "tag": "playbill-line-occurrence-lapsed-v1",
        "lapse": lapse.model_dump(mode="json"),
        "lapse_identity_digest": occurrence_lapse_identity_digest(lapse),
        # Runner and backend identity are audit-only: they cannot change the
        # disposition, the lapse payload, or the occurrence identity.
        "audit": {
            "deployment_digest": line_deployment_digest(deployment),
            "deployment_revision": deployment.revision,
            "recorded_at": format_datetime(recorded_at),
            "runner_id": deployment.runner.runner_id,
        },
    }


def read_line_lapse_records(
    journal: LocalJournalBackend,
    deployment: LineDeploymentV1,
) -> tuple[StoredProcedureJournalRecordV1, ...]:
    """Read the control partition's lapse receipts; reading never appends."""

    binding = deployment.journal_binding
    return tuple(
        stored
        for stored in journal.all_records(binding.logical_stream, binding.control_partition_id)
        if stored.record.event_kind == "occurrence_lapsed"
    )


class LineLapseRecorder:
    """Only the active fenced writer memorializes a lapse, and only once."""

    def __init__(
        self,
        *,
        journal: LocalJournalBackend,
        bodies: ContentAddressedBodyStore,
        deployment: LineDeploymentV1,
        lease: LineLeaseV1,
        accepted_coordinate: AcceptedCoordinate,
    ) -> None:
        self.journal = journal
        self.bodies = bodies
        self.deployment = deployment
        self.lease = lease
        self.accepted_coordinate = accepted_coordinate
        self._writer = ProcedureExhaustWriter(
            journal=journal,
            bodies=bodies,
            fencing_token=lease.fencing_token,
        )

    def record(
        self,
        lapse: OccurrenceLapseV1,
        *,
        line_spec_digest: str,
        procedure_artifact_digest: str,
        definition_digest: str,
        actor_context: GovernedActorContext,
        recorded_at: datetime,
    ) -> StoredProcedureJournalRecordV1:
        """Append one lapse receipt under the fence; a retry is a no-op."""

        verify_line_lease(self.journal, self.deployment, self.lease)
        if lapse.line_id != self.deployment.line_id or (
            lapse.occurrence_epoch != self.deployment.occurrence_epoch
        ):
            raise LineRuntimeRefusal(
                "playbill.line.lapse_occurrence_foreign",
                "Lapse names another Line identity or occurrence epoch than this deployment.",
            )
        payload = lapse_journal_payload(
            lapse,
            deployment=self.deployment,
            recorded_at=recorded_at,
        )
        existing = self._existing(lapse)
        if existing is not None:
            return existing
        try:
            return self._writer.append(
                stream=self.deployment.journal_binding.logical_stream,
                partition_id=self.deployment.journal_binding.control_partition_id,
                event_kind="occurrence_lapsed",
                accepted_coordinate=self.accepted_coordinate,
                procedure_artifact_digest=procedure_artifact_digest,
                definition_digest=definition_digest,
                actor_context=actor_context,
                recorded_at=recorded_at,
                payload=payload,
                line_spec_digest=line_spec_digest,
                occurrence_id=lapse.occurrence_digest,
            )
        except PlaybillJournalError as exc:
            raise LineRuntimeRefusal(
                "playbill.line.journal_append_refused",
                f"Line control journal refused the lapse append: {exc}",
            ) from exc

    def _existing(self, lapse: OccurrenceLapseV1) -> StoredProcedureJournalRecordV1 | None:
        """Return the receipt already memorializing this exact lapse identity."""

        identity = occurrence_lapse_identity_digest(lapse)
        access = BodyAccessContext(principal_id="line-lapse-recorder", can_read_body=True)
        for stored in read_line_lapse_records(self.journal, self.deployment):
            if stored.record.occurrence_id != lapse.occurrence_digest:
                continue
            recorded = parse_journal_payload(
                self.bodies.read(stored.record.payload_digest, access=access)
            )
            if not isinstance(recorded, dict):  # pragma: no cover - writer wrote a dict
                continue
            if recorded.get("lapse_identity_digest") != identity:
                continue
            if recorded.get("lapse") != lapse.model_dump(mode="json"):
                raise LineRuntimeRefusal(
                    "playbill.line.lapse_payload_conflict",
                    "This lapse identity is already recorded with a different payload.",
                )
            return stored
        return None


__all__ = [
    "COVERAGE_FORKED",
    "COVERAGE_HEAD_INCOMPLETE",
    "COVERAGE_MISSING",
    "COVERAGE_TRUNCATED",
    "COVERAGE_UNAUTHORIZED",
    "COVERAGE_UNVERIFIED",
    "LAPSE_OUT_OF_AGE",
    "LAPSE_UNREPLAYABLE",
    "AcquisitionLapseHorizonV1",
    "BacklogOccurrenceV1",
    "LineControlCoverageV1",
    "LineLandingCoverageV1",
    "LineLandingJournalProtocol",
    "LineLapseRecorder",
    "LineOccurrenceBacklogV1",
    "OccurrenceBacklogEntryV1",
    "OccurrenceBacklogStateV1",
    "OccurrenceLapseV1",
    "OperationalCoverageV1",
    "acquisition_lapse_horizon",
    "build_backlog_occurrences",
    "build_occurrence_lapse",
    "cadence_backlog_candidates",
    "cadence_window_end",
    "classify_occurrence_backlog",
    "lapse_journal_payload",
    "occurrence_lapse_effective_at",
    "occurrence_lapse_identity_digest",
    "occurrence_lapse_reason_codes",
    "read_landing_backlog_candidates",
    "read_line_control_state",
    "read_line_lapse_records",
    "read_window_close_backlog_candidates",
    "require_original_window_acquirable",
    "unavailable_occurrence_backlog",
    "verify_line_journal_coverage",
]
