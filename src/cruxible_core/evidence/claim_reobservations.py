"""Recorded re-observations: content a Claim already cites, observed again (identity-design-1009).

Restating a Claim on a Capture whose observed content it already cites, under
the same contract and in the same citation association, is a no-op: no
revision, no generation, no proposal. The newer Capture is not discarded: it is
recorded here, on the evidence plane, so the cited content's freshness can move
to when it was last observed (``claim_verdicts.CaptureReobservationEvidence``).

Storage follows the Line inbox's pattern: a journal (``claim-reobservation-v1``)
is the append-only authority, and a SQLite projection keyed by Claim lineage is
caught up from its last applied sequence on every open. A record is admitted only
for a Claim live at the current head, read under the append's lock, and a
daemon-admitted Capture that verifies there and whose observed content equals a
Capture that Claim cites under the same contract. The
freshness lookup reads one indexed range per Claim lineage, never a scan.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.captures import (
    CaptureEnvelopeAny,
    observed_content,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_verdicts import (
    CaptureReobservationEvidence,
    CaptureVerdictEvidence,
)
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.errors import CruxibleError, ReadRefusalError
from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.exhaust.backends import LocalJournalBackend, journal_log_path
from cruxible_core.exhaust.records import (
    CLAIM_REOBSERVATION_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    parse_journal_payload,
)
from cruxible_core.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.preview_fence import is_previewing

_LOCK = threading.RLock()
_FENCE = "claim-reobservation-local-v1"
_PARTITION = "reobservations"
_EVENT_TAG = "cruxible-claim-reobservation-v1"
_ACCESS = BodyAccessContext(principal_id="claim-reobservation", can_read_body=True)
_SCHEMA_VERSION = 2
REOBSERVATION_DIRECTORY = "claim-reobservations"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The full history is kept (audit, historical verification). Freshness reads
# never materialize it: ``reobservations_latest`` answers the latest applicable
# record of one (lineage, contract, content) at an instant with one ordered
# probe, ``reobservations_next`` the next instant a record starts counting, and
# ``reobservations_by_sequence`` whether a lineage's records changed at all.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (singleton INTEGER PRIMARY KEY, sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reobservations (
 claim_id TEXT NOT NULL, capture_digest TEXT NOT NULL, contract_digest TEXT NOT NULL,
 observed_content_digest TEXT NOT NULL, observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
 observed_us INTEGER NOT NULL, recorded_us INTEGER NOT NULL, sequence INTEGER NOT NULL,
 PRIMARY KEY(claim_id, capture_digest)
) STRICT;
CREATE INDEX IF NOT EXISTS reobservations_latest ON reobservations
 (claim_id, contract_digest, observed_content_digest, observed_us, capture_digest);
CREATE INDEX IF NOT EXISTS reobservations_next ON reobservations
 (claim_id, contract_digest, observed_content_digest, recorded_us);
CREATE INDEX IF NOT EXISTS reobservations_by_sequence ON reobservations (claim_id, sequence);
"""


def _micros(value: datetime) -> int:
    return (value.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)


def _from_micros(value: object) -> datetime:
    return _EPOCH + timedelta(microseconds=int(str(value)))


class ClaimReobservationRefused(CruxibleError):
    """A re-observation that is not admissible is never recorded."""

    error_code = "cruxible.claim.reobservation_refused"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.error_code}: {message}")


@dataclass(frozen=True)
class ReobservationRecord:
    """One recorded re-observation of a Claim lineage's cited content."""

    claim_id: str
    capture_digest: str
    contract_digest: str
    observed_content_digest: str
    observed_at: datetime
    recorded_at: datetime


def _instant(value: object) -> datetime:
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise ValueError("re-observation instants are never empty")
    return parsed


def _root(instance: PlaybillInstance) -> Path:
    return instance.root / instance.descriptor.storage.exhaust / REOBSERVATION_DIRECTORY


