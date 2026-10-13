"""Incremental next: each Claim's facts (Layer 1) and rows (Layer 2), kept from change records.

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
door root the chain no longer holds. Layer 2 (``rows``) derives each row
family's keys from these facts in the same transaction. A read at the head
serves the stored rows, catching up in memory on whatever the worker has not
yet followed and rolling that back (reads never write this state); orient's
Claim counts come from ``tally``.

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
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import TypeAdapter

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.claim_verdicts import (
    ClaimVerdictResult,
    ClaimVerdictResultAny,
    claim_verdict_v1_compat,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
)
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.consumers.next import rows
from cruxible_core.consumers.protocol import ConsumerHealth, ConsumerRepair, ConsumerWork
from cruxible_core.consumers.state import DisposableState
from cruxible_core.errors import ReadInputsMovingError
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
    from cruxible_core.service.discovery.next import PlaybillNextItemV1, _Holds
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

#: The derivation format. Bump it whenever a row's derivation changes: the
#: number is part of the schema text, so a new one rebuilds the state (R5).
FORMAT = 4
#: The one deadline next arms: the earliest instant a stored fact or row stops holding.
NEXT_EXPIRE = "next.expire"
#: The daily evidence sweep: each fire compares every consulted body's file identity,
#: the one change no record carries (a body rewritten in place, R2).
EVIDENCE_SWEEP = "evidence.sweep"
#: The statuses ``tally`` counts; ``error`` is a live Claim whose slot did not resolve.
TALLY_STATUSES = ("accepted", "conflicted", "overturned", "refused", "retired", "error")

_SCHEMA = (
    f"""
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
 expire INTEGER NOT NULL, sweep INTEGER NOT NULL, evaluated_at TEXT NOT NULL
) STRICT;
CREATE TABLE claim_facts (
 claim_id TEXT PRIMARY KEY, path TEXT NOT NULL, artifact_digest TEXT NOT NULL,
 lifecycle TEXT NOT NULL, predicate TEXT NOT NULL, qualifier TEXT,
 subject_path TEXT NOT NULL, object_path TEXT, res_slot TEXT NOT NULL,
 conflict_slot TEXT NOT NULL, claim_type_digest TEXT, input_digests TEXT NOT NULL,
 lineage_digests TEXT, lineage_complete INTEGER, verdict_json TEXT, verdict TEXT,
 currency TEXT, status TEXT, visible INTEGER, max_support_expiry_us INTEGER,
 set_args_json TEXT, lower_us INTEGER, upper_us INTEGER, error_code TEXT,
 error_detail TEXT, citation_error TEXT, unservable INTEGER NOT NULL, account_captures TEXT,
 pin_targets TEXT NOT NULL
) STRICT;
CREATE INDEX claim_facts_by_slot ON claim_facts(res_slot,claim_id);
CREATE INDEX claim_facts_by_support_expiry ON claim_facts(max_support_expiry_us)
 WHERE max_support_expiry_us IS NOT NULL;
