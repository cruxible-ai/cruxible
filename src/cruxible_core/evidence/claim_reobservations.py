"""Recorded re-observations: matching content, or the same typed extraction (D-R8).

Restating a Claim on a Capture whose observed content it already cites, under
the same contract and in the same citation association, is a no-op: no
revision, no generation, no proposal. The newer Capture is not discarded: it is
recorded here, on the evidence plane, so the cited content's freshness can move
to when it was last observed (``claim_verdicts.CaptureReobservationEvidence``).

Storage follows the Line inbox's pattern: a journal (``claim-reobservation-v1``)
is the append-only authority, and a SQLite projection keyed by Claim lineage is
caught up from its last applied sequence on every open. A record is admitted only
for a Claim live at the current head, held still through the append, and a
daemon-admitted Capture that verifies there and whose observed content equals a
Capture that Claim cites under the same contract, or whose core extraction proves
the same value and record key, including new production lineage. The
freshness lookup reads one indexed range per Claim lineage, never a scan.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from cruxible_client.contracts.captures import (
    CaptureEnvelopeAny,
    observed_content,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_evidence import (
    AbsenceWitness,
    ClaimEvidenceReceipt,
    ExtractedBasis,
)
from cruxible_client.contracts.claim_verdicts import (
    CaptureReobservationEvidence,
    CaptureVerdictEvidence,
)
from cruxible_client.contracts.claims import claim_artifact_digest, claim_path, parse_claim
from cruxible_client.contracts.errors import CruxibleError, ReadRefusalError
from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.procedures.node_summary import ReobservationRefusalReason
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
from cruxible_core.storage.read_positions import reobservations_through

_LOCK = threading.RLock()
_FENCE = "claim-reobservation-local-v1"
_PARTITION = "reobservations"
_EVENT_TAG = "cruxible-claim-reobservation-v1"
#: One acquiring call's observation of every content it fetched (ruling 1a):
#: recorded at the fetch, keyed by (contract, observed content), never per Claim.
CONTENT_OBSERVATION_TAG = "cruxible-content-observation-v1"
#: The (contract, content, Capture) set one observation covers: run-free, so a
#: call that fetched the same contents again cites the body it stored before.
OBSERVED_CONTENTS_BODY = "cruxible-observed-contents-v1"
#: One extract step's observation of its whole extracted row set (ruling
#: 2026-11-16 a), keyed by each row's (contract, source, record key, value)
#: digest: a Claim produced from a row is fresh as of the latest observation of
#: a set holding it, whatever bytes carried the rows. The same record also
#: carries a complete absence enumeration's key (F-001).
ROW_OBSERVATION_TAG = "cruxible-row-observation-v1"
#: The (contract, row) set one row observation covers: run-free and
#: Capture-free, so the same rows in new bytes cite the body stored before.
OBSERVED_ROWS_BODY = "cruxible-observed-rows-v1"
#: Which row each record of one Capture's extraction is, by extract spec and
#: record locator: how a Claim citing that record finds the row it depends on.
ROW_MAP_BODY = "cruxible-row-map-v1"
#: The row maps of one extract step's Captures (run-free).
ROW_MAPS_BODY = "cruxible-row-maps-v1"
_ACCESS = BodyAccessContext(principal_id="claim-reobservation", can_read_body=True)
_SCHEMA_VERSION = 8
REOBSERVATION_DIRECTORY = "claim-reobservations"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# ``reobservations`` keeps the full history (audit, historical verification).
# Freshness never reads it: records append in recorded order, so the latest
# observation of one (lineage, contract, content) is a step function of the
# recording instant. ``reobservation_points`` holds one row per step -- written
# only when a record raises that key's running latest observation (the larger
# Capture digest breaks a tie) -- keyed by recording instant, and the answer at
# an instant is the last step recorded by it: one seek, whatever the history.
# Each step keeps its journal sequence, so a read recorded at a journal position
# counts only the steps through it (``read_positions``): the steps are written in
# sequence order, so those are exactly the steps the projection held then.
# Each record also names the Claim version (artifact digest) live when it was
# admitted, so a version's latest observation by a given instant is one seek on
# ``reobservations_by_version`` (prediction settlement), however many records of
# the lineage, or of that version observed later, there are.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
 singleton INTEGER PRIMARY KEY, sequence INTEGER NOT NULL, recorded_us INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS reobservations (
 claim_id TEXT NOT NULL, capture_digest TEXT NOT NULL, contract_digest TEXT NOT NULL,
 observed_content_digest TEXT NOT NULL, observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
 observed_us INTEGER NOT NULL, recorded_us INTEGER NOT NULL, sequence INTEGER NOT NULL,
 claim_artifact_digest TEXT NOT NULL,
 PRIMARY KEY(claim_id, capture_digest)
) STRICT;
CREATE INDEX IF NOT EXISTS reobservations_by_sequence ON reobservations (claim_id, sequence);
CREATE INDEX IF NOT EXISTS reobservations_by_version
 ON reobservations (claim_id, claim_artifact_digest, observed_us, capture_digest);
CREATE TABLE IF NOT EXISTS reobservation_points (
 claim_id TEXT NOT NULL, contract_digest TEXT NOT NULL, observed_content_digest TEXT NOT NULL,
 recorded_us INTEGER NOT NULL, observed_us INTEGER NOT NULL, capture_digest TEXT NOT NULL,
 sequence INTEGER NOT NULL,
 PRIMARY KEY(claim_id, contract_digest, observed_content_digest, recorded_us, observed_us,
  capture_digest)
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS observed_sets (
 contract_digest TEXT NOT NULL, observed_content_digest TEXT NOT NULL, set_digest TEXT NOT NULL,
 capture_digest TEXT NOT NULL,
 PRIMARY KEY(contract_digest, observed_content_digest, set_digest)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS observed_sets_by_set ON observed_sets (set_digest);
CREATE TABLE IF NOT EXISTS instants (
 sequence INTEGER PRIMARY KEY, recorded_us INTEGER NOT NULL, generation INTEGER NOT NULL,
 latest_us INTEGER NOT NULL, bucket INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS instants_by_recorded ON instants (recorded_us, sequence);
CREATE INDEX IF NOT EXISTS instants_by_generation ON instants (generation, sequence);
CREATE INDEX IF NOT EXISTS instants_by_delay ON instants (bucket, latest_us);
CREATE TABLE IF NOT EXISTS delay_buckets (bucket INTEGER PRIMARY KEY) STRICT;
CREATE TABLE IF NOT EXISTS observation_sets (
 set_digest TEXT PRIMARY KEY, procedure TEXT NOT NULL, last_sequence INTEGER NOT NULL,
 earliest_us INTEGER NOT NULL, earliest_recorded_us INTEGER NOT NULL,
 earliest_sequence INTEGER NOT NULL, latest_us INTEGER NOT NULL
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS observation_points (
 set_digest TEXT NOT NULL, sequence INTEGER NOT NULL, recorded_us INTEGER NOT NULL,
 earliest_us INTEGER NOT NULL, latest_us INTEGER NOT NULL,
 PRIMARY KEY(set_digest, sequence)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS observation_points_by_sequence ON observation_points (sequence);
CREATE TABLE IF NOT EXISTS set_peaks (
 set_digest TEXT NOT NULL, sequence INTEGER NOT NULL, recorded_us INTEGER NOT NULL,
 earliest_us INTEGER NOT NULL, earliest_recorded_us INTEGER NOT NULL,
 earliest_sequence INTEGER NOT NULL, latest_us INTEGER NOT NULL,
 PRIMARY KEY(set_digest, sequence)
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS source_heads (
 source TEXT PRIMARY KEY, set_digest TEXT NOT NULL
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS key_sources (
 contract_digest TEXT NOT NULL, observed_content_digest TEXT NOT NULL, source TEXT NOT NULL,
 PRIMARY KEY(contract_digest, observed_content_digest, source)
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS key_heads (
 contract_digest TEXT NOT NULL, observed_content_digest TEXT NOT NULL, source TEXT NOT NULL,
 sequence INTEGER NOT NULL, recorded_us INTEGER NOT NULL, set_digest TEXT NOT NULL,
 carried_us INTEGER NOT NULL, carried_recorded_us INTEGER NOT NULL,
 carried_sequence INTEGER NOT NULL, carried_capture TEXT NOT NULL,
 carried_latest_us INTEGER NOT NULL,
 PRIMARY KEY(contract_digest, observed_content_digest, source, sequence)
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS capture_rows (
 capture_digest TEXT NOT NULL, spec_digest TEXT NOT NULL, locator TEXT NOT NULL,
 contract_digest TEXT NOT NULL, row_digest TEXT NOT NULL,
 PRIMARY KEY(capture_digest, spec_digest, locator)
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seen_maps (digest TEXT PRIMARY KEY) STRICT, WITHOUT ROWID;
"""
# Content observations (ruling 1a) and row observations (2026-11-16), as maintained heads.
#
# ``observed_sets`` holds one row per member of each distinct observed set,
# written once when the set first appears. Every observation writes one
# ``observation_points`` row (the history, for bounded windows only), one
# ``instants`` row (its recording instant and the accepted generation it was
# made at) and moves its set's ``observation_sets`` head, which keeps the set's
# running maxima: the greatest call-earliest instant (with the record that set
# it) and the greatest call-latest instant. ``set_peaks`` holds one row per
# change of those maxima, by sequence: a set's maxima through any journal
# position are one seek, so an older call recorded later never moves freshness
# back (F-003), and historical and read-position reads see exactly the records
# they admit.
#
# Each content key's observations are reached through ``key_heads``: for each
# observing source (Procedure and call node), the set the source's latest call
# holding the key observed, with the best observation of the key carried from
# the sets that source held it in before. A row is written only when a source's
# call observes a different set than its last (a change: the same O(set) as the
# set's first appearance); an unchanged call writes none. A key's freshness
# through a position is then, per source, one seek on ``key_heads`` and one on
# ``set_peaks``: never a walk over the sets or calls that ever held it (F-004).
# Recorded order is journal order, so a recording instant maps to a journal
# position by one seek on ``instants``; so does an accepted generation (the
# head a record was made at only moves forward).
_LAST_POINT = (
    "SELECT capture_digest, observed_us, recorded_us FROM reobservation_points "
    "WHERE claim_id=? AND contract_digest=? AND observed_content_digest=? AND recorded_us<=? "
    "AND sequence<=? ORDER BY recorded_us DESC, observed_us DESC, capture_digest DESC LIMIT 1"
)
_NEXT_POINT = (
    "SELECT recorded_us FROM reobservation_points "
    "WHERE claim_id=? AND contract_digest=? AND observed_content_digest=? AND recorded_us>? "
    "AND sequence<=? ORDER BY recorded_us LIMIT 1"
)
_NEVER = 1 << 62
_LAST_SEQUENCE = "SELECT MAX(sequence) FROM reobservations WHERE claim_id=? AND sequence<=?"
_VERSION_RECORD = (
    "SELECT capture_digest, contract_digest, observed_content_digest, observed_at, recorded_at, "
    "sequence FROM reobservations INDEXED BY reobservations_by_version "
    "WHERE claim_id=? AND claim_artifact_digest=? AND observed_us<=? AND sequence<=? "
    "ORDER BY observed_us DESC, capture_digest DESC LIMIT 1"
)


