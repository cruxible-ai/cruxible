"""The one shared Line watcher: occurrence derivation, attempts, and fencing.

Occurrence identity is a deterministic function of the accepted trigger policy,
the journal state, and the occurrence epoch.  No wall clock enters an
occurrence preimage: cadence occurrences carry a tick index derived from the
pinned schedule, window-close occurrences carry the journal cursor vectors they
cover, landing occurrences carry the exact landing coordinate, and manual
occurrences carry the request handle.  Two schedulers reading the same head
therefore derive byte-identical occurrence identities.

Everything that must survive a restart lives in the exhaust journal.  The
SQLite index in this module is a disposable cache: deleting it and rebuilding
from the verified journal prefix must change no answer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
)
from cruxible_client.contracts.capture_journal import CaptureCursorV1, CaptureLandingEventV1
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.errors import PlaybillJournalError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpecV1,
    CadenceTriggerPolicyV1,
    CaptureLandingTriggerPolicyV1,
    LineSpecV1,
    ManualTriggerPolicyV1,
    WindowCloseTriggerPolicyV1,
)
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    LocalJournalBackend,
    ProcedureExhaustWriter,
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
)
from cruxible_core.playbill.lines import (
    LineDeploymentV1,
    LineLeaseV1,
    LineRuntimeRefusal,
    line_deployment_digest,
    verify_line_lease,
)
from cruxible_core.playbill.occurrences import (
    CadenceOccurrenceV1,
    CaptureLandingOccurrenceV1,
    LineOccurrenceV1,
    ManualOccurrenceV1,
    OccurrenceAttemptV1,
    WindowCloseOccurrenceV1,
    capture_landing_occurrence,
    line_occurrence_digest,
    window_advance_count,
)
from cruxible_core.playbill.procedures.execution import ProcedureRunStatusV1
from cruxible_core.playbill.projection import AcceptedCoordinate

LineAttemptStatusV1 = ProcedureRunStatusV1
_RETRYABLE_STATUSES: frozenset[str] = frozenset({"failed", "budget_exhausted"})
_MAX_MANUAL_REQUESTS = 1024


class _StrictSchedulerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _tagged_digest(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


class LineDispatchRulesV1(_StrictSchedulerModel):
    """Operational retry/overlap discipline; it never enters occurrence identity."""

    tag: Literal["playbill-line-dispatch-rules-v1"] = "playbill-line-dispatch-rules-v1"
    overlap: Literal["serial", "concurrent"]
    max_attempts: int = Field(ge=1, le=64)
    backoff: CanonicalDurationV1
    backoff_multiplier: int = Field(ge=1, le=16)
    max_backoff: CanonicalDurationV1

    @model_validator(mode="after")
    def _bounds(self) -> "LineDispatchRulesV1":
        if self.max_backoff.microseconds < self.backoff.microseconds:
            raise ValueError("Line dispatch max_backoff must not be below its base backoff")
        return self


class ResolvedCadenceTriggerV1(_StrictSchedulerModel):
    """The pinned cadence Policy resolved into an exact deterministic schedule."""

    tag: Literal["playbill-resolved-cadence-trigger-v1"] = "playbill-resolved-cadence-trigger-v1"
    trigger_kind: Literal["cadence"] = "cadence"
    cadence_policy_digest: str
    anchor: datetime
    interval: CanonicalDurationV1
    max_backfill_occurrences: int = Field(ge=0, le=4096)
    dispatch: LineDispatchRulesV1

    _policy = field_validator("cadence_policy_digest")(_tagged_digest)

    @field_validator("anchor")
    @classmethod
    def _anchor(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("anchor", when_used="json")
    def _serialize_anchor(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _interval(self) -> "ResolvedCadenceTriggerV1":
        if self.interval.microseconds < 1:
            raise ValueError("cadence interval must be at least one microsecond")
        return self


class ResolvedCaptureLandingTriggerV1(_StrictSchedulerModel):
    """The pinned anchor CaptureContract and LandingFilter resolved for matching."""

    tag: Literal["playbill-resolved-capture-landing-trigger-v1"] = (
        "playbill-resolved-capture-landing-trigger-v1"
    )
    trigger_kind: Literal["capture_landing"] = "capture_landing"
    anchor_capture_contract_digest: str
    landing_filter_digest: str
    producer_binding_digests: tuple[str, ...] = ()
    dispatch: LineDispatchRulesV1

    _digests = field_validator("anchor_capture_contract_digest", "landing_filter_digest")(
        _tagged_digest
    )

    @field_validator("producer_binding_digests")
    @classmethod
    def _producers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("landing filter producer bindings must be sorted and unique")
        for item in value:
            _tagged_digest(item)
        return value


class ResolvedWindowCloseTriggerV1(_StrictSchedulerModel):
    """The pinned window Policy resolved into a cursor-vector close rule."""

    tag: Literal["playbill-resolved-window-close-trigger-v1"] = (
        "playbill-resolved-window-close-trigger-v1"
    )
    trigger_kind: Literal["window_close"] = "window_close"
    window_policy_digest: str
    min_new_events: int = Field(ge=1, le=2**31 - 1)
    dispatch: LineDispatchRulesV1

    _policy = field_validator("window_policy_digest")(_tagged_digest)


class ResolvedManualTriggerV1(_StrictSchedulerModel):
    """Manual triggers pin no policy artifact, so only dispatch rules resolve."""

    tag: Literal["playbill-resolved-manual-trigger-v1"] = "playbill-resolved-manual-trigger-v1"
    trigger_kind: Literal["manual"] = "manual"
    dispatch: LineDispatchRulesV1


ResolvedTriggerV1 = Annotated[
    ResolvedCadenceTriggerV1
    | ResolvedCaptureLandingTriggerV1
    | ResolvedManualTriggerV1
    | ResolvedWindowCloseTriggerV1,
    Field(discriminator="trigger_kind"),
]

_OCCURRENCE_ADAPTER: TypeAdapter[LineOccurrenceV1] = TypeAdapter(LineOccurrenceV1)


def bind_resolved_trigger(line: LineSpecV1, resolved: ResolvedTriggerV1) -> None:
    """Refuse a resolved trigger that is not the exact policy the LineSpec pins."""

    policy = line.trigger_policy
    if policy.kind != resolved.trigger_kind:
        raise LineRuntimeRefusal(
            "playbill.line.trigger_policy_mismatch",
            "Resolved trigger kind differs from the accepted LineSpec trigger policy.",
        )
    matched = True
    if isinstance(policy, CadenceTriggerPolicyV1) and isinstance(
        resolved, ResolvedCadenceTriggerV1
    ):
        matched = policy.cadence_policy_digest == resolved.cadence_policy_digest
    elif isinstance(policy, CaptureLandingTriggerPolicyV1) and isinstance(
        resolved, ResolvedCaptureLandingTriggerV1
    ):
        matched = (
            policy.anchor_capture_contract_digest == resolved.anchor_capture_contract_digest
            and policy.landing_filter_digest == resolved.landing_filter_digest
        )
    elif isinstance(policy, WindowCloseTriggerPolicyV1) and isinstance(
        resolved, ResolvedWindowCloseTriggerV1
    ):
        matched = policy.window_policy_digest == resolved.window_policy_digest
    elif not (
        isinstance(policy, ManualTriggerPolicyV1) and isinstance(resolved, ResolvedManualTriggerV1)
    ):  # pragma: no cover - closed discriminated union
        matched = False
    if not matched:
        raise LineRuntimeRefusal(
            "playbill.line.trigger_policy_mismatch",
            "Resolved trigger does not carry the exact pinned trigger digests.",
        )


def cadence_occurrence_time(trigger: ResolvedCadenceTriggerV1, tick_index: int) -> datetime:
    """Return one tick's scheduled time; a derived fact, never part of identity."""

    return trigger.anchor + timedelta(microseconds=trigger.interval.microseconds * tick_index)


