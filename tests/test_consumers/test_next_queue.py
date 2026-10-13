"""next's Claim rows served from the stored state equal the full fold, caught up or behind.

The stored rows (``consumers.next.facts``: Layer 1 facts, Layer 2 rows per
family key) serve every read at the current accepted and door heads. A read
that finds the worker behind -- in records or in time -- derives what the
worker has not followed in a transaction it rolls back, so the answer is the
fold's byte for byte and the stored state never moves on a read (R3). The
fold itself (``_oracle_claim_rows``) still serves a historical coordinate, a
pinned door head, a disabled or cold worker, and is what every test here
compares against.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.claims import claim_statement_digest
from cruxible_client.contracts.errors import CruxibleError
from cruxible_core.consumers.next import facts, rows
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.indexes.typed_state import utc_microseconds
from cruxible_core.service.discovery import next as next_module
from cruxible_core.service.discovery.next import (
    DEFAULT_EXPIRING_WITHIN_MICROSECONDS,
    NextCursorMismatch,
    NextRequest,
    claim_unsure_holds,
    service_playbill_next,
    summarize_playbill_next,
)
from tests.core_support._claim_authoring_support import (
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
)
from tests.core_support._knowledge_loop_support import activate, authoring, seed_claims
from tests.test_authoring.test_authoring_existing_capture import shared_capture_world
from tests.test_authoring.test_derivation_admission import (
    _run_and_accept,
    world,  # noqa: F401
)
from tests.test_evidence.test_attestation_consequence_next import threshold_world
from tests.test_integration.test_next_closed_loop import (
    EVALUATION_TIME,
    _access,
    _current_claim,
    _foreign_world,
    _freshness_world,
    _refresh_claim,
    _revise_claim,
)
from tests.test_integration.test_next_holds import _all_claims, _attest, _capture

WORKER = facts._PART
_DERIVED_AT = datetime.fromisoformat("2026-10-02T00:00:00+00:00")
_DEFAULT_LEAD = CanonicalDuration(microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS)


def _drain(instance, at: datetime = EVALUATION_TIME) -> None:  # type: ignore[no-untyped-def]
    WORKER.match(instance, now=at, daemon_id="first")
    manager = SimpleNamespace(get=lambda _id: instance)
    for work in WORKER.due(instance, now=at):
        WORKER.run(manager, "instance", work, now=at)


def _served(  # type: ignore[no-untyped-def]
    instance, at: datetime, *, expiring_within: CanonicalDuration = _DEFAULT_LEAD
):
    return facts.served_claim_rows(
        instance,
        coordinate=instance.accepted_coordinate(),
        door_head=instance.claim_attestation_evidence_store().head(),
        evaluation_time=at,
        expiring_within=expiring_within,
    )


@contextmanager
def _oracle_only() -> Iterator[None]:
    """Every read folds: the stored rows are never asked."""

    with patch.object(facts, "served_claim_rows", return_value=None):
        yield


@contextmanager
def _no_fold() -> Iterator[None]:
    with patch.object(next_module, "_oracle_claim_rows", side_effect=AssertionError("folded")):
        yield


def _assert_equivalent(instance, at: datetime, *, served: bool = True, **request: Any) -> None:  # type: ignore[no-untyped-def]
    """Every read surface answers byte for byte as the fold does, and writes nothing."""

    claims = _all_claims(instance)
    before = facts.stored_tables(instance)
    assert (_served(instance, at, **request) is not None) == served
    # A first read settles other parts' read-side bookkeeping (settled-refusal
    # counts); compare the reads after it.
    service_playbill_next(
        instance, request=NextRequest(evaluation_time=at, access_profile=_access(), **request)
    )
    for surface, rung in ((None, None), ("sdk", 0), ("mcp", 1)):
        ask = NextRequest(
            evaluation_time=at, access_profile=_access(), caller_surface=surface, **request
        )
        incremental = service_playbill_next(instance, request=ask, caller_rung=rung)
        with _oracle_only():
            oracle = service_playbill_next(instance, request=ask, caller_rung=rung)
        assert incremental.model_dump_json() == oracle.model_dump_json()
        assert incremental.result_digest == oracle.result_digest
    ask = NextRequest(evaluation_time=at, access_profile=_access(), **request)
    summary = summarize_playbill_next(instance, request=ask)
    held = claim_unsure_holds(
        instance, coordinate=instance.accepted_coordinate(), claims=claims, evaluation_time=at
    )
    with _oracle_only():
        assert summary == summarize_playbill_next(instance, request=ask)
        assert held == claim_unsure_holds(
            instance, coordinate=instance.accepted_coordinate(), claims=claims, evaluation_time=at
        )
    # Reads never write the worker's state (R3).
    assert facts.stored_tables(instance) == before


def _instant(microseconds: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=EVALUATION_TIME.tzinfo) + timedelta(
        microseconds=microseconds
    )


def _edges(instance, at: datetime) -> list[datetime]:  # type: ignore[no-untyped-def]
    """The instants around the nearest bounds of every stored fact and key."""

    tables = facts.stored_tables(instance)
    assert tables is not None
    at_us = utc_microseconds(at)
    assert at_us is not None
    lower_index = facts._FACT_COLUMNS.index("lower_us")
    upper_index = facts._FACT_COLUMNS.index("upper_us")
    bounds = [(row[lower_index], row[upper_index]) for row in tables["claim_facts"]]
    bounds += [(row[2], row[3]) for row in tables["row_keys"]]
    lowers = [lower for lower, _upper in bounds if lower is not None and lower <= at_us]
    uppers = [upper for _lower, upper in bounds if upper is not None and upper > at_us]
    instants = [at, at + timedelta(microseconds=1)]
    if lowers:
        lower = _instant(max(lowers))
        instants += [lower - timedelta(microseconds=1), lower]
    if uppers:
        upper = _instant(min(uppers))
        instants += [
            upper - timedelta(microseconds=1),
            upper,
            upper + timedelta(microseconds=1),
        ]
    return sorted(set(instants))


def _check_edges(instance, at: datetime) -> None:  # type: ignore[no-untyped-def]
    _drain(instance, at)
    for instant in _edges(instance, at):
        _assert_equivalent(instance, instant)


def _contend(instance, owner) -> None:  # type: ignore[no-untyped-def]
    first = _current_claim(instance)
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
            proposal_name="queue-conflict",
            timestamp="2026-08-24T17:00:03.000000Z",
        ),
    )


def _every_backing_input(source_identity, dependents, *, lineage_complete):  # type: ignore[no-untyped-def]
    """Hand every backing-input dependent an entry, stale or not, so both paths' show."""

    return [
        (
            dependent.identity,
            {
                "source_claim_identity": source_identity,
                "used_artifact_digest": dependent.used_artifact_digest,
                "current_artifact_digest": dependent.current_artifact_digest,
                "impact_reasons": list(dependent.impact_reasons),
                "lineage_complete": lineage_complete,
            },
        )
        for dependent in dependents
        if dependent.kind == "Claim" and dependent.dependency_kind == "backing_input"
    ]


