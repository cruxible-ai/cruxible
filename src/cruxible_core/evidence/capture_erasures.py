"""Recorded Capture erasures: which retained body was destroyed, by whom, when and why.

Storage follows the re-observation journal's pattern: a journal
(``capture-erasure-v1``) is the append-only authority, and a SQLite projection
keyed by journal position, Capture and body is caught up from its last applied
sequence on every open. The journal record is written before the bytes are
unlinked, and carries the landing log's position at that moment: an
interrupted erasure leaves its record and the body, and the retry (the bytes
last landed at or before that position) finishes the unlink without writing
another. Bytes stored again after it (landed later) are another Capture's
material, so erasing them again is another erasure, recorded at its own
position; so is erasing bytes present under a record that carries no position
(written before positions were kept) or names a replaced log, since it cannot
show they are the bytes it destroyed.

A record outlives the bytes it names. Whether a Capture reads as erased is the
record together with the body store: the body must also be absent at the read's
landing position (``erased_body``), so a body that lands again (the same bytes
captured anew) reads as present, as the landing log says it is.

Lookups are one indexed row per Capture or body; an instance that never erased
anything has no projection file and answers at once.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from cruxible_client.contracts.capture_erasure import (
    CaptureErasureRecord,
    ErasureLandingPosition,
    RecordedCaptureErasure,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_digest,
    capture_retained_body_digests,
)
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.exhaust.backends import LocalJournalBackend, journal_log_path
from cruxible_core.exhaust.records import (
    CAPTURE_ERASURE_JOURNAL_FAMILY,
    JournalEventKindV1,
    JournalStreamIdentityV1,
    parse_journal_payload,
)
from cruxible_core.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.storage.capture_metadata import capture_envelope
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.preview_fence import is_previewing
from cruxible_core.storage.read_positions import body_present

_LOCK = threading.RLock()
_FENCE = "capture-erasure-local-v1"
_PARTITION = "erasures"
_EVENT_KIND: JournalEventKindV1 = "capture_erasure"
_ACCESS = BodyAccessContext(principal_id="capture-erasure", can_read_body=True)
_SCHEMA_VERSION = 3
ERASURE_DIRECTORY = "capture-erasures"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), sequence INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS erasures (
 sequence INTEGER PRIMARY KEY, capture_digest TEXT NOT NULL, record TEXT NOT NULL,
 coordinate TEXT NOT NULL, landing TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS erasures_by_capture ON erasures(capture_digest, sequence);
CREATE TABLE IF NOT EXISTS erased_bodies (
 body_digest TEXT NOT NULL, sequence INTEGER NOT NULL, capture_digest TEXT NOT NULL,
 PRIMARY KEY(body_digest, sequence)
) STRICT, WITHOUT ROWID;
"""
#: The payload key the landing position travels under; the record never restates it.
_LANDING = "landing"


def _root(instance: Any) -> Path:
    return Path(instance.root) / str(instance.descriptor.storage.exhaust) / ERASURE_DIRECTORY


def _stream(instance: Any) -> JournalStreamIdentityV1:
    return JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=CAPTURE_ERASURE_JOURNAL_FAMILY,
        stream_id="erasures",
    )


def _record(
    payload: object, recorded_at: datetime
) -> tuple[CaptureErasureRecord, ErasureLandingPosition | None]:
    if not isinstance(payload, dict):
        raise ValueError("invalid Capture erasure record")
    fields = dict(payload)
    raw_landing = fields.pop(_LANDING, None)
    landing = None if raw_landing is None else ErasureLandingPosition.model_validate(raw_landing)
    # The journal record's own instant is the erasure's: the payload never restates it.
    return CaptureErasureRecord.model_validate({**fields, "erased_at": recorded_at}), landing


def _recorded(row: tuple[Any, ...]) -> RecordedCaptureErasure:
    """One projection row (sequence, record, coordinate, landing) as the law reads it."""

    sequence, raw, coordinate, landing = row
    return RecordedCaptureErasure(
        record=CaptureErasureRecord.model_validate_json(raw),
        coordinate=AcceptedCoordinate.model_validate_json(coordinate),
        sequence=int(sequence),
        landing=None if landing is None else ErasureLandingPosition.model_validate_json(landing),
    )


