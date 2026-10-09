"""The Line inbox: retained postings and dispositions, and their rebuildable index.

The journal owns every transition: a ``posted`` batch (what one terminal of one
run posted) and a ``disposition`` (an agent's decision about one item). SQLite
is a disposable projection of that journal, caught up from its last applied
sequence on every open, and never accepted state.

An item is keyed by its dedupe key (payload digest, source Capture digests and
Line). Posting an existing key creates nothing new: it counts the
re-occurrence, and an item already decided returns to ``unread`` with that
decision still on it. A posting is identified by its run, node and item key, so
a run that delivers the same terminal twice (recovery) counts once.

Every read is keyed: an item by id, a page by recency through an index, and the
unread volume per Line from a maintained count -- none of them grows with the
inbox's history.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.line_inbox import (
    InboxDisposition,
    InboxItemEnvelope,
    InboxItemStatus,
    inbox_item_id,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.exhaust.records import (
    LINE_INBOX_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    parse_journal_payload,
)
from cruxible_core.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.preview_fence import is_previewing

_LOCK = threading.RLock()
_FENCE = "line-inbox-local-v1"
_PARTITION = "inbox"
_EVENT_TAG = "cruxible-line-inbox-transition-v1"
_DISPOSITION: TypeAdapter[InboxDisposition] = TypeAdapter(InboxDisposition)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (singleton INTEGER PRIMARY KEY, sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS items (
 item_id TEXT PRIMARY KEY, line TEXT NOT NULL, status TEXT NOT NULL, last_seen TEXT NOT NULL,
 envelope TEXT NOT NULL, payload TEXT NOT NULL, dispositions TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS items_recent ON items(last_seen, item_id);
CREATE INDEX IF NOT EXISTS items_status ON items(status, last_seen, item_id);
CREATE INDEX IF NOT EXISTS items_line ON items(line, last_seen, item_id);
CREATE INDEX IF NOT EXISTS items_line_status ON items(line, status, last_seen, item_id);
CREATE TABLE IF NOT EXISTS postings (
 run_id TEXT NOT NULL, node_id TEXT NOT NULL, item_key TEXT NOT NULL, item_id TEXT NOT NULL,
 PRIMARY KEY(run_id, node_id, item_key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS unread (line TEXT PRIMARY KEY, count INTEGER NOT NULL);
"""


class InboxItemNotFound(LookupError):
    """No inbox item has this id, or a selector prefix names none or several."""

    def __init__(self, selector: str, *, ambiguous: bool = False) -> None:
        super().__init__(selector)
        self.selector = selector
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class InboxPosting:
    """One item one terminal of one run posts, with what the daemon attached to it."""

    line: str
    procedure: str
    procedure_artifact_digest: str
    run_id: str
    node_id: str
    item_key: str
    payload: CanonicalValue
    source_capture_digests: tuple[str, ...]
    posted_at: datetime


@dataclass(frozen=True)
class StoredInboxItem:
    envelope: InboxItemEnvelope
    payload: CanonicalValue
    dispositions: tuple[InboxDisposition, ...]