@pytest.mark.parametrize(
    "history",
    (
        "supported",
        "uncovered",
        "freshness",
        "door-contradict",
        "door-support",
        "door-unsure",
        "threshold",
        "conflict",
        "derivation",
    ),
)
def test_served_rows_equal_the_fold_across_histories_caught_up_behind_and_at_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    history: str,
) -> None:
    at = EVALUATION_TIME
    change = None
    if history == "supported":
        instance, _owner = seed_claims(tmp_path)
    elif history == "uncovered":
        instance, owner, *_rest = _foreign_world(tmp_path, bind=False)

        def change() -> None:
            _attest(instance, owner, _current_claim(instance), tmp_path, at=at)

    elif history == "freshness":
        instance, _owner = _freshness_world(tmp_path)
        _drain(instance, datetime.fromisoformat("2026-08-09T20:00:00+00:00"))
        # Entering the seven-day horizon, the observation and the evidence expiry.
        for time in (
            datetime.fromisoformat("2026-08-09T20:00:10+00:00"),
            datetime.fromisoformat("2026-08-16T20:00:00+00:00"),
            datetime.fromisoformat("2026-08-16T20:00:10+00:00"),
        ):
            # Behind in time only: the read derives what ended since the worker's pass.
            _assert_equivalent(instance, time)
            _check_edges(instance, time)
        found = _served(instance, time)
        assert found is not None
        assert any(row.reason == "claim_stale_evidence" for row in found.rows)
        return
    elif history == "conflict":
        instance, owner = seed_claims(tmp_path)

        def change() -> None:
            _contend(instance, owner)
            for claim in _all_claims(instance):
                _attest(
                    instance,
                    owner,
                    claim,
                    tmp_path,
                    at=at - timedelta(minutes=1),
                    valid_until=at + timedelta(minutes=3),
                )

    elif history == "derivation":
        # Every backing-input dependent gets an entry, so the dependency rows
        # compare the source keys' dependents with the impact walker's.
        monkeypatch.setattr(next_module, "stale_input_entries", _every_backing_input)
        derivation = request.getfixturevalue("world")
        derived = _run_and_accept(derivation)
        instance = derivation.instance
        at = _DERIVED_AT
        assert derived.backing.input_claim_digests
    elif history.startswith("door-"):
        instance, owner, _actor, _first, claim_id, *_rest = shared_capture_world(tmp_path)
        claim = next(c for c in _all_claims(instance) if c.identity.name == claim_id)
        capture = _capture(instance, claim_id, b"new observation")
        _attest(instance, owner, claim, tmp_path, at=at - timedelta(minutes=2))

        def change() -> None:
            _attest(
                instance,
                owner,
                claim,
                tmp_path,
                at=at + timedelta(minutes=1),
                basis="new_capture",
                stance=history.removeprefix("door-"),
                valid_until=at + timedelta(minutes=3),
                captures=(capture,),
            )

    else:
        instance, _owner, _claim = threshold_world(
            tmp_path,
            monkeypatch,
            attestation_mutator=lambda _i, _c, attestations: tuple(
                att.model_copy(
                    update={
                        "statement": att.statement.model_copy(
                            update={
                                "observed_at": at + timedelta(minutes=index),
                                "valid_until": at + timedelta(minutes=3),
                            }
                        )
                    }
                )
                for index, att in enumerate(attestations)
            ),
        )
    # Cold: the fold serves; no stored rows exist yet.
    _assert_equivalent(instance, at, served=False)
    _drain(instance, at)
    if change is not None:
        change()
        # Behind in records: the read derives the change and rolls it back.
        _assert_equivalent(instance, at)
    # A landing nothing reads leaves the worker behind with no Claim to derive.
    instance.body_store().store(b"a body no Claim consults\n")
    _assert_equivalent(instance, at)
    _check_edges(instance, at)
    expected = {
        "uncovered": "claim_uncovered",
        "conflict": "claim_conflicted",
        "derivation": "claim_dependency_stale",
    }.get(history)
    if history.startswith("door-") or history == "threshold":
        later = at + timedelta(minutes=2)
        _assert_equivalent(instance, later)
        _check_edges(instance, later)
        expected = {
            "door-contradict": "claim_contradicting_evidence_available",
            "door-support": "claim_new_evidence_supporting",
            "door-unsure": "claim_new_evidence_unreviewed",
            "threshold": "claim_attestation_threshold_met",
        }[history]
        at = later
    if expected is not None:
        found = _served(instance, at)
        assert found is not None
        assert any(row.reason == expected for row in found.rows)


