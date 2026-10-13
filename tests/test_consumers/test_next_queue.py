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
    # A read that pins a door head the worker has passed is historical: its pages
    # fold at those pins.
    _drain(instance, request.evaluation_time)
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
    path = facts._STATE.path(instance)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alien(value TEXT)")
    stored = path.read_bytes()
    # A read folds and leaves the incompatible state exactly as it was (R3):
    # only the worker rebuilds it.
    assert _served(instance, EVALUATION_TIME) is None
    assert (
        facts.claim_status_tally(
            instance, coordinate=instance.accepted_coordinate(), evaluation_time=EVALUATION_TIME
        )
        is None
    )
    _assert_equivalent(instance, EVALUATION_TIME, served=False)
    assert path.read_bytes() == stored
    # A missing state is not created by a read either.
    path.unlink()
    assert _served(instance, EVALUATION_TIME) is None
    assert not path.exists()
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
    # An examined door event leaves the Claim's facts as they were: it reaches its
    # threshold, which reads it, and no family that reads only facts (early cutoff).
    before = facts.stored_tables(instance)
    assert before is not None
    _attest(instance, owner, first, tmp_path, at=EVALUATION_TIME - timedelta(minutes=1))
    with _keys_derived() as derived:
        _drain(instance)
    after = facts.stored_tables(instance)
    assert after is not None and after["claim_facts"] == before["claim_facts"]
    assert derived == [{(rows.THRESHOLD, identity)}]
    assert facts.stored_tables(instance) == facts.rebuilt_tables(instance, at=EVALUATION_TIME)
    # A read at the head derives nothing either.
    with _keys_derived() as derived:
        assert _served(instance, EVALUATION_TIME) is not None
    assert derived == [set()]


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
    assert health.repair.required_change == (
        "resolve_the_worker_error_then_run_cruxible_instance_rebuild_next"
    )
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


def test_concurrent_behind_reads_derive_the_dirty_keys_once(tmp_path: Path) -> None:
    """The first behind-read settles Layer 1 and 2 once per (stored, current) heads;
    every later read of the same pair replays that settlement and derives nothing."""

    instance, owner = seed_claims(tmp_path)
    _drain(instance)
    _attest(
        instance,
        owner,
        _current_claim(instance),
        tmp_path,
        at=EVALUATION_TIME - timedelta(minutes=1),
    )
    facts._OVERLAYS.clear()
    with _keys_derived() as derived:
        first = _served(instance, EVALUATION_TIME)
        second = _served(instance, EVALUATION_TIME)
    assert first is not None and second is not None
    assert [keys for keys in derived if keys] == [derived[0]] and derived[0]
    assert first.rows == second.rows
    _assert_equivalent(instance, EVALUATION_TIME)


# -- bodies rewritten in place (R2): caught by the daily sweep -----------------------


def _rewrite_backing(instance, *, keep_mtime: bool) -> bytes:  # type: ignore[no-untyped-def]
    from tests.test_evidence.test_slot_verdict_reuse import _rewrite_in_place

    path = instance.body_store()._path(_current_claim(instance).backing.capture_digests[0])
    original = path.read_bytes()
    _rewrite_in_place(path, keep_mtime=keep_mtime)
    return original


def _sweep(instance, at: datetime) -> None:  # type: ignore[no-untyped-def]
    from tests.support.internal_triggers import fire_internal_triggers

    assert any(
        event.action == facts.EVIDENCE_SWEEP for event in fire_internal_triggers(instance, now=at)
    )
    _drain(instance, at)


@pytest.mark.parametrize("keep_mtime", (False, True), ids=("rewrite", "rewrite-keep-mtime"))
def test_a_body_rewritten_in_place_reaches_its_claims_at_the_next_sweep(
    tmp_path: Path, keep_mtime: bool
) -> None:
    instance, _owner = seed_claims(tmp_path)
    _drain(instance)
    original = _rewrite_backing(instance, keep_mtime=keep_mtime)
    # No log records an in-place rewrite: until the sweep the stored rows stand (R2).
    _sweep(instance, EVALUATION_TIME)
    served = _outcome(instance, EVALUATION_TIME)
    with _oracle_only():
        assert served == _outcome(instance, EVALUATION_TIME)
    assert served[0] == "CasError"
    # Repaired in place: no record either, and the next sweep follows it back.
    instance.body_store()._path(_current_claim(instance).backing.capture_digests[0]).write_bytes(
        original
    )
    later = EVALUATION_TIME + timedelta(days=1)
    _sweep(instance, later)
    assert facts.stored_tables(instance) == facts.rebuilt_tables(instance, at=later)
    _assert_equivalent(instance, later)


