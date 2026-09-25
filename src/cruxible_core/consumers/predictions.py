"""Prediction settlement as a consumer kind: resuming, findings only.

A ResolutionContract tests its hypothesis over a bound observation window. A
fixed window is bound when the contract is accepted. An event window is bound
once per landed Capture its selector matches, and each bound window is its own
contract instance with its own resolution journal. Once a window closes with
no current answer in that journal, the prediction is settleable. It stays so
until a settlement lands, and is settleable again if that answer is overturned.

The worker follows three logs: accepted generations, for contracts accepted,
revised, or retired; the capture landing index, for event anchors; and the
resolution journal, for settlements and overturns. The shared clock closes
windows. On a new instance it reads every live contract and every retained
Capture each selector matches, because a prediction made before the worker ran
is still owed. An anchor whose material is gone cannot bind a window, and that
is a finding, never a silent skip.

The findings are a disposable projection of those logs: deleting them costs
only a rebuild. Nothing here is governed, so the worker needs no authority and
a restart resumes from its cursors.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.procedures.windows import (
    BoundObservationWindowV1,
    CaptureEventWindowV1,
    TriggerEventReferenceV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.resolution_contracts import (
    InvestigationBindingV1,
    ResolutionContractReferenceV1,
    ResolutionContractV1,
)
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.consumers.protocol import (
    ConsumerHealth,
    ConsumerRepair,
    ConsumerWork,
    CursorPolicy,
    EffectClass,
)
from cruxible_core.server.config import get_disabled_consumers

if TYPE_CHECKING:
    from cruxible_core.procedures.resolution import ResolutionContractActivationV3

#: Generations one matching pass reads before yielding.
GENERATION_BATCH = 64
#: Resolution journal records one matching pass reads before yielding.
EVENT_BATCH = 1024
#: Contracts one unit of work loads, or scans the capture index for.
CONTRACT_BATCH = 64
#: Landed Captures one contract's scan binds per page.
CAPTURE_PAGE = 256
#: Bound windows one unit of work checks against their resolution journals.
CHECK_BATCH = 256
#: How long an unbindable anchor waits before its material is looked for again.
UNBINDABLE_RETRY = timedelta(hours=1)

_PREFIX = "resolutions:"
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 generation INTEGER NOT NULL, backfill_after TEXT, index_generation TEXT,
 capture_head INTEGER NOT NULL DEFAULT 0, resolution_ordinal INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, last_error_at TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS pending (identity TEXT PRIMARY KEY) STRICT;
CREATE TABLE IF NOT EXISTS contracts (
 identity TEXT PRIMARY KEY, artifact_digest TEXT NOT NULL, hypothesis TEXT NOT NULL,
 reference TEXT NOT NULL, contract TEXT NOT NULL, accepted_at TEXT NOT NULL,
 selector_digest TEXT, capture_generation TEXT, capture_ordinal INTEGER NOT NULL DEFAULT 0,
 scan_through INTEGER, scan_cursor TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS contracts_by_capture
 ON contracts(capture_ordinal) WHERE selector_digest IS NOT NULL;
CREATE TABLE IF NOT EXISTS windows (
 contract_id TEXT PRIMARY KEY, identity TEXT NOT NULL, window TEXT NOT NULL,
 ends_at_us INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','settleable','resolved')),
 dirty TEXT, checked_at TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS windows_by_status ON windows(status,ends_at_us);
CREATE INDEX IF NOT EXISTS windows_by_identity ON windows(identity);
CREATE INDEX IF NOT EXISTS windows_dirty ON windows(contract_id) WHERE dirty IS NOT NULL;
CREATE TABLE IF NOT EXISTS unbindable (
 identity TEXT NOT NULL, record_digest TEXT NOT NULL, event TEXT NOT NULL,
 code TEXT NOT NULL, checked_at TEXT NOT NULL, checked_at_us INTEGER NOT NULL,
 PRIMARY KEY(identity, record_digest)
) STRICT;
CREATE INDEX IF NOT EXISTS unbindable_by_check ON unbindable(checked_at_us);
"""