class ClaimReobservationStore:
    """The re-observation journal and its disposable SQLite projection."""

    def __init__(self, instance: PlaybillInstance) -> None:
        self.instance = instance
        self.root = _root(instance)
        self.path = self.root / "reobservations.sqlite3"
        self.stream = JournalStreamIdentityV1(
            instance_id=instance.descriptor.instance_id,
            journal_family=CLAIM_REOBSERVATION_JOURNAL_FAMILY,
            stream_id="reobservations",
        )
        #: Index probes and rows the freshness lookups made, for counted-work tests.
        self.probes = 0
        self.rows_read = 0

    def _journal(self, *, create: bool) -> LocalJournalBackend | None:
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return LocalJournalBackend(self.root) if self.root.is_dir() else None

    @contextmanager
    def _locked(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        with _LOCK:
            journal = self._journal(create=create)
            if self.path.is_symlink():
                raise ValueError("re-observation projection must be a regular file")
            conn = sqlite3.connect(self.path) if self.root.is_dir() else sqlite3.connect(":memory:")
            try:
                if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                    conn.execute("DROP TABLE IF EXISTS progress")
                    conn.execute("DROP TABLE IF EXISTS reobservations")
                    conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT sequence FROM progress WHERE singleton=1").fetchone()
                start = row[0] + 1 if row else 1
                for stored in (
                    ()
                    if journal is None
                    else journal.select_records(
                        self.stream, partition_id=_PARTITION, first_sequence=start
                    )
                ):
                    payload = parse_journal_payload(
                        self.instance.body_store().read(
                            stored.record.payload_digest, access=_ACCESS
                        )
                    )
                    if not isinstance(payload, dict) or payload.get("tag") != _EVENT_TAG:
                        raise ValueError("invalid Claim re-observation record")
                    self._apply(conn, payload, stored.record.sequence, stored.record.recorded_at)
                conn.commit()
                yield conn
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _apply(
        conn: sqlite3.Connection, payload: Mapping[str, Any], sequence: int, recorded_at: datetime
    ) -> None:
        observed_at = _instant(payload["observed_at"])
        conn.execute(
            "INSERT OR IGNORE INTO reobservations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                payload["claim_id"],
                payload["capture_digest"],
                payload["contract_digest"],
                payload["observed_content_digest"],
                format_datetime(observed_at),
                format_datetime(recorded_at),
                _micros(observed_at),
                _micros(recorded_at),
                sequence,
            ),
        )
        conn.execute("INSERT OR REPLACE INTO progress VALUES(1,?)", (sequence,))

    # -- admission ----------------------------------------------------------

    def _admissible(
        self,
        *,
        claim_id: str,
        capture_digest: str,
        coordinate: AcceptedProjectionCoordinate,
    ) -> CaptureEnvelopeAny:
        """The re-observing Capture's envelope, or the typed refusal.

        It must be a daemon-admitted Capture that verifies at ``coordinate``,
        the Claim must be live there, and the Capture's observed content must
        equal a Capture the Claim cites there under the same contract.
        """

        from cruxible_core.service.evidence.capture_reads import (
            CaptureReadInvalid,
            verify_accepted_capture,
        )

        try:
            verified = verify_accepted_capture(
                self.instance,
                coordinate,
                capture_digest,
                access=_ACCESS,
                captures=self.instance.capture_verification_context(),
            )
        except (CaptureReadInvalid, CruxibleError, ReadRefusalError, ValueError) as exc:
            raise ClaimReobservationRefused(
                f"Capture {capture_digest} does not verify as daemon-admitted evidence: {exc}"
            ) from exc
        if isinstance(verified, str):
            raise ClaimReobservationRefused(
                f"Capture {capture_digest} is not admissible evidence here ({verified})"
            )
        envelope = verified.envelope
        content = self.instance.blob_at(coordinate.git_oid, claim_path(claim_id))
        if content is None:
            raise ClaimReobservationRefused(f"no accepted Claim {claim_id} to re-observe")
        claim = parse_claim(content, path=claim_path(claim_id))
        if claim.lifecycle.state != "live":
            raise ClaimReobservationRefused(
                f"Claim {claim_id} is {claim.lifecycle.state} at the head: a re-observation "
                "is recorded only for a live Claim"
            )
        if capture_digest in claim.backing.capture_digests:
            raise ClaimReobservationRefused(
                f"Claim {claim_id} already cites Capture {capture_digest}: nothing new was observed"
            )
        wanted = (envelope.capture_contract_digest, observed_content(envelope).digest)
        for cited in claim.backing.capture_digests:
            try:
                held = parse_capture_envelope(
                    self.instance.body_store().read(cited, access=_ACCESS)
                )
            except (CruxibleError, ValueError):
                continue
            if (held.capture_contract_digest, observed_content(held).digest) == wanted:
                return envelope
        raise ClaimReobservationRefused(
            f"Capture {capture_digest} observed no content Claim {claim_id} "
            "cites under its contract"
        )

    def _admitted(
        self,
        *,
        claim_id: str,
        capture_digest: str,
        head: AcceptedProjectionCoordinate,
        now: datetime,
    ) -> ReobservationRecord:
        envelope = self._admissible(
            claim_id=claim_id, capture_digest=capture_digest, coordinate=head
        )
        observed_at = envelope.observed_at.astimezone(UTC)
        if observed_at > now:
            raise ClaimReobservationRefused(
                f"Capture {capture_digest} was observed at {format_datetime(observed_at)}, "
                f"after the re-observation is recorded ({format_datetime(now)})"
            )
        return ReobservationRecord(
            claim_id=claim_id,
            capture_digest=capture_digest,
            contract_digest=envelope.capture_contract_digest,
            observed_content_digest=observed_content(envelope).digest,
            observed_at=observed_at,
            recorded_at=now,
        )

    def record(
        self,
        *,
        claim_id: str,
        capture_digest: str,
        actor_id: str,
        now: datetime,
    ) -> ReobservationRecord | None:
        """Record one admissible re-observation at the current head; a repeat is the same record.

        Admission is judged at the accepted head read under the same lock as the
        append: the Claim must be live there and cite a Capture of the same
        contract and observed content, and the re-observing Capture must be
        daemon-admitted, verify there, and not be observed after ``now``. A
        historical record stays readable whatever later happens to its Claim;
        this decides only whether a new one may be written. Nothing is written
        while previewing (``dry_run``): a preview observes nothing durable.
        """

        if is_previewing():
            self._admitted(
                claim_id=claim_id,
                capture_digest=capture_digest,
                head=self.instance.accepted_coordinate(),
                now=now,
            )
            return None
        with self._locked(create=True) as conn:
            known = conn.execute(
                "SELECT contract_digest, observed_content_digest, observed_at, recorded_at "
                "FROM reobservations WHERE claim_id=? AND capture_digest=?",
                (claim_id, capture_digest),
            ).fetchone()
            if known is not None:
                return ReobservationRecord(
                    claim_id=claim_id,
                    capture_digest=capture_digest,
                    contract_digest=str(known[0]),
                    observed_content_digest=str(known[1]),
                    observed_at=_instant(known[2]),
                    recorded_at=_instant(known[3]),
                )
            accepted = self.instance.accepted_coordinate()
            record = self._admitted(
                claim_id=claim_id, capture_digest=capture_digest, head=accepted, now=now
            )
            payload = {
                "tag": _EVENT_TAG,
                "claim_id": record.claim_id,
                "capture_digest": record.capture_digest,
                "contract_digest": record.contract_digest,
                "observed_content_digest": record.observed_content_digest,
                "observed_at": format_datetime(record.observed_at),
            }
            journal = self._journal(create=True)
            assert journal is not None
            from cruxible_core.storage.material_reservations import (
                ProcedureMaterialReservationStore,
            )

            bodies = self.instance.body_store()
            ProcedureMaterialReservationStore(bodies.reservation_root).recover_run_material(
                lambda reservation: journal.select_records(
                    self.stream,
                    event_kind="claim_reobservation",
                    payload_digest=reservation.body_digest,
                ),
                bodies=bodies,
                intended_event_kinds=frozenset({"claim_reobservation"}),
            )
            head = journal.read_head(self.stream, _PARTITION)
            journal.activate_writer(
                self.stream, _PARTITION, fencing_token=_FENCE, expected_head=head
            )
            stored = ProcedureExhaustWriter(
                journal=journal, bodies=bodies, fencing_token=_FENCE
            ).append(
                stream=self.stream,
                partition_id=_PARTITION,
                event_kind="claim_reobservation",
                accepted_coordinate=AcceptedCoordinate.from_internal(accepted),
                definition_digest=self.stream.identity_digest,
                actor_context=GovernedActorContext(
                    actor_type="service_account",
                    actor_id=actor_id,
                    org_id=self.instance.descriptor.instance_id,
                    operation_id=new_id("op", length=16, separator="_"),
                    timestamp=now,
                ),
                recorded_at=now,
                payload=payload,
                expected_head=head,
            )
            # The append is the record: from here a reader's catch-up and the
            # verdict-input fingerprint (``reobservation_journal_signature``)
            # already see it, whatever happens to the projection below.
            self._apply(conn, payload, stored.record.sequence, stored.record.recorded_at)
            conn.commit()
        return record

    # -- reads --------------------------------------------------------------

    def history(self, claim_id: str) -> tuple[ReobservationRecord, ...]:
        """Every re-observation ever recorded for one lineage, in capture order.

        The audit read: freshness never calls it (``ReobservationReader.latest``).
        """

        if not self.root.is_dir():
            return ()
        with self._locked() as conn:
            rows = conn.execute(
                "SELECT capture_digest, contract_digest, observed_content_digest, "
                "observed_at, recorded_at FROM reobservations WHERE claim_id=? "
                "ORDER BY capture_digest",
                (claim_id,),
            ).fetchall()
        return tuple(
            ReobservationRecord(
                claim_id=claim_id,
                capture_digest=str(row[0]),
                contract_digest=str(row[1]),
                observed_content_digest=str(row[2]),
                observed_at=_instant(row[3]),
                recorded_at=_instant(row[4]),
            )
            for row in rows
        )