def test_a_door_row_follows_the_repair_of_an_account_capture_it_could_not_read(
    tmp_path: Path,
) -> None:
    """A door row whose Claim's citation envelope was corrupt in place read its accounts
    as incomplete. That envelope is a read of the door key all the same, so the sweep
    after its in-place repair reaches the door: behind and caught up, it reads proven."""

    from tests.support.internal_triggers import fire_internal_triggers
    from tests.test_consumers.test_next_incremental import World, _door_capture

    instance, owner = seed_claims(tmp_path)
    _drain(instance)
    claim = _current_claim(instance)
    state = World(instance=instance, owner=owner, root=tmp_path, rng=None)  # type: ignore[arg-type]
    _attest(
        instance,
        owner,
        claim,
        tmp_path,
        at=EVALUATION_TIME - timedelta(minutes=1),
        basis="new_capture",
        stance="unsure",
        captures=(_door_capture(state, claim.identity.name),),
    )
    _drain(instance)
    original = _rewrite_backing(instance, keep_mtime=False)
    _sweep(instance, EVALUATION_TIME)
    instance.body_store()._path(claim.backing.capture_digests[0]).write_bytes(original)
    later = EVALUATION_TIME + timedelta(days=1)
    assert any(
        event.action == facts.EVIDENCE_SWEEP
        for event in fire_internal_triggers(instance, now=later)
    )
    _assert_equivalent(instance, later)
    _drain(instance, later)
    assert facts.stored_tables(instance) == facts.rebuilt_tables(instance, at=later)
    statuses = {
        item.detail.get("lineage_status")
        for item in service_playbill_next(
            instance, request=NextRequest(evaluation_time=later, access_profile=_access())
        ).items
        if item.reason == "claim_new_evidence_unreviewed"
    }
    assert statuses == {"proven"}


def test_a_retired_dependency_capture_rewritten_in_place_is_caught_by_the_sweep(
    world,  # noqa: F811
) -> None:
    from tests.core_support._retirement_support import retire_claim
    from tests.test_evidence.test_slot_verdict_reuse import _rewrite_in_place

    derived = _run_and_accept(world)
    retire_claim(world.instance, world.owner, derived.identity.name)
    instance = world.instance
    _drain(instance, _DERIVED_AT)
    capture = derived.backing.capture_digests[0]
    tables = facts.stored_tables(instance)
    assert tables is not None
    # Read by the retired Claim's own facts, which the dependency fold reads.
    assert any(row[1] == capture for row in tables["capture_bodies"])
    _rewrite_in_place(instance.body_store()._path(capture), keep_mtime=True)
    _sweep(instance, _DERIVED_AT)
    served = _outcome(instance, _DERIVED_AT)
    with _oracle_only():
        assert served == _outcome(instance, _DERIVED_AT)


# -- undeclared kinds and expiring mandates from maintained counts (R9) --------------


def test_undeclared_kinds_and_expiring_mandates_come_from_the_maintained_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cruxible_core.proposals.proposals as proposals_module
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.authoring.inputs import SubjectKindInput, lower_authoring_input
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_digest,
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.artifacts import render_procedure
    from cruxible_client.contracts.subjects import render_subject, subject_path
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from tests.core_support._identity_support import shell
    from tests.core_support._support import initialize_local
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_procedure_mandates import _mandate, _procedure

    instance, owner = initialize_local(tmp_path)
    # A Subject accepted before Subject identity existed (an older build).
    with monkeypatch.context() as patched:
        patched.setattr(proposals_module, "boundary_identity_issues", lambda *_a, **_k: ())
        base = instance.accepted_coordinate()
        raw = instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=ProposalAdmissionRequest(
                target_ref="refs/proposals/owner/pre-subject-kinds", proposed_base_oid=base.git_oid
            ),
            candidate_tree={
                **instance.tree_at(base.git_oid),
                subject_path("dev.legacy", "old"): render_subject(shell("old", "dev.legacy")),
            },
            timestamp="2026-08-24T17:00:00.000000Z",
        )
        assert raw.candidate is not None, raw.evaluation.diagnostics
        service_activate_playbill_proposal(
            instance, proposal_id=raw.admission.proposal_id, activated_by="owner"
        )
    procedure = _procedure()
    lapsing = _mandate(procedure=procedure).model_copy(
        update={"expires_at": datetime(2026, 8, 27, tzinfo=EVALUATION_TIME.tzinfo)}
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            procedure.path: render_procedure(procedure.procedure),
            procedure_mandate_path("triage"): render_procedure_mandate(lapsing),
        },
        timestamp="2026-08-24T17:00:01.000000Z",
        proposal_name="served-mandate",
    )
    _drain(instance)
    found = _served(instance, EVALUATION_TIME)
    assert found is not None
    assert found.subject_kinds == (("dev.legacy", 1, "subjects/dev.legacy/old.json"),)
    assert found.expiring_mandates == ("ProcedureMandate:triage",)
    request = NextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access())
    reasons = {item.reason for item in service_playbill_next(instance, request=request).items}
    assert {"subject_kind_undeclared", "mandate_expiring"} <= reasons
    _assert_equivalent(instance, EVALUATION_TIME)
    one_day = CanonicalDuration(microseconds=86_400_000_000)
    _assert_equivalent(instance, EVALUATION_TIME, expiring_within=one_day)

    # Behind: the renewal and the declaration are derived on read, then followed.
    renewed = lapsing.model_copy(
        update={
            "expires_at": datetime(2027, 8, 27, tzinfo=EVALUATION_TIME.tzinfo),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(lapsing).tagged
            ),
        }
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            procedure_mandate_path("triage"): render_procedure_mandate(renewed),
        },
        timestamp="2026-08-24T17:00:02.000000Z",
        proposal_name="served-mandate-renewal",
    )
    submitted = AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=lower_authoring_input(
            SubjectKindInput(kind="subject_kind", name="dev.legacy", minted=True)
        ),
        canonical_timestamp="2026-08-24T17:00:03.000000Z",
    )
    assert submitted.status.proposal_id is not None
    service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    _assert_equivalent(instance, EVALUATION_TIME)
    _drain(instance)
    found = _served(instance, EVALUATION_TIME)
    assert found is not None and found.expiring_mandates == ()
    reasons = {item.reason for item in service_playbill_next(instance, request=request).items}
    assert not {"subject_kind_undeclared", "mandate_expiring"} & reasons
    assert facts.stored_tables(instance) == facts.rebuilt_tables(instance, at=EVALUATION_TIME)


