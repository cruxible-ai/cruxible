"""Evidence availability as a consumer kind: resuming, findings only.

A Claim's evidence is only as good as the stored Capture it cites. This kind
re-checks cited Captures against the content-addressed store and records the
ones that are missing or no longer hash to their address. It checks a Capture
when a generation starts citing it, and sweeps every cited Capture on a slow
cadence, because bytes can rot with no event to say so.

What counts is the Capture's own retention policy, exactly as admission reads
it: the envelope is the evidence record and must always be there, a body that
is present must hash to its address, and a missing body is a finding only
while the Capture's contract requires it to be retained. Absence a policy
permits is not a defect.

The findings are a disposable projection of the store's current state:
deleting them costs only a fresh sweep. Nothing here is governed, so the
worker needs no authority and a restart resumes from its cursor.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    CaptureContractV1,
    CaptureRetentionErasurePolicyV1,
    capture_contract_digest,
    foreign_source_capture_contract,
    parse_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.consumers.clock import cadence_due
from cruxible_core.consumers.protocol import (
    ConsumerHealth,
    ConsumerRepair,
    ConsumerWork,
    CursorPolicy,
    EffectClass,
)
from cruxible_core.server.config import get_disabled_consumers

#: How often every cited Capture is re-hashed.
SWEEP_INTERVAL = timedelta(days=1)
#: Captures one unit of work checks before yielding.
CHECK_BATCH = 256
#: Generations one matching pass reads before yielding.
GENERATION_BATCH = 64

_READER = BodyAccessContext(principal_id="evidence-availability", can_read_body=True)
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 generation INTEGER NOT NULL, sweep_after TEXT, sweep_completed_at TEXT,
 last_error TEXT, last_error_at TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS pending (capture_digest TEXT PRIMARY KEY) STRICT;
CREATE TABLE IF NOT EXISTS findings (
 capture_digest TEXT NOT NULL, part TEXT NOT NULL CHECK(part IN ('envelope','body')),
 object_digest TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('missing','corrupt')),
 checked_at TEXT NOT NULL, PRIMARY KEY(capture_digest, part)
) STRICT;
"""


@dataclass(frozen=True)
class EvidenceFinding:
    capture_digest: str
    part: Literal["envelope", "body"]
    object_digest: str
    state: Literal["missing", "corrupt"]
    checked_at: datetime


def _root(instance: Any) -> Path:
    exhaust = Path(instance.root) / str(instance.descriptor.storage.exhaust)
    return exhaust / "evidence-availability"


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


def evidence_findings(instance: Any) -> tuple[EvidenceFinding, ...]:
    """The unavailable cited evidence the worker last observed; empty before it ran."""

    with _state(instance, create=False) as connection:
        if connection is None:
            return ()
        rows = connection.execute(
            "SELECT capture_digest,part,object_digest,state,checked_at FROM findings "
            "ORDER BY capture_digest,part"
        ).fetchall()
    return tuple(
        EvidenceFinding(
            capture_digest=row[0],
            part=row[1],
            object_digest=row[2],
            state=row[3],
            checked_at=_instant(row[4]),
        )
        for row in rows
    )


def _instant(value: str) -> datetime:
    parsed = parse_datetime(value)
    assert parsed is not None
    return parsed


def _cited_captures(
    connection: sqlite3.Connection, *, after: str, limit: int
) -> tuple[list[str], str | None]:
    """The next page of Captures live Claims cite, and where the page stopped.

    Each step seeks the next distinct digest through the capture index and
    asks whether a live Claim cites it, so a page costs its own size, not the
    citation population. Returns None as the stop position once exhausted.
    """

    cited: list[str] = []
    position = after
    for _step in range(limit * 4):
        row = connection.execute(
            "SELECT capture_digest FROM citation_uses INDEXED BY citations_by_capture "
            "WHERE capture_digest>? ORDER BY capture_digest LIMIT 1",
            (position,),
        ).fetchone()
        if row is None:
            return cited, None
        position = row[0]
        if connection.execute(
            "SELECT 1 FROM citation_uses u INDEXED BY citations_by_capture "
            "JOIN claims c ON c.identity=u.owner_key "
            "WHERE u.capture_digest=? AND u.owner_kind='Claim' AND c.lifecycle='live' LIMIT 1",
            (position,),
        ).fetchone():
            cited.append(position)
            if len(cited) == limit:
                break
    return cited, position