def _through() -> int:
    """The last journal sequence the read in progress counts: every one outside a read."""

    through = reobservations_through()
    return _NEVER if through is None else through


def _micros(value: datetime) -> int:
    return (value.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)


def _from_micros(value: object) -> datetime:
    return _EPOCH + timedelta(microseconds=int(str(value)))


class ClaimReobservationRefused(CruxibleError):
    """A re-observation that is not admissible is never recorded."""

    error_code = "cruxible.claim.reobservation_refused"

    def __init__(self, message: str, *, reason: ReobservationRefusalReason) -> None:
        #: The closed reason a run's claims summary counts the refusal under.
        self.reason: ReobservationRefusalReason = reason
        super().__init__(f"{self.error_code}: {message}")


class ClaimReobservationJournalCorrupt(CruxibleError):
    """The journal holds a record recorded before the one ahead of it.

    The writer stamps each record with the daemon's clock at append, raised to
    the previous record's instant, so recorded order is journal order by
    construction; a journal that breaks it was not written by this store.
    """

    error_code = "cruxible.claim.reobservation_journal_corrupt"

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


@dataclass(frozen=True)
class RecordedReobservation:
    """A lineage's latest re-observation and the journal record that holds it.

    ``accepted_coordinate`` is the accepted head the record was admitted at:
    the Claim version live there is the version it re-observed.
    """

    record: ReobservationRecord
    sequence: int
    record_digest: str
    accepted_coordinate: AcceptedCoordinate


def _instant(value: object) -> datetime:
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise ValueError("re-observation instants are never empty")
    return parsed


def _root(instance: PlaybillInstance) -> Path:
    return instance.root / instance.descriptor.storage.exhaust / REOBSERVATION_DIRECTORY


_SHARED_TAGS = frozenset({CONTENT_OBSERVATION_TAG, ROW_OBSERVATION_TAG})


def _ordered(conn: sqlite3.Connection, sequence: int, recorded_at: datetime) -> int:
    """The record's recording instant, refused when it precedes the record ahead of it.

    Journal order is recorded order by construction (``record``): replay takes
    every instant as written and refuses a journal that breaks it.
    """

    row = conn.execute("SELECT recorded_us FROM progress WHERE singleton=1").fetchone()
    recorded_us = _micros(recorded_at)
    if row is not None and recorded_us < int(row[0]):
        raise ClaimReobservationJournalCorrupt(
            f"record {sequence} was recorded at {format_datetime(recorded_at)}, before "
            f"the record ahead of it ({format_datetime(_from_micros(row[0]))})"
        )
    return recorded_us


def _shared_body(instance: PlaybillInstance, digest: str, *, tag: str) -> dict[str, Any]:
    """One content or row observation's canonical body."""

    import json

    from cruxible_client.contracts.canonical import canonical_bytes

    content = instance.body_store().read(digest, access=_ACCESS)
    value = json.loads(content)
    if not isinstance(value, dict) or value.get("tag") != tag or canonical_bytes(value) != content:
        raise ValueError(f"invalid {tag} body")
    return value


@dataclass(frozen=True)
class ObservedContent:
    """One content an acquiring call observed: its contract, content and Capture."""

    contract_digest: str
    observed_content_digest: str
    capture_digest: str


@dataclass(frozen=True)
class RowMap:
    """One Capture's extracted records as rows: (record locator, row digest) each."""

    capture_digest: str
    spec_digest: str
    contract_digest: str
    rows: tuple[tuple[str, str], ...]


def row_digest(contract: str, source: str, key: object, fields: object) -> str:
    """One extracted row's identity: its contract, logical source, record key and value."""

    from cruxible_client.contracts.canonical import Sha256Value, typed_digest

    value = typed_digest(Sha256Value, "cruxible-row-value-v1", {"fields": fields}).tagged
    return typed_digest(
        Sha256Value,
        "cruxible-observed-row-v1",
        {"contract": contract, "source": source, "key": key, "value": value},
    ).tagged


