"""Layer 2 of incremental next: each Claim row family's rows, kept per key.

next's Claim rows come from four families, each a function of a few keys'
inputs: a Claim's attestation threshold, a (predicate, qualifier, Subject)
slot's conflict and evidence rows, a Claim's new-capture door, and a dependent
Claim's stale backing inputs. Each key is derived by the very function the
full fold (``next``'s oracle) uses for it, from Layer 1's facts, and stored
with the half-open interval its answer holds on and the order the fold emits
it in (``emit_order``), so the stored rows, read in that order, are the rows
the fold would produce.

A Layer 1 change, a door event, a body landing or the passage of an interval
dirties only the keys that read it; a dependency source keeps the stale-input
entries it hands each dependent (``dependency_contributions``), so a source's
move rewrites only its dependents' rows. Every derivation here reads the
facts through one connection, so it sees Layer 1 exactly as the same
transaction is about to leave it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_verdicts import ClaimVerdictResultAny
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
)
from cruxible_client.contracts.errors import ClaimNotFoundError
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.indexes.typed_state import utc_microseconds

if TYPE_CHECKING:
    from cruxible_client.contracts.captures import CanonicalDuration
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.service.discovery.next import PlaybillNextItemV1
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

THRESHOLD = "threshold"
SLOT = "slot"
DOOR = "door"
SOURCE = "source"
DEPENDENCY = "dependency"
#: The order the fold emits its families in; within one, ``emit_order`` decides.
FAMILY_RANK = {THRESHOLD: 0, SLOT: 1, DOOR: 2, DEPENDENCY: 3}

SCHEMA = """
CREATE TABLE row_keys (
 family TEXT NOT NULL, key TEXT NOT NULL, lower_us INTEGER, upper_us INTEGER,
 error_code TEXT, error_detail TEXT,
 PRIMARY KEY(family,key)
) STRICT;
CREATE INDEX row_keys_by_upper ON row_keys(upper_us) WHERE upper_us IS NOT NULL;
CREATE INDEX row_keys_by_lower ON row_keys(lower_us) WHERE lower_us IS NOT NULL;
CREATE INDEX row_keys_with_errors ON row_keys(family,key) WHERE error_code IS NOT NULL;
CREATE TABLE claim_rows (
 family TEXT NOT NULL, key TEXT NOT NULL, ordinal INTEGER NOT NULL,
 emit_order TEXT NOT NULL, row_json TEXT NOT NULL, PRIMARY KEY(family,key,ordinal)
) STRICT;
CREATE TABLE row_reads (
 read_key TEXT NOT NULL, family TEXT NOT NULL, key TEXT NOT NULL,
 PRIMARY KEY(read_key,family,key)
) STRICT, WITHOUT ROWID;
CREATE INDEX row_reads_by_key ON row_reads(family,key);
CREATE TABLE dependency_contributions (
 source TEXT NOT NULL, dependent TEXT NOT NULL, ordinal INTEGER NOT NULL,
 entry_json TEXT NOT NULL, PRIMARY KEY(source,dependent,ordinal)
) STRICT;
CREATE INDEX contributions_by_dependent ON dependency_contributions(dependent,source);
"""
#: The Layer 2 tables, in the order a dump lists them.
TABLES = ("row_keys", "claim_rows", "row_reads", "dependency_contributions")

_VERDICTS: TypeAdapter[ClaimVerdictResultAny] = TypeAdapter(ClaimVerdictResultAny)
_FACT_COLUMNS = (
    "claim_id",
    "path",
    "artifact_digest",
    "lifecycle",
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
    "lower_us",
    "upper_us",
    "error_code",
)


def _json(value: object) -> str:
    return canonical_json(value)


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None) or getattr(exc, "error_code", None)
    return type(exc).__name__ if not isinstance(code, str) else f"{type(exc).__name__}:{code}"


def _intersect(
    intervals: Iterable[tuple[int | None, int | None]],
) -> tuple[int | None, int | None]:
    lower: int | None = None
    upper: int | None = None
    for low, high in intervals:
        if low is not None and (lower is None or low > lower):
            lower = low
        if high is not None and (upper is None or high < upper):
            upper = high
    return lower, upper


@dataclass
class KeyRows:
    """One key's derived rows, the interval they hold on, and what they read."""

    family: str
    key: str
    rows: list[tuple[tuple[Any, ...], PlaybillNextItemV1]] = field(default_factory=list)
    lower: int | None = None
    upper: int | None = None
    reads: set[str] = field(default_factory=set)
    error: tuple[str, str] | None = None
    #: A dependency source's stale-input entries, by dependent.
    contributions: list[tuple[str, dict[str, object]]] = field(default_factory=list)


