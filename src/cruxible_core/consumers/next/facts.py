"""Layer 1 of incremental next: each Claim's facts, kept current from change records.

next's Claim rows are a function of each Claim's facts -- its verdict,
resolution status, visibility, lineage and citations -- and of the door. This
part keeps those facts in one disposable state and moves them with the change
records that already exist: an accepted generation's member paths, the door's
published events, the body store's landing log, the re-observation journal and
this part's own expiry deadline. ``fact_reads`` maps every read a Claim's facts
made to that Claim, so a record that touches a read recomputes only the Claims
behind it, and a resolution slot is resolved again only when a member's
verdict, membership or ClaimType moved (early cutoff).

A full rebuild runs only on a cold start, a format change (``FORMAT``, part of
the schema text), a compiler change, or cursors that no longer validate against
the logs they follow: a reseeded history, a replaced landing log or journal, a
door root the chain no longer holds. The live fold still serves every next
read; orient's Claim counts come from ``tally`` whenever the facts are current
at the read's coordinate and instant.

Time enters only through the trigger log. Each fact holds on the half-open
interval its verdict's boundaries bound; the part arms one deadline at the
earliest end, and a fire recomputes the facts it reaches. The body store logs
landings and erases, not a body rewritten in place: ``capture_bodies`` keeps the
identity each consulted object had, for the evidence sweep to compare.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_verdicts import (
    ClaimVerdictResult,
    ClaimVerdictResultAny,
    claim_verdict_v1_compat,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
)
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.consumers.protocol import ConsumerHealth, ConsumerRepair, ConsumerWork
from cruxible_core.consumers.state import DisposableState
from cruxible_core.indexes.typed_state import utc_microseconds
from cruxible_core.server.config import get_disabled_consumers
from cruxible_core.triggers.journal import (
    cancel_deadline,
    latest_sequence,
    schedule_deadline,
    trigger_events,
)

if TYPE_CHECKING:
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

#: The derivation format. Bump it whenever a row's derivation changes: the
#: number is part of the schema text, so a new one rebuilds the state (R5).
FORMAT = 1
#: This part's own deadline: the earliest instant a stored fact stops holding.
FACTS_EXPIRE = "next.facts.expire"
#: The statuses ``tally`` counts; ``error`` is a live Claim whose slot did not resolve.
TALLY_STATUSES = ("accepted", "conflicted", "overturned", "refused", "retired", "error")

_SCHEMA = f"""
CREATE TABLE format (v INTEGER NOT NULL CHECK(v={FORMAT})) STRICT;
INSERT INTO format VALUES ({FORMAT});
CREATE TABLE progress (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), target TEXT NOT NULL,
 failed TEXT, last_error TEXT, last_error_at TEXT
) STRICT;
CREATE TABLE cursors (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), heads TEXT NOT NULL,
 generation INTEGER NOT NULL, generation_oid TEXT NOT NULL, compiler TEXT NOT NULL,
 door_root TEXT NOT NULL, landing_log TEXT NOT NULL, landing INTEGER NOT NULL,
 reobs_journal TEXT NOT NULL, reobs_signature TEXT NOT NULL, reobs INTEGER NOT NULL,
 expire INTEGER NOT NULL, evaluated_at TEXT NOT NULL
) STRICT;
CREATE TABLE claim_facts (
 claim_id TEXT PRIMARY KEY, path TEXT NOT NULL, artifact_digest TEXT NOT NULL,
 lifecycle TEXT NOT NULL, predicate TEXT NOT NULL, qualifier TEXT,
 subject_path TEXT NOT NULL, object_path TEXT, res_slot TEXT NOT NULL,
 conflict_slot TEXT NOT NULL, claim_type_digest TEXT, input_digests TEXT NOT NULL,
 lineage_digests TEXT, lineage_complete INTEGER, verdict_json TEXT, verdict TEXT,
 currency TEXT, status TEXT, visible INTEGER, max_support_expiry_us INTEGER,
 set_args_json TEXT, lower_us INTEGER, upper_us INTEGER, error_code TEXT,
 error_detail TEXT, citation_error TEXT, unservable INTEGER NOT NULL
) STRICT;
CREATE INDEX claim_facts_by_slot ON claim_facts(res_slot,claim_id);
CREATE INDEX claim_facts_by_conflict_slot ON claim_facts(conflict_slot,claim_id);
CREATE INDEX claim_facts_by_upper ON claim_facts(upper_us) WHERE upper_us IS NOT NULL;
CREATE INDEX claim_facts_by_lower ON claim_facts(lower_us) WHERE lower_us IS NOT NULL;
CREATE TABLE slot_status (
 res_slot TEXT PRIMARY KEY, members TEXT NOT NULL, cardinality TEXT,
 claim_type_digest TEXT, statuses TEXT, lower_us INTEGER, upper_us INTEGER,
 error_code TEXT, error_detail TEXT
) STRICT;
CREATE TABLE fact_reads (
 read_key TEXT NOT NULL, claim_id TEXT NOT NULL, PRIMARY KEY(read_key,claim_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX fact_reads_by_claim ON fact_reads(claim_id,read_key);
CREATE TABLE capture_bodies (
 body_digest TEXT NOT NULL, capture_digest TEXT NOT NULL, identity TEXT,
 PRIMARY KEY(body_digest,capture_digest)
) STRICT, WITHOUT ROWID;
CREATE INDEX capture_bodies_by_capture ON capture_bodies(capture_digest,body_digest);
CREATE TABLE door_events (
 event_digest TEXT PRIMARY KEY, chain_pos INTEGER NOT NULL UNIQUE,
 claim_identity TEXT NOT NULL, partition TEXT NOT NULL, partition_seq INTEGER NOT NULL,
 claim_artifact_digest TEXT NOT NULL, basis TEXT NOT NULL, principal TEXT NOT NULL,
 stance TEXT NOT NULL, attested_at_us INTEGER NOT NULL, valid_until_us INTEGER,
 current_at_append INTEGER NOT NULL, referent_json TEXT NOT NULL, cited_captures TEXT NOT NULL
) STRICT;
CREATE INDEX door_events_by_claim ON door_events(claim_identity,chain_pos);
CREATE TABLE citation_commitments (
 claim_id TEXT NOT NULL, citation_id TEXT NOT NULL, commitment_digest TEXT NOT NULL,
 byte_length INTEGER NOT NULL, source_id TEXT, source_digest TEXT, original_start INTEGER,
 original_end INTEGER, whole_source INTEGER NOT NULL, lineage_note TEXT, set_arguments TEXT,
 PRIMARY KEY(claim_id,citation_id)
) STRICT;
CREATE INDEX citation_commitments_by_source ON citation_commitments(source_id,claim_id)
 WHERE source_id IS NOT NULL;
CREATE TABLE tally (status TEXT PRIMARY KEY, count INTEGER NOT NULL) STRICT;
INSERT INTO tally VALUES ('accepted',0),('conflicted',0),('overturned',0),('refused',0),
 ('retired',0),('error',0);
CREATE TRIGGER tally_added AFTER INSERT ON claim_facts BEGIN
 UPDATE tally SET count=count+1 WHERE status=coalesce(new.status,'error'); END;
CREATE TRIGGER tally_removed AFTER DELETE ON claim_facts BEGIN
 UPDATE tally SET count=count-1 WHERE status=coalesce(old.status,'error'); END;
CREATE TRIGGER tally_moved AFTER UPDATE OF status ON claim_facts BEGIN
 UPDATE tally SET count=count-1 WHERE status=coalesce(old.status,'error');
 UPDATE tally SET count=count+1 WHERE status=coalesce(new.status,'error'); END;
"""
_STATE = DisposableState("next/facts", _SCHEMA)

#: The Layer 1 tables, in the order a dump lists them (cursors and progress are bookkeeping).
LAYER1_TABLES = (
    "claim_facts",
    "slot_status",
    "fact_reads",
    "capture_bodies",
    "door_events",
    "citation_commitments",
    "tally",
)
_FACT_COLUMNS = (
    "claim_id",
    "path",
    "artifact_digest",
    "lifecycle",
    "predicate",
    "qualifier",
    "subject_path",
    "object_path",
    "res_slot",
    "conflict_slot",
    "claim_type_digest",
    "input_digests",
    "lineage_digests",
    "lineage_complete",
    "verdict_json",
    "verdict",
    "currency",
    "status",
    "visible",
    "max_support_expiry_us",
    "set_args_json",
    "lower_us",
    "upper_us",
    "error_code",
    "error_detail",
    "citation_error",
    "unservable",
)
_CLAIM_PATH = re.compile(r"claims/[0-9a-f]{2}/(CLM-[0-9a-f]{32})\.json")
_VERDICTS: TypeAdapter[ClaimVerdictResultAny] = TypeAdapter(ClaimVerdictResultAny)
_FIRES_PAGE = 1024


def _json(value: object) -> str:
    return canonical_json(value)


def _error_code(exc: BaseException) -> str:
    """An error compared by class and code (R4), never by its message."""

    code = getattr(exc, "code", None) or getattr(exc, "error_code", None)
    return type(exc).__name__ if not isinstance(code, str) else f"{type(exc).__name__}:{code}"


# -- heads and cursors -------------------------------------------------------------


@dataclass(frozen=True)
class _Heads:
    """Where every log this part follows stands now: one cheap read each."""

    coordinate: AcceptedProjectionCoordinate
    generation: int
    generation_oid: str
    compiler: str
    door_root: str
    landing_log: str
    landing: int
    reobs_journal: str
    reobs_signature: str
    expire: int

    def key(self) -> str:
        return _json(
            [
                self.generation,
                self.generation_oid,
                self.compiler,
                self.door_root,
                self.landing_log,
                self.landing,
                self.reobs_signature,
                self.expire,
            ]
        )


@dataclass(frozen=True)
class _Cursors:
    heads: str
    generation: int
    generation_oid: str
    compiler: str
    door_root: str
    landing_log: str
    landing: int
    reobs_journal: str
    reobs_signature: str
    reobs: int
    expire: int


def _heads(instance: Any) -> _Heads:
    from cruxible_core.evidence.claim_reobservations import reobservation_journal_identity

    coordinate = instance.accepted_coordinate()
    public = AcceptedCoordinate.from_internal(coordinate)
    with instance.accepted_history_reader(at=public) as history:
        generation = history.sequence
    landing = instance.body_store().landings.head()
    journal = reobservation_journal_identity(
        instance.root,
        exhaust=instance.descriptor.storage.exhaust,
        instance_id=instance.descriptor.instance_id,
    )
    return _Heads(
        coordinate=coordinate,
        generation=generation,
        generation_oid=public.git_oid,
        compiler=public.compiler_digest,
        door_root=instance.claim_attestation_evidence_store().head(),
        landing_log=landing.identity,
        landing=landing.sequence,
        reobs_journal="" if journal is None else journal[0],
        reobs_signature="" if journal is None else journal[1],
        expire=latest_sequence(instance, action=FACTS_EXPIRE),
    )


def _read_cursors(connection: sqlite3.Connection) -> _Cursors | None:
    row = connection.execute(
        "SELECT heads,generation,generation_oid,compiler,door_root,landing_log,landing,"
        "reobs_journal,reobs_signature,reobs,expire FROM cursors"
    ).fetchone()
    return None if row is None else _Cursors(*row)


def _cursors_hold(instance: Any, cursors: _Cursors, heads: _Heads) -> bool:
    """Whether stored cursors still name positions in the logs they follow."""

    if cursors.compiler != heads.compiler or cursors.generation > heads.generation:
        return False
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(heads.coordinate)
    ) as history:
        if history.generation(cursors.generation).git_oid != cursors.generation_oid:
            return False
    if (cursors.landing_log, cursors.landing) != ("", 0) and (
        cursors.landing_log != heads.landing_log or cursors.landing > heads.landing
    ):
        return False
    if cursors.reobs_journal and cursors.reobs_journal != heads.reobs_journal:
        return False
    return cursors.expire <= heads.expire


# -- one Claim's facts ---------------------------------------------------------------


@dataclass
class _Facts:
    """One Claim's facts at one instant, and everything they read."""

    claim: ClaimArtifactAny
    row: dict[str, Any]
    reads: frozenset[str]
    capture_bodies: dict[str, dict[str, Any] | None]
    citations: tuple[tuple[Any, ...], ...]
    verdict: ClaimVerdictResultAny | None

    @property
    def claim_id(self) -> str:
        return str(self.row["claim_id"])


def _slot_key(claim: ClaimArtifactAny, *, qualifier: bool) -> str:
    key: dict[str, object] = {
        "predicate": claim.statement.predicate,
        "subject": claim.statement.subject.model_dump(mode="json"),
    }
    if qualifier:
        key["qualifier"] = claim.statement.qualifier
    return canonical_bytes(key).decode("utf-8")


def _read_key(read: tuple[str, ...]) -> str:
    kind, *values = read
    return f"{kind}:{'|'.join(values)}"


def _max_support_expiry(verdict: ClaimVerdictResultAny, at: datetime) -> int | None:
    """The latest current support expiry: an expiring row is due once it is within the lead."""

    if not isinstance(verdict, ClaimVerdictResult) or verdict.verdict not in {
        "supported",
        "contradicted",
        "unresolved",
    }:
        return None
    supporting = set(verdict.supporting_evidence_digests)
    current = [
        item.expires_at
        for item in verdict.freshness_expirations
        if item.capture_digest in supporting and at < item.expires_at
    ]
    return utc_microseconds(max(current)) if current else None


def _citation_row(claim_id: str, commitment: Any) -> tuple[Any, ...]:
    return (
        claim_id,
        commitment.citation_id,
        commitment.commitment_digest,
        commitment.byte_length,
        commitment.source_id,
        commitment.source_digest,
        commitment.original_start,
        commitment.original_end,
        int(commitment.whole_source),
        commitment.lineage_note,
        None
        if commitment.set_arguments is None
        else _json([list(item) for item in commitment.set_arguments]),
    )


def _claim_facts(
    instance: Any,
    context: ClaimVerdictReadContext,
    claim: ClaimArtifactAny,
    *,
    at: datetime,
    stored: Mapping[str, Any] | None,
    claim_types: dict[str, Any],
) -> _Facts:
    """Derive one Claim's facts at ``at``, recording every read they made.

    ``stored`` is the Claim's previous row: an unchanged artifact keeps its
    lineage, which reads only immutable history.
    """

    from cruxible_client.contracts.claim_types import claim_type_digest, claim_type_path
    from cruxible_core.service.authoring.projection_sync import PROJECTION_VISIBILITY_POLICY
    from cruxible_core.service.claims.verdict_memo import invariance_interval
    from cruxible_core.service.discovery.next import (
        _bounded_claim_lineages,
        _claim_set_arguments,
        _claim_type_at,
        claim_citation_commitments,
    )
    from cruxible_core.service.evidence.evidence import service_evaluate_playbill_claim_verdict

    statement = claim.statement
    identity = claim.identity.qualified
    path = claim_path(claim.identity.name)
    type_path = claim_type_path(statement.predicate)
    subject_path = statement.subject.artifact_path
    object_path = (
        statement.object.address.artifact_path
        if isinstance(statement.object, SubjectClaimObject)
        else None
    )
    digest = claim_artifact_digest(claim).tagged
    coordinate = context.coordinate
    reads = {
        f"path:{path}",
        f"law:{path}",
        f"path:{type_path}",
        f"path:{subject_path}",
        f"reobserved:{claim.identity.name}",
        # Every Capture the Claim cites, consulted or not: a landing of one
        # reaches the Claim even when its availability was never read.
        *(f"backing:{capture}" for capture in claim.backing.capture_digests),
    }
    if object_path is not None:
        reads.add(f"path:{object_path}")
    errors: list[tuple[str, str]] = []
    boundaries: set[datetime] = set()
    verdict: ClaimVerdictResultAny | None = None
    type_digest: str | None = None
    recording = context.record()
    try:
        try:
            type_digest = claim_type_digest(context.claim_type(type_path)).tagged
        except CruxibleError:
            type_digest = None
        verdict = service_evaluate_playbill_claim_verdict(
            instance,
            claim_identity=identity,
            evaluation_time=at,
            at=AcceptedCoordinate.from_internal(coordinate),
            time_boundaries=boundaries,
            read_context=context,
        ).verdict
    except CruxibleError as exc:
        errors.append((_error_code(exc), str(exc)))
    finally:
        context.stop()
    reads.update(_read_key(read) for read in recording.keys())
    lower, upper = (
        invariance_interval(boundaries, evaluation_time=at) if verdict is not None else (None, None)
    )
    visible: int | None = None
    if verdict is not None:
        reported = claim_verdict_v1_compat(verdict).verdict
        visible = int(
            context.tree.get(subject_path) is not None
            and reported in PROJECTION_VISIBILITY_POLICY.visible_verdicts
            and verdict.currency in PROJECTION_VISIBILITY_POLICY.visible_currency
        )
    lineage_digests: str | None = None
    lineage_complete: int | None = None
    if stored is not None and stored["artifact_digest"] == digest:
        lineage_digests, lineage_complete = stored["lineage_digests"], stored["lineage_complete"]
    else:
        try:
            lineages, incomplete = _bounded_claim_lineages(
                instance, coordinate=coordinate, current_claims={path: claim}
            )
            lineage_digests = _json(list(lineages[path]))
            lineage_complete = int(path not in incomplete)
        except CruxibleError as exc:
            errors.append((_error_code(exc), str(exc)))
    set_args_json: str | None = None
    try:
        restated = _claim_set_arguments(
            claim, _claim_type_at(instance, coordinate.git_oid, statement.predicate, claim_types)
        )
        set_args_json = None if restated is None else _json(restated)
    except CruxibleError as exc:
        errors.append((_error_code(exc), str(exc)))
    citations: tuple[tuple[Any, ...], ...] = ()
    citation_error: str | None = None
    if claim.lifecycle.state == "live" and visible:
        try:
            citations = tuple(
                _citation_row(identity, commitment)
                for commitment in claim_citation_commitments(
                    instance,
                    claim=claim,
                    coordinate=coordinate,
                    store=context.body_store(),
                    claim_types=claim_types,
                )
            )
        except Exception as exc:  # noqa: BLE001 - mirrors the inventory's own refusal
            citation_error = _error_code(exc)
    verdict_json: str | None = None
    if verdict is not None:
        dumped = verdict.model_dump(mode="json")
        dumped.pop("evaluation_time")
        verdict_json = _json(dumped)
    row = {
        "claim_id": identity,
        "path": path,
        "artifact_digest": digest,
        "lifecycle": claim.lifecycle.state,
        "predicate": statement.predicate,
        "qualifier": statement.qualifier,
        "subject_path": subject_path,
        "object_path": object_path,
        "res_slot": _slot_key(claim, qualifier=False),
        "conflict_slot": _slot_key(claim, qualifier=True),
        "claim_type_digest": type_digest,
        "input_digests": _json(sorted(claim.backing.input_claim_digests)),
        "lineage_digests": lineage_digests,
        "lineage_complete": lineage_complete,
        "verdict_json": verdict_json,
        "verdict": None if verdict is None else verdict.verdict,
        "currency": None if verdict is None else verdict.currency,
        "status": "retired" if claim.lifecycle.state == "retired" else None,
        "visible": visible,
        "max_support_expiry_us": None if verdict is None else _max_support_expiry(verdict, at),
        "set_args_json": set_args_json,
        "lower_us": utc_microseconds(lower),
        "upper_us": utc_microseconds(upper),
        "error_code": errors[0][0] if errors else None,
        "error_detail": errors[0][1] if errors else None,
        "citation_error": citation_error,
        "unservable": int(not recording.bodies_complete or recording.inconsistent),
    }
    return _Facts(
        claim=claim,
        row=row,
        reads=frozenset(reads),
        capture_bodies=dict(recording.capture_bodies),
        citations=citations,
        verdict=verdict,
    )


def _slot_inputs(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """What a slot's resolution reads from one member: early cutoff compares this."""

    return (
        row["lifecycle"],
        row["res_slot"],
        row["artifact_digest"],
        row["verdict_json"],
        row["claim_type_digest"],
        row["error_code"],
    )


def _holds_at(row: Mapping[str, Any], at_us: int) -> bool:
    lower, upper = row["lower_us"], row["upper_us"]
    return (lower is None or lower <= at_us) and (upper is None or at_us < upper)


@dataclass
class _SlotResult:
    members: tuple[str, ...]
    cardinality: str | None
    claim_type_digest: str | None
    statuses: dict[str, str] | None
    error: tuple[str, str] | None


def _resolve_slot(
    instance: Any,
    context: ClaimVerdictReadContext,
    members: Sequence[ClaimArtifactAny],
    verdicts: Mapping[str, ClaimVerdictResultAny | None],
    type_digests: Mapping[str, str | None],
    *,
    at: datetime,
) -> _SlotResult:
    """Resolve one (predicate, Subject) slot exactly as the status derivation does."""

    from cruxible_core.claims.claim_slots import classify_claim_slot
    from cruxible_core.service.claims.claims import resolve_playbill_claim_group
    from cruxible_core.service.discovery.claim_status import _apply_resolution_statuses

    names = tuple(claim.identity.qualified for claim in members)
    first = members[0]
    type_digest = type_digests.get(first.identity.qualified)
    missing = [identity for identity in names if verdicts.get(identity) is None]
    if missing:
        return _SlotResult(
            names, None, type_digest, None, ("member_unresolved", ",".join(sorted(missing)))
        )
    try:
        resolution = resolve_playbill_claim_group(
            instance,
            subject=first.statement.subject,
            predicate=first.statement.predicate,
            coordinate=context.coordinate,
            evaluated_at=at,
            claims=tuple(members),
            verdicts_by_identity={
                identity: verdict for identity, verdict in verdicts.items() if verdict is not None
            },
            read_context=context,
        )
    except CruxibleError as exc:
        return _SlotResult(names, None, type_digest, None, (_error_code(exc), str(exc)))
    by_qualifier: dict[str | None, list[ClaimArtifactAny]] = defaultdict(list)
    for claim in members:
        by_qualifier[claim.statement.qualifier].append(claim)
    slots = {
        claim.identity.name: classification
        for group in by_qualifier.values()
        for classification in (classify_claim_slot(group),)
        for claim in group
    }
    statuses: dict[str, Any] = {}
    _apply_resolution_statuses(resolution, statuses, slots=slots)
    return _SlotResult(
        names,
        resolution.cardinality,
        type_digest,
        {f"Claim:{name}": status for name, status in statuses.items()},
        None,
    )


def _prefetch(context: ClaimVerdictReadContext, claims: Iterable[ClaimArtifactAny]) -> None:
    from cruxible_client.contracts.claim_types import claim_type_path

    batch = tuple(claims)
    context.prefetch(
        tuple(
            path
            for claim in batch
            for path in (
                claim_path(claim.identity.name),
                claim_type_path(claim.statement.predicate),
                claim.statement.subject.artifact_path,
                *(
                    (claim.statement.object.address.artifact_path,)
                    if isinstance(claim.statement.object, SubjectClaimObject)
                    else ()
                ),
            )
        )
    )
    for claim in batch:
        context.claim(claim.identity.qualified)
    context.prefetch_law_evidence(tuple(claim_path(claim.identity.name) for claim in batch))


def _door_row(position: int, event: Any, payload: Any) -> tuple[Any, ...]:
    statement = payload.attestation.statement
    return (
        event.event_digest,
        position,
        statement.claim_identity.qualified,
        event.partition_digest,
        event.sequence,
        statement.claim_artifact_digest,
        statement.attestation_basis,
        payload.attesting_principal_id,
        statement.stance,
        utc_microseconds(statement.attested_at),
        utc_microseconds(statement.valid_until),
        int(bool(payload.current_at_append)),
        _json(statement.referent_coordinate.model_dump(mode="json")),
        _json(list(statement.cited_capture_digests)),
    )


# -- the change set -----------------------------------------------------------------


@dataclass
class _Delta:
    """What one catch-up writes: replaced Claims, resolved slots, new door events, cursors."""

    facts: dict[str, _Facts] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)
    statuses: dict[str, str | None] = field(default_factory=dict)
    slots: dict[str, _SlotResult] = field(default_factory=dict)
    emptied_slots: set[str] = field(default_factory=set)
    interval_slots: set[str] = field(default_factory=set)
    door: list[tuple[Any, ...]] = field(default_factory=list)
    door_root: str = ""
    reobs: int = 0


def _rows(connection: sqlite3.Connection, sql: str, keys: Sequence[str]) -> list[tuple[Any, ...]]:
    """Run an ``IN (...)`` query over ``keys`` in bounded chunks."""

    found: list[tuple[Any, ...]] = []
    for start in range(0, len(keys), 500):
        chunk = tuple(keys[start : start + 500])
        found.extend(
            connection.execute(sql.format(marks=",".join("?" for _ in chunk)), chunk).fetchall()
        )
    return found


def _stored_facts(
    connection: sqlite3.Connection, claim_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    rows = _rows(
        connection,
        f"SELECT {','.join(_FACT_COLUMNS)} FROM claim_facts WHERE claim_id IN ({{marks}})",
        tuple(sorted(set(claim_ids))),
    )
    return {str(row[0]): dict(zip(_FACT_COLUMNS, row, strict=True)) for row in rows}


def _claim_of_path(path: str) -> str | None:
    match = _CLAIM_PATH.fullmatch(path)
    return None if match is None else f"Claim:{match.group(1)}"


def _attested_claims(instance: Any, paths: Iterable[str], oids: Iterable[str]) -> set[str]:
    """The Claims accepted attestation members name, read from either end of the range."""

    from cruxible_client.contracts.accepted_attestations import parse_accepted_attestation

    claims: set[str] = set()
    for path in paths:
        for oid in oids:
            content = instance.blob_at(oid, path)
            if content is not None:
                attestation = parse_accepted_attestation(content, path=path)
                claims.add(attestation.statement.claim_identity.qualified)
                break
    return claims


def _touched(
    instance: Any, connection: sqlite3.Connection, cursors: _Cursors, heads: _Heads
) -> tuple[set[str], int, datetime | None]:
    """The Claims the records past each cursor touch, the re-observation position, and expiry.

    Every term is a range read sized by what changed, mapped to Claims through
    ``fact_reads`` (and ``capture_bodies`` for a body some Capture consulted).
    """

    from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore

    claims: set[str] = set()
    keys: set[str] = set()
    if cursors.generation != heads.generation:
        with instance.accepted_history_reader(
            at=AcceptedCoordinate.from_internal(heads.coordinate)
        ) as history:
            paths = history.member_paths_between(cursors.generation, heads.generation)
        attested: list[str] = []
        for path in paths:
            keys.add(f"path:{path}")
            claim = _claim_of_path(path)
            if claim is not None:
                claims.add(claim)
                keys.add(f"law:{path}")
            elif path.startswith("providers/") and path.endswith(".json"):
                keys.add(f"provider:Provider:{path.removeprefix('providers/')[:-5]}")
            elif path.startswith("attestations/"):
                attested.append(path)
        claims |= _attested_claims(
            instance, attested, (heads.generation_oid, cursors.generation_oid)
        )
    if heads.landing != cursors.landing:
        landed = {
            row.digest
            for row in instance.body_store().landings.after(cursors.landing, through=heads.landing)
        }
        consulting = {
            str(row[0])
            for row in _rows(
                connection,
                "SELECT capture_digest FROM capture_bodies WHERE body_digest IN ({marks})",
                tuple(sorted(landed)),
            )
        }
        keys.update(f"capture:{digest}" for digest in landed | consulting)
        keys.update(f"backing:{digest}" for digest in landed)
    reobs = cursors.reobs
    if heads.reobs_signature != cursors.reobs_signature:
        for sequence, claim_id in ClaimReobservationStore(instance).records_after(cursors.reobs):
            claims.add(f"Claim:{claim_id}")
            reobs = max(reobs, sequence)
    expired_through: datetime | None = None
    if heads.expire != cursors.expire:
        after = cursors.expire
        while True:
            fires = trigger_events(instance, after=after, action=FACTS_EXPIRE, limit=_FIRES_PAGE)
            for fire in fires:
                if fire.sequence > heads.expire:
                    break
                if expired_through is None or fire.fired_at > expired_through:
                    expired_through = fire.fired_at
            if len(fires) < _FIRES_PAGE or fires[-1].sequence >= heads.expire:
                break
            after = fires[-1].sequence
        if expired_through is not None:
            claims.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT claim_id FROM claim_facts WHERE upper_us<=?",
                    (utc_microseconds(expired_through),),
                )
            )
    claims.update(
        str(row[0])
        for row in _rows(
            connection,
            "SELECT DISTINCT claim_id FROM fact_reads WHERE read_key IN ({marks})",
            tuple(sorted(keys)),
        )
    )
    return claims, reobs, expired_through


def _slot_members(
    instance: Any, context: ClaimVerdictReadContext, res_slot: str
) -> list[ClaimArtifactAny]:
    """The live Claims in one (predicate, Subject) slot at the coordinate: one index seek."""

    key = json.loads(res_slot)
    subject = SemanticAddress.model_validate(key["subject"])
    with instance.bind_accepted_projection(context.coordinate) as projection:
        identities = tuple(
            str(row[0])
            for row in projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE subject_path=? AND predicate=? "
                "AND subject_selector_scheme=? AND subject_selector_value=? "
                "AND lifecycle='live' ORDER BY identity",
                (
                    subject.artifact_path,
                    key["predicate"],
                    subject.selector.scheme,
                    subject.selector.value,
                ),
            )
        )
    members = [context.claim(identity) for identity in identities]
    # The index only locates rows; the parsed statement is the authority.
    return [claim for claim in members if _slot_key(claim, qualifier=False) == res_slot]