# -- review fixes: one input state, maintained citations and counts, error order -----


def _stale_evidence_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Two Claims whose evidence is expired; the first's cited body erased, the worker
    behind it: whether each body is present decides its stale-evidence row."""

    from cruxible_client.contracts.claim_types import ClaimType
    from tests.test_claims.test_claims import _claim_type

    claim_type = ClaimType.model_validate(
        {
            **_claim_type().model_dump(mode="json"),
            "artifact_format": "playbill-claim-type-v3",
            "evidence_freshness": {"stale_after": {"microseconds": 1_000_000}},
        }
    )
    instance, owner = seed_claims(tmp_path, claim_type_override=claim_type)
    _drain(instance)
    bodies = {
        _current_claim(instance, subject_id="wi-42").identity.qualified: b"status: ready",
        _current_claim(instance, subject_id="wi-43").identity.qualified: b"status: blocked",
    }
    store = instance.body_store()
    store.erase(store.digest_bytes(bodies[min(bodies)]).tagged)
    return instance, owner, bodies


def _stale(items) -> set[str]:  # type: ignore[no-untyped-def]
    return {item.subject_identity for item in items if item.reason == "claim_stale_evidence"}


@contextmanager
def _after_recording(write: Any) -> Iterator[list[Any]]:
    """Run ``write`` once, right after the first read records its heads: mid-read."""

    recorded: list[Any] = []
    original = facts.read_heads

    def recording(*args, **kwargs):  # type: ignore[no-untyped-def]
        heads = original(*args, **kwargs)
        if not recorded:
            recorded.append(heads)
            write()
        return heads

    with patch.object(facts, "read_heads", recording):
        yield recorded


@pytest.mark.parametrize("move", ("bodies", "accepted", "door"))
def test_writes_landing_mid_read_are_not_in_its_answer(tmp_path: Path, move: str) -> None:
    """Ruling 1: a read is a snapshot of the heads it records at its start. Bodies
    restored and erased, a generation accepted or a door event appended once it has
    recorded them neither change its answer nor fail it, and nothing folds."""

    from tests.test_consumers.test_next_incremental import World, _subject

    instance, owner, bodies = _stale_evidence_world(tmp_path)
    first, second = sorted(bodies)
    store = instance.body_store()
    state = World(instance=instance, owner=owner, root=tmp_path, rng=None)  # type: ignore[arg-type]
    request = NextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access())
    with _oracle_only():
        before = service_playbill_next(instance, request=request)
    coordinate = instance.accepted_coordinate()
    door = instance.claim_attestation_evidence_store().head()

    def write() -> None:
        store.store(bodies[first])
        store.erase(store.digest_bytes(bodies[second]).tagged)
        if move == "accepted":
            _subject(state)
        elif move == "door":
            _attest(
                instance,
                owner,
                _all_claims(instance)[0],
                tmp_path,
                at=EVALUATION_TIME - timedelta(minutes=1),
                stance="support",
            )

    facts._OVERLAYS.clear()
    with _after_recording(write) as recorded, _no_fold():
        served = service_playbill_next(instance, request=request)
    assert recorded
    assert served.coordinate == AcceptedCoordinate.from_internal(coordinate)
    assert served.attestation_head_digest == door
    assert served.items == before.items
    assert _stale(served.items) == _stale(before.items) and _stale(before.items)
    # The writes are in the next read, which records later heads.
    assert (instance.accepted_coordinate() != coordinate) == (move == "accepted")
    _assert_equivalent(instance, EVALUATION_TIME)


def test_a_body_landed_after_the_reads_position_is_absent_to_that_read(tmp_path: Path) -> None:
    """Ruling 2: presence is the landing log's at the read's landing position. A body
    that lands after the read recorded it is absent to that read (served and folded
    alike), whatever the file says, and present to the next read."""

    instance, _owner, bodies = _stale_evidence_world(tmp_path)
    _drain(instance)
    first = min(bodies)
    store = instance.body_store()
    digest = store.digest_bytes(bodies[first]).tagged
    request = NextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access())
    with _oracle_only():
        before = service_playbill_next(instance, request=request)
    with _after_recording(lambda: store.store(bodies[first])) as recorded, _no_fold():
        served = service_playbill_next(instance, request=request)
    (heads,) = recorded
    assert store.verify(digest)
    assert not store.landings.present(digest, identity=heads.landing_log, sequence=heads.landing)
    assert served.items == before.items and first not in _stale(served.items)
    # The fold at the same recorded heads answers the same.
    with _oracle_only(), patch.object(facts, "read_heads", return_value=heads):
        assert service_playbill_next(instance, request=request).items == served.items
    later = service_playbill_next(instance, request=request)
    assert _stale(later.items) == _stale(before.items) | {first}
    _assert_equivalent(instance, EVALUATION_TIME)


def _reobservation_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    from tests.test_evidence.test_claim_reobservations import _HOUR, _START
    from tests.test_evidence.test_claim_reobservations import _freshness_world as freshness_world

    instance, _owner = freshness_world(tmp_path, stale_after=_HOUR)
    at = _START + timedelta(hours=1.2)
    _drain(instance, at)
    return instance, at, NextRequest(evaluation_time=at, access_profile=_access())


def _reobserve_mid_read(instance) -> None:  # type: ignore[no-untyped-def]
    from tests.test_evidence.test_claim_reobservations import _START, _reobserve

    _reobserve(instance, _START + timedelta(minutes=50))


def test_a_fold_at_older_positions_does_not_store_its_verdicts_for_a_later_read(
    tmp_path: Path,
) -> None:
    """F-015: a fold at a read's recorded positions names its remembered verdicts by
    those positions. Folded again at the older positions after a re-observation
    landed, it answers as of them, and the next read (at the later positions) is
    not served that older answer."""

    instance, _at, request = _reobservation_world(tmp_path)
    with _oracle_only():
        before = service_playbill_next(instance, request=request)
    with _after_recording(lambda: _reobserve_mid_read(instance)) as recorded, _no_fold():
        served = service_playbill_next(instance, request=request)
    assert recorded and served.items == before.items
    with _oracle_only(), patch.object(facts, "read_heads", return_value=recorded[0]):
        assert service_playbill_next(instance, request=request).items == served.items
    later = service_playbill_next(instance, request=request)
    assert later.items != before.items
    with _oracle_only():
        assert (
            service_playbill_next(instance, request=request).model_dump_json()
            == later.model_dump_json()
        )


def test_a_fold_at_older_positions_is_not_served_a_later_reads_verdicts(
    tmp_path: Path,
) -> None:
    """F-015: a read records its positions, a re-observation lands and a later read
    remembers its verdicts; the first read, folding on, answers as of its own
    positions, not from the later read's entry."""

    instance, _at, request = _reobservation_world(tmp_path)
    with _oracle_only():
        before = service_playbill_next(instance, request=request)
    later: list[Any] = []

    def land_and_read_later() -> None:
        _reobserve_mid_read(instance)
        later.append(service_playbill_next(instance, request=request))

    with _oracle_only(), _after_recording(land_and_read_later) as recorded:
        actual = service_playbill_next(instance, request=request)
    assert recorded and later and later[0].items != before.items
    assert actual.items == before.items