def reobservation_journal_signature(root: Path, *, exhaust: str, instance_id: str) -> str | None:
    """What the re-observation journal holds, from its append-only log alone.

    The log is the authority, so this changes the moment a record is appended,
    before any projection catches up, and no separately written marker can
    lag it. ``None`` when nothing was ever recorded.
    """

    stream = JournalStreamIdentityV1(
        instance_id=instance_id,
        journal_family=CLAIM_REOBSERVATION_JOURNAL_FAMILY,
        stream_id="reobservations",
    )
    log = journal_log_path(root / exhaust / REOBSERVATION_DIRECTORY, stream, _PARTITION)
    try:
        stat = log.stat()
    except FileNotFoundError:
        return None
    return f"{stat.st_ino}:{stat.st_size}"


@dataclass(frozen=True)
class ReobservationKey:
    """One cited Capture of a Claim and the (contract, content) it observed."""

    cited_capture_digest: str
    contract_digest: str
    observed_content_digest: str


@dataclass(frozen=True)
class ReobservationReading:
    """What a freshness read of one lineage at one instant found.

    ``evidence`` is the latest applicable record per cited Capture;
    ``boundaries`` every instant that answer can change at (each record's
    observation and recording, and the next recording of each key);
    ``sequence`` the lineage's last journal sequence, which names its whole
    record set for a remembered answer.
    """

    evidence: tuple[CaptureReobservationEvidence, ...] = ()
    boundaries: tuple[datetime, ...] = ()
    sequence: int = 0


