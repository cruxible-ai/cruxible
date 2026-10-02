"""Operational reads for ``get`` and ``orient``: Lines, Captures, predictions, mandates.

Every card and row here is built from an index -- the accepted projection, the
Line dispatch projection, the Procedure-run journal index, or a worker's
findings -- and each list is cut to a fixed bound, so a card stays small on an
instance with thousands of occurrences, captures or runs.

Accepted artifacts are read at the requested coordinate. Operational state
(arms, pending occurrences, runs, bound prediction windows, capture
availability) has no history: it is always read as of now at the current head,
whatever coordinate the read names, and every answer that carries it says so
with a ``live`` marker naming that head (``live_view``).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from cruxible_client.contracts.captures import parse_capture_envelope
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ExactContentClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.get_reads import summary_value
from cruxible_client.contracts.operational_reads import (
    CAPTURE_HANDLE_HEX,
    LINE_CARD_ARMS,
    LINE_CARD_RUNS,
    OPERATIONAL_CARD_LIST_LIMIT,
    PlaybillGetCaptureCardV1,
    PlaybillGetLineArmV1,
    PlaybillGetLineCardV1,
    PlaybillGetLineOccurrenceV1,
    PlaybillGetMandateCardV1,
    PlaybillGetPredictionWindowV1,
    PlaybillGetResolutionContractCardV1,
    PlaybillLineArmState,
    PlaybillLiveHeadV1,
    PlaybillLiveViewV1,
    PlaybillMandateState,
    PlaybillOrientCaptureContractV1,
    PlaybillOrientCaptureV1,
    PlaybillOrientLineV1,
    PlaybillOrientMandateV1,
    PlaybillOrientPredictionV1,
    capture_handle,
)
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateV1,
    ProcedureMandateV2,
    mandate_grant,
)
from cruxible_client.contracts.procedures.line_specs import (
    CadenceTriggerPolicyV1,
    CaptureLandingTriggerPolicyV1,
    CaptureLandingTriggerPolicyV2,
    LineSpecAny,
    LineSpecV6,
    WindowCloseTriggerPolicyV1,
    WindowCloseTriggerPolicyV2,
    line_identity_digest,
    line_requested_rung,
)
from cruxible_client.contracts.procedures.models import RUNG_AUTHORITY
from cruxible_client.contracts.procedures.windows import CaptureEventWindowV1, FixedWindowV1
from cruxible_client.contracts.resolution_contracts import ResolutionContractV1
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_client.contracts.triggers import (
    CadenceScheduleV1,
    CaptureLandingScheduleV1,
    CronScheduleV1,
    GenerationAcceptedScheduleV1,
    TriggerFormatError,
    TriggerScheduleV1,
    TriggerV1,
    WindowCloseScheduleV1,
)
from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.operational_viewer import (
    OperationalViewer,
    arm_principal_kind,
    may_see_arming,
)
from cruxible_core.service.discovery.runs import run_counts, run_rows
from cruxible_core.storage.cas import BodyAccessContext

_SERVICE_ACCESS = BodyAccessContext(principal_id="playbill-operational-reads", can_read_body=True)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
#: How far ahead a mandate's expiry reads as ``expiring``: next's default lead time.
MANDATE_EXPIRING_WITHIN = timedelta(days=7)
_CAPTURE_HANDLE = re.compile(rf"^CAP-(?P<hex>[0-9a-f]{{{CAPTURE_HANDLE_HEX},64}})$")
_CAPTURE_DIGEST = re.compile(r"^(?:sha256:)?(?P<hex>[0-9a-f]{12,64})$")
_LINE_DIGEST = re.compile(r"^sha256:[0-9a-f]{12,64}$")

#: A ``get`` call spelled for the caller's surface: (ref, detail) -> call.
RenderGet = Callable[[str, str | None], str]


def live_view(instance: PlaybillInstance, fields: tuple[str, ...]) -> PlaybillLiveViewV1:
    """The marker an answer carries for the parts of it read live, at the current head."""

    head = instance.accepted_coordinate()
    generation = next(
        item.sequence for item in reversed(instance.accepted_history()) if item.oid == head.git_oid
    )
    return PlaybillLiveViewV1(
        as_of=PlaybillLiveHeadV1(git_oid=head.git_oid[:12], generation=generation),
        fields=fields,
    )


#: What each card reads live.
LIVE_CARD_FIELDS: dict[str, tuple[str, ...]] = {
    "line": ("arms", "arms_total", "due", "waiting", "occurrences", "recent_runs", "runs_total"),
    "resolution_contract": ("state", "windows", "windows_total"),
    "capture": ("status", "status_detail"),
    "procedure_run": ("card",),
}


def _instant(value: str) -> datetime:
    parsed = parse_datetime(value)
    assert parsed is not None
    return parsed


def _from_microseconds(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _short(digest: str) -> str:
    return digest.removeprefix("sha256:")[:12]


# -- references ------------------------------------------------------------------------


def capture_hex(value: str) -> str | None:
    """The hex digits a Capture reference names (``CAP-<hex>`` or a digest), or ``None``."""

    handle = _CAPTURE_HANDLE.fullmatch(value)
    if handle is not None:
        return handle["hex"]
    digest = _CAPTURE_DIGEST.fullmatch(value)
    return None if digest is None else digest["hex"]


def is_line_digest(value: str) -> bool:
    return _LINE_DIGEST.fullmatch(value) is not None


def lines_with_digest(connection: sqlite3.Connection, value: str, *, limit: int) -> tuple[str, ...]:
    """The accepted Lines whose identity digest is (or starts with) ``value``."""

    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT identity FROM lines WHERE identity_digest>=? AND identity_digest<? "
            "ORDER BY identity_digest LIMIT ?",
            (value, value + "￿", limit),
        )
    )


def captures_with_prefix(
    connection: sqlite3.Connection, hex_prefix: str, *, limit: int
) -> tuple[str, ...]:
    """Accepted (cited) Capture digests starting with ``hex_prefix``, in digest order."""

    lower = "sha256:" + hex_prefix
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT capture_digest FROM captures WHERE capture_digest>=? AND capture_digest<? "
            "ORDER BY capture_digest LIMIT ?",
            (lower, lower + "￿", limit),
        )
    )


def uncited_capture_present(instance: PlaybillInstance, digest: str) -> bool:
    """Whether a full Capture digest no accepted Claim cites is retained as a Capture."""

    try:
        if not instance.body_store().metadata(digest, access=_SERVICE_ACCESS).present:
            return False
        parse_capture_envelope(instance.body_store().read(digest, access=_SERVICE_ACCESS))
    except (PlaybillError, ValueError):
        return False
    return True


# -- Lines -----------------------------------------------------------------------------


def trigger_summary(line: LineSpecAny, triggers: tuple[TriggerV1, ...]) -> tuple[str, str | None]:
    """A Line's trigger kind, and one line saying when it fires.

    A v6 Line embeds no trigger: the live Triggers aimed at it say when it
    fires (``triggers``, in identity order), and with none it runs only when
    run explicitly. An older Line answers from the trigger it embeds.
    """

    if isinstance(line, LineSpecV6):
        if not triggers:
            return "manual", "runs only when run explicitly"
        kinds = sorted({item.schedule.kind for item in triggers})
        return "+".join(kinds), "; ".join(
            f"{item.identity.qualified} fires {schedule_summary(item.schedule)}"
            for item in triggers
        )
    trigger = line.trigger_policy
    if isinstance(trigger, CadenceTriggerPolicyV1):
        return trigger.kind, f"every {trigger.interval_seconds}s"
    if isinstance(trigger, CaptureLandingTriggerPolicyV2):
        return trigger.kind, f"when {trigger.event.capture_contract_identity.qualified} lands"
    if isinstance(trigger, CaptureLandingTriggerPolicyV1):
        return trigger.kind, (
            f"when a Capture of contract {_short(trigger.anchor_capture_contract_digest)} lands"
        )
    if isinstance(trigger, WindowCloseTriggerPolicyV2):
        return trigger.kind, "when " + window_summary(trigger.window) + " closes"
    if isinstance(trigger, WindowCloseTriggerPolicyV1):
        return trigger.kind, f"when a {trigger.window_seconds}s window closes"
    return trigger.kind, None


def schedule_summary(schedule: TriggerScheduleV1) -> str:
    """One phrase saying when a Trigger schedule fires."""

    if isinstance(schedule, CadenceScheduleV1):
        return f"every {schedule.interval_seconds}s"
    if isinstance(schedule, CronScheduleV1):
        return f"on cron {schedule.expression} (UTC)"
    if isinstance(schedule, CaptureLandingScheduleV1):
        return f"when {schedule.event.capture_contract_identity.qualified} lands"
    if isinstance(schedule, WindowCloseScheduleV1):
        return "when " + window_summary(schedule.window) + " closes"
    if isinstance(schedule, GenerationAcceptedScheduleV1):
        return "when a new generation is accepted"
    raise TriggerFormatError(f"unsupported Trigger schedule kind {schedule.kind!r}")


def aimed_triggers(projection: Any, lines: tuple[str, ...]) -> dict[str, tuple[TriggerV1, ...]]:
    """The live Triggers aimed at each named Line in a bound projection, in identity order."""

    if not lines:
        return {}
    marks = ",".join("?" * len(lines))
    found: dict[str, list[TriggerV1]] = {}
    for identity, target in projection.typed.connection.execute(
        "SELECT identity,target FROM triggers WHERE target_kind='line' AND lifecycle='live' "
        f"AND target IN ({marks}) ORDER BY identity",
        lines,
    ):
        found.setdefault(str(target), []).append(
            cast(TriggerV1, projection.typed.source(str(identity)))
        )
    return {line: tuple(items) for line, items in found.items()}


def window_summary(window: object) -> str:
    if isinstance(window, FixedWindowV1):
        return (
            f"the fixed window from {format_datetime(window.starts_at)} "
            f"({window.duration_seconds}s)"
        )
    if isinstance(window, CaptureEventWindowV1):
        return (
            f"the {window.duration_seconds}s window after each "
            f"{window.event.capture_contract_identity.qualified} Capture"
        )
    return "its window"


def line_authority(line: LineSpecAny) -> Literal["observe", "propose", "settle"]:
    return RUNG_AUTHORITY[line_requested_rung(line)]


def _line_stall_after() -> timedelta:
    from cruxible_core.consumers.lines import LINE_STALL_AFTER

    return LINE_STALL_AFTER


@dataclass(frozen=True)
class LineOperations:
    """What the Line dispatch projection holds for one Line, as of ``now``."""

    arms: tuple[PlaybillGetLineArmV1, ...] = ()
    arms_total: int = 0
    due: int = 0
    waiting: int = 0
    occurrences: tuple[PlaybillGetLineOccurrenceV1, ...] = ()

    @property
    def arm_state(self) -> PlaybillLineArmState | None:
        return self.arms[0].state if self.arms else None


def _arm(
    store: LineDispatchStore,
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    now: datetime,
    viewer: OperationalViewer | None,
) -> PlaybillGetLineArmV1:
    active = data["stops_at"] is None
    automatic = (
        int(
            conn.execute(
                "SELECT count(*) FROM pending WHERE session_id=? AND disposition='pending'",
                (data["session_id"],),
            ).fetchone()[0]
        )
        if active
        else 0
    )
    total = int(
        conn.execute(
            "SELECT count(*) FROM pending WHERE line_id=? AND disposition='pending'",
            (data["line_id"],),
        ).fetchone()[0]
    )
    view = store.arm_view(data, pending_automatic=automatic, pending_explicit=total - automatic)
    state: PlaybillLineArmState
    if active:
        oldest = conn.execute(
            "SELECT min(eligible_at) FROM pending WHERE session_id=? AND disposition='pending'",
            (data["session_id"],),
        ).fetchone()[0]
        stalled = oldest is not None and _instant(oldest) <= now - _line_stall_after()
        state = "stalled" if stalled else "running"
    else:
        state = "disarmed" if view.stop_reason in {None, "disarmed"} else "stopped"
    visible = may_see_arming(viewer, view.armed_by)
    return PlaybillGetLineArmV1(
        arm=view.arm_id,
        state=state,
        principal_kind=arm_principal_kind(data["armed_by"], view.armed_by),
        armed_by=view.armed_by.label if visible else None,
        credential=view.armed_by.credential_id if visible else None,
        armed_by_withheld=not visible,
        armed_at=view.armed_at,
        stopped_at=view.stopped_at,
        stop_reason=view.stop_reason,
        detail=view.detail,
        pending_automatic=view.pending_automatic,
        pending_explicit=view.pending_explicit,
    )


def line_operations(
    instance: PlaybillInstance,
    line_digest: str,
    *,
    now: datetime,
    arm_limit: int = LINE_CARD_ARMS,
    occurrence_limit: int = OPERATIONAL_CARD_LIST_LIMIT,
    viewer: OperationalViewer | None = None,
) -> LineOperations:
    """One Line's arms (latest first) and pending occurrences, each list bounded.

    Nothing is created: an instance that never evaluated a Line keeps no
    dispatch state and has none to report.
    """

    if not dispatch_root(instance).exists():
        return LineOperations()
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        return _line_operations(
            store,
            conn,
            line_digest,
            now=now,
            arm_limit=arm_limit,
            occurrence_limit=occurrence_limit,
            viewer=viewer,
        )


def _line_operations(
    store: LineDispatchStore,
    conn: sqlite3.Connection,
    line_digest: str,
    *,
    now: datetime,
    arm_limit: int,
    occurrence_limit: int,
    viewer: OperationalViewer | None = None,
) -> LineOperations:
    stamp = str(format_datetime(now))
    arms_total = int(
        conn.execute(
            "SELECT count(DISTINCT json_extract(payload,'$.arm_id')) FROM sessions WHERE line_id=?",
            (line_digest,),
        ).fetchone()[0]
    )
    latest: dict[str, dict[str, Any]] = {}
    for (payload,) in conn.execute(
        "SELECT payload FROM sessions WHERE line_id=? ORDER BY rowid DESC", (line_digest,)
    ):
        data = json.loads(payload)
        latest.setdefault(str(data["arm_id"]), data)
        if len(latest) >= arm_limit:
            break
    arms = tuple(_arm(store, conn, data, now=now, viewer=viewer) for data in latest.values())
    due, pending = conn.execute(
        "SELECT coalesce(sum(eligible_at<=?),0), count(*) FROM pending "
        "WHERE line_id=? AND disposition='pending'",
        (stamp, line_digest),
    ).fetchone()
    occurrences = tuple(
        PlaybillGetLineOccurrenceV1(
            occurrence=str(occurrence_id),
            eligible_at=_instant(str(eligible_at)),
            state="due" if str(eligible_at) <= stamp else "waiting",
        )
        for occurrence_id, eligible_at in conn.execute(
            "SELECT occurrence_id, eligible_at FROM pending "
            "WHERE line_id=? AND disposition='pending' "
            "ORDER BY eligible_at, occurrence_id LIMIT ?",
            (line_digest, occurrence_limit),
        )
    )
    return LineOperations(
        arms=arms,
        arms_total=arms_total,
        due=int(due),
        waiting=int(pending) - int(due),
        occurrences=occurrences,
    )


def line_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    identity: str,
    *,
    evaluation_time: datetime,
    render: RenderGet,
    viewer: OperationalViewer | None = None,
) -> PlaybillGetLineCardV1:
    with instance.bind_accepted_projection(coordinate) as projection:
        line = cast(LineSpecAny, projection.typed.source(identity))
        triggers = aimed_triggers(projection, (line.identity.qualified,))
    digest = line_identity_digest(line.identity)
    trigger, detail = trigger_summary(line, triggers.get(line.identity.qualified, ()))
    procedure = line.procedure.target.qualified
    next_steps = [render(procedure, None)]
    operations = line_operations(instance, digest, now=evaluation_time, viewer=viewer)
    runs, _more = run_rows(instance, limit=LINE_CARD_RUNS, line=line.identity)
    total, _running = run_counts(instance, line=line.identity)
    fields: dict[str, Any] = dict(
        arms=operations.arms,
        arms_total=operations.arms_total,
        due=operations.due,
        waiting=operations.waiting,
        occurrences=operations.occurrences,
        recent_runs=runs,
        runs_total=total,
    )
    next_steps.extend(render(f"ProcedureRun:{row.run}", None) for row in runs[:1])
    next_steps.append(render(line.identity.qualified, "history"))
    return PlaybillGetLineCardV1(
        line=line.identity.qualified,
        identity_digest=digest,
        lifecycle=line.lifecycle.state,
        procedure=procedure,
        authority=line_authority(line),
        trigger=trigger,
        trigger_detail=detail,
        occurrence_epoch=line.occurrence_epoch,
        next=tuple(next_steps),
        **fields,
    )


def line_rows(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    evaluation_time: datetime,
) -> tuple[PlaybillOrientLineV1, ...]:
    """Every accepted Line as a compact row, with its live arm state and pending counts."""

    with instance.bind_accepted_projection(coordinate) as projection:
        lines = [
            cast(LineSpecAny, projection.typed.source(str(identity)))
            for (identity,) in projection.typed.connection.execute(
                "SELECT identity FROM lines ORDER BY identity"
            )
        ]
        triggers = aimed_triggers(projection, tuple(line.identity.qualified for line in lines))
    operations_by_line: dict[str, LineOperations] = {}
    if lines and dispatch_root(instance).exists():
        # One store session for every Line, not one replay per row.
        store = LineDispatchStore(instance)
        with store.locked() as conn:
            for line in lines:
                operations_by_line[line.identity.qualified] = _line_operations(
                    store,
                    conn,
                    line_identity_digest(line.identity),
                    now=evaluation_time,
                    arm_limit=1,
                    occurrence_limit=0,
                )
    rows: list[PlaybillOrientLineV1] = []
    for line in lines:
        operations = operations_by_line.get(line.identity.qualified, LineOperations())
        rows.append(
            PlaybillOrientLineV1(
                line=line.identity.qualified,
                lifecycle="retired" if line.lifecycle.state == "retired" else "live",
                procedure=line.procedure.target.qualified,
                authority=line_authority(line),
                trigger=trigger_summary(line, triggers.get(line.identity.qualified, ()))[0],
                arm=operations.arm_state,
                due=operations.due,
                waiting=operations.waiting,
            )
        )
    return tuple(rows)


# -- Captures ----------------------------------------------------------------------------


def _availability(
    instance: PlaybillInstance, digest: str
) -> tuple[Literal["available", "unavailable"], str | None]:
    """Whether the store can still produce a Capture, as the evidence worker last saw."""

    from cruxible_core.consumers.next.evidence import evidence_findings

    found = [item for item in evidence_findings(instance) if item.capture_digest == digest]
    if found:
        return "unavailable", "; ".join(
            f"{item.part} {item.state} (checked {format_datetime(item.checked_at)})"
            for item in found
        )
    if not instance.body_store().metadata(digest, access=_SERVICE_ACCESS).present:
        return "unavailable", "envelope missing from the store"
    return "available", None


def capture_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    digest: str,
    *,
    render: RenderGet,
    read_capture: Callable[[str], str],
) -> PlaybillGetCaptureCardV1:
    """One Capture: cited ones from the accepted index, an uncited one from its envelope."""

    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        names = CaptureContractNames(instance, coordinate, connection=connection)
        row = connection.execute(
            "SELECT contract_digest, commitment_byte_length_decimal, logical_source_id, "
            "observed_at_us, access_class FROM captures WHERE capture_digest=?",
            (digest,),
        ).fetchone()
        citing_total = int(
            connection.execute(
                "SELECT count(DISTINCT u.owner_key) FROM citation_uses u "
                "JOIN claims c ON c.identity=u.owner_key "
                "WHERE u.owner_kind='Claim' AND u.capture_digest=? AND c.lifecycle='live'",
                (digest,),
            ).fetchone()[0]
        )
        citing = tuple(
            str(owner).removeprefix("Claim:")
            for (owner,) in connection.execute(
                "SELECT DISTINCT u.owner_key FROM citation_uses u "
                "JOIN claims c ON c.identity=u.owner_key "
                "WHERE u.owner_kind='Claim' AND u.capture_digest=? AND c.lifecycle='live' "
                "ORDER BY u.owner_key LIMIT ?",
                (digest, OPERATIONAL_CARD_LIST_LIMIT),
            )
        )
        subjects = tuple(
            _subject_ref(str(path))
            for (path,) in connection.execute(
                "SELECT DISTINCT c.subject_path FROM citation_uses u "
                "JOIN claims c ON c.identity=u.owner_key "
                "WHERE u.owner_kind='Claim' AND u.capture_digest=? AND c.lifecycle='live' "
                "ORDER BY c.subject_path LIMIT ?",
                (digest, OPERATIONAL_CARD_LIST_LIMIT),
            )
        )
        if row is not None:
            contract_digest, length, source, observed_us, access = row
            observed_at = _from_microseconds(int(observed_us))
            size = None if length is None else int(length)
        else:
            envelope = parse_capture_envelope(
                instance.body_store().read(digest, access=_SERVICE_ACCESS)
            )
            contract_digest = envelope.capture_contract_digest
            source = getattr(envelope.source, "source_identity", None)
            observed_at = envelope.observed_at
            size, access = None, None
        contract = names.name(contract_digest, qualified=True)
        version = (
            None
            if contract.startswith("unresolved:")
            else names.version_number(contract, contract_digest)
        )
    status, status_detail = _availability(instance, digest)
    next_steps = [render(claim, None) for claim in citing[:1]]
    next_steps.append(read_capture(digest))
    return PlaybillGetCaptureCardV1(
        capture=capture_handle(digest),
        digest=digest,
        contract=contract,
        version=version,
        source=None if source is None else str(source),
        observed_at=observed_at,
        size=size,
        access=None if access is None else str(access),
        status=status,
        status_detail=status_detail,
        subjects=subjects,
        citing=citing,
        citing_total=citing_total,
        next=tuple(next_steps),
    )


def _subject_ref(path: str) -> str:
    if path.startswith("subjects/") and path.endswith(".json"):
        return path[len("subjects/") : -len(".json")]
    return path


CaptureKey = tuple[str, str]


def capture_rows(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    limit: int,
    after: CaptureKey | None,
) -> tuple[tuple[PlaybillOrientCaptureV1, ...], CaptureKey | None]:
    """Cited Captures, newest observation first, one keyset page and where it stopped."""

    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        names = CaptureContractNames(instance, coordinate, connection=connection)
        where, args = "", list[object]([limit + 1])
        if after is not None:
            where = (
                " WHERE (c.observed_at_us < ? OR (c.observed_at_us = ? AND c.capture_digest > ?))"
            )
            args = [int(after[0]), int(after[0]), after[1], limit + 1]
        rows = connection.execute(
            "SELECT c.capture_digest, c.contract_digest, c.observed_at_us, "
            "(SELECT count(DISTINCT u.owner_key) FROM citation_uses u "
            "JOIN claims cl ON cl.identity=u.owner_key WHERE u.owner_kind='Claim' "
            "AND u.capture_digest=c.capture_digest AND cl.lifecycle='live') "
            "FROM captures c" + where + " ORDER BY c.observed_at_us DESC, c.capture_digest ASC "
            "LIMIT ?",
            args,
        ).fetchall()
        page = [
            PlaybillOrientCaptureV1(
                capture=capture_handle(str(digest)),
                contract=names.name(str(contract_digest), qualified=True),
                observed_at=_from_microseconds(int(observed_us)),
                citing=int(citing),
            )
            for digest, contract_digest, observed_us, citing in rows[:limit]
        ]
    more = len(rows) > limit
    last = rows[limit - 1] if more and limit else None
    return tuple(page), (None if last is None else (str(last[2]), str(last[0])))


def capture_count(instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate) -> int:
    with instance.bind_accepted_projection(coordinate) as projection:
        return int(
            projection.typed.connection.execute("SELECT count(*) FROM captures").fetchone()[0]
        )


def capture_contract_rows(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> tuple[PlaybillOrientCaptureContractV1, ...]:
    """Every accepted CaptureContract, with its version and how many ClaimTypes admit it."""

    from cruxible_client.contracts.captures import CaptureContractV1
    from cruxible_client.contracts.claim_types import ClaimType

    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        names = CaptureContractNames(instance, coordinate, connection=connection)
        contracts = [
            (str(identity), str(digest), projection.typed.source(str(identity)))
            for identity, digest in connection.execute(
                "SELECT identity, artifact_digest FROM capture_contracts ORDER BY identity"
            )
        ]
        claim_types = [
            cast(ClaimType, projection.typed.source(str(identity)))
            for (identity,) in connection.execute(
                "SELECT identity FROM claim_types WHERE lifecycle='live' ORDER BY identity"
            )
        ]
        admitted = [set(names.admitted(item, qualified=True)) for item in claim_types]
    rows: list[PlaybillOrientCaptureContractV1] = []
    for identity, digest, contract in contracts:
        if not isinstance(contract, CaptureContractV1):
            continue
        rows.append(
            PlaybillOrientCaptureContractV1(
                contract=identity,
                version=names.version_number(identity, digest),
                lifecycle=contract.lifecycle.state,
                grade=contract.epistemic_grade,
                admitted_by=sum(identity in names_ for names_ in admitted),
            )
        )
    return tuple(rows)


# -- ResolutionContracts and their windows -------------------------------------------------


def _claim_value(claim: ClaimArtifactAny) -> object:
    obj = claim.statement.object
    if isinstance(obj, SubjectClaimObject):
        return _subject_ref(obj.address.artifact_path)
    if isinstance(obj, ExactContentClaimObject):
        return obj.content_digest
    return obj.value


def _prediction_state(
    counts: dict[str, int] | None,
) -> Literal["open", "settleable", "resolved", "unbound", "not_observed"]:
    if counts is None:
        return "not_observed"
    if not any(counts.values()):
        return "unbound"
    if counts.get("settleable"):
        return "settleable"
    if counts.get("open"):
        return "open"
    return "resolved"


def resolution_contract_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    identity: str,
    *,
    evaluation_time: datetime,
    render: RenderGet,
) -> PlaybillGetResolutionContractCardV1:
    from cruxible_core.consumers.next.predictions import contract_windows

    with instance.bind_accepted_projection(coordinate) as projection:
        contract = cast(ResolutionContractV1, projection.typed.source(identity))
        (rule_kind,) = projection.typed.connection.execute(
            "SELECT rule_kind FROM resolution_contracts WHERE identity=?", (identity,)
        ).fetchone()
        hypothesis = projection.typed.source(contract.hypothesis.identity.qualified)
    claim_id = contract.hypothesis.identity.name
    fields: dict[str, Any] = {}
    counts: dict[str, int] | None = None
    found = contract_windows(
        instance, identity, limit=OPERATIONAL_CARD_LIST_LIMIT, evaluation_time=evaluation_time
    )
    if found is not None:
        windows, counts = found
        fields["windows"] = tuple(
            PlaybillGetPredictionWindowV1(
                window=window_id,
                starts_at=window.starts_at,
                ends_at=window.ends_at,
                status=cast(Any, status),
            )
            for window_id, window, status in windows
        )
        fields["windows_total"] = sum(counts.values())
    return PlaybillGetResolutionContractCardV1(
        contract=identity,
        lifecycle=contract.lifecycle.state,
        hypothesis=claim_id,
        hypothesis_value=(
            None
            if hypothesis is None
            else summary_value(_claim_value(cast(ClaimArtifactAny, hypothesis)))
        ),
        window=window_summary(contract.window),
        rule=str(rule_kind),
        state=_prediction_state(counts),
        next=(render(claim_id, None), render(identity, "history")),
        **fields,
    )


def prediction_rows(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    evaluation_time: datetime,
) -> tuple[PlaybillOrientPredictionV1, ...]:
    """Every live ResolutionContract with its bound windows counted by status."""

    from cruxible_core.consumers.next.predictions import window_tallies

    with instance.bind_accepted_projection(coordinate) as projection:
        contracts = [
            cast(ResolutionContractV1, projection.typed.source(str(identity)))
            for (identity,) in projection.typed.connection.execute(
                "SELECT identity FROM resolution_contracts WHERE lifecycle='live' ORDER BY identity"
            )
        ]
    tallies = window_tallies(
        instance,
        [item.identity.qualified for item in contracts],
        evaluation_time=evaluation_time,
    )
    rows: list[PlaybillOrientPredictionV1] = []
    for contract in contracts:
        tally = tallies.get(contract.identity.qualified)
        rows.append(
            PlaybillOrientPredictionV1(
                contract=contract.identity.qualified,
                hypothesis=contract.hypothesis.identity.name,
                window=contract.window.kind,
                open=0 if tally is None else tally.open,
                settleable=0 if tally is None else tally.settleable,
                resolved=0 if tally is None else tally.resolved,
                next_close=None if tally is None else tally.next_close,
            )
        )
    return tuple(rows)


# -- mandates ----------------------------------------------------------------------------


def mandate_state(
    mandate: ProcedureMandateV1 | ProcedureMandateV2, *, now: datetime
) -> PlaybillMandateState:
    if mandate.lifecycle.state == "retired":
        return "retired"
    if isinstance(mandate, ProcedureMandateV2) and mandate.suspended:
        return "suspended"
    if now < mandate.valid_from:
        return "not_yet_valid"
    if now >= mandate.expires_at:
        return "expired"
    if mandate.expires_at <= now + MANDATE_EXPIRING_WITHIN:
        return "expiring"
    return "active"


def mandate_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    identity: str,
    *,
    evaluation_time: datetime,
    render: RenderGet,
) -> PlaybillGetMandateCardV1:
    with instance.bind_accepted_projection(coordinate) as projection:
        mandate = cast(ProcedureMandateV1 | ProcedureMandateV2, projection.typed.source(identity))
    procedure = mandate.procedure.target.qualified
    return PlaybillGetMandateCardV1(
        mandate=identity,
        procedure=procedure,
        grants=mandate_grant(mandate),
        lifecycle=mandate.lifecycle.state,
        state=mandate_state(mandate, now=evaluation_time),
        valid_from=mandate.valid_from,
        expires_at=mandate.expires_at,
        namespace=mandate.namespace,
        next=(render(procedure, None), render(identity, "history")),
    )


def mandate_rows(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    evaluation_time: datetime,
) -> tuple[PlaybillOrientMandateV1, ...]:
    with instance.bind_accepted_projection(coordinate) as projection:
        mandates = [
            cast(ProcedureMandateV1 | ProcedureMandateV2, projection.typed.source(str(identity)))
            for (identity,) in projection.typed.connection.execute(
                "SELECT identity FROM procedure_mandates ORDER BY identity"
            )
        ]
    return tuple(
        PlaybillOrientMandateV1(
            mandate=mandate.identity.qualified,
            procedure=mandate.procedure.target.qualified,
            grants=mandate_grant(mandate),
            state=mandate_state(mandate, now=evaluation_time),
            expires_at=mandate.expires_at,
        )
        for mandate in mandates
    )


__all__ = [
    "LIVE_CARD_FIELDS",
    "LineOperations",
    "OperationalViewer",
    "aimed_triggers",
    "capture_card",
    "capture_contract_rows",
    "capture_count",
    "capture_hex",
    "capture_rows",
    "captures_with_prefix",
    "is_line_digest",
    "line_card",
    "line_operations",
    "line_rows",
    "live_view",
    "lines_with_digest",
    "mandate_card",
    "mandate_rows",
    "mandate_state",
    "may_see_arming",
    "prediction_rows",
    "resolution_contract_card",
    "schedule_summary",
    "trigger_summary",
    "uncited_capture_present",
    "window_summary",
]