@dataclass
class RowsDelta:
    """The keys one pass derived again; ``None`` deletes a key."""

    keys: dict[tuple[str, str], KeyRows | None] = field(default_factory=dict)


class RowContext:
    """What one pass's derivations share: the coordinate, the instant and the facts."""

    def __init__(
        self,
        instance: Any,
        coordinate: AcceptedProjectionCoordinate,
        connection: sqlite3.Connection,
        *,
        at: datetime,
        expiring_within: CanonicalDuration,
    ) -> None:
        from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

        self.instance = instance
        self.coordinate = coordinate
        self.public = AcceptedCoordinate.from_internal(coordinate)
        self.connection = connection
        self.at = at
        self.at_us = utc_microseconds(at)
        self.expiring_within = expiring_within
        self.reads: ClaimVerdictReadContext = ClaimVerdictReadContext(instance, coordinate)
        self.threshold_types: dict[str, Any] = {}
        self._threshold_law: Mapping[str, Any] | None = None
        self.set_types: dict[str, Any] = {}
        #: Raise a derivation's error instead of recording it (a read refusing as the fold does).
        self.strict = False
        self._facts: dict[str, dict[str, Any] | None] = {}

    # -- facts --------------------------------------------------------------

    def facts(self, claim_ids: Iterable[str]) -> dict[str, dict[str, Any] | None]:
        claim_ids = tuple(claim_ids)
        wanted = sorted({claim_id for claim_id in claim_ids if claim_id not in self._facts})
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            found = {
                str(row[0]): dict(zip(_FACT_COLUMNS, row, strict=True))
                for row in self.connection.execute(
                    f"SELECT {','.join(_FACT_COLUMNS)} FROM claim_facts "
                    f"WHERE claim_id IN ({','.join('?' for _ in chunk)})",
                    chunk,
                )
            }
            for claim_id in chunk:
                self._facts[claim_id] = found.get(claim_id)
        return {claim_id: self._facts[claim_id] for claim_id in claim_ids}

    def fact(self, claim_id: str) -> dict[str, Any] | None:
        return self.facts((claim_id,))[claim_id]

    def verdict(self, fact: Mapping[str, Any] | None) -> ClaimVerdictResultAny | None:
        if fact is None or fact["verdict_json"] is None:
            return None
        return _VERDICTS.validate_python(
            {**json.loads(fact["verdict_json"]), "evaluation_time": self.at}
        )

    def claim(self, claim_id: str) -> ClaimArtifactAny | None:
        try:
            return self.reads.claim(claim_id)
        except ClaimNotFoundError:
            return None

    def interval(self, fact: Mapping[str, Any] | None) -> tuple[int | None, int | None]:
        return (None, None) if fact is None else (fact["lower_us"], fact["upper_us"])

    def claims_by(self, table: str, column: str, values: Iterable[str]) -> set[str]:
        wanted = sorted(set(values))
        found: set[str] = set()
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            found.update(
                str(row[0])
                for row in self.connection.execute(
                    f"SELECT claim_id FROM {table} "
                    f"WHERE {column} IN ({','.join('?' for _ in chunk)})",
                    chunk,
                )
            )
        return found

    def slot_members(self, res_slot: str) -> list[ClaimArtifactAny]:
        """The live Claims of one (predicate, Subject) slot at the coordinate: one seek."""

        key = json.loads(res_slot)
        subject = SemanticAddress.model_validate(key["subject"])
        with self.instance.bind_accepted_projection(self.coordinate) as projection:
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
        members = [self.reads.claim(identity) for identity in identities]
        # The index only locates rows; the parsed statement is the authority.
        return [claim for claim in members if slot_key(claim, qualifier=False) == res_slot]

    def threshold_law(self) -> Mapping[str, Any]:
        """The lazy law-evidence map the threshold fold reads, through the fold's own accessor."""

        from cruxible_core.service.discovery.next import _claim_threshold_evidence

        if self._threshold_law is None:
            self._threshold_law = _claim_threshold_evidence(self.instance, at=self.coordinate)
        return self._threshold_law

    # -- the door -----------------------------------------------------------

    def door_events(
        self, claim_id: str, *, basis: str, artifact_digest: str | None = None
    ) -> list[tuple[int, Any, Any]]:
        """This Claim's door events of one basis (of one version), in chain order."""

        rows = self.connection.execute(
            "SELECT chain_pos,event_digest FROM door_events WHERE claim_identity=? AND basis=?"
            + ("" if artifact_digest is None else " AND claim_artifact_digest=?")
            + " ORDER BY chain_pos",
            (claim_id, basis) if artifact_digest is None else (claim_id, basis, artifact_digest),
        ).fetchall()
        if not rows:
            return []
        pairs = self.instance.claim_attestation_evidence_store().event_pairs(
            str(row[1]) for row in rows
        )
        return [
            (int(row[0]), event, payload) for row, (event, payload) in zip(rows, pairs, strict=True)
        ]


