"""Rebuildable accepted-history locators, outside frozen generation databases.

Only the instance's replay-verified epoch supplies inputs. A process restart or
unexpected file change reconciles every retained row against those inputs; a
persisted cursor alone never authorizes reads. Within a verified epoch, successors
append changed artifact occurrences and progress in one transaction. Readers hold
one SQLite snapshot with an explicit accepted-history cutoff.
"""

from __future__ import annotations

import sqlite3
import stat
import threading
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    MemberLawEvaluationV2,
)
from cruxible_client.contracts.errors import PlaybillFormatError, ProjectionIntegrityError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.compiler.projection_artifacts import ArtifactEnvelopeRow
from cruxible_core.ledger.recovery import RecoveredInstanceState
from cruxible_core.proposals.settlement import (
    ChangeSetRecord,
    ChangeSetRecordAnyVersion,
    parse_change_set_record,
)

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS accepted_generations (
 sequence INTEGER PRIMARY KEY,
 git_oid TEXT NOT NULL, semantic_root TEXT NOT NULL, generation_root TEXT NOT NULL,
 compiler_digest TEXT NOT NULL, schema_version INTEGER NOT NULL,
 parent_sequence INTEGER REFERENCES accepted_generations(sequence),
 candidate_digest TEXT, actor_id TEXT, source_record_path TEXT, source_record_digest TEXT,
 CHECK ((sequence=0 AND parent_sequence IS NULL AND candidate_digest IS NULL
         AND actor_id IS NULL AND source_record_path IS NULL AND source_record_digest IS NULL)
     OR (sequence>0 AND parent_sequence=sequence-1 AND candidate_digest IS NOT NULL
         AND actor_id IS NOT NULL AND source_record_path IS NOT NULL
         AND source_record_digest IS NOT NULL))
) STRICT;
CREATE INDEX IF NOT EXISTS generations_by_oid ON accepted_generations(git_oid,sequence);
CREATE INDEX IF NOT EXISTS generations_by_candidate
 ON accepted_generations(candidate_digest,sequence) WHERE candidate_digest IS NOT NULL;
CREATE TABLE IF NOT EXISTS artifact_versions (
 identity TEXT NOT NULL, artifact_digest TEXT NOT NULL,
 occurrence_sequence INTEGER NOT NULL REFERENCES accepted_generations(sequence),
 path TEXT NOT NULL, predecessor_digest TEXT, artifact_revision INTEGER NOT NULL,
 PRIMARY KEY(identity,artifact_digest,occurrence_sequence)
) STRICT;
CREATE INDEX IF NOT EXISTS versions_by_digest
 ON artifact_versions(artifact_digest,identity,occurrence_sequence);
CREATE INDEX IF NOT EXISTS versions_by_path
 ON artifact_versions(path,occurrence_sequence DESC,identity);
CREATE INDEX IF NOT EXISTS versions_by_sequence ON artifact_versions(occurrence_sequence);
CREATE TABLE IF NOT EXISTS history_progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 instance_id TEXT NOT NULL, genesis_root TEXT NOT NULL,
 sequence INTEGER NOT NULL REFERENCES accepted_generations(sequence)
) STRICT;
"""

_MEMBER_SCHEMA = """
CREATE TABLE accepted_member_locations (
 sequence INTEGER NOT NULL REFERENCES accepted_generations(sequence),
 member_ordinal INTEGER NOT NULL CHECK(member_ordinal>=0),
 member_path TEXT NOT NULL, artifact_kind TEXT NOT NULL,
 artifact_identity TEXT, artifact_digest TEXT,
 has_law_evidence INTEGER NOT NULL CHECK(has_law_evidence IN (0,1)),
 PRIMARY KEY(sequence,member_ordinal)
) STRICT;
CREATE INDEX members_by_identity
 ON accepted_member_locations(artifact_identity,sequence DESC,member_ordinal);