@dataclass(frozen=True)
class SettleableWindow:
    """A closed bound window whose resolution journal holds no current answer."""

    contract: ResolutionContractReferenceV1
    hypothesis: str
    bound_contract_id: str
    window: BoundObservationWindowV1
    checked_at: datetime


@dataclass(frozen=True)
class UnbindableAnchor:
    """A matching landed Capture whose retained material no longer binds a window."""

    contract: ResolutionContractReferenceV1
    hypothesis: str
    event: TriggerEventReferenceV1
    code: str
    checked_at: datetime


@dataclass(frozen=True)
class _Contract:
    identity: str
    reference: ResolutionContractReferenceV1
    contract: ResolutionContractV1
    accepted_at: datetime


def _root(instance: Any) -> Path:
    exhaust = Path(instance.root) / str(instance.descriptor.storage.exhaust)
    return exhaust / "prediction-settlement"


@contextmanager
def _state(instance: Any, *, create: bool = True) -> Iterator[sqlite3.Connection | None]:
    root = _root(instance)
    if not root.exists():
        if not create:
            yield None
            return
        root.mkdir(mode=0o700, parents=True)
    path = root / "state.sqlite3"
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(path.resolve(), threading.Lock())
    with lock:
        connection = sqlite3.connect(path, timeout=30)
        try:
            connection.executescript(_SCHEMA)
            yield connection
            connection.commit()
        finally:
            connection.close()


def _instant(value: str) -> datetime:
    parsed = parse_datetime(value)
    assert parsed is not None
    return parsed


def _microseconds(value: datetime) -> int:
    from cruxible_core.indexes.typed_state import utc_microseconds

    micros = utc_microseconds(value)
    assert micros is not None
    return micros


def _contract_row(row: tuple[Any, ...]) -> _Contract:
    identity, reference, contract, accepted_at = row
    return _Contract(
        identity=identity,
        reference=ResolutionContractReferenceV1.model_validate_json(reference),
        contract=ResolutionContractV1.model_validate_json(contract),
        accepted_at=_instant(accepted_at),
    )


def _activation(
    contract: _Contract, window: BoundObservationWindowV1
) -> ResolutionContractActivationV3:
    """The exact activation a settlement of this bound window journals under."""

    from cruxible_core.procedures.resolution import build_independent_activation

    return build_independent_activation(
        contract.contract,
        InvestigationBindingV1(
            contract=contract.reference, hypothesis=contract.contract.hypothesis, window=window
        ),
        activated_at=contract.accepted_at,
    )


def _window_row(contract: _Contract, window: BoundObservationWindowV1) -> tuple[Any, ...] | None:
    # Settlement needs an observation after acceptance and inside the window,
    # so a window that closed before its contract was accepted can never be
    # settled: it is no prediction at all, not an owed one.
    if window.ends_at <= contract.accepted_at:
        return None
    return (
        _activation(contract, window).contract_id,
        contract.identity,
        window.model_dump_json(),
        _microseconds(window.ends_at),
    )


def _bind(
    instance: Any, contract: _Contract, event: TriggerEventReferenceV1, *, now: datetime
) -> BoundObservationWindowV1 | str | None:
    """The event's bound window; its refusal code when unbindable; None to retry later."""

    from cruxible_core.service.procedures.resolution_contracts import (
        TriggerCaptureRefused,
        bind_window,
    )

    try:
        return bind_window(instance, contract.contract.window, event, now=now)
    except TriggerCaptureRefused as exc:
        return None if exc.retryable else str(exc.refusal_code)