def slot_key(claim: ClaimArtifactAny, *, qualifier: bool) -> str:
    key: dict[str, object] = {
        "predicate": claim.statement.predicate,
        "subject": claim.statement.subject.model_dump(mode="json"),
    }
    if qualifier:
        key["qualifier"] = claim.statement.qualifier
    return canonical_bytes(key).decode("utf-8")


def res_slot_of(conflict_slot: str) -> str:
    key = json.loads(conflict_slot)
    key.pop("qualifier")
    return canonical_bytes(key).decode("utf-8")


# -- the families -------------------------------------------------------------


def threshold_key(context: RowContext, claim_id: str) -> KeyRows | None:
    """One live Claim's attestation-threshold rows, rule by rule."""

    from cruxible_core.service.discovery.next import _claim_threshold_rows

    claim = context.claim(claim_id)
    if claim is None or claim.lifecycle.state != "live":
        return None
    result = KeyRows(THRESHOLD, claim_id)
    boundaries: set[datetime] = set()
    try:
        events = context.door_events(
            claim_id,
            basis="examined_existing",
            artifact_digest=claim_artifact_digest(claim).tagged,
        )
        rows = _claim_threshold_rows(
            context.instance,
            accepted=context.coordinate,
            tree=context.reads.tree,
            claim=claim,
            law_evidence=context.threshold_law(),
            claim_types=context.threshold_types,
            evaluation_time=context.at,
            door_events=[(event, payload) for _position, event, payload in events],
            time_boundaries=boundaries,
        )
    except Exception as exc:  # noqa: BLE001 - recorded, and raised again on read
        if context.strict:
            raise
        result.error = (_error_code(exc), str(exc))
        return result
    if not rows and not boundaries:
        return None
    result.rows = [
        ((FAMILY_RANK[THRESHOLD], claim_id, ordinal), row) for ordinal, row in enumerate(rows)
    ]
    result.lower, result.upper = _boundary_interval(boundaries, context.at)
    return result


