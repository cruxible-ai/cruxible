"""A rebuildable index of earlier refusals, found by statement and evidence.

Two kinds of decision are indexed under the Claim's refusal match key
(`claim_refusal_match_key`: subject, predicate, qualifier, object, and the
evidence commitment digests -- the content -- of the Captures its backing
cites):

* a Claim retired in accepted history, for any reason, indexed as accepted
  generations are caught up -- each generation once, from its change-set record;
* a Claim in a proposal a reviewer rejected, indexed at rejection from the
  statement and Capture digests the rejection record carries (taken while the
  candidate is still readable), keyed under the current match-key domain.

Everything here is derived: the file lives with the other projections and is
rebuilt from accepted history and the rejection records when it is missing,
damaged, belongs to another instance, or was keyed under another match-key
domain (an earlier layout is dropped first). A damaged file never blocks a
read: it is discarded and rebuilt, and if that fails too the read reports no
receipts and logs why.

Closing a proposal is serialized per proposal. A rejection row is written
before its record, so a crash between them leaves a row with no record; reads
keep a rejection row only while its record exists and says ``rejected``. Once
the close resolves -- written, lost to a competing close, or failed -- the
proposal's rows are rewritten from the record that actually won.

Settlements are counted per Line version, in post and never on delivery: a
background step (and `next`, before it reads the rows) reads settle deliveries
from the journal index after a persisted cursor, matches each settled item, and
upserts the Line's row for its exact spec digest together with the cursor, once
per settled proposal. A rebuild resets the cursor and counts the journal again,
so nothing is lost with the file.

Lookups are keyed: one probe per proposed Claim, never a scan of history.
Cruxible resurfaces what was recorded and nothing else: it does not interpret a
reason, refuse, or change settlement.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, TypeVar, cast

import structlog

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.candidates import CandidateMemberLawEvidence
from cruxible_client.contracts.captures import observed_content, parse_capture_envelope
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimArtifactAny,
    ClaimFormatError,
    parse_claim,
)
from cruxible_client.contracts.errors import CruxibleError, ProposalIntegrityError
from cruxible_client.contracts.proposal_models import ProposalWithdrawalRecord
from cruxible_client.contracts.refusal_receipts import (
    CLAIM_REFUSAL_MATCH_KEY_DOMAIN,
    PriorRefusalCoverage,
    PriorRefusalReceipt,
    RefusedClaim,
    RefusedClaimKeyV1,
    claim_producer,
    claim_refusal_match_key,
)
from cruxible_core.indexes.history.history_index import history_reads_detached
from cruxible_core.indexes.proposals.proposal_index import _micros
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.storage.preview_fence import is_previewing

if TYPE_CHECKING:
    from cruxible_core.indexes.history.history_index import HistoryReader
    from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
    from cruxible_core.proposals.settlement import ChangeSetRecordAnyVersion
    from cruxible_core.runtime.instance import PlaybillInstance

_SCHEMA = """
CREATE TABLE IF NOT EXISTS refusal_progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 instance_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence>=0),
 generation_root TEXT NOT NULL, key_domain TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS refusals (
 match_key TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('claim_retired','proposal_rejected')),
 source_id TEXT NOT NULL, claim_id TEXT NOT NULL, proposal_id TEXT,
 decided_by TEXT NOT NULL, decided_at TEXT NOT NULL, decided_at_us INTEGER NOT NULL,
 reason_code TEXT, rationale TEXT, producer TEXT,
 PRIMARY KEY(match_key,kind,source_id,claim_id)
) STRICT;
CREATE TABLE IF NOT EXISTS proposal_claim_keys (
 proposal_id TEXT NOT NULL, path TEXT NOT NULL, claim_id TEXT NOT NULL,
 match_key TEXT NOT NULL, producer TEXT,
 PRIMARY KEY(proposal_id,path)
) STRICT;
CREATE TABLE IF NOT EXISTS proposals_keyed (proposal_id TEXT PRIMARY KEY) STRICT;
CREATE TABLE IF NOT EXISTS line_settled_refusals (
 line_identity TEXT NOT NULL, line_spec_digest TEXT NOT NULL,
 items INTEGER NOT NULL CHECK(items>0), runs INTEGER NOT NULL CHECK(runs>0),
 latest_run_id TEXT NOT NULL, latest_settled_at TEXT NOT NULL,
 PRIMARY KEY(line_identity,line_spec_digest)
) STRICT;
CREATE TABLE IF NOT EXISTS settled_counted (proposal_id TEXT PRIMARY KEY) STRICT;
CREATE TABLE IF NOT EXISTS settled_cursor (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 journal_identity TEXT, record_id INTEGER NOT NULL CHECK(record_id>=0)
) STRICT;
"""
_ACCESS = BodyAccessContext(principal_id="cruxible-refusal-index", can_read_body=True)
_log = structlog.get_logger("cruxible.refusal_index")
_T = TypeVar("_T")
_SIDECARS = ("", "-wal", "-shm", "-journal")


def _damaged(exc: sqlite3.DatabaseError) -> bool:
    """Whether a SQLite failure means the derived file itself is unusable.

    Lock contention is transient and never discards the file; everything else
    SQLite raises against this cache (not a database, malformed image, a missing
    table or column of a torn layout) does.
    """

    if isinstance(exc, sqlite3.IntegrityError):
        # A constraint failure is a fault in what was written, not a damaged file.
        return False
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return not ("locked" in text or "busy" in text)
    return True


def _expected_schema() -> list[tuple[object, ...]]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_SCHEMA)
        return _schema(reference)
    finally:
        reference.close()


def _schema(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type,name"
        )
    ]


def capture_evidence_content(bodies: ContentAddressedBodyStore, capture_digest: str) -> str:
    """The content-only digest one cited Capture contributes to a match key.

    A Capture is what it observed, by content (``observed_content``): the bytes
    a workspace.file read and the body a web.fetch origin sent, which core
    records on the envelope when it builds the Capture and re-verifies with it;
    otherwise its ``commitment.digest`` -- the digest of the material it
    committed, independent of the run, the observation time, the contract and
    where the material is kept.
    """

    envelope = parse_capture_envelope(bodies.read(capture_digest, access=_ACCESS))
    return observed_content(envelope).digest


def claim_refusal_key(instance: PlaybillInstance, claim: ClaimArtifactAny) -> str:
    """A Claim's match key, over the content of the Captures it cites."""

    bodies = instance.body_store()
    return claim_refusal_match_key(
        claim.statement,
        {capture_evidence_content(bodies, digest) for digest in claim.backing.capture_digests},
    )