def _resolve_slots(
    instance: Any,
    context: ClaimVerdictReadContext,
    slots: Iterable[str],
    delta: _Delta,
    stored: dict[str, dict[str, Any]],
    *,
    at: datetime,
    claim_types: dict[str, Any],
    load: Callable[[Sequence[str]], dict[str, dict[str, Any]]],
) -> None:
    """Resolve each slot from its members' facts, recomputing any that no longer hold."""

    at_us = utc_microseconds(at)
    assert at_us is not None
    for res_slot in sorted(slots):
        members = _slot_members(instance, context, res_slot)
        if not members:
            delta.emptied_slots.add(res_slot)
            continue
        unread = [
            claim.identity.qualified
            for claim in members
            if claim.identity.qualified not in stored
            and claim.identity.qualified not in delta.facts
        ]
        if unread:
            stored.update(load(unread))
        verdicts: dict[str, ClaimVerdictResultAny | None] = {}
        type_digests: dict[str, str | None] = {}
        for claim in members:
            identity = claim.identity.qualified
            facts = delta.facts.get(identity)
            row = stored.get(identity)
            if facts is None and (row is None or not _holds_at(row, at_us)):
                facts = _claim_facts(
                    instance, context, claim, at=at, stored=row, claim_types=claim_types
                )
                delta.facts[identity] = facts
                delta.interval_slots.add(res_slot)
            if facts is not None:
                verdicts[identity] = facts.verdict
                type_digests[identity] = facts.row["claim_type_digest"]
            else:
                assert row is not None
                verdicts[identity] = (
                    None
                    if row["verdict_json"] is None
                    else _VERDICTS.validate_python(
                        {**json.loads(row["verdict_json"]), "evaluation_time": at}
                    )
                )
                type_digests[identity] = row["claim_type_digest"]
        result = _resolve_slot(instance, context, members, verdicts, type_digests, at=at)
        delta.slots[res_slot] = result
        for identity in result.members:
            delta.statuses[identity] = (
                None if result.statuses is None else result.statuses.get(identity)
            )