def slot_rows(
    context: RowContext, conflict_slot: str, *, expiring_within: CanonicalDuration
) -> KeyRows | None:
    """One (predicate, qualifier, Subject) slot's rows: its conflict, else its members'."""

    from cruxible_core.service.discovery.next import _claim_slot_rows

    res_slot = res_slot_of(conflict_slot)
    contenders = context.slot_members(res_slot)
    members = [claim for claim in contenders if slot_key(claim, qualifier=True) == conflict_slot]
    if not members:
        return None
    facts = context.facts(claim.identity.qualified for claim in contenders)
    result = KeyRows(SLOT, conflict_slot)
    verdicts = {
        claim.identity.qualified: verdict
        for claim in members
        if (verdict := context.verdict(facts[claim.identity.qualified])) is not None
    }
    statuses = {
        claim.identity.name: fact["status"]
        for claim in members
        if (fact := facts[claim.identity.qualified]) is not None
    }
    boundaries: set[datetime] = set()
    try:
        rows = _claim_slot_rows(
            context.instance,
            coordinate=context.public,
            internal=context.coordinate,
            group=members,
            resolution_statuses=statuses,
            verdicts_by_identity=verdicts,
            evaluation_time=context.at,
            expiring_within=expiring_within,
            time_boundaries=boundaries,
            claim_types=context.set_types,
        )
    except Exception as exc:  # noqa: BLE001 - recorded, and raised again on read
        if context.strict:
            raise
        result.error = (_error_code(exc), str(exc))
        return result
    first = members[0].identity.qualified
    result.rows = [((FAMILY_RANK[SLOT], first, ordinal), row) for ordinal, row in enumerate(rows)]
    # Its statuses rest on every contender's verdict in the (predicate, Subject) slot.
    result.lower, result.upper = _intersect(
        (
            _boundary_interval(boundaries, context.at),
            *(context.interval(fact) for fact in facts.values()),
        )
    )
    return result


def door_key(context: RowContext, claim_id: str) -> KeyRows | None:
    """One Claim's new-capture door rows, in the fold's observation order."""

    return door_keys(context, (claim_id,))[claim_id]


@dataclass
class _DoorPlan:
    """One Claim's door derivation in progress: its fold and what it has produced."""

    result: KeyRows
    fold: Any
    boundaries: set[datetime] = field(default_factory=set)