_ROW = "e.sequence, e.record, e.coordinate, e.landing"


class CaptureErasureStore:
    """The erasure journal and its disposable SQLite projection."""

    def __init__(self, instance: Any) -> None:
        self.instance = instance
        self.root = _root(instance)
        self.path = self.root / "erasures.sqlite3"
        self.stream = _stream(instance)
        self._bodies: Any = None

    @classmethod
    def reader(cls, *, exhaust_root: Path, instance_id: str, bodies: Any) -> CaptureErasureStore:
        """A read-only view from the parts a verification context holds (it never records)."""

        store = cls.__new__(cls)
        store.instance = None
        store.root = exhaust_root / ERASURE_DIRECTORY
        store.path = store.root / "erasures.sqlite3"
        store.stream = JournalStreamIdentityV1(
            instance_id=instance_id,
            journal_family=CAPTURE_ERASURE_JOURNAL_FAMILY,
            stream_id="erasures",
        )
        store._bodies = bodies
        return store

    def _body_store(self) -> Any:
        return self._bodies if self._bodies is not None else self.instance.body_store()

    def _journal(self, *, create: bool) -> LocalJournalBackend | None:
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return LocalJournalBackend(self.root) if self.root.is_dir() else None

    @contextmanager
    def _locked(self, *, create: bool = False) -> Iterator[sqlite3.Connection | None]:
        """The projection, caught up from the journal; None when nothing was ever erased."""

        with _LOCK:
            journal = self._journal(create=create)
            if journal is None:
                yield None
                return
            if self.path.is_symlink():
                raise ValueError("Capture erasure projection must be a regular file")
            conn = sqlite3.connect(self.path)
            try:
                if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                    for table in ("progress", "erasures", "erased_bodies"):
                        conn.execute(f"DROP TABLE IF EXISTS {table}")
                    conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT sequence FROM progress WHERE singleton=1").fetchone()
                for entry in self._read_entries(journal, 0 if row is None else row[0]):
                    self._apply(conn, entry)
                # A preview reads the caught-up projection and leaves the file as it was.
                keep = conn.rollback if is_previewing() else conn.commit
                keep()
                yield conn
                keep()
            finally:
                conn.close()

    def _read_after(
        self, journal: LocalJournalBackend, sequence: int, *, limit: int | None = None
    ) -> Iterator[tuple[int, CaptureErasureRecord]]:
        """Each journal record after ``sequence``: one payload read per record."""

        for entry in self._read_entries(journal, sequence, limit=limit):
            yield entry.sequence, entry.record

    def _read_entries(
        self, journal: LocalJournalBackend, sequence: int, *, limit: int | None = None
    ) -> Iterator[RecordedCaptureErasure]:
        """Each journal record after ``sequence``, with its position and accepted head."""

        bodies = self._body_store()
        for count, stored in enumerate(
            journal.select_records(
                self.stream, partition_id=_PARTITION, first_sequence=sequence + 1
            )
        ):
            if limit is not None and count >= limit:
                return
            payload = parse_journal_payload(
                bodies.read(stored.record.payload_digest, access=_ACCESS)
            )
            record, landing = _record(payload, stored.record.recorded_at)
            yield RecordedCaptureErasure(
                record=record,
                coordinate=stored.record.accepted_coordinate,
                sequence=stored.record.sequence,
                landing=landing,
            )

    @staticmethod
    def _apply(conn: sqlite3.Connection, entry: RecordedCaptureErasure) -> None:
        record, sequence = entry.record, entry.sequence
        conn.execute(
            "INSERT OR IGNORE INTO erasures VALUES (?,?,?,?,?)",
            (
                sequence,
                record.capture_digest,
                record.model_dump_json(),
                entry.coordinate.model_dump_json(),
                None if entry.landing is None else entry.landing.model_dump_json(),
            ),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO erased_bodies VALUES (?,?,?)",
            ((body, sequence, record.capture_digest) for body in record.body_digests),
        )
        conn.execute("INSERT OR REPLACE INTO progress VALUES (1,?)", (sequence,))

    def _latest(
        self, conn: sqlite3.Connection, capture_digest: str
    ) -> RecordedCaptureErasure | None:
        row = conn.execute(
            f"SELECT {_ROW} FROM erasures e WHERE e.capture_digest=? "
            "ORDER BY e.sequence DESC LIMIT 1",
            (capture_digest,),
        ).fetchone()
        return None if row is None else _recorded(row)

    def _retries(self, entry: RecordedCaptureErasure) -> bool:
        """Whether erasing these bytes now finishes ``entry``: its positions prove it.

        ``entry`` destroyed what had landed by its recorded landing position; the
        bytes present now are the same ones (an interrupted erasure being retried)
        only when none of its bodies landed again after that position, in the log
        it was recorded in. A record without a landing position (written before
        positions were kept), or one whose log was replaced, cannot show that:
        the bytes present now are erased under a record of their own.
        """

        landing = entry.landing
        log = getattr(self._body_store(), "landings", None)
        if landing is None or log is None:
            return False
        head = log.head()
        if head.identity != landing.log:
            return False
        for body in entry.record.body_digests:
            last = log.last_completion_at(body, identity=head.identity, sequence=head.sequence)
            if last is not None and last[1] == "stored" and last[0] > landing.sequence:
                return False
        return True

    def unfinished(self, capture_digest: str) -> CaptureErasureRecord | None:
        """The latest erasure of this Capture that erasing its bytes now would finish.

        Its retry finishes the unlink under it; None when this Capture was never
        erased, or its bytes may have been stored again since (erasing them is a
        new erasure): they landed again, or the record cannot show they did not.
        """

        if not self.root.is_dir():
            return None
        with self._locked() as conn:
            if conn is None:
                return None
            latest = self._latest(conn, capture_digest)
        return None if latest is None or not self._retries(latest) else latest.record

    # -- writes -------------------------------------------------------------

    def record(
        self, record: CaptureErasureRecord, *, actor_id: str, coordinate: AcceptedCoordinate
    ) -> CaptureErasureRecord:
        """Append one erasure record, or answer the unfinished one these bytes still have.

        A Capture erased before whose bytes landed again since (stored anew,
        by any Capture of them), or whose record cannot show they did not, is
        erased again: a new record at its own journal and landing position. Its
        instant is the instance's recording clock at the append, raised to the
        last record's, never a caller's. Nothing is written while previewing.
        The caller unlinks the bytes after this returns.
        """

        if is_previewing():
            raise RuntimeError("a preview records no erasure")
        with self._locked(create=True) as conn:
            assert conn is not None
            known = self._latest(conn, record.capture_digest)
            if known is not None and self._retries(known):
                return known.record
            journal = self._journal(create=True)
            assert journal is not None
            from cruxible_core.storage.material_reservations import (
                ProcedureMaterialReservationStore,
            )

            bodies = self.instance.body_store()
            ProcedureMaterialReservationStore(bodies.reservation_root).recover_run_material(
                lambda reservation: journal.select_records(
                    self.stream, event_kind=_EVENT_KIND, payload_digest=reservation.body_digest
                ),
                bodies=bodies,
                intended_event_kinds=frozenset({_EVENT_KIND}),
            )
            head = journal.read_head(self.stream, _PARTITION)
            journal.activate_writer(
                self.stream, _PARTITION, fencing_token=_FENCE, expected_head=head
            )
            now = self.instance.recording_clock()
            last = conn.execute(
                "SELECT record FROM erasures ORDER BY sequence DESC LIMIT 1"
            ).fetchone()  # the newest record (the primary key is the journal position)
            if last is not None:
                now = max(now, CaptureErasureRecord.model_validate_json(last[0]).erased_at)
            payload: dict[str, Any] = record.model_dump(mode="json", exclude={"erased_at"})
            # The landing log's position as the record lands, before any byte goes:
            # a Capture landed by then held what this erasure destroys.
            landed = bodies.landings.head() if hasattr(bodies, "landings") else None
            if landed is not None and landed.identity:
                payload[_LANDING] = ErasureLandingPosition(
                    log=landed.identity, sequence=landed.sequence
                ).model_dump(mode="json")
            stored = ProcedureExhaustWriter(
                journal=journal, bodies=bodies, fencing_token=_FENCE
            ).append(
                stream=self.stream,
                partition_id=_PARTITION,
                event_kind=_EVENT_KIND,
                accepted_coordinate=coordinate,
                definition_digest=self.stream.identity_digest,
                actor_context=GovernedActorContext(
                    actor_type="human_user",
                    actor_id=actor_id,
                    org_id=self.instance.descriptor.instance_id,
                    operation_id=new_id("op", length=16, separator="_"),
                    timestamp=now,
                ),
                recorded_at=now,
                payload=payload,
                expected_head=head,
            )
            written, landing = _record(payload, stored.record.recorded_at)
            self._apply(
                conn,
                RecordedCaptureErasure(
                    record=written,
                    coordinate=stored.record.accepted_coordinate,
                    sequence=stored.record.sequence,
                    landing=landing,
                ),
            )
            conn.commit()
            return written

    # -- reads --------------------------------------------------------------

    def for_capture(self, capture_digest: str) -> CaptureErasureRecord | None:
        """The latest erasure recorded for this Capture, if any: one indexed row."""

        if not self.root.is_dir():
            return None
        with self._locked() as conn:
            if conn is None:
                return None
            latest = self._latest(conn, capture_digest)
        return None if latest is None else latest.record

    def for_bodies(self, body_digests: tuple[str, ...]) -> dict[str, CaptureErasureRecord]:
        """The latest erasure recorded for each of these body objects that has one.

        A body is content-addressed: erasing one Capture's body erases the same
        bytes for every Capture that holds them, so the record is found by body.
        """

        if not body_digests or not self.root.is_dir():
            return {}
        found: dict[str, CaptureErasureRecord] = {}
        with self._locked() as conn:
            if conn is None:
                return {}
            for body in dict.fromkeys(body_digests):
                row = conn.execute(
                    "SELECT e.record FROM erased_bodies b JOIN erasures e "
                    "ON e.sequence=b.sequence WHERE b.body_digest=? "
                    "ORDER BY b.sequence DESC LIMIT 1",
                    (body,),
                ).fetchone()
                if row is not None:
                    found[body] = CaptureErasureRecord.model_validate_json(row[0])
        return found

    def records_for_bodies(
        self, body_digests: tuple[str, ...]
    ) -> tuple[RecordedCaptureErasure, ...]:
        """Every erasure naming any of these body objects, each occurrence, in journal order.

        One indexed range per body; an instance that never erased anything
        answers without opening a file.
        """

        if not body_digests or not self.root.is_dir():
            return ()
        found: dict[int, RecordedCaptureErasure] = {}
        with self._locked() as conn:
            if conn is None:
                return ()
            for body in dict.fromkeys(body_digests):
                for row in conn.execute(
                    f"SELECT {_ROW} FROM erased_bodies b "
                    "JOIN erasures e ON e.sequence=b.sequence WHERE b.body_digest=?",
                    (body,),
                ):
                    if int(row[0]) not in found:
                        found[int(row[0])] = _recorded(row)
        return tuple(found[sequence] for sequence in sorted(found))

    def head_sequence(self) -> int:
        """The journal's newest sequence (0 before any record): where a follower starts."""

        journal = self._journal(create=False)
        return 0 if journal is None else journal.read_head(self.stream, _PARTITION).sequence

    def records_after(
        self, sequence: int, *, limit: int
    ) -> tuple[tuple[int, CaptureErasureRecord], ...]:
        """At most ``limit`` journal records after ``sequence``, from the journal itself."""

        journal = self._journal(create=False)
        if journal is None:
            return ()
        return tuple(self._read_after(journal, sequence, limit=limit))


