"""Layer 1 of incremental next: facts follow change records and always equal a fresh rebuild.

Each test applies changes one at a time -- a body landing, a generation (a
Claim created in an occupied slot, a revision, a retirement), a door
attestation (examined and new-capture), a re-observation, an erase and a
restore, an expiry fire, an interrupted landing -- lets the worker catch up,
and asserts every Layer 1 table equals a rebuild from nothing at the same
instant. The work is counted exactly: each Claim the records past the
cursors reach is derived once and no other is, and a (predicate, Subject)
slot is resolved again exactly when one of its members' resolution inputs
moved. The expectation comes from the change records and the stored read
index, never from the worker's own bookkeeping.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from cruxible_client.contracts.accepted_attestations import parse_accepted_attestation
from cruxible_client.contracts.claims import claim_statement_digest
from cruxible_client.contracts.errors import CasError
from cruxible_core.consumers.next import facts
from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.indexes.typed_state import utc_microseconds
from cruxible_core.service.discovery.claim_status import claim_resolution_statuses
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.landing_log import LandingLog
from cruxible_core.triggers.journal import journal_path
from tests.core_support._claim_authoring_support import (
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
)
from tests.core_support._knowledge_loop_support import activate, authoring, seed_claims
from tests.core_support._retirement_support import retire_claim
from tests.support.internal_triggers import fire_internal_triggers
from tests.test_authoring.test_authoring_existing_capture import shared_capture_world
from tests.test_integration.test_next_closed_loop import (
    EVALUATION_TIME,
    _current_claim,
    _freshness_world,
    _refresh_claim,
    _revise_claim,
)
from tests.test_integration.test_next_holds import _all_claims, _attest, _capture

PART = facts._PART
_CLAIM_PATH = re.compile(r"claims/[0-9a-f]{2}/(CLM-[0-9a-f]{32})\.json")
_READ = BodyAccessContext(principal_id="facts-test", can_read_body=True)
_STATUS = facts._FACT_COLUMNS.index("status")
_VERDICT = facts._FACT_COLUMNS.index("verdict")
_UPPER = facts._FACT_COLUMNS.index("upper_us")
_SLOT_INPUTS = tuple(
    facts._FACT_COLUMNS.index(column)
    for column in (
        "lifecycle",
        "res_slot",
        "artifact_digest",
        "verdict_json",
        "claim_type_digest",
        "error_code",
    )
)


def _catch_up(instance, at: datetime) -> None:  # type: ignore[no-untyped-def]
    PART.match(instance, now=at, daemon_id="test")
    manager = SimpleNamespace(get=lambda _id: instance)
    for work in PART.due(instance, now=at):
        PART.run(manager, "instance", work, now=at)


def _assert_rebuilt(instance, at: datetime) -> dict[str, list[tuple[Any, ...]]]:  # type: ignore[no-untyped-def]
    stored = facts.layer1_tables(instance)
    assert stored is not None
    rebuilt = facts.rebuilt_layer1_tables(instance, at=at)
    for table in facts.LAYER1_TABLES:
        assert stored[table] == rebuilt[table], table
    return stored


# -- counting the work exactly ----------------------------------------------------


@dataclass
class _Work:
    claims: Counter[str]
    slots: Counter[str]


@contextmanager
def _counted() -> Iterator[_Work]:
    """Every Claim derivation and every slot resolution a catch-up makes."""

    work = _Work(Counter(), Counter())
    derive, resolve = facts._claim_facts, facts._resolve_slot

    def counting_facts(instance, context, claim, **kwargs):  # type: ignore[no-untyped-def]
        work.claims[claim.identity.qualified] += 1
        return derive(instance, context, claim, **kwargs)

    def counting_slots(instance, context, members, *args, **kwargs):  # type: ignore[no-untyped-def]
        work.slots[facts._slot_key(members[0], qualifier=False)] += 1
        return resolve(instance, context, members, *args, **kwargs)

    with (
        patch.object(facts, "_claim_facts", counting_facts),
        patch.object(facts, "_resolve_slot", counting_slots),
    ):
        yield work


@dataclass
class _Snapshot:
    tables: dict[str, list[tuple[Any, ...]]]
    sequence: int
    oid: str
    landing: int
    door: str
    reobs: int


def _snapshot(instance) -> _Snapshot:  # type: ignore[no-untyped-def]
    tables = facts.layer1_tables(instance)
    assert tables is not None
    with instance.accepted_history_reader() as history:
        sequence = history.sequence
    return _Snapshot(
        tables=tables,
        sequence=sequence,
        oid=AcceptedCoordinate.from_internal(instance.accepted_coordinate()).git_oid,
        landing=instance.body_store().landings.head().sequence,
        door=instance.claim_attestation_evidence_store().head(),
        reobs=ClaimReobservationStore(instance).head_sequence(),
    )


def _expected_claims(instance, before: _Snapshot) -> Counter[str]:  # type: ignore[no-untyped-def]
    """The Claims the records since ``before`` reach, each once: from records and reads."""

    reads: dict[str, set[str]] = defaultdict(set)
    for key, claim_id in before.tables["fact_reads"]:
        reads[key].add(claim_id)
    consulting: dict[str, set[str]] = defaultdict(set)
    for body, capture, _claim_id, _identity in before.tables["capture_bodies"]:
        consulting[body].add(capture)
    claims: set[str] = set()
    keys: set[str] = set()
    with instance.accepted_history_reader() as history:
        paths = history.member_paths_between(before.sequence, history.sequence)
    head_oid = AcceptedCoordinate.from_internal(instance.accepted_coordinate()).git_oid
    for path in paths:
        keys.add(f"path:{path}")
        if (match := _CLAIM_PATH.fullmatch(path)) is not None:
            claims.add(f"Claim:{match.group(1)}")
            keys.add(f"law:{path}")
        elif path.startswith("providers/"):
            keys.add(f"provider:Provider:{path.removeprefix('providers/')[:-5]}")
        elif path.startswith("attestations/"):
            content = instance.blob_at(head_oid, path) or instance.blob_at(before.oid, path)
            claims.add(
                parse_accepted_attestation(content, path=path).statement.claim_identity.qualified
            )
    landed = {row.digest for row in instance.body_store().landings.after(before.landing)}
    keys.update(f"capture:{digest}" for digest in landed)
    keys.update(f"capture:{capture}" for digest in landed for capture in consulting[digest])
    keys.update(f"backing:{digest}" for digest in landed)
    _head, events = instance.claim_attestation_evidence_store().events_after(before.door)
    claims.update(
        payload.attestation.statement.claim_identity.qualified for _p, _e, payload in events
    )
    claims.update(
        f"Claim:{claim_id}"
        for _sequence, claim_id in ClaimReobservationStore(instance).records_after(before.reobs)
    )
    claims.update(claim_id for key in keys for claim_id in reads.get(key, ()))
    return Counter(claims)


def _expected_slots(
    before: dict[str, list[tuple[Any, ...]]], after: dict[str, list[tuple[Any, ...]]]
) -> Counter[str]:
    """Each slot whose membership or a member's resolution inputs moved, once."""

    def inputs(tables: dict[str, list[tuple[Any, ...]]]) -> dict[str, tuple[Any, ...]]:
        return {
            str(row[0]): tuple(row[index] for index in _SLOT_INPUTS)
            for row in tables["claim_facts"]
        }

    old, new = inputs(before), inputs(after)
    slots: set[str] = set()
    for claim_id in set(old) | set(new):
        if old.get(claim_id) == new.get(claim_id):
            continue
        for side in (old.get(claim_id), new.get(claim_id)):
            if side is not None and side[0] == "live":
                slots.add(str(side[1]))
    return Counter(slots)