def test_a_read_during_a_landing_in_flight_answers_as_before_it(tmp_path: Path) -> None:
    """Between a landing's intent and its completion the body has not landed for a read:
    the read serves (no fold, no refusal) as of its position, where it is absent."""

    from cruxible_core.storage.landing_log import LandingLog

    instance, _owner = seed_claims(tmp_path)
    _drain(instance)
    store = instance.body_store()
    content = b"landing while a read runs\n"
    digest = store.digest_bytes(content).tagged
    original = LandingLog._append
    seen: list[Any] = []

    def in_flight(log, kind, landed, *, sync):  # type: ignore[no-untyped-def]
        result = original(log, kind, landed, sync=sync)
        if kind == "store" and not seen:
            with _no_fold():
                seen.append(_served(instance, EVALUATION_TIME))
        return result

    with patch.object(LandingLog, "_append", in_flight):
        store.store(content)
    (served,) = seen
    assert served is not None and served.heads is not None
    assert not store.landings.present(
        digest, identity=served.heads.landing_log, sequence=served.heads.landing
    )
    head = store.landings.head()
    assert store.landings.present(digest, identity=head.identity, sequence=head.sequence)
    _assert_equivalent(instance, EVALUATION_TIME)


def test_reads_never_retry_or_refuse_while_landings_keep_arriving(tmp_path: Path) -> None:
    """Ruling 1: a landing during every derivation used to exhaust a read's attempts.
    next, the summary, orient and the hold flags now each derive once at the heads
    they recorded and answer, folding nothing."""

    from cruxible_core.service.discovery.orient import service_playbill_orient

    instance, _owner, _bodies = _stale_evidence_world(tmp_path)
    store = instance.body_store()
    original = facts._changes
    derived: list[int] = []
    landings = iter(range(1_000))

    def landing_meanwhile(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)
        derived.append(1)
        # New bytes each time: a body already present lands nothing.
        store.store(f"uncited racing landing {next(landings)}".encode())
        return result

    request = NextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access())
    reads = (
        lambda: service_playbill_next(instance, request=request),
        lambda: summarize_playbill_next(instance, request=request),
        lambda: service_playbill_orient(instance, evaluation_time=EVALUATION_TIME),
        lambda: claim_unsure_holds(
            instance,
            coordinate=instance.accepted_coordinate(),
            claims=tuple(_all_claims(instance)),
            evaluation_time=EVALUATION_TIME,
        ),
    )
    for read in reads:
        derived.clear()
        facts._OVERLAYS.clear()
        with (
            patch.object(facts, "_changes", landing_meanwhile),
            patch(
                "cruxible_core.service.discovery.claim_status.claim_resolution_statuses",
                side_effect=AssertionError("population fold"),
            ),
            _no_fold(),
        ):
            read()
        assert derived
    _assert_equivalent(instance, EVALUATION_TIME)