def capture_landed_by(bodies: Any, capture_digest: str, erasure: RecordedCaptureErasure) -> bool:
    """Whether a Capture had landed when ``erasure`` was recorded: it held the bytes destroyed.

    Read from the landing log at the erasure's recorded position: the Capture's
    envelope present there. A Capture landed later holds bytes stored again, its
    own. A record without a position, or a store or log that cannot answer at
    it, counts the erasure (the material is taken to predate it): no clock
    stands in for the order.
    """

    landing = erasure.landing
    log = getattr(bodies, "landings", None)
    if landing is None or log is None or landing.sequence <= 0:
        return True
    try:
        return bool(log.present(capture_digest, identity=landing.log, sequence=landing.sequence))
    except (CruxibleError, OSError, ValueError):
        return True


def erasure_journal_present(instance: Any) -> bool:
    """Whether this instance ever recorded an erasure: one stat, no open."""

    return journal_log_path(_root(instance), _stream(instance), _PARTITION).is_file()


def retained_body_digests(envelope: Any) -> tuple[str, ...]:
    """The body objects a Capture keeps in the store: its CAS commitment and CAS source."""

    return capture_retained_body_digests(envelope)


def erased_body(instance: Any, envelope: Any) -> CaptureErasureRecord | None:
    """The erasure behind a Capture's missing body, or None when nothing it keeps was erased.

    A body counts as erased while an erasure record names it and it is absent
    at the read's landing position. No erasure was ever recorded: one stat.
    """

    if not erasure_journal_present(instance):
        return None
    bodies = retained_body_digests(envelope)
    store = instance.body_store()
    missing = tuple(body for body in bodies if body_present(store, body) is False)
    if not missing:
        return None
    records = CaptureErasureStore(instance).for_bodies(missing)
    for body in missing:
        if body in records:
            return records[body]
    return None