def _step(instance, at: datetime, change, *, expect: Counter[str] | None = None):  # type: ignore[no-untyped-def]
    """Apply one change, catch up, and assert exactly the expected work and a rebuild's tables."""

    before = _snapshot(instance)
    change()
    with _counted() as work:
        _catch_up(instance, at)
    expected = _expected_claims(instance, before) if expect is None else expect
    assert work.claims == expected
    after = _assert_rebuilt(instance, at)
    assert work.slots == _expected_slots(before.tables, after)
    return work, after


def _live_counts(instance, at: datetime) -> dict[str, int]:  # type: ignore[no-untyped-def]
    coordinate = instance.accepted_coordinate()
    context = ClaimVerdictReadContext(instance, coordinate)
    statuses = claim_resolution_statuses(
        instance,
        claims=context.claims(),
        at=AcceptedCoordinate.from_internal(coordinate),
        evaluation_time=at,
        read_context=context,
    )
    counts = Counter(statuses.values())
    return {status: counts[status] for status in facts.TALLY_STATUSES[:-1]}


def _statuses(tables: dict[str, list[tuple[Any, ...]]]) -> dict[str, Any]:
    return {str(row[0]): row[_STATUS] for row in tables["claim_facts"]}


def _subject_claim(instance, subject_id: str) -> str:  # type: ignore[no-untyped-def]
    return str(_current_claim(instance, subject_id=subject_id).identity.qualified)


