"""Tests for decision record and event model invariants."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cruxible_core.decision.types import DecisionEvent, DecisionRecord


def test_finalized_decision_record_requires_all_terminal_fields() -> None:
    with pytest.raises(ValidationError, match="final_decision.*finalized_at"):
        DecisionRecord(
            question="Ship the decision?",
            status="finalized",
            decision_class="recommended",
        )


def test_finalized_decision_record_accepts_complete_terminal_state() -> None:
    record = DecisionRecord(
        question="Ship the decision?",
        status="finalized",
        decision_class="recommended",
        final_decision="Ship",
        finalized_at=datetime.now(timezone.utc),
    )

    assert record.status == "finalized"


def test_decision_event_requires_explicit_timestamps() -> None:
    with pytest.raises(ValidationError, match="started_at"):
        DecisionEvent(
            decision_record_id="DR-test",
            command="manual",
            status="success",
            input_digest="sha256:input",
            input_summary="{}",
            finished_at=datetime.now(timezone.utc),
        )