def test_serving_skips_the_fold_and_keeps_paging_and_delta(tmp_path: Path) -> None:
    from tests.test_discovery.test_next_request_reads import _rich_request
    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, _owner = _instance_with_query(tmp_path)
    request = _rich_request(instance).model_copy(update={"limit": 1})
    _drain(instance, request.evaluation_time)
    with _no_fold():
        first = service_playbill_next(instance, request=request)
        summary = summarize_playbill_next(instance, request=request)
        assert summary.total_items == first.total_items
        delta = service_playbill_next(
            instance,
            request=request.model_copy(update={"since_result_digest": first.result_digest}),
        )
        assert delta.items == ()
        assert first.next_cursor is not None
        second = service_playbill_next(
            instance, request=request.model_copy(update={"cursor": first.next_cursor})
        )
        assert second.result_digest == first.result_digest
    with _oracle_only():
        assert service_playbill_next(instance, request=request) == first


def test_a_head_page_cursor_refuses_after_a_head_move_without_folding(tmp_path: Path) -> None:
    """R8: paging a head read across a head move refuses at once; a pinned read folds."""

    from tests.test_discovery.test_next_request_reads import _rich_request
    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, owner = _instance_with_query(tmp_path)
    request = _rich_request(instance).model_copy(update={"limit": 1})
    _drain(instance, request.evaluation_time)
    first = service_playbill_next(instance, request=request)
    assert first.next_cursor is not None
    old_door = instance.claim_attestation_evidence_store().head()
    _attest(
        instance,
        owner,
        _all_claims(instance)[0],
        tmp_path,
        at=request.evaluation_time - timedelta(minutes=1),
    )
    with patch.object(next_module, "_next_queue", side_effect=AssertionError("folded")):
        with pytest.raises(NextCursorMismatch, match="moved since the cursor's first page"):
            service_playbill_next(
                instance, request=request.model_copy(update={"cursor": first.next_cursor})
            )
    # A read that pins its door head is historical: its pages fold at those pins.
    pinned = request.model_copy(update={"at_attestation_head_digest": old_door})
    with patch.object(
        next_module, "_oracle_claim_rows", wraps=next_module._oracle_claim_rows
    ) as fold:
        historical = service_playbill_next(instance, request=pinned)
        assert historical.next_cursor is not None
        page = service_playbill_next(
            instance, request=pinned.model_copy(update={"cursor": historical.next_cursor})
        )
        assert fold.call_count == 2
    assert page.result_digest == historical.result_digest


