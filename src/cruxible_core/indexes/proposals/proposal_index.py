"""Proposal locators in the instance's shared, mutable working database.

Evidence remains authoritative. A durable dirty marker precedes source writes;
record fsyncs precede the transaction publishing rows and source progress. A lost
marker, interruption, changed directory membership or unexpected database change
requires explicit reconstruction. Metadata establishes inventory continuity only:
every selected record is freshly read and checked against its exact byte digest.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
    ProposalWithdrawalRecordV1,
)
from cruxible_core.indexes.acquisition import open_working_snapshot
from cruxible_core.indexes.history.history_index import commit_working_write
from cruxible_core.proposals.proposal_notes import admission_bytes

if TYPE_CHECKING:
    from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposal_progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 source_epoch TEXT NOT NULL, verified_sequence INTEGER NOT NULL CHECK(verified_sequence>=0),
 source_root TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS proposals (
 proposal_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, target_ref TEXT NOT NULL,
 admission_path TEXT NOT NULL, admission_digest TEXT NOT NULL,
 evaluation_path TEXT, evaluation_digest TEXT, withdrawal_path TEXT, withdrawal_digest TEXT,
 evaluation_status TEXT NOT NULL CHECK(evaluation_status IN ('missing','refused','candidate')),
 proposed_base_oid TEXT NOT NULL, candidate_commit_oid TEXT NOT NULL,
 review_commit_oid TEXT, review_context_digest TEXT,
 candidate_tree_oid TEXT NOT NULL, evaluated_base_oid TEXT, evaluated_tree_oid TEXT,
 candidate_digest TEXT, candidate_parent_semantic_root TEXT,
 rebased INTEGER CHECK(rebased IN (0,1)), admitted_at_us INTEGER NOT NULL, evaluated_at_us INTEGER,
 source_epoch TEXT NOT NULL, verified_sequence INTEGER NOT NULL CHECK(verified_sequence>=0),
 CHECK((evaluation_path IS NULL)=(evaluation_digest IS NULL)),
 CHECK((withdrawal_path IS NULL)=(withdrawal_digest IS NULL)),
 CHECK((review_commit_oid IS NULL)=(review_context_digest IS NULL)),
 CHECK((evaluation_status='missing')=(evaluation_path IS NULL)),
 CHECK((evaluation_status='candidate')=(candidate_digest IS NOT NULL)),
 CHECK(evaluation_status='missing' OR (evaluated_base_oid IS NOT NULL AND rebased IS NOT NULL
       AND evaluated_at_us IS NOT NULL)),
 CHECK(evaluation_status!='candidate' OR evaluated_tree_oid IS NOT NULL)
) STRICT;
CREATE INDEX IF NOT EXISTS proposals_by_candidate ON proposals(candidate_digest,proposal_id)
 WHERE candidate_digest IS NOT NULL;
CREATE INDEX IF NOT EXISTS proposals_by_submitted_commit
 ON proposals(candidate_commit_oid,proposal_id);
CREATE INDEX IF NOT EXISTS proposals_by_review_commit ON proposals(review_commit_oid,proposal_id)
 WHERE review_commit_oid IS NOT NULL;
"""


def _schema_rows(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE tbl_name IN ('proposals','proposal_progress') ORDER BY type,name"
    ).fetchall()


with sqlite3.connect(":memory:") as _reference:
    _reference.executescript(_SCHEMA)
    _EXPECTED_SCHEMA = _schema_rows(_reference)


