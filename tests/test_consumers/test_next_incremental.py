"""The randomized oracle for incremental next: every op, every read surface, byte for byte.

A seeded RNG drives a world through the design's ops: a Claim revised, a
contender in the same slot, a retirement; a ClaimType's freshness, threshold
policy or cardinality revised; a new Subject; a door attestation (examined
support, contradict or unsure, or new-capture with its Capture landing); an
accepted attestation member; a cited body erased and restored, an uncited
body stored; a re-observation; the read instant moved to a stored bound
-1us, the bound or +1us with ``next.expire`` fired; a body rewritten in place
and the sweep that finds it (then its repair and the next sweep); a restart
(every per-process memo and overlay gone). After each op:

- with the worker behind (no tick yet) and again caught up, each surface and
  tier (CLI, SDK, MCP), at the default and a custom ``expiring_within``,
  answers exactly as the full fold (``model_dump_json`` and
  ``result_digest``), as do the summary and ``claim_unsure_holds``; a
  refusal compares by class and code (R4); a read at the previous
  coordinate folds;
- the catching-up pass derives exactly the Claims the change records reach,
  resolves exactly the slots whose inputs moved, and derives exactly the
  Layer 2 keys of the expected dirty set, each computed from the records and
  the tables as they stood before it;
- every stored table equals a rebuild from nothing at the same instant.

CI runs 20 seeds x 25 steps on fixed instants; no wall clock enters.
``CRUXIBLE_NEXT_ORACLE_STEPS`` runs longer locally.
"""

from __future__ import annotations

import json
import os
import random
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from cruxible_client.contracts.accepted_attestations import (
    attestation_path,
    render_accepted_attestation,
)
from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.captures import (
    CanonicalDuration,
    DirectForeignSourceSelection,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_attestations import claim_attestation_v2_envelope_digest
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicy,
    ClaimAttestationConsequenceRule,
    ClaimEvidenceFreshness,
    ClaimFreshnessDuration,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import claim_artifact_digest, claim_statement_digest
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_client.contracts.triggers import CadenceSchedule
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDispositionV1,
    ClaimTypeMigrationRequestV1,
    service_migrate_claim_type,
)
from cruxible_core.consumers.next import facts, rows
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.indexes.typed_state import utc_microseconds
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.discovery.claim_status import reset_claim_resolution_memo
from cruxible_core.service.discovery.next import (
    NextRequest,
    claim_unsure_holds,
    service_playbill_next,
    summarize_playbill_next,
)
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.triggers.journal import InternalTrigger, evaluate_triggers, latest_sequence
from tests.core_support._claim_authoring_support import (
    DirectClaimAuthoringV1,
    ExistingStatementHandoffV1,
    _activate_direct_claim,
    service_propose_playbill_claim,
    service_reobserve_playbill_claim,
)
from tests.core_support._knowledge_loop_support import (
    SUBJECT_KIND,
    activate,
    authoring,
    seed_claims,
    subject_shell,
)
from tests.core_support._retirement_support import retire_claim
from tests.test_authoring.test_derivation_admission import (
    _run_and_accept,
    world,  # noqa: F401
)
from tests.test_consumers.test_next_facts import (
    _counted,
    _expected_claims,
    _expected_slots,
    _Snapshot,
    _snapshot,
)
from tests.test_evidence.test_evidence_freshness import _activate as _activate_migration
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_integration.test_next_closed_loop import _access
from tests.test_integration.test_next_holds import _all_claims, _attest

SEEDS = tuple(range(20))
STEPS = int(os.environ.get("CRUXIBLE_NEXT_ORACLE_STEPS", "25"))
START = datetime(2026, 8, 16, 20, 0, 5, tzinfo=UTC)
SOURCE = "fixture.work-items"
STATUS = "project.work_item.status"
_READER = BodyAccessContext(principal_id="next-oracle", can_read_body=True)
_LEADS: tuple[CanonicalDuration | None, ...] = (
    None,
    CanonicalDuration(microseconds=86_400_000_000),
)
_SURFACES: tuple[tuple[Any, int | None], ...] = ((None, None), ("sdk", 0), ("mcp", 1))
_FACT = {column: index for index, column in enumerate(facts._FACT_COLUMNS)}