def test_restart_does_no_work_and_a_schema_mismatch_folds_until_rebuilt(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    _drain(instance)
    WORKER.match(instance, now=EVALUATION_TIME + timedelta(days=99), daemon_id="restart")
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME)) == ()
    with sqlite3.connect(facts._STATE.path(instance)) as connection:
        connection.execute("CREATE TABLE alien(value TEXT)")
    assert _served(instance, EVALUATION_TIME) is None
    _drain(instance)
    assert _served(instance, EVALUATION_TIME) is not None


def test_door_movement_is_served_behind_and_a_pinned_door_head_folds(tmp_path: Path) -> None:
    from cruxible_core.consumers.runner import consumer_statuses
    from tests.test_consumers.test_prediction_settlement import drain

    instance, owner, *_rest = _foreign_world(tmp_path, bind=False)
    _drain(instance)
    old_door = instance.claim_attestation_evidence_store().head()
    _attest(instance, owner, _current_claim(instance), tmp_path, at=EVALUATION_TIME)
    (health,) = WORKER.health(instance, now=EVALUATION_TIME)
    assert health.state == "lagging" and health.detail["attestations_behind"]
    _assert_equivalent(instance, EVALUATION_TIME)
    _drain(instance)
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"
    found = _served(instance, EVALUATION_TIME)
    assert found is not None and found.holds
    drain(instance, now=EVALUATION_TIME)
    statuses = consumer_statuses(SimpleNamespace(open_instances=lambda: (("inst", instance),)))
    assert any(row.kind == "next" and row.state == "running" for row in statuses)
    _assert_equivalent(instance, EVALUATION_TIME)
    request = NextRequest(
        evaluation_time=EVALUATION_TIME,
        access_profile=_access(),
        at_attestation_head_digest=old_door,
    )
    with patch.object(
        next_module, "_oracle_claim_rows", wraps=next_module._oracle_claim_rows
    ) as fold:
        service_playbill_next(instance, request=request)
        assert fold.called


def test_accepted_movement_is_served_behind_and_each_fallback_folds(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    before = instance.accepted_coordinate()
    _drain(instance)
    _revise_claim(instance, owner, timestamp="2026-08-24T18:00:00.000000Z")
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "lagging"
    _assert_equivalent(instance, EVALUATION_TIME)
    _drain(instance)
    request = NextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access())
    for update in (
        {"at": AcceptedCoordinate.from_internal(before)},
        {
            "access_profile": CoverageAccessProfile(
                profile_id="public", permitted_access_classes=("public",)
            )
        },
    ):
        with patch.object(
            next_module, "_oracle_claim_rows", wraps=next_module._oracle_claim_rows
        ) as fold:
            service_playbill_next(instance, request=request.model_copy(update=update))
            # A historical coordinate folds; a profile without instance access
            # asks the fold, which then says nothing about Claims.
            assert fold.called


