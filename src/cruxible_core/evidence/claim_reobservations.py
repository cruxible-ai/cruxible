"""Recorded re-observations: content a Claim already cites, observed again (identity-design-1009).

Restating a Claim on a Capture whose observed content it already cites, under
the same contract and in the same citation association, is a no-op: no
revision, no generation, no proposal. The newer Capture is not discarded: it is
recorded here, on the evidence plane, so the cited content's freshness can move
to when it was last observed (``claim_verdicts.CaptureReobservationEvidence``).

Storage follows the Line inbox's pattern: a journal (``claim-reobservation-v1``)
is the append-only authority, and a SQLite projection keyed by Claim lineage is
caught up from its last applied sequence on every open. A record is admitted only
for a daemon-admitted Capture that verifies at the head and whose observed
content equals a Capture the Claim currently cites under the same contract. The
freshness lookup reads one indexed range per Claim lineage, never a scan.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
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
from cruxible_core.exhaust.backends import LocalJournalBackend
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
_SCHEMA_VERSION = 1
#: The marker naming the journal's last appended sequence (``verdict_input_fingerprint``).
REOBSERVATION_GENERATION = "generation"
REOBSERVATION_DIRECTORY = "claim-reobservations"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (singleton INTEGER PRIMARY KEY, sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reobservations (
 claim_id TEXT NOT NULL, capture_digest TEXT NOT NULL, contract_digest TEXT NOT NULL,
 observed_content_digest TEXT NOT NULL, observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
 PRIMARY KEY(claim_id, capture_digest)
) STRICT;
CREATE INDEX IF NOT EXISTS reobservations_by_claim
 ON reobservations(claim_id, contract_digest, observed_content_digest);
"""


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
        conn.execute(
            "INSERT OR IGNORE INTO reobservations VALUES (?,?,?,?,?,?)",
            (
                payload["claim_id"],
                payload["capture_digest"],
                payload["contract_digest"],
                payload["observed_content_digest"],
                payload["observed_at"],
                format_datetime(recorded_at),
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

        It must be a daemon-admitted Capture that verifies at the head, and its
        observed content must equal a Capture the Claim currently cites under
        the same contract.
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

    def record(
        self,
        *,
        claim_id: str,
        capture_digest: str,
        coordinate: AcceptedProjectionCoordinate,
        actor_id: str,
        now: datetime,
    ) -> ReobservationRecord | None:
        """Record one admissible re-observation; a repeat is the same record.

        Nothing is written while previewing (``dry_run``): a preview observes
        nothing durable.
        """

        envelope = self._admissible(
            claim_id=claim_id, capture_digest=capture_digest, coordinate=coordinate
        )
        record = ReobservationRecord(
            claim_id=claim_id,
            capture_digest=capture_digest,
            contract_digest=envelope.capture_contract_digest,
            observed_content_digest=observed_content(envelope).digest,
            observed_at=envelope.observed_at.astimezone(UTC),
            recorded_at=now,
        )
        if is_previewing():
            return None
        payload = {
            "tag": _EVENT_TAG,
            "claim_id": record.claim_id,
            "capture_digest": record.capture_digest,
            "contract_digest": record.contract_digest,
            "observed_content_digest": record.observed_content_digest,
            "observed_at": format_datetime(record.observed_at),
        }
        with self._locked(create=True) as conn:
            known = conn.execute(
                "SELECT recorded_at FROM reobservations WHERE claim_id=? AND capture_digest=?",
                (claim_id, capture_digest),
            ).fetchone()
            if known is not None:
                return ReobservationRecord(
                    claim_id=record.claim_id,
                    capture_digest=record.capture_digest,
                    contract_digest=record.contract_digest,
                    observed_content_digest=record.observed_content_digest,
                    observed_at=record.observed_at,
                    recorded_at=_instant(known[0]),
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
            stored = ProcedureExhaustWriter(
                journal=journal, bodies=bodies, fencing_token=_FENCE
            ).append(
                stream=self.stream,
                partition_id=_PARTITION,
                event_kind="claim_reobservation",
                accepted_coordinate=AcceptedCoordinate.from_internal(coordinate),
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
            self._apply(conn, payload, stored.record.sequence, stored.record.recorded_at)
            conn.commit()
            # The verdict-input fingerprint reads this marker: a remembered
            # verdict derivation never outlives a newer re-observation.
            marker = self.root / REOBSERVATION_GENERATION
            staged = marker.with_suffix(".tmp")
            staged.write_text(str(stored.record.sequence), "ascii")
            staged.replace(marker)
        return record

    # -- reads --------------------------------------------------------------

    def _read(self, conn: sqlite3.Connection, claim_id: str) -> tuple[ReobservationRecord, ...]:
        self.probes += 1
        rows = conn.execute(
            "SELECT capture_digest, contract_digest, observed_content_digest, "
            "observed_at, recorded_at FROM reobservations WHERE claim_id=? "
            "ORDER BY capture_digest",
            (claim_id,),
        ).fetchall()
        self.rows_read += len(rows)
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

    def for_claims(self, claim_ids: Sequence[str]) -> dict[str, tuple[ReobservationRecord, ...]]:
        """Every re-observation recorded for each Claim lineage named.

        One connection, and one indexed range probe per lineage: what a lineage
        costs never depends on how many records other lineages hold.
        """

        if not claim_ids or not self.root.is_dir():
            return {}
        with self._locked() as conn:
            return {
                claim_id: found for claim_id in claim_ids if (found := self._read(conn, claim_id))
            }

    @classmethod
    @contextmanager
    def lookup(cls, instance: object) -> Iterator[Callable[[str], tuple[ReobservationRecord, ...]]]:
        """A per-lineage lookup on one connection, for a read that assembles many Claims.

        A source that is not a live instance (recovery replay) and an instance
        that never recorded one both answer nothing without opening anything.
        """

        if not isinstance(instance, PlaybillInstance):
            yield lambda _claim_id: ()
            return
        store = cls(instance)
        if not store.root.is_dir():
            yield lambda _claim_id: ()
            return
        with store._locked() as conn:
            yield lambda claim_id: store._read(conn, claim_id)

    def for_claim(self, claim_id: str) -> tuple[ReobservationRecord, ...]:
        """Every re-observation recorded for one Claim lineage: one indexed range."""

        return self.for_claims((claim_id,)).get(claim_id, ())


def reobservation_reading(
    records: Sequence[ReobservationRecord],
) -> tuple[tuple[str, str, str], ...]:
    """What a verdict read of one lineage's records returned, comparable later."""

    return tuple(
        (item.capture_digest, _stamp(item.observed_at), _stamp(item.recorded_at))
        for item in records
    )


def _stamp(value: datetime) -> str:
    stamped = format_datetime(value)
    assert stamped is not None
    return stamped


def claim_reobservation_evidence(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    captures: Sequence[CaptureVerdictEvidence],
    records: Sequence[ReobservationRecord],
) -> tuple[CaptureReobservationEvidence, ...]:
    """The recorded re-observations of each Capture this Claim cites, for its verdict.

    ``records`` are the Claim lineage's (``ClaimReobservationStore.for_claims``);
    each is matched to a cited Capture of the same contract and observed content
    through the accepted evidence index at ``coordinate``.
    """

    if not records:
        return ()
    cited: dict[tuple[str, str], list[str]] = {}
    with instance.bind_accepted_projection(coordinate) as projection:
        for item in captures:
            row = projection.typed.connection.execute(
                "SELECT contract_digest, observed_content_digest FROM captures "
                "WHERE capture_digest=?",
                (item.capture_digest,),
            ).fetchone()
            if row is not None:
                cited.setdefault((str(row[0]), str(row[1])), []).append(item.capture_digest)
    found: list[CaptureReobservationEvidence] = []
    for record in records:
        for digest in cited.get((record.contract_digest, record.observed_content_digest), ()):
            found.append(
                CaptureReobservationEvidence(
                    cited_capture_digest=digest,
                    reobserving_capture_digest=record.capture_digest,
                    observed_at=record.observed_at,
                    recorded_at=record.recorded_at,
                )
            )
    return tuple(
        sorted(
            found,
            key=lambda item: (
                item.cited_capture_digest.encode("ascii"),
                item.reobserving_capture_digest.encode("ascii"),
            ),
        )
    )


__all__ = [
    "REOBSERVATION_DIRECTORY",
    "reobservation_reading",
    "REOBSERVATION_GENERATION",
    "ClaimReobservationRefused",
    "ClaimReobservationStore",
    "ReobservationRecord",
    "claim_reobservation_evidence",
]