_BUILT_IN_CONTRACTS = {
    capture_contract_digest(contract).tagged: contract
    for contract in (
        DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    )
}


def _retention(
    instance: Any, projection: Any, *, git_oid: str, capture_digest: str
) -> CaptureRetentionErasurePolicyV1 | None:
    row = projection.typed.connection.execute(
        "SELECT contract_digest,logical_source_id FROM captures WHERE capture_digest=?",
        (capture_digest,),
    ).fetchone()
    if row is None:
        return None
    contract_digest, source_id = row
    contract: CaptureContractV1 | None = _BUILT_IN_CONTRACTS.get(contract_digest)
    if contract is None and source_id is not None:
        foreign = foreign_source_capture_contract(source_id)
        if capture_contract_digest(foreign).tagged == contract_digest:
            contract = foreign
    if contract is None:
        path = projection.citations.capture_contract_path(contract_digest)
        raw = None if path is None else instance.blob_at(git_oid, path)
        if raw is not None and path is not None:
            contract = parse_capture_contract(raw, path=path)
    return None if contract is None else contract.retention_erasure_policy


def _absence_permitted(
    policy: CaptureRetentionErasurePolicyV1 | None, *, observed_at: datetime, now: datetime
) -> bool:
    if policy is None:
        return False
    if policy.body_retention in {"never_materialize", "optional"}:
        return True
    assert policy.minimum_retention is not None
    return now >= observed_at + timedelta(microseconds=policy.minimum_retention.microseconds)