@pytest.mark.parametrize("days", (1, 6, 8, 30))
def test_a_custom_expiring_lead_is_served_without_evaluating_a_verdict(
    tmp_path: Path, days: int
) -> None:
    instance, _owner = _freshness_world(tmp_path)
    at = datetime.fromisoformat("2026-08-09T20:00:10+00:00")
    _drain(instance, at)
    lead = CanonicalDuration(microseconds=int(timedelta(days=days).total_seconds() * 1_000_000))
    with (
        _no_fold(),
        patch.object(facts, "_claim_facts", side_effect=AssertionError("derived")),
    ):
        assert _served(instance, at, expiring_within=lead) is not None
    _assert_equivalent(instance, at, expiring_within=lead)


def test_library_reads_create_no_worker_state_and_a_disabled_worker_folds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    assert _served(instance, EVALUATION_TIME) is None
    service_playbill_next(
        instance,
        request=NextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access()),
    )
    assert not facts._STATE.path(instance).parent.exists()
    _drain(instance)
    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "next")
    assert not WORKER.active(instance)
    assert _served(instance, EVALUATION_TIME) is None


def test_cas_and_reobservation_movement_are_served_behind(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    _drain(instance)
    coordinate = instance.accepted_coordinate()
    door = instance.claim_attestation_evidence_store().head()
    instance.body_store().store(b"new material")
    (health,) = WORKER.health(instance, now=EVALUATION_TIME)
    assert health.state == "lagging" and health.detail["inputs_changed"]
    assert instance.accepted_coordinate() == coordinate
    assert instance.claim_attestation_evidence_store().head() == door
    _assert_equivalent(instance, EVALUATION_TIME)
    _drain(instance)
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"
    # A re-observation moves freshness without moving accepted state or bodies.
    _refresh_claim(instance, owner, timestamp="2026-08-16T20:00:09.000000Z")
    assert instance.accepted_coordinate() == coordinate
    _assert_equivalent(instance, EVALUATION_TIME)
    _drain(instance)
    with _no_fold():
        service_playbill_next(
            instance,
            request=NextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access()),
        )