CREATE INDEX claim_facts_with_errors ON claim_facts(lifecycle,path) WHERE error_code IS NOT NULL;
CREATE INDEX claim_facts_unservable ON claim_facts(claim_id) WHERE unservable=1;
CREATE TABLE claim_inputs (
 digest TEXT NOT NULL, claim_id TEXT NOT NULL, PRIMARY KEY(digest,claim_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX claim_inputs_by_claim ON claim_inputs(claim_id);
CREATE TABLE claim_lineage (
 digest TEXT NOT NULL, claim_id TEXT NOT NULL, PRIMARY KEY(digest,claim_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX claim_lineage_by_claim ON claim_lineage(claim_id);
CREATE TABLE claim_pins (
 target TEXT NOT NULL, claim_id TEXT NOT NULL, PRIMARY KEY(target,claim_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX claim_pins_by_claim ON claim_pins(claim_id);
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
 body_digest TEXT NOT NULL, capture_digest TEXT NOT NULL, claim_id TEXT NOT NULL, identity TEXT,
 PRIMARY KEY(body_digest,capture_digest,claim_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX capture_bodies_by_claim ON capture_bodies(claim_id);
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
CREATE INDEX citation_commitments_by_id ON citation_commitments(citation_id);
CREATE INDEX claim_facts_citation_errors ON claim_facts(claim_id) WHERE citation_error IS NOT NULL;
CREATE TABLE subject_shells (path TEXT PRIMARY KEY, kind TEXT NOT NULL) STRICT, WITHOUT ROWID;
CREATE INDEX subject_shells_by_kind ON subject_shells(kind,path);
CREATE TABLE subject_kind_counts (kind TEXT PRIMARY KEY, live INTEGER NOT NULL) STRICT;
CREATE TRIGGER subject_shell_added AFTER INSERT ON subject_shells BEGIN
 INSERT OR IGNORE INTO subject_kind_counts VALUES (new.kind,0);
 UPDATE subject_kind_counts SET live=live+1 WHERE kind=new.kind; END;
CREATE TRIGGER subject_shell_removed AFTER DELETE ON subject_shells BEGIN
 UPDATE subject_kind_counts SET live=live-1 WHERE kind=old.kind;
 DELETE FROM subject_kind_counts WHERE kind=old.kind AND live=0; END;
CREATE TABLE mandate_expiries (
 path TEXT PRIMARY KEY, identity TEXT NOT NULL, expires_at_us INTEGER NOT NULL
) STRICT, WITHOUT ROWID;
CREATE INDEX mandate_expiries_by_expiry ON mandate_expiries(expires_at_us,identity);
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
    + rows.SCHEMA
)
_STATE = DisposableState("next/facts", _SCHEMA)

#: The Layer 1 tables, in the order a dump lists them (cursors and progress are bookkeeping).
LAYER1_TABLES = (
    "claim_facts",
    "slot_status",
    "fact_reads",
    "capture_bodies",
    "door_events",
    "citation_commitments",
    "claim_inputs",
    "claim_lineage",
    "claim_pins",
    "subject_shells",
    "subject_kind_counts",
    "mandate_expiries",
    "tally",
)
#: Every table a rebuild writes, Layer 1 then Layer 2.
TABLES = (*LAYER1_TABLES, *rows.TABLES)
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
    "account_captures",
    "pin_targets",
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
    sweep: int

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
                self.sweep,
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
    sweep: int


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
        expire=latest_sequence(instance, action=NEXT_EXPIRE),
        sweep=latest_sequence(instance, action=EVIDENCE_SWEEP),
    )


def _read_cursors(connection: sqlite3.Connection) -> _Cursors | None:
    row = connection.execute(
        "SELECT heads,generation,generation_oid,compiler,door_root,landing_log,landing,"
        "reobs_journal,reobs_signature,reobs,expire,sweep FROM cursors"
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
    return cursors.expire <= heads.expire and cursors.sweep <= heads.sweep


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
    door_captures: Iterable[str] = (),
    strict: bool = False,
) -> _Facts:
    """Derive one Claim's facts at ``at``, recording every read they made.

    ``stored`` is the Claim's previous row: an unchanged artifact keeps its
    lineage, which reads only immutable history. ``door_captures`` are the
    Captures this Claim's new-capture door events cite: with its accepted
    new-capture attestations and its lineage's admission accounts, they are
    read for the door's rows, so their bodies are this Claim's reads too.
    ``strict`` raises the first error instead of recording it, as a read
    refusing like the fold does.
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
    door_reads, account_captures = _door_reads(
        instance, context, claim, door_captures=door_captures, stored=stored, digest=digest
    )
    reads.update(f"capture:{capture}" for capture in door_reads)
    errors: list[tuple[str, str]] = []
    raised: list[BaseException] = []
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
        raised.append(exc)
    finally:
        context.stop()
    if strict and raised:
        raise raised[0]
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
            raised.append(exc)
    set_args_json: str | None = None
    try:
        restated = _claim_set_arguments(
            claim, _claim_type_at(instance, coordinate.git_oid, statement.predicate, claim_types)
        )
        set_args_json = None if restated is None else _json(restated)
    except CruxibleError as exc:
        errors.append((_error_code(exc), str(exc)))
        raised.append(exc)
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
    if strict and raised:
        raise raised[0]
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
        "res_slot": rows.slot_key(claim, qualifier=False),
        "conflict_slot": rows.slot_key(claim, qualifier=True),
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
        "account_captures": None if account_captures is None else _json(account_captures),
        "pin_targets": _json(sorted({pin.target.qualified for pin in claim.pins})),
    }
    capture_bodies = dict(recording.capture_bodies)
    store = context.body_store()
    for capture in door_reads:
        consulted = capture_bodies.get(capture, {})
        if consulted is not None and capture not in consulted:
            # The door reads the Capture envelope itself.
            capture_bodies[capture] = {**consulted, capture: store.file_identity(capture)}
    return _Facts(
        claim=claim,
        row=row,
        reads=frozenset(reads),
        capture_bodies=capture_bodies,
        citations=citations,
        verdict=verdict,
    )


def _door_reads(
    instance: Any,
    context: ClaimVerdictReadContext,
    claim: ClaimArtifactAny,
    *,
    door_captures: Iterable[str],
    stored: Mapping[str, Any] | None,
    digest: str,
) -> tuple[tuple[str, ...], list[str] | None]:
    """The Captures the door reads for this Claim, and its lineage's account Captures.

    A Claim with new-capture observations (door events or accepted
    attestations) has its observed contents read and its lineage's admission
    accounts read from their citation envelopes. Accounts rest on immutable
    history, so an unchanged artifact keeps the ones it found.
    """

    from cruxible_core.service.discovery.next import _attestation_claim_lineage

    cited = set(door_captures)
    with instance.bind_accepted_projection(context.coordinate) as projection:
        for envelope in projection.typed.claim_attestations(
            basis="new_capture", claim_identities=(claim.identity.qualified,)
        ):
            cited.update(envelope.statement.cited_capture_digests)
    if not cited:
        return (), None
    if (
        stored is not None
        and stored["artifact_digest"] == digest
        and stored["account_captures"] is not None
    ):
        accounts = [str(item) for item in json.loads(stored["account_captures"])]
    else:
        lineage, _incomplete = _attestation_claim_lineage(
            instance, coordinate=context.coordinate, claim_identity=claim.identity.name
        )
        accounts = sorted(
            {
                citation.capture_digest
                for artifact in lineage
                for citation in claim_citation_references(artifact.claim)
            }
        )
    return tuple(sorted(cited | set(accounts))), accounts


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
    """What one pass replaces in Layer 1: Claims, resolved slots, new door events."""

    facts: dict[str, _Facts] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)
    #: Every replaced or removed Claim's previous row (None when it had none).
    before: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    statuses: dict[str, str | None] = field(default_factory=dict)
    slots: dict[str, _SlotResult] = field(default_factory=dict)
    emptied_slots: set[str] = field(default_factory=set)
    interval_slots: set[str] = field(default_factory=set)
    door: list[tuple[Any, ...]] = field(default_factory=list)
    door_root: str = ""
    reobs: int = 0
    #: Objects that landed, left or moved in place since the last pass, with the
    #: Captures that consulted them: what a door row may have read.
    landed: set[str] = field(default_factory=set)
    #: Accepted attestation members added or removed since the last pass, as
    #: (Claim, basis, Claim artifact digest): a threshold reads its version's
    #: members, a door every new-capture member of its Claim.
    members: set[tuple[str, str, str]] = field(default_factory=set)
    #: Subject and ClaimType member paths the generations changed: a threshold
    #: reads its Claim's referents at them.
    referents: set[str] = field(default_factory=set)
    #: Live Subjects by path (their kind), None for one no longer live (R9).
    subjects: dict[str, str | None] = field(default_factory=dict)
    #: Live ProcedureMandates by path (identity, expiry), None for one no longer live.
    mandates: dict[str, tuple[str, int] | None] = field(default_factory=dict)


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
    found = _rows(
        connection,
        f"SELECT {','.join(_FACT_COLUMNS)} FROM claim_facts WHERE claim_id IN ({{marks}})",
        tuple(sorted(set(claim_ids))),
    )
    return {str(row[0]): dict(zip(_FACT_COLUMNS, row, strict=True)) for row in found}


def _claim_of_path(path: str) -> str | None:
    match = _CLAIM_PATH.fullmatch(path)
    return None if match is None else f"Claim:{match.group(1)}"


def _attestation_members(
    instance: Any, paths: Iterable[str], oids: Iterable[str]
) -> set[tuple[str, str, str]]:
    """(Claim, basis, Claim artifact digest) of each accepted attestation member at these
    paths, read from either end of the range."""

    from cruxible_client.contracts.accepted_attestations import parse_accepted_attestation

    members: set[tuple[str, str, str]] = set()
    for path in paths:
        for oid in oids:
            content = instance.blob_at(oid, path)
            if content is not None:
                statement = parse_accepted_attestation(content, path=path).statement
                members.add(
                    (
                        statement.claim_identity.qualified,
                        statement.attestation_basis,
                        statement.claim_artifact_digest,
                    )
                )
                break
    return members


def _door_captures(
    pending: Sequence[tuple[Any, ...]],
    stored: Callable[[Sequence[str]], dict[str, set[str]]],
) -> Callable[[Sequence[str]], dict[str, tuple[str, ...]]]:
    """Each Claim's door-cited Captures: its stored new-capture events and these new ones."""

    fresh: dict[str, set[str]] = {}
    for row in pending:
        if row[6] == "new_capture":
            fresh.setdefault(str(row[2]), set()).update(json.loads(row[13]))

    def captures(claim_ids: Sequence[str]) -> dict[str, tuple[str, ...]]:
        found = stored(claim_ids)
        return {
            claim_id: tuple(sorted(found.get(claim_id, set()) | fresh.get(claim_id, set())))
            for claim_id in claim_ids
        }

    return captures


def _stored_door_captures(
    connection: sqlite3.Connection, claim_ids: Sequence[str]
) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for claim_id, cited in _rows(
        connection,
        "SELECT claim_identity,cited_captures FROM door_events WHERE basis='new_capture' "
        "AND claim_identity IN ({marks})",
        tuple(sorted(set(claim_ids))),
    ):
        found.setdefault(str(claim_id), set()).update(json.loads(cited))
    return found


class _StateGone(Exception):
    """The state a derivation reads vanished or was replaced under it (a rebuild)."""


def _load_door_captures(instance: Any, claim_ids: Sequence[str]) -> dict[str, set[str]]:
    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            raise _StateGone
        return _stored_door_captures(connection, claim_ids)


def _landing_rows(instance: Any, after: int, through: int) -> tuple[set[str], int]:
    """The digests the landing rows past ``after`` name, and the cursor they leave.

    An intent whose completion is not yet in the log keeps the cursor before
    it: the next pass reads the digest again once its writer (or
    reconciliation) publishes the completion.
    """

    landed: set[str] = set()
    open_intents: dict[str, int] = {}
    for row in instance.body_store().landings.after(after, through=through):
        landed.add(row.digest)
        if row.intent:
            open_intents.setdefault(row.digest, row.sequence)
        else:
            open_intents.pop(row.digest, None)
    cursor = through if not open_intents else min(open_intents.values()) - 1
    return landed, cursor


_SUBJECTS = "subjects/"
_MANDATES = "procedure-mandates/"
_SHELL_PREFIXES = (_SUBJECTS, _MANDATES)
#: The paths a Claim's statement can name as its subject or object referent.
_REFERENT_PREFIXES = (_SUBJECTS, "claim-types/")


def _shells(
    instance: Any, coordinate: AcceptedProjectionCoordinate, paths: Iterable[str] | None
) -> tuple[dict[str, str | None], dict[str, tuple[str, int] | None]]:
    """Live Subjects' kinds and live ProcedureMandates' expiries at these paths (all on None).

    One indexed read per changed path; the whole population only on a rebuild.
    """

    subjects: dict[str, str | None] = {}
    mandates: dict[str, tuple[str, int] | None] = {}
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        if paths is None:
            subjects.update(
                (str(path), str(kind))
                for path, kind in connection.execute(
                    "SELECT path,subject_kind FROM subjects WHERE lifecycle='live'"
                )
            )
            mandates.update(
                (str(path), (str(identity), int(expires)))
                for path, identity, expires in connection.execute(
                    "SELECT path,identity,expires_at_us FROM procedure_mandates "
                    "WHERE lifecycle='live'"
                )
            )
            return subjects, mandates
        for path in sorted(paths):
            if path.startswith(_SUBJECTS):
                row = connection.execute(
                    "SELECT subject_kind FROM subjects WHERE path=? AND lifecycle='live'", (path,)
                ).fetchone()
                subjects[path] = None if row is None else str(row[0])
            else:
                row = connection.execute(
                    "SELECT identity,expires_at_us FROM procedure_mandates "
                    "WHERE path=? AND lifecycle='live'",
                    (path,),
                ).fetchone()
                mandates[path] = None if row is None else (str(row[0]), int(row[1]))
    return subjects, mandates


def _rewritten(instance: Any, connection: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """The Claims that consulted a body whose file identity no longer matches, and the
    objects that moved: those bodies and the Captures that consulted them.

    The landing log sees bodies arrive and leave, not a body rewritten in
    place; the daily sweep closes that gap by comparing every consulted
    object's identity (one stat each), the one whole-table pass this part
    makes, and only on an ``evidence.sweep`` fire. Each fact whose derivation
    failed is retried there too, since a failed read may have recorded nothing.
    """

    store = instance.body_store()
    recorded: dict[str, set[str | None]] = defaultdict(set)
    consulted: dict[str, set[str]] = defaultdict(set)
    captures: dict[str, set[str]] = defaultdict(set)
    for body, capture, claim_id, identity in connection.execute(
        "SELECT body_digest,capture_digest,claim_id,identity FROM capture_bodies"
    ):
        recorded[str(body)].add(None if identity is None else str(identity))
        consulted[str(body)].add(str(claim_id))
        captures[str(body)].add(str(capture))
    claims: set[str] = set()
    moved: set[str] = set()
    for body, identities in recorded.items():
        found = store.file_identity(body)
        current = None if found is None else _json(list(found))
        if identities != {current}:
            claims |= consulted[body]
            moved |= {body, *captures[body]}
    return claims, moved


def _expired_facts(connection: sqlite3.Connection, at: datetime) -> set[str]:
    """Claims whose stored facts do not hold at ``at``: one range on each bound's index."""

    at_us = utc_microseconds(at)
    return {str(row[0]) for row in connection.execute(_EXPIRED_FACTS, (at_us, at_us))}


@dataclass
class _Touched:
    """The Claims a pass derives again, where it leaves its cursors, and its instant."""

    claims: set[str]
    reobs: int
    landing: int
    at: datetime
    landed: set[str]
    #: Subject and ProcedureMandate member paths the generations changed.
    shells: set[str] = field(default_factory=set)
    #: Accepted attestation members the generations added or removed.
    members: set[tuple[str, str, str]] = field(default_factory=set)
    #: Subject and ClaimType member paths the generations changed.
    referents: set[str] = field(default_factory=set)


def _touched(
    instance: Any,
    connection: sqlite3.Connection,
    cursors: _Cursors,
    heads: _Heads,
    door_rows: Sequence[tuple[Any, ...]] = (),
    *,
    now: datetime,
) -> _Touched:
    """The Claims the records past each cursor touch, and the instant to evaluate them at.

    A door event touches its statement's Claim. Every term is a range read
    sized by what changed, mapped to Claims through ``fact_reads`` (and
    ``capture_bodies`` for a body some Capture consulted). Time reaches the
    facts through the trigger log: a pass that follows a deadline fire
    evaluates at the fire's instant, as the fold does, never the worker's
    clock; every stored fact that does not hold then is derived again.
    """

    from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore

    claims: set[str] = set()
    keys: set[str] = set()
    shells: set[str] = set()
    members: set[tuple[str, str, str]] = set()
    referents: set[str] = set()
    if cursors.generation != heads.generation:
        with instance.accepted_history_reader(
            at=AcceptedCoordinate.from_internal(heads.coordinate)
        ) as history:
            paths = history.member_paths_between(cursors.generation, heads.generation)
        attested: list[str] = []
        for path in paths:
            keys.add(f"path:{path}")
            if path.startswith(_SHELL_PREFIXES):
                shells.add(path)
            if path.startswith(_REFERENT_PREFIXES):
                referents.add(path)
            claim = _claim_of_path(path)
            if claim is not None:
                claims.add(claim)
                keys.add(f"law:{path}")
            elif path.startswith("providers/") and path.endswith(".json"):
                keys.add(f"provider:Provider:{path.removeprefix('providers/')[:-5]}")
            elif path.startswith("attestations/"):
                attested.append(path)
        members = _attestation_members(
            instance, attested, (heads.generation_oid, cursors.generation_oid)
        )
        # A member is a verdict input of its Claim's version.
        claims.update(claim_id for claim_id, _basis, _digest in members)
    landing = cursors.landing
    landed: set[str] = set()
    if heads.landing != cursors.landing:
        landed, landing = _landing_rows(instance, cursors.landing, heads.landing)
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
        # A door row reads Captures: a landed body reaches it through the
        # Capture that consulted the body, as it reaches the Claim's facts.
        landed |= consulting
    reobs = cursors.reobs
    if heads.reobs_signature != cursors.reobs_signature:
        for sequence, claim_id in ClaimReobservationStore(instance).records_after(cursors.reobs):
            claims.add(f"Claim:{claim_id}")
            reobs = max(reobs, sequence)
    fired_at: datetime | None = None
    if heads.expire != cursors.expire:
        after = cursors.expire
        while True:
            fires = trigger_events(instance, after=after, action=NEXT_EXPIRE, limit=_FIRES_PAGE)
            for fire in fires:
                if fire.sequence > heads.expire:
                    break
                if fired_at is None or fire.fired_at > fired_at:
                    fired_at = fire.fired_at
            if len(fires) < _FIRES_PAGE or fires[-1].sequence >= heads.expire:
                break
            after = fires[-1].sequence
    if heads.sweep != cursors.sweep:
        rewritten_claims, rewritten = _rewritten(instance, connection)
        claims |= rewritten_claims
        landed |= rewritten
        # A derivation that failed may have read nothing to compare: retry it.
        claims.update(str(row[0]) for row in connection.execute(_BROKEN_FACTS))
    at = now if fired_at is None else fired_at
    claims |= _expired_facts(connection, at)
    reached = _rows(
        connection,
        "SELECT read_key,claim_id FROM fact_reads WHERE read_key IN ({marks})",
        tuple(sorted(keys)),
    )
    claims.update(str(row[1]) for row in reached)
    claims.update(str(row[2]) for row in door_rows)
    return _Touched(
        claims=claims,
        reobs=reobs,
        landing=landing,
        at=at,
        landed=landed,
        shells=shells,
        members=members,
        referents=referents,
    )


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
    return [claim for claim in members if rows.slot_key(claim, qualifier=False) == res_slot]


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
    door: Callable[[Sequence[str]], dict[str, tuple[str, ...]]],
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
                    instance,
                    context,
                    claim,
                    at=at,
                    stored=row,
                    claim_types=claim_types,
                    door_captures=door((identity,)).get(identity, ()),
                )
                delta.facts[identity] = facts
                delta.before.setdefault(identity, row)
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
    door: Callable[[Sequence[str]], dict[str, tuple[str, ...]]],
) -> _Delta:
    """Recompute these Claims' facts and resolve every slot whose inputs moved.

    ``stored`` holds the previous rows read so far; ``load`` reads more of them
    when a slot needs a member nothing touched; ``door`` gives each Claim's
    door-cited Captures.
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
            delta.before[identity] = stored.get(identity)
    _prefetch(context, present)
    cited = door(tuple(claim.identity.qualified for claim in present))
    for claim in present:
        identity = claim.identity.qualified
        delta.before[identity] = stored.get(identity)
        delta.facts[identity] = _claim_facts(
            instance,
            context,
            claim,
            at=at,
            stored=stored.get(identity),
            claim_types=claim_types,
            door_captures=cited.get(identity, ()),
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
        instance,
        context,
        slots,
        delta,
        stored,
        at=at,
        claim_types=claim_types,
        load=load,
        door=door,
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
    door_root, events = instance.claim_attestation_evidence_store().events_after(None)
    door_rows = [_door_row(*event) for event in events]
    delta = _derive(
        instance,
        context,
        identities,
        {},
        at=at,
        every_slot=True,
        load=lambda _ids: {},
        door=_door_captures(door_rows, lambda _ids: {}),
    )
    delta.door = door_rows
    delta.door_root = door_root
    delta.reobs = reobs
    delta.subjects, delta.mandates = _shells(instance, heads.coordinate, None)
    return delta


# -- writing -----------------------------------------------------------------------


def _json_list(value: str | None) -> list[str]:
    return [] if value is None else [str(item) for item in json.loads(value)]


def _apply_facts(connection: sqlite3.Connection, delta: _Delta) -> set[str]:
    """Apply one Layer 1 change set in the caller's transaction.

    Returns the Claims whose stored status moved: a slot's rows read its
    members' statuses, so each moved status dirties its slot.
    """

    moved: set[str] = set()
    replaced = sorted(delta.removed | set(delta.facts))
    stored_statuses = {
        str(row[0]): row[1]
        for row in _rows(
            connection,
            "SELECT claim_id,status FROM claim_facts WHERE claim_id IN ({marks})",
            replaced,
        )
    }
    for table in (
        "claim_facts",
        "fact_reads",
        "citation_commitments",
        "capture_bodies",
        "claim_inputs",
        "claim_lineage",
        "claim_pins",
    ):
        connection.executemany(
            f"DELETE FROM {table} WHERE claim_id=?", ((identity,) for identity in replaced)
        )
    moved.update(identity for identity in delta.removed if identity in stored_statuses)
    for identity, facts in sorted(delta.facts.items()):
        row = dict(facts.row)
        if row["lifecycle"] == "live":
            row["status"] = (
                delta.statuses[identity]
                if identity in delta.statuses
                else stored_statuses.get(identity)
            )
        if identity not in stored_statuses or stored_statuses[identity] != row["status"]:
            moved.add(identity)
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
        connection.executemany(
            "INSERT INTO capture_bodies VALUES (?,?,?,?)",
            (
                (body, capture, identity, None if found is None else _json(list(found)))
                for capture, bodies in sorted(facts.capture_bodies.items())
                if bodies is not None
                for body, found in sorted(bodies.items())
            ),
        )
        for table, column in (
            ("claim_inputs", "input_digests"),
            ("claim_lineage", "lineage_digests"),
            ("claim_pins", "pin_targets"),
        ):
            connection.executemany(
                f"INSERT OR IGNORE INTO {table} VALUES (?,?)",
                ((value, identity) for value in _json_list(row[column])),
            )
    for identity, status in delta.statuses.items():
        if identity not in delta.facts:
            changed = connection.execute(
                "UPDATE claim_facts SET status=? WHERE claim_id=? AND status IS NOT ?",
                (status, identity, status),
            ).rowcount
            if changed:
                moved.add(identity)
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
    for path, kind in sorted(delta.subjects.items()):
        connection.execute("DELETE FROM subject_shells WHERE path=?", (path,))
        if kind is not None:
            connection.execute("INSERT INTO subject_shells VALUES (?,?)", (path, kind))
    for path, mandate in sorted(delta.mandates.items()):
        connection.execute("DELETE FROM mandate_expiries WHERE path=?", (path,))
        if mandate is not None:
            connection.execute("INSERT INTO mandate_expiries VALUES (?,?,?)", (path, *mandate))
    return moved


def _write_cursors(
    connection: sqlite3.Connection,
    delta: _Delta,
    *,
    heads: _Heads,
    landing: int,
    at: datetime,
) -> None:
    """Record where this pass reached, in the caller's one transaction.

    ``landing`` is the landing cursor the pass reached: before the head while
    an operation it read is unfinished, so the heads it records are not the
    current ones and the part stays due until the completion lands.
    """

    connection.execute(
        "INSERT INTO cursors VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE "
        "SET heads=excluded.heads,generation=excluded.generation,"
        "generation_oid=excluded.generation_oid,compiler=excluded.compiler,"
        "door_root=excluded.door_root,landing_log=excluded.landing_log,"
        "landing=excluded.landing,reobs_journal=excluded.reobs_journal,"
        "reobs_signature=excluded.reobs_signature,reobs=excluded.reobs,expire=excluded.expire,"
        "sweep=excluded.sweep,evaluated_at=excluded.evaluated_at",
        (
            replace(heads, landing=landing).key(),
            heads.generation,
            heads.generation_oid,
            heads.compiler,
            delta.door_root,
            heads.landing_log,
            landing,
            heads.reobs_journal,
            heads.reobs_signature,
            delta.reobs,
            heads.expire,
            heads.sweep,
            format_datetime(at),
        ),
    )


def _clear(connection: sqlite3.Connection) -> None:
    for table in (*TABLES, "cursors"):
        if table != "tally":
            connection.execute(f"DELETE FROM {table}")
    connection.execute("UPDATE tally SET count=0")


# -- Layer 2 -----------------------------------------------------------------------


#: The fact columns each Layer 2 family reads of a Claim. A replaced fact whose
#: columns for a family are unchanged does not dirty that family's keys (early
#: cutoff); what a family reads besides facts arrives by its own subscription.
_THRESHOLD_READS = ("path", "artifact_digest", "lifecycle", "claim_type_digest")
_DOOR_READS = ("path", "artifact_digest", "lifecycle", "claim_type_digest")
_SLOT_READS = (
    "lifecycle",
    "res_slot",
    "conflict_slot",
    "artifact_digest",
    "claim_type_digest",
    "verdict_json",
    "status",
    "set_args_json",
    "error_code",
    "unservable",
    "lower_us",
    "upper_us",
)
_SOURCE_READS = (
    "path",
    "artifact_digest",
    "lifecycle",
    "lineage_digests",
    "lineage_complete",
    "visible",
    "verdict",
    "currency",
    "error_code",
    "unservable",
    "lower_us",
    "upper_us",
)
_DEPENDENT_READS = (
    "path",
    "artifact_digest",
    "lifecycle",
    "input_digests",
    "pin_targets",
    "visible",
    "verdict",
    "currency",
    "unservable",
    "lower_us",
    "upper_us",
)


def _moved(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None, reads: tuple[str, ...]
) -> bool:
    return (None if before is None else tuple(before[c] for c in reads)) != (
        None if after is None else tuple(after[c] for c in reads)
    )


def _dirty_keys(
    connection: sqlite3.Connection, delta: _Delta, moved: set[str]
) -> set[tuple[str, str]]:
    """The Layer 2 keys a Layer 1 change set reaches, read through the post-change indexes.

    A replaced Claim dirties a family's keys only when the fact columns that
    family reads moved: its threshold and door keys, its own source key, every
    slot of the (predicate, Subject) it left or joined (a slot's interval spans
    all its contenders), and every source its inputs or pins name. Besides
    facts, each family is reached only by the records it reads (design 3): a
    live Claim's threshold by an examined door event or an accepted
    attestation member of its current version and by a change at one of its
    referents; a door by a new-capture door event or accepted member of its
    Claim; and every key by a landing of an object it read.
    """

    keys: set[tuple[str, str]] = set()
    replaced = set(delta.facts) | delta.removed
    after = _stored_facts(connection, replaced)
    res_slots: set[str] = set()
    conflict_slots: set[str] = set()
    digests: set[str] = set()
    targets: set[str] = set()
    for identity in replaced:
        old, new = delta.before.get(identity), after.get(identity)
        if _moved(old, new, _THRESHOLD_READS) or (
            new is not None
            and new["lifecycle"] == "live"
            and (new["subject_path"] in delta.referents or new["object_path"] in delta.referents)
        ):
            keys.add((rows.THRESHOLD, identity))
        if _moved(old, new, _DOOR_READS):
            keys.add((rows.DOOR, identity))
        if _moved(old, new, _SOURCE_READS):
            keys.add((rows.SOURCE, identity))
        if _moved(old, new, _SLOT_READS):
            for side in (old, new):
                if side is not None and side["lifecycle"] == "live":
                    res_slots.add(str(side["res_slot"]))
                    conflict_slots.add(str(side["conflict_slot"]))
        if _moved(old, new, _DEPENDENT_READS):
            for side in (old, new):
                if side is not None:
                    digests.update(_json_list(side["input_digests"]))
                    targets.update(_json_list(side["pin_targets"]))
    for row in _stored_facts(connection, moved - replaced).values():
        if row["lifecycle"] == "live":
            res_slots.add(str(row["res_slot"]))
    keys.update((rows.SLOT, slot) for slot in conflict_slots)
    keys.update(
        (rows.SLOT, str(row[0]))
        for row in _rows(
            connection,
            "SELECT DISTINCT conflict_slot FROM claim_facts WHERE lifecycle='live' "
            "AND res_slot IN ({marks})",
            tuple(sorted(res_slots)),
        )
    )

    def current_version(claim_id: str, artifact_digest: str) -> bool:
        fact = after.get(claim_id)
        return (
            fact is not None
            and fact["lifecycle"] == "live"
            and fact["artifact_digest"] == artifact_digest
        )

    for event in delta.door:
        claim_id, artifact_digest, basis = str(event[2]), str(event[5]), event[6]
        if basis == "new_capture":
            keys.add((rows.DOOR, claim_id))
        elif current_version(claim_id, artifact_digest):
            keys.add((rows.THRESHOLD, claim_id))
    for claim_id, basis, artifact_digest in delta.members:
        if basis == "new_capture":
            keys.add((rows.DOOR, claim_id))
        if current_version(claim_id, artifact_digest):
            keys.add((rows.THRESHOLD, claim_id))
    keys.update(
        (str(row[0]), str(row[1]))
        for row in _rows(
            connection,
            "SELECT family,key FROM row_reads WHERE read_key IN ({marks})",
            tuple(sorted(f"capture:{digest}" for digest in delta.landed)),
        )
    )
    keys.update(
        (rows.SOURCE, str(row[0]))
        for row in _rows(
            connection,
            "SELECT DISTINCT claim_id FROM claim_lineage WHERE digest IN ({marks})",
            tuple(sorted(digests)),
        )
    )
    keys.update((rows.SOURCE, target) for target in targets if target.startswith("Claim:"))
    return keys


def _expired_keys(connection: sqlite3.Connection, at: datetime) -> set[tuple[str, str]]:
    """Keys whose stored rows do not hold at ``at``: one range on each bound's index."""

    at_us = utc_microseconds(at)
    return {(str(row[0]), str(row[1])) for row in connection.execute(_EXPIRED_KEYS, (at_us, at_us))}


def _all_keys(
    instance: Any, connection: sqlite3.Connection, coordinate: AcceptedProjectionCoordinate
) -> set[tuple[str, str]]:
    """Every key a rebuild derives: each live Claim's threshold and slot, each door, each source."""

    keys: set[tuple[str, str]] = set()
    for claim_id, lifecycle, conflict_slot in connection.execute(
        "SELECT claim_id,lifecycle,conflict_slot FROM claim_facts"
    ):
        keys.add((rows.SOURCE, str(claim_id)))
        if lifecycle == "live":
            keys.update(((rows.THRESHOLD, str(claim_id)), (rows.SLOT, str(conflict_slot))))
    keys.update(
        (rows.DOOR, str(row[0]))
        for row in connection.execute(
            "SELECT DISTINCT claim_identity FROM door_events WHERE basis='new_capture'"
        )
    )
    with instance.bind_accepted_projection(coordinate) as projection:
        keys.update(
            (rows.DOOR, str(envelope.statement.claim_identity.qualified))
            for envelope in projection.typed.claim_attestations(basis="new_capture")
        )
    return keys


@dataclass
class _Settlement:
    """What one settle wrote: its Layer 1 change sets in order, then its derived keys.

    Replaying it on the same stored state writes the same rows without
    deriving anything.
    """

    facts: list[_Delta] = field(default_factory=list)
    keys: rows.RowsDelta = field(default_factory=rows.RowsDelta)


def _replay(connection: sqlite3.Connection, settlement: _Settlement) -> None:
    for delta in settlement.facts:
        _apply_facts(connection, delta)
    rows.apply_dependents(connection, rows.apply(connection, settlement.keys))


def _settle(
    instance: Any,
    connection: sqlite3.Connection,
    *,
    coordinate: AcceptedProjectionCoordinate,
    at: datetime,
    delta: _Delta | None,
    rebuild: bool = False,
    reading: bool = False,
) -> _Settlement:
    """Apply a Layer 1 change set, bring every fact and key to ``at``, derive the keys it moved.

    Runs inside the caller's one transaction: the worker commits it, a read
    rolls it back. Every derivation reads Layer 1 through ``connection``, so
    it sees the facts exactly as this transaction leaves them. A read also
    derives again every fact some body read could not stand for (unservable):
    nothing records when such a body changes.
    """

    from cruxible_core.service.discovery.next import DEFAULT_EXPIRING_WITHIN_MICROSECONDS
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    settlement = _Settlement()
    keys: set[tuple[str, str]] = set()
    if rebuild:
        _clear(connection)
    if delta is not None:
        keys |= _dirty_keys(connection, delta, _apply_facts(connection, delta))
        settlement.facts.append(delta)
    again = _expired_facts(connection, at)
    if reading:
        again.update(str(row[0]) for row in connection.execute(_UNSERVABLE))
    if again:
        later = _derive(
            instance,
            ClaimVerdictReadContext(instance, coordinate),
            again,
            _stored_facts(connection, again),
            at=at,
            every_slot=False,
            load=lambda claim_ids: _stored_facts(connection, claim_ids),
            door=_door_captures((), lambda claim_ids: _stored_door_captures(connection, claim_ids)),
        )
        keys |= _dirty_keys(connection, later, _apply_facts(connection, later))
        settlement.facts.append(later)
    if rebuild:
        keys |= _all_keys(instance, connection, coordinate)
    keys |= _expired_keys(connection, at)
    context = rows.RowContext(
        instance,
        coordinate,
        connection,
        at=at,
        expiring_within=CanonicalDuration(microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS),
    )
    rows.derive(context, keys, settlement.keys)
    rows.apply_dependents(connection, rows.apply(connection, settlement.keys))
    return settlement


#: Every probe a pass or a read makes of the stored state: each one seek or one
#: range on a partial index, sized by its answer (pinned by plan tests).
_EXPIRED_FACTS = (
    "SELECT claim_id FROM claim_facts WHERE upper_us IS NOT NULL AND upper_us<=? "
    "UNION SELECT claim_id FROM claim_facts WHERE lower_us IS NOT NULL AND lower_us>?"
)
_EXPIRED_KEYS = (
    "SELECT family,key FROM row_keys WHERE upper_us IS NOT NULL AND upper_us<=? "
    "UNION SELECT family,key FROM row_keys WHERE lower_us IS NOT NULL AND lower_us>?"
)
_UNSERVABLE = "SELECT claim_id FROM claim_facts WHERE unservable=1"
_BROKEN_KEYS = "SELECT family,key FROM row_keys WHERE error_code IS NOT NULL"
_BROKEN_OF_KEYS = (
    "SELECT key FROM row_keys WHERE family=? AND key IN ({marks}) AND error_code IS NOT NULL"
)
_BROKEN_FACTS = "SELECT claim_id FROM claim_facts WHERE error_code IS NOT NULL"
_BROKEN_RETIRED = (
    "SELECT claim_id FROM claim_facts WHERE lifecycle='retired' AND error_code IS NOT NULL "
    "ORDER BY path LIMIT 1"
)
_EXPIRING_SLOTS = (
    "SELECT conflict_slot FROM claim_facts WHERE max_support_expiry_us IS NOT NULL "
    "AND max_support_expiry_us>? AND max_support_expiry_us<=? AND lifecycle='live'"
)
_KIND_COUNTS = "SELECT kind,live FROM subject_kind_counts ORDER BY kind"
_CITATION_ERROR = "SELECT 1 FROM claim_facts WHERE citation_error IS NOT NULL LIMIT 1"
_FIRST_OF_KIND = "SELECT path FROM subject_shells WHERE kind=? ORDER BY path LIMIT 1"
_EXPIRING_MANDATES = (
    "SELECT identity FROM mandate_expiries WHERE expires_at_us>? AND expires_at_us<=? "
    "ORDER BY identity"
)
_EARLIEST_END = (
    "SELECT upper_us FROM claim_facts WHERE upper_us IS NOT NULL ORDER BY upper_us LIMIT 1"
)
_KEYS_EARLIEST_END = (
    "SELECT upper_us FROM row_keys WHERE upper_us IS NOT NULL ORDER BY upper_us LIMIT 1"
)


def _earliest_end(connection: sqlite3.Connection) -> int | None:
    """The earliest instant a stored fact or key stops holding: one seek on each index."""

    ends = [
        int(row[0])
        for query in (_EARLIEST_END, _KEYS_EARLIEST_END)
        for row in (connection.execute(query).fetchone(),)
        if row is not None
    ]
    return min(ends) if ends else None


def _arm(instance: Any, upper_us: int | None) -> None:
    from datetime import UTC, timedelta

    if upper_us is None:
        cancel_deadline(instance, NEXT_EXPIRE)
    else:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        schedule_deadline(instance, NEXT_EXPIRE, epoch + timedelta(microseconds=upper_us))


def _load_facts(instance: Any, claim_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            raise _StateGone
        return _stored_facts(connection, claim_ids)


@dataclass(frozen=True)
class _Change:
    """One Layer 1 change set past the stored cursors, its instant and landing cursor."""

    delta: _Delta
    at: datetime
    landing: int


def _changes(instance: Any, original: _Cursors, heads: _Heads, *, now: datetime) -> _Change | None:
    """The Layer 1 change set past the stored cursors, or None when only a rebuild will do.

    Derived outside any lock: verdicts are the expensive part. Raises
    ``_StateGone`` when the stored state moved or vanished meanwhile.
    """

    from cruxible_core.evidence.claim_attestation_store import ClaimAttestationStoreError
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    if not _cursors_hold(instance, original, heads):
        return None
    try:
        door_root, events = instance.claim_attestation_evidence_store().events_after(
            original.door_root
        )
    except ClaimAttestationStoreError as exc:
        if exc.error_code != "cruxible.claim_attestation.attestation_head_unknown":
            raise
        return None
    door_rows = [_door_row(*event) for event in events]
    with _STATE.open(instance, create=False) as connection:
        if connection is None or _read_cursors(connection) != original:
            raise _StateGone
        touched = _touched(instance, connection, original, heads, door_rows, now=now)
        stored = _stored_facts(connection, touched.claims)
    delta = _derive(
        instance,
        ClaimVerdictReadContext(instance, heads.coordinate),
        touched.claims,
        stored,
        at=touched.at,
        every_slot=False,
        load=lambda claim_ids: _load_facts(instance, claim_ids),
        door=_door_captures(door_rows, lambda claim_ids: _load_door_captures(instance, claim_ids)),
    )
    delta.door = door_rows
    delta.door_root = door_root
    delta.reobs = touched.reobs
    delta.landed = touched.landed
    delta.members = touched.members
    delta.referents = touched.referents
    if touched.shells:
        delta.subjects, delta.mandates = _shells(instance, heads.coordinate, touched.shells)
    return _Change(delta, touched.at, touched.landing)


def catch_up(instance: Any, *, now: datetime) -> None:
    """Move facts and rows to the current heads: from the records past each cursor, else rebuild."""

    # Settle any landing a writer left unfinished, so its completion is a row to follow.
    instance.body_store().landings.reconcile()
    heads = _heads(instance)
    with _STATE.open(instance) as connection:
        assert connection is not None
        original = _read_cursors(connection)
    if original is not None and original.heads == heads.key():
        return
    try:
        change = None if original is None else _changes(instance, original, heads, now=now)
    except _StateGone:
        return  # another pass moved the state; the next pass starts from there
    rebuild = change is None
    if change is None:
        change = _Change(_rebuild(instance, heads, at=now), now, heads.landing)
    with _STATE.open(instance) as connection:
        assert connection is not None
        if _read_cursors(connection) != original:
            return  # another catch-up moved the state; the next pass starts from there
        _settle(
            instance,
            connection,
            coordinate=heads.coordinate,
            at=change.at,
            delta=change.delta,
            rebuild=rebuild,
        )
        _write_cursors(connection, change.delta, heads=heads, landing=change.landing, at=change.at)
        # Armed before the commit: no published fact or row is left without its
        # refresh signal. An early fire is a no-op pass that arms again.
        _arm(instance, _earliest_end(connection))


@dataclass(frozen=True)
class Rebuilt:
    """What a repair rebuilt: Claims with facts, row keys and stored rows."""

    claims: int
    keys: int
    rows: int


def rebuild(instance: Any, *, now: datetime) -> Rebuilt:
    """Drop every next table and derive them again from nothing (R6).

    The operator's repair for state the worker cannot follow forward: the
    whole population is derived, as on a cold start, and the part's error is
    cleared. Reads fold until it lands.
    """

    _STATE.reset(instance)
    with _OVERLAYS_LOCK:
        for key in [key for key in _OVERLAYS if key[0] == str(instance.root)]:
            del _OVERLAYS[key]
    catch_up(instance, now=now)
    _PART.match(instance, now=now, daemon_id="rebuild")
    with _STATE.open(instance) as connection:
        assert connection is not None
        counts = [
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("claim_facts", "row_keys", "claim_rows")
        ]
    return Rebuilt(*counts)


# -- reads -------------------------------------------------------------------------


@dataclass(frozen=True)
class ServedRows:
    """Stored Claim rows at the head, caught up to one read's instant, in the fold's order.

    ``holds`` decides each row's ``unsure`` hold over exactly these rows' Claims.
    """

    rows: tuple[PlaybillNextItemV1, ...]
    holds: _Holds
    #: Each kind with live Subjects: (kind, live count, first path), in kind order.
    #: None on a read of a few Claims' keys.
    subject_kinds: tuple[tuple[str, int, str], ...] | None = None
    #: Live ProcedureMandates expiring within the read's lead, in identity order.
    expiring_mandates: tuple[str, ...] | None = None
    #: The citation commitments the read asked for, by id: of these citation ids,
    #: or every one that names a workspace source (live visible Claims only).
    citations: dict[str, Any] | None = None
    #: Whether some live visible Claim's citations could not be read (the fold refuses).
    citation_error: bool = False
    #: The projection-backed Claims the read asked for, by qualified identity:
    #: (resolution status, visible), or None for a Claim whose facts failed.
    backed: dict[str, tuple[str, bool] | None] | None = None
    #: The heads the rows were read at; ``served_inputs_hold`` checks them again.
    heads: _Heads | None = None


#: The citation commitments a workspace read asks for: those of these citation ids, or
#: every one naming a workspace source ("sources"), or none.
CitationRequest = Literal["sources"] | tuple[str, ...] | None

#: In-flight and recent overlays, by (instance, stored heads, current heads).
_OVERLAYS: OrderedDict[tuple[str, str, str], Future[_Overlay | None]] = OrderedDict()
_OVERLAYS_LOCK = threading.Lock()
_OVERLAY_CAPACITY = 8
#: How many times a read tries when the logs move under it before it refuses, and
#: how long it pauses before each retry: a landing in flight is usually done by then.
_ATTEMPTS = 3
_PAUSES = (0.01, 0.05)


class _Unstable(Exception):
    """The inputs a derivation read moved while it ran: its result describes no one state."""


@dataclass(frozen=True)
class _Overlay:
    """The worker's next pass, settled in memory: what a read replays instead of deriving."""

    settlement: _Settlement
    door_root: str


def _inputs_hold(instance: Any, heads: _Heads) -> bool:
    """Whether every log a derivation reads still stands where ``heads`` saw it.

    The accepted coordinate, the door head, the landing log (with no landing
    in flight: a body moves between an operation's intent and its completion)
    and the re-observation journal. A derivation bracketed by two such checks
    read one state of its inputs.
    """

    from cruxible_core.evidence.claim_reobservations import reobservation_journal_identity

    landings = instance.body_store().landings
    head = landings.head()
    if (head.identity, head.sequence) != (heads.landing_log, heads.landing):
        return False
    if landings.unfinished():
        return False
    if instance.accepted_coordinate() != heads.coordinate:
        return False
    if instance.claim_attestation_evidence_store().head() != heads.door_root:
        return False
    journal = reobservation_journal_identity(
        instance.root,
        exhaust=instance.descriptor.storage.exhaust,
        instance_id=instance.descriptor.instance_id,
    )
    return ("" if journal is None else journal[1]) == heads.reobs_signature


def _overlay(instance: Any, original: _Cursors, heads: _Heads, *, now: datetime) -> _Overlay | None:
    """The settlement past the worker's cursors, derived once per (stored, current) heads.

    The first reader derives the Layer 1 change set, then settles it and the
    Layer 2 keys it dirties in a transaction it rolls back, keeping what it
    wrote; concurrent readers of the same pair wait on that, and every read
    replays it in its own rolled-back transaction. A read at another instant
    derives only what does not hold at its instant. A settlement whose inputs
    moved while it was derived is never kept: every waiter retries.
    """

    key = (str(instance.root), original.heads, heads.key())
    with _OVERLAYS_LOCK:
        flight = _OVERLAYS.get(key)
        owner = flight is None
        if flight is None:
            flight = Future()
            _OVERLAYS[key] = flight
            while len(_OVERLAYS) > _OVERLAY_CAPACITY:
                _OVERLAYS.popitem(last=False)
    if owner:
        try:
            flight.set_result(_settled_overlay(instance, original, heads, now=now))
        except BaseException as exc:
            with _OVERLAYS_LOCK:
                if _OVERLAYS.get(key) is flight:
                    del _OVERLAYS[key]
            flight.set_exception(exc)
            raise
    return flight.result()


def _settled_overlay(
    instance: Any, original: _Cursors, heads: _Heads, *, now: datetime
) -> _Overlay | None:
    if not _inputs_hold(instance, heads):
        raise _Unstable
    try:
        change = _changes(instance, original, heads, now=now)
    except _StateGone as exc:
        raise _Unstable from exc
    if change is None:
        return None
    if not _inputs_hold(instance, heads):
        raise _Unstable
    with _STATE.open(instance, create=False) as connection:
        if connection is None or _read_cursors(connection) != original:
            raise _Unstable
        connection.execute("SAVEPOINT overlay")
        try:
            settlement = _settle(
                instance,
                connection,
                coordinate=heads.coordinate,
                at=change.at,
                delta=change.delta,
            )
        finally:
            connection.execute("ROLLBACK TO overlay")
            connection.execute("RELEASE overlay")
    if not _inputs_hold(instance, heads):
        raise _Unstable
    return _Overlay(settlement, change.delta.door_root)


_T = TypeVar("_T")


def _serving(
    instance: Any,
    *,
    coordinate: AcceptedProjectionCoordinate,
    door_head: str | None,
    evaluation_time: datetime,
    read: Callable[[sqlite3.Connection], _T],
) -> tuple[_T, _Heads] | None:
    """Run ``read`` on the stored state brought to the heads and to ``evaluation_time``.

    Served only at the current accepted (and, when given, door) head with the
    worker active and its state warm and valid; in any state that cannot
    serve, None: fold. Whatever the worker has not yet followed is replayed
    from the overlay and every fact or key that does not hold at the instant
    is derived again, all in a transaction rolled back after ``read`` (reads
    never write this state). The read is returned, with the heads it was read
    at, only when every log it depended on stood still throughout. When the
    logs move under every one of a few attempts it refuses
    (``ReadInputsMovingError``, retryable): a fold then would read the same
    moving logs for longer, so it is never the fallback for a moving read.
    """

    if not _PART.active(instance) or coordinate != instance.accepted_coordinate():
        return None
    for attempt in range(_ATTEMPTS):
        if attempt:
            # A landing whose writer is gone stays unfinished until someone settles
            # it: settle it, as opening the store does (a live writer's is skipped),
            # then give a landing in flight a moment to complete.
            instance.body_store().landings.reconcile()
            time.sleep(_PAUSES[attempt - 1])
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return None
            original = _read_cursors(connection)
        if original is None:
            return None
        heads = _heads(instance)
        if heads.coordinate != coordinate or door_head not in (None, heads.door_root):
            return None
        if not _inputs_hold(instance, heads):
            continue
        overlay: _Overlay | None = None
        if original.heads != heads.key():
            try:
                overlay = _overlay(instance, original, heads, now=evaluation_time)
            except _Unstable:
                continue
            if overlay is None or overlay.door_root != heads.door_root:
                return None
        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return None
            if _read_cursors(connection) != original:
                continue
            connection.execute("SAVEPOINT served")
            try:
                if overlay is not None:
                    _replay(connection, overlay.settlement)
                _settle(
                    instance,
                    connection,
                    coordinate=coordinate,
                    at=evaluation_time,
                    delta=None,
                    reading=True,
                )
                result = read(connection)
            finally:
                connection.execute("ROLLBACK TO served")
                connection.execute("RELEASE served")
        if _inputs_hold(instance, heads):
            return result, heads
    raise ReadInputsMovingError(
        f"the accepted state, the attestation door, body landings or re-observations moved "
        f"during each of {_ATTEMPTS} attempts to read next's stored rows"
    )


def served_inputs_hold(instance: Any, served: ServedRows) -> bool:
    """Whether every log ``served`` was read from still stands where the read saw it.

    A request that reads more live state after its stored rows (a projection
    query's evidence) checks again once done: true means everything it read
    describes one state.
    """

    return served.heads is None or _inputs_hold(instance, served.heads)


def served_claim_rows(
    instance: Any,
    *,
    coordinate: AcceptedProjectionCoordinate,
    door_head: str,
    evaluation_time: datetime,
    expiring_within: CanonicalDuration,
    claims: Iterable[str] | None = None,
    citations: CitationRequest = None,
    backed: Iterable[str] = (),
) -> ServedRows | None:
    """The Claim rows next would fold, served from Layer 2, or None to fold.

    ``claims`` reads only those Claims' keys -- their threshold, door and
    dependency rows and their slots' -- instead of the whole queue.
    ``citations`` also reads the maintained citation commitments a workspace
    observation asks about, and ``backed`` the resolution status and
    visibility of the Claims its projection markers are backed by.
    """

    selected = None if claims is None else tuple(claims)
    wanted = tuple(sorted(set(backed)))
    served = _serving(
        instance,
        coordinate=coordinate,
        door_head=door_head,
        evaluation_time=evaluation_time,
        read=lambda connection: _read_served(
            instance,
            connection,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
            expiring_within=expiring_within,
            claims=selected,
            citations=citations,
            backed=wanted,
        ),
    )
    if served is None:
        return None
    found, heads = served
    return replace(found, heads=heads)


def _selected_keys(connection: sqlite3.Connection, claims: Sequence[str]) -> set[tuple[str, str]]:
    """These Claims' own keys and their slots': what a hold over them reads."""

    keys: set[tuple[str, str]] = set()
    for claim_id in claims:
        keys.update(
            ((rows.THRESHOLD, claim_id), (rows.DOOR, claim_id), (rows.DEPENDENCY, claim_id))
        )
    keys.update(
        (rows.SLOT, str(row[0]))
        for row in _rows(
            connection,
            "SELECT conflict_slot FROM claim_facts WHERE lifecycle='live' "
            "AND claim_id IN ({marks})",
            tuple(sorted(set(claims))),
        )
    )
    return keys


def _broken_keys(
    connection: sqlite3.Connection, keys: set[tuple[str, str]] | None
) -> set[tuple[str, str]]:
    """The keys whose stored derivation failed: of these keys (one seek each), or all."""

    if keys is None:
        return {(str(row[0]), str(row[1])) for row in connection.execute(_BROKEN_KEYS)}
    broken: set[tuple[str, str]] = set()
    for family in sorted({family for family, _key in keys}):
        wanted = sorted(key for found, key in keys if found == family)
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            broken.update(
                (family, str(row[0]))
                for row in connection.execute(
                    _BROKEN_OF_KEYS.format(marks=",".join("?" for _ in chunk)), (family, *chunk)
                )
            )
    return broken


def _slot_order(connection: sqlite3.Connection, slot: str) -> bytes:
    """Where the fold reaches a slot: at its first live member, in identity order."""

    row = connection.execute(
        "SELECT min(claim_id) FROM claim_facts WHERE conflict_slot=? AND lifecycle='live'",
        (slot,),
    ).fetchone()
    return (slot if row is None or row[0] is None else str(row[0])).encode()


def _refuse_errors(
    instance: Any,
    connection: sqlite3.Connection,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    keys: set[tuple[str, str]] | None,
) -> None:
    """Refuse as the fold refuses (R4): its first failure among the broken keys, strictly.

    The fold takes its families in order -- thresholds by Claim identity,
    slots by their first member, the door's observations in one sequence
    across every Claim, then dependency sources -- and stops at the first
    failure, so the broken keys are derived again strictly in that order: the
    door's together, observation by observation (``rows.door_keys``). A key
    whose strict derivation now succeeds is applied in its place. A whole read
    also refuses a broken retired Claim, which the dependency fold reads with
    every Claim's facts.
    """

    from cruxible_core.service.discovery.next import DEFAULT_EXPIRING_WITHIN_MICROSECONDS

    broken = _broken_keys(connection, keys)
    if broken:
        context = rows.RowContext(
            instance,
            coordinate,
            connection,
            at=evaluation_time,
            expiring_within=CanonicalDuration(microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS),
        )
        context.strict = True
        derived = rows.RowsDelta()

        def of(family: str) -> list[str]:
            return sorted(
                (key for found, key in broken if found == family), key=lambda key: key.encode()
            )

        for key in of(rows.THRESHOLD):
            derived.keys[(rows.THRESHOLD, key)] = rows.threshold_key(context, key)
        for key in sorted(of(rows.SLOT), key=lambda slot: _slot_order(connection, slot)):
            derived.keys[(rows.SLOT, key)] = rows.derive_one(context, rows.SLOT, key)
        for key, result in rows.door_keys(context, of(rows.DOOR)).items():
            derived.keys[(rows.DOOR, key)] = result
        for key in of(rows.SOURCE):
            derived.keys[(rows.SOURCE, key)] = rows.source_key(context, key)
        rows.apply_dependents(connection, rows.apply(connection, derived))
    if keys is not None:
        return
    retired = connection.execute(_BROKEN_RETIRED).fetchone()
    if retired is None:
        return
    from cruxible_core.service.discovery.query import verify_retired_claim_adjudication
    from cruxible_core.service.evidence.evidence import service_evaluate_playbill_claim_verdict

    if connection.execute("SELECT 1 FROM claim_inputs LIMIT 1").fetchone() is None:
        # No Claim consumes an input: the fold checks only the retired adjudications.
        verify_retired_claim_adjudication(instance, coordinate=coordinate)
        return
    service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=str(retired[0]),
        evaluation_time=evaluation_time,
        at=AcceptedCoordinate.from_internal(coordinate),
    )


def _read_served(
    instance: Any,
    connection: sqlite3.Connection,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    expiring_within: CanonicalDuration,
    claims: tuple[str, ...] | None,
    citations: CitationRequest = None,
    backed: tuple[str, ...] = (),
) -> ServedRows:
    from cruxible_core.service.discovery.next import DEFAULT_EXPIRING_WITHIN_MICROSECONDS

    keys = None if claims is None else _selected_keys(connection, claims)
    if claims is not None:
        # A hold over a few Claims is decided over their slots' contenders too,
        # and with no hold on any of them nothing is read, as the fold decides.
        population = _with_contenders(connection, claims)
        holds = _served_holds(
            instance,
            connection,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
            named=population,
        )
        if not holds:
            return ServedRows(rows=(), holds=holds)
    _refuse_errors(
        instance, connection, coordinate=coordinate, evaluation_time=evaluation_time, keys=keys
    )
    found = rows.stored_rows(connection, keys)
    if expiring_within.microseconds != DEFAULT_EXPIRING_WITHIN_MICROSECONDS:
        found = _recomposed(
            instance,
            connection,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
            expiring_within=expiring_within,
            found=found,
            keys=keys,
        )
    items = tuple(item for _order, item in found)
    if claims is not None:
        return ServedRows(rows=items, holds=holds)
    holds = _served_holds(
        instance,
        connection,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        named=_named_in(items),
    )
    start = utc_microseconds(evaluation_time)
    assert start is not None
    kinds = tuple(
        (str(kind), int(live), str(first))
        for kind, live in connection.execute(_KIND_COUNTS).fetchall()
        for (first,) in (connection.execute(_FIRST_OF_KIND, (kind,)).fetchone(),)
    )
    mandates = tuple(
        str(row[0])
        for row in connection.execute(
            _EXPIRING_MANDATES, (start, start + expiring_within.microseconds)
        )
    )
    commitments, citation_error = _served_citations(connection, citations)
    return ServedRows(
        rows=items,
        holds=holds,
        subject_kinds=kinds,
        expiring_mandates=mandates,
        citations=commitments,
        citation_error=citation_error,
        backed=_served_backings(connection, backed) if backed else None,
    )


def _served_backings(
    connection: sqlite3.Connection, claims: tuple[str, ...]
) -> dict[str, tuple[str, bool] | None]:
    """Each live backed Claim's resolution status and projection visibility, from Layer 1.

    The facts hold at the read's instant, their statuses resolved over every
    contender of each slot, and ``visible`` is the projection visibility rule
    (the Subject accepted, the verdict and currency ones the policy shows), so
    a projection check takes them in place of deriving its backings' verdicts.
    A Claim whose facts or slot failed maps to None: the check derives it.
    """

    found: dict[str, tuple[str, bool] | None] = {}
    for claim_id, status, visible, error in _rows(
        connection,
        "SELECT claim_id,status,visible,error_code FROM claim_facts "
        "WHERE lifecycle='live' AND claim_id IN ({marks})",
        claims,
    ):
        found[str(claim_id)] = (
            None
            if error is not None or status is None or visible is None
            else (str(status), bool(visible))
        )
    return found


def _served_citations(
    connection: sqlite3.Connection, citations: CitationRequest
) -> tuple[dict[str, Any] | None, bool]:
    """The maintained commitments of these citation ids, or of every one naming a source.

    Layer 1 keeps them for exactly the Claims the fold's inventory reads (live,
    and visible at the instant the facts hold for), so a workspace read takes
    them from here, never from every Claim's envelopes. A source observation
    reads only the commitments naming a source, through the source index: each
    one is a row or a check against the observation, and a commitment naming
    none can be neither. A Claim whose citations could not be read refuses the
    inventory, as the fold does.
    """

    from cruxible_core.service.discovery.next import _CitationCommitment

    if citations is None:
        return None, False
    error = connection.execute(_CITATION_ERROR).fetchone() is not None
    columns = (
        "claim_id,citation_id,commitment_digest,byte_length,source_id,source_digest,"
        "original_start,original_end,whole_source,lineage_note,set_arguments"
    )
    found = (
        connection.execute(
            f"SELECT {columns} FROM citation_commitments INDEXED BY citation_commitments_by_source "
            "WHERE source_id IS NOT NULL AND source_digest IS NOT NULL"
        ).fetchall()
        if citations == "sources"
        else _rows(
            connection,
            f"SELECT {columns} FROM citation_commitments WHERE citation_id IN ({{marks}})",
            tuple(sorted(set(citations))),
        )
    )
    commitments: dict[str, Any] = {}
    for row in sorted(found, key=lambda item: (str(item[0]), str(item[1]))):
        (claim_id, citation_id, digest, length, source_id, source_digest) = row[:6]
        start, end, whole, note, arguments = row[6:]
        commitments[str(citation_id)] = _CitationCommitment(
            citation_id=str(citation_id),
            commitment_digest=str(digest),
            byte_length=int(length),
            claim_identity=str(claim_id),
            source_id=None if source_id is None else str(source_id),
            source_digest=None if source_digest is None else str(source_digest),
            original_start=start,
            original_end=end,
            whole_source=bool(whole),
            lineage_note=note,
            set_arguments=(
                None
                if arguments is None
                else tuple((str(key), value) for key, value in json.loads(arguments))
            ),
        )
    return commitments, error


def _recomposed(
    instance: Any,
    connection: sqlite3.Connection,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    expiring_within: CanonicalDuration,
    found: list[tuple[tuple[Any, ...], PlaybillNextItemV1]],
    keys: set[tuple[str, str]] | None,
) -> list[tuple[tuple[Any, ...], PlaybillNextItemV1]]:
    """Slot rows under a caller's own expiring lead: only the slots a lead can move.

    A member's ``evidence_expiring`` row depends on the lead only through its
    latest current support expiry (the row stands while that expiry is within
    the lead), so only slots holding a member whose latest expiry lies within
    the longer of the two leads are composed again, from the stored facts. No
    verdict is evaluated.
    """

    from cruxible_core.service.discovery.next import DEFAULT_EXPIRING_WITHIN_MICROSECONDS

    at_us = utc_microseconds(evaluation_time)
    assert at_us is not None
    reach = max(expiring_within.microseconds, DEFAULT_EXPIRING_WITHIN_MICROSECONDS)
    slots = {str(row[0]) for row in connection.execute(_EXPIRING_SLOTS, (at_us, at_us + reach))}
    if keys is not None:
        slots &= {key for family, key in keys if family == rows.SLOT}
    if not slots:
        return found
    context = rows.RowContext(
        instance, coordinate, connection, at=evaluation_time, expiring_within=expiring_within
    )
    firsts: set[str] = set()
    replaced: list[tuple[tuple[Any, ...], PlaybillNextItemV1]] = []
    for slot in sorted(slots):
        firsts.update(
            str(json.loads(row[0])[1])
            for row in connection.execute(
                "SELECT emit_order FROM claim_rows WHERE family=? AND key=?", (rows.SLOT, slot)
            )
        )
        result = rows.slot_rows(context, slot, expiring_within=expiring_within)
        if result is not None:
            replaced.extend(result.rows)
    slot_rank = rows.FAMILY_RANK[rows.SLOT]
    kept = [
        (order, item) for order, item in found if not (order[0] == slot_rank and order[1] in firsts)
    ]
    return sorted((*kept, *replaced), key=lambda pair: pair[0])


def _named_in(items: Iterable[PlaybillNextItemV1]) -> set[str]:
    """The Claims a hold over these rows decides by: each row's subject and its contenders."""

    from cruxible_core.service.discovery.next import _hold_members, _repair_arguments

    named: set[str] = set()
    for item in items:
        for row in _hold_members(item):
            named.add(row.subject_identity)
            contenders = _repair_arguments(row).get("claim_ids")
            if isinstance(contenders, list):
                named.update(str(contender) for contender in contenders)
    return named


def _with_contenders(connection: sqlite3.Connection, claims: Sequence[str]) -> set[str]:
    """These Claims and every live Claim of their (predicate, Subject) slots: one seek each."""

    found = set(claims)
    found.update(
        str(row[0])
        for row in _rows(
            connection,
            "SELECT claim_id FROM claim_facts WHERE lifecycle='live' AND res_slot IN "
            "(SELECT res_slot FROM claim_facts WHERE claim_id IN ({marks}))",
            tuple(sorted(set(claims))),
        )
    )
    return found


def _served_holds(
    instance: Any,
    connection: sqlite3.Connection,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    named: set[str],
) -> _Holds:
    """The ``unsure`` holds over these Claims: their examined statements, nothing else.

    A hold covers a row through the row's subject and its conflict's
    contenders, so only those Claims' accepted examined attestations and door
    examined events of their current versions are read. Reading every such
    door event decides each principal's latest statement exactly as the fold
    does from its folded events plus, when one postdates the instant, the
    whole chain.
    """

    from cruxible_core.service.discovery.next import _Holds
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    live = sorted(
        str(row[0])
        for row in _rows(
            connection,
            "SELECT claim_id FROM claim_facts WHERE lifecycle='live' AND claim_id IN ({marks})",
            tuple(sorted(name for name in named if name.startswith("Claim:"))),
        )
    )
    context = ClaimVerdictReadContext(instance, coordinate)
    claims = tuple(context.claim(identity) for identity in live)
    versions = tuple(
        (claim.identity.qualified, claim_artifact_digest(claim).tagged) for claim in claims
    )
    accepted: list[Any] = []
    digests: list[tuple[int, str]] = []
    with instance.bind_accepted_projection(coordinate) as projection:
        for start in range(0, len(versions), 400):
            chunk = versions[start : start + 400]
            accepted.extend(
                projection.typed.claim_attestations(basis="examined_existing", claim_versions=chunk)
            )
            digests.extend(
                (int(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT chain_pos,event_digest FROM door_events "
                    "WHERE basis='examined_existing' AND (claim_identity,claim_artifact_digest) "
                    "IN (VALUES " + ",".join("(?,?)" for _ in chunk) + ")",
                    tuple(value for pair in chunk for value in pair),
                )
            )
    events = instance.claim_attestation_evidence_store().event_pairs(
        digest for _position, digest in sorted(digests)
    )
    return _Holds(
        instance,
        coordinate=coordinate,
        claims=claims,
        door_events=tuple(events),
        evaluation_time=evaluation_time,
        accepted=tuple(accepted),
    )


def claim_status_tally(
    instance: Any, *, coordinate: AcceptedProjectionCoordinate, evaluation_time: datetime
) -> dict[str, int] | None:
    """Accepted Claims counted by status from the maintained tally, or None to derive live.

    Read in the same rolled-back transaction a served next read uses: what the
    worker has not yet followed is replayed from the overlay and every fact
    that does not hold at ``evaluation_time`` is derived again, so a worker
    behind or an instant past an interval costs the change, not the
    population. None only where next folds (cold, disabled, historical) or a
    live Claim's slot did not resolve; logs that keep moving refuse
    (``ReadInputsMovingError``), as a next read does.
    """

    served = _serving(
        instance,
        coordinate=coordinate,
        door_head=None,
        evaluation_time=evaluation_time,
        read=lambda connection: dict(connection.execute("SELECT status,count FROM tally")),
    )
    if served is None:
        return None
    counts, _heads_read = served
    if counts.get("error"):
        return None
    return {status: int(counts.get(status, 0)) for status in TALLY_STATUSES[:-1]}


def _dump(connection: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
        for table in TABLES
    }


def stored_tables(instance: Any) -> dict[str, list[tuple[Any, ...]]] | None:
    """Every stored Layer 1 and Layer 2 table, rows sorted: what a fresh rebuild must equal."""

    with _STATE.open(instance, create=False) as connection:
        return None if connection is None else _dump(connection)


def rebuilt_tables(instance: Any, *, at: datetime) -> dict[str, list[tuple[Any, ...]]]:
    """The tables a full rebuild at ``at`` writes, built apart from the stored state."""

    heads = _heads(instance)
    delta = _rebuild(instance, heads, at=at)
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        _settle(instance, connection, coordinate=heads.coordinate, at=at, delta=delta, rebuild=True)
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
            # The heads this pass reached are the target until matching sees new
            # ones; a target matched while the pass ran stays, and stays due.
            connection.execute(
                "UPDATE progress SET target=coalesce((SELECT heads FROM cursors),target) "
                "WHERE target=?",
                (work.item,),
            )
            connection.execute("UPDATE progress SET failed=NULL,last_error=NULL,last_error_at=NULL")

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        """Lag against the live heads, so a change no tick has matched yet shows at once."""

        with _STATE.open(instance, create=False) as connection:
            if connection is None:
                return ()
            progress = connection.execute("SELECT last_error FROM progress").fetchone()
            cursors = _read_cursors(connection)
        if progress is None:
            return ()
        (error,) = progress
        heads = _heads(instance)
        generation = None if cursors is None else cursors.generation
        behind = heads.generation - (0 if generation is None else generation)
        return (
            ConsumerHealth(
                kind=self.name,
                consumer_id="consumer:next",
                # A pending sweep is scheduled work, not lag: the evidence part
                # reports the sweep's own lag.
                state="stalled"
                if error is not None
                else "running"
                if cursors is not None
                and cursors.heads == replace(heads, sweep=cursors.sweep).key()
                else "lagging",
                detail={
                    "format": FORMAT,
                    "generation": generation,
                    "generations_behind": max(0, behind),
                    "attestations_behind": cursors is None or cursors.door_root != heads.door_root,
                    "inputs_changed": cursors is None
                    or (cursors.landing_log, cursors.landing, cursors.reobs_signature)
                    != (heads.landing_log, heads.landing, heads.reobs_signature),
                    "pending_expiry": cursors is None or cursors.expire != heads.expire,
                    "sweep_pending": cursors is None or cursors.sweep != heads.sweep,
                    "last_error": error,
                },
                repair=(
                    ConsumerRepair(
                        operation="hand_edit",
                        required_change=(
                            "resolve_the_worker_error_then_run_cruxible_instance_rebuild_next"
                        ),
                        arguments={},
                    )
                    if error is not None
                    else None
                ),
            ),
        )


_PART = ClaimFactsPart()


__all__ = [
    "FORMAT",
    "LAYER1_TABLES",
    "NEXT_EXPIRE",
    "TABLES",
    "TALLY_STATUSES",
    "ClaimFactsPart",
    "ServedRows",
    "catch_up",
    "claim_status_tally",
    "Rebuilt",
    "rebuild",
    "rebuilt_tables",
    "served_claim_rows",
    "stored_tables",
]