def _derive(
    instance: Any,
    context: ClaimVerdictReadContext,
    claim_ids: Iterable[str],
    stored: dict[str, dict[str, Any]],
    *,
    at: datetime,
    every_slot: bool,
    load: Callable[[Sequence[str]], dict[str, dict[str, Any]]],
) -> _Delta:
    """Recompute these Claims' facts and resolve every slot whose inputs moved.

    ``stored`` holds the previous rows read so far; ``load`` reads more of them
    when a slot needs a member nothing touched.
    """

    from cruxible_client.contracts.errors import ClaimNotFoundError

    delta = _Delta()
    claim_types: dict[str, Any] = {}
    present: list[ClaimArtifactAny] = []
    for identity in sorted(set(claim_ids)):
        try:
            present.append(context.claim(identity))
        except ClaimNotFoundError:
            delta.removed.add(identity)
    _prefetch(context, present)
    for claim in present:
        identity = claim.identity.qualified
        delta.facts[identity] = _claim_facts(
            instance,
            context,
            claim,
            at=at,
            stored=stored.get(identity),
            claim_types=claim_types,
        )
    slots: set[str] = set()
    for identity in sorted(delta.removed | set(delta.facts)):
        before = stored.get(identity)
        after = delta.facts.get(identity)
        moved = (
            every_slot
            or before is None
            or after is None
            or _slot_inputs(before) != _slot_inputs(after.row)
        )
        if before is not None and before["lifecycle"] == "live":
            if moved:
                slots.add(str(before["res_slot"]))
            elif (before["lower_us"], before["upper_us"]) != (
                after.row["lower_us"] if after else None,
                after.row["upper_us"] if after else None,
            ):
                delta.interval_slots.add(str(before["res_slot"]))
        if after is not None and after.row["lifecycle"] == "live" and moved:
            slots.add(str(after.row["res_slot"]))
    _resolve_slots(
        instance, context, slots, delta, stored, at=at, claim_types=claim_types, load=load
    )
    delta.interval_slots |= set(delta.slots)
    return delta