def file_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _micros(value: str) -> int:
    delta = datetime.fromisoformat(value.replace("Z", "+00:00")) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def timestamp(value: int) -> str:
    return (
        (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class ProposalIndex:
    """Component of AcceptedHistoryIndex, sharing its file and acquisition lock."""

    def __init__(
        self,
        *,
        path: Path,
        lock: threading.RLock,
        file_stamp: Callable[[], tuple[int, ...] | None],
        on_commit: Callable[[tuple[int, ...] | None, tuple[int, ...]], None],
        connections: list[sqlite3.Connection],
        shutdown_proof: dict[str, Any],
    ) -> None:
        self.path, self._lock = path, lock
        self._file_stamp, self._on_commit = file_stamp, on_commit
        self._connections, self._shutdown_proof = connections, shutdown_proof
        self._connection: sqlite3.Connection | None = None
        self._identity: tuple[int, ...] | None = None
        self._close: Callable[[], None] | None = None
        self._stamp: tuple[int, ...] | None = None
        self._root: Path | None = None
        self.reconstructions = 0
        self.records_verified = 0
        self.rows_written = 0
        self.source_checks = 0

    def clear(self) -> None:
        self._stamp = None

    def _writer(self) -> sqlite3.Connection:
        stamp = self._file_stamp()
        identity = None if stamp is None else stamp[:2]
        if self._connection is not None and identity != self._identity:
            assert self._close is not None
            self._close()
            self._connection = None
            self._stamp = None
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._close = self._connection.close
            self._connections.append(self._connection)
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            if not _schema_rows(self._connection):
                self._connection.execute("BEGIN IMMEDIATE")
                before = self._file_stamp()
                try:
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            self._connection.execute(statement)
                    after = commit_working_write(self._connection, self._file_stamp, before)
                    self._on_commit(before, after)
                    self._connection.rollback()
                except BaseException:
                    self._connection.rollback()
                    raise
            self._identity = self._file_stamp()[:2]  # type: ignore[index]
        return self._connection

    @contextmanager
    def _source_lock(self, evidence: ProposalEvidenceStore) -> Iterator[None]:
        with self._lock:
            if evidence.root.is_symlink() or not evidence.root.is_dir():
                raise ProposalIntegrityError("proposal source root is not trustworthy")
            self._root = evidence.root
            descriptor = os.open(
                evidence.root / ".proposal-source.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    @staticmethod
    def _inventory(evidence: ProposalEvidenceStore) -> list[list[int]]:
        result = []
        for path in (
            evidence.proposals,
            evidence.evaluations,
            evidence.candidates,
            evidence.withdrawals,
        ):
            if path.is_symlink() or not path.is_dir():
                raise ProposalIntegrityError("proposal source directory is not trustworthy")
            stat = path.stat()
            result.append([stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns])
        return result

    @staticmethod
    def _marker(root: Path) -> dict[str, Any] | None:
        path = root / ".proposal-source.json"
        if path.is_symlink():
            raise ProposalIntegrityError("proposal source checkpoint is not trustworthy")
        try:
            value = json.loads(path.read_bytes())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_marker(root: Path, value: dict[str, Any]) -> None:
        from cruxible_core.proposals.proposal_evidence import _fsync_directory

        target = root / ".proposal-source.json"
        temporary = root / (".proposal-source-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(root)
        finally:
            temporary.unlink(missing_ok=True)

    def database_committed(self, before: tuple[int, ...] | None, after: tuple[int, ...]) -> None:
        """Publish C's captured stamp while it retains reacquired SQLite ownership."""
        if (
            self._root is not None
            and before is not None
            and self._stamp == before
            and after[:2] == before[:2]
        ):
            marker = self._marker(self._root)
            if marker is not None and marker.get("database_stamp") == list(before):
                self._stamp = after
                marker["database_stamp"] = after
                self._write_marker(self._root, marker)
                self._remember(self._root, marker)

    def _sync(
        self,
        evidence: ProposalEvidenceStore,
        connection: sqlite3.Connection,
        *,
        check_review_context: bool = False,
        source_write: bool = False,
    ) -> dict[str, Any]:
        self.source_checks += 1
        if _schema_rows(connection) != _EXPECTED_SCHEMA:
            raise ProposalIntegrityError("proposal index schema differs; rebuild required")
        marker = self._marker(evidence.root)
        progress = connection.execute(
            "SELECT source_epoch,verified_sequence,source_root FROM proposal_progress"
        ).fetchall()
        context = marker.get("review_context") if marker is not None else None
        if evidence.transport is not None and (check_review_context or marker is None):
            context = file_digest(evidence.transport.review_commit_context())
        stamp = self._file_stamp()
        if (
            marker is not None
            and marker.get("clean") is True
            and (not source_write or marker.get("orphan_evaluations", 0) == 0)
            and marker.get("inventory") == self._inventory(evidence)
            and marker.get("database_stamp") == (list(stamp) if stamp else None)
            and marker.get("review_context") == context
            and progress == [(marker.get("epoch"), marker.get("sequence"), str(evidence.root))]
        ):
            self._stamp = stamp
            self._remember(evidence.root, marker)
            return marker
        return self._rebuild(
            evidence, connection, context if evidence.transport is not None else None
        )

    def _record(
        self,
        evidence: ProposalEvidenceStore,
        path: Path,
        model: type[Any],
        render: Callable[..., bytes] | None = None,
    ) -> tuple[Any, str]:
        raw = evidence.read_record_bytes(path)
        self.records_verified += 1
        return evidence.parse_model_bytes(
            raw, model, label="proposal index", render=render
        ), file_digest(raw)

    def _row(
        self,
        evidence: ProposalEvidenceStore,
        admission_path: Path,
        evaluation_path: Path | None,
        withdrawal_path: Path | None,
        epoch: str,
        sequence: int,
    ) -> dict[str, Any]:
        admission, admission_digest = self._record(
            evidence, admission_path, ProposalAdmissionRecord, admission_bytes
        )
        evaluation, evaluation_digest = (
            (None, None)
            if evaluation_path is None
            else self._record(evidence, evaluation_path, ProposalEvaluationRecord)
        )
        withdrawal, withdrawal_digest = (
            (None, None)
            if withdrawal_path is None
            else self._record(evidence, withdrawal_path, ProposalWithdrawalRecordV1)
        )
        if any(
            record is not None and record.proposal_id != admission.proposal_id
            for record in (evaluation, withdrawal)
        ):
            raise ProposalIntegrityError("proposal located evidence names another admission")
        candidate_digest = evaluation.candidate_digest if evaluation else None
        summary = (
            evidence.read_candidate_review_summary_if_present(candidate_digest)
            if candidate_digest
            else None
        )
        review_oid = None
        review_context = None
        if summary is not None and evidence.transport is not None:
            assert evaluation is not None
            review_context = file_digest(evidence.transport.review_commit_context())
            review_oid = evidence.transport.proposal_review_commit_oid(
                tree_oid=evaluation.evaluated_tree_oid,
                base_oid=evaluation.evaluated_base_oid,
                actor_id=admission.actor_id,
                timestamp=admission.admitted_at,
                message=summary.message(rationale=admission.rationale),
            )
        return dict(
            proposal_id=admission.proposal_id,
            actor_id=admission.actor_id,
            target_ref=admission.target_ref,
            admission_path=str(admission_path.relative_to(evidence.root)),
            admission_digest=admission_digest,
            evaluation_path=str(evaluation_path.relative_to(evidence.root))
            if evaluation_path
            else None,
            evaluation_digest=evaluation_digest,
            withdrawal_path=str(withdrawal_path.relative_to(evidence.root))
            if withdrawal_path
            else None,
            withdrawal_digest=withdrawal_digest,
            evaluation_status=evaluation.verdict if evaluation else "missing",
            proposed_base_oid=admission.proposed_base_oid,
            candidate_commit_oid=admission.candidate_commit_oid,
            review_commit_oid=review_oid,
            review_context_digest=review_context,
            candidate_tree_oid=admission.candidate_tree_oid,
            evaluated_base_oid=evaluation.evaluated_base_oid if evaluation else None,
            evaluated_tree_oid=evaluation.evaluated_tree_oid if evaluation else None,
            candidate_digest=candidate_digest,
            candidate_parent_semantic_root=summary.parent_semantic_root if summary else None,
            rebased=int(evaluation.rebased) if evaluation else None,
            admitted_at_us=_micros(admission.admitted_at),
            evaluated_at_us=_micros(evaluation.evaluated_at) if evaluation else None,
            source_epoch=epoch,
            verified_sequence=sequence,
        )

    def _put(self, connection: sqlite3.Connection, row: dict[str, Any]) -> None:
        names = ",".join(row)
        connection.execute(
            f"INSERT OR REPLACE INTO proposals ({names}) VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
        self.rows_written += 1

    def _rebuild(
        self, evidence: ProposalEvidenceStore, connection: sqlite3.Connection, context: str | None
    ) -> dict[str, Any]:
        self.reconstructions += 1
        inventory = self._inventory(evidence)
        records: list[dict[str, Path]] = []
        for directory, model, render in (
            (evidence.proposals, ProposalAdmissionRecord, admission_bytes),
            (evidence.evaluations, ProposalEvaluationRecord, None),
            (evidence.withdrawals, ProposalWithdrawalRecordV1, None),
        ):
            by_id: dict[str, Path] = {}
            digests: dict[str, str] = {}
            for path in sorted(directory.glob("*.json")):
                record, digest = self._record(evidence, path, model, render)
                if record.proposal_id in by_id:
                    if model is ProposalEvaluationRecord:
                        raise ProposalIntegrityError(
                            "proposal evidence contains multiple evaluations"
                        )
                    if digests[record.proposal_id] != digest or evidence.read_record_bytes(
                        by_id[record.proposal_id]
                    ) != evidence.read_record_bytes(path):
                        raise ProposalIntegrityError(
                            "proposal evidence contains conflicting admissions or withdrawals"
                        )
                    continue
                by_id[record.proposal_id], digests[record.proposal_id] = path, digest
            records.append(by_id)
        admissions, evaluations, withdrawals = records
        evidence._recovered_evaluations = evaluations
        marker: dict[str, Any] = dict(
            epoch=uuid.uuid4().hex,
            sequence=0,
            clean=False,
            review_context=context,
            orphan_evaluations=bool(set(evaluations) - set(admissions)),
        )
        connection.execute("DELETE FROM proposals")
        for pid, path in admissions.items():
            self._put(
                connection,
                self._row(
                    evidence, path, evaluations.get(pid), withdrawals.get(pid), marker["epoch"], 0
                ),
            )
        if self._inventory(evidence) != inventory:
            raise ProposalIntegrityError("proposal source changed during reconstruction")
        self._progress(connection, evidence, marker)
        return marker

    @staticmethod
    def _progress(
        connection: sqlite3.Connection, evidence: ProposalEvidenceStore, marker: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO proposal_progress VALUES (1,?,?,?)",
            (marker["epoch"], marker["sequence"], str(evidence.root)),
        )

    def _finish(
        self,
        evidence: ProposalEvidenceStore,
        connection: sqlite3.Connection,
        marker: dict[str, Any],
        before: tuple[int, ...] | None,
    ) -> None:
        after = commit_working_write(connection, self._file_stamp, before)
        self._on_commit(before, after)
        marker.update(clean=True, inventory=self._inventory(evidence), database_stamp=after)
        self._write_marker(evidence.root, marker)
        self._stamp = after
        self._remember(evidence.root, marker)

    def _remember(self, root: Path, marker: dict[str, Any]) -> None:
        self._shutdown_proof.clear()
        self._shutdown_proof.update(root=str(root), marker=json.loads(json.dumps(marker)))

    @contextmanager
    def read(
        self, evidence: ProposalEvidenceStore, *, review_context: bool = False
    ) -> Iterator[sqlite3.Connection]:
        reader = None
        try:
            with self._source_lock(evidence):
                writer = self._writer()
                writer.execute("BEGIN IMMEDIATE")
                before = self._file_stamp()
                try:
                    marker = self._sync(evidence, writer, check_review_context=review_context)
                    # A clean checkpoint needs no durable marker rewrite on a read.
                    if marker.get("clean") is True:
                        commit_working_write(writer, self._file_stamp, before)
                    else:
                        self._finish(evidence, writer, marker, before)
                except BaseException:
                    writer.rollback()
                    raise
                assert self._stamp is not None
                reader = open_working_snapshot(
                    self.path, expected_stamp=self._stamp, file_stamp=self._file_stamp
                )
                reader.row_factory = sqlite3.Row
                reader.execute("PRAGMA foreign_keys=ON")
                progress = reader.execute(
                    "SELECT source_epoch,verified_sequence FROM proposal_progress"
                ).fetchone()
                if (
                    tuple(progress or ()) != (marker["epoch"], marker["sequence"])
                    or self._file_stamp() != self._stamp
                ):
                    raise ProposalIntegrityError(
                        "proposal index changed during snapshot acquisition"
                    )
                writer.rollback()
            evidence._recovered_evaluations = {}
            yield reader
        except sqlite3.DatabaseError as exc:
            raise ProposalIntegrityError("proposal index requires reconstruction") from exc
        finally:
            with self._lock:
                if self._connection is not None and self._connection.in_transaction:
                    self._connection.rollback()
            if reader is not None:
                reader.close()

    @contextmanager
    def publication(self, evidence: ProposalEvidenceStore) -> Iterator[None]:
        """One source-owner batch; existing candidate/evaluation/admission writes remain ordered."""
        with self._source_lock(evidence):
            writer = self._writer()
            writer.execute("BEGIN IMMEDIATE")
            before = self._file_stamp()
            try:
                marker = self._sync(evidence, writer, source_write=True)
                marker = dict(marker, clean=False, sequence=marker["sequence"] + 1)
                self._write_marker(evidence.root, marker)
                evidence._pending_records = {}
                yield
                pending = evidence._pending_records
                admissions = pending.get("admission", {})
                evaluations = pending.get("evaluation", {})
                withdrawals = pending.get("withdrawal", {})
                affected = set(admissions) | set(withdrawals)
                affected.update(
                    pid
                    for pid in evaluations
                    if writer.execute(
                        "SELECT 1 FROM proposals WHERE proposal_id=?", (pid,)
                    ).fetchone()
                    is not None
                )
                for digest in pending.get("candidate", {}):
                    affected.update(
                        row[0]
                        for row in writer.execute(
                            "SELECT proposal_id FROM proposals WHERE candidate_digest=?", (digest,)
                        )
                    )
                for pid in affected:
                    old = writer.execute(
                        "SELECT admission_path,evaluation_path,withdrawal_path FROM proposals "
                        "WHERE proposal_id=?",
                        (pid,),
                    ).fetchone()
                    admission_path = admissions.get(pid) or (
                        evidence.root / old[0] if old else None
                    )
                    evaluation_path = (
                        evaluations.get(pid)
                        or evidence._recovered_evaluations.get(pid)
                        or (evidence.root / old[1] if old and old[1] else None)
                    )
                    withdrawal_path = withdrawals.get(pid) or (
                        evidence.root / old[2] if old and old[2] else None
                    )
                    if admission_path is None:
                        raise ProposalIntegrityError("withdrawal has no admission")
                    if evaluation_path is None:
                        # Low-level/legacy split writes have no completed batch locator.
                        marker = self._rebuild(evidence, writer, marker.get("review_context"))
                        break
                    row = self._row(
                        evidence,
                        admission_path,
                        evaluation_path,
                        withdrawal_path,
                        marker["epoch"],
                        marker["sequence"],
                    )
                    if (
                        old
                        and admissions.get(pid)
                        and evidence.read_record_bytes(evidence.root / old[0])
                        != evidence.read_record_bytes(admissions[pid])
                    ):
                        raise ProposalIntegrityError(
                            "proposal evidence contains conflicting admissions"
                        )
                    self._put(writer, row)
                marker["orphan_evaluations"] = any(
                    writer.execute("SELECT 1 FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
                    is None
                    for pid in set(evidence._recovered_evaluations) | set(evaluations)
                )
                self._progress(writer, evidence, marker)
                self._finish(evidence, writer, marker, before)
                writer.rollback()
            except BaseException:
                writer.rollback()
                self._stamp = None
                raise
            finally:
                evidence._pending_records = None
                evidence._recovered_evaluations = {}

    def rows(
        self, evidence: ProposalEvidenceStore, where: str = "1", parameters: tuple[Any, ...] = ()
    ) -> tuple[dict[str, Any], ...]:
        with self.read(evidence) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM proposals WHERE " + where + " ORDER BY proposal_id", parameters
                )
            )

    def locate(self, evidence: ProposalEvidenceStore, proposal_id: str) -> dict[str, Any]:
        rows = self.rows(evidence, "proposal_id=?", (proposal_id,))
        if not rows:
            raise ProposalIntegrityError("proposal admission evidence is missing")
        return rows[0]


def close_working_database(
    path: Path, connections: list[sqlite3.Connection], proof: dict[str, Any]
) -> None:
    """One owner finalizer; no component finalizers race a WAL checkpoint.

    The proof contains values only, never its owner or a bound callback. A
    graceful close may checkpoint SQLite/WAL bytes. Verify the old binding,
    acquire exclusive SQLite ownership, and publish the checkpoint's physical
    identity before releasing that ownership. Unexpected changes stay untrusted.
    """
    from cruxible_core.indexes.history.history_index import working_file_stamp

    descriptor = None
    root = Path(proof["root"]) if proof else None
    valid = False
    marker = None
    try:
        if root is not None and root.is_dir():
            descriptor = os.open(
                root / ".proposal-source.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            marker = ProposalIndex._marker(root)
            directories = [
                root / name for name in ("proposals", "evaluations", "candidates", "withdrawals")
            ]
            inventory = [
                [st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns]
                for directory in directories
                if not directory.is_symlink()
                for st in (directory.stat(),)
            ]
            stamp = working_file_stamp(path)
            valid = (
                marker is not None
                and marker == proof.get("marker")
                and marker.get("clean") is True
                and marker.get("database_stamp") == (list(stamp) if stamp else None)
                and marker.get("inventory") == inventory
            )
            if valid:
                assert marker is not None
                active = next(
                    (connection for connection in reversed(connections) if _is_open(connection)),
                    None,
                )
                if active is None:
                    valid = False
                else:
                    progress = active.execute(
                        "SELECT source_epoch,verified_sequence,source_root FROM proposal_progress"
                    ).fetchall()
                    valid = progress == [(marker["epoch"], marker["sequence"], str(root))]
        last = next(
            (connection for connection in reversed(connections) if _is_open(connection)), None
        )
        for connection in connections:
            if connection is not last:
                connection.close()
        if valid and last is not None and root is not None and marker is not None:
            # EXCLUSIVE mode retains ownership after COMMIT. No other SQLite
            # writer can change the checked bytes between this lock acquisition,
            # the WAL checkpoint, and publishing its new physical identity.
            last.execute("PRAGMA busy_timeout=0")
            if last.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone() != ("exclusive",):
                return
            last.execute("BEGIN EXCLUSIVE")
            if working_file_stamp(path) != stamp or ProposalIndex._marker(root) != marker:
                return
            last.commit()
            checkpoint = last.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            after = working_file_stamp(path)
            if (
                checkpoint is not None
                and checkpoint[0] == 0
                and after is not None
                and stamp is not None
                and after[:2] == stamp[:2]
            ):
                after_inventory = [
                    [st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns]
                    for directory in directories
                    if not directory.is_symlink()
                    for st in (directory.stat(),)
                ]
                if marker["inventory"] == after_inventory:
                    marker["database_stamp"] = after
                    ProposalIndex._write_marker(root, marker)
            # Never refresh the marker after releasing exclusive ownership.
            # Empty WAL removal carries no bytes; any other close-time or later
            # mutation leaves a different stamp and requires reconstruction.
    except (OSError, sqlite3.DatabaseError, ProposalIntegrityError):
        # Cleanup cannot certify a changed or unavailable source. Retain its
        # old checkpoint so the next reader must reconstruct.
        pass
    finally:
        for connection in connections:
            connection.close()
        connections.clear()
        if descriptor is not None:
            os.close(descriptor)


def _is_open(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1")
        return True
    except sqlite3.ProgrammingError:
        return False
