"""The next projection is an interval-bounded substitute for the live Claim fold."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.claims import claim_statement_digest
from cruxible_core.consumers.next import queue as consumer
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.service.discovery.next import (
    DEFAULT_EXPIRING_WITHIN_MICROSECONDS,
    PlaybillNextRequest,
    PlaybillNextRequestV1,
    claim_unsure_holds,
    service_playbill_next,
    summarize_playbill_next,
)
from tests.core_support._claim_authoring_support import (
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
)
from tests.core_support._knowledge_loop_support import activate, authoring, seed_claims
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_existing_capture import shared_capture_world
from tests.test_authoring.test_derivation_admission import world  # noqa: F401
from tests.test_evidence.test_attestation_consequence_next import threshold_world
from tests.test_integration.test_next_closed_loop import (
    EVALUATION_TIME,
    _access,
    _current_claim,
    _foreign_world,
    _freshness_world,
    _refresh_claim,
)
from tests.test_integration.test_next_holds import _all_claims, _attest, _capture
from tests.test_query.test_dependency_impact import (
    _derived,
    _facts,
    _source_v1,
    _source_v2,
)

WORKER = consumer._PART


def _drain(instance, at: datetime = EVALUATION_TIME) -> None:  # type: ignore[no-untyped-def]
    WORKER.match(instance, now=at, daemon_id="first")
    manager = SimpleNamespace(get=lambda _id: instance)
    for work in WORKER.due(instance, now=at):
        WORKER.run(manager, "instance", work, now=at)


def _stored(instance, at: datetime, version: int = 2):  # type: ignore[no-untyped-def]
    return consumer.stored_claim_queue(
        instance,
        coordinate=instance.accepted_coordinate(),
        door_head=instance.claim_attestation_evidence_store().head(),
        evaluation_time=at,
        version=version,
    )


def _assert_equivalent(instance, at: datetime, *, expect_stored: bool) -> None:  # type: ignore[no-untyped-def]
    claims = _all_claims(instance)
    for version, model in ((1, PlaybillNextRequestV1), (2, PlaybillNextRequest)):
        if version == 2:
            assert (_stored(instance, at, version) is not None) == expect_stored
        for surface, rung in ((None, None), ("sdk", 0), ("mcp", 1)):
            request = model(evaluation_time=at, access_profile=_access(), caller_surface=surface)
            served = service_playbill_next(instance, request=request, caller_rung=rung)
            # Force only the queue lookup to miss; health and every other input remain identical.
            with patch.object(consumer, "stored_claim_queue", return_value=None):
                live = service_playbill_next(instance, request=request, caller_rung=rung)
            assert served.model_dump_json() == live.model_dump_json()
            assert served.items == live.items and served.result_digest == live.result_digest
    request = PlaybillNextRequest(evaluation_time=at, access_profile=_access())
    served_summary = summarize_playbill_next(instance, request=request)
    held = claim_unsure_holds(
        instance, coordinate=instance.accepted_coordinate(), claims=claims, evaluation_time=at
    )
    with patch.object(consumer, "stored_claim_queue", return_value=None):
        assert served_summary == summarize_playbill_next(instance, request=request)
        assert held == claim_unsure_holds(
            instance, coordinate=instance.accepted_coordinate(), claims=claims, evaluation_time=at
        )


def _check_edges(instance, at: datetime) -> None:  # type: ignore[no-untyped-def]
    _drain(instance, at)
    stored = _stored(instance, at)
    assert stored is not None
    inside = {at, at + timedelta(microseconds=1)}
    if stored.valid_from is not None:
        inside.add(stored.valid_from)
        _assert_equivalent(
            instance, stored.valid_from - timedelta(microseconds=1), expect_stored=False
        )
    if stored.valid_until is not None:
        inside.add(stored.valid_until - timedelta(microseconds=1))
        _assert_equivalent(instance, stored.valid_until, expect_stored=False)
        _assert_equivalent(
            instance, stored.valid_until + timedelta(microseconds=1), expect_stored=False
        )
    for time in inside:
        if stored.valid_until is None or time < stored.valid_until:
            _assert_equivalent(instance, time, expect_stored=True)


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
        "dependency",
    ),
)
def test_queue_equivalence_across_histories_and_interval_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: str
) -> None:
    at = EVALUATION_TIME
    if history == "supported":
        instance, _owner = seed_claims(tmp_path)
    elif history == "uncovered":
        instance, owner, *_rest = _foreign_world(tmp_path, bind=False)
        _attest(instance, owner, _current_claim(instance), tmp_path, at=at)
    elif history == "freshness":
        instance, _owner = _freshness_world(tmp_path)
        # Includes entering the seven-day horizon, the observation and evidence expiry.
        for time in (
            datetime.fromisoformat("2026-08-09T20:00:10+00:00"),
            datetime.fromisoformat("2026-08-16T20:00:00+00:00"),
            datetime.fromisoformat("2026-08-16T20:00:10+00:00"),
        ):
            # Rebuild this disposable projection to test each interval.
            consumer._STATE.path(instance).unlink(missing_ok=True)
            _check_edges(instance, time)
        assert any(row.reason == "claim_stale_evidence" for row in _stored(instance, time).rows)
        return
    elif history == "conflict":
        instance, owner = seed_claims(tmp_path)
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
        for claim in _all_claims(instance):
            _attest(
                instance,
                owner,
                claim,
                tmp_path,
                at=at - timedelta(minutes=1),
                valid_until=at + timedelta(minutes=3),
            )
    elif history == "dependency":
        instance, _owner = initialize_local(tmp_path)
        source = _source_v2()
        # Dependency visibility changes at this source's effective end.
        claim = source.accepted.claim.model_copy(
            update={
                "statement": source.accepted.claim.statement.model_copy(
                    update={"effective_until": at + timedelta(minutes=2)}
                )
            }
        )
        source = source.model_copy(
            update={"accepted": source.accepted.model_copy(update={"claim": claim})}
        )
        dependent = _derived()
        facts = _facts((source, dependent)).model_copy(
            update={"coordinate": instance.accepted_coordinate()}
        )
        monkeypatch.setattr(
            "cruxible_core.service.discovery.next._AcceptedQueryFactsRead.build",
            lambda *_a, **_k: facts,
        )
        monkeypatch.setattr(
            "cruxible_core.service.discovery.next._bounded_claim_lineages",
            lambda *_a, **_k: (
                {
                    source.accepted.path: (
                        _source_v1().accepted.artifact_digest,
                        source.accepted.artifact_digest,
                    ),
                    dependent.accepted.path: (dependent.accepted.artifact_digest,),
                },
                frozenset(),
            ),
        )
        from cruxible_core.service.discovery import next as next_module

        original = next_module._claim_dependency_items

        def dependencies(*args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["claims"] = tuple(row.accepted.claim for row in facts.claims)
            return original(*args, **kwargs)

        monkeypatch.setattr(next_module, "_claim_dependency_items", dependencies)
    elif history.startswith("door-"):
        instance, owner, _actor, _first, claim_id, *_rest = shared_capture_world(tmp_path)
        claim = next(c for c in _all_claims(instance) if c.identity.name == claim_id)
        capture = _capture(instance, claim_id, b"new observation")
        _attest(instance, owner, claim, tmp_path, at=at - timedelta(minutes=2))
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
    _check_edges(instance, at)
    expected = {
        "uncovered": "claim_uncovered",
        "conflict": "claim_conflicted",
        "dependency": "claim_dependency_stale",
    }.get(history)
    if history.startswith("door-") or history == "threshold":
        consumer._STATE.path(instance).unlink()
        later = at + timedelta(minutes=2)
        _check_edges(instance, later)
        expected = {
            "door-contradict": "claim_contradicting_evidence_available",
            "door-support": "claim_new_evidence_supporting",
            "door-unsure": "claim_new_evidence_unreviewed",
            "threshold": "claim_attestation_threshold_met",
        }[history]
        at = later
    if expected is not None:
        assert any(row.reason == expected for row in _stored(instance, at).rows)


def test_serving_skips_claim_folds_and_keeps_paging_and_delta(tmp_path: Path) -> None:
    instance, _owner, *_rest = _foreign_world(tmp_path, bind=False)
    _drain(instance)
    request = PlaybillNextRequest(
        evaluation_time=EVALUATION_TIME, access_profile=_access(), limit=1
    )
    with patch(
        "cruxible_core.service.discovery.next._claim_rows", side_effect=AssertionError("live")
    ):
        first = service_playbill_next(instance, request=request)
        summary = summarize_playbill_next(instance, request=request)
        assert summary.total_items == first.total_items
        delta = service_playbill_next(
            instance,
            request=request.model_copy(update={"since_result_digest": first.result_digest}),
        )
        assert delta.items == ()
        if first.next_cursor is not None:
            second = service_playbill_next(
                instance, request=request.model_copy(update={"cursor": first.next_cursor})
            )
            assert second.result_digest == first.result_digest
    assert _stored(instance, EVALUATION_TIME) is not None


def test_resume_does_not_recompute_for_time_or_restart_and_schema_mismatch_rebuilds(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    _drain(instance)
    WORKER.match(instance, now=EVALUATION_TIME + timedelta(days=99), daemon_id="restart")
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME)) == ()
    with sqlite3.connect(consumer._STATE.path(instance)) as connection:
        connection.execute("CREATE TABLE alien(value TEXT)")
    assert _stored(instance, EVALUATION_TIME) is None
    _drain(instance)
    assert _stored(instance, EVALUATION_TIME) is not None


def test_door_movement_recomputes_and_health_reports_lag(tmp_path: Path) -> None:
    from cruxible_core.consumers.runner import consumer_statuses
    from tests.test_consumers.test_prediction_settlement import drain

    instance, owner, *_rest = _foreign_world(tmp_path, bind=False)
    _drain(instance)
    old_door = instance.claim_attestation_evidence_store().head()
    _attest(instance, owner, _current_claim(instance), tmp_path, at=EVALUATION_TIME)
    assert _stored(instance, EVALUATION_TIME) is None
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "lagging"
    _drain(instance)
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"
    assert _stored(instance, EVALUATION_TIME).held_claims
    drain(instance, now=EVALUATION_TIME)
    statuses = consumer_statuses(SimpleNamespace(open_instances=lambda: (("inst", instance),)))
    assert any(row.kind == "next" and row.state == "running" for row in statuses)
    _assert_equivalent(instance, EVALUATION_TIME, expect_stored=True)
    request = PlaybillNextRequest(
        evaluation_time=EVALUATION_TIME,
        access_profile=_access(),
        at_attestation_head_digest=old_door,
    )
    from cruxible_core.service.discovery import next as next_module

    with patch.object(next_module, "_claim_rows", wraps=next_module._claim_rows) as live:
        service_playbill_next(instance, request=request)
        assert live.called


def test_accepted_movement_recomputes_and_serving_guards_fall_back(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    before = instance.accepted_coordinate()
    _drain(instance)
    _refresh_claim(instance, owner, timestamp="2026-08-24T18:00:00.000000Z")
    assert _stored(instance, EVALUATION_TIME) is None
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "lagging"
    _drain(instance)
    assert _stored(instance, EVALUATION_TIME) is not None
    request = PlaybillNextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access())
    for update in (
        {"at": AcceptedCoordinate.from_internal(before)},
        {
            "expiring_within": CanonicalDuration(
                microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS + 1
            )
        },
        {
            "access_profile": CoverageAccessProfile(
                profile_id="public", permitted_access_classes=("public",)
            )
        },
    ):
        with patch(
            "cruxible_core.service.discovery.next._claim_rows",
            wraps=__import__(
                "cruxible_core.service.discovery.next", fromlist=["_claim_rows"]
            )._claim_rows,
        ) as live:
            service_playbill_next(instance, request=request.model_copy(update=update))
            assert live.called


def test_library_reads_create_no_worker_state_and_disabled_worker_is_not_served(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    assert _stored(instance, EVALUATION_TIME) is None
    service_playbill_next(
        instance,
        request=PlaybillNextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access()),
    )
    assert not consumer._STATE.path(instance).exists()
    _drain(instance)
    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "next")
    assert not WORKER.active(instance)
    assert _stored(instance, EVALUATION_TIME) is None


def test_cas_movement_recomputes_without_an_accepted_or_door_change(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path)
    _drain(instance)
    assert _stored(instance, EVALUATION_TIME) is not None
    coordinate = instance.accepted_coordinate()
    door = instance.claim_attestation_evidence_store().head()
    instance.body_store().store(b"new material")
    assert _stored(instance, EVALUATION_TIME) is None
    (health,) = WORKER.health(instance, now=EVALUATION_TIME)
    assert health.state == "lagging" and health.detail["inputs_changed"]
    assert instance.accepted_coordinate() == coordinate
    assert instance.claim_attestation_evidence_store().head() == door
    _drain(instance)
    assert _stored(instance, EVALUATION_TIME) is not None
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"
    with patch(
        "cruxible_core.service.discovery.next._claim_rows", side_effect=AssertionError("live")
    ):
        service_playbill_next(
            instance,
            request=PlaybillNextRequest(
                evaluation_time=EVALUATION_TIME,
                access_profile=_access(),
            ),
        )


@pytest.mark.parametrize("changed", ("accepted", "door", "cas"))
def test_a_failed_target_is_not_retried_until_an_input_changes(
    tmp_path: Path, changed: str
) -> None:
    instance, owner = _freshness_world(tmp_path) if changed == "accepted" else seed_claims(tmp_path)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    manager = SimpleNamespace(get=lambda _id: instance)
    (work,) = WORKER.due(instance, now=EVALUATION_TIME)
    with patch(
        "cruxible_core.service.discovery.next.build_stored_claim_queue",
        side_effect=OSError("unreadable"),
    ) as fold:
        with pytest.raises(OSError, match="unreadable"):
            WORKER.run(manager, "instance", work, now=EVALUATION_TIME)
        for days in (0, 1, 99):
            _drain(instance, EVALUATION_TIME + timedelta(days=days))
        WORKER.match(instance, now=EVALUATION_TIME, daemon_id="restart")
        assert tuple(WORKER.due(instance, now=EVALUATION_TIME)) == ()
        assert fold.call_count == 1
    (health,) = WORKER.health(instance, now=EVALUATION_TIME)
    assert health.state == "stalled" and health.repair is not None
    assert (
        health.repair.required_change
        == "resolve_the_worker_error_then_rebuild_the_next_queue_state"
    )
    from cruxible_core.service.discovery.next import _consumer_stalled_items

    (row,) = _consumer_stalled_items((health,))
    assert "checked_at" not in row.detail and "last_error_at" not in row.detail
    with sqlite3.connect(consumer._STATE.path(instance)) as connection:
        connection.execute("UPDATE progress SET checked_at=?,last_error_at=?", ("later", "later"))
    assert _consumer_stalled_items(WORKER.health(instance, now=EVALUATION_TIME)) == (row,)
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME)) == ()
    if changed == "accepted":
        _refresh_claim(instance, owner, timestamp="2026-08-24T18:00:00.000000Z")
    elif changed == "door":
        _attest(instance, owner, _current_claim(instance), tmp_path, at=EVALUATION_TIME)
    else:
        instance.body_store().store(b"new input after failure")
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="restart")
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME))
    _drain(instance)
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"
    assert _stored(instance, EVALUATION_TIME) is not None


def test_a_new_target_is_not_lost_when_an_old_flight_finishes(tmp_path: Path) -> None:
    instance, owner, *_rest = _foreign_world(tmp_path, bind=False)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    (old_work,) = WORKER.due(instance, now=EVALUATION_TIME)
    _attest(instance, owner, _current_claim(instance), tmp_path, at=EVALUATION_TIME)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    WORKER.run(SimpleNamespace(get=lambda _id: instance), "instance", old_work, now=EVALUATION_TIME)
    assert _stored(instance, EVALUATION_TIME) is None
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME))
    _drain(instance)
    assert _stored(instance, EVALUATION_TIME) is not None


def test_request_observations_merge_with_stored_rows_exactly(tmp_path: Path) -> None:
    from tests.test_discovery.test_next_request_reads import _rich_request
    from tests.test_query.test_query_execution_service import _instance_with_query

    instance, _owner = _instance_with_query(tmp_path)
    request = _rich_request(instance)
    _drain(instance, request.evaluation_time)
    assert _stored(instance, request.evaluation_time, version=1) is not None
    served = service_playbill_next(instance, request=request)
    with patch.object(consumer, "stored_claim_queue", return_value=None):
        live = service_playbill_next(instance, request=request)
    assert served.model_dump_json() == live.model_dump_json()


def test_a_queue_behind_head_is_never_used_by_a_read(tmp_path: Path) -> None:
    from cruxible_core.service.discovery import next as next_module

    instance, owner = _freshness_world(tmp_path)
    _drain(instance)
    _refresh_claim(instance, owner, timestamp="2026-08-24T18:00:00.000000Z")
    request = PlaybillNextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access())
    with patch.object(next_module, "_claim_rows", wraps=next_module._claim_rows) as live:
        service_playbill_next(instance, request=request)
        assert live.called


def test_unchanged_targets_match_without_a_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    statements: list[str] = []
    original = consumer._STATE.open

    @contextmanager
    def traced(*args, **kwargs):  # type: ignore[no-untyped-def]
        with original(*args, **kwargs) as connection:
            assert connection is not None
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(consumer._STATE, "open", traced)
    for tick in range(3):
        WORKER.match(instance, now=EVALUATION_TIME + timedelta(seconds=tick), daemon_id="restart")
    assert statements
    assert all(statement.lstrip().split()[0] == "SELECT" for statement in statements)


def test_a_cas_change_during_a_fold_is_not_published_under_the_previous_target(
    tmp_path: Path,
) -> None:
    from cruxible_core.service.discovery import next as next_module

    instance, _owner = seed_claims(tmp_path)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    (work,) = WORKER.due(instance, now=EVALUATION_TIME)
    original = next_module.build_stored_claim_queue

    def moved(*args, **kwargs):  # type: ignore[no-untyped-def]
        snapshot = original(*args, **kwargs)
        instance.body_store().store(b"material landed during fold")
        return snapshot

    with patch.object(next_module, "build_stored_claim_queue", moved):
        WORKER.run(SimpleNamespace(get=lambda _id: instance), "instance", work, now=EVALUATION_TIME)
    assert _stored(instance, EVALUATION_TIME) is None
    _drain(instance)
    assert _stored(instance, EVALUATION_TIME) is not None


def test_an_old_failure_does_not_suppress_a_newer_matched_target(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    (old_work,) = WORKER.due(instance, now=EVALUATION_TIME)
    instance.body_store().store(b"new target before old failure")
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    # An old coordinate failure can arrive after matching advanced the CAS target.
    with patch.object(instance, "resolve_accepted_coordinate", side_effect=OSError("old failure")):
        with pytest.raises(OSError, match="old failure"):
            WORKER.run(
                SimpleNamespace(get=lambda _id: instance),
                "instance",
                old_work,
                now=EVALUATION_TIME,
            )
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME))
    _drain(instance)
    assert _stored(instance, EVALUATION_TIME) is not None


def test_orient_profile_reuses_inside_the_interval_and_falls_back_at_valid_until(
    tmp_path: Path,
) -> None:
    from cruxible_core.service.discovery import next as next_module
    from cruxible_core.service.discovery.orient import _NEXT_PROFILE

    instance, _owner = _freshness_world(tmp_path)
    at = datetime.fromisoformat("2026-08-16T20:00:00+00:00")
    assert set(_NEXT_PROFILE.permitted_access_classes) == {"instance", "public"}
    _drain(instance, at)
    snapshot = _stored(instance, at)
    assert snapshot is not None and snapshot.valid_until is not None
    request = PlaybillNextRequest(evaluation_time=at, access_profile=_NEXT_PROFILE)
    with patch.object(next_module, "_claim_rows", side_effect=AssertionError("live")):
        service_playbill_next(instance, request=request)
        summarize_playbill_next(instance, request=request)
    edge = request.model_copy(update={"evaluation_time": snapshot.valid_until})
    assert _stored(instance, snapshot.valid_until) is None
    with patch.object(next_module, "_claim_rows", wraps=next_module._claim_rows) as live:
        served = service_playbill_next(instance, request=edge)
        summarize_playbill_next(instance, request=edge)
        assert live.call_count == 2
    with patch.object(consumer, "stored_claim_queue", return_value=None):
        assert served == service_playbill_next(instance, request=edge)


def _next_outcome(instance, version: int):  # type: ignore[no-untyped-def]
    model = PlaybillNextRequestV1 if version == 1 else PlaybillNextRequest
    request = model(evaluation_time=EVALUATION_TIME, access_profile=_access())
    try:
        return service_playbill_next(instance, request=request).model_dump_json()
    except Exception as exc:  # noqa: BLE001 - the refusal itself is the outcome compared
        return (type(exc).__name__, str(exc))


def _rewrite_backing(instance, *, keep_mtime: bool) -> None:  # type: ignore[no-untyped-def]
    from cruxible_core.service.claims.verdict_memo import verdict_input_fingerprint
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
    from tests.test_evidence.test_slot_verdict_reuse import _rewrite_in_place

    context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
    capture = context.claims()[0].backing.capture_digests[0]
    fingerprint = verdict_input_fingerprint(instance)
    _rewrite_in_place(instance.body_store()._path(capture), keep_mtime=keep_mtime)
    # The shard fingerprint does not see an in-place rewrite; body identities must.
    assert verdict_input_fingerprint(instance) == fingerprint


def _assert_stalled_without_retry(instance) -> None:  # type: ignore[no-untyped-def]
    (health,) = WORKER.health(instance, now=EVALUATION_TIME)
    assert health.state == "stalled" and "PlaybillCasError" in health.detail["last_error"]
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="restart")
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME)) == ()


@pytest.mark.parametrize("version", (1, 2))
@pytest.mark.parametrize("keep_mtime", (False, True), ids=("rewrite", "rewrite-keep-mtime"))
def test_a_body_rewritten_in_place_is_never_served_from_the_stored_queue(
    tmp_path: Path, version: int, keep_mtime: bool
) -> None:
    instance, _owner = seed_claims(tmp_path)
    _drain(instance)
    assert _stored(instance, EVALUATION_TIME, version) is not None

    _rewrite_backing(instance, keep_mtime=keep_mtime)

    # Served and live agree: both refuse the corrupt backing.
    assert _stored(instance, EVALUATION_TIME, version) is None
    served = _next_outcome(instance, version)
    with patch.object(consumer, "stored_claim_queue", return_value=None):
        assert served == _next_outcome(instance, version)
    assert served[0] == "PlaybillCasError"

    # Matching withdraws the queue and asks for one rebuild; a rebuild that fails
    # on the same inputs is not retried until an input changes.
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    (work,) = WORKER.due(instance, now=EVALUATION_TIME)
    with pytest.raises(Exception, match="CAS object bytes"):
        WORKER.run(SimpleNamespace(get=lambda _id: instance), "instance", work, now=EVALUATION_TIME)
    _assert_stalled_without_retry(instance)
    assert _stored(instance, EVALUATION_TIME, version) is None


@pytest.mark.parametrize("version", (1, 2))
def test_a_body_rewritten_during_a_fold_is_not_published(tmp_path: Path, version: int) -> None:
    from cruxible_core.service.discovery import next as next_module

    instance, _owner = seed_claims(tmp_path)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    (work,) = WORKER.due(instance, now=EVALUATION_TIME)
    original = next_module.build_stored_claim_queue
    calls = 0

    def rewritten(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        snapshot = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            # Both wire versions are folded; the body changes before the publish.
            _rewrite_backing(instance, keep_mtime=True)
        return snapshot

    with patch.object(next_module, "build_stored_claim_queue", rewritten):
        WORKER.run(SimpleNamespace(get=lambda _id: instance), "instance", work, now=EVALUATION_TIME)
    assert _stored(instance, EVALUATION_TIME, version) is None
    served = _next_outcome(instance, version)
    with patch.object(consumer, "stored_claim_queue", return_value=None):
        assert served == _next_outcome(instance, version)
    assert served[0] == "PlaybillCasError"
    (work,) = WORKER.due(instance, now=EVALUATION_TIME)
    with pytest.raises(Exception, match="CAS object bytes"):
        WORKER.run(SimpleNamespace(get=lambda _id: instance), "instance", work, now=EVALUATION_TIME)
    _assert_stalled_without_retry(instance)


def test_a_fold_without_body_identities_is_published_but_never_served(tmp_path: Path) -> None:
    from cruxible_core.service.discovery import next as next_module

    instance, _owner = seed_claims(tmp_path)
    original = next_module.build_stored_claim_queue

    def unobserved(*args, **kwargs):  # type: ignore[no-untyped-def]
        from cruxible_core.storage.cas import note_unobservable_body

        snapshot = original(*args, **kwargs)
        # As an external reader's answer does: nothing CAS-held stands for it.
        note_unobservable_body()
        return snapshot

    with patch.object(next_module, "build_stored_claim_queue", unobserved):
        _drain(instance)
    assert _stored(instance, EVALUATION_TIME) is None
    # The target is folded once, not again on every pass; reads compute live.
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="restart")
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME)) == ()
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"


def _recorded_bodies(instance) -> dict[str, object]:  # type: ignore[no-untyped-def]
    import json

    with consumer._STATE.open(instance) as connection:
        assert connection is not None
        (bodies,) = connection.execute("SELECT bodies FROM progress").fetchone()
    return dict(json.loads(bodies))


def test_repairing_a_body_in_place_releases_the_stalled_target_once(tmp_path: Path) -> None:
    from cruxible_core.service.claims.verdict_memo import verdict_input_fingerprint
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
    from tests.test_evidence.test_slot_verdict_reuse import _rewrite_in_place

    instance, _owner = seed_claims(tmp_path)
    context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
    path = instance.body_store()._path(context.claims()[0].backing.capture_digests[0])
    original = path.read_bytes()
    _drain(instance)
    fingerprint = verdict_input_fingerprint(instance)
    _rewrite_in_place(path, keep_mtime=True)
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="first")
    (work,) = WORKER.due(instance, now=EVALUATION_TIME)
    with pytest.raises(Exception, match="CAS object bytes"):
        WORKER.run(SimpleNamespace(get=lambda _id: instance), "instance", work, now=EVALUATION_TIME)
    _assert_stalled_without_retry(instance)

    # Restored in place: no target field moves, but the failed fold's body did.
    path.write_bytes(original)
    assert verdict_input_fingerprint(instance) == fingerprint
    WORKER.match(instance, now=EVALUATION_TIME, daemon_id="restart")
    assert tuple(WORKER.due(instance, now=EVALUATION_TIME))
    _drain(instance)
    assert WORKER.health(instance, now=EVALUATION_TIME)[0].state == "running"
    assert _stored(instance, EVALUATION_TIME) is not None
    _assert_equivalent(instance, EVALUATION_TIME, expect_stored=True)


def _retired_dependency(derivation):  # type: ignore[no-untyped-def]
    from tests.core_support._retirement_support import retire_claim
    from tests.test_authoring.test_derivation_admission import _run_and_accept

    derived = _run_and_accept(derivation)
    assert derived.backing.input_claim_digests
    retire_claim(derivation.instance, derivation.owner, derived.identity.name)
    return derived.backing.capture_digests[0]


_DERIVED_AT = datetime.fromisoformat("2026-10-02T00:00:00+00:00")


def _derived_outcome(instance, version: int):  # type: ignore[no-untyped-def]
    model = PlaybillNextRequestV1 if version == 1 else PlaybillNextRequest
    request = model(evaluation_time=_DERIVED_AT, access_profile=_access())
    try:
        return service_playbill_next(instance, request=request).model_dump_json()
    except Exception as exc:  # noqa: BLE001 - the refusal itself is the outcome compared
        return (type(exc).__name__, str(exc))


@pytest.mark.parametrize("version", (1, 2))
def test_a_retired_dependency_capture_rewritten_after_publish_is_not_served(
    world,  # noqa: F811
    version: int,
) -> None:
    from tests.test_evidence.test_slot_verdict_reuse import _rewrite_in_place

    capture = _retired_dependency(world)
    instance = world.instance
    _drain(instance, _DERIVED_AT)
    assert _stored(instance, _DERIVED_AT, version) is not None
    # Read by the dependency facts of a retired Claim, not by live resolution.
    assert capture in _recorded_bodies(instance)

    _rewrite_in_place(instance.body_store()._path(capture), keep_mtime=True)
    assert _stored(instance, _DERIVED_AT, version) is None
    served = _derived_outcome(instance, version)
    with patch.object(consumer, "stored_claim_queue", return_value=None):
        assert served == _derived_outcome(instance, version)
    assert served[0] == "PlaybillCasError"


@pytest.mark.parametrize("version", (1, 2))
def test_a_retired_dependency_capture_rewritten_during_a_fold_is_not_published(
    world,  # noqa: F811
    version: int,
) -> None:
    from cruxible_core.service.discovery import next as next_module
    from tests.test_evidence.test_slot_verdict_reuse import _rewrite_in_place

    capture = _retired_dependency(world)
    instance = world.instance
    original = next_module.build_stored_claim_queue
    calls = 0

    def rewritten(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        snapshot = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            _rewrite_in_place(instance.body_store()._path(capture), keep_mtime=True)
        return snapshot

    with patch.object(next_module, "build_stored_claim_queue", rewritten):
        _drain(instance, _DERIVED_AT)
    assert calls == 2
    assert _stored(instance, _DERIVED_AT, version) is None
    served = _derived_outcome(instance, version)
    with patch.object(consumer, "stored_claim_queue", return_value=None):
        assert served == _derived_outcome(instance, version)


def test_a_historical_admission_account_capture_is_in_the_observed_set(tmp_path: Path) -> None:
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
    assert _stored(instance, EVALUATION_TIME) is not None
    # Read only by the door rows' historical admission accounts.
    assert old_capture in _recorded_bodies(instance)