def _instant(value: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError("Line inbox transition carries no instant")
    return parsed


def inbox_payload_digest(payload: CanonicalValue) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()


def inbox_dedupe_key(
    *, line: str, payload_digest: str, source_capture_digests: Sequence[str]
) -> str:
    """The item a posting lands in: the same payload, on the same evidence, from the same Line."""

    return typed_digest(
        Sha256Value,
        "cruxible-line-inbox-dedupe-v1",
        {
            "line": line,
            "payload_digest": payload_digest,
            "source_capture_digests": sorted(set(source_capture_digests)),
        },
    ).tagged


def inbox_root(instance: PlaybillInstance) -> Path:
    return instance.root / instance.descriptor.storage.exhaust / "line-inbox"


def _encode_cursor(last_seen: str, item_id: str) -> str:
    raw = json.dumps([last_seen, item_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_inbox_cursor(cursor: str) -> tuple[str, str]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise ValueError("inbox cursor is not one this list returned") from exc
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError("inbox cursor is not one this list returned")
    return value[0], value[1]


class LineInboxStore:
    """The Line inbox journal and its disposable SQLite projection.

    Inside a preview (``previewing()``) the store writes nothing: the projection
    is an in-memory copy of the one on disk, and a transition is applied to that
    copy only, never appended to the journal.
    """

    def __init__(self, instance: PlaybillInstance):
        self.instance = instance
        self.root = inbox_root(instance)
        self.path = self.root / "inbox.sqlite3"
        self.stream = JournalStreamIdentityV1(
            instance_id=instance.descriptor.instance_id,
            journal_family=LINE_INBOX_JOURNAL_FAMILY,
            stream_id="inbox",
        )
        self._preview_sequence = 0

    def _journal(self, *, create: bool) -> LocalJournalBackend | None:
        if create and not is_previewing():
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return LocalJournalBackend(self.root) if self.root.is_dir() else None

    def _open_projection(self) -> sqlite3.Connection:
        if not is_previewing() and self.root.is_dir():
            return sqlite3.connect(self.path)
        conn = sqlite3.connect(":memory:")
        if self.path.is_file():
            source = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                source.backup(conn)
            finally:
                source.close()
        return conn

    @contextmanager
    def locked(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        """The projection, caught up from the journal; ``create`` makes the store's root."""

        with _LOCK:
            journal = self._journal(create=create)
            if self.path.is_symlink():
                raise ValueError("inbox projection must be a regular file")
            conn = self._open_projection()
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT sequence FROM progress WHERE singleton=1").fetchone()
                start = row[0] + 1 if row else 1
                self._preview_sequence = start - 1
                for stored in (
                    ()
                    if journal is None
                    else journal.select_records(
                        self.stream, partition_id=_PARTITION, first_sequence=start
                    )
                ):
                    payload = parse_journal_payload(
                        self.instance.body_store().read(
                            stored.record.payload_digest,
                            access=BodyAccessContext(
                                principal_id="line-inbox-projection", can_read_body=True
                            ),
                        )
                    )
                    if not isinstance(payload, dict) or payload.get("tag") != _EVENT_TAG:
                        raise ValueError("invalid Line inbox transition")
                    self._apply(conn, payload, stored.record.sequence)
                    self._preview_sequence = stored.record.sequence
                conn.commit()
                yield conn
                conn.commit()
            finally:
                conn.close()

    # -- projection --------------------------------------------------------

    def _apply(self, conn: sqlite3.Connection, event: dict[str, Any], sequence: int) -> None:
        kind, data = event["kind"], event["data"]
        if kind == "posted":
            for posting in data["items"]:
                self._apply_posting(conn, posting)
        elif kind == "disposition":
            self._apply_disposition(conn, data)
        else:
            raise ValueError("unknown Line inbox transition")
        conn.execute("INSERT OR REPLACE INTO progress VALUES(1,?)", (sequence,))

    @staticmethod
    def _bump_unread(conn: sqlite3.Connection, line: str, delta: int) -> None:
        conn.execute(
            "INSERT INTO unread(line,count) VALUES(?,?) "
            "ON CONFLICT(line) DO UPDATE SET count=count+excluded.count",
            (line, delta),
        )
        conn.execute("DELETE FROM unread WHERE line=? AND count<=0", (line,))

    def _apply_posting(self, conn: sqlite3.Connection, posting: dict[str, Any]) -> None:
        item_id = posting["item_id"]
        cursor = conn.execute(
            "INSERT OR IGNORE INTO postings VALUES(?,?,?,?)",
            (posting["run_id"], posting["node_id"], posting["item_key"], item_id),
        )
        if cursor.rowcount == 0:
            return  # The same run delivered this item already.
        instant = _instant(posting["posted_at"])
        posted_at = format_datetime(instant)
        row = conn.execute(
            "SELECT envelope,status FROM items WHERE item_id=?", (item_id,)
        ).fetchone()
        if row is None:
            envelope = InboxItemEnvelope(
                item_id=item_id,
                status="unread",
                line=posting["line"],
                procedure=posting["procedure"],
                procedure_artifact_digest=posting["procedure_artifact_digest"],
                node_id=posting["node_id"],
                run_id=posting["run_id"],
                posted_at=instant,
                last_run_id=posting["run_id"],
                last_posted_at=instant,
                occurrences=1,
                source_capture_digests=tuple(posting["source_capture_digests"]),
                dedupe_key=posting["dedupe_key"],
                payload_digest=posting["payload_digest"],
                payload_bytes=len(canonical_bytes(posting["payload"])),
            )
            conn.execute(
                "INSERT INTO items VALUES(?,?,?,?,?,?,?)",
                (
                    item_id,
                    envelope.line,
                    "unread",
                    posted_at,
                    envelope.model_dump_json(),
                    canonical_bytes(posting["payload"]).decode("utf-8"),
                    "[]",
                ),
            )
            self._bump_unread(conn, envelope.line, 1)
            return
        current = InboxItemEnvelope.model_validate_json(row["envelope"])
        latest = instant >= current.last_posted_at
        updated = current.model_copy(
            update={
                "occurrences": current.occurrences + 1,
                "last_posted_at": instant if latest else current.last_posted_at,
                "last_run_id": posting["run_id"] if latest else current.last_run_id,
                "status": "unread",
                "recurred_since_disposition": current.recurred_since_disposition
                + (current.disposition is not None),
            }
        )
        conn.execute(
            "UPDATE items SET status='unread',last_seen=?,envelope=? WHERE item_id=?",
            (
                format_datetime(updated.last_posted_at),
                InboxItemEnvelope.model_validate(updated.model_dump()).model_dump_json(),
                item_id,
            ),
        )
        if row["status"] != "unread":
            self._bump_unread(conn, current.line, 1)

    def _apply_disposition(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        item_id = data["item_id"]
        disposition = _DISPOSITION.validate_python(data["disposition"])
        row = conn.execute(
            "SELECT envelope,status,dispositions FROM items WHERE item_id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Line inbox disposition names no posted item")
        current = InboxItemEnvelope.model_validate_json(row["envelope"])
        updated = InboxItemEnvelope.model_validate(
            {
                **current.model_dump(),
                "status": disposition.disposition,
                "disposition": disposition.model_dump(),
                "recurred_since_disposition": 0,
            }
        )
        history = json.loads(row["dispositions"])
        history.append(disposition.model_dump(mode="json"))
        conn.execute(
            "UPDATE items SET status=?,envelope=?,dispositions=? WHERE item_id=?",
            (
                disposition.disposition,
                updated.model_dump_json(),
                json.dumps(history, separators=(",", ":"), sort_keys=True),
                item_id,
            ),
        )
        if row["status"] == "unread":
            self._bump_unread(conn, current.line, -1)

    # -- writes ------------------------------------------------------------

    def _append(
        self,
        conn: sqlite3.Connection,
        kind: str,
        data: dict[str, Any],
        *,
        actor: GovernedActorContext,
        now: datetime,
        coordinate: AcceptedCoordinate,
    ) -> None:
        event = {"tag": _EVENT_TAG, "kind": kind, "data": data}
        if is_previewing():
            self._preview_sequence += 1
            self._apply(conn, event, self._preview_sequence)
            return
        journal = self._journal(create=True)
        assert journal is not None
        from cruxible_core.storage.material_reservations import ProcedureMaterialReservationStore

        bodies = self.instance.body_store()
        ProcedureMaterialReservationStore(bodies.reservation_root).recover_run_material(
            lambda reservation: journal.select_records(
                self.stream,
                event_kind="line_inbox_transition",
                payload_digest=reservation.body_digest,
            ),
            bodies=bodies,
            intended_event_kinds=frozenset({"line_inbox_transition"}),
        )
        head = journal.read_head(self.stream, _PARTITION)
        journal.activate_writer(self.stream, _PARTITION, fencing_token=_FENCE, expected_head=head)
        stored = ProcedureExhaustWriter(
            journal=journal, bodies=bodies, fencing_token=_FENCE
        ).append(
            stream=self.stream,
            partition_id=_PARTITION,
            event_kind="line_inbox_transition",
            accepted_coordinate=coordinate,
            definition_digest=self.stream.identity_digest,
            actor_context=actor,
            recorded_at=now,
            payload=event,
            expected_head=head,
        )
        self._apply(conn, event, stored.record.sequence)
        conn.commit()

    def post(
        self,
        postings: Sequence[InboxPosting],
        *,
        actor: GovernedActorContext,
        now: datetime,
        coordinate: AcceptedCoordinate,
    ) -> tuple[str, ...]:
        """Post one terminal's items; return each one's dedupe key, in order.

        A posting this run already delivered is not journaled again.
        """

        keys: list[str] = []
        fresh: list[dict[str, Any]] = []
        with self.locked(create=True) as conn:
            for posting in postings:
                payload_digest = inbox_payload_digest(posting.payload)
                captures = tuple(sorted(set(posting.source_capture_digests)))
                key = inbox_dedupe_key(
                    line=posting.line,
                    payload_digest=payload_digest,
                    source_capture_digests=captures,
                )
                keys.append(key)
                seen = conn.execute(
                    "SELECT 1 FROM postings WHERE run_id=? AND node_id=? AND item_key=?",
                    (posting.run_id, posting.node_id, posting.item_key),
                ).fetchone()
                if seen is not None:
                    continue
                fresh.append(
                    {
                        "item_id": inbox_item_id(key),
                        "dedupe_key": key,
                        "payload_digest": payload_digest,
                        "payload": posting.payload,
                        "line": posting.line,
                        "procedure": posting.procedure,
                        "procedure_artifact_digest": posting.procedure_artifact_digest,
                        "run_id": posting.run_id,
                        "node_id": posting.node_id,
                        "item_key": posting.item_key,
                        "posted_at": format_datetime(posting.posted_at),
                        "source_capture_digests": list(captures),
                    }
                )
            if fresh:
                self._append(
                    conn,
                    "posted",
                    {"items": fresh},
                    actor=actor,
                    now=now,
                    coordinate=coordinate,
                )
        return tuple(keys)

    def record_disposition(
        self,
        item_id: str,
        disposition: InboxDisposition,
        *,
        actor: GovernedActorContext,
        now: datetime,
        coordinate: AcceptedCoordinate,
    ) -> tuple[InboxItemEnvelope, bool]:
        """Record one disposition; a repeat of the item's current decision records nothing."""

        with self.locked(create=True) as conn:
            row = conn.execute("SELECT envelope FROM items WHERE item_id=?", (item_id,)).fetchone()
            if row is None:
                raise InboxItemNotFound(item_id)
            current = InboxItemEnvelope.model_validate_json(row["envelope"])
            if current.status != "unread" and _same_decision(current.disposition, disposition):
                return current, False
            self._append(
                conn,
                "disposition",
                {"item_id": item_id, "disposition": disposition.model_dump(mode="json")},
                actor=actor,
                now=now,
                coordinate=coordinate,
            )
            row = conn.execute("SELECT envelope FROM items WHERE item_id=?", (item_id,)).fetchone()
            return InboxItemEnvelope.model_validate_json(row["envelope"]), True

    # -- reads -------------------------------------------------------------

    def resolve(self, selector: str) -> str:
        """The one item id a full id, dedupe key or ``INB-`` prefix names."""

        candidate = (
            "INB-" + selector.removeprefix("sha256:")
            if selector.startswith("sha256:")
            else selector
        )
        with self.locked() as conn:
            rows = conn.execute(
                "SELECT item_id FROM items WHERE item_id>=? AND item_id<? ORDER BY item_id LIMIT 2",
                (candidate, candidate + "~"),
            ).fetchall()
        if len(rows) != 1:
            raise InboxItemNotFound(selector, ambiguous=len(rows) > 1)
        return str(rows[0][0])

    def get(self, item_id: str) -> StoredInboxItem:
        with self.locked() as conn:
            row = conn.execute(
                "SELECT envelope,payload,dispositions FROM items WHERE item_id=?", (item_id,)
            ).fetchone()
        if row is None:
            raise InboxItemNotFound(item_id)
        return StoredInboxItem(
            envelope=InboxItemEnvelope.model_validate_json(row["envelope"]),
            payload=json.loads(row["payload"]),
            dispositions=tuple(
                _DISPOSITION.validate_python(item) for item in json.loads(row["dispositions"])
            ),
        )

    def page(
        self,
        *,
        line: str | None,
        status: InboxItemStatus | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[tuple[StoredInboxItem, ...], str | None]:
        """One page, most recently posted first; a full page returns the next cursor."""

        clauses: list[str] = []
        params: list[object] = []
        if line is not None:
            clauses.append("line=?")
            params.append(line)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if cursor is not None:
            last_seen, item_id = decode_inbox_cursor(cursor)
            clauses.append("(last_seen<? OR (last_seen=? AND item_id<?))")
            params.extend((last_seen, last_seen, item_id))
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self.locked() as conn:
            rows = conn.execute(
                "SELECT item_id,last_seen,envelope,payload FROM items "
                f"{where}ORDER BY last_seen DESC, item_id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = tuple(
            StoredInboxItem(
                envelope=InboxItemEnvelope.model_validate_json(row["envelope"]),
                payload=json.loads(row["payload"]),
                dispositions=(),
            )
            for row in rows
        )
        next_cursor = (
            _encode_cursor(rows[-1]["last_seen"], rows[-1]["item_id"]) if more and rows else None
        )
        return items, next_cursor

    def unread_by_line(self) -> tuple[tuple[str, int], ...]:
        """Each Line with unread items and how many, by Line."""

        if not self.root.is_dir():
            return ()
        with self.locked() as conn:
            return tuple(
                (str(row[0]), int(row[1]))
                for row in conn.execute("SELECT line,count FROM unread ORDER BY line")
            )


def _same_decision(current: InboxDisposition | None, new: InboxDisposition) -> bool:
    if current is None or current.disposition != new.disposition:
        return False
    ignore = {"actor_id", "decided_at"}
    return current.model_dump(mode="json", exclude=ignore) == new.model_dump(
        mode="json", exclude=ignore
    )


__all__ = [
    "InboxItemNotFound",
    "InboxPosting",
    "LineInboxStore",
    "StoredInboxItem",
    "decode_inbox_cursor",
    "inbox_dedupe_key",
    "inbox_payload_digest",
    "inbox_root",
]