def test_a_pin_the_worker_passed_folds_and_orient_reads_at_the_head_it_selected(
    tmp_path: Path,
) -> None:
    """R8 with snapshots: a coordinate pin the worker has already followed past folds
    there; orient reads at the coordinate it selected even when a generation is
    accepted mid-read, served without a population fold."""

    from cruxible_core.service.discovery.orient import service_playbill_orient
    from tests.test_consumers.test_next_incremental import World, _subject

    instance, owner, _bodies = _stale_evidence_world(tmp_path)
    state = World(instance=instance, owner=owner, root=tmp_path, rng=None)  # type: ignore[arg-type]
    pinned = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    request = NextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access(), at=pinned)
    with _oracle_only():
        expected = service_playbill_next(instance, request=request)
    _subject(state)
    _drain(instance)
    with patch.object(
        next_module, "_oracle_claim_rows", wraps=next_module._oracle_claim_rows
    ) as fold:
        historical = service_playbill_next(instance, request=request)
        assert fold.called
    assert historical.coordinate == pinned and historical.items == expected.items

    instance.body_store().store(b"an uncited landing: the worker is behind")
    selected = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    with (
        _after_recording(lambda: _subject(state)) as recorded,
        patch(
            "cruxible_core.service.discovery.claim_status.claim_resolution_statuses",
            side_effect=AssertionError("population fold"),
        ),
        _no_fold(),
    ):
        oriented = service_playbill_orient(instance, evaluation_time=EVALUATION_TIME)
    assert recorded and oriented.coordinate == selected
    assert AcceptedCoordinate.from_internal(instance.accepted_coordinate()) != selected


def test_live_state_read_after_the_stored_rows_reads_at_their_positions(tmp_path: Path) -> None:
    """A projection check evaluates its query after the stored rows were read; a
    landing in between is not in the answer, which neither refuses nor folds."""

    from cruxible_core.service.authoring.projection_sync import ProjectionCheckContext
    from tests.test_discovery.test_next_request_reads import _rich_request
    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, _owner = _instance_with_query(tmp_path)
    request = _rich_request(instance)
    _drain(instance, request.evaluation_time)
    with _oracle_only():
        before = service_playbill_next(instance, request=request)
    query = ProjectionCheckContext._query

    def landing_meanwhile(self, backing):  # type: ignore[no-untyped-def]
        instance.body_store().store(b"a landing while the projection query runs")
        return query(self, backing)

    with patch.object(ProjectionCheckContext, "_query", landing_meanwhile), _no_fold():
        served = service_playbill_next(instance, request=request)
    assert served.items == before.items
    warm = service_playbill_next(instance, request=request)
    with _oracle_only():
        assert (
            warm.model_dump_json()
            == service_playbill_next(instance, request=request).model_dump_json()
        )


