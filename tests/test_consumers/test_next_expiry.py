"""A durable expiry fire refreshes the stored queue without any other input changing."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cruxible_core.consumers.next import NEXT_QUEUE, queue
from cruxible_core.service.claims.verdict_memo import verdict_input_fingerprint
from cruxible_core.service.discovery import next as next_module
from cruxible_core.service.discovery.next import PlaybillNextRequestV2, service_playbill_next
from cruxible_core.triggers.journal import journal_path, schedule_deadline
from tests.core_support._knowledge_loop_support import seed_claims
from tests.support.internal_triggers import fire_internal_triggers
from tests.test_consumers.test_next_queue import _stored
from tests.test_consumers.test_prediction_settlement import drain
from tests.test_integration.test_next_closed_loop import _access, _freshness_world

AT = datetime.fromisoformat("2026-08-16T20:00:00+00:00")


def _inputs(instance):  # type: ignore[no-untyped-def]
    return (
        instance.accepted_coordinate(),
        instance.claim_attestation_evidence_store().head(),
        verdict_input_fingerprint(instance),
    )


def _queue_work(instance):  # type: ignore[no-untyped-def]
    return tuple(work for work in NEXT_QUEUE.due(instance, now=AT) if work.key.startswith("queue:"))


def test_expiry_fire_rebuilds_at_its_recorded_time_and_serving_resumes(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path)
    drain(instance, now=AT)
    before = _inputs(instance)
    snapshot = _stored(instance, AT)
    assert snapshot is not None and snapshot.valid_until is not None
    edge = snapshot.valid_until
    request = PlaybillNextRequestV2(evaluation_time=edge, access_profile=_access())
    with patch.object(next_module, "_claim_rows", wraps=next_module._claim_rows) as live:
        service_playbill_next(instance, request=request)
        assert live.called
    assert not _queue_work(instance)  # The worker owns no clock.
    fires = fire_internal_triggers(instance, now=edge)
    (fire,) = [event for event in fires if event.action == "next.expire"]
    assert fire.due_at == edge and _inputs(instance) == before
    NEXT_QUEUE.match(instance, now=edge + timedelta(days=100), daemon_id="restart")
    (work,) = _queue_work(instance)
    assert NEXT_QUEUE.health(instance, now=AT)[0].detail["queue"]["pending_expiry"]
    with patch.object(
        next_module, "build_stored_claim_queue", wraps=next_module.build_stored_claim_queue
    ) as fold:
        NEXT_QUEUE.run(
            SimpleNamespace(get=lambda _id: instance), "inst", work, now=edge + timedelta(days=100)
        )
        assert [call.kwargs["evaluation_time"] for call in fold.call_args_list] == [
            fire.fired_at
        ] * 2
    assert _inputs(instance) == before and _stored(instance, edge) is not None
    assert not _queue_work(instance)
    with patch.object(next_module, "_claim_rows", side_effect=AssertionError("live fold")):
        served = service_playbill_next(instance, request=request)
    with patch.object(queue, "stored_claim_queue", return_value=None):
        assert (
            served.model_dump_json()
            == service_playbill_next(instance, request=request).model_dump_json()
        )


def test_refresh_rearms_each_bound_and_an_unbounded_replacement_cancels_the_old_deadline(
    tmp_path: Path,
) -> None:
    instance, _owner = _freshness_world(tmp_path)
    at = AT
    drain(instance, now=at)
    for _ in range(8):
        snapshot = _stored(instance, at)
        assert snapshot is not None
        if snapshot.valid_until is None:
            break
        at = snapshot.valid_until
        assert any(
            event.action == "next.expire" for event in fire_internal_triggers(instance, now=at)
        )
        drain(instance, now=at)
    else:
        raise AssertionError("freshness fixture did not reach its last bound")
    with sqlite3.connect(journal_path(instance)) as connection:
        assert (
            connection.execute("SELECT due_at FROM deadlines WHERE name='next.expire'").fetchone()
            is None
        )
    # A replacement with no bound must also withdraw an already pending instant.
    schedule_deadline(instance, "next.expire", at + timedelta(minutes=1))
    instance.body_store().store(b"new CAS input")
    drain(instance, now=at)
    assert not any(
        event.action == "next.expire"
        for event in fire_internal_triggers(instance, now=at + timedelta(minutes=2))
    )


def test_a_failed_queue_target_can_retry_on_an_expiry_fire(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path)
    drain(instance, now=AT)
    snapshot = _stored(instance, AT)
    assert snapshot is not None and snapshot.valid_until is not None
    instance.body_store().store(b"new CAS target")
    NEXT_QUEUE.match(instance, now=AT, daemon_id="daemon")
    (work,) = _queue_work(instance)
    with patch.object(next_module, "build_stored_claim_queue", side_effect=OSError("unreadable")):
        with pytest.raises(OSError):
            NEXT_QUEUE.run(SimpleNamespace(get=lambda _id: instance), "inst", work, now=AT)
    assert not _queue_work(instance)
    assert NEXT_QUEUE.health(instance, now=AT)[0].state == "stalled"
    fire_internal_triggers(instance, now=snapshot.valid_until)
    drain(instance, now=snapshot.valid_until)
    assert NEXT_QUEUE.health(instance, now=AT)[0].state == "running"
    assert _stored(instance, snapshot.valid_until) is not None


def test_an_unbounded_queue_schedules_no_deadline(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    drain(instance, now=AT)
    snapshot = _stored(instance, AT)
    assert snapshot is not None and snapshot.valid_until is None
    assert not journal_path(instance).exists()


def test_a_new_fire_during_an_old_fold_remains_due(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path)
    drain(instance, now=AT)
    snapshot = _stored(instance, AT)
    assert snapshot is not None and snapshot.valid_until is not None
    edge = snapshot.valid_until
    fire_internal_triggers(instance, now=edge)
    NEXT_QUEUE.match(instance, now=edge, daemon_id="daemon")
    (old_work,) = _queue_work(instance)
    build = next_module.build_stored_claim_queue
    moved = []
    later = edge + timedelta(microseconds=1)

    def fire_while_folding(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = build(*args, **kwargs)
        if not moved:
            moved.append(True)
            schedule_deadline(instance, "next.expire", later)
            fire_internal_triggers(instance, now=later)
            NEXT_QUEUE.match(instance, now=later, daemon_id="daemon")
        return result

    with patch.object(next_module, "build_stored_claim_queue", side_effect=fire_while_folding):
        NEXT_QUEUE.run(SimpleNamespace(get=lambda _id: instance), "inst", old_work, now=edge)
    assert moved and _queue_work(instance)
    assert _stored(instance, later) is None
    drain(instance, now=later)
    assert _stored(instance, later) is not None and not _queue_work(instance)


def test_a_queue_is_not_published_when_its_deadline_cannot_be_armed(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path)
    NEXT_QUEUE.match(instance, now=AT, daemon_id="daemon")
    (work,) = _queue_work(instance)
    with patch.object(queue, "schedule_deadline", side_effect=OSError("journal unavailable")):
        with pytest.raises(OSError, match="journal unavailable"):
            NEXT_QUEUE.run(SimpleNamespace(get=lambda _id: instance), "inst", work, now=AT)
    assert _stored(instance, AT) is None
    assert NEXT_QUEUE.health(instance, now=AT)[0].state == "stalled"
    assert not _queue_work(instance)


def test_the_earliest_wire_version_bound_arms_the_deadline(tmp_path: Path) -> None:
    from cruxible_client.contracts.temporal import format_datetime

    instance, _owner = _freshness_world(tmp_path)
    NEXT_QUEUE.match(instance, now=AT, daemon_id="daemon")
    (work,) = _queue_work(instance)
    build = next_module.build_stored_claim_queue
    earlier = AT + timedelta(seconds=1)

    def narrower_v1(*args, **kwargs):  # type: ignore[no-untyped-def]
        snapshot = build(*args, **kwargs)
        assert snapshot.valid_until is not None and earlier < snapshot.valid_until
        if kwargs["attestation_head"] is None:
            return snapshot.model_copy(update={"valid_until": earlier})
        return snapshot

    with patch.object(next_module, "build_stored_claim_queue", side_effect=narrower_v1):
        NEXT_QUEUE.run(SimpleNamespace(get=lambda _id: instance), "inst", work, now=AT)
    with sqlite3.connect(journal_path(instance)) as connection:
        assert connection.execute(
            "SELECT due_at FROM deadlines WHERE name='next.expire'"
        ).fetchone() == (format_datetime(earlier),)