def _tally(instance, at: datetime) -> dict[str, int] | None:  # type: ignore[no-untyped-def]
    return facts.claim_status_tally(
        instance, coordinate=instance.accepted_coordinate(), evaluation_time=at
    )


def _contend(instance, owner, first) -> None:  # type: ignore[no-untyped-def]
    activate(
        instance,
        owner,
        service_propose_playbill_claim(
            instance,
            authoring=authoring("wi-42", "blocked", with_claim_type=False).model_copy(
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
            proposal_name="facts-conflict",
            timestamp="2026-08-24T17:00:03.000000Z",
        ),
    )


# -- the sequences -------------------------------------------------------------------


def test_layer1_follows_generations_door_and_bodies_and_equals_a_rebuild(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    at = EVALUATION_TIME
    with _counted() as cold:
        _catch_up(instance, at)
    every = Counter(claim.identity.qualified for claim in _all_claims(instance))
    # A cold start derives every Claim exactly once, and resolves each slot once.
    assert cold.claims == every
    tables = _assert_rebuilt(instance, at)
    assert cold.slots == Counter(
        {str(row[facts._FACT_COLUMNS.index("res_slot")]) for row in tables["claim_facts"]}
    )
    assert _tally(instance, at) == _live_counts(instance, at)
    first = _current_claim(instance)
    other = _subject_claim(instance, "wi-43")

    # A body nothing cites lands: no Claim's facts move.
    work, _tables = _step(
        instance, at, lambda: instance.body_store().store(b"a body no Claim consults\n")
    )
    assert work.claims == Counter()

    # A contender joins wi-42's slot: the slot resolves again, once; wi-43 is untouched.
    work, tables = _step(instance, at, lambda: _contend(instance, owner, first))
    contender = next(identity for identity in _statuses(tables) if identity not in every)
    assert work.claims[contender] == 1 and other not in work.claims
    assert work.slots == Counter({facts._slot_key(first, qualifier=False): 1})
    statuses = _statuses(tables)
    assert statuses[first.identity.qualified] == statuses[contender] == "conflicted"
    assert _tally(instance, at) == _live_counts(instance, at)

    # Door events reach their statement's Claim: derived once, and an unchanged
    # result resolves no slot (early cutoff).
    work, tables = _step(
        instance,
        at,
        lambda: _attest(instance, owner, first, tmp_path, at=at - timedelta(minutes=1)),
    )
    assert work.claims == Counter({first.identity.qualified: 1})
    assert work.slots == Counter()
    work, _tables = _step(
        instance,
        at,
        lambda: _attest(
            instance, owner, first, tmp_path, at=at - timedelta(seconds=30), stance="support"
        ),
    )
    assert work.claims == Counter({first.identity.qualified: 1})

    # Retiring the contender resolves the slot again; wi-43 is still untouched.
    work, tables = _step(
        instance, at, lambda: retire_claim(instance, owner, contender.removeprefix("Claim:"))
    )
    assert work.claims[contender] == 1 and other not in work.claims
    statuses = _statuses(tables)
    assert statuses[contender] == "retired"
    assert statuses[first.identity.qualified] == "accepted"
    assert _tally(instance, at) == _live_counts(instance, at)


def test_an_erase_and_a_restore_reach_exactly_the_claims_that_consulted_the_body(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    at = EVALUATION_TIME
    _catch_up(instance, at)
    first = _subject_claim(instance, "wi-42")
    store = instance.body_store()
    source = store.digest_bytes(b"status: ready").tagged
    stored = facts.layer1_tables(instance)
    assert stored is not None
    assert any(row[0] == source for row in stored["capture_bodies"])

    work, erased = _step(instance, at, lambda: store.erase(source))
    assert work.claims == Counter({first: 1})
    assert (source, None) in {(row[0], row[3]) for row in erased["capture_bodies"]}

    work, restored = _step(instance, at, lambda: store.store(b"status: ready"))
    assert work.claims == Counter({first: 1})
    assert restored["claim_facts"] == stored["claim_facts"]


def test_a_door_only_capture_is_a_read_of_its_claim(tmp_path: Path) -> None:
    """A new-capture door event routes to its Claim, and its cited Capture becomes a read
    of that Claim: a later landing of that Capture reaches it."""

    instance, owner, _actor, _first, claim_id, *_rest = shared_capture_world(tmp_path)
    at = EVALUATION_TIME
    _catch_up(instance, at)
    claim = next(c for c in _all_claims(instance) if c.identity.name == claim_id)
    identity = claim.identity.qualified
    capture = _capture(instance, claim_id, b"a door-only observation")
    envelope = instance.body_store().read(capture, access=_READ)

    work, tables = _step(
        instance,
        at,
        lambda: _attest(
            instance,
            owner,
            claim,
            tmp_path,
            at=at - timedelta(minutes=1),
            basis="new_capture",
            stance="support",
            captures=(capture,),
        ),
    )
    assert work.claims == Counter({identity: 1})
    assert work.slots == Counter()
    assert (f"capture:{capture}", identity) in set(tables["fact_reads"])
    assert any(
        row[:3] == (capture, capture, identity) and row[3] is not None
        for row in tables["capture_bodies"]
    )

    work, tables = _step(instance, at, lambda: instance.body_store().erase(capture))
    assert work.claims == Counter({identity: 1})
    assert any(
        row[:3] == (capture, capture, identity) and row[3] is None
        for row in tables["capture_bodies"]
    )
    work, _tables = _step(instance, at, lambda: instance.body_store().store(envelope))
    assert work.claims == Counter({identity: 1})


def test_expiry_fires_and_reobservations_recompute_only_their_claim(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    fresh = datetime(2026, 8, 16, 20, 0, 5, tzinfo=UTC)
    _catch_up(instance, fresh)
    stored = _assert_rebuilt(instance, fresh)
    claim = _current_claim(instance).identity.qualified
    row = next(row for row in stored["claim_facts"] if row[0] == claim)
    assert row[_VERDICT] == "supported" and row[_UPPER] is not None

    # The part armed its deadline at the earliest fact end; a fire is the only
    # way time reaches the facts, and it reaches exactly the facts that ended.
    with sqlite3.connect(journal_path(instance)) as connection:
        (due,) = connection.execute(
            "SELECT due_at FROM deadlines WHERE name=?", (facts.FACTS_EXPIRE,)
        ).fetchone()
    expiry = datetime(2026, 8, 16, 20, 0, 10, tzinfo=UTC)
    earliest = min(row[_UPPER] for row in stored["claim_facts"] if row[_UPPER] is not None)
    assert datetime.fromisoformat(due.replace("Z", "+00:00")) == datetime(
        1970, 1, 1, tzinfo=UTC
    ) + timedelta(microseconds=earliest)
    ended = Counter(
        str(row[0])
        for row in stored["claim_facts"]
        if row[_UPPER] is not None and row[_UPPER] <= utc_microseconds(expiry)
    )
    assert claim in ended
    with _counted() as work:
        _catch_up(instance, expiry)  # no fire yet: nothing to do
    assert work.claims == Counter() and work.slots == Counter()
    work, stored = _step(
        instance, expiry, lambda: fire_internal_triggers(instance, now=expiry), expect=ended
    )
    row = next(row for row in stored["claim_facts"] if row[0] == claim)
    assert row[_VERDICT] == "stale_evidence"

    # A re-observation reaches its own Claim through the journal cursor.
    work, _stored = _step(
        instance,
        expiry,
        lambda: _refresh_claim(instance, owner, timestamp="2026-08-16T20:00:09.000000Z"),
    )
    assert work.claims == Counter({claim: 1})

    # A revision is a generation: the Claim's path names it.
    work, _stored = _step(
        instance,
        expiry,
        lambda: _revise_claim(instance, owner, timestamp="2026-08-16T20:00:09.500000Z"),
    )
    assert work.claims[claim] == 1


def test_invalid_cursors_and_a_new_format_rebuild_from_nothing(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    at = EVALUATION_TIME
    _catch_up(instance, at)
    every = Counter(claim.identity.qualified for claim in _all_claims(instance))
    path = facts._STATE.path(instance)

    landings = iter(range(1, 100))

    def rebuilt(break_cursors) -> None:  # type: ignore[no-untyped-def]
        with sqlite3.connect(path) as connection:
            break_cursors(connection)
        instance.body_store().store(f"something to catch up on {next(landings)}\n".encode())
        with _counted() as work:
            _catch_up(instance, at)
        assert work.claims == every  # each Claim exactly once
        _assert_rebuilt(instance, at)

    # A generation the cursor names but history no longer holds (a reseed, a rollback).
    rebuilt(lambda connection: connection.execute("UPDATE cursors SET generation_oid='0'"))
    # A replaced landing log is a different log: its sequence means nothing here.
    rebuilt(lambda connection: connection.execute("UPDATE cursors SET landing_log='another'"))
    # A door root the chain does not hold.
    rebuilt(
        lambda connection: connection.execute(
            "UPDATE cursors SET door_root=?", ("sha256:" + "9" * 64,)
        )
    )

    # A different derivation format is a different schema: the state starts over.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE format")
        connection.execute("CREATE TABLE format (v INTEGER NOT NULL CHECK(v=0)) STRICT")
    with _counted() as work:
        _catch_up(instance, at)
    assert work.claims == every
    _assert_rebuilt(instance, at)

    # An idle pass derives nothing.
    with _counted() as work:
        _catch_up(instance, at)
    assert work.claims == Counter() and work.slots == Counter()


def test_an_interrupted_landing_is_followed_once_it_is_settled(tmp_path: Path) -> None:
    """A follower that read an intent whose completion failed keeps its cursor before
    it, and the retry's settlement reaches the Claims: facts and tally stay true."""

    instance, _owner = seed_claims(tmp_path)
    at = EVALUATION_TIME
    _catch_up(instance, at)
    first = _subject_claim(instance, "wi-42")
    store = instance.body_store()
    source = store.digest_bytes(b"status: ready").tagged
    store.erase(source)
    _catch_up(instance, at)
    original = LandingLog._append

    def failing(log, kind, digest, *, sync):  # type: ignore[no-untyped-def]
        if kind == "stored" and digest == source:
            # The follower reads the intent while the object is final but unpublished.
            _catch_up(instance, at)
            raise CasError("the completion row could not be written")
        return original(log, kind, digest, sync=sync)

    with patch.object(LandingLog, "_append", failing):
        with pytest.raises(CasError, match="completion row"):
            store.store(b"status: ready")
    assert store.landings.unfinished()
    # Still behind: the tally does not serve, and the next tick's pass derives again.
    assert _tally(instance, at) is None
    PART.match(instance, now=at, daemon_id="test")
    assert tuple(PART.due(instance, now=at))
    # The retry finds the object present and settles the landing it interrupted.
    store.store(b"status: ready")
    assert not store.landings.unfinished()
    with _counted() as work:
        _catch_up(instance, at)
    assert work.claims == Counter({first: 1})
    _assert_rebuilt(instance, at)
    assert _tally(instance, at) == _live_counts(instance, at)


def test_a_landing_whose_writer_died_is_settled_on_the_next_pass(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    at = EVALUATION_TIME
    _catch_up(instance, at)
    first = _subject_claim(instance, "wi-42")
    store = instance.body_store()
    source = store.digest_bytes(b"status: ready").tagged

    def dies(log, kind, digest, *, sync):  # type: ignore[no-untyped-def]
        if kind == "erased":
            raise SystemExit("the writer died before its completion")
        return original(log, kind, digest, sync=sync)

    original = LandingLog._append
    with patch.object(LandingLog, "_append", dies), pytest.raises(SystemExit):
        store.erase(source)
    assert store.landings.unfinished()
    # The next pass settles it before reading the log, then follows the completion.
    with _counted() as work:
        _catch_up(instance, at)
    assert not store.landings.unfinished()
    assert work.claims == Counter({first: 1})
    _assert_rebuilt(instance, at)
    assert _tally(instance, at) == _live_counts(instance, at)


# -- the mutation guard --------------------------------------------------------------


@pytest.mark.parametrize("mutation", ("extra", "missing"))
def test_the_exact_work_assertion_rejects_extra_and_missing_derivations(
    tmp_path: Path, mutation: str
) -> None:
    instance, owner = seed_claims(tmp_path)
    at = EVALUATION_TIME
    _catch_up(instance, at)
    first = _current_claim(instance)
    original = facts._derive

    def mutated(instance, context, claim_ids, stored, **kwargs):  # type: ignore[no-untyped-def]
        claims = set(claim_ids)
        if not kwargs["every_slot"]:
            if mutation == "extra":
                claims.add(_subject_claim(instance, "wi-43"))
            else:
                claims.discard(first.identity.qualified)
        return original(instance, context, claims, stored, **kwargs)

    with patch.object(facts, "_derive", mutated), pytest.raises(AssertionError):
        _step(
            instance,
            at,
            lambda: _attest(instance, owner, first, tmp_path, at=at - timedelta(minutes=1)),
        )


# -- bounded probes -------------------------------------------------------------------


def _sized_state(size: int) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(facts._SCHEMA)
    required = [
        str(row[1])
        for row in connection.execute("PRAGMA table_info(claim_facts)")
        if row[3] and row[1] not in {"claim_id", "lower_us", "upper_us", "unservable"}
    ]
    connection.executemany(
        f"INSERT INTO claim_facts(claim_id,{','.join(required)},lower_us,upper_us,unservable) "
        f"VALUES ({','.join('?' for _ in range(len(required) + 4))})",
        (
            (f"Claim:{index}", *("x" for _ in required), index, index + 100, 0)
            for index in range(size)
        ),
    )
    return connection


def _steps(connection: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> int:
    count = [0]

    def tick() -> int:
        count[0] += 1
        return 0

    connection.set_progress_handler(tick, 1)
    try:
        connection.execute(sql, parameters).fetchall()
    finally:
        connection.set_progress_handler(None, 0)
    return count[0]


@pytest.mark.parametrize(
    ("sql", "parameters", "index"),
    (
        (facts._EARLIEST_END, (), "claim_facts_by_upper"),
        (facts._LATEST_START, (), "claim_facts_by_lower"),
        (
            "SELECT claim_id FROM claim_facts WHERE upper_us IS NOT NULL AND upper_us<=?",
            (5,),
            "claim_facts_by_upper",
        ),
    ),
)
def test_the_interval_probes_seek_their_index_whatever_the_claim_count(
    sql: str, parameters: tuple[Any, ...], index: str
) -> None:
    small, large = _sized_state(100), _sized_state(10_000)
    for connection in (small, large):
        plan = " ".join(
            str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)
        )
        assert index in plan
    # The same work at a hundred Claims and at ten thousand.
    assert _steps(small, sql, parameters) == _steps(large, sql, parameters)


def test_the_tally_answers_only_while_the_facts_are_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.discovery import orient

    instance, _owner = _freshness_world(tmp_path)
    fresh = datetime(2026, 8, 16, 20, 0, 5, tzinfo=UTC)
    coordinate = instance.accepted_coordinate()
    assert facts.claim_status_tally(instance, coordinate=coordinate, evaluation_time=fresh) is None
    _catch_up(instance, fresh)
    tally = facts.claim_status_tally(instance, coordinate=coordinate, evaluation_time=fresh)
    assert tally == _live_counts(instance, fresh)

    def derived(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("orient derived statuses the tally holds")

    with monkeypatch.context() as patched:
        patched.setattr(
            "cruxible_core.service.discovery.claim_status.claim_resolution_statuses", derived
        )
        patched.setattr(
            "cruxible_core.service.discovery.claim_status.remembered_resolution_statuses", derived
        )
        counts = orient._claim_counts(instance, coordinate, evaluation_time=fresh)
    assert counts.model_dump() == tally

    # Past a fact's interval, after a landing, or with the worker disabled: derive live.
    past = datetime(2026, 8, 16, 20, 0, 10, tzinfo=UTC)
    assert facts.claim_status_tally(instance, coordinate=coordinate, evaluation_time=past) is None
    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "next")
    assert facts.claim_status_tally(instance, coordinate=coordinate, evaluation_time=fresh) is None
    monkeypatch.delenv("CRUXIBLE_DISABLED_CONSUMERS")
    instance.body_store().store(b"a landing the facts have not followed\n")
    assert facts.claim_status_tally(instance, coordinate=coordinate, evaluation_time=fresh) is None
    _catch_up(instance, fresh)
    assert facts.claim_status_tally(instance, coordinate=coordinate, evaluation_time=fresh) == tally