def derive_cadence_occurrences(
    *,
    line_id: str,
    occurrence_epoch: int,
    trigger: ResolvedCadenceTriggerV1,
    evaluation_time: datetime,
    last_tick_index: int | None = None,
) -> tuple[CadenceOccurrenceV1, ...]:
    """Derive due cadence ticks under the pinned schedule and its backfill bound."""

    evaluation = ensure_utc(evaluation_time)
    if evaluation < trigger.anchor:
        return ()
    interval = timedelta(microseconds=trigger.interval.microseconds)
    due_tick = (evaluation - trigger.anchor) // interval
    first = 0 if last_tick_index is None else last_tick_index + 1
    first = max(first, due_tick - trigger.max_backfill_occurrences)
    if first > due_tick:
        return ()
    return tuple(
        CadenceOccurrenceV1(
            line_id=line_id,
            occurrence_epoch=occurrence_epoch,
            cadence_policy_digest=trigger.cadence_policy_digest,
            tick_index=tick,
        )
        for tick in range(first, due_tick + 1)
    )


def derive_landing_occurrences(
    *,
    line_id: str,
    occurrence_epoch: int,
    trigger: ResolvedCaptureLandingTriggerV1,
    events: tuple[CaptureLandingEventV1, ...],
) -> tuple[CaptureLandingOccurrenceV1, ...]:
    """Select anchor landings deterministically across every capture partition."""

    admitted = tuple(
        event
        for event in events
        if event.capture_contract_digest == trigger.anchor_capture_contract_digest
        and (
            not trigger.producer_binding_digests
            or event.producer_binding_digest in trigger.producer_binding_digests
        )
    )
    ordered = sorted(
        admitted, key=lambda event: (event.partition_id.encode("ascii"), event.sequence)
    )
    seen: set[str] = set()
    occurrences: list[CaptureLandingOccurrenceV1] = []
    for event in ordered:
        occurrence = capture_landing_occurrence(
            line_id=line_id,
            occurrence_epoch=occurrence_epoch,
            anchor=event,
        )
        digest = line_occurrence_digest(occurrence)
        if digest in seen:
            continue
        seen.add(digest)
        occurrences.append(occurrence)
    return tuple(occurrences)