def _rebuild(instance: Any, heads: _Heads, *, at: datetime) -> _Delta:
    """Every Claim's facts from nothing: a cold start, a format or compiler change, a repair."""

    from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    # Read before the facts: a record appended meanwhile is followed from here.
    reobs = ClaimReobservationStore(instance).head_sequence()

    context = ClaimVerdictReadContext(instance, heads.coordinate)
    identities = tuple(claim.identity.qualified for claim in context.claims())
    delta = _derive(instance, context, identities, {}, at=at, every_slot=True, load=lambda _ids: {})
    door_root, events = instance.claim_attestation_evidence_store().events_after(None)
    delta.door = [_door_row(*event) for event in events]
    delta.door_root = door_root
    delta.reobs = reobs
    return delta


# -- writing -----------------------------------------------------------------------


def _write(
    connection: sqlite3.Connection,
    delta: _Delta,
    *,
    heads: _Heads,
    at: datetime,
) -> None:
    """Apply one change set and its cursors in the caller's one transaction."""

    replaced = sorted(delta.removed | set(delta.facts))
    previous = {
        str(row[0]).removeprefix("capture:")
        for row in _rows(
            connection,
            "SELECT read_key FROM fact_reads WHERE claim_id IN ({marks}) "
            "AND read_key LIKE 'capture:%'",
            replaced,
        )
    }
    stored_statuses = {
        str(row[0]): row[1]
        for row in _rows(
            connection,
            "SELECT claim_id,status FROM claim_facts WHERE claim_id IN ({marks})",
            replaced,
        )
    }
    for table in ("claim_facts", "fact_reads", "citation_commitments"):
        connection.executemany(
            f"DELETE FROM {table} WHERE claim_id=?", ((identity,) for identity in replaced)
        )
    consulted: dict[str, dict[str, Any] | None] = {}
    for identity, facts in sorted(delta.facts.items()):
        row = dict(facts.row)
        if row["lifecycle"] == "live":
            row["status"] = (
                delta.statuses[identity]
                if identity in delta.statuses
                else stored_statuses.get(identity)
            )
        connection.execute(
            f"INSERT INTO claim_facts({','.join(_FACT_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in _FACT_COLUMNS)})",
            tuple(row[column] for column in _FACT_COLUMNS),
        )
        connection.executemany(
            "INSERT INTO fact_reads VALUES (?,?)", ((key, identity) for key in sorted(facts.reads))
        )
        connection.executemany(
            "INSERT INTO citation_commitments VALUES (?,?,?,?,?,?,?,?,?,?,?)", facts.citations
        )
        consulted.update(facts.capture_bodies)
    for identity, status in delta.statuses.items():
        if identity not in delta.facts:
            connection.execute(
                "UPDATE claim_facts SET status=? WHERE claim_id=?", (status, identity)
            )
    connection.executemany(
        "DELETE FROM capture_bodies WHERE capture_digest=?", ((digest,) for digest in consulted)
    )
    connection.executemany(
        "INSERT INTO capture_bodies VALUES (?,?,?)",
        (
            (body, capture, None if identity is None else _json(list(identity)))
            for capture, bodies in sorted(consulted.items())
            if bodies is not None
            for body, identity in sorted(bodies.items())
        ),
    )
    # A Capture no remaining Claim consulted keeps no bodies.
    for capture in sorted(previous - set(consulted)):
        if (
            connection.execute(
                "SELECT 1 FROM fact_reads WHERE read_key=? LIMIT 1", (f"capture:{capture}",)
            ).fetchone()
            is None
        ):
            connection.execute("DELETE FROM capture_bodies WHERE capture_digest=?", (capture,))
    for res_slot in sorted(delta.emptied_slots):
        connection.execute("DELETE FROM slot_status WHERE res_slot=?", (res_slot,))
    for res_slot, result in sorted(delta.slots.items()):
        connection.execute(
            "INSERT INTO slot_status VALUES (?,?,?,?,?,NULL,NULL,?,?) ON CONFLICT(res_slot) "
            "DO UPDATE SET members=excluded.members,cardinality=excluded.cardinality,"
            "claim_type_digest=excluded.claim_type_digest,statuses=excluded.statuses,"
            "error_code=excluded.error_code,error_detail=excluded.error_detail",
            (
                res_slot,
                _json(list(result.members)),
                result.cardinality,
                result.claim_type_digest,
                None if result.statuses is None else _json(result.statuses),
                None if result.error is None else result.error[0],
                None if result.error is None else result.error[1],
            ),
        )
    # A slot holds while every member's verdict does: the intersection of their intervals.
    for res_slot in sorted(delta.interval_slots - delta.emptied_slots):
        connection.execute(
            "UPDATE slot_status SET (lower_us,upper_us)=(SELECT max(lower_us),min(upper_us) "
            "FROM claim_facts WHERE res_slot=? AND lifecycle='live') WHERE res_slot=?",
            (res_slot, res_slot),
        )
    connection.executemany(
        "INSERT OR IGNORE INTO door_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", delta.door
    )
    connection.execute(
        "INSERT INTO cursors VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE "
        "SET heads=excluded.heads,generation=excluded.generation,"
        "generation_oid=excluded.generation_oid,compiler=excluded.compiler,"
        "door_root=excluded.door_root,landing_log=excluded.landing_log,"
        "landing=excluded.landing,reobs_journal=excluded.reobs_journal,"
        "reobs_signature=excluded.reobs_signature,reobs=excluded.reobs,expire=excluded.expire,"
        "evaluated_at=excluded.evaluated_at",
        (
            heads.key(),
            heads.generation,
            heads.generation_oid,
            heads.compiler,
            delta.door_root,
            heads.landing_log,
            heads.landing,
            heads.reobs_journal,
            heads.reobs_signature,
            delta.reobs,
            heads.expire,
            format_datetime(at),
        ),
    )


