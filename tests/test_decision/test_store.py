"""Tests for SQLite decision record storage."""

from __future__ import annotations

import sqlite3

import pytest

from cruxible_core.decision.store import DecisionStore
from cruxible_core.decision.types import DecisionEvent, DecisionRecord
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.temporal import format_datetime, utc_now


@pytest.fixture
def store() -> DecisionStore:
    s = DecisionStore(":memory:")
    yield s
    s.close()


def _event(record_id: str) -> DecisionEvent:
    now = utc_now()
    return DecisionEvent(
        decision_record_id=record_id,
        command="query:parts_for_vehicle",
        status="success",
        input_digest="sha256:input",
        input_summary="{}",
        started_at=now,
        finished_at=now,
    )


class TestDecisionStoreConstraints:
    def test_foreign_keys_enabled(self, store: DecisionStore) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO decision_events "
                "(decision_event_id, decision_record_id, sequence, command, status, "
                "input_digest, input_summary, started_at, finished_at, event_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DE-orphan",
                    "DR-missing",
                    1,
                    "query:q",
                    "success",
                    "sha256:input",
                    "{}",
                    format_datetime(utc_now()),
                    format_datetime(utc_now()),
                    "{}",
                ),
            )

    def test_save_record_refuses_to_overwrite_an_existing_record(
        self, store: DecisionStore
    ) -> None:
        record = DecisionRecord(question="Should we investigate?")
        store.save_record(record)

        rewritten = record.model_copy(update={"question": "Something else entirely"})
        with pytest.raises(ConfigError, match="already exists"):
            store.save_record(rewritten)

        assert store.get_record(record.decision_record_id).question == "Should we investigate?"

    def test_find_events_by_trace_id_matches_exact_json_member(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Should we investigate?")
        store.save_record(record)
        trace_1 = _event(record.decision_record_id).model_copy(update={"trace_ids": ["TRC-1"]})
        trace_10 = _event(record.decision_record_id).model_copy(update={"trace_ids": ["TRC-10"]})
        store.append_event(trace_1)
        store.append_event(trace_10)

        events = store.find_events(trace_id="TRC-1")

        assert [event.decision_event_id for event in events] == [trace_1.decision_event_id]


def _actor(actor_type: str = "human_user") -> GovernedActorContext:
    return GovernedActorContext(
        actor_type=actor_type,  # type: ignore[arg-type]
        actor_id="usr_1",
        org_id="org_1",
        operation_id="op_1",
        timestamp="2026-06-05T12:00:00Z",  # type: ignore[arg-type]
    )


class TestDecisionRecordsAreAppendOnly:
    def test_finalized_record_cannot_be_reopened(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Ship it?")
        store.save_record(record)
        finalized = store.finalize_record(
            record.decision_record_id,
            final_decision="ship",
            decision_class="recommended",
        )

        reopened = finalized.model_copy(
            update={
                "status": "open",
                "final_decision": None,
                "decision_class": None,
                "finalized_at": None,
            }
        )
        with pytest.raises(ConfigError) as excinfo:
            store._close_record(reopened)

        message = str(excinfo.value)
        assert record.decision_record_id in message
        assert "finalized" in message
        assert store.get_record(record.decision_record_id).status == "finalized"

    def test_abandoned_record_cannot_be_reopened(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Ship it?")
        store.save_record(record)
        abandoned = store.abandon_record(record.decision_record_id, reason="superseded")

        with pytest.raises(ConfigError) as excinfo:
            store._close_record(abandoned.model_copy(update={"status": "open"}))

        message = str(excinfo.value)
        assert record.decision_record_id in message
        assert "abandoned" in message

    def test_close_record_refuses_to_rewrite_the_opening_claim(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Ship it?", subject_type="Release", subject_id="R-1")
        store.save_record(record)

        with pytest.raises(ConfigError, match="cannot rewrite question"):
            store._close_record(record.model_copy(update={"question": "Something else"}))

        with pytest.raises(ConfigError, match="cannot rewrite subject_id"):
            store._close_record(record.model_copy(update={"subject_id": "R-2"}))

        assert store.get_record(record.decision_record_id).question == "Ship it?"

    def test_close_record_still_rejects_unknown_records(self, store: DecisionStore) -> None:
        with pytest.raises(ConfigError, match="not found"):
            store._close_record(DecisionRecord(question="Never stored"))


class TestTerminalEvents:
    def test_finalize_logs_its_own_event(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Ship it?")
        store.save_record(record)

        store.finalize_record(
            record.decision_record_id,
            final_decision="ship",
            decision_class="recommended",
            rationale="benchmarks held",
            actor_context=_actor(),
        )

        events = store.list_events(record.decision_record_id)
        assert [event.command for event in events] == ["decision_record:finalize"]
        assert events[0].status == "success"
        assert events[0].actor_context is not None
        assert events[0].actor_context.actor_id == "usr_1"

    def test_abandon_logs_its_own_event(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Ship it?")
        store.save_record(record)

        store.abandon_record(record.decision_record_id, reason="superseded")

        events = store.list_events(record.decision_record_id)
        assert [event.command for event in events] == ["decision_record:abandon"]

    def test_terminal_event_lands_after_earlier_events(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Ship it?")
        store.save_record(record)
        store.append_event(_event(record.decision_record_id))

        store.finalize_record(
            record.decision_record_id,
            final_decision="ship",
            decision_class="recommended",
        )

        events = store.list_events(record.decision_record_id)
        assert [event.sequence for event in events] == [1, 2]
        assert events[-1].command == "decision_record:finalize"

    def test_closed_record_still_refuses_further_events(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Ship it?")
        store.save_record(record)
        store.finalize_record(
            record.decision_record_id,
            final_decision="ship",
            decision_class="recommended",
        )

        with pytest.raises(ConfigError, match="is not open"):
            store.append_event(_event(record.decision_record_id))


class TestDerivedOpenedBy:
    @pytest.mark.parametrize(
        ("actor_type", "expected"),
        [("human_user", "human"), ("service_account", "agent"), ("system", "system")],
    )
    def test_opened_by_column_is_derived_from_the_actor_context(
        self, store: DecisionStore, actor_type: str, expected: str
    ) -> None:
        record = DecisionRecord(question="Ship it?", opened_actor_context=_actor(actor_type))
        store.save_record(record)

        row = store._conn.execute(
            "SELECT opened_by FROM decision_records WHERE decision_record_id = ?",
            (record.decision_record_id,),
        ).fetchone()
        assert row["opened_by"] == expected

    def test_missing_actor_context_is_unknown_not_human(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Ship it?")
        store.save_record(record)

        row = store._conn.execute(
            "SELECT opened_by FROM decision_records WHERE decision_record_id = ?",
            (record.decision_record_id,),
        ).fetchone()
        assert row["opened_by"] == "unknown"

    def test_records_no_longer_carry_a_declared_opener(self) -> None:
        assert "opened_by" not in DecisionRecord.model_fields


class TestTerminalTransitionIsRaceSafe:
    """The status check must hold in SQL, not only in the preceding read."""

    def test_a_concurrent_close_loses_instead_of_overwriting(self, tmp_path) -> None:
        """SQLite serializes writers, not read-then-write pairs across connections.

        The status check used to live only in a separate SELECT, so two writers
        could both read ``open`` and both UPDATE — the second silently
        overwriting the first's terminal state and leaving a record whose status
        contradicted its own event log.
        """
        db = tmp_path / "state.db"
        first = DecisionStore(db)
        second = DecisionStore(db)
        try:
            record = DecisionRecord(question="Ship it?")
            first.save_record(record)
            first._conn.commit()

            # Both writers observe an OPEN record before either transition lands.
            assert first.get_record(record.decision_record_id).status == "open"
            assert second.get_record(record.decision_record_id).status == "open"

            first.finalize_record(
                record.decision_record_id,
                final_decision="ship",
                decision_class="recommended",
            )
            first._conn.commit()

            with pytest.raises(ConfigError):
                second.abandon_record(record.decision_record_id, reason="superseded")

            assert second.get_record(record.decision_record_id).status == "finalized"
            assert second.get_record(record.decision_record_id).final_decision == "ship"
        finally:
            first.close()
            second.close()

    def test_the_sql_guard_holds_when_the_preceding_read_is_stale(self, tmp_path) -> None:
        """The UPDATE refuses on its own, without help from the read.

        The read is forced to report ``open`` — exactly what the losing writer of
        a real race observes — so the only thing left to refuse the write is the
        ``AND status = 'open'`` predicate and the rowcount check on it.
        """
        db = tmp_path / "state.db"
        writer = DecisionStore(db)
        racer = DecisionStore(db)
        try:
            record = DecisionRecord(question="Ship it?")
            writer.save_record(record)
            writer._conn.commit()
            stale = racer.get_record(record.decision_record_id)
            assert stale.status == "open"

            writer.finalize_record(
                record.decision_record_id,
                final_decision="ship",
                decision_class="recommended",
            )
            writer._conn.commit()

            racer.get_record = lambda _decision_record_id: stale  # type: ignore[method-assign]
            closed = stale.model_copy(
                update={
                    "status": "abandoned",
                    "abandoned_reason": "superseded",
                    "finalized_at": utc_now(),
                }
            )
            with pytest.raises(ConfigError, match="closed by another writer"):
                racer._close_record(closed)
        finally:
            writer.close()
            racer.close()

        reader = DecisionStore(db)
        try:
            assert reader.get_record(record.decision_record_id).status == "finalized"
            assert reader.get_record(record.decision_record_id).final_decision == "ship"
        finally:
            reader.close()