def enumeration_digest(witness: AbsenceWitness) -> str:
    """A complete absence enumeration's key: what an absence Claim depends on (F-001).

    The logical source, the extract specification and the digest of every
    key the complete enumeration read: observed again only when a run's
    whole enumeration is complete and enumerates the same keys.
    """

    from cruxible_client.contracts.canonical import Sha256Value, typed_digest

    return typed_digest(
        Sha256Value,
        "cruxible-observed-enumeration-v1",
        {
            "source": witness.logical_source,
            "spec": witness.extract_spec,
            "keys": witness.enumerated_key_digest,
        },
    ).tagged


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
                    conn.execute("DROP TABLE IF EXISTS reobservation_points")
                    for table in (
                        "observed_sets",
                        "instants",
                        "limits",
                        "delay_buckets",
                        "observation_sets",
                        "observation_points",
                        "set_peaks",
                        "source_heads",
                        "key_sources",
                        "key_heads",
                        "capture_rows",
                        "seen_maps",
                        "transitions",
                        "transition_exceptions",
                    ):
                        conn.execute(f"DROP TABLE IF EXISTS {table}")
                    conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT sequence FROM progress WHERE singleton=1").fetchone()
                start = row[0] + 1 if row else 1
                generations: dict[str, int] = {}
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
                    if isinstance(payload, dict) and payload.get("tag") in _SHARED_TAGS:
                        self._apply_shared(
                            conn,
                            payload,
                            stored.record.sequence,
                            stored.record.recorded_at,
                            generation=self._generation(
                                stored.record.accepted_coordinate.git_oid, generations
                            ),
                        )
                        continue
                    if not isinstance(payload, dict) or payload.get("tag") != _EVENT_TAG:
                        raise ValueError("invalid Claim re-observation record")
                    if "claim_artifact_digest" not in payload:
                        # A record written before records named their version:
                        # the version live at the head it was admitted at (one
                        # Claim read, paid once, when the projection is rebuilt).
                        payload = {
                            **payload,
                            "claim_artifact_digest": self._version_at(
                                str(payload["claim_id"]),
                                stored.record.accepted_coordinate.git_oid,
                            ),
                        }
                    self._apply(conn, payload, stored.record.sequence, stored.record.recorded_at)
                conn.commit()
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _version_at(self, claim_id: str, git_oid: str) -> str:
        """The artifact digest of the Claim version at one accepted head ('' when absent)."""

        path = claim_path(claim_id)
        content = self.instance.blob_at(git_oid, path)
        if content is None:
            return ""
        return claim_artifact_digest(parse_claim(content, path=path)).tagged

    @staticmethod
    def _apply(
        conn: sqlite3.Connection, payload: Mapping[str, Any], sequence: int, recorded_at: datetime
    ) -> None:
        observed_at = _instant(payload["observed_at"])
        recorded_us = _ordered(conn, sequence, recorded_at)
        observed_us = _micros(observed_at)
        key = (payload["claim_id"], payload["contract_digest"], payload["observed_content_digest"])
        inserted = conn.execute(
            "INSERT OR IGNORE INTO reobservations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                payload["claim_id"],
                payload["capture_digest"],
                payload["contract_digest"],
                payload["observed_content_digest"],
                format_datetime(observed_at),
                format_datetime(recorded_at),
                observed_us,
                recorded_us,
                sequence,
                str(payload.get("claim_artifact_digest", "")),
            ),
        ).rowcount
        if inserted:
            last = conn.execute(_LAST_POINT, (*key, _NEVER, _NEVER)).fetchone()
            if last is None or (observed_us, payload["capture_digest"]) > (
                int(last[1]),
                str(last[0]),
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO reobservation_points VALUES (?,?,?,?,?,?,?)",
                    (*key, recorded_us, observed_us, payload["capture_digest"], sequence),
                )
        conn.execute("INSERT OR REPLACE INTO progress VALUES(1,?,?)", (sequence, recorded_us))

    def _generation(self, git_oid: str, known: dict[str, int]) -> int:
        """The accepted generation a record was made at (-1 when not in the history).

        One indexed lookup per record (``known`` remembers them for one catch-up).
        """

        if git_oid not in known:
            with self.instance.accepted_history_reader() as history:
                location = history.generation_for_oid(git_oid)
            known[git_oid] = -1 if location is None else int(location.sequence)
        return known[git_oid]

    def _apply_shared(
        self,
        conn: sqlite3.Connection,
        payload: Mapping[str, Any],
        sequence: int,
        recorded_at: datetime,
        *,
        generation: int,
    ) -> None:
        """Project one content or row observation: rows for what changed only.

        An observation writes its set's members once (the first time the set
        appears), then its point, its instant, its set's head and, when it
        raised the set's maxima, one change point. When the observing source's
        call observed another set before, each member key gets a ``key_heads``
        row carrying its best observation so far: that is the set's size, paid
        only on a change. A row observation's Capture row maps are projected
        the first time each map is seen (``capture_rows``): sized by the new
        Captures' records, never by the Claims produced from them.
        """

        recorded_us = _ordered(conn, sequence, recorded_at)
        earliest_us = _micros(_instant(payload["earliest"]))
        latest_us = _micros(_instant(payload["latest"]))
        bucket = max(0, recorded_us - latest_us).bit_length()
        conn.execute(
            "INSERT OR REPLACE INTO instants VALUES (?,?,?,?,?)",
            (sequence, recorded_us, generation, latest_us, bucket),
        )
        conn.execute("INSERT OR IGNORE INTO delay_buckets VALUES (?)", (bucket,))
        rows_tag = payload["tag"] == ROW_OBSERVATION_TAG
        set_digest = str(payload["set_digest"])
        head = conn.execute(
            "SELECT procedure, earliest_us, earliest_recorded_us, earliest_sequence, "
            "latest_us FROM observation_sets WHERE set_digest=?",
            (set_digest,),
        ).fetchone()
        maps = self._project_maps(conn, payload, need_rows=head is None) if rows_tag else {}
        if head is None:
            body = _shared_body(
                self.instance,
                set_digest,
                tag=OBSERVED_ROWS_BODY if rows_tag else OBSERVED_CONTENTS_BODY,
            )
            if rows_tag:
                # A row's reported Capture: one holding it in this record (an
                # enumeration's, the record's own root Capture).
                fallback = str(payload.get("capture", ""))
                members = [
                    (str(contract), str(row), maps.get(str(row), fallback))
                    for contract, row in body["rows"]
                ]
            else:
                members = [
                    (str(contract), str(content), str(capture))
                    for contract, content, capture in body["contents"]
                ]
            conn.executemany(
                "INSERT OR REPLACE INTO observed_sets VALUES (?,?,?,?)",
                [(contract, key, set_digest, capture) for contract, key, capture in members],
            )
            lineage = body.get("lineage")
            procedure = str(lineage.get("procedure", "")) if isinstance(lineage, dict) else ""
        else:
            procedure = str(head[0])
        source = f"{procedure}\x1f{payload.get('node_id', '')}"
        last = conn.execute(
            "SELECT set_digest FROM source_heads WHERE source=?", (source,)
        ).fetchone()
        if last is None or str(last[0]) != set_digest:
            self._switch_source(conn, source, set_digest, sequence, recorded_us)
        conn.execute(
            "INSERT OR IGNORE INTO observation_points VALUES (?,?,?,?,?)",
            (set_digest, sequence, recorded_us, earliest_us, latest_us),
        )
        best = (earliest_us, recorded_us, sequence)
        if head is not None and int(head[1]) >= earliest_us:
            best = (int(head[1]), int(head[2]), int(head[3]))
        latest = latest_us if head is None else max(latest_us, int(head[4]))
        if head is None or best[2] == sequence or latest != int(head[4]):
            conn.execute(
                "INSERT OR IGNORE INTO set_peaks VALUES (?,?,?,?,?,?,?)",
                (set_digest, sequence, recorded_us, *best, latest),
            )
        conn.execute(
            "INSERT OR REPLACE INTO observation_sets VALUES (?,?,?,?,?,?,?)",
            (set_digest, procedure, sequence, *best, latest),
        )
        conn.execute("INSERT OR REPLACE INTO progress VALUES(1,?,?)", (sequence, recorded_us))

    def _project_maps(
        self, conn: sqlite3.Connection, payload: Mapping[str, Any], *, need_rows: bool
    ) -> dict[str, str]:
        """Project a row observation's Capture row maps not seen before.

        Returns row -> a Capture holding it when ``need_rows`` (the set is new:
        its rows' Captures are written with its members, O(rows) once). A maps
        body seen before and an existing set read nothing: the unchanged case.
        """

        digest = payload.get("maps_digest")
        if not isinstance(digest, str):
            return {}
        seen = conn.execute("SELECT 1 FROM seen_maps WHERE digest=?", (digest,)).fetchone()
        if seen and not need_rows:
            return {}
        found: dict[str, str] = {}
        for map_digest in _shared_body(self.instance, digest, tag=ROW_MAPS_BODY)["maps"]:
            body = _shared_body(self.instance, str(map_digest), tag=ROW_MAP_BODY)
            capture = str(body["capture"])
            for _locator, row in body["rows"]:
                found.setdefault(str(row), capture)
            if conn.execute("SELECT 1 FROM seen_maps WHERE digest=?", (map_digest,)).fetchone():
                continue
            conn.executemany(
                "INSERT OR IGNORE INTO capture_rows VALUES (?,?,?,?,?)",
                [
                    (capture, str(body["spec"]), str(locator), str(body["contract"]), str(row))
                    for locator, row in body["rows"]
                ],
            )
            conn.execute("INSERT OR IGNORE INTO seen_maps VALUES (?)", (map_digest,))
        conn.execute("INSERT OR IGNORE INTO seen_maps VALUES (?)", (digest,))
        return found

    @staticmethod
    def _switch_source(
        conn: sqlite3.Connection, source: str, set_digest: str, sequence: int, recorded_us: int
    ) -> None:
        """Point each member key of the set at it for this source, carrying its best so far.

        A key's carried observation is the best of what it carried and what its
        previous set held for this source, as of just before this record: two
        seeks per member, written only when the source's set changed.
        """

        rows = []
        for contract, content, _capture in conn.execute(
            "SELECT contract_digest, observed_content_digest, capture_digest FROM observed_sets "
            "WHERE set_digest=?",
            (set_digest,),
        ).fetchall():
            prior = conn.execute(
                "SELECT k.carried_us, k.carried_recorded_us, k.carried_sequence, "
                "k.carried_capture, k.carried_latest_us, s.earliest_us, s.earliest_recorded_us, "
                "s.earliest_sequence, s.latest_us, o.capture_digest FROM key_heads k "
                "JOIN observation_sets s ON s.set_digest=k.set_digest "
                "JOIN observed_sets o ON o.contract_digest=k.contract_digest AND "
                "o.observed_content_digest=k.observed_content_digest AND "
                "o.set_digest=k.set_digest "
                "WHERE k.contract_digest=? AND k.observed_content_digest=? AND k.source=? "
                "ORDER BY k.sequence DESC LIMIT 1",
                (contract, content, source),
            ).fetchone()
            carried: tuple[int, int, int, str, int] = (-1, -1, -1, "", -1)
            if prior is not None:
                held = (int(prior[0]), int(prior[1]), int(prior[2]), str(prior[3]))
                peak = (int(prior[5]), int(prior[6]), int(prior[7]), str(prior[9]))
                top = max(held, peak, key=lambda item: (item[0], item[3]))
                carried = (*top, max(int(prior[4]), int(prior[8])))
            rows.append((contract, content, source, sequence, recorded_us, set_digest, *carried))
        conn.executemany("INSERT OR IGNORE INTO key_heads VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT OR IGNORE INTO key_sources VALUES (?,?,?)",
            [(row[0], row[1], source) for row in rows],
        )
        conn.execute("INSERT OR REPLACE INTO source_heads VALUES (?,?)", (source, set_digest))

    # -- admission ----------------------------------------------------------

    def _admissible(
        self,
        *,
        claim_id: str,
        capture_digest: str,
        coordinate: AcceptedProjectionCoordinate,
        extraction: ClaimEvidenceReceipt | None = None,
        extracted_value: object = None,
        cited_envelopes: dict[str, CaptureEnvelopeAny] | None = None,
        verified_witnesses: dict[object, None] | None = None,
    ) -> tuple[CaptureEnvelopeAny, str, str]:
        """The re-observing Capture's envelope, or the typed refusal.

        It must be a daemon-admitted Capture that verifies at ``coordinate``,
        the Claim must be live there, and the Capture's observed content must
        equal a Capture the Claim cites there under the same contract, or a
        core extraction receipt must prove the same value and key.
        The returned content digest is the cited Capture's unchanged digest:
        freshness keeps using its existing indexed key, even if other bytes in
        the newly observed catalog changed.

        An absence re-observation owes its witness's every Capture the same
        proof (``_witness_refusal``, D-R37 a6): over a list of Captures, not
        only the member the receipt cites.
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
                f"Capture {capture_digest} does not verify as daemon-admitted evidence: {exc}",
                reason="capture_not_admitted",
            ) from exc
        if isinstance(verified, str):
            raise ClaimReobservationRefused(
                f"Capture {capture_digest} is not admissible evidence here ({verified})",
                reason="capture_not_admitted",
            )
        envelope = verified.envelope
        content = self.instance.blob_at(coordinate.git_oid, claim_path(claim_id))
        if content is None:
            raise ClaimReobservationRefused(
                f"no accepted Claim {claim_id} to re-observe", reason="claim_absent"
            )
        claim = parse_claim(content, path=claim_path(claim_id))
        if claim.lifecycle.state != "live":
            raise ClaimReobservationRefused(
                f"Claim {claim_id} is {claim.lifecycle.state} at the head: a re-observation "
                "is recorded only for a live Claim",
                reason="claim_not_live",
            )
        if capture_digest in claim.backing.capture_digests:
            raise ClaimReobservationRefused(
                f"Claim {claim_id} already cites Capture {capture_digest}: "
                "nothing new was observed",
                reason="capture_already_cited",
            )
        original = claim.backing.procedure_evidence
        same_extraction = False
        if extraction is not None:
            same_extraction = (
                original is not None
                and original.value.basis == "extracted"
                and extraction.value.basis == "extracted"
                and extraction.value.capture_digest == capture_digest
                and original.value.record_key == extraction.value.record_key
                and claim.statement.object.model_dump(mode="json") == extracted_value
            )
            if (
                extraction.absence is not None
                and original is not None
                and original.absence is not None
            ):
                same_extraction = (
                    claim.statement.object.kind == "none"
                    and extraction.absence.capture == capture_digest
                    and extraction.absence.logical_source == original.absence.logical_source
                    and claim.statement.object.model_dump(mode="json") == extracted_value
                )
                if same_extraction:
                    refusal = self._witness_refusal(
                        extraction.absence,
                        envelope=envelope,
                        coordinate=coordinate,
                        verified=verified_witnesses,
                    )
                    if refusal is not None:
                        raise ClaimReobservationRefused(
                            refusal, reason="absence_witness_unverified"
                        )
            if not same_extraction:
                raise ClaimReobservationRefused(
                    "extraction does not prove the same value and key", reason="extraction_differs"
                )
        wanted = (envelope.capture_contract_digest, observed_content(envelope).digest)
        cited_captures = (
            (original.value.capture_digest,)
            if same_extraction
            and original is not None
            and (original.value.basis == "extracted" or original.value.basis == "capture")
            else claim.backing.capture_digests
        )
        for cited in cited_captures:
            try:
                held = None if cited_envelopes is None else cited_envelopes.get(cited)
                if held is None:
                    held = parse_capture_envelope(
                        self.instance.body_store().read(cited, access=_ACCESS)
                    )
                    if cited_envelopes is not None:
                        cited_envelopes[cited] = held
            except (CruxibleError, ValueError):
                continue
            held_digest = observed_content(held).digest
            if (held.capture_contract_digest, held_digest) == wanted or (
                same_extraction
                and original is not None
                and (original.value.basis == "extracted" or original.value.basis == "capture")
                and cited == original.value.capture_digest
                and held.capture_contract_digest == envelope.capture_contract_digest
            ):
                return envelope, held_digest, claim_artifact_digest(claim).tagged
        raise ClaimReobservationRefused(
            f"Capture {capture_digest} observed no content Claim {claim_id} "
            "cites under its contract",
            reason="content_not_cited",
        )

    def _witness_refusal(
        self,
        witness: AbsenceWitness,
        *,
        envelope: CaptureEnvelopeAny,
        coordinate: AcceptedProjectionCoordinate,
        verified: dict[object, None] | None,
    ) -> str | None:
        """Why an absence witness's Captures do not stand at ``coordinate``, or ``None``.

        Every Capture the enumeration read must verify there as daemon-admitted
        evidence (retained, its exact accepted CaptureContract, production
        proof) and name the witness's logical source. ``envelope`` is the cited
        Capture's, already verified. ``verified`` is the delivery's record of
        witnesses already found standing, keyed by the coordinate and the body
        store's landing head: many re-observations on one witness verify its
        members once, and no answer outlives a body landing or erasure.
        """

        from cruxible_core.service.evidence.capture_reads import (
            CaptureReadInvalid,
            verify_accepted_capture,
        )

        try:
            landed: object = self.instance.body_store().landings.head()
        except (OSError, CruxibleError, ValueError):
            landed = None
        key = (
            None
            if verified is None or landed is None
            else (coordinate.git_oid, landed, witness.captures, witness.logical_source)
        )
        if key is not None and verified is not None and key in verified:
            return None
        for member in witness.captures:
            held = envelope
            if member != witness.capture:
                try:
                    checked = verify_accepted_capture(
                        self.instance,
                        coordinate,
                        member,
                        access=_ACCESS,
                        captures=self.instance.capture_verification_context(),
                    )
                except (CaptureReadInvalid, CruxibleError, ReadRefusalError, ValueError) as exc:
                    return (
                        f"absence witness Capture {member} does not verify as daemon-admitted "
                        f"evidence: {exc}"
                    )
                if isinstance(checked, str):
                    return f"absence witness Capture {member} is not admissible here ({checked})"
                held = checked.envelope
            if getattr(held.source, "source_identity", None) != witness.logical_source:
                return f"absence witness Capture {member} names a different logical source"
        if key is not None and verified is not None:
            verified[key] = None
        return None

    @staticmethod
    def _at(
        *, claim_id: str, capture_digest: str, envelope: CaptureEnvelopeAny, now: datetime
    ) -> ReobservationRecord:
        """The record an admitted Capture makes at the clock's actual ``now``.

        Refuses a Capture observed after ``now``: the same check whether
        previewing or recording, against the instant the clock actually read.
        """

        observed_at = envelope.observed_at.astimezone(UTC)
        if observed_at > now:
            raise ClaimReobservationRefused(
                f"Capture {capture_digest} was observed at {format_datetime(observed_at)}, "
                f"after the re-observation is recorded ({format_datetime(now)})",
                reason="observed_after_recording",
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
        extraction: ClaimEvidenceReceipt | None = None,
        extracted_value: object = None,
        cited_envelopes: dict[str, CaptureEnvelopeAny] | None = None,
        verified_witnesses: dict[object, None] | None = None,
    ) -> ReobservationRecord | None:
        """Record one admissible re-observation at the current head; a repeat is the same record.

        Admission is judged at the accepted head, held still from the check
        through the durable append (``holding_accepted_head``), and only when the
        held ledger head is the verified coordinate: the Claim must be live
        there and cite a Capture of the same contract and observed content,
        and the re-observing Capture must be
        daemon-admitted, verify there, and not be observed after the clock's
        actual instant -- the same check a preview makes. The clock is the
        store's own: the instance's live recording clock
        (``PlaybillInstance.recording_clock``), which no caller supplies. The
        record's one recording instant is read from it at the durable append, raised to the previous
        record's so the journal is in recorded order by construction, and is
        the ``recorded_at`` the journal record carries. A historical record
        stays readable whatever later happens to its Claim; this decides only
        whether a new one may be written. Nothing is written while previewing
        (``dry_run``): a preview observes nothing durable.
        """

        if is_previewing():
            envelope, _cited, _version = self._admissible(
                claim_id=claim_id,
                capture_digest=capture_digest,
                coordinate=self.instance.accepted_coordinate(),
                extraction=extraction,
                extracted_value=extracted_value,
                cited_envelopes=cited_envelopes,
                verified_witnesses=verified_witnesses,
            )
            self._at(
                claim_id=claim_id,
                capture_digest=capture_digest,
                envelope=envelope,
                now=self.instance.recording_clock(),
            )
            return None
        # Accepted main is held still from the admission check through the
        # durable append, under the locks every acceptance takes in the order
        # it takes them (instance state, ledger activation), then this store's.
        with self.instance.holding_accepted_head() as main, self._locked(create=True) as conn:
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
            if accepted.git_oid != main:
                raise ClaimReobservationRefused(
                    "accepted state moved past this instance's verified head; "
                    "the re-observation is judged again at the new head",
                    reason="head_moved",
                )
            # Admission's immutable work first: verify the Capture, the live Claim
            # and its citations at the held head, then recover any reserved
            # material and the journal head and activate the writer.
            envelope, cited_content, version = self._admissible(
                claim_id=claim_id,
                capture_digest=capture_digest,
                coordinate=accepted,
                extraction=extraction,
                extracted_value=extracted_value,
                cited_envelopes=cited_envelopes,
                verified_witnesses=verified_witnesses,
            )
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
            # Then the clock, at the append boundary: the observation is judged
            # against the instant it actually reads (as a preview judges it),
            # and only the durable stamp is raised to the last record's, so the
            # journal is in recorded order. That one stamp is the journal
            # record's recorded_at and the projection's.
            record = self._at(
                claim_id=claim_id,
                capture_digest=capture_digest,
                envelope=envelope,
                now=self.instance.recording_clock(),
            )
            record = replace(record, observed_content_digest=cited_content)
            last = conn.execute("SELECT recorded_us FROM progress WHERE singleton=1").fetchone()
            if last is not None and _micros(record.recorded_at) < int(last[0]):
                record = replace(record, recorded_at=_from_micros(last[0]))
            now = record.recorded_at
            payload: dict[str, Any] = {
                "tag": _EVENT_TAG,
                "claim_id": record.claim_id,
                "capture_digest": record.capture_digest,
                "contract_digest": record.contract_digest,
                "observed_content_digest": record.observed_content_digest,
                "observed_at": format_datetime(record.observed_at),
                "claim_artifact_digest": version,
            }
            if extraction is not None:
                payload["extraction"] = extraction.model_dump(mode="json")
                payload["extracted_value"] = extracted_value
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

    def observe(
        self,
        contents: Sequence[ObservedContent],
        *,
        earliest: datetime,
        latest: datetime,
        run_id: str,
        node_id: str,
        actor_id: str,
        lineage: Mapping[str, str] | None = None,
    ) -> int | None:
        """Record one acquiring call's observation of every content it fetched (ruling 1a).

        ``lineage`` names the Procedure (identity and artifact digest) whose
        call observed it (D-R14): it is part of the run-free set body, so an
        unchanged Procedure reproduces the body and a changed one (a new
        extract spec over the same bytes) records its new lineage.

        One journal record per call, keyed by content: the set of (contract,
        content, Capture) is a run-free body, so a call that fetched the same
        contents again stores no new body and the projection writes one point.
        ``earliest`` and ``latest`` are the call's first and last member
        observation instants (ruling 1b). Nothing is judged per Claim: a Claim
        citing any Capture of one of these contents under its contract reads
        this observation as its freshness. A call observed after the clock's
        instant records nothing (the provider's instant cannot be trusted
        then). Returns the record's sequence, or None when nothing was recorded.
        """

        from cruxible_client.contracts.canonical import canonical_bytes

        if not contents or is_previewing():
            return None
        unique: dict[tuple[str, str], str] = {}
        for item in contents:
            pair = (item.contract_digest, item.observed_content_digest)
            unique[pair] = max(unique.get(pair, ""), item.capture_digest)
        body = canonical_bytes(
            {
                "tag": OBSERVED_CONTENTS_BODY,
                **({} if lineage is None else {"lineage": dict(lineage)}),
                "contents": [
                    [contract, content, capture]
                    for (contract, content), capture in sorted(unique.items())
                ],
            }
        )
        return self._append_shared(
            {
                "tag": CONTENT_OBSERVATION_TAG,
                "set_digest": self.instance.body_store().store(body, journal_payload=True).digest,
                "earliest": format_datetime(earliest.astimezone(UTC)),
                "latest": format_datetime(latest.astimezone(UTC)),
                "run_id": run_id,
                "node_id": node_id,
            },
            latest=latest,
            actor_id=actor_id,
        )

    def observe_rows(
        self,
        maps: Sequence[RowMap],
        *,
        earliest: datetime,
        latest: datetime,
        run_id: str,
        node_id: str,
        actor_id: str,
        lineage: Mapping[str, str] | None = None,
    ) -> int | None:
        """Record one extract step's observation of its whole row set (2026-11-16 a).

        ``maps`` holds each extracted Capture's records as rows. The row set
        body is run-free and Capture-free: the same rows in new bytes cite the
        set stored before, and a Claim produced from any of them reads this
        observation as its freshness, with no per-Claim write. Each Capture's
        row map is a run-free body too (stored new only for a new Capture), so
        an unchanged step writes one constant-size journal record. ``earliest``
        and ``latest`` are the acquiring call's member instants (ruling 1b).
        """

        from cruxible_client.contracts.canonical import canonical_bytes

        if not maps or is_previewing():
            return None
        bodies = self.instance.body_store()
        rows = sorted({(item.contract_digest, row) for item in maps for _locator, row in item.rows})
        if not rows:
            return None
        set_body = canonical_bytes(
            {
                "tag": OBSERVED_ROWS_BODY,
                **({} if lineage is None else {"lineage": dict(lineage)}),
                "rows": [list(row) for row in rows],
            }
        )
        map_digests = sorted(
            bodies.store(
                canonical_bytes(
                    {
                        "tag": ROW_MAP_BODY,
                        "capture": item.capture_digest,
                        "spec": item.spec_digest,
                        "contract": item.contract_digest,
                        "rows": [list(row) for row in item.rows],
                    }
                ),
                journal_payload=True,
            ).digest
            for item in maps
        )
        maps_digest = bodies.store(
            canonical_bytes({"tag": ROW_MAPS_BODY, "maps": map_digests}), journal_payload=True
        ).digest
        return self._append_shared(
            {
                "tag": ROW_OBSERVATION_TAG,
                "set_digest": bodies.store(set_body, journal_payload=True).digest,
                "maps_digest": maps_digest,
                "earliest": format_datetime(earliest.astimezone(UTC)),
                "latest": format_datetime(latest.astimezone(UTC)),
                "run_id": run_id,
                "node_id": node_id,
            },
            latest=latest,
            actor_id=actor_id,
        )

    def observe_enumeration(
        self,
        *,
        contract_digest: str,
        enumeration: str,
        capture_digest: str,
        earliest: datetime,
        latest: datetime,
        run_id: str,
        node_id: str,
        actor_id: str,
        lineage: Mapping[str, str] | None = None,
    ) -> int | None:
        """Record a complete absence enumeration's observation (F-001).

        ``enumeration`` is ``enumeration_digest`` of the run's complete witness:
        an absence Claim citing an enumeration with the same keys is fresh as
        of it. One shared record per complete enumeration, never per Claim;
        ``capture_digest`` is the witness's root Capture.
        """

        from cruxible_client.contracts.canonical import canonical_bytes

        if is_previewing():
            return None
        body = canonical_bytes(
            {
                "tag": OBSERVED_ROWS_BODY,
                **({} if lineage is None else {"lineage": dict(lineage)}),
                "rows": [[contract_digest, enumeration]],
            }
        )
        return self._append_shared(
            {
                "tag": ROW_OBSERVATION_TAG,
                "set_digest": self.instance.body_store().store(body, journal_payload=True).digest,
                "capture": capture_digest,
                "earliest": format_datetime(earliest.astimezone(UTC)),
                "latest": format_datetime(latest.astimezone(UTC)),
                "run_id": run_id,
                "node_id": node_id,
            },
            latest=latest,
            actor_id=actor_id,
        )

    def _append_shared(
        self, payload: dict[str, Any], *, latest: datetime, actor_id: str
    ) -> int | None:
        """Append one content or row observation at the store's own clock."""

        with self._locked(create=True) as conn:
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
            now = self.instance.recording_clock().astimezone(UTC)
            if latest.astimezone(UTC) > now:
                return None
            last = conn.execute("SELECT recorded_us FROM progress WHERE singleton=1").fetchone()
            if last is not None and _micros(now) < int(last[0]):
                now = _from_micros(last[0])
            stored = ProcedureExhaustWriter(
                journal=journal, bodies=bodies, fencing_token=_FENCE
            ).append(
                stream=self.stream,
                partition_id=_PARTITION,
                event_kind="claim_reobservation",
                accepted_coordinate=AcceptedCoordinate.from_internal(
                    self.instance.accepted_coordinate()
                ),
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
            self._apply_shared(
                conn,
                payload,
                stored.record.sequence,
                stored.record.recorded_at,
                generation=self._generation(stored.record.accepted_coordinate.git_oid, {}),
            )
            conn.commit()
            return stored.record.sequence

    # -- reads --------------------------------------------------------------

    def head_sequence(self) -> int:
        """The journal's newest sequence (0 before any record): where a new cursor starts."""

        journal = self._journal(create=False)
        return 0 if journal is None else journal.read_head(self.stream, _PARTITION).sequence

    def records_after(
        self, sequence: int, *, through: int | None = None
    ) -> tuple[tuple[int, str], ...]:
        """Each journal record after ``sequence`` (through ``through``): its sequence and
        the lineage it re-observed.

        Read from the journal, the authority, never the projection (which has no
        index on sequence alone): a follower pays one payload read per record past
        its cursor, whatever the journal holds.
        """

        journal = self._journal(create=False)
        if journal is None:
            return ()
        records: list[tuple[int, str]] = []
        for stored in journal.select_records(
            self.stream, partition_id=_PARTITION, first_sequence=sequence + 1
        ):
            if through is not None and stored.record.sequence > through:
                break
            payload = parse_journal_payload(
                self.instance.body_store().read(stored.record.payload_digest, access=_ACCESS)
            )
            if isinstance(payload, dict) and payload.get("tag") in _SHARED_TAGS:
                continue  # keyed by content, not by a lineage (``observed_after``)
            if not isinstance(payload, dict) or payload.get("tag") != _EVENT_TAG:
                raise ValueError("invalid Claim re-observation record")
            records.append((stored.record.sequence, str(payload["claim_id"])))
        return tuple(records)

    def observed_after(
        self, sequence: int, *, through: int | None = None
    ) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
        """What the records after ``sequence`` (through ``through``) re-observed.

        The lineages a per-Claim record names, and the (contract, content)
        keys a content or row observation covers: a follower reaches
        the Claims whose freshness read a key through what it recorded for it,
        never by visiting every Claim citing the content. One payload read per
        record, and one body read per record keyed by content.
        """

        journal = self._journal(create=False)
        if journal is None:
            return frozenset(), frozenset()
        claims: set[str] = set()
        keys: set[tuple[str, str]] = set()
        for stored in journal.select_records(
            self.stream, partition_id=_PARTITION, first_sequence=sequence + 1
        ):
            if through is not None and stored.record.sequence > through:
                break
            payload = parse_journal_payload(
                self.instance.body_store().read(stored.record.payload_digest, access=_ACCESS)
            )
            if not isinstance(payload, dict):
                raise ValueError("invalid Claim re-observation record")
            tag = payload.get("tag")
            if tag == CONTENT_OBSERVATION_TAG:
                body = _shared_body(
                    self.instance, str(payload["set_digest"]), tag=OBSERVED_CONTENTS_BODY
                )
                keys.update((str(item[0]), str(item[1])) for item in body["contents"])
            elif tag == ROW_OBSERVATION_TAG:
                body = _shared_body(
                    self.instance, str(payload["set_digest"]), tag=OBSERVED_ROWS_BODY
                )
                keys.update((str(item[0]), str(item[1])) for item in body["rows"])
            elif tag == _EVENT_TAG:
                claims.add(str(payload["claim_id"]))
            else:
                raise ValueError("invalid Claim re-observation record")
        return frozenset(claims), frozenset(keys)

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
    lag it. It names the log file's full identity -- device, inode, size and
    modification and change times, as the journal index does -- so a writer
    that truncates a crash tail and appends a frame of the same size still
    changes it. ``None`` when nothing was ever recorded.
    """

    found = reobservation_journal_identity(root, exhaust=exhaust, instance_id=instance_id)
    return None if found is None else found[1]


def reobservation_journal_identity(
    root: Path, *, exhaust: str, instance_id: str
) -> tuple[str, str] | None:
    """The re-observation log's file (device and inode) and its full signature, from one stat.

    The file names the journal a cursor was kept against: a journal rebuilt
    from nothing is a different file. The signature moves with every append.
    ``None`` when nothing was ever recorded.
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
    return (
        f"{stat.st_dev}:{stat.st_ino}",
        f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}",
    )


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
    #: The cited keys the read looked up: what ``sequence`` is taken over.
    keys: tuple["ReobservationKey", ...] = ()