def derive_window_close_occurrences(
    *,
    line_id: str,
    occurrence_epoch: int,
    trigger: ResolvedWindowCloseTriggerV1,
    from_cursors: tuple[CaptureCursorV1, ...],
    to_cursors: tuple[CaptureCursorV1, ...],
) -> tuple[WindowCloseOccurrenceV1, ...]:
    """Close a window from cursor vectors alone; close time never enters identity."""

    try:
        advance = window_advance_count(from_cursors, to_cursors)
    except ValueError as exc:
        raise LineRuntimeRefusal(
            "playbill.line.window_cursor_regressed",
            f"Window cursor vector is not a forward extension: {exc}",
        ) from exc
    if advance < trigger.min_new_events:
        return ()
    return (
        WindowCloseOccurrenceV1(
            line_id=line_id,
            occurrence_epoch=occurrence_epoch,
            window_policy_digest=trigger.window_policy_digest,
            from_cursors=from_cursors,
            to_cursors=to_cursors,
        ),
    )


def derive_manual_occurrences(
    *,
    line_id: str,
    occurrence_epoch: int,
    request_ids: tuple[str, ...],
) -> tuple[ManualOccurrenceV1, ...]:
    """Materialize one occurrence per distinct request handle, in canonical order."""

    if len(request_ids) > _MAX_MANUAL_REQUESTS:
        raise LineRuntimeRefusal(
            "playbill.line.manual_request_batch_too_large",
            "Manual trigger batch exceeds the bounded request limit.",
        )
    ordered = tuple(sorted(set(request_ids), key=lambda item: item.encode("utf-8")))
    return tuple(
        ManualOccurrenceV1(
            line_id=line_id,
            occurrence_epoch=occurrence_epoch,
            request_id=request_id,
        )
        for request_id in ordered
    )


class IndexedLineOccurrenceV1(_StrictSchedulerModel):
    """One materialized occurrence with the exact LineSpec it is pinned to."""

    tag: Literal["playbill-indexed-line-occurrence-v1"] = "playbill-indexed-line-occurrence-v1"
    occurrence: LineOccurrenceV1
    occurrence_digest: str
    line_spec_digest: str
    procedure_artifact_digest: str
    definition_digest: str
    sequence: int = Field(ge=1)

    _digests = field_validator(
        "occurrence_digest",
        "line_spec_digest",
        "procedure_artifact_digest",
        "definition_digest",
    )(_tagged_digest)

    @model_validator(mode="after")
    def _reproduces(self) -> "IndexedLineOccurrenceV1":
        if line_occurrence_digest(self.occurrence) != self.occurrence_digest:
            raise ValueError("indexed occurrence digest does not reproduce")
        return self