def _producer_text(pin: ArtifactPin | None) -> str | None:
    return None if pin is None else json.dumps(pin.model_dump(mode="json"), sort_keys=True)


def _producer(text: str | None) -> ArtifactPin | None:
    return None if text is None else ArtifactPin.model_validate(json.loads(text))


def _candidate_claims(
    instance: PlaybillInstance, proposal_id: str
) -> tuple[tuple[str, ClaimArtifactAny], ...] | None:
    """Every live Claim a proposal's candidate authors, by path; ``None`` if unreadable.

    Only authored members count (a generated successor restates a Claim the
    change did not propose), and only live ones (a retirement proposes no
    belief). The candidate tree must still be readable: a closed proposal's
    candidate may have been released.
    """

    evidence = instance.proposal_evidence()
    evaluation = evidence.read_evaluation(proposal_id)
    if evaluation.candidate_digest is None or evaluation.evaluated_tree_oid is None:
        return ()
    candidate = evidence.read_candidate(evaluation.candidate_digest)
    paths = tuple(
        member.path
        for member in candidate.members
        if member.artifact_kind == "claim"
        and (
            not isinstance(member, CandidateMemberLawEvidence)
            or (member.closure_role == "authored" and member.disposition in {"create", "replace"})
        )
    )
    if not paths:
        return ()
    try:
        tree = instance.proposal_blobs(
            evaluation.evaluated_tree_oid, paths, proposal_id=proposal_id
        )
    except Exception:  # noqa: BLE001 - a released candidate keys nothing
        return None
    claims: list[tuple[str, ClaimArtifactAny]] = []
    for path in sorted(paths, key=lambda item: item.encode("utf-8")):
        content = tree.get(path)
        if content is None:
            return None
        try:
            claim = parse_claim(content, path=path)
        except ClaimFormatError:
            continue
        if claim.lifecycle.state == "live":
            claims.append((path, claim))
    return tuple(claims)


def candidate_refused_claims(
    instance: PlaybillInstance, proposal_id: str
) -> tuple[RefusedClaim, ...] | None:
    """What a rejection records of each Claim its candidate proposed.

    The statement and the Capture digests it cites: the inputs of the match key,
    which stay readable after the candidate is released (Captures are retained),
    so the key is recomputed under whichever key domain reads the record.
    """

    claims = _candidate_claims(instance, proposal_id)
    if claims is None:
        return None
    return tuple(
        RefusedClaim(
            claim_id=claim.identity.name,
            path=path,
            statement=claim.statement,
            capture_digests=claim.backing.capture_digests,
            producer=claim_producer(claim),
        )
        for path, claim in claims
    )


def _rejection_keys(
    instance: PlaybillInstance, record: ProposalWithdrawalRecord
) -> tuple[tuple[RefusedClaim | RefusedClaimKeyV1, str], ...]:
    """Each refused Claim of a rejection with its key under the current domain.

    A recorded statement and its Captures are keyed afresh. A key-only entry
    (the first, unreleased record shape) is re-derived from the rejected
    candidate while its objects are retained; when they are not, it cannot be
    keyed in this domain and is dropped with a logged note.
    """

    bodies = instance.body_store()
    candidate: dict[str, str] | None = None
    keyed: list[tuple[RefusedClaim | RefusedClaimKeyV1, str]] = []
    for item in record.refused_claims:
        if isinstance(item, RefusedClaim):
            try:
                contents = {capture_evidence_content(bodies, d) for d in item.capture_digests}
            except CruxibleError as exc:
                _log.warning(
                    "refusal_index_rejection_unkeyable",
                    proposal_id=record.proposal_id,
                    claim_id=item.claim_id,
                    reason=str(exc),
                )
                continue
            keyed.append((item, claim_refusal_match_key(item.statement, contents)))
            continue
        if candidate is None:
            claims = _candidate_claims(instance, record.proposal_id)
            candidate = (
                {}
                if claims is None
                else {path: claim_refusal_key(instance, claim) for path, claim in claims}
            )
        key = candidate.get(item.path)
        if key is None:
            _log.warning(
                "refusal_index_rejection_dropped",
                proposal_id=record.proposal_id,
                claim_id=item.claim_id,
                reason="key-only record and its rejected candidate is no longer retained",
            )
            continue
        keyed.append((item, key))
    return tuple(keyed)