def test_a_summary_pinned_only_at_a_door_head_is_read_at_that_head(tmp_path: Path) -> None:
    """F-013: a summary naming an attestation head and no coordinate reads at that head
    (folded once the worker is past it) and at the coordinate now; a current door pin
    serves, and a generation accepted mid-read is not in its answer."""

    from tests.test_consumers.test_next_incremental import World, _subject

    instance, owner = seed_claims(tmp_path)
    _drain(instance)
    store = instance.claim_attestation_evidence_store()
    old_door = store.head()
    coordinate = instance.accepted_coordinate()
    _attest(
        instance,
        owner,
        _current_claim(instance),
        tmp_path,
        at=EVALUATION_TIME - timedelta(minutes=1),
    )
    _drain(instance)
    assert instance.accepted_coordinate() == coordinate and store.head() != old_door
    historical = NextRequest(
        evaluation_time=EVALUATION_TIME,
        access_profile=_access(),
        at_attestation_head_digest=old_door,
    )
    summary = summarize_playbill_next(instance, request=historical)
    with _oracle_only():
        assert summary == summarize_playbill_next(instance, request=historical)

    current = historical.model_copy(update={"at_attestation_head_digest": store.head()})
    with _oracle_only():
        expected = summarize_playbill_next(instance, request=current)
    state = World(instance=instance, owner=owner, root=tmp_path, rng=None)  # type: ignore[arg-type]
    with _after_recording(lambda: _subject(state)) as recorded, _no_fold():
        assert summarize_playbill_next(instance, request=current) == expected
    assert recorded and instance.accepted_coordinate() != coordinate


@contextmanager
def _maintained_workspace_reads() -> Iterator[list[tuple[str, ...] | None]]:
    """No citation inventory fold, no backed-Claim verdict derivation, and the query facts
    each projection check builds: scoped to its predicates, never the whole population."""

    from cruxible_core.service.authoring.projection_sync import ProjectionCheckContext
    from cruxible_core.service.discovery import query as query_module

    build = query_module._AcceptedQueryFactsRead.build
    scopes: list[tuple[str, ...] | None] = []

    def scoped(self, **kwargs):  # type: ignore[no-untyped-def]
        scopes.append(self._predicates)
        return build(self, **kwargs)

    with (
        patch.object(
            next_module, "_citation_commitments", side_effect=AssertionError("inventory fold")
        ),
        patch.object(
            ProjectionCheckContext,
            "_resolve_backed_claims",
            side_effect=AssertionError("backing verdicts derived"),
        ),
        patch.object(query_module._AcceptedQueryFactsRead, "build", scoped),
    ):
        yield scopes


def test_workspace_reads_take_citations_from_the_maintained_table(tmp_path: Path) -> None:
    """F-007: warm and behind workspace reads never rebuild the citation inventory, take
    their projection backings' statuses and visibility from the facts, and evaluate each
    backing query over its own predicates only."""

    from tests.test_discovery.test_next_request_reads import _rich_request
    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, owner = _instance_with_query(tmp_path)
    request = _rich_request(instance)
    _drain(instance, request.evaluation_time)
    with _maintained_workspace_reads() as scopes:
        warm = service_playbill_next(instance, request=request)
    with _oracle_only():
        assert (
            warm.model_dump_json()
            == service_playbill_next(instance, request=request).model_dump_json()
        )
    assert scopes and None not in scopes
    # Behind: a door event the worker has not followed.
    _attest(
        instance,
        owner,
        _all_claims(instance)[0],
        tmp_path,
        at=request.evaluation_time - timedelta(minutes=1),
    )
    with _maintained_workspace_reads() as scopes:
        behind = service_playbill_next(instance, request=request)
    assert scopes and None not in scopes
    with _oracle_only():
        assert (
            behind.model_dump_json()
            == service_playbill_next(instance, request=request).model_dump_json()
        )
    # A citation the facts could not read refuses the inventory, as the fold does.
    _drain(instance, request.evaluation_time)
    with sqlite3.connect(facts._STATE.path(instance)) as connection:
        connection.execute("UPDATE claim_facts SET citation_error='CasError'")
    with (
        _maintained_workspace_reads(),
        pytest.raises(next_module.NextAcceptedStateInvalid, match="citation"),
    ):
        service_playbill_next(instance, request=request)


def _vm_steps(connection: sqlite3.Connection, read: Any) -> int:
    count = [0]

    def tick() -> int:
        count[0] += 1
        return 0

    connection.set_progress_handler(tick, 1)
    try:
        read()
    finally:
        connection.set_progress_handler(None, 0)
    return count[0]


