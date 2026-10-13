"""Layer 1 of incremental next: facts follow change records and always equal a fresh rebuild.

Each test applies changes one at a time -- a body landing, a generation (a
Claim created in an occupied slot, a revision, a retirement), a door
attestation, a re-observation, an erase and a restore, an expiry fire -- lets
the worker catch up, and asserts every Layer 1 table equals a rebuild from
nothing at the same instant. The recomputed Claims are counted too: a change
recomputes the Claims it reaches, never the instance.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cruxible_client.contracts.claims import claim_statement_digest
from cruxible_core.consumers.next import facts
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.indexes.typed_state import utc_microseconds
from cruxible_core.service.discovery.claim_status import claim_resolution_statuses
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
from cruxible_core.triggers.journal import journal_path
from tests.core_support._claim_authoring_support import (
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
)
from tests.core_support._knowledge_loop_support import activate, authoring, seed_claims
from tests.core_support._retirement_support import retire_claim
from tests.support.internal_triggers import fire_internal_triggers
from tests.test_integration.test_next_closed_loop import (
    EVALUATION_TIME,
    _current_claim,
    _freshness_world,
    _refresh_claim,
    _revise_claim,
)
from tests.test_integration.test_next_holds import _all_claims, _attest

PART = facts._PART


def _catch_up(instance, at: datetime) -> None:  # type: ignore[no-untyped-def]
    PART.match(instance, now=at, daemon_id="test")
    manager = SimpleNamespace(get=lambda _id: instance)
    for work in PART.due(instance, now=at):
        PART.run(manager, "instance", work, now=at)


def _assert_rebuilt(instance, at: datetime) -> dict[str, list[tuple[object, ...]]]:  # type: ignore[no-untyped-def]
    stored = facts.layer1_tables(instance)
    assert stored is not None
    rebuilt = facts.rebuilt_layer1_tables(instance, at=at)
    for table in facts.LAYER1_TABLES:
        assert stored[table] == rebuilt[table], table
    return stored


@contextmanager
def _recomputed() -> Iterator[list[str]]:
    """Every Claim whose facts a catch-up derives, in order."""

    seen: list[str] = []
    original = facts._claim_facts

    def counting(instance, context, claim, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(claim.identity.qualified)
        return original(instance, context, claim, **kwargs)

    with patch.object(facts, "_claim_facts", counting):
        yield seen


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


def _statuses(instance) -> dict[str, str | None]:  # type: ignore[no-untyped-def]
    stored = facts.layer1_tables(instance)
    assert stored is not None
    return {str(row[0]): row[17] for row in stored["claim_facts"]}


def _subject_claim(instance, subject_id: str) -> str:  # type: ignore[no-untyped-def]
    return str(_current_claim(instance, subject_id=subject_id).identity.qualified)


def test_layer1_follows_generations_door_and_bodies_and_equals_a_rebuild(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    at = EVALUATION_TIME
    with _recomputed() as cold:
        _catch_up(instance, at)
    every = {claim.identity.qualified for claim in _all_claims(instance)}
    assert set(cold) == every  # a cold start derives every Claim, once
    _assert_rebuilt(instance, at)
    tally = facts.claim_status_tally(
        instance, coordinate=instance.accepted_coordinate(), evaluation_time=at
    )
    assert tally == _live_counts(instance, at)
    first = _current_claim(instance)
    other = _subject_claim(instance, "wi-43")

    # A body nothing cites lands: no Claim's facts move.
    with _recomputed() as seen:
        instance.body_store().store(b"a body no Claim consults\n")
        _catch_up(instance, at)
    assert seen == []
    _assert_rebuilt(instance, at)

    # A contender joins wi-42's slot: the slot resolves again, wi-43 is untouched.
    with _recomputed() as seen:
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
        _catch_up(instance, at)
    contender = next(
        claim.identity.qualified
        for claim in _all_claims(instance)
        if claim.identity.qualified not in every
    )
    assert contender in seen and other not in seen
    stored = _assert_rebuilt(instance, at)
    statuses = _statuses(instance)
    assert statuses[first.identity.qualified] == statuses[contender] == "conflicted"
    assert sum(row[1] for row in stored["tally"]) == len(every) + 1
    assert facts.claim_status_tally(
        instance, coordinate=instance.accepted_coordinate(), evaluation_time=at
    ) == _live_counts(instance, at)

    # A door attestation is followed from the door's cursor; no fact moves.
    door_before = len(stored["door_events"])
    with _recomputed() as seen:
        _attest(instance, owner, first, tmp_path, at=at - timedelta(minutes=1))
        _catch_up(instance, at)
    assert seen == []
    stored = _assert_rebuilt(instance, at)
    assert len(stored["door_events"]) == door_before + 1

    # Retiring the contender resolves the slot again; wi-43 is still untouched.
    with _recomputed() as seen:
        retire_claim(instance, owner, contender.removeprefix("Claim:"))
        _catch_up(instance, at)
    assert contender in seen and other not in seen
    _assert_rebuilt(instance, at)
    statuses = _statuses(instance)
    assert statuses[contender] == "retired"
    assert statuses[first.identity.qualified] == "accepted"
    assert facts.claim_status_tally(
        instance, coordinate=instance.accepted_coordinate(), evaluation_time=at
    ) == _live_counts(instance, at)


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

    with _recomputed() as seen:
        assert store.erase(source)
        _catch_up(instance, at)
    assert seen == [first]
    erased = _assert_rebuilt(instance, at)
    assert (source, None) in {(row[0], row[2]) for row in erased["capture_bodies"]}

    with _recomputed() as seen:
        store.store(b"status: ready")
        _catch_up(instance, at)
    assert seen == [first]
    restored = _assert_rebuilt(instance, at)
    assert restored["claim_facts"] == stored["claim_facts"]


def test_expiry_fires_and_reobservations_recompute_only_their_claim(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    fresh = datetime(2026, 8, 16, 20, 0, 5, tzinfo=UTC)
    _catch_up(instance, fresh)
    _assert_rebuilt(instance, fresh)
    claim = _current_claim(instance).identity.qualified
    stored = facts.layer1_tables(instance)
    assert stored is not None
    row = next(row for row in stored["claim_facts"] if row[0] == claim)
    assert row[15] == "supported" and row[22] is not None

    # The part armed its deadline at the earliest fact end; a fire is the only
    # way time reaches the facts, and it reaches the facts that ended by then.
    with sqlite3.connect(journal_path(instance)) as connection:
        (due,) = connection.execute(
            "SELECT due_at FROM deadlines WHERE name=?", (facts.FACTS_EXPIRE,)
        ).fetchone()
    expiry = datetime(2026, 8, 16, 20, 0, 10, tzinfo=UTC)
    earliest = min(row[22] for row in stored["claim_facts"] if row[22] is not None)
    assert datetime.fromisoformat(due.replace("Z", "+00:00")) == datetime(
        1970, 1, 1, tzinfo=UTC
    ) + timedelta(microseconds=earliest)
    ended = {
        str(row[0])
        for row in stored["claim_facts"]
        if row[22] is not None and row[22] <= utc_microseconds(expiry)
    }
    assert claim in ended
    with _recomputed() as seen:
        _catch_up(instance, expiry)  # no fire yet: nothing to do
    assert seen == []
    fires = fire_internal_triggers(instance, now=expiry)
    assert any(fire.action == facts.FACTS_EXPIRE for fire in fires)
    with _recomputed() as seen:
        _catch_up(instance, expiry)
    assert set(seen) == ended
    stored = _assert_rebuilt(instance, expiry)
    row = next(row for row in stored["claim_facts"] if row[0] == claim)
    assert row[15] == "stale_evidence"

    # A re-observation reaches its own Claim through the journal cursor.
    with _recomputed() as seen:
        _refresh_claim(instance, owner, timestamp="2026-08-16T20:00:09.000000Z")
        _catch_up(instance, expiry)
    assert seen == [claim]
    _assert_rebuilt(instance, expiry)

    # A revision is a generation: the Claim's path names it.
    with _recomputed() as seen:
        _revise_claim(instance, owner, timestamp="2026-08-16T20:00:09.500000Z")
        _catch_up(instance, expiry)
    assert claim in seen
    _assert_rebuilt(instance, expiry)


def test_invalid_cursors_and_a_new_format_rebuild_from_nothing(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    at = EVALUATION_TIME
    _catch_up(instance, at)
    every = {claim.identity.qualified for claim in _all_claims(instance)}
    path = facts._STATE.path(instance)

    # A generation the cursor names but history no longer holds (a reseed, a rollback).
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE cursors SET generation_oid='0'")
    instance.body_store().store(b"something to catch up on\n")
    with _recomputed() as seen:
        _catch_up(instance, at)
    assert set(seen) == every
    _assert_rebuilt(instance, at)

    # A replaced landing log is a different log: its sequence means nothing here.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE cursors SET landing_log='another-log'")
    instance.body_store().store(b"and another\n")
    with _recomputed() as seen:
        _catch_up(instance, at)
    assert set(seen) == every
    _assert_rebuilt(instance, at)

    # A door root the chain does not hold.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE cursors SET door_root=?", ("sha256:" + "9" * 64,))
    instance.body_store().store(b"and a third\n")
    with _recomputed() as seen:
        _catch_up(instance, at)
    assert set(seen) == every
    _assert_rebuilt(instance, at)

    # A different derivation format is a different schema: the state starts over.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE format")
        connection.execute("CREATE TABLE format (v INTEGER NOT NULL CHECK(v=0)) STRICT")
    with _recomputed() as seen:
        _catch_up(instance, at)
    assert set(seen) == every
    _assert_rebuilt(instance, at)

    # An idle pass derives nothing.
    with _recomputed() as seen:
        _catch_up(instance, at)
    assert seen == []


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