def door_keys(context: RowContext, claim_ids: Iterable[str]) -> dict[str, KeyRows | None]:
    """These Claims' door rows, their observations taken in the fold's one global order.

    The fold takes every new-capture observation in a single sequence: the door
    events in chain order, then the accepted attestations no event carries, by
    attestation instant and envelope digest. Each Claim keeps its own rows and
    reads, exactly as a key derived alone; taking the observations of several
    Claims together, in that sequence, means a strict derivation fails at the
    observation the fold first fails at, however the Claims interleave.
    """

    from cruxible_client.contracts.claim_attestations import claim_attestation_v2_envelope_digest
    from cruxible_core.service.claims.claims import claim_law_evidence_by_artifact_for_path
    from cruxible_core.service.discovery.next import _DoorFold

    found: dict[str, KeyRows | None] = {}
    plans: dict[str, _DoorPlan] = {}
    observations: list[tuple[tuple[Any, ...], str, Any, str | None, bool | None]] = []
    for claim_id in sorted(set(claim_ids)):
        result = KeyRows(DOOR, claim_id)
        try:
            events = context.door_events(claim_id, basis="new_capture")
            with context.instance.bind_accepted_projection(context.coordinate) as projection:
                accepted = projection.typed.claim_attestations(
                    basis="new_capture", claim_identities=(claim_id,)
                )
            seen = {
                claim_attestation_v2_envelope_digest(payload.attestation)
                for _position, _event, payload in events
            }
            mine: list[tuple[tuple[Any, ...], str, Any, str | None, bool | None]] = [
                (
                    (0, position),
                    claim_id,
                    payload.attestation,
                    event.event_digest,
                    payload.current_at_append,
                )
                for position, event, payload in events
            ]
            mine.extend(
                (
                    (
                        1,
                        utc_microseconds(envelope.statement.attested_at),
                        claim_attestation_v2_envelope_digest(envelope),
                    ),
                    claim_id,
                    envelope,
                    None,
                    None,
                )
                for envelope in accepted
                if claim_attestation_v2_envelope_digest(envelope) not in seen
            )
            if not mine:
                found[claim_id] = None
                continue
            fold = _DoorFold(
                context.instance,
                context.coordinate,
                law_by_artifact=partial(
                    claim_law_evidence_by_artifact_for_path,
                    context.instance,
                    at=context.coordinate,
                    path=claim_path(claim_id.removeprefix("Claim:")),
                ),
                claim_types=context.set_types,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, and raised again on read
            if context.strict:
                raise
            result.error = (_error_code(exc), str(exc))
            found[claim_id] = result
            continue
        plans[claim_id] = _DoorPlan(result, fold)
        observations.extend(mine)
    for order, claim_id, envelope, event_digest, current_at_append in sorted(
        observations, key=lambda observation: observation[0]
    ):
        plan = plans[claim_id]
        if plan.result.error is not None:
            continue
        try:
            rows = plan.fold.rows(
                envelope,
                event_digest=event_digest,
                current_at_append=current_at_append,
                evaluation_time=context.at,
                time_boundaries=plan.boundaries,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, and raised again on read
            if context.strict:
                raise
            plan.result.rows = []
            plan.result.error = (_error_code(exc), str(exc))
            continue
        plan.result.rows.extend(
            ((FAMILY_RANK[DOOR], *order, ordinal), row) for ordinal, row in enumerate(rows)
        )
        plan.result.reads.update(
            f"capture:{digest}" for digest in envelope.statement.cited_capture_digests
        )
    for claim_id, plan in plans.items():
        if plan.result.error is None:
            plan.result.reads.update(
                f"capture:{digest}" for digest in plan.fold.read_captures(claim_id)
            )
            plan.result.lower, plan.result.upper = _boundary_interval(plan.boundaries, context.at)
        found[claim_id] = plan.result
    return found


def source_key(context: RowContext, claim_id: str) -> KeyRows | None:
    """One dependency source's stale-input entries for each dependent it keeps.

    The impact walker's dependents of this source, budgeted exactly as it
    budgets them, read from the facts of the source and of every Claim that
    names it: by a backing input in its lineage, or by a pin.
    """

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.query.grammar import byte_sorted
    from cruxible_core.query.impact import (
        DependencyImpactBudgetV1,
        DependencyImpactSourceV1,
        _dependent,
        _dependent_order,
        _matching_pins,
        budgeted_dependents,
    )
    from cruxible_core.service.discovery.next import stale_input_entries

    source_fact = context.fact(claim_id)
    if source_fact is None or source_fact["lineage_digests"] is None:
        return None
    lineage = tuple(json.loads(source_fact["lineage_digests"]))
    candidates = (
        context.claims_by("claim_inputs", "digest", lineage)
        | context.claims_by("claim_pins", "target", (claim_id,))
    ) - {claim_id}
    if not candidates:
        return None
    facts = context.facts(candidates)
    result = KeyRows(SOURCE, claim_id)
    result.lower, result.upper = _intersect(
        (context.interval(source_fact), *(context.interval(fact) for fact in facts.values()))
    )
    if source_fact["visible"] != 1 or source_fact["error_code"] is not None:
        return result
    source_claim = context.claim(claim_id)
    if source_claim is None:
        return result
    identity = source_claim.identity
    path = claim_path(identity.name)
    searched = set(lineage)
    source = DependencyImpactSourceV1(
        address=SemanticAddress.claim_statement(path),
        claim_path=path,
        identity=identity.qualified,
        statement_digest=claim_statement_digest(source_claim.statement).tagged,
        accepted_artifact_digest=claim_artifact_digest(source_claim).tagged,
        lifecycle_state=source_claim.lifecycle.state,
        predecessor_digest=source_claim.lifecycle.predecessor_digest,
        searched_artifact_digests=byte_sorted(lineage),
        verdict=str(source_fact["verdict"]),
        currency=str(source_fact["currency"]),
    )
    pinned = ArtifactIdentity(kind=identity.kind, name=identity.name)
    found = []
    for candidate_id in sorted(candidates, key=lambda item: item.encode("utf-8")):
        fact = facts[candidate_id]
        if fact is None or fact["lifecycle"] != "live" or fact["visible"] != 1:
            continue
        claim = context.claim(candidate_id)
        if claim is None:
            continue
        used_inputs = byte_sorted(tuple(searched.intersection(claim.backing.input_claim_digests)))
        pins = _matching_pins(claim.pins, pinned)
        if not used_inputs and not pins:
            continue
        address = SemanticAddress.claim_statement(str(fact["path"]))
        for digest in used_inputs:
            found.append(
                _dependent(
                    kind="Claim",
                    address=address,
                    identity=claim.identity.qualified,
                    artifact_digest=str(fact["artifact_digest"]),
                    dependency_kind="backing_input",
                    used_pin_role=None,
                    source=source,
                    used_artifact_digest=digest,
                    dependent_verdict=fact["verdict"],
                    dependent_currency=fact["currency"],
                )
            )
        for pin in pins:
            found.append(
                _dependent(
                    kind="Claim",
                    address=address,
                    identity=claim.identity.qualified,
                    artifact_digest=str(fact["artifact_digest"]),
                    dependency_kind="pin",
                    used_pin_role=pin.role,
                    source=source,
                    used_artifact_digest=pin.artifact_digest,
                    dependent_verdict=fact["verdict"],
                    dependent_currency=fact["currency"],
                )
            )
    kept, _truncated, _reasons = budgeted_dependents(
        tuple(sorted(found, key=_dependent_order)), DependencyImpactBudgetV1()
    )
    result.contributions = stale_input_entries(
        identity.qualified, kept, lineage_complete=bool(source_fact["lineage_complete"])
    )
    return result


def dependency_rows(connection: sqlite3.Connection, dependent: str) -> KeyRows | None:
    """One dependent's stale-dependency row, from every source's stored entries."""

    from cruxible_core.service.discovery.next import claim_dependency_row

    entries = [
        json.loads(row[0])
        for row in connection.execute(
            "SELECT entry_json FROM dependency_contributions WHERE dependent=? "
            "ORDER BY source,ordinal",
            (dependent,),
        )
    ]
    if not entries:
        return None
    result = KeyRows(DEPENDENCY, dependent)
    result.rows = [
        ((FAMILY_RANK[DEPENDENCY], dependent, 0), claim_dependency_row(dependent, entries))
    ]
    return result


def _boundary_interval(boundaries: set[datetime], at: datetime) -> tuple[int | None, int | None]:
    from cruxible_core.service.claims.verdict_memo import invariance_interval

    lower, upper = invariance_interval(boundaries, evaluation_time=at)
    return utc_microseconds(lower), utc_microseconds(upper)


def derive_one(context: RowContext, family: str, key: str) -> KeyRows | None:
    """One key's rows, by its family's derivation."""

    if family == THRESHOLD:
        return threshold_key(context, key)
    if family == SLOT:
        return slot_rows(context, key, expiring_within=context.expiring_within)
    if family == DOOR:
        return door_key(context, key)
    if family == SOURCE:
        return source_key(context, key)
    raise ValueError(f"no derivation for row family {family}")


def derive(context: RowContext, keys: Iterable[tuple[str, str]], delta: RowsDelta) -> None:
    """Derive these keys again into ``delta``, in any order: each reads only Layer 1.

    Dependent rows are not derived here; ``apply`` reports the dependents whose
    sources' entries moved, for ``apply_dependents``.
    """

    for family, key in sorted(set(keys)):
        if family != DEPENDENCY:
            delta.keys[(family, key)] = derive_one(context, family, key)


def apply(connection: sqlite3.Connection, delta: RowsDelta) -> set[str]:
    """Write derived keys; returns the dependents whose sources' entries changed."""

    dependents: set[str] = set()
    for (family, key), result in sorted(delta.keys.items()):
        if family == DEPENDENCY:
            continue
        if family == SOURCE:
            before = {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT dependent FROM dependency_contributions WHERE source=?",
                    (key,),
                )
            }
            previous = connection.execute(
                "SELECT dependent,ordinal,entry_json FROM dependency_contributions WHERE source=? "
                "ORDER BY dependent,ordinal",
                (key,),
            ).fetchall()
            connection.execute("DELETE FROM dependency_contributions WHERE source=?", (key,))
            rows = _contribution_rows(key, () if result is None else result.contributions)
            connection.executemany("INSERT INTO dependency_contributions VALUES (?,?,?,?)", rows)
            if [tuple(row) for row in previous] != [row[1:] for row in rows]:
                dependents |= before | {str(row[1]) for row in rows}
        connection.execute("DELETE FROM claim_rows WHERE family=? AND key=?", (family, key))
        connection.execute("DELETE FROM row_reads WHERE family=? AND key=?", (family, key))
        if result is None:
            connection.execute("DELETE FROM row_keys WHERE family=? AND key=?", (family, key))
            continue
        _write_key(connection, result)
    return dependents


def apply_dependents(connection: sqlite3.Connection, dependents: Iterable[str]) -> None:
    """Rewrite each dependent's row from the entries every source now holds for it."""

    for dependent in sorted(set(dependents)):
        connection.execute(
            "DELETE FROM claim_rows WHERE family=? AND key=?", (DEPENDENCY, dependent)
        )
        connection.execute("DELETE FROM row_keys WHERE family=? AND key=?", (DEPENDENCY, dependent))
        result = dependency_rows(connection, dependent)
        if result is not None:
            _write_key(connection, result)


def _contribution_rows(
    source: str, contributions: Sequence[tuple[str, dict[str, object]]]
) -> list[tuple[str, str, int, str]]:
    ordinals: dict[str, int] = {}
    rows = []
    for dependent, entry in contributions:
        ordinal = ordinals.get(dependent, 0)
        ordinals[dependent] = ordinal + 1
        rows.append((source, dependent, ordinal, _json(entry)))
    return sorted(rows, key=lambda row: (row[1], row[2]))


def _write_key(connection: sqlite3.Connection, result: KeyRows) -> None:
    connection.execute(
        "INSERT INTO row_keys VALUES (?,?,?,?,?,?) ON CONFLICT(family,key) DO UPDATE SET "
        "lower_us=excluded.lower_us,upper_us=excluded.upper_us,"
        "error_code=excluded.error_code,error_detail=excluded.error_detail",
        (
            result.family,
            result.key,
            result.lower,
            result.upper,
            None if result.error is None else result.error[0],
            None if result.error is None else result.error[1],
        ),
    )
    connection.executemany(
        "INSERT INTO claim_rows VALUES (?,?,?,?,?)",
        (
            (result.family, result.key, ordinal, _json(list(order)), row.model_dump_json())
            for ordinal, (order, row) in enumerate(result.rows)
        ),
    )
    connection.executemany(
        "INSERT INTO row_reads VALUES (?,?,?)",
        ((read, result.family, result.key) for read in sorted(result.reads)),
    )


def _items(rows: Iterable[tuple[str, str]]) -> list[tuple[tuple[Any, ...], PlaybillNextItemV1]]:
    from cruxible_core.service.discovery.next import PlaybillNextItemV1

    return [
        (tuple(json.loads(order)), PlaybillNextItemV1.model_validate_json(row_json))
        for order, row_json in rows
    ]


def stored_rows(
    connection: sqlite3.Connection, keys: Iterable[tuple[str, str]] | None = None
) -> list[tuple[tuple[Any, ...], PlaybillNextItemV1]]:
    """Stored rows (of these keys, or all), in the fold's emit order."""

    if keys is None:
        found = _items(connection.execute("SELECT emit_order,row_json FROM claim_rows"))
    else:
        found = []
        for family, key in sorted(set(keys)):
            found.extend(
                _items(
                    connection.execute(
                        "SELECT emit_order,row_json FROM claim_rows WHERE family=? AND key=?",
                        (family, key),
                    )
                )
            )
    return sorted(found, key=lambda item: item[0])


__all__ = [
    "DEPENDENCY",
    "DOOR",
    "FAMILY_RANK",
    "SCHEMA",
    "SLOT",
    "SOURCE",
    "TABLES",
    "THRESHOLD",
    "KeyRows",
    "RowContext",
    "RowsDelta",
    "apply",
    "apply_dependents",
    "derive",
    "derive_one",
    "door_keys",
    "res_slot_of",
    "slot_key",
    "stored_rows",
]