@dataclass
class World:
    instance: Any
    owner: Any
    root: Path
    rng: random.Random
    at: datetime = START
    clock: int = 0
    #: A cited body erased by an op, with its bytes: the next op stores it again.
    erased: dict[str, bytes] = field(default_factory=dict)
    #: A body rewritten in place, with its bytes: the next op repairs it.
    rewritten: tuple[Path, bytes] | None = None
    #: The source bytes and span each Claim last cited, for a re-observation of the same.
    sources: dict[str, tuple[bytes, int, int]] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    def timestamp(self) -> str:
        self.clock += 1
        instant = datetime(2026, 8, 16, 20, 1, tzinfo=UTC) + timedelta(seconds=self.clock)
        return instant.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


# -- reading --------------------------------------------------------------------------


@contextmanager
def _oracle_only() -> Iterator[None]:
    with patch.object(facts, "served_claim_rows", return_value=None):
        yield


def _outcome(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except CruxibleError as exc:  # R4: a refusal is its class and code
        return (type(exc).__name__, getattr(exc, "code", None) or getattr(exc, "error_code", None))


def _reads(state: World, *, at: Any = None) -> list[Any]:
    """Every read surface's answer, in a fixed order."""

    instance = state.instance
    coordinate = None if at is None else AcceptedCoordinate.from_internal(at)
    answers: list[Any] = []
    for lead in _LEADS:
        extra: dict[str, Any] = {} if lead is None else {"expiring_within": lead}
        for surface, rung in _SURFACES:
            request = NextRequest(
                evaluation_time=state.at,
                access_profile=_access(),
                caller_surface=surface,
                at=coordinate,
                **extra,
            )

            def read(request: NextRequest = request, rung: int | None = rung) -> Any:
                result = service_playbill_next(instance, request=request, caller_rung=rung)
                return (result.model_dump_json(), result.result_digest)

            answers.append(_outcome(read))
        if at is None:
            summary = NextRequest(evaluation_time=state.at, access_profile=_access(), **extra)
            answers.append(
                _outcome(lambda summary=summary: summarize_playbill_next(instance, request=summary))
            )
    if at is None:
        claims = _all_claims(instance)
        answers.append(
            _outcome(
                lambda: claim_unsure_holds(
                    instance,
                    coordinate=instance.accepted_coordinate(),
                    claims=claims,
                    evaluation_time=state.at,
                )
            )
        )
    return answers


def _assert_answers_equal(state: World, label: str, *, at: Any = None) -> None:
    before = facts.stored_tables(state.instance)
    # A first read settles other parts' read-side bookkeeping; compare after it.
    _outcome(
        lambda: service_playbill_next(
            state.instance,
            request=NextRequest(evaluation_time=state.at, access_profile=_access()),
        )
    )
    incremental = _reads(state, at=at)
    with _oracle_only():
        oracle = _reads(state, at=at)
    for index, (got, expected) in enumerate(zip(incremental, oracle, strict=True)):
        assert got == expected, f"{label}: read {index} differs after {state.log}"
    # Reads never write the worker's state (R3).
    assert facts.stored_tables(state.instance) == before, f"{label}: a read wrote state"


# -- the worker, with its work counted ----------------------------------------------------


@dataclass
class _Work:
    claims: Counter[str] = field(default_factory=Counter)
    slots: Counter[str] = field(default_factory=Counter)
    keys: set[tuple[str, str]] = field(default_factory=set)


def _tick(state: World) -> _Work:
    work = _Work()
    part = facts._PART
    manager = SimpleNamespace(get=lambda _id: state.instance)
    original = rows.derive

    def derive(context, keys, delta):  # type: ignore[no-untyped-def]
        keys = set(keys)
        work.keys |= keys
        return original(context, keys, delta)

    with _counted() as counted, patch.object(rows, "derive", derive):
        part.match(state.instance, now=state.at, daemon_id="oracle")
        for unit in part.due(state.instance, now=state.at):
            part.run(manager, "instance", unit, now=state.at)
    work.claims = counted.claims
    work.slots = counted.slots
    return work


def _rewritten_claims(state: World, before: _Snapshot) -> set[str]:
    """The Claims a sweep reaches: those that consulted a body whose identity moved."""

    store = state.instance.body_store()
    recorded: dict[str, set[Any]] = {}
    consulted: dict[str, set[str]] = {}
    for body, _capture_digest, claim_id, identity in before.tables["capture_bodies"]:
        recorded.setdefault(body, set()).add(identity)
        consulted.setdefault(body, set()).add(claim_id)
    found: set[str] = set()
    for body, identities in recorded.items():
        current = store.file_identity(body)
        if identities != {None if current is None else facts._json(list(current))}:
            found |= consulted[body]
    return found


def _expected_keys(
    state: World,
    before: _Snapshot,
    after: dict[str, list[tuple[Any, ...]]],
    claims: set[str],
) -> set[tuple[str, str]]:
    """The Layer 2 keys a pass must derive, from the tables before it and the records since."""

    old = {str(row[0]): row for row in before.tables["claim_facts"]}
    new = {str(row[0]): row for row in after["claim_facts"]}
    keys: set[tuple[str, str]] = set()
    digests: set[str] = set()
    targets: set[str] = set()
    for claim_id in claims:
        keys |= {(rows.THRESHOLD, claim_id), (rows.DOOR, claim_id), (rows.SOURCE, claim_id)}
        for side in (old.get(claim_id), new.get(claim_id)):
            if side is None:
                continue
            if side[_FACT["lifecycle"]] == "live":
                keys.add((rows.SLOT, str(side[_FACT["conflict_slot"]])))
            digests.update(json.loads(side[_FACT["input_digests"]]))
            targets.update(json.loads(side[_FACT["pin_targets"]]))
    for claim_id in set(old) | set(new):
        status_before = None if claim_id not in old else old[claim_id][_FACT["status"]]
        side = new.get(claim_id)
        status_after = None if side is None else side[_FACT["status"]]
        if status_before != status_after and side is not None:
            if side[_FACT["lifecycle"]] == "live":
                keys.add((rows.SLOT, str(side[_FACT["conflict_slot"]])))
    _head, events = state.instance.claim_attestation_evidence_store().events_after(before.door)
    for _position, _event, payload in events:
        statement = payload.attestation.statement
        family = rows.THRESHOLD if statement.attestation_basis == "examined_existing" else rows.DOOR
        keys.add((family, statement.claim_identity.qualified))
    landed = {
        f"capture:{row.digest}"
        for row in state.instance.body_store().landings.after(before.landing)
    }
    keys |= {(family, key) for read, family, key in before.tables["row_reads"] if read in landed}
    keys |= {
        (rows.SOURCE, str(claim_id))
        for digest, claim_id in after["claim_lineage"]
        if digest in digests
    }
    keys |= {(rows.SOURCE, target) for target in targets if target.startswith("Claim:")}
    at_us = utc_microseconds(state.at)
    assert at_us is not None
    keys |= {
        (str(row[0]), str(row[1]))
        for row in before.tables["row_keys"]
        if (row[3] is not None and row[3] <= at_us) or (row[2] is not None and row[2] > at_us)
    }
    return keys


def _catch_up_and_check(state: World, before: _Snapshot, swept: bool) -> _Snapshot:
    work = _tick(state)
    after = facts.stored_tables(state.instance)
    assert after is not None
    expected = _expected_claims(state.instance, before, state.at)
    if swept:
        broken = {str(row[0]) for row in before.tables["claim_facts"] if row[_FACT["error_code"]]}
        expected = Counter(set(expected) | _rewritten_claims(state, before) | broken)
    assert work.claims == expected, f"Layer 1 Claims differ after {state.log}"
    assert work.slots == _expected_slots(before.tables, after), f"slots differ after {state.log}"
    assert work.keys == _expected_keys(state, before, after, set(expected)), (
        f"Layer 2 keys differ after {state.log}"
    )
    assert after == facts.rebuilt_tables(state.instance, at=state.at), (
        f"stored tables differ from a rebuild after {state.log}"
    )
    return _snapshot(state.instance)


# -- the ops ------------------------------------------------------------------------------


def _claims(state: World) -> tuple[Any, ...]:
    """Every accepted Claim, retired ones too, at the head."""

    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    return ClaimVerdictReadContext(state.instance, state.instance.accepted_coordinate()).claims()


def _live_status_claims(state: World) -> list[Any]:
    return [
        claim
        for claim in _claims(state)
        if claim.lifecycle.state == "live" and claim.statement.predicate == STATUS
    ]


def _restate(state: World, current: Any, *, same: bool = False) -> DirectClaimAuthoringV1:
    """Restate a Claim on new bytes, or (``same``) on the very content it cites."""

    value = str(current.statement.object.value)
    if same:
        content, start, end = state.sources[current.identity.qualified]
    else:
        # New selected bytes: new evidence, so a revision and never a re-observation.
        content = f"status: {value} (observed {state.clock})".encode()
        start, end = 0, len(content)
    state.sources[current.identity.qualified] = (content, start, end)
    body = state.instance.body_store().store(content)
    return DirectClaimAuthoringV1(
        statement=current.statement,
        rationale="Restate the statement on newly observed bytes.",
        claim_id=current.identity.name,
        predecessor_artifact_digest=claim_artifact_digest(current).tagged,
        source_selection=DirectForeignSourceSelection(
            logical_source_identity=SOURCE,
            span=ContentSpan(content_digest=body.digest, start_byte=start, end_byte=end),
        ),
    )


def _revise(state: World) -> str | None:
    claims = _live_status_claims(state)
    if not claims:
        return None
    current = state.rng.choice(claims)
    try:
        proposed = service_propose_playbill_claim(
            state.instance,
            authoring=_restate(state, current),
            actor_id="owner",
            proposal_name=f"oracle-revise-{state.clock}",
            timestamp=state.timestamp(),
        )
    except AssertionError as exc:
        exc.add_note(f"revising {current.identity.name}: {current.statement.object}")
        raise
    _activate_direct_claim(state.instance, state.owner, proposed)
    return f"revise {current.identity.name}"


def _contend(state: World) -> str | None:
    subject = state.rng.choice(("wi-42", "wi-43"))
    current = [
        claim
        for claim in _live_status_claims(state)
        if claim.statement.subject.artifact_path == subject_path(SUBJECT_KIND, subject)
    ]
    if len(current) != 1:
        return None
    (first,) = current
    value = "blocked" if first.statement.object.value != "blocked" else "done"
    activate(
        state.instance,
        state.owner,
        service_propose_playbill_claim(
            state.instance,
            authoring=authoring(subject, value, with_claim_type=False).model_copy(
                update={
                    "existing_statement_handoffs": (
                        ExistingStatementHandoffV1(
                            statement_digest=claim_statement_digest(first.statement).tagged,
                            disposition="contradict",
                        ),
                    ),
                }
            ),
            actor_id="owner",
            proposal_name=f"oracle-contend-{state.clock}",
            timestamp=state.timestamp(),
        ),
    )
    return f"contend {subject} {value}"


def _retire(state: World) -> str | None:
    claims = _live_status_claims(state)
    if len(claims) < 2:
        return None
    claim = state.rng.choice(claims)
    retire_claim(state.instance, state.owner, claim.identity.name)
    return f"retire {claim.identity.name}"


def _claim_type(state: World) -> str | None:
    from cruxible_client.contracts.claim_types import ClaimResolutionPolicy

    instance = state.instance
    path = claim_type_path(STATUS)
    current = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    change = state.rng.choice(("freshness", "policy", "cardinality"))
    fields = current.model_dump(mode="json")
    fields["lifecycle"] = ArtifactLifecycle(
        predecessor_digest=claim_type_digest(current).tagged
    ).model_dump(mode="json")
    if change == "freshness":
        stale_after = state.rng.choice((10_000_000, 86_400_000_000, 30 * 86_400_000_000))
        fields["evidence_freshness"] = ClaimEvidenceFreshness(
            stale_after=ClaimFreshnessDuration(microseconds=stale_after)
        ).model_dump(mode="json")
        if fields["artifact_format"] == "playbill-claim-type-v1":
            fields["artifact_format"] = "playbill-claim-type-v3"
    elif change == "policy":
        fields["artifact_format"] = "playbill-claim-type-v4"
        fields["attestation_consequence_policy"] = ClaimAttestationConsequencePolicy(
            rules=(
                ClaimAttestationConsequenceRule(
                    rule_id="independent-unsure",
                    stance="unsure",
                    minimum_independent_control_components=state.rng.choice((1, 2)),
                ),
            )
        ).model_dump(mode="json")
    else:
        cardinality = "many" if current.cardinality == "one" else "one"
        fields["cardinality"] = cardinality
        fields["resolution_policy"] = {
            **current.resolution_policy.model_dump(mode="json"),
            "cardinality": cardinality,
            "selector": "all" if cardinality == "many" else "only_contender",
        }
    try:
        ClaimResolutionPolicy.model_validate(fields["resolution_policy"])
        successor = ClaimType.model_validate(fields)
    except ValueError:
        return None  # a change this ClaimType's format or policy cannot carry
    migration = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV1(
            successor=successor,
            dependents=tuple(
                ClaimTypeDependentDispositionV1(
                    claim_id=claim.identity.name, disposition="successor"
                )
                # Every Claim the ClaimType governs, retired ones too.
                for claim in _claims(state)
                if claim.statement.predicate == STATUS
            ),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    candidate = migration.proposal.proposal.candidate
    if candidate is None:
        return None
    _activate_migration(
        instance,
        state.owner,
        migration.proposal.proposal.admission.proposal_id,
        candidate.candidate_digest,
    )
    return f"claim-type {change}"


def _subject(state: World) -> str | None:
    name = f"wi-{100 + state.clock}"
    _accept_tree(
        state.instance,
        state.owner,
        {
            **state.instance.tree_at(state.instance.accepted_coordinate().git_oid),
            subject_path(SUBJECT_KIND, name): render_subject(subject_shell(name)),
        },
        timestamp=state.timestamp(),
        proposal_name=f"oracle-subject-{state.clock}",
    )
    return f"subject {name}"


def _door(state: World) -> str | None:
    claims = _live_status_claims(state)
    if not claims:
        return None
    claim = state.rng.choice(claims)
    stance = state.rng.choice(("support", "contradict", "unsure"))
    when = state.at + timedelta(seconds=state.rng.choice((-60, -1, 0, 30)))
    if state.rng.random() < 0.4:
        # A door-cited body: the Capture lands with the new-capture event.
        capture = _door_capture(state, claim.identity.name)
        _attest(
            state.instance,
            state.owner,
            claim,
            state.root,
            at=when,
            basis="new_capture",
            stance=stance,
            valid_until=when + timedelta(minutes=state.rng.choice((1, 60))),
            captures=(capture,),
        )
        return f"door new-capture {stance} {claim.identity.name}"
    _attest(
        state.instance,
        state.owner,
        claim,
        state.root,
        at=when,
        stance=stance,
        valid_until=None if state.rng.random() < 0.5 else when + timedelta(minutes=5),
    )
    return f"door examined {stance} {claim.identity.name}"


def _door_capture(state: World, claim_id: str) -> str:
    """A new Capture of the world's own source, landing with the door event that cites it."""

    from cruxible_client.contracts.captures import build_working_selection_capture

    state.clock += 1
    selected = f"new observation {state.clock}".encode()
    source = state.instance.body_store().store(selected)
    return build_working_selection_capture(
        store=state.instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        rationale="A new independent source observation awaits adjudication.",
        observed_at=START - timedelta(minutes=5),
        accepted_coordinate=AcceptedCoordinate.from_internal(state.instance.accepted_coordinate()),
        source_id=SOURCE,
        coordinate={"source_byte_length": len(selected), "source_content_digest": source.digest},
        selector={"anchor": "new", "start_byte": 0, "end_byte": len(selected)},
        selected_content=selected,
    ).capture_digest


def _member(state: World) -> str | None:
    from tests.test_claims.test_claim_attestation_service import _request

    claims = _live_status_claims(state)
    if not claims:
        return None
    claim = state.rng.choice(claims)
    timestamp = state.timestamp()
    envelope = _request(
        state.instance,
        state.owner,
        claim.identity.name,
        state.root,
        stance=state.rng.choice(("support", "unsure")),
        attested_at=datetime.fromisoformat(timestamp.replace("Z", "+00:00")) - timedelta(seconds=1),
    ).attestation
    tree = state.instance.tree_at(state.instance.accepted_coordinate().git_oid)
    tree[attestation_path(claim_attestation_v2_envelope_digest(envelope))] = (
        render_accepted_attestation(envelope)
    )
    _accept_tree(
        state.instance,
        state.owner,
        tree,
        timestamp=timestamp,
        proposal_name=f"oracle-member-{state.clock}",
    )
    return f"member {claim.identity.name}"


def _restore(state: World) -> str:
    """Store the erased cited body again: its landing reaches the Claims that read it."""

    body, content = state.erased.popitem()
    state.instance.body_store().store(content)
    return f"store cited {body[7:19]}"


def _body(state: World) -> str | None:
    """Store an uncited body, or erase a cited one (the next op stores it again)."""

    store = state.instance.body_store()
    if state.rng.random() < 0.4:
        store.store(f"uncited {state.clock} {state.rng.random()}".encode())
        state.clock += 1
        return "store uncited"
    claims = _live_status_claims(state)
    if not claims:
        return None
    capture = state.rng.choice(state.rng.choice(claims).backing.capture_digests)
    body = parse_capture_envelope(store.read(capture, access=_READER)).commitment.digest
    if store.file_identity(body) is None:
        return None
    state.erased[body] = store.read(body, access=_READER)
    store.erase(body)
    return f"erase cited {body[7:19]}"


def _reobserve(state: World) -> str | None:
    claims = [
        claim for claim in _live_status_claims(state) if claim.identity.qualified in state.sources
    ]
    if not claims:
        return None
    current = state.rng.choice(claims)
    clock = state.instance.recording_clock
    try:
        submitted = service_reobserve_playbill_claim(
            state.instance,
            authoring=_restate(state, current, same=True),
            actor_id="owner",
            timestamp=state.timestamp(),
        )
    finally:
        # The helper pins the recording clock to its instant; later ops record at theirs.
        state.instance.recording_clock = clock
    if submitted.status.state != "accepted":
        return None
    return f"reobserve {current.identity.name}"


def _fire(state: World, *triggers: InternalTrigger) -> tuple[Any, ...]:
    """Fire what is due at the read instant: ``next.expire`` and these timers."""

    return evaluate_triggers(
        state.instance, now=state.at, listening_since=state.at, triggers=triggers
    )


def _time(state: World) -> str | None:
    """Move the read instant to a stored bound -1us, the bound or +1us, and fire what is due."""

    tables = facts.stored_tables(state.instance)
    assert tables is not None
    at_us = utc_microseconds(state.at)
    assert at_us is not None
    bounds = sorted(
        {
            int(bound)
            for bound in (
                *(row[_FACT["upper_us"]] for row in tables["claim_facts"]),
                *(row[3] for row in tables["row_keys"]),
            )
            if bound is not None and bound > at_us + 1
        }
    )
    if bounds:
        bound = state.rng.choice(bounds[:3])
        state.at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            microseconds=bound + state.rng.choice((-1, 0, 1))
        )
    else:
        state.at += timedelta(hours=state.rng.choice((1, 24)))
    _fire(state)
    return f"time {state.at.isoformat()}"


def _sweep(state: World) -> None:
    """Fire one evidence sweep at the read instant (one microsecond on, if one fired there)."""

    state.at += timedelta(microseconds=1)
    sweep = InternalTrigger(
        "Trigger:oracle-sweep",
        facts.EVIDENCE_SWEEP,
        CadenceSchedule(interval_seconds=1),
        accepted_at=state.at - timedelta(seconds=1),
    )
    fired = _fire(state, sweep)
    assert any(event.action == facts.EVIDENCE_SWEEP for event in fired)


def _rewrite(state: World) -> str | None:
    """Rewrite a cited body in place and fire the sweep that finds it."""

    from tests.test_evidence.test_slot_verdict_reuse import _rewrite_in_place

    claims = _live_status_claims(state)
    if not claims:
        return None
    capture = state.rng.choice(state.rng.choice(claims).backing.capture_digests)
    path = state.instance.body_store()._path(capture)
    if not path.exists():
        return None
    state.rewritten = (path, path.read_bytes())
    _rewrite_in_place(path, keep_mtime=state.rng.random() < 0.5)
    _sweep(state)
    return f"rewrite {capture[7:19]}"


def _repair(state: World) -> str:
    """Restore the rewritten body in place and fire the sweep that follows it."""

    assert state.rewritten is not None
    path, content = state.rewritten
    state.rewritten = None
    path.write_bytes(content)
    _sweep(state)
    return f"repair {path.name[:12]}"


def _restart(state: World) -> str | None:
    """A new process: every per-process memo and overlay gone; the stored state stays."""

    facts._OVERLAYS.clear()
    reset_claim_resolution_memo()
    return "restart"


_OPS: tuple[tuple[str, Callable[[World], str | None], int], ...] = (
    ("revise", _revise, 3),
    ("contend", _contend, 2),
    ("retire", _retire, 1),
    ("claim_type", _claim_type, 2),
    ("subject", _subject, 1),
    ("door", _door, 4),
    ("member", _member, 2),
    ("body", _body, 3),
    ("reobserve", _reobserve, 2),
    ("time", _time, 3),
    ("rewrite", _rewrite, 1),
    ("restart", _restart, 1),
)
_WEIGHTED = tuple((name, op) for name, op, weight in _OPS for _ in range(weight))


def run_oracle(root: Path, seed: int, steps: int) -> World:
    instance, owner = seed_claims(root)
    state = World(instance=instance, owner=owner, root=root, rng=random.Random(seed))
    # The seeded Claims cite their whole source (``seed_claims_into``).
    for claim in _live_status_claims(state):
        content = f"status: {claim.statement.object.value}".encode()
        state.sources[claim.identity.qualified] = (content, 0, len(content))
    facts._OVERLAYS.clear()
    _tick(state)
    before = _snapshot(state.instance)
    for _step in range(steps):
        previous = state.instance.accepted_coordinate()
        sweeps = latest_sequence(state.instance, action=facts.EVIDENCE_SWEEP)
        if state.rewritten is not None:
            name, applied = "repair", _repair(state)
        elif state.erased:
            name, applied = "restore", _restore(state)
        else:
            name, op = state.rng.choice(_WEIGHTED)
            try:
                applied = op(state)
            except Exception as exc:
                exc.add_note(f"op {name} after {state.log}")
                raise
        if applied is None:
            continue
        state.log.append(applied)
        # Behind: the worker has not followed the op yet.
        _assert_answers_equal(state, f"behind after {name}")
        swept = latest_sequence(state.instance, action=facts.EVIDENCE_SWEEP) != sweeps
        before = _catch_up_and_check(state, before, swept)
        _assert_answers_equal(state, f"caught up after {name}")
        if previous != state.instance.accepted_coordinate():
            _assert_answers_equal(state, f"previous coordinate after {name}", at=previous)
    return state


@pytest.mark.parametrize("seed", SEEDS)
def test_incremental_next_equals_the_oracle_fold(tmp_path: Path, seed: int) -> None:
    state = run_oracle(tmp_path, seed, STEPS)
    assert state.log, "no op applied"


@contextmanager
def _contradicted(statement_digest: str) -> Iterator[None]:
    """Every verdict path reads this statement as contradicted, as admitted contradicting
    evidence would make it: Layer 1, the fold's facts and the impact walker alike."""

    from cruxible_client.contracts import claim_verdicts

    original = claim_verdicts.evaluate_claim_verdict

    def evaluate(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if kwargs.get("claim_statement_digest") == statement_digest:
            return result.model_copy(update={"verdict": "contradicted"})
        return result

    with (
        patch("cruxible_core.service.evidence.evidence.evaluate_claim_verdict", evaluate),
        patch("cruxible_core.query.backends.evaluate_claim_verdict", evaluate),
        patch("cruxible_core.query.impact.evaluate_claim_verdict", evaluate),
    ):
        yield


def test_a_contradicted_source_leaves_its_dependent_stale(world: Any) -> None:  # noqa: F811
    """The stale-dependency row through a contradicted source, behind and caught up.

    Accepted state cannot hold a superseded input (an input-claim pin must
    name the current artifact), so a dependent goes stale when its source's
    verdict is contradicted. No accepted path here admits contradicting
    evidence for the derivation world's source, so every verdict path reads it
    as contradicted at its root.
    """

    from tests.test_consumers.test_next_queue import _assert_equivalent, _drain, _served

    at = datetime.fromisoformat("2026-10-02T00:00:00+00:00")
    instance = world.instance
    basis = next(claim for claim in _all_claims(instance) if claim.statement.predicate == STATUS)
    with _contradicted(claim_statement_digest(basis.statement).tagged):
        _drain(instance, at)
        derived = _run_and_accept(world)
        assert claim_artifact_digest(basis).tagged in derived.backing.input_claim_digests
        # Behind: the dependent and its source's entries are derived on read.
        _assert_equivalent(instance, at)
        _drain(instance, at)
        found = _served(instance, at)
        assert found is not None
        (row,) = [item for item in found.rows if item.reason == "claim_dependency_stale"]
        assert row.subject_identity == derived.identity.qualified
        assert row.related_identities == (basis.identity.qualified,)
        _assert_equivalent(instance, at)
        assert facts.stored_tables(instance) == facts.rebuilt_tables(instance, at=at)