_BUSY_TIMEOUT_MS = 5000
_SettledRead: TypeAlias = tuple[
    dict[tuple[str, str], "LineSettledRefusals"], PriorRefusalCoverage | None
]
_ReceiptsRead: TypeAlias = tuple[
    dict[str, tuple[PriorRefusalReceipt, ...]], PriorRefusalCoverage | None
]
#: Accepted generations one catch-up indexes before returning (each its own transaction).
RETIREMENT_PAGE = 64


class _Deferred(Exception):
    """A background step that would have to wait, or build the index first."""


@dataclass(frozen=True)
class SettledDelivery:
    """One settle terminal's accepted delivery, as its journal records it."""

    line_identity: str
    line_spec_digest: str
    run_id: str
    proposal_id: str
    settled_at: str


@dataclass(frozen=True)
class SettledCursor:
    """Where counting stands in the journal index: a position under one identity."""

    journal_identity: str | None
    record_id: int


@dataclass(frozen=True)
class LineSettledRefusals:
    """Settled items of one Line version that matched earlier refusals."""

    line_identity: str
    line_spec_digest: str
    items: int
    runs: int
    latest_run_id: str
    latest_settled_at: str


class RefusalIndex:
    """The refusal rows of one instance, in one rebuildable SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._open: list[sqlite3.Connection] = []
        self._finalizer = weakref.finalize(self, _close_all, self._open)
        self._closing: dict[str, threading.Lock] = {}
        self._no_wait = False
        # Set by every catch-up: how far retirements are indexed, while lagging.
        self._coverage: PriorRefusalCoverage | None = None
        # Work counters, for counted-work tests.
        self.generations_indexed = 0
        self.rebuilds = 0
        self.key_probes = 0
        self.rows_read = 0
        self.proposals_keyed = 0
        self.line_probes = 0
        self.settled_counted = 0

    # -- connections ------------------------------------------------------

    def _writer(self) -> sqlite3.Connection:
        if not self._no_wait:
            return self._open_writer()
        if self._connection is None:
            # A non-waiting caller opens without waiting, and never builds,
            # repairs, drops or creates the layout: anything but the exact
            # schema is left to a reader allowed to wait.
            if not self.path.exists():
                raise _Deferred("index_not_built")
            connection = sqlite3.connect(
                self.path, check_same_thread=False, isolation_level=None, timeout=0
            )
            try:
                if _schema(connection) != _expected_schema():
                    raise _Deferred("index_layout_needs_repair")
            except BaseException:
                connection.close()
                raise
            self._open.append(connection)
            self._connection = connection
        # A non-waiting caller gives up at once on contention.
        self._connection.execute("PRAGMA busy_timeout=0")
        return self._connection

    def _open_writer(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                check_same_thread=False,
                isolation_level=None,
                timeout=_BUSY_TIMEOUT_MS / 1000,
            )
            self._open.append(connection)
            self._connection = connection
            connection.execute("PRAGMA journal_mode=WAL")
            schema = _schema(connection)
            if schema and schema != _expected_schema():
                # Derived rows only: an earlier or unknown layout is dropped and
                # rebuilt from history and the rejection records.
                for table in {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }:
                    connection.execute(f'DROP TABLE IF EXISTS "{table}"')
            connection.executescript(_SCHEMA)
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection = None
            _close_all(self._open)

    def _discard(self) -> None:
        """Drop the damaged derived file and its sidecars; the next use rebuilds it."""

        with self._lock:
            self.close()
            for suffix in _SIDECARS:
                Path(str(self.path) + suffix).unlink(missing_ok=True)

    def _recovering(self, operation: str, run: Callable[[], _T], default: _T) -> _T:
        """Run one cache operation; a damaged file is discarded and rebuilt once.

        The cache never blocks the governed work reading it: if the rebuilt file
        fails too (or SQLite stays busy), the operation reports ``default`` -- no
        receipts -- and logs why. Errors from the authoritative sources (proposal
        evidence, history, Captures) are not caught here.
        """

        with self._lock:
            for attempt in (1, 2):
                try:
                    return run()
                except sqlite3.DatabaseError as exc:
                    damaged = _damaged(exc) and not (is_previewing() or history_reads_detached())
                    _log.warning(
                        "refusal_index_unusable",
                        path=str(self.path),
                        operation=operation,
                        attempt=attempt,
                        error=str(exc),
                        action="rebuild" if damaged and attempt == 1 else "no_receipts",
                    )
                    if not damaged:
                        break
                    self._discard()
            return default

    @contextmanager
    def closing(self, proposal_id: str) -> Iterator[None]:
        """Serialize every close of one proposal, from its open check to its rows."""

        with self._lock:
            lock = self._closing.setdefault(proposal_id, threading.Lock())
        with lock:
            yield

    @contextmanager
    def current(self, instance: PlaybillInstance) -> Iterator[sqlite3.Connection]:
        """A connection whose retirement rows reach the accepted head.

        A preview writes nothing: it catches a private in-memory copy up instead.
        """

        with self._lock:
            if is_previewing() or history_reads_detached():
                memory = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
                try:
                    if self.path.exists():
                        try:
                            source = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
                            try:
                                source.backup(memory)
                            finally:
                                source.close()
                            if _schema(memory) != _expected_schema():
                                raise sqlite3.DatabaseError("refusal index layout differs")
                        except sqlite3.DatabaseError as exc:
                            # A preview never repairs the file; it rebuilds privately.
                            _log.warning(
                                "refusal_index_unusable",
                                path=str(self.path),
                                operation="preview",
                                error=str(exc),
                                action="private_rebuild",
                            )
                            memory.close()
                            memory = sqlite3.connect(
                                ":memory:", check_same_thread=False, isolation_level=None
                            )
                    memory.executescript(_SCHEMA)
                    self._coverage = self._catch_up(memory, instance)
                    yield memory
                finally:
                    memory.close()
                return
            connection = self._writer()
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._coverage = self._catch_up(connection, instance)
                connection.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
            yield connection

    # -- maintenance --------------------------------------------------------

    def catch_up(self, instance: PlaybillInstance) -> None:
        """Index every accepted generation not yet indexed (a read path; it may wait)."""

        def run() -> None:
            with self.current(instance):
                pass

        self._recovering("catch_up", run, None)

    def _catch_up(
        self, connection: sqlite3.Connection, instance: PlaybillInstance
    ) -> PriorRefusalCoverage | None:
        """Bring the retirement rows to the accepted head, through the history index.

        Caught up, this reads one generation locator. Behind, it indexes at most
        one page (`RETIREMENT_PAGE` generations) from their change-set records
        and records its progress, so one call's work is bounded whatever the
        backlog; the background step drives the rest, a page per transaction.
        Returns the coverage while the index still lags the head (``None`` once
        caught up). With no progress (a new or foreign file) it rebuilds: every
        retired Claim at the head, from the accepted projection, plus every
        rejection record -- never by walking accepted history.
        """

        instance_id = instance.descriptor.instance_id
        progress = connection.execute(
            "SELECT instance_id,sequence,generation_root,key_domain FROM refusal_progress"
        ).fetchone()
        evidence = instance.proposal_evidence()
        with instance.accepted_history_reader() as history:
            head = history.generation(history.sequence)
            if (
                progress is not None
                and progress[0] == instance_id
                and progress[1] == head.sequence
                and progress[2] == head.generation_root
                and progress[3] == CLAIM_REFUSAL_MATCH_KEY_DOMAIN
            ):
                return None
            if self._no_wait and (progress is None or progress[0] != instance_id):
                raise _Deferred("index_needs_rebuild")
            if (
                progress is None
                or progress[0] != instance_id
                or progress[3] != CLAIM_REFUSAL_MATCH_KEY_DOMAIN
                or progress[1] > head.sequence
                or history.generation(progress[1]).generation_root != progress[2]
            ):
                if self._no_wait:
                    raise _Deferred("index_needs_rebuild")
                self._rebuild(connection, instance, evidence, history)
                through = head
            else:
                last = min(head.sequence, progress[1] + RETIREMENT_PAGE)
                for sequence in range(progress[1] + 1, last + 1):
                    self._index_generation(connection, instance, evidence, history, sequence)
                through = history.generation(last)
        connection.execute(
            "INSERT OR REPLACE INTO refusal_progress VALUES (1,?,?,?,?)",
            (
                instance_id,
                through.sequence,
                through.generation_root,
                CLAIM_REFUSAL_MATCH_KEY_DOMAIN,
            ),
        )
        if through.sequence == head.sequence:
            return None
        return PriorRefusalCoverage(
            indexed_through_generation=through.sequence,
            accepted_generation=head.sequence,
        )

    def _rebuild(
        self,
        connection: sqlite3.Connection,
        instance: PlaybillInstance,
        evidence: ProposalEvidenceStore,
        history: HistoryReader,
    ) -> None:
        self.rebuilds += 1
        connection.execute("DELETE FROM refusals")
        connection.execute("DELETE FROM proposal_claim_keys")
        connection.execute("DELETE FROM proposals_keyed")
        # Settled counts derive from the journal: restart them from its start.
        connection.execute("DELETE FROM line_settled_refusals")
        connection.execute("DELETE FROM settled_counted")
        connection.execute("DELETE FROM settled_cursor")
        self._index_rejections(connection, instance, evidence)
        head = instance.accepted_coordinate()
        with instance.bind_accepted_projection(head) as projection:
            paths = tuple(
                str(row[0])
                for row in projection.typed.connection.execute(
                    "SELECT path FROM claims WHERE lifecycle='retired' ORDER BY path"
                )
            )
        if not paths:
            return
        tree = instance.blobs_at(head.git_oid, paths)
        for path in paths:
            claim = parse_claim(tree[path], path=path)
            sequence = _retiring_sequence(instance, history, path)
            if sequence is None:
                continue
            record = history.read_generation_record(sequence, instance.blob_at)
            self._put_retirements(connection, instance, evidence, record, (claim,))

    def _index_generation(
        self,
        connection: sqlite3.Connection,
        instance: PlaybillInstance,
        evidence: ProposalEvidenceStore,
        history: HistoryReader,
        sequence: int,
    ) -> None:
        location = history.generation(sequence)
        if location.candidate_digest is None:
            return
        self.generations_indexed += 1
        record = history.read_generation_record(sequence, instance.blob_at)
        paths = tuple(
            member.path
            for member in record.members
            if member.artifact_kind == "claim"
            and (
                member.disposition == "retire"
                if isinstance(member, CandidateMemberLawEvidence)
                else True
            )
        )
        if not paths:
            return
        tree = instance.blobs_at(location.git_oid, paths)
        retired = []
        for path in paths:
            content = tree.get(path)
            if content is None:
                continue
            claim = parse_claim(content, path=path)
            if claim.lifecycle.state == "retired":
                retired.append(claim)
        if retired:
            self._put_retirements(connection, instance, evidence, record, tuple(retired))

    @staticmethod
    def _put_retirements(
        connection: sqlite3.Connection,
        instance: PlaybillInstance,
        evidence: ProposalEvidenceStore,
        record: ChangeSetRecordAnyVersion,
        claims: tuple[ClaimArtifactAny, ...],
    ) -> None:
        proposal_id, rationale = _retiring_proposal(evidence, record.candidate_digest)
        decided_at = record.candidate.timestamp
        connection.executemany(
            "INSERT OR IGNORE INTO refusals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    claim_refusal_key(instance, claim),
                    "claim_retired",
                    claim.identity.name,
                    claim.identity.name,
                    proposal_id,
                    record.actor_binding.actor_id,
                    decided_at,
                    _micros(decided_at),
                    claim.retirement.reason if isinstance(claim, ClaimArtifact) else None,
                    rationale,
                    _producer_text(claim_producer(claim)),
                )
                for claim in claims
            ],
        )

    def _index_rejections(
        self,
        connection: sqlite3.Connection,
        instance: PlaybillInstance,
        evidence: ProposalEvidenceStore,
    ) -> None:
        """Rebuild only: every rejection record's Claims, keyed under this domain."""

        directory = evidence.withdrawals
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.json")):
            record = evidence.parse_model_bytes(
                evidence.read_record_bytes(path),
                ProposalWithdrawalRecord,
                label="refusal index rejection",
            )
            if record.kind == "rejected":
                self._put_rejection(connection, instance, record)

    @staticmethod
    def _put_rejection(
        connection: sqlite3.Connection,
        instance: PlaybillInstance,
        record: ProposalWithdrawalRecord,
    ) -> None:
        """Replace one proposal's rejection rows with those of ``record``."""

        connection.execute(
            "DELETE FROM refusals WHERE kind='proposal_rejected' AND source_id=?",
            (record.proposal_id,),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO refusals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    key,
                    "proposal_rejected",
                    record.proposal_id,
                    item.claim_id,
                    record.proposal_id,
                    record.actor_id,
                    record.withdrawn_at,
                    _micros(record.withdrawn_at),
                    None,
                    record.reason,
                    _producer_text(item.producer),
                )
                for item, key in _rejection_keys(instance, record)
            ],
        )

    def _write(
        self, instance: PlaybillInstance, change: Callable[[sqlite3.Connection], None]
    ) -> None:
        with self.current(instance) as connection:
            if is_previewing() or history_reads_detached():
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                change(connection)
                connection.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise

    def record_rejection(
        self, instance: PlaybillInstance, record: ProposalWithdrawalRecord
    ) -> None:
        """Index a rejection's Claims before its record is written (inside `closing`)."""

        if record.kind != "rejected" or not record.refused_claims:
            return
        self._recovering(
            "record_rejection",
            lambda: self._write(
                instance, lambda connection: self._put_rejection(connection, instance, record)
            ),
            None,
        )

    def settle_close(self, instance: PlaybillInstance, proposal_id: str) -> None:
        """Rewrite one proposal's rejection rows from the closing record that won.

        Called (inside `closing`) once a close resolves, however it resolved: the
        winner's rows replace any speculative ones, and a proposal that did not
        end rejected keeps none.
        """

        def change(connection: sqlite3.Connection) -> None:
            record = instance.proposal_evidence().read_withdrawal(proposal_id)
            if record is not None and record.kind == "rejected":
                self._put_rejection(connection, instance, record)
            else:
                connection.execute(
                    "DELETE FROM refusals WHERE kind='proposal_rejected' AND source_id=?",
                    (proposal_id,),
                )

        self._recovering("settle_close", lambda: self._write(instance, change), None)

    # -- settled items per Line ----------------------------------------------
    #
    # Counted in post, never on delivery: a background step (and `next`, when it
    # reads the rows) reads settle deliveries from the journal after a persisted
    # cursor, matches each settled item through this index, and upserts the
    # Line's row and the cursor in one transaction. Losing the file loses
    # nothing: a rebuild resets the cursor and the journal is counted again.

    def settled_cursor(self, instance: PlaybillInstance, *, wait: bool) -> SettledCursor | None:
        """Where counting stands; ``None`` when this step should skip (busy, or unbuilt)."""

        state = self.settled_state(instance, wait=wait)
        return None if state is None else state[0]

    def settled_state(
        self, instance: PlaybillInstance, *, wait: bool
    ) -> tuple[SettledCursor, PriorRefusalCoverage | None] | None:
        """The cursor after one bounded retirement page, and the coverage if still behind.

        Counting waits for the retirements before it: a step that finds the
        index lagging spends itself on the page and counts nothing yet.
        """

        def read(
            connection: sqlite3.Connection,
        ) -> tuple[SettledCursor, PriorRefusalCoverage | None]:
            return _cursor(connection), self._coverage

        return self._step(instance, "settled_state", read, wait=wait)

    def peek_coverage(self, instance: PlaybillInstance) -> PriorRefusalCoverage | None:
        """Retirement coverage now, read-only and without waiting; ``None`` if current.

        ``None`` too when the index is missing, busy or not in this layout: then
        nothing is known to report.
        """

        peeked = self.peek(instance)
        if peeked is None:
            return None
        head = instance.accepted_history()[-1].sequence
        if peeked[0] >= head:
            return None
        return PriorRefusalCoverage(indexed_through_generation=peeked[0], accepted_generation=head)

    def peek(self, instance: PlaybillInstance) -> tuple[int, SettledCursor] | None:
        """Retirements indexed through, and the counting cursor: a read-only look.

        Opens its own read-only connection without waiting and changes nothing,
        so consumer scheduling and health never act or block. ``None`` when the
        file is missing, busy, or not in this layout.
        """

        if not self.path.exists():
            return None
        try:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=0)
        except sqlite3.Error:
            return None
        try:
            if _schema(connection) != _expected_schema():
                return None
            progress = connection.execute("SELECT sequence FROM refusal_progress").fetchone()
            cursor = _cursor(connection)
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        return None if progress is None else (int(progress[0]), cursor)

    def advance_settled(
        self,
        instance: PlaybillInstance,
        *,
        expected: SettledCursor,
        through: SettledCursor,
        deliveries: tuple[SettledDelivery, ...],
        wait: bool,
    ) -> bool:
        """Count one page of settle deliveries and move the cursor past it, atomically.

        Each settled proposal counts once (redeliveries and recoveries are
        no-ops). Totals take every counted settlement; the latest run moves only
        to a later settlement instant (ties by run ID), whatever the order.
        Returns False, changing nothing, when the step must skip or the cursor
        moved under it (another step counted this page).
        """

        def advance(connection: sqlite3.Connection) -> bool:
            matched = {
                delivery.proposal_id: len(self._prior_refusals_now(instance, delivery.proposal_id))
                for delivery in deliveries
            }
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = _cursor(connection)
                if current != expected:
                    connection.execute("ROLLBACK")
                    return False
                for delivery in deliveries:
                    count = matched[delivery.proposal_id]
                    if (
                        not count
                        or connection.execute(
                            "SELECT 1 FROM settled_counted WHERE proposal_id=?",
                            (delivery.proposal_id,),
                        ).fetchone()
                    ):
                        continue
                    connection.execute(
                        "INSERT INTO settled_counted VALUES (?)", (delivery.proposal_id,)
                    )
                    connection.execute(
                        "INSERT INTO line_settled_refusals VALUES (?,?,?,1,?,?) "
                        "ON CONFLICT(line_identity,line_spec_digest) DO UPDATE SET "
                        "items=items+excluded.items, runs=runs+1, "
                        "latest_run_id=CASE WHEN "
                        "(excluded.latest_settled_at, excluded.latest_run_id) > "
                        "(latest_settled_at, latest_run_id) "
                        "THEN excluded.latest_run_id ELSE latest_run_id END, "
                        "latest_settled_at=MAX(latest_settled_at, excluded.latest_settled_at)",
                        (
                            delivery.line_identity,
                            delivery.line_spec_digest,
                            count,
                            delivery.run_id,
                            delivery.settled_at,
                        ),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO settled_cursor VALUES (1,?,?)",
                    (through.journal_identity, through.record_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
            self.settled_counted += len(deliveries)
            return True

        return bool(self._step(instance, "advance_settled", advance, wait=wait))

    def _step(
        self,
        instance: PlaybillInstance,
        operation: str,
        body: Callable[[sqlite3.Connection], _T],
        *,
        wait: bool,
    ) -> _T | None:
        """Run one background-counting body on a current index, or skip it.

        Without ``wait`` the step takes the index only if it is free, never builds
        the index (an unbuilt one is left to the next read), and gives up at once
        on SQLite contention: the cursor makes skipping lossless.
        """

        if is_previewing() or history_reads_detached():
            # A read-only view never counts or moves the cursor.
            return None
        if wait:

            def run() -> _T:
                with self.current(instance) as connection:
                    return body(connection)

            return self._recovering(operation, run, None)
        if not self._lock.acquire(blocking=False):
            return None
        try:
            self._no_wait = True
            with self.current(instance) as connection:
                return body(connection)
        except (_Deferred, sqlite3.DatabaseError) as exc:
            _log.info("settled_refusal_step_skipped", operation=operation, reason=str(exc))
            return None
        finally:
            self._no_wait = False
            if self._connection is not None:
                with suppress(sqlite3.Error):
                    self._connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            self._lock.release()

    def settled_refusals(
        self, instance: PlaybillInstance, lines: tuple[tuple[str, str], ...]
    ) -> dict[tuple[str, str], LineSettledRefusals]:
        """The maintained count of each (Line, spec digest) given: one keyed probe each."""

        return self.settled_refusals_with_coverage(instance, lines)[0]

    def settled_refusals_with_coverage(
        self, instance: PlaybillInstance, lines: tuple[tuple[str, str], ...]
    ) -> tuple[dict[tuple[str, str], LineSettledRefusals], PriorRefusalCoverage | None]:
        """The counts, and the retirement coverage of the same read (``None`` if current)."""

        def run() -> tuple[dict[tuple[str, str], LineSettledRefusals], PriorRefusalCoverage | None]:
            found: dict[tuple[str, str], LineSettledRefusals] = {}
            with self.current(instance) as connection:
                for line_identity, spec_digest in lines:
                    self.line_probes += 1
                    row = connection.execute(
                        "SELECT items,runs,latest_run_id,latest_settled_at "
                        "FROM line_settled_refusals WHERE line_identity=? AND line_spec_digest=?",
                        (line_identity, spec_digest),
                    ).fetchone()
                    if row is not None:
                        found[(line_identity, spec_digest)] = LineSettledRefusals(
                            line_identity=line_identity,
                            line_spec_digest=spec_digest,
                            items=int(row[0]),
                            runs=int(row[1]),
                            latest_run_id=str(row[2]),
                            latest_settled_at=str(row[3]),
                        )
                coverage = self._coverage
            return found, coverage

        return self._recovering("settled_refusals", run, cast(_SettledRead, ({}, None)))

    # -- lookups ------------------------------------------------------------

    def _proposal_keys(
        self,
        connection: sqlite3.Connection,
        instance: PlaybillInstance,
        proposal_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """(path, match key) of each live Claim the proposal authors, cached once keyed."""

        keyed = connection.execute(
            "SELECT 1 FROM proposals_keyed WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if keyed is not None:
            return tuple(
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT path,match_key FROM proposal_claim_keys "
                    "WHERE proposal_id=? ORDER BY path",
                    (proposal_id,),
                )
            )
        claims = _candidate_claims(instance, proposal_id)
        if claims is None:
            return ()
        self.proposals_keyed += 1
        rows = [
            (
                proposal_id,
                path,
                claim.identity.name,
                claim_refusal_key(instance, claim),
                _producer_text(claim_producer(claim)),
            )
            for path, claim in claims
        ]
        writable = not (is_previewing() or history_reads_detached())
        if writable:
            connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                "INSERT OR REPLACE INTO proposal_claim_keys VALUES (?,?,?,?,?)", rows
            )
            connection.execute("INSERT OR REPLACE INTO proposals_keyed VALUES (?)", (proposal_id,))
            if writable:
                connection.execute("COMMIT")
        except BaseException:
            if writable:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
            raise
        return tuple((row[1], row[3]) for row in rows)

    def prior_refusals(
        self,
        instance: PlaybillInstance,
        proposal_id: str,
    ) -> dict[str, tuple[PriorRefusalReceipt, ...]]:
        """Per candidate Claim path, every earlier refusal of its statement on its evidence.

        "Earlier" is before the proposal was admitted, so a proposal's receipts
        do not change as later decisions land, and a proposal never matches its
        own rejection.
        """

        return self.prior_refusals_with_coverage(instance, proposal_id)[0]

    def prior_refusals_with_coverage(
        self, instance: PlaybillInstance, proposal_id: str
    ) -> tuple[dict[str, tuple[PriorRefusalReceipt, ...]], PriorRefusalCoverage | None]:
        """`prior_refusals`, and how far retirements were indexed when it was read.

        The coverage is ``None`` when the index is current. While it lags (a
        backlog is indexed a page per read and by the background step), the
        receipts reflect retirements accepted through the generation it names
        -- the caller says so, neutrally, rather than implying there are none.
        """

        def run() -> _ReceiptsRead:
            found = self._prior_refusals_now(instance, proposal_id)
            return found, self._coverage

        return self._recovering("prior_refusals", run, cast(_ReceiptsRead, ({}, None)))

    def coverage(self, instance: PlaybillInstance) -> PriorRefusalCoverage | None:
        """One bounded catch-up page; how far retirements are indexed if still behind."""

        def run() -> PriorRefusalCoverage | None:
            with self.current(instance):
                return self._coverage

        return self._recovering("coverage", run, None)

    def _prior_refusals_now(
        self, instance: PlaybillInstance, proposal_id: str
    ) -> dict[str, tuple[PriorRefusalReceipt, ...]]:
        """`prior_refusals` without recovery: SQLite and deferral errors propagate."""

        evidence = instance.proposal_evidence()
        admitted_us = _micros(evidence.read_admission(proposal_id).admitted_at)
        found: dict[str, tuple[PriorRefusalReceipt, ...]] = {}
        with self.current(instance) as connection:
            for path, match_key in self._proposal_keys(connection, instance, proposal_id):
                self.key_probes += 1
                receipts: list[PriorRefusalReceipt] = []
                for row in connection.execute(
                    "SELECT kind,source_id,claim_id,proposal_id,decided_by,decided_at,"
                    "reason_code,rationale,producer FROM refusals WHERE match_key=? "
                    "AND decided_at_us<? ORDER BY decided_at_us,kind,source_id",
                    (match_key, admitted_us),
                ):
                    self.rows_read += 1
                    receipt = _receipt(evidence, row, proposal_id=proposal_id)
                    if receipt is not None:
                        receipts.append(receipt)
                if receipts:
                    found[path] = tuple(receipts)
        return found


def _retiring_sequence(instance: PlaybillInstance, history: HistoryReader, path: str) -> int | None:
    """The accepted generation that first retired the Claim at ``path``.

    Read from the path's own member locations (a Claim's few versions), never
    from a walk of history.
    """

    for location in history.member_history(path):
        record = history.read_generation_record(location.sequence, instance.blob_at)
        member = record.members[location.member_ordinal]
        if isinstance(member, CandidateMemberLawEvidence):
            if member.disposition == "retire":
                return location.sequence
            continue
        content = instance.blob_at(history.generation(location.sequence).git_oid, path)
        if content is not None and parse_claim(content, path=path).lifecycle.state == "retired":
            return location.sequence
    return None


def _cursor(connection: sqlite3.Connection) -> SettledCursor:
    row = connection.execute("SELECT journal_identity,record_id FROM settled_cursor").fetchone()
    if row is None:
        return SettledCursor(None, 0)
    return SettledCursor(None if row[0] is None else str(row[0]), int(row[1]))


def _close_all(connections: list[sqlite3.Connection]) -> None:
    while connections:
        connections.pop().close()


def _retiring_proposal(
    evidence: ProposalEvidenceStore, candidate_digest: str
) -> tuple[str | None, str | None]:
    """The admitted proposal whose candidate a generation accepted, and its rationale."""

    if evidence.index is None:
        return None, None
    rows = evidence.index.rows(
        evidence, "candidate_digest=? AND admission_path IS NOT NULL", (candidate_digest,)
    )
    if not rows:
        return None, None
    proposal_id = str(rows[0]["proposal_id"])
    return proposal_id, evidence.read_admission(proposal_id).rationale


def _receipt(
    evidence: ProposalEvidenceStore,
    row: tuple[object, ...],
    *,
    proposal_id: str,
) -> PriorRefusalReceipt | None:
    kind, source_id, claim_id, row_proposal, decided_by, decided_at, code, rationale, producer = row
    if kind == "proposal_rejected":
        if source_id == proposal_id:
            return None
        # The record is the authority: a row whose rejection never landed, or
        # whose proposal closed another way, is no refusal.
        try:
            record = evidence.read_withdrawal(str(source_id))
        except ProposalIntegrityError:
            return None
        if record is None or record.kind != "rejected":
            return None
        return PriorRefusalReceipt(
            kind="proposal_rejected",
            claim_id=str(claim_id),
            proposal_id=record.proposal_id,
            decided_by=record.actor_id,
            decided_at=record.withdrawn_at,
            rationale=record.reason,
            producer=_producer(producer if isinstance(producer, str) else None),
        )
    return PriorRefusalReceipt(
        kind="claim_retired",
        claim_id=str(claim_id),
        proposal_id=row_proposal if isinstance(row_proposal, str) else None,
        decided_by=str(decided_by),
        decided_at=str(decided_at),
        reason_code=code if isinstance(code, str) else None,
        rationale=rationale if isinstance(rationale, str) else None,
        producer=_producer(producer if isinstance(producer, str) else None),
    )


__all__ = [
    "LineSettledRefusals",
    "RefusalIndex",
    "SettledCursor",
    "SettledDelivery",
    "candidate_refused_claims",
    "capture_evidence_content",
    "claim_refusal_key",
]
