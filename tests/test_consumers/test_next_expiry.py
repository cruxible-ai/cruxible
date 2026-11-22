"""One durable ``next.expire`` deadline moves the stored facts and rows through time.

The worker arms it at the earliest instant any stored fact or row key stops
holding; a fire is the only way time reaches the worker, and its pass
evaluates at the fire's recorded instant. A read never waits for it: a read
past a bound derives what ended, in a transaction it rolls back.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cruxible_core.consumers.next import NEXT_QUEUE, facts
from cruxible_core.service.discovery import next as next_module
from cruxible_core.service.discovery.next import NextRequest, service_playbill_next
from cruxible_core.triggers.journal import journal_path, schedule_deadline
from tests.core_support._knowledge_loop_support import seed_claims
from tests.support.internal_triggers import fire_internal_triggers
from tests.test_consumers.test_next_queue import _served
from tests.test_consumers.test_prediction_settlement import drain
from tests.test_integration.test_next_closed_loop import _access, _freshness_world

AT = datetime.fromisoformat("2026-08-16T20:00:00+00:00")


def _facts_work(instance, now: datetime = AT):  # type: ignore[no-untyped-def]
    return tuple(
        work for work in NEXT_QUEUE.due(instance, now=now) if work.key.startswith("facts:")
    )


def _deadline(instance) -> datetime | None:  # type: ignore[no-untyped-def]
    if not journal_path(instance).exists():
        return None
    with sqlite3.connect(journal_path(instance)) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM deadlines")]
        assert set(names) <= {facts.NEXT_EXPIRE}
        row = connection.execute(
            "SELECT due_at FROM deadlines WHERE name=?", (facts.NEXT_EXPIRE,)
        ).fetchone()
    return None if row is None else datetime.fromisoformat(row[0].replace("Z", "+00:00"))


def _earliest_end(instance) -> datetime | None:  # type: ignore[no-untyped-def]
    tables = facts.stored_tables(instance)
    assert tables is not None
    upper = facts._FACT_COLUMNS.index("upper_us")
    ends = [row[upper] for row in tables["claim_facts"] if row[upper] is not None]
    ends += [row[3] for row in tables["row_keys"] if row[3] is not None]
    if not ends:
        return None
    return datetime(1970, 1, 1, tzinfo=AT.tzinfo) + timedelta(microseconds=min(ends))


def _earliest_fact_end(instance) -> datetime | None:  # type: ignore[no-untyped-def]
    tables = facts.stored_tables(instance)
    assert tables is not None
    upper = facts._FACT_COLUMNS.index("upper_us")
    ends = [row[upper] for row in tables["claim_facts"] if row[upper] is not None]
    if not ends:
        return None
    return datetime(1970, 1, 1, tzinfo=AT.tzinfo) + timedelta(microseconds=min(ends))


def test_a_fire_refreshes_at_its_recorded_instant_and_serving_needs_no_fold(
    tmp_path: Path,
) -> None:
    instance, _owner = _freshness_world(tmp_path)
    drain(instance, now=AT)
    lead = _deadline(instance)
    # Armed at the earliest end over the facts and the row keys together: the
    # evidence_expiring lead's start, clamped to horizon/4 before the support
    # expires (stale_after 10 s). The facts end there too, so the row starts
    # from facts that read every observation recorded by then.
    assert lead is not None and lead == _earliest_end(instance) == _earliest_fact_end(instance)
    assert any(
        event.action == facts.NEXT_EXPIRE for event in fire_internal_triggers(instance, now=lead)
    )
    drain(instance, now=lead)
    edge = _deadline(instance)
    fact_end = _earliest_fact_end(instance)
    assert edge is not None and edge - lead == timedelta(seconds=10) / 4
    assert edge == fact_end == _earliest_end(instance)
    request = NextRequest(evaluation_time=edge, access_profile=_access())
    # A read past the bound is served: it derives what ended and rolls it back.
    before = facts.stored_tables(instance)
    assert _served(instance, edge) is not None
    assert facts.stored_tables(instance) == before
    assert not _facts_work(instance)  # The worker owns no clock.
    fires = fire_internal_triggers(instance, now=edge)
    (fire,) = [event for event in fires if event.action == facts.NEXT_EXPIRE]
    assert fire.due_at == edge
    NEXT_QUEUE.match(instance, now=edge + timedelta(days=100), daemon_id="restart")
    (work,) = _facts_work(instance)
    assert NEXT_QUEUE.health(instance, now=AT)[0].detail["facts"]["pending_expiry"]
    instants: list[datetime] = []
    original = facts._claim_facts

    def recorded(instance, context, claim, *, at, **kwargs):  # type: ignore[no-untyped-def]
        instants.append(at)
        return original(instance, context, claim, at=at, **kwargs)

    with patch.object(facts, "_claim_facts", recorded):
        NEXT_QUEUE.run(
            SimpleNamespace(get=lambda _id: instance), "inst", work, now=edge + timedelta(days=100)
        )
    assert instants and set(instants) == {fire.fired_at}
    assert not _facts_work(instance)
    with (
        patch.object(next_module, "_oracle_claim_rows", side_effect=AssertionError("folded")),
        patch.object(facts, "_claim_facts", side_effect=AssertionError("derived")),
    ):
        served = service_playbill_next(instance, request=request)
    with patch.object(facts, "served_claim_rows", return_value=None):
        assert (
            served.model_dump_json()
            == service_playbill_next(instance, request=request).model_dump_json()
        )


def test_evidence_expiring_starts_from_facts_that_read_every_reobservation(
    tmp_path: Path,
) -> None:
    """One lead: the facts end where the row starts, so a re-observation before it counts.

    stale_after 40 s: the row's lead is 10 s (a quarter of the horizon). The
    Claim is re-observed at 20 s; at 31 s the served queue and the live fold
    agree that its evidence is not expiring (it now expires at 60 s).
    """

    from tests.test_integration.test_next_closed_loop import _refresh_claim

    instance, owner = _freshness_world(tmp_path, stale_after=40_000_000)
    drain(instance, now=AT)
    lead_start = AT + timedelta(seconds=30)
    assert _earliest_fact_end(instance) == lead_start == _deadline(instance)
    _refresh_claim(instance, owner, timestamp="2026-08-16T20:00:20.000000Z")
    later = AT + timedelta(seconds=31)
    fire_internal_triggers(instance, now=later)
    drain(instance, now=later)
    request = NextRequest(evaluation_time=later, access_profile=_access())

    def reasons(page) -> set[str]:  # type: ignore[no-untyped-def]
        return {item.reason for item in page.items}

    served = service_playbill_next(instance, request=request)
    with patch.object(facts, "served_claim_rows", return_value=None):
        folded = service_playbill_next(instance, request=request)
    assert "evidence_expiring" not in reasons(folded)
    assert reasons(served) == reasons(folded)
    # At the re-observed lead start it is expiring, in both.
    again = NextRequest(evaluation_time=AT + timedelta(seconds=50), access_profile=_access())
    fire_internal_triggers(instance, now=again.evaluation_time)
    drain(instance, now=again.evaluation_time)
    assert "evidence_expiring" in reasons(service_playbill_next(instance, request=again))


def test_each_bound_rearms_and_an_unbounded_state_cancels_the_deadline(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path)
    at = AT
    drain(instance, now=at)
    for _ in range(8):
        edge = _deadline(instance)
        if edge is None:
            break
        assert edge == _earliest_end(instance)
        at = edge
        assert any(
            event.action == facts.NEXT_EXPIRE for event in fire_internal_triggers(instance, now=at)
        )
        drain(instance, now=at)
    else:
        raise AssertionError("freshness fixture did not reach its last bound")
    assert _earliest_end(instance) is None
    # A state with no bound must also withdraw an already pending instant.
    schedule_deadline(instance, facts.NEXT_EXPIRE, at + timedelta(minutes=1))
    instance.body_store().store(b"new CAS input")
    drain(instance, now=at)
    assert _deadline(instance) is None
    assert not any(
        event.action == facts.NEXT_EXPIRE
        for event in fire_internal_triggers(instance, now=at + timedelta(minutes=2))
    )


def test_a_failed_pass_retries_on_an_expiry_fire(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path)
    drain(instance, now=AT)
    edge = _deadline(instance)
    assert edge is not None
    instance.body_store().store(b"new CAS target")
    NEXT_QUEUE.match(instance, now=AT, daemon_id="daemon")
    (work,) = _facts_work(instance)
    with patch.object(facts, "_settle", side_effect=OSError("unreadable")):
        with pytest.raises(OSError):
            NEXT_QUEUE.run(SimpleNamespace(get=lambda _id: instance), "inst", work, now=AT)
    assert not _facts_work(instance)
    assert NEXT_QUEUE.health(instance, now=AT)[0].state == "stalled"
    fire_internal_triggers(instance, now=edge)
    drain(instance, now=edge)
    assert NEXT_QUEUE.health(instance, now=AT)[0].state == "running"
    assert _served(instance, edge) is not None


def test_an_unbounded_state_schedules_no_deadline(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    drain(instance, now=AT)
    assert _earliest_end(instance) is None
    assert _deadline(instance) is None


def test_a_new_fire_during_an_old_pass_remains_due(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path)
    drain(instance, now=AT)
    edge = _deadline(instance)
    assert edge is not None
    fire_internal_triggers(instance, now=edge)
    NEXT_QUEUE.match(instance, now=edge, daemon_id="daemon")
    (old_work,) = _facts_work(instance, edge)
    changes = facts._changes
    moved: list[bool] = []
    later = edge + timedelta(microseconds=1)

    def fire_while_deriving(*args, **kwargs):  # type: ignore[no-untyped-def]
        # The pass derives outside the state's lock; a fire lands meanwhile.
        found = changes(*args, **kwargs)
        if not moved:
            moved.append(True)
            schedule_deadline(instance, facts.NEXT_EXPIRE, later)
            fire_internal_triggers(instance, now=later)
            NEXT_QUEUE.match(instance, now=later, daemon_id="daemon")
        return found

    with patch.object(facts, "_changes", side_effect=fire_while_deriving):
        NEXT_QUEUE.run(SimpleNamespace(get=lambda _id: instance), "inst", old_work, now=edge)
    assert moved and _facts_work(instance, later)
    drain(instance, now=later)
    assert not _facts_work(instance, later)
    assert _served(instance, later) is not None


def test_nothing_is_published_when_the_deadline_cannot_be_armed(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path)
    NEXT_QUEUE.match(instance, now=AT, daemon_id="daemon")
    (work,) = _facts_work(instance)
    with patch.object(facts, "schedule_deadline", side_effect=OSError("journal unavailable")):
        with pytest.raises(OSError, match="journal unavailable"):
            NEXT_QUEUE.run(SimpleNamespace(get=lambda _id: instance), "inst", work, now=AT)
    assert _served(instance, AT) is None
    assert NEXT_QUEUE.health(instance, now=AT)[0].state == "stalled"
    assert not _facts_work(instance)