#: The contracts a Claim's own authoring produces: its own words, never evidence about the world.
OWN_WORDS_CONTRACT_DIGESTS = frozenset(
    capture_contract_digest(contract).tagged
    for contract in (
        DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    )
)


def citing_live_claims(connection: Any, capture_digest: str) -> list[str]:
    """Every live Claim citing the Capture: one indexed range."""

    return [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT u.owner_key FROM citation_uses u INDEXED BY citations_by_capture "
            "JOIN claims c ON c.identity=u.owner_key "
            "WHERE u.capture_digest=? AND u.owner_kind='Claim' AND c.lifecycle='live' "
            "ORDER BY u.owner_key",
            (capture_digest,),
        )
    ]


def evidence_captures(connection: Any, claim_identity: str) -> list[str]:
    """The Captures a Claim cites other than its own words: one indexed range."""

    return [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT u.capture_digest, k.contract_digest FROM citation_uses u "
            "LEFT JOIN captures k ON k.capture_digest=u.capture_digest "
            "WHERE u.owner_kind='Claim' AND u.owner_key=? ORDER BY u.capture_digest",
            (claim_identity,),
        )
        if row[1] not in OWN_WORDS_CONTRACT_DIGESTS
    ]


def capture_erased(
    instance: Any, capture_digest: str, *, erasing: frozenset[str] = frozenset()
) -> bool:
    """Whether a Capture's retained body is erased (or among ``erasing``, about to be)."""

    try:
        envelope = capture_envelope(instance.body_store(), capture_digest, access=_ACCESS)
    except (CruxibleError, ValueError):
        return False
    bodies = retained_body_digests(envelope)
    if erasing.intersection(bodies):
        return True
    return erased_body(instance, envelope) is not None


def claims_left_without_evidence(
    instance: Any,
    connection: Any,
    claims: list[str],
    *,
    erasing: frozenset[str] = frozenset(),
) -> list[str]:
    """The Claims whose every cited Capture, own words aside, has an erased body."""

    left: list[str] = []
    for claim in claims:
        cited = evidence_captures(connection, claim)
        if cited and all(capture_erased(instance, item, erasing=erasing) for item in cited):
            left.append(claim)
    return left


__all__ = [
    "ERASURE_DIRECTORY",
    "OWN_WORDS_CONTRACT_DIGESTS",
    "CaptureErasureStore",
    "capture_erased",
    "capture_landed_by",
    "citing_live_claims",
    "claims_left_without_evidence",
    "erased_body",
    "evidence_captures",
    "erasure_journal_present",
    "retained_body_digests",
]