def test_source_observations_read_only_source_citations_by_index() -> None:
    """F-007: a source observation reads the commitments naming a source (every one is a
    row or a check), never the others: the same work at 100 and 10,000 of those."""

    steps = []
    for size in (100, 10_000):
        connection = sqlite3.connect(":memory:")
        connection.executescript(facts._SCHEMA)
        connection.executemany(
            "INSERT INTO citation_commitments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                (f"Claim:{i}", f"citation:{i}", "sha256:" + "a" * 64, 1, None, None)
                + (None, None, 0, None, None)
                for i in range(size)
            ),
        )
        connection.executemany(
            "INSERT INTO citation_commitments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                (f"Claim:s{i}", f"citation:s{i}", "sha256:" + "b" * 64, 1, "fixture.items")
                + ("sha256:" + "c" * 64, 0, 1, 0, None, None)
                for i in range(3)
            ),
        )
        found: list[Any] = []
        steps.append(
            _vm_steps(
                connection,
                lambda connection=connection: found.append(
                    facts._served_citations(connection, "sources")
                ),
            )
        )
        commitments, error = found[0]
        assert not error and sorted(commitments) == [f"citation:s{i}" for i in range(3)]
    assert steps[0] == steps[1], steps


def test_a_selected_read_seeks_only_its_own_broken_keys() -> None:
    """F-012: a read of a few Claims looks up their keys' errors one seek each; it never
    walks the instance's other broken keys."""

    steps = []
    for size in (100, 10_000):
        connection = sqlite3.connect(":memory:")
        connection.executescript(facts._SCHEMA)
        connection.executemany(
            "INSERT INTO row_keys VALUES (?,?,NULL,NULL,?,?)",
            ((rows.THRESHOLD, f"Claim:unrelated-{i}", "CasError", "x") for i in range(size)),
        )
        steps.append(
            _vm_steps(
                connection,
                lambda connection=connection: facts._refuse_errors(
                    None,
                    connection,
                    coordinate=None,  # type: ignore[arg-type]
                    evaluation_time=EVALUATION_TIME,
                    keys={(rows.THRESHOLD, "Claim:requested"), (rows.DOOR, "Claim:requested")},
                ),
            )
        )
    assert steps[0] == steps[1], steps


def test_orient_behind_the_worker_counts_from_the_overlay(tmp_path: Path) -> None:
    """F-008: public orient behind the worker never resolves the whole population."""

    from cruxible_core.service.discovery.claim_status import reset_claim_resolution_memo
    from cruxible_core.service.discovery.orient import service_playbill_orient

    instance, owner = seed_claims(tmp_path)
    _drain(instance)
    instance.body_store().store(b"an uncited landing")
    _contend(instance, owner)
    reset_claim_resolution_memo()
    with patch(
        "cruxible_core.service.discovery.claim_status.claim_resolution_statuses",
        side_effect=AssertionError("population fold"),
    ):
        served = service_playbill_orient(instance, evaluation_time=EVALUATION_TIME)
    with patch.object(facts, "claim_status_tally", return_value=None):
        folded = service_playbill_orient(instance, evaluation_time=EVALUATION_TIME)
    assert served.model_dump_json() == folded.model_dump_json()


def test_broken_keys_refuse_in_the_folds_order(tmp_path: Path) -> None:
    """F-009: thresholds before doors, doors in chain order, whatever the identities."""

    from cruxible_client.contracts.errors import CasError, ProposalIntegrityError
    from tests.test_consumers.test_next_incremental import World, _door_capture

    instance, owner = seed_claims(tmp_path)
    _drain(instance)
    claim = _current_claim(instance).identity.qualified
    with facts._STATE.open(instance) as connection:
        assert connection is not None
        for family in (rows.THRESHOLD, rows.DOOR):
            connection.execute(
                "INSERT OR REPLACE INTO row_keys VALUES (?,?,NULL,NULL,?,?)",
                (family, claim, "error", "injected"),
            )
    with (
        patch.object(
            next_module, "_claim_threshold_rows", side_effect=ProposalIntegrityError("first")
        ),
        patch.object(rows, "door_keys", side_effect=CasError("second")),
    ):
        served = _outcome(instance, EVALUATION_TIME)
        with _oracle_only():
            assert served == _outcome(instance, EVALUATION_TIME)
    assert served[0] == "ProposalIntegrityError"

    # Two door Claims observed in the reverse of their identities' order.
    state = World(instance=instance, owner=owner, root=tmp_path, rng=None)  # type: ignore[arg-type]
    later, earlier = sorted(_all_claims(instance), key=lambda c: c.identity.qualified)[::-1][:2]
    for target in (later, earlier):
        _attest(
            instance,
            owner,
            target,
            tmp_path,
            at=EVALUATION_TIME - timedelta(minutes=1),
            basis="new_capture",
            stance="support",
            captures=(_door_capture(state, target.identity.name),),
        )
    errors: dict[str, type[Exception]] = {
        later.identity.qualified: CasError,
        earlier.identity.qualified: ProposalIntegrityError,
    }
    fold_rows = next_module._DoorFold.rows

    def broken(self, envelope, **kwargs):  # type: ignore[no-untyped-def]
        raise errors[envelope.statement.claim_identity.qualified]("door")

    with patch.object(next_module._DoorFold, "rows", broken):
        _drain(instance)
        served = _outcome(instance, EVALUATION_TIME)
        with _oracle_only():
            assert served == _outcome(instance, EVALUATION_TIME)
    assert served[0] == "CasError"
    assert next_module._DoorFold.rows is fold_rows