class ReobservationReader:
    """Indexed freshness reads over one instance's projection, on one connection.

    The connection is kept for the process; before each read the journal's log
    signature is compared with the one last caught up to, and only a change
    catches the projection up from the journal. Counters say how much each read
    cost, for counted-work tests.
    """

    _cache: dict[Path, "ReobservationReader"] = {}
    _cache_lock = threading.Lock()

    def __init__(self, instance: PlaybillInstance) -> None:
        self.store = ClaimReobservationStore(instance)
        self.instance = instance
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._signature: str | None = None
        self.probes = 0
        self.rows_read = 0

    @classmethod
    def of(cls, instance: PlaybillInstance) -> "ReobservationReader":
        """The process's reader for this instance's store."""

        root = _root(instance)
        with cls._cache_lock:
            reader = cls._cache.get(root)
            if reader is None or reader.instance is not instance:
                reader = cls(instance)
                cls._cache[root] = reader
            return reader

    def _connection(self) -> sqlite3.Connection | None:
        signature = reobservation_journal_signature(
            self.instance.root,
            exhaust=self.instance.descriptor.storage.exhaust,
            instance_id=self.instance.descriptor.instance_id,
        )
        if signature is None:
            return None
        if signature != self._signature or self._conn is None:
            with self.store._locked():
                pass  # catch the projection up from the journal
            if self._conn is None:
                self._conn = sqlite3.connect(self.store.path, check_same_thread=False)
            self._signature = signature
        return self._conn

    def sequence(self, claim_id: str) -> int:
        """The lineage's last journal sequence: 0 when it was never re-observed."""

        with self._lock:
            conn = self._connection()
            if conn is None:
                return 0
            self.probes += 1
            row = conn.execute(
                "SELECT MAX(sequence) FROM reobservations WHERE claim_id=?", (claim_id,)
            ).fetchone()
        return 0 if row is None or row[0] is None else int(row[0])

    def latest(
        self, claim_id: str, keys: Sequence[ReobservationKey], at: datetime
    ) -> ReobservationReading:
        """The latest record per key applicable at ``at``: one ordered probe each.

        Applicable means recorded by ``at`` (and so observed by it: admission
        refuses a Capture observed after its record). Among those the latest
        observation wins, ties broken by the larger Capture digest, exactly as
        ``claim_verdicts`` selects. Work is one indexed probe per distinct key,
        never the lineage's history.
        """

        at_us = _micros(at)
        evidence: list[CaptureReobservationEvidence] = []
        boundaries: set[datetime] = set()
        with self._lock:
            conn = self._connection()
            if conn is None:
                return ReobservationReading()
            self.probes += 1
            row = conn.execute(
                "SELECT MAX(sequence) FROM reobservations WHERE claim_id=?", (claim_id,)
            ).fetchone()
            sequence = 0 if row is None or row[0] is None else int(row[0])
            if not sequence:
                return ReobservationReading()
            found: dict[tuple[str, str], tuple[str, int, int] | None] = {}
            for key in keys:
                pair = (key.contract_digest, key.observed_content_digest)
                if pair not in found:
                    self.probes += 2
                    hit = conn.execute(
                        "SELECT capture_digest, observed_us, recorded_us FROM reobservations "
                        "INDEXED BY reobservations_latest WHERE claim_id=? AND "
                        "contract_digest=? AND observed_content_digest=? AND observed_us<=? "
                        "AND recorded_us<=? ORDER BY observed_us DESC, capture_digest DESC "
                        "LIMIT 1",
                        (claim_id, *pair, at_us, at_us),
                    ).fetchone()
                    upcoming = conn.execute(
                        "SELECT MIN(recorded_us) FROM reobservations "
                        "INDEXED BY reobservations_next WHERE claim_id=? AND "
                        "contract_digest=? AND observed_content_digest=? AND recorded_us>?",
                        (claim_id, *pair, at_us),
                    ).fetchone()
                    self.rows_read += int(hit is not None)
                    if upcoming is not None and upcoming[0] is not None:
                        boundaries.add(_from_micros(upcoming[0]))
                    found[pair] = None if hit is None else (str(hit[0]), int(hit[1]), int(hit[2]))
                selected = found[pair]
                if selected is None:
                    continue
                digest, observed_us, recorded_us = selected
                observed, recorded = _from_micros(observed_us), _from_micros(recorded_us)
                boundaries.update((observed, recorded))
                evidence.append(
                    CaptureReobservationEvidence(
                        cited_capture_digest=key.cited_capture_digest,
                        reobserving_capture_digest=digest,
                        observed_at=observed,
                        recorded_at=recorded,
                    )
                )
        return ReobservationReading(
            evidence=tuple(
                sorted(evidence, key=lambda item: item.cited_capture_digest.encode("ascii"))
            ),
            boundaries=tuple(sorted(boundaries)),
            sequence=sequence,
        )