def settleable_windows(instance: Any) -> tuple[SettleableWindow, ...]:
    """The closed, unanswered windows the worker last observed; empty before it ran."""

    with _state(instance, create=False) as connection:
        if connection is None:
            return ()
        rows = connection.execute(
            "SELECT c.reference,c.hypothesis,w.contract_id,w.window,w.checked_at "
            "FROM windows w INDEXED BY windows_by_status JOIN contracts c ON c.identity=w.identity "
            "WHERE w.status='settleable' ORDER BY w.ends_at_us,w.contract_id"
        ).fetchall()
    return tuple(
        SettleableWindow(
            contract=ResolutionContractReferenceV1.model_validate_json(reference),
            hypothesis=hypothesis,
            bound_contract_id=contract_id,
            window=BoundObservationWindowV1.model_validate_json(window),
            checked_at=_instant(checked_at),
        )
        for reference, hypothesis, contract_id, window, checked_at in rows
    )


def unbindable_anchors(instance: Any) -> tuple[UnbindableAnchor, ...]:
    """Matching anchors whose material could not bind a window at the last look."""

    with _state(instance, create=False) as connection:
        if connection is None:
            return ()
        rows = connection.execute(
            "SELECT c.reference,c.hypothesis,u.event,u.code,u.checked_at "
            "FROM unbindable u JOIN contracts c ON c.identity=u.identity "
            "ORDER BY u.identity,u.record_digest"
        ).fetchall()
    return tuple(
        UnbindableAnchor(
            contract=ResolutionContractReferenceV1.model_validate_json(reference),
            hypothesis=hypothesis,
            event=TriggerEventReferenceV1.model_validate_json(event),
            code=code,
            checked_at=_instant(checked_at),
        )
        for reference, hypothesis, event, code, checked_at in rows
    )


def _journals(instance: Any) -> tuple[Any, Any, Any]:
    """The shared journal backend, its capture stream, and its resolution stream."""

    from cruxible_core.service.procedures import predictions, procedure_runs

    journal, resolutions = predictions._journal(instance)
    return journal, procedure_runs._stream(instance), resolutions


