"""Tests for SQLite decision record storage."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from cruxible_core.decision.store import DecisionStore
from cruxible_core.decision.types import DecisionEvent, DecisionRecord


@pytest.fixture
def store() -> DecisionStore:
    s = DecisionStore(":memory:")
    yield s
    s.close()


def _event(record_id: str) -> DecisionEvent:
    now = datetime.now(timezone.utc)
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
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    "{}",
                ),
            )

    def test_save_record_update_preserves_events(self, store: DecisionStore) -> None:
        record = DecisionRecord(question="Should we investigate?", opened_by="agent")
        store.save_record(record)
        event_id = store.append_event(_event(record.decision_record_id))

        updated = record.model_copy(update={"rationale": "new context"})
        store.save_record(updated)

        events = store.list_events(record.decision_record_id)
        assert [event.decision_event_id for event in events] == [event_id]
        assert store.get_record(record.decision_record_id).rationale == "new context"
