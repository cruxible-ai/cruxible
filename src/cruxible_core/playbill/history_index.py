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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.errors import PlaybillFormatError, ProjectionIntegrityError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.projection_artifacts import ArtifactEnvelopeRow
from cruxible_core.playbill.recovery import RecoveredInstanceState

_SCHEMA = """
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


def _schema_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()


def _expected_schema() -> list[tuple[object, ...]]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_SCHEMA)
        return _schema_rows(reference)
    finally:
        reference.close()


_EXPECTED_SCHEMA = _expected_schema()


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


class AcceptedHistoryIndex:
    """Shared derived owner; no source mutation and no frozen compiler schema change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._ready: tuple[int, str, str, str, int] | None = None
        self._stamp: tuple[int, int, int, int, int] | None = None
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

    def _file_stamp(self) -> tuple[int, int, int, int, int] | None:
        if self.path.is_symlink():
            raise ProjectionIntegrityError("history index must not be a symlink")
        try:
            value = self.path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode):
            raise ProjectionIntegrityError("history index must be a regular file")
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns

    @contextmanager
    def read(
        self,
        recovered: RecoveredInstanceState,
        load_envelopes: EnvelopeLoader,
        *,
        at: AcceptedCoordinate | None = None,
    ) -> Iterator[HistoryReader]:
        # Serialize local publication and reader acquisition. The SQLite read
        # transaction also keeps other processes' suffix publication invisible.
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            opened_stamp = self._file_stamp()  # Refuse symlinks before opening.
            connection = sqlite3.connect(self.path)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                # Check the schema before issuing DML: unexpected triggers or
                # views must not rewrite rows during source reconciliation.
                if not _schema_rows(connection):
                    connection.executescript(_SCHEMA)
                connection.execute("BEGIN IMMEDIATE")
                if _schema_rows(connection) != _EXPECTED_SCHEMA:
                    raise ProjectionIntegrityError("history index schema differs; rebuild required")
                stamp = self._file_stamp()
                if opened_stamp is not None and (stamp is None or opened_stamp[:2] != stamp[:2]):
                    self.invalidate()
                    raise ProjectionIntegrityError("history index file was replaced while opening")
                ready = self._ready if stamp is not None and stamp == self._stamp else None
                self._sync(connection, recovered, load_envelopes, ready)
                connection.execute("PRAGMA query_only=ON")
                reader = HistoryReader(connection, recovered.head.sequence)
                if at is not None:
                    reader = HistoryReader(connection, reader.resolve(at).sequence)
                yield reader
                connection.commit()
                published_stamp = self._file_stamp()
                if stamp is None or published_stamp is None or stamp[:2] != published_stamp[:2]:
                    self.invalidate()
                    raise ProjectionIntegrityError("history index file was replaced during read")
                self._stamp = published_stamp
                self._ready = (
                    recovered.head.sequence,
                    recovered.head.generation_root.tagged,
                    recovered.coordinate.instance_id,
                    recovered.coordinate.compiler.rule_digest,
                    recovered.coordinate.compiler.schema_version,
                )
            except sqlite3.DatabaseError as exc:
                self.invalidate()
                raise ProjectionIntegrityError(
                    "accepted history index could not be read or updated; repair or retry required"
                ) from exc
            finally:
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