class PredictionSettlementConsumers:
    name = "prediction"
    cursor_policy: CursorPolicy = "resume"
    effect_class: EffectClass = "findings"
    workers = 1

    def active(self, instance: Any) -> bool:
        return self.name not in get_disabled_consumers()

    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        with instance.accepted_history_reader() as history:
            head = history.sequence
            with _state(instance) as connection:
                assert connection is not None
                row = connection.execute("SELECT generation FROM progress").fetchone()
            cursor = head if row is None else row[0]
            through = min(head, cursor + GENERATION_BATCH)
            contracts = {
                version.identity
                for sequence in range(cursor + 1, through + 1)
                for version in history.versions_at(sequence)
                if version.path.startswith("resolution-contracts/")
            }
        journal, captures_stream, resolution_stream = _journals(instance)
        captures = journal.index.positions(captures_stream, event_kind="produced_capture")
        resolutions = journal.index.positions(resolution_stream)
        generation = captures["generation"]
        with _state(instance) as connection:
            assert connection is not None
            if row is None:
                # A new worker reads today's live contracts, not their history;
                # none has a window yet, so no settlement before now needs reading.
                connection.execute(
                    "INSERT INTO progress(singleton,generation,backfill_after,index_generation,"
                    "capture_head,resolution_ordinal) VALUES (1,?,'',?,?,?)",
                    (head, generation, captures["ordinal"], resolutions["ordinal"]),
                )
                return
            (known, ordinal) = connection.execute(
                "SELECT index_generation,resolution_ordinal FROM progress"
            ).fetchone()
        # A rebuilt index renumbers every record: read the resolution journal
        # again from its start, and let each contract rescan its captures.
        after = {"generation": generation, "ordinal": ordinal if known == generation else 0}
        partitions, stopped = journal.index.appended_partitions(
            resolution_stream, after=after, through=resolutions, limit=EVENT_BATCH
        )
        mark = uuid4().hex
        with _state(instance) as connection:
            assert connection is not None
            connection.executemany(
                "INSERT OR IGNORE INTO pending VALUES (?)", ((identity,) for identity in contracts)
            )
            connection.executemany(
                "UPDATE windows SET dirty=? WHERE contract_id=?",
                (
                    (mark, partition.removeprefix(_PREFIX))
                    for partition in partitions
                    if partition.startswith(_PREFIX)
                ),
            )
            connection.execute(
                "UPDATE progress SET generation=?,index_generation=?,capture_head=?,"
                "resolution_ordinal=?",
                (
                    through,
                    generation,
                    captures["ordinal"],
                    (stopped or resolutions)["ordinal"],
                ),
            )

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        with _state(instance) as connection:
            assert connection is not None
            progress = connection.execute(
                "SELECT backfill_after,index_generation,capture_head FROM progress"
            ).fetchone()
            if progress is None:
                return ()
            backfill_after, generation, capture_head = progress
            contracts = (
                backfill_after is not None
                or connection.execute("SELECT 1 FROM pending LIMIT 1").fetchone()
            )
            captures = connection.execute(
                "SELECT 1 FROM contracts WHERE selector_digest IS NOT NULL AND "
                "(capture_generation IS NOT ? OR capture_ordinal<? OR scan_cursor IS NOT NULL) "
                "LIMIT 1",
                (generation, capture_head),
            ).fetchone()
            windows = (
                connection.execute(
                    "SELECT 1 FROM windows WHERE status='open' AND ends_at_us<=? LIMIT 1",
                    (_microseconds(now),),
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM windows WHERE dirty IS NOT NULL LIMIT 1"
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM unbindable WHERE checked_at_us<=? LIMIT 1",
                    (_microseconds(now - UNBINDABLE_RETRY),),
                ).fetchone()
            )
        return tuple(
            ConsumerWork(key=key, item=key)
            for key, due in (("contracts", contracts), ("captures", captures), ("windows", windows))
            if due
        )

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        instance = manager.get(instance_id)
        try:
            if work.item == "contracts":
                self._contracts(instance, now=now)
            elif work.item == "captures":
                self._captures(instance, now=now)
            else:
                self._windows(instance, now=now)
        except Exception as exc:
            with _state(instance) as connection:
                assert connection is not None
                connection.execute(
                    "UPDATE progress SET last_error=?,last_error_at=?",
                    (f"{type(exc).__name__}: {exc}", format_datetime(now)),
                )
            raise
        with _state(instance) as connection:
            assert connection is not None
            connection.execute("UPDATE progress SET last_error=NULL,last_error_at=NULL")

    def _contracts(self, instance: Any, *, now: datetime) -> None:
        """Follow accepted contracts: load a new version, drop a retired or revised one."""

        with _state(instance) as connection:
            assert connection is not None
            pending = [
                row[0]
                for row in connection.execute(
                    "SELECT identity FROM pending ORDER BY identity LIMIT ?", (CONTRACT_BATCH,)
                ).fetchall()
            ]
            (backfill_after,) = connection.execute("SELECT backfill_after FROM progress").fetchone()
        coordinate = instance.accepted_coordinate()
        live: dict[str, str | None] = {}
        backfilled: str | None = None
        with instance.bind_accepted_projection(coordinate) as projection:
            typed = projection.typed.connection
            identities = list(pending)
            if backfill_after is not None:
                page = [
                    row[0]
                    for row in typed.execute(
                        "SELECT identity FROM resolution_contracts WHERE identity>? "
                        "ORDER BY identity LIMIT ?",
                        (backfill_after, CONTRACT_BATCH),
                    ).fetchall()
                ]
                identities.extend(page)
                backfilled = page[-1] if len(page) == CONTRACT_BATCH else None
            for identity in identities:
                row = typed.execute(
                    "SELECT artifact_digest FROM resolution_contracts "
                    "WHERE identity=? AND lifecycle='live'",
                    (identity,),
                ).fetchone()
                live[identity] = None if row is None else row[0]
        with _state(instance) as connection:
            assert connection is not None
            known = dict(
                connection.execute(
                    "SELECT identity,artifact_digest FROM contracts WHERE identity IN "
                    f"({','.join('?' * len(live))})",
                    tuple(live),
                ).fetchall()
            )
        at = AcceptedCoordinate.from_internal(coordinate)
        loaded = [
            self._load(instance, identity, digest, at=at, now=now)
            for identity, digest in live.items()
            if digest is not None and known.get(identity) != digest
        ]
        # A retired contract is withdrawn, and a revised one is a new test:
        # neither leaves the old version's windows owed.
        dropped = [
            identity
            for identity, digest in live.items()
            if (digest is None and identity in known)
            or (digest is not None and known.get(identity) not in {None, digest})
        ]
        with _state(instance) as connection:
            assert connection is not None
            for identity in dropped:
                for table in ("contracts", "windows", "unbindable"):
                    connection.execute(f"DELETE FROM {table} WHERE identity=?", (identity,))
            for contract, windows in loaded:
                window = contract.contract.window
                connection.execute(
                    "INSERT INTO contracts(identity,artifact_digest,hypothesis,reference,contract,"
                    "accepted_at,selector_digest) VALUES (?,?,?,?,?,?,?)",
                    (
                        contract.identity,
                        contract.reference.artifact_digest,
                        contract.contract.hypothesis.identity.qualified,
                        contract.reference.model_dump_json(),
                        contract.contract.model_dump_json(),
                        format_datetime(contract.accepted_at),
                        window.event.capture_contract_digest
                        if isinstance(window, CaptureEventWindowV1)
                        else None,
                    ),
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO windows(contract_id,identity,window,ends_at_us,status) "
                    "VALUES (?,?,?,?,'open')",
                    windows,
                )
            connection.executemany(
                "DELETE FROM pending WHERE identity=?", ((identity,) for identity in pending)
            )
            if backfill_after is not None:
                connection.execute("UPDATE progress SET backfill_after=?", (backfilled,))

    def _load(
        self, instance: Any, identity: str, digest: str, *, at: AcceptedCoordinate, now: datetime
    ) -> tuple[_Contract, list[tuple[Any, ...]]]:
        from cruxible_core.service.procedures.resolution_contracts import (
            artifact_accepted_time,
            bind_window,
            canonical_contract_reference,
            read_resolution_contract,
        )

        kind, name = identity.split(":", 1)
        reference = canonical_contract_reference(
            instance,
            ResolutionContractReferenceV1(
                identity=ArtifactIdentity(kind=kind, name=name),
                artifact_digest=digest,
                coordinate=at,
            ),
        )
        contract = _Contract(
            identity=identity,
            reference=reference,
            contract=read_resolution_contract(instance, reference),
            accepted_at=artifact_accepted_time(instance, reference),
        )
        if isinstance(contract.contract.window, CaptureEventWindowV1):
            # Its windows wait for the anchors the capture scan finds.
            return contract, []
        row = _window_row(contract, bind_window(instance, contract.contract.window, None, now=now))
        return contract, [] if row is None else [row]

    def _captures(self, instance: Any, *, now: datetime) -> None:
        """Bind one window per landed Capture an event contract's selector matches."""

        with _state(instance) as connection:
            assert connection is not None
            generation, head = connection.execute(
                "SELECT index_generation,capture_head FROM progress"
            ).fetchone()
            rows = connection.execute(
                "SELECT identity,reference,contract,accepted_at,selector_digest,"
                "capture_generation,capture_ordinal,scan_through,scan_cursor FROM contracts "
                "WHERE selector_digest IS NOT NULL AND (capture_generation IS NOT ? "
                "OR capture_ordinal<? OR scan_cursor IS NOT NULL) ORDER BY capture_ordinal LIMIT ?",
                (generation, head, CONTRACT_BATCH),
            ).fetchall()
        journal, stream, _resolutions = _journals(instance)
        for row in rows:
            contract = _contract_row(row[:4])
            selector, scanned_in, ordinal, scan_through, scan_cursor = row[4:]
            if scanned_in != generation:
                ordinal, scan_through, scan_cursor = 0, None, None
            through = head if scan_through is None else scan_through
            records, cursor, complete = journal.index.captures(
                stream,
                bodies=instance.body_store(),
                contract_digest=selector,
                since=None,
                until=now,
                limit=CAPTURE_PAGE,
                cursor=None if scan_cursor is None else tuple(json.loads(scan_cursor)),
                after={"generation": generation, "ordinal": ordinal},
                through={"generation": generation, "ordinal": through},
            )
            if not complete:
                continue  # the index is still projecting new landings
            windows: list[tuple[Any, ...]] = []
            unbound: list[tuple[str, TriggerEventReferenceV1, str]] = []
            bound: list[str] = []
            retry = False
            for stored in records:
                event = TriggerEventReferenceV1(
                    run_id=stored.record.run_id or "",
                    partition_id=stored.record.partition_id,
                    sequence=stored.record.sequence,
                    record_digest=stored.record_digest,
                )
                window = _bind(instance, contract, event, now=now)
                if window is None:
                    retry = True
                    break
                if isinstance(window, str):
                    unbound.append((stored.record_digest, event, window))
                    continue
                bound.append(stored.record_digest)
                if (item := _window_row(contract, window)) is not None:
                    windows.append(item)
            with _state(instance) as connection:
                assert connection is not None
                self._record_bindings(
                    connection, contract.identity, windows, unbound, bound, now=now
                )
                if retry:
                    continue
                connection.execute(
                    "UPDATE contracts SET capture_generation=?,capture_ordinal=?,scan_through=?,"
                    "scan_cursor=? WHERE identity=? AND artifact_digest=?",
                    (
                        generation,
                        ordinal if cursor is not None else through,
                        through if cursor is not None else None,
                        None if cursor is None else json.dumps(list(cursor)),
                        contract.identity,
                        contract.reference.artifact_digest,
                    ),
                )

    @staticmethod
    def _record_bindings(
        connection: sqlite3.Connection,
        identity: str,
        windows: list[tuple[Any, ...]],
        unbound: list[tuple[str, TriggerEventReferenceV1, str]],
        bound: list[str],
        *,
        now: datetime,
    ) -> None:
        # The contract may have been revised or retired while its anchors bound.
        if not connection.execute(
            "SELECT 1 FROM contracts WHERE identity=?", (identity,)
        ).fetchone():
            return
        connection.executemany(
            "INSERT OR IGNORE INTO windows(contract_id,identity,window,ends_at_us,status) "
            "VALUES (?,?,?,?,'open')",
            windows,
        )
        connection.executemany(
            "DELETE FROM unbindable WHERE identity=? AND record_digest=?",
            ((identity, digest) for digest in bound),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO unbindable VALUES (?,?,?,?,?,?)",
            (
                (
                    identity,
                    digest,
                    event.model_dump_json(),
                    code,
                    format_datetime(now),
                    _microseconds(now),
                )
                for digest, event, code in unbound
            ),
        )

    def _windows(self, instance: Any, *, now: datetime) -> None:
        """Check closed or newly answered windows against their own resolution journals."""

        from cruxible_core.procedures.resolution import (
            ProcedureResolutionBook,
            resolution_contract_partition_id,
        )

        columns = (
            "w.contract_id,w.window,w.dirty,c.identity,c.reference,c.contract,c.accepted_at "
            "FROM windows w JOIN contracts c ON c.identity=w.identity"
        )
        with _state(instance) as connection:
            assert connection is not None
            rows = connection.execute(
                f"SELECT {columns} WHERE w.dirty IS NOT NULL LIMIT ?", (CHECK_BATCH,)
            ).fetchall()
            rows += connection.execute(
                f"SELECT {columns} WHERE w.status='open' AND w.ends_at_us<=? AND w.dirty IS NULL "
                "ORDER BY w.ends_at_us LIMIT ?",
                (_microseconds(now), CHECK_BATCH - len(rows)),
            ).fetchall()
            retries = connection.execute(
                "SELECT u.record_digest,u.event,c.identity,c.reference,c.contract,c.accepted_at "
                "FROM unbindable u INDEXED BY unbindable_by_check "
                "JOIN contracts c ON c.identity=u.identity "
                "WHERE u.checked_at_us<=? ORDER BY u.checked_at_us LIMIT ?",
                (_microseconds(now - UNBINDABLE_RETRY), CHECK_BATCH),
            ).fetchall()
        journal, _captures, stream = _journals(instance)
        bodies = instance.body_store()
        checked: list[tuple[str, str, str | None]] = []
        for contract_id, window, dirty, *contract_row in rows:
            contract = _contract_row(tuple(contract_row))
            bound = BoundObservationWindowV1.model_validate_json(window)
            activation = _activation(contract, bound)
            assert activation.contract_id == contract_id
            book = ProcedureResolutionBook((activation,))
            book.replay(
                journal.all_records(stream, resolution_contract_partition_id(activation)),
                bodies=bodies,
            )
            status = (
                "resolved"
                if book.latest_non_overturned(contract_id) is not None
                else "settleable"
                if bound.ends_at <= now
                else "open"
            )
            checked.append((contract_id, status, dirty))
        rebound: list[tuple[_Contract, list[tuple[Any, ...]], list[Any], list[str]]] = []
        for digest, event_json, *contract_row in retries:
            contract = _contract_row(tuple(contract_row))
            event = TriggerEventReferenceV1.model_validate_json(event_json)
            window = _bind(instance, contract, event, now=now)
            if window is None:
                continue
            if isinstance(window, str):
                rebound.append((contract, [], [(digest, event, window)], []))
                continue
            row = _window_row(contract, window)
            rebound.append((contract, [] if row is None else [row], [], [digest]))
        with _state(instance) as connection:
            assert connection is not None
            for contract_id, status, dirty in checked:
                connection.execute(
                    "UPDATE windows SET status=?,checked_at=? WHERE contract_id=?",
                    (status, format_datetime(now), contract_id),
                )
                # A journal record that landed after this read marked it again.
                connection.execute(
                    "UPDATE windows SET dirty=NULL WHERE contract_id=? AND dirty IS ?",
                    (contract_id, dirty),
                )
            for contract, windows, unbound, found in rebound:
                self._record_bindings(
                    connection, contract.identity, windows, unbound, found, now=now
                )

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        with _state(instance, create=False) as connection:
            if connection is None:
                return ()
            row = connection.execute(
                "SELECT generation,backfill_after,capture_head,resolution_ordinal,last_error,"
                "last_error_at FROM progress"
            ).fetchone()
            if row is None:
                return ()
            counts = {
                name: connection.execute(sql, args).fetchone()[0]
                for name, sql, args in (
                    ("pending_contracts", "SELECT count(*) FROM pending", ()),
                    ("contracts", "SELECT count(*) FROM contracts", ()),
                    ("open_windows", "SELECT count(*) FROM windows WHERE status=?", ("open",)),
                    (
                        "settleable_windows",
                        "SELECT count(*) FROM windows WHERE status=?",
                        ("settleable",),
                    ),
                    ("unbindable_anchors", "SELECT count(*) FROM unbindable", ()),
                )
            }
        generation, backfill_after, capture_head, resolution_ordinal, error, error_at = row
        failing = error is not None
        return (
            ConsumerHealth(
                kind=self.name,
                consumer_id="consumer:prediction",
                state="stalled" if failing else "running",
                detail={
                    "generation": generation,
                    "contract_backfill_in_progress": backfill_after is not None,
                    "capture_position": capture_head,
                    "resolution_position": resolution_ordinal,
                    **counts,
                    "last_error": error,
                    "last_error_at": error_at,
                },
                repair=(
                    ConsumerRepair(
                        operation="hand_edit",
                        required_change="resolve_the_worker_error_then_restart_the_daemon",
                        arguments={},
                    )
                    if failing
                    else None
                ),
            ),
        )


PREDICTION_SETTLEMENT = PredictionSettlementConsumers()