CREATE INDEX members_by_path
 ON accepted_member_locations(member_path,sequence,member_ordinal);
"""
_SCHEMA = _HISTORY_SCHEMA + _MEMBER_SCHEMA


def _schema_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    # The shared working database also contains the proposal component. That
    # component verifies its own tables and indexes before using them. Unknown
    # objects and triggers still fail this check, including triggers on either
    # component that could mutate history as a side effect of a proposal write.
    return [
        row
        for row in rows
        if not (row[0] in {"table", "index"} and row[2] in {"proposals", "proposal_progress"})
    ]


def _expected_schema(schema: str) -> list[tuple[object, ...]]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(schema)
        return _schema_rows(reference)
    finally:
        reference.close()


_EXPECTED_SCHEMA = _expected_schema(_SCHEMA)
_PRE_MEMBER_SCHEMA = _expected_schema(_HISTORY_SCHEMA)


@dataclass(frozen=True)
class AcceptedGenerationLocation:
    sequence: int
    git_oid: str
    semantic_root: str
    generation_root: str
    compiler_digest: str
    schema_version: int
    parent_sequence: int | None
    candidate_digest: str | None
    actor_id: str | None
    source_record_path: str | None
    source_record_digest: str | None


@dataclass(frozen=True)
class ArtifactVersionLocation:
    identity: str
    artifact_digest: str
    occurrence_sequence: int
    path: str
    predecessor_digest: str | None
    artifact_revision: int


@dataclass(frozen=True)
class AcceptedMemberLocation:
    sequence: int
    member_ordinal: int
    member_path: str
    artifact_kind: str
    artifact_identity: str | None
    artifact_digest: str | None
    has_law_evidence: int


class HistoryReader:
    """A transaction-scoped reader. Locations identify retained bytes, not liveness."""

    def __init__(self, connection: sqlite3.Connection, sequence: int) -> None:
        self._connection = connection
        self._sequence = sequence

    @property
    def sequence(self) -> int:
        return self._sequence

    def generation(self, sequence: int) -> AcceptedGenerationLocation:
        if sequence < 0 or sequence > self.sequence:
            raise PlaybillFormatError("generation is outside requested accepted history")
        row = self._connection.execute(
            "SELECT * FROM accepted_generations WHERE sequence=?", (sequence,)
        ).fetchone()
        if row is None:
            raise ProjectionIntegrityError("accepted history locator is incomplete")
        return AcceptedGenerationLocation(*row)

    def resolve(self, at: AcceptedCoordinate) -> AcceptedGenerationLocation:
        rows = self._connection.execute(
            "SELECT * FROM accepted_generations WHERE git_oid=? AND sequence<=? "
            "AND semantic_root=? AND generation_root=? AND compiler_digest=?",
            (at.git_oid, self.sequence, at.semantic_root, at.generation_root, at.compiler_digest),
        ).fetchall()
        if len(rows) != 1:
            raise PlaybillFormatError("coordinate is not one generation in requested history")
        return AcceptedGenerationLocation(*rows[0])

    def candidate_accepted(self, candidate_digest: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM accepted_generations "
                "WHERE candidate_digest=? AND sequence<=? LIMIT 1",
                (candidate_digest, self.sequence),
            ).fetchone()
            is not None
        )

    def member_history(self, path: str) -> tuple[AcceptedMemberLocation, ...]:
        """All member occurrences, including evaluations with unchanged bytes."""
        return tuple(
            AcceptedMemberLocation(*row)
            for row in self._connection.execute(
                "SELECT * FROM accepted_member_locations WHERE member_path=? "
                "AND sequence<=? ORDER BY sequence,member_ordinal",
                (path, self.sequence),
            )
        )

    def claim_law_evidence(
        self, identity: str, *, artifact_digest: str, path: str
    ) -> AcceptedMemberLocation | None:
        row = self._connection.execute(
            "SELECT * FROM accepted_member_locations WHERE artifact_identity=? "
            "AND sequence<=? ORDER BY sequence DESC,member_ordinal DESC LIMIT 1",
            (identity, self.sequence),
        ).fetchone()
        if row is None:
            return None
        location = AcceptedMemberLocation(*row)
        if (
            location.artifact_kind != "claim"
            or location.member_path != path
            or location.artifact_digest != artifact_digest
            or not location.has_law_evidence
        ):
            raise ProjectionIntegrityError(
                "latest Claim law evidence differs from requested version"
            )
        return location

    def read_member_record(
        self,
        location: AcceptedMemberLocation,
        load_record: Callable[[str, str], bytes | None],
    ) -> ChangeSetRecordAnyVersion:
        """Verify one exact retained record; a locator never substitutes for it."""
        generation = self.generation(location.sequence)
        if generation.source_record_path is None:
            raise ProjectionIntegrityError("genesis has no member evidence record")
        raw = load_record(generation.git_oid, generation.source_record_path)
        if raw is None:
            raise ProjectionIntegrityError("accepted member source record is unavailable")
        record = parse_change_set_record(raw, path=generation.source_record_path)
        if (
            record.sequence != generation.sequence
            or record.changeset_digest != generation.source_record_digest
            or record.candidate_digest != generation.candidate_digest
            or record.compiler_digest != generation.compiler_digest
            or not 0 <= location.member_ordinal < len(record.members)
        ):
            raise ProjectionIntegrityError("accepted member source record binding differs")
        member = record.members[location.member_ordinal]
        if (
            member.path != location.member_path
            or member.artifact_kind != location.artifact_kind
            or _member_digest(member) != location.artifact_digest
        ):
            raise ProjectionIntegrityError("accepted member locator differs from retained member")
        return record

    def read_claim_law_evidence(
        self,
        identity: str,
        *,
        artifact_digest: str,
        path: str,
        load_record: Callable[[str, str], bytes | None],
    ) -> MemberLawEvaluationV2 | None:
        location = self.claim_law_evidence(identity, artifact_digest=artifact_digest, path=path)
        if location is None:
            return None
        record = self.read_member_record(location, load_record)
        matches = [e for e in _law_evidence(record) if e.path == path]
        if len(matches) != 1:
            raise ProjectionIntegrityError("accepted Claim law evidence is missing or ambiguous")
        return matches[0]

    def artifact(
        self, artifact_digest: str, *, identity: str | None = None
    ) -> ArtifactVersionLocation | None:
        """Latest occurrence of an exact version within this prefix, even if now deleted.

        Digest-only lookup refuses multiple identities. A missing version is None;
        callers must never substitute today's version for a missing historical one.
        """
        if identity is None:
            identities = self._connection.execute(
                "SELECT DISTINCT identity FROM artifact_versions "
                "WHERE artifact_digest=? AND occurrence_sequence<=? LIMIT 2",
                (artifact_digest, self.sequence),
            ).fetchall()
            if len(identities) > 1:
                raise PlaybillFormatError("one accepted artifact digest names multiple identities")
            if not identities:
                return None
            identity = identities[0][0]
        row = self._connection.execute(
            "SELECT * FROM artifact_versions WHERE identity=? AND artifact_digest=? "
            "AND occurrence_sequence<=? ORDER BY occurrence_sequence DESC LIMIT 1",
            (identity, artifact_digest, self.sequence),
        ).fetchone()
        return None if row is None else ArtifactVersionLocation(*row)

    def occurrences(self, identity: str) -> tuple[ArtifactVersionLocation, ...]:
        rows = self._connection.execute(
            "SELECT * FROM artifact_versions WHERE identity=? AND occurrence_sequence<=? "
            "ORDER BY occurrence_sequence,artifact_digest",
            (identity, self.sequence),
        ).fetchall()
        return tuple(ArtifactVersionLocation(*row) for row in rows)


EnvelopeLoader = Callable[[int], Sequence[ArtifactEnvelopeRow]]


def _record_members(
    record: ChangeSetRecordAnyVersion,
) -> Sequence[CandidateMemberEvidence | CandidateMemberLawEvidenceV2]:
    return record.members


def _law_evidence(record: ChangeSetRecordAnyVersion) -> tuple[MemberLawEvaluationV2, ...]:
    return () if isinstance(record, ChangeSetRecord) else record.law_evidence


def _member_digest(member: object) -> str | None:
    # Early retained formats carry an input digest, not a candidate artifact
    # digest. Preserve that distinction rather than reinterpreting old bytes.
    value = getattr(member, "candidate_artifact_digest", getattr(member, "artifact_digest", None))
    return value if isinstance(value, str) else None


class AcceptedHistoryIndex:
    """Shared derived owner; no source mutation and no frozen compiler schema change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._ready: tuple[int, str, str, str, int] | None = None
        self._stamp: tuple[int, ...] | None = None
        self._writer: sqlite3.Connection | None = None
        self._writer_identity: tuple[int, ...] | None = None
        self._close_writer: Callable[[], None] | None = None
        self.generations_checked = 0
        self.generations_written = 0
        self.artifact_rows_written = 0

    def invalidate(self) -> None:
        """Force full source reconciliation on the next read.

        Accepted prefix readiness survives ordinary derived-root eviction and
        successful refresh: _sync proves the retained prefix still belongs to
        the new verified epoch. It is not a head-scoped memo.
        """
        with self._lock:
            self._ready = None
            self._stamp = None

    def proposal_committed(self, before_stamp: tuple[int, ...] | None) -> None:
        """Preserve verified history across a known proposal-only transaction.

        The proposal component holds our lock and captures ``before_stamp``
        after acquiring its SQLite writer transaction, before changing rows.
        Call only after commit. A previously unexplained file change must still
        force reconciliation; this callback cannot certify that earlier change.
        """
        with self._lock:
            after_stamp = self._file_stamp()
            if (
                self._ready is not None
                and before_stamp is not None
                and before_stamp == self._stamp
                and after_stamp is not None
                and after_stamp[:2] == before_stamp[:2]
            ):
                self._stamp = after_stamp
            else:
                self.invalidate()

    def _file_stamp(self) -> tuple[int, ...] | None:
        if self.path.is_symlink():
            raise ProjectionIntegrityError("history index must not be a symlink")
        try:
            value = self.path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode):
            raise ProjectionIntegrityError("history index must be a regular file")
        result = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        wal = self.path.with_name(self.path.name + "-wal")
        if wal.is_symlink():
            raise ProjectionIntegrityError("history WAL must not be a symlink")
        try:
            value = wal.stat()
        except FileNotFoundError:
            return result
        return (
            *result,
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def _write_connection(self) -> sqlite3.Connection:
        """Keep one idle connection so closing readers does not checkpoint the WAL.

        The connection has no transaction between synchronizations. Its finalizer
        closes it when this instance-owned adapter is released.
        """
        stamp = self._file_stamp()
        identity = None if stamp is None else stamp[:2]
        if self._writer is not None and identity != self._writer_identity:
            assert self._close_writer is not None
            self._close_writer()
            self._writer = None
            self.invalidate()
        if self._writer is None:
            connection = sqlite3.connect(self.path, check_same_thread=False)
            self._writer = connection
            self._close_writer = weakref.finalize(self, connection.close)
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
                raise ProjectionIntegrityError("history index requires WAL mode")
            schema = _schema_rows(connection)
            if not schema:
                connection.executescript(_SCHEMA)
            elif schema == _PRE_MEMBER_SCHEMA:
                connection.executescript(_MEMBER_SCHEMA)
                self.invalidate()
            stamp = self._file_stamp()
            self._writer_identity = None if stamp is None else stamp[:2]
        return self._writer

    @contextmanager
    def read(
        self,
        recovered: RecoveredInstanceState,
        load_envelopes: EnvelopeLoader,
        *,
        at: AcceptedCoordinate | None = None,
    ) -> Iterator[HistoryReader]:
        connection: sqlite3.Connection | None = None
        try:
            # Serialize only synchronization and snapshot acquisition. Neither
            # this lock nor the writer transaction survives into caller code.
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                writer = self._write_connection()
                try:
                    writer.execute("BEGIN IMMEDIATE")
                    if _schema_rows(writer) != _EXPECTED_SCHEMA:
                        raise ProjectionIntegrityError(
                            "history index schema differs; rebuild required"
                        )
                    source_version = writer.execute("PRAGMA data_version").fetchone()
                    stamp = self._file_stamp()
                    ready = self._ready if stamp is not None and stamp == self._stamp else None
                    self._sync(writer, recovered, load_envelopes, ready)
                    writer.commit()
                except BaseException:
                    writer.rollback()
                    raise
                published_stamp = self._file_stamp()
                self._stamp = published_stamp
                self._ready = (
                    recovered.head.sequence,
                    recovered.head.generation_root.tagged,
                    recovered.coordinate.instance_id,
                    recovered.coordinate.compiler.rule_digest,
                    recovered.coordinate.compiler.schema_version,
                )
                connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN DEFERRED")
                reader = HistoryReader(connection, recovered.head.sequence)
                # BEGIN alone does not establish a snapshot. Pin it before
                # releasing the acquisition lock, including for an empty caller.
                reader.resolve(
                    AcceptedCoordinate(
                        git_oid=recovered.head.oid,
                        semantic_root=recovered.head.semantic_root.tagged,
                        generation_root=recovered.head.generation_root.tagged,
                        compiler_digest=recovered.coordinate.compiler.rule_digest,
                    )
                )
                if (
                    self._file_stamp() != published_stamp
                    or writer.execute("PRAGMA data_version").fetchone() != source_version
                ):
                    self.invalidate()
                    raise ProjectionIntegrityError(
                        "history index changed during snapshot acquisition"
                    )
                if at is not None:
                    reader = HistoryReader(connection, reader.resolve(at).sequence)
            yield reader
            current_stamp = self._file_stamp()
            if (
                published_stamp is None
                or current_stamp is None
                or published_stamp[:2] != current_stamp[:2]
            ):
                self.invalidate()
                raise ProjectionIntegrityError("history index file was replaced during read")
        except sqlite3.DatabaseError as exc:
            self.invalidate()
            raise ProjectionIntegrityError(
                "accepted history index could not be read or updated; repair or retry required"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def _sync(
        self,
        connection: sqlite3.Connection,
        recovered: RecoveredInstanceState,
        load_envelopes: EnvelopeLoader,
        ready: tuple[int, str, str, str, int] | None,
    ) -> None:
        history = recovered.history
        start = 0
        if ready is not None:
            sequence, root, instance_id, compiler, schema = ready
            if (
                instance_id == recovered.coordinate.instance_id
                and compiler == recovered.coordinate.compiler.rule_digest
                and schema == recovered.coordinate.compiler.schema_version
                and sequence < len(history)
                and history[sequence].generation_root.tagged == root
            ):
                start = sequence + 1
        # A foreign/replaced/truncated epoch cannot leave future rows visible.
        connection.execute(
            "DELETE FROM history_progress WHERE singleton=1 AND sequence>?",
            (recovered.head.sequence,),
        )
        connection.execute(
            "DELETE FROM accepted_member_locations WHERE sequence>?", (recovered.head.sequence,)
        )
        connection.execute(
            "DELETE FROM artifact_versions WHERE occurrence_sequence>?", (recovered.head.sequence,)
        )
        connection.execute(
            "DELETE FROM accepted_generations WHERE sequence>?", (recovered.head.sequence,)
        )
        for position in range(start, len(history)):
            generation = history[position]
            if generation.sequence != position:
                raise ProjectionIntegrityError("verified history sequence is not contiguous")
            record = generation.record
            expected = (
                position,
                generation.oid,
                generation.semantic_root.tagged,
                generation.generation_root.tagged,
                recovered.coordinate.compiler.rule_digest,
                recovered.coordinate.compiler.schema_version,
                position - 1 if position else None,
                record.candidate_digest if record else None,
                record.actor_binding.actor_id if record else None,
                f"changesets/cs-{position:020d}.json" if record else None,
                record.changeset_digest if record else None,
            )
            envelopes = load_envelopes(position)
            versions = sorted(
                (
                    row.identity,
                    row.artifact_digest,
                    position,
                    row.path,
                    row.predecessor_digest,
                    row.revision,
                )
                for row in envelopes
            )
            self.generations_checked += 1
            actual = connection.execute(
                "SELECT * FROM accepted_generations WHERE sequence=?", (position,)
            ).fetchone()
            actual_versions = connection.execute(
                "SELECT * FROM artifact_versions WHERE occurrence_sequence=? "
                "ORDER BY identity,artifact_digest",
                (position,),
            ).fetchall()
            if actual != expected or actual_versions != versions:
                # Update in place so later retained occurrences keep their FK.
                connection.execute(
                    "INSERT INTO accepted_generations VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(sequence) DO UPDATE SET "
                    "git_oid=excluded.git_oid, semantic_root=excluded.semantic_root, "
                    "generation_root=excluded.generation_root, "
                    "compiler_digest=excluded.compiler_digest, "
                    "schema_version=excluded.schema_version, "
                    "parent_sequence=excluded.parent_sequence, "
                    "candidate_digest=excluded.candidate_digest, "
                    "actor_id=excluded.actor_id, source_record_path=excluded.source_record_path, "
                    "source_record_digest=excluded.source_record_digest",
                    expected,
                )
                connection.execute(
                    "DELETE FROM artifact_versions WHERE occurrence_sequence=?", (position,)
                )
                connection.executemany(
                    "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?)", versions
                )
                self.generations_written += 1
                self.artifact_rows_written += len(versions)
            members = []
            if record is not None:
                law_paths = {e.path for e in _law_evidence(record)}
                for ordinal, member in enumerate(_record_members(record)):
                    digest = _member_digest(member)
                    identity_digest = digest or getattr(member, "predecessor_artifact_digest", None)
                    identities = connection.execute(
                        "SELECT DISTINCT identity FROM artifact_versions WHERE path=? "
                        "AND artifact_digest=? AND occurrence_sequence<=? LIMIT 2",
                        (member.path, identity_digest, position),
                    ).fetchall()
                    identity = identities[0][0] if len(identities) == 1 else None
                    members.append(
                        (
                            position,
                            ordinal,
                            member.path,
                            member.artifact_kind,
                            identity,
                            digest,
                            int(member.path in law_paths),
                        )
                    )
            actual_members = connection.execute(
                "SELECT * FROM accepted_member_locations WHERE sequence=? ORDER BY member_ordinal",
                (position,),
            ).fetchall()
            if actual_members != members:
                connection.execute(
                    "DELETE FROM accepted_member_locations WHERE sequence=?", (position,)
                )
                connection.executemany(
                    "INSERT INTO accepted_member_locations VALUES (?,?,?,?,?,?,?)", members
                )
        progress = (
            recovered.coordinate.instance_id,
            history[0].generation_root.tagged,
            recovered.head.sequence,
        )
        if (
            connection.execute(
                "SELECT instance_id,genesis_root,sequence FROM history_progress WHERE singleton=1"
            ).fetchone()
            != progress
        ):
            connection.execute(
                "INSERT INTO history_progress VALUES (1,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET instance_id=excluded.instance_id, "
                "genesis_root=excluded.genesis_root, sequence=excluded.sequence",
                progress,
            )