class EvidenceAvailabilityConsumers:
    name = "evidence"
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
                if row is None:
                    # A new worker does not replay history: the first sweep covers it.
                    connection.execute(
                        "INSERT INTO progress(singleton,generation) VALUES (1,?)", (head,)
                    )
                    return
                cursor = row[0]
            if cursor >= head:
                return
            through = min(head, cursor + GENERATION_BATCH)
            claims = {
                version.identity
                for sequence in range(cursor + 1, through + 1)
                for version in history.versions_at(sequence)
                if version.path.startswith("claims/")
            }
        captures: set[str] = set()
        if claims:
            with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
                for identity in sorted(claims):
                    captures.update(
                        str(use["capture_digest"])
                        for use in projection.citations.owner_uses("Claim", identity)
                    )
        with _state(instance) as connection:
            assert connection is not None
            connection.executemany(
                "INSERT OR IGNORE INTO pending VALUES (?)", ((digest,) for digest in captures)
            )
            connection.execute("UPDATE progress SET generation=?", (through,))

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        with _state(instance) as connection:
            assert connection is not None
            pending = connection.execute("SELECT 1 FROM pending LIMIT 1").fetchone()
            row = connection.execute(
                "SELECT sweep_after,sweep_completed_at FROM progress"
            ).fetchone()
        work = []
        if pending is not None:
            work.append(ConsumerWork(key="events", item="events"))
        if row is not None:
            sweep_after, completed = row
            next_sweep = cadence_due(
                SWEEP_INTERVAL, last=None if completed is None else _instant(completed)
            )
            if sweep_after is not None or next_sweep is None or next_sweep <= now:
                work.append(ConsumerWork(key="sweep", item="sweep"))
        return tuple(work)

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        instance = manager.get(instance_id)
        try:
            if work.item == "events":
                self._check_pending(instance, now=now)
            else:
                self._sweep(instance, now=now)
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

    def _check_pending(self, instance: Any, *, now: datetime) -> None:
        with _state(instance) as connection:
            assert connection is not None
            digests = [
                row[0]
                for row in connection.execute(
                    "SELECT capture_digest FROM pending ORDER BY capture_digest LIMIT ?",
                    (CHECK_BATCH,),
                ).fetchall()
            ]
        self._check(instance, digests, now=now)
        with _state(instance) as connection:
            assert connection is not None
            connection.executemany(
                "DELETE FROM pending WHERE capture_digest=?", ((digest,) for digest in digests)
            )

    def _sweep(self, instance: Any, *, now: datetime) -> None:
        with _state(instance) as connection:
            assert connection is not None
            (after,) = connection.execute("SELECT sweep_after FROM progress").fetchone()
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            digests, stopped = _cited_captures(
                projection.typed.connection, after=after or "", limit=CHECK_BATCH
            )
        self._check(instance, digests, now=now)
        with _state(instance) as connection:
            assert connection is not None
            if stopped is None:
                connection.execute(
                    "UPDATE progress SET sweep_after=NULL,sweep_completed_at=?",
                    (format_datetime(now),),
                )
            else:
                connection.execute("UPDATE progress SET sweep_after=?", (stopped,))

    def _check(self, instance: Any, digests: list[str], *, now: datetime) -> None:
        if not digests:
            return
        store = instance.body_store()
        coordinate = instance.accepted_coordinate()
        observed: list[tuple[str, str, str, str]] = []
        cleared: list[str] = []
        with instance.bind_accepted_projection(coordinate) as projection:
            for digest in digests:
                envelope_state = store.availability(digest)
                if envelope_state != "present":
                    observed.append((digest, "envelope", digest, envelope_state))
                    continue
                cleared.append(digest)
                try:
                    envelope = parse_capture_envelope(store.read(digest, access=_READER))
                except PlaybillError:
                    observed.append((digest, "envelope", digest, "corrupt"))
                    continue
                if envelope.commitment.materialization != "cas":
                    continue
                body = envelope.commitment.digest
                body_state = store.availability(body)
                if body_state == "corrupt" or (
                    body_state == "missing"
                    and not _absence_permitted(
                        _retention(
                            instance, projection, git_oid=coordinate.git_oid, capture_digest=digest
                        ),
                        observed_at=envelope.observed_at,
                        now=now,
                    )
                ):
                    observed.append((digest, "body", body, body_state))
        with _state(instance) as connection:
            assert connection is not None
            connection.executemany(
                "DELETE FROM findings WHERE capture_digest=?", ((digest,) for digest in digests)
            )
            connection.executemany(
                "INSERT INTO findings VALUES (?,?,?,?,?)",
                ((*finding, format_datetime(now)) for finding in observed),
            )

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        with _state(instance, create=False) as connection:
            if connection is None:
                return ()
            row = connection.execute(
                "SELECT generation,sweep_after,sweep_completed_at,last_error,last_error_at "
                "FROM progress"
            ).fetchone()
            pending = connection.execute("SELECT count(*) FROM pending").fetchone()[0]
        if row is None:
            return ()
        generation, sweep_after, completed, error, error_at = row
        failing = error is not None
        with instance.accepted_history_reader() as history:
            behind = history.sequence - generation
        # Behind by more than one matching pass, or a sweep a full interval late.
        lagging = behind > GENERATION_BATCH or (
            completed is not None and now >= _instant(completed) + 2 * SWEEP_INTERVAL
        )
        return (
            ConsumerHealth(
                kind=self.name,
                consumer_id="consumer:evidence",
                state="stalled" if failing else "lagging" if lagging else "running",
                detail={
                    "generation": generation,
                    "generations_behind": behind,
                    "pending_checks": pending,
                    "sweep_in_progress": sweep_after is not None,
                    "sweep_completed_at": completed,
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


EVIDENCE_AVAILABILITY = EvidenceAvailabilityConsumers()
