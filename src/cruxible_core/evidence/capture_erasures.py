"""Recorded Capture erasures: which retained body was destroyed, by whom, when and why.

Storage follows the re-observation journal's pattern: a journal
(``capture-erasure-v1``) is the append-only authority, and a SQLite projection
keyed by journal position, Capture and body is caught up from its last applied
sequence on every open. An erase deletes the bytes and appends one record: the
record lands first, so an interrupted erase leaves it and the body, and erasing
again (like erasing bytes stored again after an erasure) appends another.

A record outlives the bytes it names. Whether a Capture reads as erased is the
record together with the body store: the body must also be absent at the read's
landing position (``erased_body``), so a body that lands again (the same bytes
captured anew) reads as present, as the landing log says it is; a body absent
with no record naming it is a defect.

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
_SCHEMA_VERSION = 4
ERASURE_DIRECTORY = "capture-erasures"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), sequence INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS erasures (
 sequence INTEGER PRIMARY KEY, capture_digest TEXT NOT NULL, record TEXT NOT NULL,
 coordinate TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS erasures_by_capture ON erasures(capture_digest, sequence);
CREATE TABLE IF NOT EXISTS erased_bodies (
 body_digest TEXT NOT NULL, sequence INTEGER NOT NULL, capture_digest TEXT NOT NULL,
 PRIMARY KEY(body_digest, sequence)
) STRICT, WITHOUT ROWID;
"""


def _root(instance: Any) -> Path:
    return Path(instance.root) / str(instance.descriptor.storage.exhaust) / ERASURE_DIRECTORY


def _stream(instance: Any) -> JournalStreamIdentityV1:
    return JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=CAPTURE_ERASURE_JOURNAL_FAMILY,
        stream_id="erasures",
    )


def _record(payload: object, recorded_at: datetime) -> CaptureErasureRecord:
    if not isinstance(payload, dict):
        raise ValueError("invalid Capture erasure record")
    # The journal record's own instant is the erasure's: the payload never restates it.
    return CaptureErasureRecord.model_validate({**payload, "erased_at": recorded_at})


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
            yield RecordedCaptureErasure(
                record=_record(payload, stored.record.recorded_at),
                coordinate=stored.record.accepted_coordinate,
                sequence=stored.record.sequence,
            )

    @staticmethod
    def _apply(conn: sqlite3.Connection, entry: RecordedCaptureErasure) -> None:
        record, sequence = entry.record, entry.sequence
        conn.execute(
            "INSERT OR IGNORE INTO erasures VALUES (?,?,?,?)",
            (
                sequence,
                record.capture_digest,
                record.model_dump_json(),
                entry.coordinate.model_dump_json(),
            ),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO erased_bodies VALUES (?,?,?)",
            ((body, sequence, record.capture_digest) for body in record.body_digests),
        )
        conn.execute("INSERT OR REPLACE INTO progress VALUES (1,?)", (sequence,))

    # -- writes -------------------------------------------------------------

    def record(
        self, record: CaptureErasureRecord, *, actor_id: str, coordinate: AcceptedCoordinate
    ) -> CaptureErasureRecord:
        """Append one erasure record; the caller unlinks the bytes after this returns.

        Its instant is the instance's recording clock at the append, raised to
        the last record's, never a caller's. Nothing is written while previewing.
        """

        if is_previewing():
            raise RuntimeError("a preview records no erasure")
        with self._locked(create=True) as conn:
            assert conn is not None
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
            written = _record(payload, stored.record.recorded_at)
            self._apply(
                conn,
                RecordedCaptureErasure(
                    record=written,
                    coordinate=stored.record.accepted_coordinate,
                    sequence=stored.record.sequence,
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
            row = conn.execute(
                "SELECT record FROM erasures WHERE capture_digest=? ORDER BY sequence DESC LIMIT 1",
                (capture_digest,),
            ).fetchone()
        return None if row is None else CaptureErasureRecord.model_validate_json(row[0])

    def for_bodies(self, body_digests: tuple[str, ...]) -> dict[str, RecordedCaptureErasure]:
        """The latest erasure recorded for each of these body objects that has one.

        A body is content-addressed: erasing one Capture's body erases the same
        bytes for every Capture that holds them, so the record is found by body.
        One indexed row per body asked, never a walk of the journal's history.
        """

        if not body_digests or not self.root.is_dir():
            return {}
        found: dict[str, RecordedCaptureErasure] = {}
        with self._locked() as conn:
            if conn is None:
                return {}
            for body in dict.fromkeys(body_digests):
                row = conn.execute(
                    "SELECT e.sequence, e.record, e.coordinate FROM erased_bodies b "
                    "JOIN erasures e ON e.sequence=b.sequence WHERE b.body_digest=? "
                    "ORDER BY b.sequence DESC LIMIT 1",
                    (body,),
                ).fetchone()
                if row is not None:
                    found[body] = RecordedCaptureErasure(
                        record=CaptureErasureRecord.model_validate_json(row[1]),
                        coordinate=AcceptedCoordinate.model_validate_json(row[2]),
                        sequence=int(row[0]),
                    )
        return found

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
            return records[body].record
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
    "citing_live_claims",
    "claims_left_without_evidence",
    "erased_body",
    "evidence_captures",
    "erasure_journal_present",
    "retained_body_digests",
]
