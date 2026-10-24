"""Evidence availability within the next consumer.

A Claim's evidence is only as good as the stored Capture it cites. This part
re-checks cited Captures against the content-addressed store and records the
ones that are missing or no longer hash to their address. It checks a Capture
when a generation starts citing it, and sweeps every cited Capture when a
durable trigger event requests it, because bytes can rot without a generation.

What counts is the Capture's own retention policy, exactly as admission reads
it: the envelope is the evidence record and must always be there, a body that
is present must hash to its address, and a missing body is a finding only
while the Capture's contract requires it to be retained. Absence a policy
permits is not a defect. The bodies checked are the ones replay reads: a CAS
source and a CAS-materialized commitment.

Hashing a body records what it finds in the body store's landing log when the
log says otherwise (``LandingLog.note``): a landed body found missing or
corrupt is recorded damaged, so every read at a later position answers it
absent and the next part's Claims citing it re-derive as not replayable; one
found whole again is recorded present.

A body erased under its contract's erasure rule (``capture erase``) is no
defect either: the erasure journal says who erased it and why. The worker
follows that journal like the generations, checking each erased Capture (and
any cited Capture holding the same bytes) once, and records the live Claims
whose every cited Capture, their own words aside, now has an erased body
(``erased_claims``); ``next`` lists each as ``claim_evidence_erased``. A
generation that re-cites such a Claim re-checks its Captures, so the row
follows the Claim, not the erasure.

Retention is evaluated at the generation or trigger event's recorded instant,
never the worker's clock. The findings are a disposable projection of the
store's current state: deleting them replays retained sweep events. Nothing here is governed, so the
worker needs no authority and a restart resumes from its cursor.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACTS,
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    CaptureRetentionErasurePolicy,
    capture_contract_digest,
    foreign_source_capture_contract,
    parse_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.captures import (
    CaptureContractAny as CaptureContract,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.source_references import CasSourceReference
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_client.contracts.triggers import INTERNAL_ACTIONS
from cruxible_core.consumers.protocol import (
    ConsumerHealth,
    ConsumerRepair,
    ConsumerWork,
)
from cruxible_core.consumers.state import DisposableState
from cruxible_core.evidence.capture_erasures import (
    CaptureErasureStore,
    citing_live_claims,
    claims_left_without_evidence,
    erasure_journal_present,
    evidence_captures,
)
from cruxible_core.triggers.journal import trigger_events

#: The internal action this part performs; its registry entry names this part.
ACTION = INTERNAL_ACTIONS["evidence.sweep"]

#: Captures one unit of work checks before yielding.
CHECK_BATCH = 256
#: Generations one matching pass reads before yielding.
GENERATION_BATCH = 64

_READER = BodyAccessContext(principal_id="next-evidence", can_read_body=True)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 generation INTEGER NOT NULL, sweep_sequence INTEGER NOT NULL DEFAULT 0,
 sweep_after TEXT, sweep_completed_at TEXT,
 last_error TEXT, last_error_at TEXT, erasure_sequence INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE TABLE IF NOT EXISTS pending (
 capture_digest TEXT PRIMARY KEY, evaluation_time TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS tally (name TEXT PRIMARY KEY, value INTEGER NOT NULL) STRICT;
INSERT OR IGNORE INTO tally VALUES ('pending',0);
CREATE TRIGGER IF NOT EXISTS pending_added AFTER INSERT ON pending
 BEGIN UPDATE tally SET value=value+1 WHERE name='pending'; END;
CREATE TRIGGER IF NOT EXISTS pending_removed AFTER DELETE ON pending
 BEGIN UPDATE tally SET value=value-1 WHERE name='pending'; END;
CREATE TABLE IF NOT EXISTS findings (
 capture_digest TEXT NOT NULL, part TEXT NOT NULL CHECK(part IN ('envelope','body')),
 object_digest TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('missing','corrupt')),
 checked_at TEXT NOT NULL, PRIMARY KEY(capture_digest, part)
) STRICT;
CREATE TABLE IF NOT EXISTS erased_claims (
 claim_identity TEXT NOT NULL, capture_digest TEXT NOT NULL, checked_by TEXT NOT NULL,
 checked_at TEXT NOT NULL, PRIMARY KEY(claim_identity, capture_digest)
) STRICT;
CREATE INDEX IF NOT EXISTS erased_claims_by_check ON erased_claims(checked_by);
"""
_STATE = DisposableState("next/evidence", _SCHEMA)