def test_door_refusal_follows_the_first_failing_observation_across_claims(
    tmp_path: Path,
) -> None:
    """F-009: observations A1, B2, A3 in chain order; A1 succeeds, B2 and A3 fail. The
    fold stops at B2, so the stored read must too, though A's key comes first."""

    from cruxible_client.contracts.errors import CasError, ProposalIntegrityError
    from tests.test_consumers.test_next_incremental import World, _door_capture

    instance, owner = seed_claims(tmp_path)
    _drain(instance)
    state = World(instance=instance, owner=owner, root=tmp_path, rng=None)  # type: ignore[arg-type]
    first, second = sorted(_all_claims(instance), key=lambda c: c.identity.qualified)[:2]
    head = instance.claim_attestation_evidence_store().head()
    for index, target in enumerate((first, second, first)):
        _attest(
            instance,
            owner,
            target,
            tmp_path,
            at=EVALUATION_TIME - timedelta(minutes=3 - index),
            basis="new_capture",
            stance="support",
            captures=(_door_capture(state, target.identity.name),),
        )
    _head, events = instance.claim_attestation_evidence_store().events_after(head)
    assert [payload.attestation.statement.claim_identity for _, _, payload in events] == [
        first.identity,
        second.identity,
        first.identity,
    ]
    failures = {
        events[1][1].event_digest: CasError,
        events[2][1].event_digest: ProposalIntegrityError,
    }
    fold_rows = next_module._DoorFold.rows

    def broken(self, envelope, **kwargs):  # type: ignore[no-untyped-def]
        error = failures.get(kwargs["event_digest"])
        if error is not None:
            raise error("failure at this observation")
        return fold_rows(self, envelope, **kwargs)

    with patch.object(next_module._DoorFold, "rows", broken):
        _drain(instance)
        tables = facts.stored_tables(instance)
        assert tables is not None
        broken_doors = {
            row[1]: row[4] for row in tables["row_keys"] if row[0] == rows.DOOR and row[4]
        }
        assert set(broken_doors) == {first.identity.qualified, second.identity.qualified}
        served = _outcome(instance, EVALUATION_TIME)
        with _oracle_only():
            folded = _outcome(instance, EVALUATION_TIME)
    assert served == folded
    assert served[0] == "CasError"


@pytest.mark.parametrize("mutation", ("extra", "missing"))
def test_the_oracle_rejects_extra_and_missing_layer2_keys(tmp_path: Path, mutation: str) -> None:
    """F-011: the oracle's expected keys are its own; a dirty-key set that adds an
    unaffected family or drops a needed one fails it."""

    from tests.test_consumers import test_next_incremental as oracle

    instance, owner = seed_claims(tmp_path)
    state = oracle.World(
        instance=instance,
        owner=owner,
        root=tmp_path,
        rng=__import__("random").Random(0),
        at=EVALUATION_TIME,
    )
    oracle._tick(state)
    before = oracle._snapshot(instance)
    first = _current_claim(instance)
    _attest(instance, owner, first, tmp_path, at=EVALUATION_TIME - timedelta(minutes=1))
    original = facts._dirty_keys

    def mutated(connection, delta, moved):  # type: ignore[no-untyped-def]
        keys = original(connection, delta, moved)
        if mutation == "extra":
            return keys | {(rows.SOURCE, first.identity.qualified)}
        return keys - {(rows.THRESHOLD, first.identity.qualified)}

    with patch.object(facts, "_dirty_keys", mutated), pytest.raises(AssertionError):
        oracle._catch_up_and_check(state, before, swept=False)


def test_a_door_row_records_the_account_captures_it_read(tmp_path: Path) -> None:
    """A door row reads its Claim's acceptance-time accounts: their Captures are reads of
    the door key, so a Capture that moves reaches it even when the facts stay the same."""

    instance, owner, _actor, _first, claim_id, *_rest = shared_capture_world(tmp_path)
    claim = next(c for c in _all_claims(instance) if c.identity.name == claim_id)
    capture = _capture(instance, claim_id, b"new observation")
    _attest(
        instance,
        owner,
        claim,
        tmp_path,
        at=EVALUATION_TIME - timedelta(minutes=1),
        basis="new_capture",
        stance="support",
        captures=(capture,),
    )
    _drain(instance)
    tables = facts.stored_tables(instance)
    assert tables is not None
    reads = {read for read, family, key in tables["row_reads"] if family == rows.DOOR}
    assert {f"capture:{digest}" for digest in claim.backing.capture_digests} <= reads