def test_concurrent_reads_behind_the_worker_derive_the_change_once(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    _drain(instance)
    _revise_claim(instance, owner, timestamp="2026-08-24T18:00:00.000000Z")
    original = facts._changes
    later = EVALUATION_TIME + timedelta(seconds=1)
    calls: list[int] = []
    second: list[Any] = []
    readers: list[threading.Thread] = []

    def changes(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            # Another reader of the same (stored, current) heads arrives mid-flight.
            reader = threading.Thread(target=lambda: second.append(_served(instance, later)))
            reader.start()
            readers.append(reader)
        return original(*args, **kwargs)

    facts._OVERLAYS.clear()
    with patch.object(facts, "_changes", changes):
        first = _served(instance, EVALUATION_TIME)
        for reader in readers:
            reader.join(60)
    assert calls == [1]
    assert first is not None and second and second[0] is not None
    _assert_equivalent(instance, later)


def test_holds_for_a_few_claims_read_only_their_keys(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    _contend(instance, owner)
    first = _current_claim(instance)
    _attest(instance, owner, first, tmp_path, at=EVALUATION_TIME - timedelta(minutes=1))
    _drain(instance)
    asked: list[Any] = []
    original = rows.stored_rows

    def stored(connection, keys=None):  # type: ignore[no-untyped-def]
        asked.append(None if keys is None else set(keys))
        return original(connection, keys)

    with patch.object(rows, "stored_rows", stored), _no_fold():
        claim_unsure_holds(
            instance,
            coordinate=instance.accepted_coordinate(),
            claims=(first,),
            evaluation_time=EVALUATION_TIME,
        )
    identity = first.identity.qualified
    assert asked == [
        {
            (rows.THRESHOLD, identity),
            (rows.DOOR, identity),
            (rows.DEPENDENCY, identity),
            (rows.SLOT, rows.slot_key(first, qualifier=True)),
        }
    ]
    _assert_equivalent(instance, EVALUATION_TIME)


@contextmanager
def _keys_derived() -> Iterator[list[set[tuple[str, str]]]]:
    derived: list[set[tuple[str, str]]] = []
    original = rows.derive

    def counting(context, keys, delta):  # type: ignore[no-untyped-def]
        keys = set(keys)
        derived.append(keys)
        return original(context, keys, delta)

    with patch.object(rows, "derive", counting):
        yield derived


def test_layer2_derives_exactly_the_keys_a_change_reaches(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    _drain(instance)
    first = _current_claim(instance)
    identity = first.identity.qualified
    # A body nothing reads reaches no key.
    instance.body_store().store(b"a body no Claim consults\n")
    with _keys_derived() as derived:
        _drain(instance)
    assert derived == [set()]
    # A door event reaches its Claim's own keys and its slot, nothing else.
    _attest(instance, owner, first, tmp_path, at=EVALUATION_TIME - timedelta(minutes=1))
    with _keys_derived() as derived:
        _drain(instance)
    assert derived == [
        {
            (rows.THRESHOLD, identity),
            (rows.DOOR, identity),
            (rows.SOURCE, identity),
            (rows.SLOT, rows.slot_key(first, qualifier=True)),
        }
    ]
    assert facts.stored_tables(instance) == facts.rebuilt_tables(instance, at=EVALUATION_TIME)
    # A read at the head derives nothing either.
    with _keys_derived() as derived:
        assert _served(instance, EVALUATION_TIME) is not None
    assert derived == [set()]


def test_an_unservable_fact_is_derived_on_every_read_and_never_persisted(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    _drain(instance)
    identity = _current_claim(instance).identity.qualified
    with sqlite3.connect(facts._STATE.path(instance)) as connection:
        connection.execute("UPDATE claim_facts SET unservable=1 WHERE claim_id=?", (identity,))
    # Not current for orient's counts either.
    assert (
        facts.claim_status_tally(
            instance,
            coordinate=instance.accepted_coordinate(),
            evaluation_time=EVALUATION_TIME,
        )
        is None
    )
    derived: list[str] = []
    original = facts._claim_facts

    def counting(instance, context, claim, **kwargs):  # type: ignore[no-untyped-def]
        derived.append(claim.identity.qualified)
        return original(instance, context, claim, **kwargs)

    with patch.object(facts, "_claim_facts", counting):
        assert _served(instance, EVALUATION_TIME) is not None
    assert derived == [identity]
    _assert_equivalent(instance, EVALUATION_TIME)
    with sqlite3.connect(facts._STATE.path(instance)) as connection:
        assert connection.execute(
            "SELECT unservable FROM claim_facts WHERE claim_id=?", (identity,)
        ).fetchone() == (1,)


def _outcome(instance, at: datetime):  # type: ignore[no-untyped-def]
    request = NextRequest(evaluation_time=at, access_profile=_access())
    try:
        return service_playbill_next(instance, request=request).model_dump_json()
    except CruxibleError as exc:  # the refusal's class and code are the outcome (R4)
        return (type(exc).__name__, getattr(exc, "code", None) or getattr(exc, "error_code", None))


def test_a_broken_key_refuses_as_the_fold_refuses(tmp_path: Path) -> None:
    from tests.test_evidence.test_slot_verdict_reuse import _rewrite_in_place

    instance, _owner = seed_claims(tmp_path)
    claim = _current_claim(instance)
    _rewrite_in_place(
        instance.body_store()._path(claim.backing.capture_digests[0]), keep_mtime=False
    )
    # The worker records each broken Claim and key; it does not stall.
    _drain(instance)
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"
    served = _outcome(instance, EVALUATION_TIME)
    with _oracle_only():
        oracle = _outcome(instance, EVALUATION_TIME)
    assert isinstance(oracle, tuple) and oracle[0] == "CasError"
    assert served == oracle


def test_a_failed_pass_is_not_retried_until_an_input_changes(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    manager = SimpleNamespace(get=lambda _id: instance)
    (work,) = WORKER.due(instance, now=EVALUATION_TIME)
    with patch.object(facts, "_rebuild", side_effect=OSError("unreadable")) as rebuild:
        with pytest.raises(OSError, match="unreadable"):
            WORKER.run(manager, "instance", work, now=EVALUATION_TIME)
        for days in (0, 1, 99):
            _drain(instance, EVALUATION_TIME + timedelta(days=days))
        assert rebuild.call_count == 1
    (health,) = WORKER.health(instance, now=EVALUATION_TIME)
    assert health.state == "stalled" and health.repair is not None
    assert health.repair.required_change == "resolve_the_worker_error_then_rebuild_the_next_facts"
    assert _served(instance, EVALUATION_TIME) is None
    instance.body_store().store(b"new input after failure")
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="restart")
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME))
    _drain(instance)
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"
    assert _served(instance, EVALUATION_TIME) is not None


def test_a_change_during_a_pass_leaves_the_worker_due(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    _drain(instance)
    instance.body_store().store(b"first new input")
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    (work,) = WORKER.due(instance, now=EVALUATION_TIME)
    original = facts._settle

    def moved(*args, **kwargs):  # type: ignore[no-untyped-def]
        original(*args, **kwargs)
        instance.body_store().store(b"material landed during the pass")

    with patch.object(facts, "_settle", moved):
        WORKER.run(SimpleNamespace(get=lambda _id: instance), "instance", work, now=EVALUATION_TIME)
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "lagging"
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME))
    _assert_equivalent(instance, EVALUATION_TIME)
    _drain(instance)
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"


def test_unchanged_targets_match_without_a_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    statements: list[str] = []
    original = facts._STATE.open

    @contextmanager
    def traced(*args, **kwargs):  # type: ignore[no-untyped-def]
        with original(*args, **kwargs) as connection:
            assert connection is not None
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(facts._STATE, "open", traced)
    for tick in range(3):
        WORKER.match(instance, now=EVALUATION_TIME + timedelta(seconds=tick), daemon_id="restart")
    assert statements
    assert all(statement.lstrip().split()[0] == "SELECT" for statement in statements)


def test_request_observations_merge_with_stored_rows_exactly(tmp_path: Path) -> None:
    from tests.test_discovery.test_next_request_reads import _rich_request
    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, _owner = _instance_with_query(tmp_path)
    request = _rich_request(instance)
    _drain(instance, request.evaluation_time)
    assert _served(instance, request.evaluation_time) is not None
    served = service_playbill_next(instance, request=request)
    with _oracle_only():
        oracle = service_playbill_next(instance, request=request)
    assert served.model_dump_json() == oracle.model_dump_json()


def test_orient_profile_is_served_inside_and_across_an_interval_edge(tmp_path: Path) -> None:
    from cruxible_core.service.discovery.orient import _NEXT_PROFILE

    instance, _owner = _freshness_world(tmp_path)
    at = datetime.fromisoformat("2026-08-16T20:00:00+00:00")
    assert set(_NEXT_PROFILE.permitted_access_classes) == {"instance", "public"}
    _drain(instance, at)
    request = NextRequest(evaluation_time=at, access_profile=_NEXT_PROFILE)
    with _no_fold():
        service_playbill_next(instance, request=request)
        summarize_playbill_next(instance, request=request)
    edge = next(instant for instant in _edges(instance, at) if instant > at)
    later = request.model_copy(update={"evaluation_time": edge})
    with _no_fold():
        served = service_playbill_next(instance, request=later)
        summary = summarize_playbill_next(instance, request=later)
    with _oracle_only():
        assert served == service_playbill_next(instance, request=later)
        assert summary == summarize_playbill_next(instance, request=later)


def test_a_historical_admission_account_capture_is_a_read_of_its_claim(tmp_path: Path) -> None:
    from cruxible_core.service.discovery.claim_status import reset_claim_resolution_memo
    from tests.core_support._support import client_material
    from tests.test_claims.test_claim_type_v7_revisions import _V7World, v7_type

    v7 = _V7World(tmp_path)
    v7.seed(v7_type())
    claim_id = v7.say(b"status: ready")
    old_capture = v7.claim(claim_id).backing.capture_digests[0]
    v7.say(b"status: done", value="done", claim_ref=claim_id)
    current = v7.claim(claim_id)
    assert old_capture not in current.backing.capture_digests
    instance = v7.instance
    owner = client_material(instance.root.parent, instance, principal_id="owner")
    _attest(
        instance,
        owner,
        current,
        tmp_path,
        at=EVALUATION_TIME,
        basis="new_capture",
        stance="support",
        captures=(old_capture,),
    )
    reset_claim_resolution_memo()
    _drain(instance)
    tables = facts.stored_tables(instance)
    assert tables is not None
    # Read only by the door rows' historical admission accounts: its landing reaches them.
    assert (f"capture:{old_capture}", current.identity.qualified) in set(tables["fact_reads"])
    _assert_equivalent(instance, EVALUATION_TIME)