@dataclass(frozen=True)
class EvidenceFinding:
    capture_digest: str
    part: Literal["envelope", "body"]
    object_digest: str
    state: Literal["missing", "corrupt"]
    checked_at: datetime


def evidence_findings(instance: Any) -> tuple[EvidenceFinding, ...]:
    """The unavailable cited evidence the worker last observed; empty before it ran."""

    with _STATE.open(instance, create=False) as connection:
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


@dataclass(frozen=True)
class ErasedClaim:
    """A live Claim whose every cited Capture, its own words aside, has an erased body."""

    claim_identity: str
    capture_digests: tuple[str, ...]
    checked_at: datetime


def erased_claims(instance: Any) -> tuple[ErasedClaim, ...]:
    """The Claims the worker last found left without retained evidence; empty before it ran."""

    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            return ()
        rows = connection.execute(
            "SELECT claim_identity,capture_digest,checked_at FROM erased_claims "
            "ORDER BY claim_identity,capture_digest"
        ).fetchall()
    grouped: dict[str, tuple[list[str], str]] = {}
    for claim, capture, checked in rows:
        captures, latest = grouped.setdefault(str(claim), ([], str(checked)))
        captures.append(str(capture))
        grouped[str(claim)] = (captures, max(latest, str(checked)))
    return tuple(
        ErasedClaim(
            claim_identity=claim, capture_digests=tuple(captures), checked_at=_instant(checked)
        )
        for claim, (captures, checked) in grouped.items()
    )


def _instant(value: str) -> datetime:
    parsed = parse_datetime(value)
    assert parsed is not None
    return parsed


def sweep_covered_sequence(instance: Any, *, timeout: float | None = None) -> int:
    """The trigger fire sequence through which ``evidence.sweep`` has fully swept.

    Advances only once a sweep is exhausted (``_sweep``'s ``stopped is None``
    branch), never mid-page, so a paged sweep behind a large cited-Capture
    population is never reported covered early.

    ``timeout``, when given, bounds the read to that many seconds and raises
    ``StateBusyError`` rather than waiting on a worker's own transaction.
    """

    with _STATE.open(instance, create=False, timeout=timeout) as connection:
        if connection is None:
            return 0
        row = connection.execute("SELECT sweep_sequence FROM progress").fetchone()
        return 0 if row is None else int(row[0])


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
        *COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACTS,
    )
}


def _retention(
    instance: Any, projection: Any, *, git_oid: str, capture_digest: str
) -> CaptureRetentionErasurePolicy | None:
    row = projection.typed.connection.execute(
        "SELECT contract_digest,logical_source_id FROM captures WHERE capture_digest=?",
        (capture_digest,),
    ).fetchone()
    if row is None:
        return None
    contract_digest, source_id = row
    contract: CaptureContract | None = _BUILT_IN_CONTRACTS.get(contract_digest)
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
    policy: CaptureRetentionErasurePolicy | None, *, observed_at: datetime, now: datetime
) -> bool:
    if policy is None:
        return False
    if policy.body_retention in {"never_materialize", "optional"}:
        return True
    assert policy.minimum_retention is not None
    return now >= observed_at + timedelta(microseconds=policy.minimum_retention.microseconds)