def cited_reobservation_keys(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    captures: Sequence[CaptureVerdictEvidence],
) -> tuple[ReobservationKey, ...]:
    """Each cited Capture with the contract and content it observed, from the evidence index."""

    keys: list[ReobservationKey] = []
    with instance.bind_accepted_projection(coordinate) as projection:
        for item in captures:
            row = projection.typed.connection.execute(
                "SELECT contract_digest, observed_content_digest FROM captures "
                "WHERE capture_digest=?",
                (item.capture_digest,),
            ).fetchone()
            if row is not None:
                keys.append(ReobservationKey(item.capture_digest, str(row[0]), str(row[1])))
    return tuple(keys)


def reobservations_at(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claim_id: str,
    captures: Sequence[CaptureVerdictEvidence],
    at: datetime,
) -> ReobservationReading:
    """The recorded re-observations that count for one Claim's verdict at ``at``.

    A lineage never re-observed costs one indexed probe and nothing else; one
    that was costs one lookup per cited Capture and one ordered probe per cited
    (contract, content), however long its history.
    """

    reader = ReobservationReader.of(instance)
    if not reader.sequence(claim_id):
        return ReobservationReading()
    keys = cited_reobservation_keys(instance, coordinate=coordinate, captures=captures)
    return reader.latest(claim_id, keys, at)


__all__ = [
    "REOBSERVATION_DIRECTORY",
    "ClaimReobservationRefused",
    "ClaimReobservationStore",
    "ReobservationKey",
    "ReobservationReader",
    "ReobservationReading",
    "ReobservationRecord",
    "cited_reobservation_keys",
    "reobservation_journal_signature",
    "reobservations_at",
]