def _clear(connection: sqlite3.Connection) -> None:
    for table in (*LAYER1_TABLES[:-1], "cursors"):
        connection.execute(f"DELETE FROM {table}")
    connection.execute("UPDATE tally SET count=0")


def _earliest_end(connection: sqlite3.Connection) -> int | None:
    (upper,) = connection.execute("SELECT min(upper_us) FROM claim_facts").fetchone()
    return None if upper is None else int(upper)


def _arm(instance: Any, upper_us: int | None) -> None:
    from datetime import UTC, timedelta

    if upper_us is None:
        cancel_deadline(instance, FACTS_EXPIRE)
    else:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        schedule_deadline(instance, FACTS_EXPIRE, epoch + timedelta(microseconds=upper_us))


def _load_facts(instance: Any, claim_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    with _STATE.open(instance) as connection:
        assert connection is not None
        return _stored_facts(connection, claim_ids)


def catch_up(instance: Any, *, now: datetime) -> None:
    """Move the facts to the current heads: from the records past each cursor, else rebuild."""

    from cruxible_core.evidence.claim_attestation_store import ClaimAttestationStoreError
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    heads = _heads(instance)
    with _STATE.open(instance) as connection:
        assert connection is not None
        original = _read_cursors(connection)
    delta: _Delta | None = None
    if original is not None and _cursors_hold(instance, original, heads):
        if original.heads == heads.key():
            return
        try:
            door_root, events = instance.claim_attestation_evidence_store().events_after(
                original.door_root
            )
        except ClaimAttestationStoreError as exc:
            if exc.error_code != "cruxible.claim_attestation.attestation_head_unknown":
                raise
        else:
            with _STATE.open(instance) as connection:
                assert connection is not None
                claims, reobs, fired_at = _touched(instance, connection, original, heads)
                stored = _stored_facts(connection, claims)
            # Time reaches the facts through the trigger log: a pass that follows a
            # fire evaluates at its instant, as the queue's fold does, never the
            # worker's clock; a stored fact that does not hold then is derived again.
            at = now if fired_at is None else fired_at
            delta = _derive(
                instance,
                ClaimVerdictReadContext(instance, heads.coordinate),
                claims,
                stored,
                at=at,
                every_slot=False,
                load=lambda claim_ids: _load_facts(instance, claim_ids),
            )
            delta.door = [_door_row(*event) for event in events]
            delta.door_root = door_root
            delta.reobs = reobs
    rebuild = delta is None
    if delta is None:
        at = now
        delta = _rebuild(instance, heads, at=at)
    # Arm before publishing: a crash after the write cannot leave facts without
    # their refresh signal. An early fire is a no-op catch-up that re-arms.
    ends = [
        facts.row["upper_us"] for facts in delta.facts.values() if facts.row["upper_us"] is not None
    ]
    if not rebuild:
        with _STATE.open(instance) as connection:
            assert connection is not None
            stored_end = _earliest_end(connection)
        if stored_end is not None:
            ends.append(stored_end)
    if ends:
        _arm(instance, min(ends))
    with _STATE.open(instance) as connection:
        assert connection is not None
        if _read_cursors(connection) != original:
            return  # another catch-up moved the facts; the next pass starts from there
        if rebuild:
            _clear(connection)
        _write(connection, delta, heads=heads, at=at)
        end = _earliest_end(connection)
    _arm(instance, end)


# -- reads -------------------------------------------------------------------------


def claim_status_tally(
    instance: Any, *, coordinate: AcceptedProjectionCoordinate, evaluation_time: datetime
) -> dict[str, int] | None:
    """Accepted Claims counted by status from the maintained tally, or None to derive live.

    Served only when the facts are current at this coordinate (generation,
    compiler, landing log and re-observation journal all at their heads), every
    live Claim's slot resolved, and ``evaluation_time`` inside every fact's
    interval: a handful of indexed reads, whatever the Claim count.
    """

    from cruxible_core.evidence.claim_reobservations import reobservation_journal_identity

    if not _PART.active(instance) or coordinate != instance.accepted_coordinate():
        return None
    public = AcceptedCoordinate.from_internal(coordinate)
    landing = instance.body_store().landings.head()
    journal = reobservation_journal_identity(
        instance.root,
        exhaust=instance.descriptor.storage.exhaust,
        instance_id=instance.descriptor.instance_id,
    )
    at_us = utc_microseconds(evaluation_time)
    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            return None
        cursors = _read_cursors(connection)
        if cursors is None or (
            cursors.generation_oid,
            cursors.compiler,
            cursors.landing_log,
            cursors.landing,
            cursors.reobs_signature,
        ) != (
            public.git_oid,
            public.compiler_digest,
            landing.identity,
            landing.sequence,
            "" if journal is None else journal[1],
        ):
            return None
        lower, upper = connection.execute(
            "SELECT max(lower_us),min(upper_us) FROM claim_facts"
        ).fetchone()
        if (lower is not None and at_us < lower) or (upper is not None and at_us >= upper):
            return None
        counts = dict(connection.execute("SELECT status,count FROM tally").fetchall())
    if counts.get("error"):
        return None
    return {status: int(counts.get(status, 0)) for status in TALLY_STATUSES[:-1]}


@contextmanager
def _layer1_connection(instance: Any) -> Iterator[sqlite3.Connection | None]:
    with _STATE.open(instance, create=False) as connection:
        yield connection


def _dump(connection: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
        for table in LAYER1_TABLES
    }


def layer1_tables(instance: Any) -> dict[str, list[tuple[Any, ...]]] | None:
    """Every stored Layer 1 table, rows sorted: what a fresh rebuild must equal."""

    with _layer1_connection(instance) as connection:
        return None if connection is None else _dump(connection)


def rebuilt_layer1_tables(instance: Any, *, at: datetime) -> dict[str, list[tuple[Any, ...]]]:
    """The Layer 1 tables a full rebuild at ``at`` writes, built apart from the stored state."""

    heads = _heads(instance)
    delta = _rebuild(instance, heads, at=at)
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        _write(connection, delta, heads=heads, at=at)
        return _dump(connection)
    finally:
        connection.close()


# -- the part ----------------------------------------------------------------------


class ClaimFactsPart:
    name = "next"

    def active(self, instance: Any) -> bool:
        return self.name not in get_disabled_consumers()

    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        target = _heads(instance).key()
        with _STATE.open(instance) as connection:
            assert connection is not None
            row = connection.execute("SELECT target FROM progress").fetchone()
            if row is not None and row[0] == target:
                return
            connection.execute(
                "INSERT INTO progress(singleton,target) VALUES (1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET target=excluded.target",
                (target,),
            )

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            row = connection.execute(
                "SELECT p.target FROM progress p LEFT JOIN cursors c ON c.singleton=1 "
                "WHERE (c.heads IS NULL OR c.heads IS NOT p.target) "
                "AND (p.failed IS NULL OR p.failed IS NOT p.target)"
            ).fetchone()
        return () if row is None else (ConsumerWork(key="facts", item=row[0]),)

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        instance = manager.get(instance_id)
        try:
            catch_up(instance, now=now)
        except Exception as exc:
            with _STATE.open(instance) as connection:
                assert connection is not None
                connection.execute(
                    "UPDATE progress SET failed=?,last_error=?,last_error_at=?",
                    (work.item, f"{type(exc).__name__}: {exc}", format_datetime(now)),
                )
            raise
        with _STATE.open(instance) as connection:
            assert connection is not None
            # The heads this pass reached are the target until matching sees new ones.
            connection.execute(
                "UPDATE progress SET target=coalesce((SELECT heads FROM cursors),target),"
                "failed=NULL,last_error=NULL,last_error_at=NULL"
            )

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            progress = connection.execute("SELECT target,last_error FROM progress").fetchone()
            cursors = _read_cursors(connection)
        if progress is None:
            return ()
        target, error = progress
        generation = None if cursors is None else cursors.generation
        with instance.accepted_history_reader() as history:
            behind = history.sequence - (0 if generation is None else generation)
        return (
            ConsumerHealth(
                kind=self.name,
                consumer_id="consumer:next",
                state="stalled"
                if error is not None
                else "lagging"
                if cursors is None or cursors.heads != target
                else "running",
                detail={
                    "format": FORMAT,
                    "generation": generation,
                    "generations_behind": max(0, behind),
                    "last_error": error,
                },
                repair=(
                    ConsumerRepair(
                        operation="hand_edit",
                        required_change="resolve_the_worker_error_then_rebuild_the_next_facts",
                        arguments={},
                    )
                    if error is not None
                    else None
                ),
            ),
        )


_PART = ClaimFactsPart()


__all__ = [
    "FACTS_EXPIRE",
    "FORMAT",
    "LAYER1_TABLES",
    "TALLY_STATUSES",
    "ClaimFactsPart",
    "catch_up",
    "claim_status_tally",
    "layer1_tables",
    "rebuilt_layer1_tables",
]