class EvidencePart:
    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        self._follow_erasures(instance)
        with instance.accepted_history_reader() as history:
            head = history.sequence
            with _STATE.open(instance) as connection:
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
        evaluated_at = instance.accepted_evaluation_time(instance.accepted_coordinate().git_oid)
        with _STATE.open(instance) as connection:
            assert connection is not None
            connection.executemany(
                "INSERT INTO pending VALUES (?,?) ON CONFLICT(capture_digest) "
                "DO UPDATE SET evaluation_time=excluded.evaluation_time",
                ((digest, format_datetime(evaluated_at)) for digest in captures),
            )
            # A changed Claim answers again from the Captures it cites now: those
            # still erased re-add its rows when they are checked (one indexed
            # delete per changed Claim).
            connection.executemany(
                "DELETE FROM erased_claims WHERE claim_identity=?",
                ((identity,) for identity in claims),
            )
            connection.execute("UPDATE progress SET generation=?", (through,))

    def _follow_erasures(self, instance: Any) -> None:
        """Queue each newly erased Capture, and every cited one holding the same bytes.

        Follows the erasure journal from this part's cursor: one record per
        erasure, never a walk of the Captures. A part that has not started yet
        leaves it to the first pass (which starts this cursor at the beginning).
        """

        if not erasure_journal_present(instance):
            return
        with _STATE.open(instance) as connection:
            assert connection is not None
            row = connection.execute("SELECT erasure_sequence FROM progress").fetchone()
        if row is None:
            return
        erasures = CaptureErasureStore(instance)
        records = erasures.records_after(int(row[0]), limit=CHECK_BATCH)
        if not records:
            return
        queued: dict[str, str] = {}
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            connection = projection.typed.connection
            for _sequence, record in records:
                at = str(format_datetime(record.erased_at))
                queued[record.capture_digest] = at
                for (holder,) in connection.execute(
                    "SELECT capture_digest FROM captures INDEXED BY captures_by_commitment "
                    "WHERE evidence_commitment_digest IN ("
                    + ",".join("?" for _ in record.body_digests)
                    + ")",
                    record.body_digests,
                ):
                    queued.setdefault(str(holder), at)
        with _STATE.open(instance) as connection:
            assert connection is not None
            connection.executemany(
                "INSERT INTO pending VALUES (?,?) ON CONFLICT(capture_digest) "
                "DO UPDATE SET evaluation_time=excluded.evaluation_time",
                queued.items(),
            )
            connection.execute("UPDATE progress SET erasure_sequence=?", (records[-1][0],))

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        with _STATE.open(instance) as connection:
            assert connection is not None
            pending = connection.execute("SELECT 1 FROM pending LIMIT 1").fetchone()
            row = connection.execute("SELECT sweep_sequence FROM progress").fetchone()
        work = []
        if pending is not None:
            work.append(ConsumerWork(key="events", item="events"))
        if row is not None and trigger_events(instance, after=row[0], action=ACTION.name, limit=1):
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
            with _STATE.open(instance) as connection:
                assert connection is not None
                connection.execute(
                    "UPDATE progress SET last_error=?,last_error_at=?",
                    (f"{type(exc).__name__}: {exc}", format_datetime(now)),
                )
            raise
        with _STATE.open(instance) as connection:
            assert connection is not None
            connection.execute("UPDATE progress SET last_error=NULL,last_error_at=NULL")

    def _check_pending(self, instance: Any, *, now: datetime) -> None:
        with _STATE.open(instance) as connection:
            assert connection is not None
            pending = connection.execute(
                "SELECT capture_digest,evaluation_time FROM pending "
                "ORDER BY capture_digest LIMIT ?",
                (CHECK_BATCH,),
            ).fetchall()
        by_time: dict[str, list[str]] = {}
        for digest, evaluated_at in pending:
            by_time.setdefault(evaluated_at, []).append(digest)
        for evaluated_at, digests in by_time.items():
            self._check(instance, digests, now=_instant(evaluated_at))
        with _STATE.open(instance) as connection:
            assert connection is not None
            connection.executemany(
                "DELETE FROM pending WHERE capture_digest=? AND evaluation_time=?", pending
            )

    def _sweep(self, instance: Any, *, now: datetime) -> None:
        with _STATE.open(instance) as connection:
            assert connection is not None
            sequence, after = connection.execute(
                "SELECT sweep_sequence,sweep_after FROM progress"
            ).fetchone()
        events = trigger_events(instance, after=sequence, action=ACTION.name, limit=1)
        if not events:
            return
        (event,) = events
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            digests, stopped = _cited_captures(
                projection.typed.connection, after=after or "", limit=CHECK_BATCH
            )
        self._check(instance, digests, now=event.fired_at)
        with _STATE.open(instance) as connection:
            assert connection is not None
            if stopped is None:
                connection.execute(
                    "UPDATE progress SET sweep_after=NULL,sweep_completed_at=?,sweep_sequence=?",
                    (format_datetime(event.fired_at), event.sequence),
                )
            else:
                connection.execute("UPDATE progress SET sweep_after=?", (stopped,))

    def _check(self, instance: Any, digests: list[str], *, now: datetime) -> None:
        if not digests:
            return
        store = instance.body_store()
        coordinate = instance.accepted_coordinate()
        observed: list[tuple[str, str, str, str]] = []
        erased: list[tuple[str, str, str]] = []
        any_erasure = erasure_journal_present(instance)
        with instance.bind_accepted_projection(coordinate) as projection:
            for digest in digests:
                envelope_state = store.availability(digest)
                if envelope_state != "present":
                    observed.append((digest, "envelope", digest, envelope_state))
                    continue
                try:
                    envelope = parse_capture_envelope(store.read(digest, access=_READER))
                except CruxibleError:
                    observed.append((digest, "envelope", digest, "corrupt"))
                    continue
                # The bytes replay reads: a CAS source, and a CAS-materialized commitment.
                bodies = [
                    *(
                        [envelope.commitment.digest]
                        if envelope.commitment.materialization == "cas"
                        else []
                    ),
                    *(
                        [envelope.source.content_digest]
                        if isinstance(envelope.source, CasSourceReference)
                        else []
                    ),
                ]
                reported = False
                states = {body: store.availability(body) for body in dict.fromkeys(bodies)}
                # An erased body is the erasure journal's record, not damage.
                records = (
                    CaptureErasureStore(instance).for_bodies(
                        tuple(body for body, state in states.items() if state == "missing")
                    )
                    if any_erasure
                    else {}
                )
                if records:
                    erased.extend(
                        (claim, cited, digest)
                        for claim, cited in _left_without_evidence(instance, projection, digest)
                    )
                for body, body_state in states.items():
                    # Hashing it records a damaged (or repaired) body in the landing
                    # log, so the Claims citing it re-derive as not replayable.
                    if body in records:
                        continue
                    if not reported and (
                        body_state == "corrupt"
                        or (
                            body_state == "missing"
                            and not _absence_permitted(
                                _retention(
                                    instance,
                                    projection,
                                    git_oid=coordinate.git_oid,
                                    capture_digest=digest,
                                ),
                                observed_at=envelope.observed_at,
                                now=now,
                            )
                        )
                    ):
                        # One row per Capture part: the first damaged body it names.
                        observed.append((digest, "body", body, body_state))
                        reported = True
        with _STATE.open(instance) as connection:
            assert connection is not None
            connection.executemany(
                "DELETE FROM findings WHERE capture_digest=?", ((digest,) for digest in digests)
            )
            connection.executemany(
                "INSERT INTO findings VALUES (?,?,?,?,?)",
                ((*finding, format_datetime(now)) for finding in observed),
            )
            # Each checked Capture answers for the rows its check found.
            connection.executemany(
                "DELETE FROM erased_claims WHERE checked_by=?", ((digest,) for digest in digests)
            )
            connection.executemany(
                "INSERT OR REPLACE INTO erased_claims VALUES (?,?,?,?)",
                ((*row, format_datetime(now)) for row in erased),
            )

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            row = connection.execute(
                "SELECT generation,sweep_sequence,sweep_completed_at,last_error,last_error_at "
                "FROM progress"
            ).fetchone()
            # Kept by triggers as checks queue and drain, so health reads no backlog.
            pending = connection.execute("SELECT value FROM tally WHERE name='pending'").fetchone()[
                0
            ]
        if row is None:
            return ()
        generation, sequence, completed, error, error_at = row
        failing = error is not None
        with instance.accepted_history_reader() as history:
            behind = history.sequence - generation
        sweeps = trigger_events(instance, after=sequence, action=ACTION.name, limit=2)
        sweep_pending = bool(sweeps)
        lagging = behind > GENERATION_BATCH or len(sweeps) > 1
        return (
            ConsumerHealth(
                kind="next",
                consumer_id="consumer:next",
                state="stalled" if failing else "lagging" if lagging else "running",
                detail={
                    "generation": generation,
                    "generations_behind": behind,
                    "pending_checks": pending,
                    "sweep_in_progress": sweep_pending,
                    "sweep_position": sequence,
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


def _left_without_evidence(
    instance: Any, projection: Any, capture_digest: str
) -> list[tuple[str, str]]:
    """(Claim, erased Capture) for each live Claim citing this one that it leaves bare.

    The Claims citing it (one indexed range), each with its own citations: the
    work grows with this Capture's citations, never with the instance.
    """

    connection = projection.typed.connection
    claims = claims_left_without_evidence(
        instance, connection, citing_live_claims(connection, capture_digest)
    )
    return [(claim, cited) for claim in claims for cited in evidence_captures(connection, claim)]


_PART = EvidencePart()