@dataclass(frozen=True)
class _Point:
    """One observation a key's freshness can read: its instants and its record."""

    observed_us: int
    latest_us: int
    recorded_us: int
    sequence: int
    capture_digest: str

    def rank(self) -> tuple[int, str]:
        return (self.observed_us, self.capture_digest)


class ReobservationReader:
    """Indexed freshness reads over one instance's projection, on one connection.

    The connection is kept for the process. Before each read the projection
    file's identity and the journal's log signature are compared with the ones
    last caught up to: a projection deleted or replaced closes the connection,
    rebuilds or catches the file up from the journal and reconnects; a journal
    change alone catches the same file up. A forked child starts with fresh
    locks and no inherited connection: cached readers through
    ``os.register_at_fork`` (as the Git and CAS caches do), and every reader
    through a PID guard checked before its mutex. Counters say how many probes each read
    made, for counted-work tests.
    """

    _cache: dict[Path, "ReobservationReader"] = {}
    _cache_lock = threading.Lock()

    def __init__(self, instance: PlaybillInstance) -> None:
        self.store = ClaimReobservationStore(instance)
        self.instance = instance
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._signature: str | None = None
        self._projection: tuple[int, int] | None = None
        #: Whether any content observation was projected (None: not known yet).
        self._shared: bool | None = None
        self._pid = os.getpid()
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

    def _after_fork(self) -> None:
        """Forget what the parent held: its lock state and its connection."""

        self._lock = threading.Lock()
        self._conn = None  # the parent's; never closed from the child
        self._signature = None
        self._projection = None
        self._shared = None
        self._pid = os.getpid()

    def _guard_fork(self) -> None:
        """In a forked child, reset before touching the inherited mutex.

        Checked ahead of every public read, so a reader the fork hook did not
        reach (constructed directly, or displaced from the cache) never waits
        on a lock its parent held at fork time.
        """

        if self._pid != os.getpid():
            self._after_fork()

    def _projection_identity(self) -> tuple[int, int] | None:
        try:
            stat = self.store.path.stat()
        except FileNotFoundError:
            return None
        return stat.st_dev, stat.st_ino

    def _connection(self) -> sqlite3.Connection | None:
        signature = reobservation_journal_signature(
            self.instance.root,
            exhaust=self.instance.descriptor.storage.exhaust,
            instance_id=self.instance.descriptor.instance_id,
        )
        if signature is None:
            return None
        identity = self._projection_identity()
        if self._conn is None or identity is None or identity != self._projection:
            # Deleted, replaced or never opened: rebuild or catch the current
            # file up from the journal, then read that file.
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            with self.store._locked():
                pass
            self._conn = sqlite3.connect(self.store.path, check_same_thread=False)
            self._projection = self._projection_identity()
            self._signature = signature
            self._shared = None
        elif signature != self._signature:
            with self.store._locked():
                pass  # catch the projection up from the journal
            self._signature = signature
        return self._conn

    def sequence(self, claim_id: str, keys: Sequence[ReobservationKey] = ()) -> int:
        """The last journal sequence that observed this lineage: 0 when none did.

        The lineage's own records, and the content and row observations of the
        keys it depends on (``keys``): what names the whole record set a
        remembered verdict read.
        """

        self._guard_fork()
        with self._lock:
            conn = self._connection()
            if conn is None:
                return 0
            through = _through()
            self.probes += 1
            row = conn.execute(_LAST_SEQUENCE, (claim_id, through)).fetchone()
            found = 0 if row is None or row[0] is None else int(row[0])
            if keys and self._content_observed(conn):
                for pair in {(key.contract_digest, key.observed_content_digest) for key in keys}:
                    found = max(found, self._key_sequence(conn, pair, through))
        return found

    def latest_observation(
        self, contract_digest: str, observed_content_digest: str
    ) -> datetime | None:
        """When the content was last observed: the latest instant of its latest call.

        What a Capture's minimum retention counts from once a re-fetch reuses
        it (ruling 1c): the envelope's instant is when its contents were first
        observed. Per observing source, its key head and its set's maxima
        through the read position: two seeks each. None when never observed.
        """

        self._guard_fork()
        with self._lock:
            conn = self._connection()
            if conn is None or not self._content_observed(conn):
                return None
            through = _through()
            best = -1
            pair = (contract_digest, observed_content_digest)
            for source in self._sources(conn, pair):
                head = self._head(conn, pair, source, through)
                if head is None:
                    continue
                best = max(best, int(head[10]))
                peak = self._peak(conn, str(head[0]), through)
                if peak is not None:
                    best = max(best, int(peak[3]))
        return None if best < 0 else _from_micros(best)

    def observed(self, claim_id: str, keys: Sequence[ReobservationKey]) -> bool:
        """Whether the lineage, or any key it depends on, was ever observed here.

        One seek for the lineage and one per key: what decides whether a
        Claim's freshness can read anything at all.
        """

        self._guard_fork()
        with self._lock:
            conn = self._connection()
            if conn is None:
                return False
            self.probes += 1
            row = conn.execute(_LAST_SEQUENCE, (claim_id, _NEVER)).fetchone()
            if row is not None and row[0] is not None:
                return True
            for key in keys:
                self.probes += 1
                if conn.execute(
                    "SELECT 1 FROM key_sources WHERE contract_digest=? "
                    "AND observed_content_digest=? LIMIT 1",
                    (key.contract_digest, key.observed_content_digest),
                ).fetchone():
                    return True
        return False

    def row_of(self, capture_digest: str, spec_digest: str, locator: str) -> tuple[str, str] | None:
        """The (contract, row) one extracted record of a Capture is: one seek, or None
        when no row observation mapped that Capture's extraction."""

        self._guard_fork()
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            self.probes += 1
            row = conn.execute(
                "SELECT contract_digest, row_digest FROM capture_rows WHERE capture_digest=? "
                "AND spec_digest=? AND locator=?",
                (capture_digest, spec_digest, locator),
            ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def content_observed(self) -> bool:
        """Whether any content or row observation was ever recorded (one probe)."""

        self._guard_fork()
        with self._lock:
            conn = self._connection()
            return conn is not None and self._content_observed(conn)

    def _content_observed(self, conn: sqlite3.Connection) -> bool:
        """Whether the projection holds any content or row observation.

        Kept once true until the projection is caught up again or replaced:
        such rows are never removed, so the answer only ever turns true.
        """

        if self._shared is None:
            row = conn.execute("SELECT EXISTS(SELECT 1 FROM observation_sets)").fetchone()
            self._shared = bool(row[0]) or None
        return bool(self._shared)

    # -- maintained heads ---------------------------------------------------

    def _position(self, conn: sqlite3.Connection, at_us: int) -> int:
        """The last observation recorded by ``at_us``: one seek (recorded order is
        journal order)."""

        if at_us >= _NEVER:
            return _NEVER
        self.probes += 1
        row = conn.execute(
            "SELECT sequence FROM instants WHERE recorded_us<=? "
            "ORDER BY recorded_us DESC, sequence DESC LIMIT 1",
            (at_us,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def _window(
        self,
        conn: sqlite3.Connection,
        *,
        at_us: int,
        through: int,
        deadline_us: int = _NEVER,
        lo: int = 0,
        hi: int = _NEVER,
    ) -> "_Window":
        """The journal positions a read admits, as sequence bounds.

        Everything recorded by the deadline ended by it (a record is never made
        before its call ends): ``through``. With a deadline, ``upper`` bounds
        the calls that ended by it but were recorded after it, found by their
        recording delay class (``_late_points``), never by a range over time.
        """

        upper = min(through, hi, self._position(conn, at_us))
        if deadline_us >= _NEVER:
            return _Window(through=upper, lo=lo, upper=upper)
        return _Window(
            through=min(upper, self._position(conn, deadline_us)),
            lo=lo,
            deadline_us=deadline_us,
            upper=upper,
        )

    def _sources(self, conn: sqlite3.Connection, pair: tuple[str, str]) -> list[str]:
        """The sources (Procedure and step) whose calls ever observed the key."""

        self.probes += 1
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT source FROM key_sources WHERE contract_digest=? "
                "AND observed_content_digest=?",
                pair,
            )
        ]

    def _head(
        self, conn: sqlite3.Connection, pair: tuple[str, str], source: str, through: int
    ) -> tuple[Any, ...] | None:
        """The source's key head through ``through``: its set and carried best (one seek)."""

        self.probes += 1
        row = conn.execute(
            "SELECT set_digest, carried_us, carried_recorded_us, carried_sequence, "
            "carried_capture, sequence, recorded_us, 0, 0, 0, carried_latest_us FROM key_heads "
            "WHERE contract_digest=? AND observed_content_digest=? AND source=? AND sequence<=? "
            "ORDER BY sequence DESC LIMIT 1",
            (*pair, source, through),
        ).fetchone()
        return None if row is None else tuple(row)

    def _peak(
        self, conn: sqlite3.Connection, set_digest: str, through: int
    ) -> tuple[int, ...] | None:
        """A set's maxima through ``through``: (earliest, its recording, its sequence, latest)."""

        self.probes += 1
        row = conn.execute(
            "SELECT earliest_us, earliest_recorded_us, earliest_sequence, latest_us FROM set_peaks "
            "WHERE set_digest=? AND sequence<=? ORDER BY sequence DESC LIMIT 1",
            (set_digest, through),
        ).fetchone()
        return None if row is None else tuple(int(item) for item in row)

    def _capture(self, conn: sqlite3.Connection, pair: tuple[str, str], set_digest: str) -> str:
        row = conn.execute(
            "SELECT capture_digest FROM observed_sets WHERE contract_digest=? "
            "AND observed_content_digest=? AND set_digest=?",
            (*pair, set_digest),
        ).fetchone()
        return "" if row is None else str(row[0])

    def _next(
        self, conn: sqlite3.Connection, sql: str, params: tuple[object, ...], into: set[datetime]
    ) -> None:
        row = conn.execute(sql, params).fetchone()
        if row is not None:
            into.add(_from_micros(row[0]))

    def _point_at(
        self, conn: sqlite3.Connection, pair: tuple[str, str], sequence: int
    ) -> _Point | None:
        """The observation recorded at ``sequence``, when its set holds the key (two seeks)."""

        self.probes += 1
        row = conn.execute(
            "SELECT earliest_us, latest_us, recorded_us, sequence, set_digest "
            "FROM observation_points WHERE sequence=?",
            (sequence,),
        ).fetchone()
        if row is None:
            return None
        capture = self._capture(conn, pair, str(row[4]))
        return None if not capture else _point_row(row[:4], capture)

    def _key_point(
        self,
        conn: sqlite3.Connection,
        pair: tuple[str, str],
        window: "_Window",
        boundaries: set[datetime] | None = None,
    ) -> _Point | None:
        """The observation of one key with the greatest earliest instant the window admits.

        Per observing source, the best of its carried observation and its
        current set's maximum through the window (one seek each): the maximum
        over everything recorded by then. When that maximum lies before a
        Claim version's interval (``lo``), the exact maximum inside it: any
        better observation was recorded after the latest in-interval call
        began (an observation is recorded after it is made), so only records
        made during that one call are visited (F-003). A deadline adds the
        calls that ended by it but were recorded after it. Ties prefer the
        larger Capture digest.
        """

        best: _Point | None = None
        last: _Point | None = None
        for source in self._sources(conn, pair):
            head = self._head(conn, pair, source, window.through)
            if boundaries is not None:
                self._next(
                    conn,
                    "SELECT recorded_us FROM key_heads WHERE contract_digest=? "
                    "AND observed_content_digest=? AND source=? AND sequence>? "
                    "ORDER BY sequence LIMIT 1",
                    (*pair, source, window.through),
                    boundaries,
                )
            if head is None:
                continue
            set_digest = str(head[0])
            capture = self._capture(conn, pair, set_digest)
            found = []
            peak = self._peak(conn, set_digest, window.through)
            if boundaries is not None:
                self._next(
                    conn,
                    "SELECT recorded_us FROM set_peaks WHERE set_digest=? AND sequence>? "
                    "ORDER BY sequence LIMIT 1",
                    (set_digest, window.through),
                    boundaries,
                )
            if peak is not None:
                found.append(_Point(peak[0], peak[3], peak[1], peak[2], capture))
            if int(head[3]) >= 0:
                found.append(
                    _Point(int(head[1]), int(head[10]), int(head[2]), int(head[3]), str(head[4]))
                )
            for point in found:
                if best is None or point.rank() > best.rank():
                    best = point
            if window.lo > 0:
                # The source's latest observation of the key by then: its
                # current set's last point (the head row is that set's first).
                self.probes += 1
                row = conn.execute(
                    "SELECT earliest_us, latest_us, recorded_us, sequence FROM observation_points "
                    "WHERE set_digest=? AND sequence<=? ORDER BY sequence DESC LIMIT 1",
                    (set_digest, window.through),
                ).fetchone()
                if row is not None and int(row[3]) >= window.lo:
                    point = _point_row(row, capture)
                    if last is None or point.sequence > last.sequence:
                        last = point
        if best is not None and best.sequence < window.lo:
            best = None if last is None else self._within(conn, pair, window, last)
        if window.deadline_us < _NEVER:
            late = self._late_point(conn, pair, window)
            if late is not None and (best is None or late.rank() > best.rank()):
                best = late
        return best

    def _within(
        self, conn: sqlite3.Connection, pair: tuple[str, str], window: "_Window", last: _Point
    ) -> _Point:
        """The best observation in ``[lo, through]`` given the latest one there.

        A better one was made after ``last``'s call began and recorded by
        ``last``'s record: only the records of that span are visited.
        """

        best = last
        self.probes += 1
        for (sequence,) in conn.execute(
            "SELECT sequence FROM instants WHERE recorded_us>? AND recorded_us<=? "
            "AND sequence>=? AND sequence<?",
            (last.observed_us, last.recorded_us, window.lo, last.sequence),
        ).fetchall():
            point = self._point_at(conn, pair, int(sequence))
            if point is not None and point.rank() > best.rank():
                best = point
        return best

    def _late_point(
        self, conn: sqlite3.Connection, pair: tuple[str, str], window: "_Window"
    ) -> _Point | None:
        """The best call of the key that ended by the deadline but was recorded after it.

        Each record keeps its recording delay's power-of-two class; a record of
        class ``b`` that straddles the deadline ended within ``2**(b+1)`` of
        it, so each class is one range over the records that ended that close
        to the deadline (sized by recording rate times that class's delay,
        never by history), checked for the key.
        """

        best: _Point | None = None
        deadline = window.deadline_us
        for (bucket,) in conn.execute("SELECT bucket FROM delay_buckets").fetchall():
            self.probes += 1
            for (sequence,) in conn.execute(
                "SELECT sequence FROM instants WHERE bucket=? AND latest_us<=? AND latest_us>? "
                "AND recorded_us>? AND sequence>? AND sequence<=? AND sequence>=?",
                (
                    int(bucket),
                    deadline,
                    deadline - (1 << (int(bucket) + 1)),
                    deadline,
                    window.through,
                    window.upper,
                    window.lo,
                ),
            ).fetchall():
                point = self._point_at(conn, pair, int(sequence))
                if point is not None and (best is None or point.rank() > best.rank()):
                    best = point
        return best

    def _key_sequence(self, conn: sqlite3.Connection, pair: tuple[str, str], through: int) -> int:
        """The last observation a read of one key depends on.

        Per source, its key head and its set's last point (one seek each).
        """

        found = 0
        for source in self._sources(conn, pair):
            head = self._head(conn, pair, source, through)
            if head is None:
                continue
            found = max(found, int(head[5]))
            self.probes += 1
            last = conn.execute(
                "SELECT sequence FROM observation_points WHERE set_digest=? AND sequence<=? "
                "ORDER BY sequence DESC LIMIT 1",
                (str(head[0]), through),
            ).fetchone()
            if last is not None:
                found = max(found, int(last[0]))
        return found

    def _version_window(
        self, conn: sqlite3.Connection, claim_id: str, version: str, live_at: str
    ) -> tuple[int, int] | None:
        """The journal positions made while one Claim version was live at the head.

        ``live_at`` is an accepted head the version was live at. The version's
        first and last generations are two seeks on the lineage path's history
        from there, and each bound one seek on ``instants`` by generation:
        never a walk over the lineage's versions (F-007). None when no
        observation was made while it was live.
        """

        with self.instance.accepted_history_reader() as history:
            bounds = history.member_version_bounds(claim_path(claim_id), version, live_at)
        if bounds is None:
            return None
        start, end = bounds
        self.probes += 1
        low = conn.execute(
            "SELECT sequence FROM instants WHERE generation>=? ORDER BY generation, sequence "
            "LIMIT 1",
            (start,),
        ).fetchone()
        if low is None:
            return None
        if end is None:
            return int(low[0]), _NEVER
        self.probes += 1
        high = conn.execute(
            "SELECT sequence FROM instants WHERE generation<? "
            "ORDER BY generation DESC, sequence DESC LIMIT 1",
            (end,),
        ).fetchone()
        if high is None or int(high[0]) < int(low[0]):
            return None
        return int(low[0]), int(high[0])

    def latest_version_record(
        self,
        claim_id: str,
        version: str,
        *,
        observed_by: datetime,
        live_at: str,
        keys: Sequence[ReobservationKey] = (),
    ) -> RecordedReobservation | None:
        """One Claim version's latest recorded observation observed by ``observed_by``.

        ``version`` is the Claim's artifact digest, live at the accepted head
        ``live_at``. The lineage's own records count only when admitted while
        that exact version was live: one seek on ``reobservations_by_version``.
        The content and row observations of the keys the version depends on
        count when recorded while it was live at the head (its live interval,
        as journal positions) and their call ended by ``observed_by`` (ruling
        1b); the instant reported is the call's earliest. The greatest eligible
        one is selected within those bounds (F-002, F-003). The latest of
        these wins; its journal record is read by its sequence.
        """

        observed_us = _micros(observed_by)
        self._guard_fork()
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            through = _through()
            self.probes += 1
            row = conn.execute(
                _VERSION_RECORD, (claim_id, version, observed_us, through)
            ).fetchone()
            content: list[tuple[_Point, tuple[str, str]]] = []
            live = (
                self._version_window(conn, claim_id, version, live_at)
                if keys and self._content_observed(conn)
                else None
            )
            if live is not None:
                window = self._window(
                    conn,
                    at_us=_NEVER,
                    through=through,
                    deadline_us=observed_us,
                    lo=live[0],
                    hi=live[1],
                )
                for pair in sorted(
                    {(key.contract_digest, key.observed_content_digest) for key in keys}
                ):
                    point = self._key_point(conn, pair, window)
                    if point is not None:
                        content.append((point, pair))
        candidates: list[tuple[tuple[int, str], ReobservationRecord, int]] = []
        if row is not None:
            record = ReobservationRecord(
                claim_id=claim_id,
                capture_digest=str(row[0]),
                contract_digest=str(row[1]),
                observed_content_digest=str(row[2]),
                observed_at=_instant(row[3]),
                recorded_at=_instant(row[4]),
            )
            candidates.append(
                ((_micros(record.observed_at), record.capture_digest), record, int(row[5]))
            )
        for point, (contract, observed_content_digest) in content:
            candidates.append(
                (
                    point.rank(),
                    ReobservationRecord(
                        claim_id=claim_id,
                        capture_digest=point.capture_digest,
                        contract_digest=contract,
                        observed_content_digest=observed_content_digest,
                        observed_at=_from_micros(point.observed_us),
                        recorded_at=_from_micros(point.recorded_us),
                    ),
                    point.sequence,
                )
            )
        if not candidates:
            return None
        _rank, record, sequence = max(candidates, key=lambda item: item[0])
        journal = self.store._journal(create=False)
        self.rows_read += 1
        stored = (
            ()
            if journal is None
            else journal.select_records(
                self.store.stream,
                partition_id=_PARTITION,
                first_sequence=sequence,
                last_sequence=sequence,
            )
        )
        if (
            len(stored) != 1
            or stored[0].record.event_kind != "claim_reobservation"
            or stored[0].record.recorded_at != record.recorded_at
        ):
            raise ClaimReobservationJournalCorrupt(
                f"re-observation record {sequence} of Claim {claim_id} is not in the journal "
                "its projection was caught up from"
            )
        return RecordedReobservation(
            record=record,
            sequence=sequence,
            record_digest=stored[0].record_digest,
            accepted_coordinate=stored[0].record.accepted_coordinate,
        )

    def latest(
        self, claim_id: str, keys: Sequence[ReobservationKey], at: datetime
    ) -> ReobservationReading:
        """The latest observation per depended-on key applicable at ``at``.

        Applicable means recorded by ``at`` (and so observed by it: admission
        refuses a Capture observed after its record). Two sources count, and
        the latest observation wins, ties broken by the larger Capture digest,
        exactly as ``claim_verdicts`` selects: the lineage's own records (the
        last change point of that key recorded by ``at``) and the content or
        row observations of the key (the call's earliest instant). Work is a
        few seeks per distinct key and observing source, however many records
        the lineage holds, runs observed it, or sets held it.
        """

        at_us = _micros(at)
        evidence: list[CaptureReobservationEvidence] = []
        boundaries: set[datetime] = set()
        self._guard_fork()
        with self._lock:
            conn = self._connection()
            if conn is None:
                return ReobservationReading()
            self.probes += 1
            through = _through()
            row = conn.execute(_LAST_SEQUENCE, (claim_id, through)).fetchone()
            sequence = 0 if row is None or row[0] is None else int(row[0])
            shared = self._content_observed(conn)
            if not sequence and not shared:
                return ReobservationReading()
            window = self._window(conn, at_us=at_us, through=through) if shared else None
            found: dict[tuple[str, str], _Point | None] = {}
            for key in keys:
                pair = (key.contract_digest, key.observed_content_digest)
                if pair not in found:
                    best: _Point | None = None
                    if sequence:
                        self.probes += 2
                        hit = conn.execute(
                            _LAST_POINT, (claim_id, *pair, at_us, through)
                        ).fetchone()
                        upcoming = conn.execute(
                            _NEXT_POINT, (claim_id, *pair, at_us, through)
                        ).fetchone()
                        if upcoming is not None:
                            boundaries.add(_from_micros(upcoming[0]))
                        if hit is not None:
                            best = _Point(
                                observed_us=int(hit[1]),
                                latest_us=int(hit[1]),
                                recorded_us=int(hit[2]),
                                sequence=0,
                                capture_digest=str(hit[0]),
                            )
                    if window is not None:
                        point = self._key_point(conn, pair, window, boundaries)
                        if point is not None and (best is None or point.rank() > best.rank()):
                            best = point
                        sequence = max(sequence, self._key_sequence(conn, pair, through))
                    self.rows_read += int(best is not None)
                    found[pair] = best
                selected = found[pair]
                if selected is None:
                    continue
                observed = _from_micros(selected.observed_us)
                recorded = _from_micros(selected.recorded_us)
                boundaries.update((observed, recorded))
                evidence.append(
                    CaptureReobservationEvidence(
                        cited_capture_digest=key.cited_capture_digest,
                        reobserving_capture_digest=selected.capture_digest,
                        observed_at=observed,
                        recorded_at=recorded,
                    )
                )
        return ReobservationReading(
            evidence=tuple(
                sorted(
                    evidence,
                    key=lambda item: (
                        item.cited_capture_digest.encode("ascii"),
                        item.observed_at,
                        item.reobserving_capture_digest.encode("ascii"),
                    ),
                )
            ),
            boundaries=tuple(sorted(boundaries)),
            sequence=sequence,
        )


def _point_row(row: Sequence[object], capture: str) -> _Point:
    """A point from an (earliest, latest, recorded, sequence) row."""

    earliest, latest, recorded, sequence = (int(cast(int, item)) for item in row)
    return _Point(earliest, latest, recorded, sequence, capture)


@dataclass(frozen=True)
class _Window:
    """The journal positions one read admits.

    ``through`` is the last position whose records all count (by the read's
    instant, read position, version interval end and deadline); ``lo`` the
    first a Claim version's interval admits; ``upper`` the last any record
    counts at (a deadline's late-recorded calls lie between them).
    """

    through: int
    lo: int = 0
    deadline_us: int = _NEVER
    upper: int = 0


def _after_fork_in_child() -> None:
    """A forked child holds none of the parent's locks or connections."""

    global _LOCK
    _LOCK = threading.RLock()
    ReobservationReader._cache_lock = threading.Lock()
    for reader in ReobservationReader._cache.values():
        reader._after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def cited_reobservation_keys(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    captures: Sequence[CaptureVerdictEvidence],
) -> tuple[ReobservationKey, ...]:
    """Each cited Capture with the contract and content it observed, from the evidence index."""

    return cited_content_keys(
        instance, coordinate=coordinate, capture_digests=[item.capture_digest for item in captures]
    )


def cited_content_keys(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    capture_digests: Sequence[str],
) -> tuple[ReobservationKey, ...]:
    """Each Capture digest with the (contract, content) it observed: one lookup each."""

    with instance.bind_accepted_projection(coordinate) as projection:
        return content_keys_on(projection.typed.connection, capture_digests)


def content_keys_on(
    connection: sqlite3.Connection, capture_digests: Sequence[str]
) -> tuple[ReobservationKey, ...]:
    """``cited_content_keys`` on an accepted projection connection already bound."""

    keys: list[ReobservationKey] = []
    for digest in capture_digests:
        row = connection.execute(
            "SELECT contract_digest, observed_content_digest FROM captures WHERE capture_digest=?",
            (digest,),
        ).fetchone()
        if row is not None:
            keys.append(ReobservationKey(digest, str(row[0]), str(row[1])))
    return tuple(keys)


def claim_keys_on(
    connection: sqlite3.Connection,
    reader: "ReobservationReader",
    claim: Any,
    capture_digests: Sequence[str],
) -> tuple[ReobservationKey, ...]:
    """The keys one Claim's freshness depends on (ruling 2026-11-16).

    An absence depends on its complete enumeration alone (F-001): the
    contents of its root page say nothing about the others. A Claim produced
    from an extracted record depends on that record's row (looked up by its
    cited Capture, extract specification and record locator) and on the
    exact cited content too; any other Claim on the contents it cites (1d).
    """

    content = content_keys_on(connection, capture_digests)
    evidence = getattr(getattr(claim, "backing", None), "procedure_evidence", None)
    witness = getattr(evidence, "absence", None)
    if isinstance(witness, AbsenceWitness):
        by_capture = {key.cited_capture_digest: key for key in content}
        root = by_capture.get(witness.capture)
        if root is None:
            return ()
        return (
            ReobservationKey(witness.capture, root.contract_digest, enumeration_digest(witness)),
        )
    value = getattr(evidence, "value", None)
    if isinstance(value, ExtractedBasis):
        row = reader.row_of(value.capture_digest, value.extract_spec_digest, value.record_locator)
        if row is not None:
            return (*content, ReobservationKey(value.capture_digest, *row))
    return content


def claim_reobservation_keys(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claim: Any,
    captures: Sequence[str],
) -> tuple[ReobservationKey, ...]:
    """``claim_keys_on`` on its own accepted projection bind."""

    with instance.bind_accepted_projection(coordinate) as projection:
        return claim_keys_on(
            projection.typed.connection, ReobservationReader.of(instance), claim, captures
        )


def reobservations_at(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claim: Any,
    captures: Sequence[CaptureVerdictEvidence],
    at: datetime,
) -> ReobservationReading:
    """The recorded re-observations that count for one Claim's verdict at ``at``.

    A lineage never observed costs one indexed probe and nothing else; one
    that was costs one lookup per cited Capture and a few seeks per key it
    depends on, however long its history.
    """

    reader = ReobservationReader.of(instance)
    claim_id = claim.identity.name
    if not reader.sequence(claim_id) and not reader.content_observed():
        return ReobservationReading()
    keys = claim_reobservation_keys(
        instance,
        coordinate=coordinate,
        claim=claim,
        captures=[item.capture_digest for item in captures],
    )
    return replace(reader.latest(claim_id, keys, at), keys=keys)


__all__ = [
    "CONTENT_OBSERVATION_TAG",
    "ROW_OBSERVATION_TAG",
    "RowMap",
    "ObservedContent",
    "REOBSERVATION_DIRECTORY",
    "ClaimReobservationJournalCorrupt",
    "ClaimReobservationRefused",
    "ClaimReobservationStore",
    "ReobservationKey",
    "ReobservationReader",
    "ReobservationReading",
    "ReobservationRecord",
    "RecordedReobservation",
    "cited_content_keys",
    "content_keys_on",
    "cited_reobservation_keys",
    "claim_keys_on",
    "claim_reobservation_keys",
    "enumeration_digest",
    "row_digest",
    "reobservation_journal_identity",
    "reobservation_journal_signature",
    "reobservations_at",
]