class IndexedLineAttemptV1(_StrictSchedulerModel):
    """One attempt against an occurrence; attempts never change occurrence identity."""

    tag: Literal["playbill-indexed-line-attempt-v1"] = "playbill-indexed-line-attempt-v1"
    attempt: OccurrenceAttemptV1
    status: LineAttemptStatusV1 | None = None
    started_sequence: int = Field(ge=1)
    finalized_sequence: int | None = None
    started_at: datetime
    finalized_at: datetime | None = None

    @field_validator("started_at", "finalized_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_serializer("started_at", "finalized_at", when_used="json")
    def _serialize_times(self, value: datetime | None) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _finalization(self) -> "IndexedLineAttemptV1":
        finalized = (
            self.status is not None,
            self.finalized_sequence is not None,
            self.finalized_at is not None,
        )
        if len(set(finalized)) != 1:
            raise ValueError("a finalized attempt carries status, sequence, and time together")
        return self

    @property
    def in_flight(self) -> bool:
        return self.status is None


_INDEX_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS line_occurrence_index (
        occurrence_digest TEXT PRIMARY KEY,
        line_id TEXT NOT NULL,
        occurrence_epoch INTEGER NOT NULL,
        trigger_kind TEXT NOT NULL,
        line_spec_digest TEXT NOT NULL,
        procedure_artifact_digest TEXT NOT NULL,
        definition_digest TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        occurrence_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS line_attempt_index (
        occurrence_digest TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        status TEXT,
        started_sequence INTEGER NOT NULL,
        finalized_sequence INTEGER,
        started_at TEXT NOT NULL,
        finalized_at TEXT,
        PRIMARY KEY (occurrence_digest, attempt)
    )
    """,
)


class LineOccurrenceIndex:
    """A cache only: deleting the database and rebuilding must change no answer."""

    def __init__(self, path: Path) -> None:
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise LineRuntimeRefusal(
                "playbill.line.index_not_a_file",
                "Line occurrence index must be a regular file.",
            )
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise LineRuntimeRefusal(
                "playbill.line.index_parent_untrusted",
                "Line occurrence index parent must be a regular directory.",
            )
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        for statement in _INDEX_SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_occurrence(self, occurrence_digest: str) -> IndexedLineOccurrenceV1 | None:
        row = self._conn.execute(
            "SELECT * FROM line_occurrence_index WHERE occurrence_digest = ?",
            (occurrence_digest,),
        ).fetchone()
        return None if row is None else self._occurrence_from_row(row)

    def occurrences(
        self,
        *,
        line_id: str,
        occurrence_epoch: int | None = None,
    ) -> tuple[IndexedLineOccurrenceV1, ...]:
        if occurrence_epoch is None:
            rows = self._conn.execute(
                "SELECT * FROM line_occurrence_index WHERE line_id = ? ORDER BY sequence",
                (line_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM line_occurrence_index WHERE line_id = ? AND occurrence_epoch = ? "
                "ORDER BY sequence",
                (line_id, occurrence_epoch),
            ).fetchall()
        return tuple(self._occurrence_from_row(row) for row in rows)

    def attempts(self, occurrence_digest: str) -> tuple[IndexedLineAttemptV1, ...]:
        rows = self._conn.execute(
            "SELECT * FROM line_attempt_index WHERE occurrence_digest = ? ORDER BY attempt",
            (occurrence_digest,),
        ).fetchall()
        return tuple(self._attempt_from_row(row) for row in rows)

    def in_flight_occurrence_digests(
        self,
        *,
        line_id: str,
        occurrence_epoch: int,
    ) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT DISTINCT a.occurrence_digest FROM line_attempt_index AS a "
            "JOIN line_occurrence_index AS o ON o.occurrence_digest = a.occurrence_digest "
            "WHERE a.status IS NULL AND o.line_id = ? AND o.occurrence_epoch = ? "
            "ORDER BY a.occurrence_digest",
            (line_id, occurrence_epoch),
        ).fetchall()
        return tuple(str(row["occurrence_digest"]) for row in rows)

    def last_cadence_tick(
        self,
        *,
        line_id: str,
        occurrence_epoch: int,
        cadence_policy_digest: str,
    ) -> int | None:
        ticks = [
            item.occurrence.tick_index
            for item in self.occurrences(line_id=line_id, occurrence_epoch=occurrence_epoch)
            if isinstance(item.occurrence, CadenceOccurrenceV1)
            and item.occurrence.cadence_policy_digest == cadence_policy_digest
        ]
        return max(ticks) if ticks else None

    def last_window_cursors(
        self,
        *,
        line_id: str,
        occurrence_epoch: int,
        window_policy_digest: str,
    ) -> tuple[CaptureCursorV1, ...]:
        latest: tuple[CaptureCursorV1, ...] = ()
        for item in self.occurrences(line_id=line_id, occurrence_epoch=occurrence_epoch):
            occurrence = item.occurrence
            if (
                isinstance(occurrence, WindowCloseOccurrenceV1)
                and occurrence.window_policy_digest == window_policy_digest
            ):
                latest = occurrence.to_cursors
        return latest

    def apply_record(
        self,
        stored: StoredProcedureJournalRecordV1,
        *,
        payload: CanonicalValue,
    ) -> None:
        record = stored.record
        if record.event_kind not in {
            "occurrence_materialized",
            "attempt_started",
            "attempt_finalized",
        }:
            return
        if not isinstance(payload, dict):
            raise LineRuntimeRefusal(
                "playbill.line.index_payload_malformed",
                "Line control payload is not a canonical object.",
            )
        if record.event_kind == "occurrence_materialized":
            self._apply_materialized(stored, payload)
        elif record.event_kind == "attempt_started":
            self._apply_attempt_started(stored, payload)
        else:
            self._apply_attempt_finalized(stored, payload)
        self._conn.commit()

    def rebuild(
        self,
        records: tuple[StoredProcedureJournalRecordV1, ...],
        *,
        bodies: ContentAddressedBodyStore,
    ) -> None:
        """Reproduce the cache from the authenticated prefix plus CAS coverage."""

        access = BodyAccessContext(principal_id="line-occurrence-index", can_read_body=True)
        self._conn.execute("DELETE FROM line_attempt_index")
        self._conn.execute("DELETE FROM line_occurrence_index")
        self._conn.commit()
        for stored in records:
            if stored.record.event_kind not in {
                "occurrence_materialized",
                "attempt_started",
                "attempt_finalized",
            }:
                continue
            payload = parse_journal_payload(
                bodies.read(stored.record.payload_digest, access=access)
            )
            self.apply_record(stored, payload=payload)

    @staticmethod
    def _occurrence_from_row(row: sqlite3.Row) -> IndexedLineOccurrenceV1:
        return IndexedLineOccurrenceV1(
            occurrence=_OCCURRENCE_ADAPTER.validate_python(json.loads(row["occurrence_json"])),
            occurrence_digest=str(row["occurrence_digest"]),
            line_spec_digest=str(row["line_spec_digest"]),
            procedure_artifact_digest=str(row["procedure_artifact_digest"]),
            definition_digest=str(row["definition_digest"]),
            sequence=int(row["sequence"]),
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> IndexedLineAttemptV1:
        finalized_at = row["finalized_at"]
        return IndexedLineAttemptV1(
            attempt=OccurrenceAttemptV1(
                occurrence_digest=str(row["occurrence_digest"]),
                attempt=int(row["attempt"]),
            ),
            status=row["status"],
            started_sequence=int(row["started_sequence"]),
            finalized_sequence=(
                None if row["finalized_sequence"] is None else int(row["finalized_sequence"])
            ),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            finalized_at=None
            if finalized_at is None
            else datetime.fromisoformat(str(finalized_at)),
        )

    def _apply_materialized(
        self,
        stored: StoredProcedureJournalRecordV1,
        payload: dict[str, CanonicalValue],
    ) -> None:
        record = stored.record
        occurrence = _OCCURRENCE_ADAPTER.validate_python(payload.get("occurrence"))
        digest = line_occurrence_digest(occurrence)
        if record.occurrence_id != digest or record.line_spec_digest is None:
            raise LineRuntimeRefusal(
                "playbill.line.index_occurrence_unbound",
                "Materialized record does not bind its exact occurrence and LineSpec.",
            )
        existing = self.get_occurrence(digest)
        if existing is not None:
            if existing.line_spec_digest != record.line_spec_digest:
                raise LineRuntimeRefusal(
                    "playbill.line.occurrence_repinned",
                    "One occurrence identity cannot be pinned to two LineSpec digests.",
                )
            return
        self._conn.execute(
            "INSERT INTO line_occurrence_index (occurrence_digest, line_id, occurrence_epoch, "
            "trigger_kind, line_spec_digest, procedure_artifact_digest, definition_digest, "
            "sequence, occurrence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                digest,
                occurrence.line_id,
                occurrence.occurrence_epoch,
                occurrence.trigger_kind,
                record.line_spec_digest,
                record.procedure_artifact_digest,
                record.definition_digest,
                record.sequence,
                canonical_bytes(occurrence.model_dump(mode="json")).decode("utf-8"),
            ),
        )

    def _apply_attempt_started(
        self,
        stored: StoredProcedureJournalRecordV1,
        payload: dict[str, CanonicalValue],
    ) -> None:
        record = stored.record
        if record.occurrence_id is None or record.attempt is None:
            raise LineRuntimeRefusal(
                "playbill.line.index_attempt_unbound",
                "Line attempt record does not bind an occurrence and attempt number.",
            )
        if self.get_occurrence(record.occurrence_id) is None:
            raise LineRuntimeRefusal(
                "playbill.line.occurrence_unknown",
                "Line attempt precedes its occurrence in the journal.",
            )
        started_at = payload.get("started_at")
        if not isinstance(started_at, str):
            raise LineRuntimeRefusal(
                "playbill.line.index_payload_malformed",
                "Line attempt record carries no start time.",
            )
        existing = self._conn.execute(
            "SELECT attempt FROM line_attempt_index WHERE occurrence_digest = ? AND attempt = ?",
            (record.occurrence_id, record.attempt),
        ).fetchone()
        if existing is not None:
            return
        self._conn.execute(
            "INSERT INTO line_attempt_index (occurrence_digest, attempt, status, "
            "started_sequence, finalized_sequence, started_at, finalized_at) "
            "VALUES (?, ?, NULL, ?, NULL, ?, NULL)",
            (record.occurrence_id, record.attempt, record.sequence, started_at),
        )

    def _apply_attempt_finalized(
        self,
        stored: StoredProcedureJournalRecordV1,
        payload: dict[str, CanonicalValue],
    ) -> None:
        record = stored.record
        if record.occurrence_id is None or record.attempt is None:
            raise LineRuntimeRefusal(
                "playbill.line.index_attempt_unbound",
                "Line attempt record does not bind an occurrence and attempt number.",
            )
        status = payload.get("status")
        finalized_at = payload.get("finalized_at")
        if status not in {"succeeded", "refused", "failed", "budget_exhausted"} or not isinstance(
            finalized_at, str
        ):
            raise LineRuntimeRefusal(
                "playbill.line.index_payload_malformed",
                "Line attempt finalization carries no valid status and time.",
            )
        row = self._conn.execute(
            "SELECT status FROM line_attempt_index WHERE occurrence_digest = ? AND attempt = ?",
            (record.occurrence_id, record.attempt),
        ).fetchone()
        if row is None:
            raise LineRuntimeRefusal(
                "playbill.line.attempt_not_in_flight",
                "Line attempt finalization has no started attempt.",
            )
        if row["status"] is not None:
            raise LineRuntimeRefusal(
                "playbill.line.attempt_already_finalized",
                "Line attempt was already finalized in this journal.",
            )
        self._conn.execute(
            "UPDATE line_attempt_index SET status = ?, finalized_sequence = ?, finalized_at = ? "
            "WHERE occurrence_digest = ? AND attempt = ?",
            (status, record.sequence, finalized_at, record.occurrence_id, record.attempt),
        )


class LineScheduler:
    """The one shared watcher: derive, materialize, and attempt under one fence."""

    def __init__(
        self,
        *,
        journal: LocalJournalBackend,
        bodies: ContentAddressedBodyStore,
        index: LineOccurrenceIndex,
        deployment: LineDeploymentV1,
        lease: LineLeaseV1,
        trigger: ResolvedTriggerV1,
        accepted_coordinate: AcceptedCoordinate,
    ) -> None:
        self.journal = journal
        self.bodies = bodies
        self.index = index
        self.deployment = deployment
        self.lease = lease
        self.trigger = trigger
        self.accepted_coordinate = accepted_coordinate
        self._writer = ProcedureExhaustWriter(
            journal=journal,
            bodies=bodies,
            fencing_token=lease.fencing_token,
        )

    @property
    def control_partition_id(self) -> str:
        return self.deployment.journal_binding.control_partition_id

    def refresh(self) -> None:
        """Rebuild the disposable index from the verified control-partition prefix."""

        self.index.rebuild(
            self.journal.all_records(
                self.deployment.journal_binding.logical_stream,
                self.control_partition_id,
            ),
            bodies=self.bodies,
        )

    def materialize(
        self,
        occurrence: LineOccurrenceV1,
        *,
        accepted_line: AcceptedLineSpecV1,
        accepted_procedure: AcceptedProcedureV1,
        actor_context: GovernedActorContext,
        materialized_at: datetime,
    ) -> IndexedLineOccurrenceV1:
        """Record one occurrence exactly once, pinned to the LineSpec that derived it."""

        self._require_lease()
        self.refresh()
        return self._materialize_one(
            occurrence,
            accepted_line=accepted_line,
            accepted_procedure=accepted_procedure,
            actor_context=actor_context,
            materialized_at=materialized_at,
        )

    def _materialize_one(
        self,
        occurrence: LineOccurrenceV1,
        *,
        accepted_line: AcceptedLineSpecV1,
        accepted_procedure: AcceptedProcedureV1,
        actor_context: GovernedActorContext,
        materialized_at: datetime,
    ) -> IndexedLineOccurrenceV1:
        self._verify_pins(occurrence, accepted_line, accepted_procedure)
        digest = line_occurrence_digest(occurrence)
        existing = self.index.get_occurrence(digest)
        if existing is not None:
            return existing
        definition_digest = accepted_procedure.procedure.definition_digest
        self._append(
            "occurrence_materialized",
            payload={
                "tag": "playbill-line-occurrence-materialized-v1",
                "occurrence": occurrence.model_dump(mode="json"),
                "occurrence_digest": digest,
                "line_spec_digest": accepted_line.artifact_digest,
                "deployment_digest": line_deployment_digest(self.deployment),
                "deployment_revision": self.deployment.revision,
                "runner_id": self.deployment.runner.runner_id,
                "materialized_at": format_datetime(materialized_at),
            },
            actor_context=actor_context,
            recorded_at=materialized_at,
            procedure_artifact_digest=accepted_procedure.artifact_digest,
            definition_digest=definition_digest,
            line_spec_digest=accepted_line.artifact_digest,
            occurrence_id=digest,
        )
        materialized = self.index.get_occurrence(digest)
        if materialized is None:  # pragma: no cover - append and index disagree
            raise LineRuntimeRefusal(
                "playbill.line.occurrence_unknown",
                "Materialized occurrence did not reproduce from its own journal.",
            )
        return materialized

    def materialize_all(
        self,
        occurrences: tuple[LineOccurrenceV1, ...],
        *,
        accepted_line: AcceptedLineSpecV1,
        accepted_procedure: AcceptedProcedureV1,
        actor_context: GovernedActorContext,
        materialized_at: datetime,
    ) -> tuple[IndexedLineOccurrenceV1, ...]:
        """Materialize one derived batch under a single lease check and index pass."""

        self._require_lease()
        self.refresh()
        return tuple(
            self._materialize_one(
                occurrence,
                accepted_line=accepted_line,
                accepted_procedure=accepted_procedure,
                actor_context=actor_context,
                materialized_at=materialized_at,
            )
            for occurrence in occurrences
        )

    def start_attempt(
        self,
        occurrence_digest: str,
        *,
        actor_context: GovernedActorContext,
        started_at: datetime,
    ) -> IndexedLineAttemptV1:
        """Open the next attempt under overlap, retry-budget, and backoff law."""

        self._require_lease()
        self.refresh()
        indexed = self.index.get_occurrence(occurrence_digest)
        if indexed is None:
            raise LineRuntimeRefusal(
                "playbill.line.occurrence_unknown",
                "No materialized occurrence carries this identity.",
            )
        attempts = self.index.attempts(occurrence_digest)
        self._check_attempt_admission(indexed, attempts, started_at=started_at)
        number = len(attempts) + 1
        self._append(
            "attempt_started",
            payload={
                "tag": "playbill-line-attempt-started-v1",
                "occurrence_digest": occurrence_digest,
                "attempt": number,
                "line_spec_digest": indexed.line_spec_digest,
                "deployment_digest": line_deployment_digest(self.deployment),
                "runner_id": self.deployment.runner.runner_id,
                "started_at": format_datetime(started_at),
            },
            actor_context=actor_context,
            recorded_at=started_at,
            procedure_artifact_digest=indexed.procedure_artifact_digest,
            definition_digest=indexed.definition_digest,
            line_spec_digest=indexed.line_spec_digest,
            occurrence_id=occurrence_digest,
            attempt=number,
        )
        return self._attempt(occurrence_digest, number)

    def finalize_attempt(
        self,
        occurrence_digest: str,
        attempt: int,
        *,
        status: LineAttemptStatusV1,
        actor_context: GovernedActorContext,
        finalized_at: datetime,
        failure: str | None = None,
    ) -> IndexedLineAttemptV1:
        """Close one attempt; the occurrence identity is untouched by the outcome."""

        self._require_lease()
        self.refresh()
        indexed = self.index.get_occurrence(occurrence_digest)
        if indexed is None:
            raise LineRuntimeRefusal(
                "playbill.line.occurrence_unknown",
                "No materialized occurrence carries this identity.",
            )
        current = next(
            (
                item
                for item in self.index.attempts(occurrence_digest)
                if item.attempt.attempt == attempt
            ),
            None,
        )
        if current is None:
            raise LineRuntimeRefusal(
                "playbill.line.attempt_not_in_flight",
                "No such attempt was started for this occurrence.",
            )
        if not current.in_flight:
            raise LineRuntimeRefusal(
                "playbill.line.attempt_already_finalized",
                "This attempt was already finalized.",
            )
        self._append(
            "attempt_finalized",
            payload={
                "tag": "playbill-line-attempt-finalized-v1",
                "occurrence_digest": occurrence_digest,
                "attempt": attempt,
                "status": status,
                "failure": failure,
                "finalized_at": format_datetime(finalized_at),
            },
            actor_context=actor_context,
            recorded_at=finalized_at,
            procedure_artifact_digest=indexed.procedure_artifact_digest,
            definition_digest=indexed.definition_digest,
            line_spec_digest=indexed.line_spec_digest,
            occurrence_id=occurrence_digest,
            attempt=attempt,
        )
        return self._attempt(occurrence_digest, attempt)

    def _attempt(self, occurrence_digest: str, attempt: int) -> IndexedLineAttemptV1:
        for item in self.index.attempts(occurrence_digest):
            if item.attempt.attempt == attempt:
                return item
        raise LineRuntimeRefusal(  # pragma: no cover - append and rebuild disagree
            "playbill.line.attempt_not_in_flight",
            "Attempt did not reproduce from its own journal.",
        )

    def _require_lease(self) -> None:
        verify_line_lease(self.journal, self.deployment, self.lease)

    def _verify_pins(
        self,
        occurrence: LineOccurrenceV1,
        accepted_line: AcceptedLineSpecV1,
        accepted_procedure: AcceptedProcedureV1,
    ) -> None:
        if accepted_line.artifact_digest != self.deployment.line_spec_digest:
            raise LineRuntimeRefusal(
                "playbill.line.deployment_spec_mismatch",
                "Accepted LineSpec is not the digest this deployment is bound to.",
            )
        if accepted_line.line.procedure.artifact_digest != accepted_procedure.artifact_digest:
            raise LineRuntimeRefusal(
                "playbill.line.procedure_pin_mismatch",
                "Accepted Procedure is not the exact artifact the LineSpec pins.",
            )
        if occurrence.line_id != self.deployment.line_id:
            raise LineRuntimeRefusal(
                "playbill.line.occurrence_line_mismatch",
                "Occurrence names a different Line than this deployment.",
            )
        if occurrence.occurrence_epoch != accepted_line.line.occurrence_epoch:
            raise LineRuntimeRefusal(
                "playbill.line.occurrence_epoch_stale",
                "Occurrence epoch differs from the accepted LineSpec epoch.",
            )
        bind_resolved_trigger(accepted_line.line, self.trigger)

    def _check_attempt_admission(
        self,
        indexed: IndexedLineOccurrenceV1,
        attempts: tuple[IndexedLineAttemptV1, ...],
        *,
        started_at: datetime,
    ) -> None:
        dispatch = self.trigger.dispatch
        if any(item.in_flight for item in attempts):
            raise LineRuntimeRefusal(
                "playbill.line.attempt_in_flight",
                "This occurrence already has an unfinalized attempt.",
            )
        if attempts and attempts[-1].status not in _RETRYABLE_STATUSES:
            raise LineRuntimeRefusal(
                "playbill.line.attempt_not_retryable",
                "A succeeded or refused occurrence is terminal for this epoch.",
            )
        if len(attempts) >= dispatch.max_attempts:
            raise LineRuntimeRefusal(
                "playbill.line.attempt_budget_exhausted",
                "Occurrence exhausted its bounded retry budget.",
            )
        if dispatch.overlap == "serial":
            others = tuple(
                digest
                for digest in self.index.in_flight_occurrence_digests(
                    line_id=indexed.occurrence.line_id,
                    occurrence_epoch=indexed.occurrence.occurrence_epoch,
                )
                if digest != indexed.occurrence_digest
            )
            if others:
                raise LineRuntimeRefusal(
                    "playbill.line.overlap_blocked",
                    "A serial Line cannot run two occurrences concurrently.",
                )
        if attempts:
            last = attempts[-1]
            if last.finalized_at is not None:
                delay = min(
                    dispatch.backoff.microseconds
                    * dispatch.backoff_multiplier ** (last.attempt.attempt - 1),
                    dispatch.max_backoff.microseconds,
                )
                if ensure_utc(started_at) < last.finalized_at + timedelta(microseconds=delay):
                    raise LineRuntimeRefusal(
                        "playbill.line.retry_backoff_pending",
                        "Retry requested before the bounded backoff elapsed.",
                    )

    def _append(
        self,
        event_kind: Literal["occurrence_materialized", "attempt_started", "attempt_finalized"],
        *,
        payload: object,
        actor_context: GovernedActorContext,
        recorded_at: datetime,
        procedure_artifact_digest: str,
        definition_digest: str,
        line_spec_digest: str,
        occurrence_id: str,
        attempt: int | None = None,
    ) -> StoredProcedureJournalRecordV1:
        try:
            stored = self._writer.append(
                stream=self.deployment.journal_binding.logical_stream,
                partition_id=self.control_partition_id,
                event_kind=event_kind,
                accepted_coordinate=self.accepted_coordinate,
                procedure_artifact_digest=procedure_artifact_digest,
                definition_digest=definition_digest,
                actor_context=actor_context,
                recorded_at=recorded_at,
                payload=payload,
                line_spec_digest=line_spec_digest,
                occurrence_id=occurrence_id,
                attempt=attempt,
            )
        except PlaybillJournalError as exc:
            raise LineRuntimeRefusal(
                "playbill.line.journal_append_refused",
                f"Line control journal refused the append: {exc}",
            ) from exc
        # The cache tracks the record just written; a rebuild must reach the same state.
        self.index.apply_record(stored, payload=normalize_canonical(payload))
        return stored


__all__ = [
    "IndexedLineAttemptV1",
    "IndexedLineOccurrenceV1",
    "LineAttemptStatusV1",
    "LineDispatchRulesV1",
    "LineOccurrenceIndex",
    "LineScheduler",
    "ResolvedCadenceTriggerV1",
    "ResolvedCaptureLandingTriggerV1",
    "ResolvedManualTriggerV1",
    "ResolvedTriggerV1",
    "ResolvedWindowCloseTriggerV1",
    "bind_resolved_trigger",
    "cadence_occurrence_time",
    "derive_cadence_occurrences",
    "derive_landing_occurrences",
    "derive_manual_occurrences",
    "derive_window_close_occurrences",
]
