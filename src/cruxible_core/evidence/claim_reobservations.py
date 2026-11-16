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
from typing import Any

from cruxible_client.contracts.captures import (
    CaptureEnvelopeAny,
    observed_content,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_evidence import AbsenceWitness, ClaimEvidenceReceipt
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
#: A claims terminal's re-check of cited content in new bytes (ruling 1d): the
#: Claims citing each ``from`` content under its contract were found unchanged
#: in the ``to`` Capture, except the Claims it names.
CONTENT_TRANSITION_TAG = "cruxible-content-transition-v1"
CONTENT_TRANSITIONS_BODY = "cruxible-content-transitions-v1"
_ACCESS = BodyAccessContext(principal_id="claim-reobservation", can_read_body=True)
_SCHEMA_VERSION = 6
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
CREATE TABLE IF NOT EXISTS observation_sets (
 set_digest TEXT PRIMARY KEY, last_recorded_us INTEGER NOT NULL, last_sequence INTEGER NOT NULL
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS observation_points (
 set_digest TEXT NOT NULL, recorded_us INTEGER NOT NULL, sequence INTEGER NOT NULL,
 earliest_us INTEGER NOT NULL, latest_us INTEGER NOT NULL,
 PRIMARY KEY(set_digest, recorded_us, sequence)
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS transitions (
 contract_digest TEXT NOT NULL, from_content_digest TEXT NOT NULL, recorded_us INTEGER NOT NULL,
 sequence INTEGER NOT NULL, earliest_us INTEGER NOT NULL, latest_us INTEGER NOT NULL,
 to_capture_digest TEXT NOT NULL, to_content_digest TEXT NOT NULL,
 PRIMARY KEY(contract_digest, from_content_digest, recorded_us, sequence, to_capture_digest)
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS transition_exceptions (
 sequence INTEGER NOT NULL, claim_id TEXT NOT NULL, PRIMARY KEY(sequence, claim_id)
) STRICT, WITHOUT ROWID;
"""
# Content observations (ruling 1a). ``observed_sets`` holds one row per member of
# each distinct observed set, written once when the set first appears; a call
# whose members are unchanged writes one ``observation_points`` row and moves its
# set's ``observation_sets`` head, never a row per content. A key's latest
# observation at an instant is one seek per set that ever held it, newest first,
# stopping once no older set can beat what was found (an observation is never
# recorded before it is made). Each point keeps the call's earliest and latest
# member instants (ruling 1b): freshness reads the earliest, a deadline the latest.
_SETS_OF_KEY = (
    "SELECT o.set_digest, o.capture_digest, s.last_recorded_us FROM observed_sets o "
    "JOIN observation_sets s ON s.set_digest=o.set_digest "
    "WHERE o.contract_digest=? AND o.observed_content_digest=? "
    "ORDER BY s.last_recorded_us DESC, o.set_digest DESC"
)
_SET_POINT = (
    "SELECT earliest_us, latest_us, recorded_us, sequence FROM observation_points "
    "WHERE set_digest=? AND recorded_us<=? AND sequence<=? AND latest_us<=? "
    "ORDER BY recorded_us DESC, sequence DESC LIMIT 1"
)
_SET_NEXT = (
    "SELECT recorded_us FROM observation_points WHERE set_digest=? AND recorded_us>? "
    "AND sequence<=? ORDER BY recorded_us LIMIT 1"
)
_LAST_TRANSITION = (
    "SELECT earliest_us, latest_us, recorded_us, sequence, to_capture_digest, to_content_digest "
    "FROM transitions t WHERE contract_digest=? AND from_content_digest=? AND recorded_us<=? "
    "AND sequence<=? AND latest_us<=? AND NOT EXISTS (SELECT 1 FROM transition_exceptions e "
    "WHERE e.sequence=t.sequence AND e.claim_id=?) "
    "ORDER BY recorded_us DESC, sequence DESC, to_capture_digest DESC LIMIT 1"
)
_NEXT_TRANSITION = (
    "SELECT recorded_us FROM transitions WHERE contract_digest=? AND from_content_digest=? "
    "AND recorded_us>? AND sequence<=? ORDER BY recorded_us LIMIT 1"
)
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


_SHARED_TAGS = frozenset({CONTENT_OBSERVATION_TAG, CONTENT_TRANSITION_TAG})


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
    """One content observation's or transition's canonical body."""

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
class ContentTransition:
    """Claims citing ``from_content_digest`` under the contract, found unchanged in ``to``."""

    contract_digest: str
    from_content_digest: str
    to_capture_digest: str
    to_content_digest: str


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
                        "observation_sets",
                        "observation_points",
                        "transitions",
                        "transition_exceptions",
                    ):
                        conn.execute(f"DROP TABLE IF EXISTS {table}")
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
                    if isinstance(payload, dict) and payload.get("tag") in _SHARED_TAGS:
                        self._apply_shared(
                            conn, payload, stored.record.sequence, stored.record.recorded_at
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

    def _apply_shared(
        self,
        conn: sqlite3.Connection,
        payload: Mapping[str, Any],
        sequence: int,
        recorded_at: datetime,
    ) -> None:
        """Project one content observation or transition: rows for what is new only.

        A content observation writes its set's members once (the first time the
        set appears), then one point and its set's head. A transition writes one
        row per (contract, from content) it re-checked and one per excepted
        Claim: its size, never the Claims citing the content.
        """

        recorded_us = _ordered(conn, sequence, recorded_at)
        earliest_us = _micros(_instant(payload["earliest"]))
        latest_us = _micros(_instant(payload["latest"]))
        if payload["tag"] == CONTENT_OBSERVATION_TAG:
            set_digest = str(payload["set_digest"])
            known = conn.execute(
                "SELECT 1 FROM observation_sets WHERE set_digest=?", (set_digest,)
            ).fetchone()
            if known is None:
                body = _shared_body(self.instance, set_digest, tag=OBSERVED_CONTENTS_BODY)
                conn.executemany(
                    "INSERT OR REPLACE INTO observed_sets VALUES (?,?,?,?)",
                    [
                        (str(contract), str(content), set_digest, str(capture))
                        for contract, content, capture in body["contents"]
                    ],
                )
            conn.execute(
                "INSERT OR REPLACE INTO observation_sets VALUES (?,?,?)",
                (set_digest, recorded_us, sequence),
            )
            conn.execute(
                "INSERT OR IGNORE INTO observation_points VALUES (?,?,?,?,?)",
                (set_digest, recorded_us, sequence, earliest_us, latest_us),
            )
        else:
            body = _shared_body(
                self.instance, str(payload["transitions_digest"]), tag=CONTENT_TRANSITIONS_BODY
            )
            conn.executemany(
                "INSERT OR IGNORE INTO transitions VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        str(contract),
                        str(source),
                        recorded_us,
                        sequence,
                        earliest_us,
                        latest_us,
                        str(capture),
                        str(content),
                    )
                    for contract, source, capture, content in body["transitions"]
                ],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO transition_exceptions VALUES (?,?)",
                [(sequence, str(claim_id)) for claim_id in body["except"]],
            )
        conn.execute("INSERT OR REPLACE INTO progress VALUES(1,?,?)", (sequence, recorded_us))

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
    ) -> int | None:
        """Record one acquiring call's observation of every content it fetched (ruling 1a).

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

    def transition(
        self,
        transitions: Sequence[ContentTransition],
        *,
        excepted: Sequence[str],
        earliest: datetime,
        latest: datetime,
        run_id: str,
        node_id: str,
        actor_id: str,
    ) -> int | None:
        """Record one claims terminal's re-check of cited content in new bytes (ruling 1d).

        The Claims citing each ``from`` content under its contract were found
        to state the same thing in the ``to`` Capture; ``excepted`` names the
        terminal's Claims citing such content that changed instead. One record
        per terminal, sized by the contents that moved and the Claims excepted,
        never by the Claims citing the content. ``earliest`` and ``latest`` are
        when the ``to`` Captures were observed.
        """

        from cruxible_client.contracts.canonical import canonical_bytes

        if not transitions or is_previewing():
            return None
        held = self._recorded_transition(transitions, excepted)
        if held is not None:
            # A recovery delivering again, or a later run re-checking the same
            # moved contents: the latest transition already says exactly this.
            return held
        body = canonical_bytes(
            {
                "tag": CONTENT_TRANSITIONS_BODY,
                "transitions": [
                    list(entry)
                    for entry in sorted(
                        {
                            (
                                item.contract_digest,
                                item.from_content_digest,
                                item.to_capture_digest,
                                item.to_content_digest,
                            )
                            for item in transitions
                        }
                    )
                ],
                "except": sorted(set(excepted)),
            }
        )
        return self._append_shared(
            {
                "tag": CONTENT_TRANSITION_TAG,
                "transitions_digest": self.instance.body_store()
                .store(body, journal_payload=True)
                .digest,
                "earliest": format_datetime(earliest.astimezone(UTC)),
                "latest": format_datetime(latest.astimezone(UTC)),
                "run_id": run_id,
                "node_id": node_id,
            },
            latest=latest,
            actor_id=actor_id,
        )

    def _recorded_transition(
        self, transitions: Sequence[ContentTransition], excepted: Sequence[str]
    ) -> int | None:
        """The latest transition's sequence when it already records exactly these moves.

        One seek per moved (contract, content) and one read of the excepted
        Claims of the record found: sized by the transition, never by history.
        """

        if not self.root.is_dir():
            return None
        found: set[int] = set()
        with self._locked() as conn:
            for item in transitions:
                row = conn.execute(
                    "SELECT sequence, to_capture_digest FROM transitions "
                    "WHERE contract_digest=? AND from_content_digest=? "
                    "ORDER BY recorded_us DESC, sequence DESC LIMIT 1",
                    (item.contract_digest, item.from_content_digest),
                ).fetchone()
                if row is None or str(row[1]) != item.to_capture_digest:
                    return None
                found.add(int(row[0]))
            if len(found) != 1:
                return None
            (sequence,) = found
            held = {
                str(row[0])
                for row in conn.execute(
                    "SELECT claim_id FROM transition_exceptions WHERE sequence=?", (sequence,)
                )
            }
        return sequence if held == set(excepted) else None

    def _append_shared(
        self, payload: dict[str, Any], *, latest: datetime, actor_id: str
    ) -> int | None:
        """Append one content observation or transition at the store's own clock."""

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
            self._apply_shared(conn, payload, stored.record.sequence, stored.record.recorded_at)
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
        keys a content observation or transition covers: a follower reaches
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
            elif tag == CONTENT_TRANSITION_TAG:
                body = _shared_body(
                    self.instance,
                    str(payload["transitions_digest"]),
                    tag=CONTENT_TRANSITIONS_BODY,
                )
                keys.update((str(item[0]), str(item[1])) for item in body["transitions"])
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

        The lineage's own records, and the content observations and transitions
        of the (contract, content) it cites (``keys``): what names the whole
        record set a remembered verdict read.
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
                    found = max(found, self._key_sequence(conn, claim_id, pair, through))
        return found

    def latest_observation(
        self, contract_digest: str, observed_content_digest: str
    ) -> datetime | None:
        """When the content was last observed: the latest instant of its latest call.

        What a Capture's minimum retention counts from once a re-fetch reuses
        it (ruling 1c): the envelope's instant is when its contents were first
        observed. Sets are visited newest first and the walk stops once no
        older set can hold a later observation. None when never observed here.
        """

        self._guard_fork()
        with self._lock:
            conn = self._connection()
            if conn is None or not self._content_observed(conn):
                return None
            best: int | None = None
            for set_digest, _capture, last in conn.execute(
                _SETS_OF_KEY, (contract_digest, observed_content_digest)
            ).fetchall():
                if best is not None and int(last) <= best:
                    break
                self.probes += 1
                row = conn.execute(
                    "SELECT MAX(latest_us) FROM observation_points WHERE set_digest=? "
                    "AND sequence<=?",
                    (set_digest, _through()),
                ).fetchone()
                if row is not None and row[0] is not None:
                    best = int(row[0]) if best is None else max(best, int(row[0]))
        return None if best is None else _from_micros(best)

    def content_observed(self) -> bool:
        """Whether any content observation or transition was ever recorded (one probe)."""

        self._guard_fork()
        with self._lock:
            conn = self._connection()
            return conn is not None and self._content_observed(conn)

    def _content_observed(self, conn: sqlite3.Connection) -> bool:
        """Whether the projection holds any content observation or transition.

        Kept once true until the projection is caught up again or replaced:
        such rows are never removed, so the answer only ever turns true.
        """

        if self._shared is None:
            row = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM observation_sets) OR EXISTS(SELECT 1 FROM transitions)"
            ).fetchone()
            self._shared = bool(row[0]) or None
        return bool(self._shared)

    def _key_sequence(
        self, conn: sqlite3.Connection, claim_id: str, pair: tuple[str, str], through: int
    ) -> int:
        """The last content observation or transition of one key through ``through``."""

        found = 0
        for set_digest, _capture, _last in conn.execute(_SETS_OF_KEY, pair).fetchall():
            self.probes += 1
            row = conn.execute(
                "SELECT MAX(sequence) FROM observation_points WHERE set_digest=? AND sequence<=?",
                (set_digest, through),
            ).fetchone()
            if row is not None and row[0] is not None:
                found = max(found, int(row[0]))
        self.probes += 1
        row = conn.execute(
            "SELECT MAX(sequence) FROM transitions WHERE contract_digest=? "
            "AND from_content_digest=? AND sequence<=?",
            (*pair, through),
        ).fetchone()
        if row is not None and row[0] is not None:
            found = max(found, int(row[0]))
        return found

    def _content_point(
        self,
        conn: sqlite3.Connection,
        pair: tuple[str, str],
        *,
        at_us: int,
        through: int,
        deadline_us: int,
        after_us: int = -1,
        boundaries: set[datetime] | None = None,
    ) -> _Point | None:
        """The latest observation of one key recorded by ``at_us``, newest set first.

        Sets are visited by when they were last observed; once a set's last
        observation is no later than the best observation instant found, no
        point of it can be later, so the walk stops. ``after_us`` keeps only
        points recorded at or after it (a transition's chain); ``deadline_us``
        only points whose call ended by it (ruling 1b).
        """

        best: _Point | None = None
        for set_digest, capture, last in conn.execute(_SETS_OF_KEY, pair).fetchall():
            if best is not None and int(last) <= best.observed_us:
                break
            self.probes += 1
            row = conn.execute(_SET_POINT, (set_digest, at_us, through, deadline_us)).fetchone()
            if boundaries is not None:
                upcoming = conn.execute(_SET_NEXT, (set_digest, at_us, through)).fetchone()
                if upcoming is not None:
                    boundaries.add(_from_micros(upcoming[0]))
            if row is None or int(row[2]) < after_us:
                continue
            point = _Point(
                observed_us=int(row[0]),
                latest_us=int(row[1]),
                recorded_us=int(row[2]),
                sequence=int(row[3]),
                capture_digest=str(capture),
            )
            if best is None or point.rank() > best.rank():
                best = point
        return best

    def _transition_point(
        self,
        conn: sqlite3.Connection,
        claim_id: str,
        pair: tuple[str, str],
        *,
        at_us: int,
        through: int,
        deadline_us: int,
        boundaries: set[datetime] | None = None,
    ) -> _Point | None:
        """The latest re-check of one cited key in new bytes that applies to this lineage.

        The latest transition from the key that does not except the lineage,
        then the latest observation of the content it moved to, recorded since
        (ruling 1d): later unchanged fetches of the new bytes keep re-checking
        the Claims that still cite the old content, with no record per run.
        """

        self.probes += 1
        row = conn.execute(
            _LAST_TRANSITION, (*pair, at_us, through, deadline_us, claim_id)
        ).fetchone()
        if boundaries is not None:
            upcoming = conn.execute(_NEXT_TRANSITION, (*pair, at_us, through)).fetchone()
            if upcoming is not None:
                boundaries.add(_from_micros(upcoming[0]))
        if row is None:
            return None
        moved = _Point(
            observed_us=int(row[0]),
            latest_us=int(row[1]),
            recorded_us=int(row[2]),
            sequence=int(row[3]),
            capture_digest=str(row[4]),
        )
        later = self._content_point(
            conn,
            (pair[0], str(row[5])),
            at_us=at_us,
            through=through,
            deadline_us=deadline_us,
            after_us=moved.recorded_us,
            boundaries=boundaries,
        )
        return later if later is not None and later.rank() > moved.rank() else moved

    def latest_version_record(
        self,
        claim_id: str,
        version: str,
        *,
        observed_by: datetime,
        keys: Sequence[ReobservationKey] = (),
    ) -> RecordedReobservation | None:
        """One Claim version's latest recorded observation observed by ``observed_by``.

        ``version`` is the Claim's artifact digest. The lineage's own records
        count only when admitted while that exact version was live: one seek
        on ``reobservations_by_version``. The content observations and
        transitions of the content the version cites (``keys``) count when
        their call ended by ``observed_by`` (a call running across the deadline
        does not count, ruling 1b) and the version was live at the head the
        record was made at; the instant reported is the call's earliest. The
        latest of these wins; its journal record is read by its sequence.
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
            if keys and self._content_observed(conn):
                for pair in sorted(
                    {(key.contract_digest, key.observed_content_digest) for key in keys}
                ):
                    for point in (
                        self._content_point(
                            conn, pair, at_us=_NEVER, through=through, deadline_us=observed_us
                        ),
                        self._transition_point(
                            conn,
                            claim_id,
                            pair,
                            at_us=_NEVER,
                            through=through,
                            deadline_us=observed_us,
                        ),
                    ):
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
        journal = self.store._journal(create=False)
        for _rank, record, sequence in sorted(candidates, key=lambda item: item[0], reverse=True):
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
            coordinate = stored[0].record.accepted_coordinate
            if (
                row is not None
                and sequence == int(row[5])
                or self.store._version_at(claim_id, coordinate.git_oid) == version
            ):
                return RecordedReobservation(
                    record=record,
                    sequence=sequence,
                    record_digest=stored[0].record_digest,
                    accepted_coordinate=coordinate,
                )
        return None

    def latest(
        self, claim_id: str, keys: Sequence[ReobservationKey], at: datetime
    ) -> ReobservationReading:
        """The latest observation per cited key applicable at ``at``.

        Applicable means recorded by ``at`` (and so observed by it: admission
        refuses a Capture observed after its record). Three sources count, and
        the latest observation wins, ties broken by the larger Capture digest,
        exactly as ``claim_verdicts`` selects: the lineage's own records (the
        last change point of that key recorded by ``at``), the content
        observations of the key (ruling 1a, the call's earliest instant), and
        the latest transition from the key that does not except this lineage,
        with the later observations of the content it moved to (ruling 1d).
        Work is a few seeks per distinct key, however many records the lineage
        holds, runs observed the content, or were recorded late.
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
                    if shared:
                        for point in (
                            self._content_point(
                                conn,
                                pair,
                                at_us=at_us,
                                through=through,
                                deadline_us=_NEVER,
                                boundaries=boundaries,
                            ),
                            self._transition_point(
                                conn,
                                claim_id,
                                pair,
                                at_us=at_us,
                                through=through,
                                deadline_us=_NEVER,
                                boundaries=boundaries,
                            ),
                        ):
                            if point is not None and (best is None or point.rank() > best.rank()):
                                best = point
                        sequence = max(sequence, self._key_sequence(conn, claim_id, pair, through))
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
                sorted(evidence, key=lambda item: item.cited_capture_digest.encode("ascii"))
            ),
            boundaries=tuple(sorted(boundaries)),
            sequence=sequence,
        )


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

    keys: list[ReobservationKey] = []
    with instance.bind_accepted_projection(coordinate) as projection:
        for digest in capture_digests:
            row = projection.typed.connection.execute(
                "SELECT contract_digest, observed_content_digest FROM captures "
                "WHERE capture_digest=?",
                (digest,),
            ).fetchone()
            if row is not None:
                keys.append(ReobservationKey(digest, str(row[0]), str(row[1])))
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
    if not reader.sequence(claim_id) and not reader.content_observed():
        return ReobservationReading()
    keys = cited_reobservation_keys(instance, coordinate=coordinate, captures=captures)
    return replace(reader.latest(claim_id, keys, at), keys=keys)


__all__ = [
    "CONTENT_OBSERVATION_TAG",
    "CONTENT_TRANSITION_TAG",
    "ContentTransition",
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
    "cited_reobservation_keys",
    "reobservation_journal_identity",
    "reobservation_journal_signature",
    "reobservations_at",
]
