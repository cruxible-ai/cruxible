"""Tests for relationship assertion-state helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cruxible_core.graph.assertion_state import (
    ASSERTION_PROPERTY,
    LEGACY_REVIEW_STATUS_PROPERTY,
    RelationshipAssertionState,
    RelationshipLifecycleState,
    RelationshipReviewState,
    dump_assertion_state,
    legacy_review_status_to_review_state,
    load_assertion_state,
    relationship_is_live,
)
from cruxible_core.graph.types import SYSTEM_OWNED_PROPERTIES


@pytest.mark.parametrize(
    ("legacy", "status", "source"),
    [
        ("human_approved", "approved", "human"),
        ("agent_approved", "approved", "agent"),
        ("human_rejected", "rejected", "human"),
        ("agent_rejected", "rejected", "agent"),
        ("pending_review", "pending", "system"),
        (None, "unreviewed", "system"),
    ],
)
def test_legacy_review_statuses_map_to_review_state(
    legacy: str | None,
    status: str,
    source: str,
) -> None:
    review = legacy_review_status_to_review_state(legacy)

    assert review.status == status
    assert review.source == source


def test_missing_assertion_defaults_to_unreviewed_active() -> None:
    state = load_assertion_state({})

    assert state.review.status == "unreviewed"
    assert state.review.source == "system"
    assert state.lifecycle.status == "active"


def test_system_owned_properties_include_relationship_system_constants() -> None:
    assert SYSTEM_OWNED_PROPERTIES == frozenset(
        {
            ASSERTION_PROPERTY,
            LEGACY_REVIEW_STATUS_PROPERTY,
            "_provenance",
        }
    )


def test_unknown_legacy_review_status_does_not_enter_typed_state() -> None:
    state = load_assertion_state({"review_status": "accepted"})

    assert state.review.status == "unreviewed"
    assert state.review.source == "system"
    assert dump_assertion_state(state) == {
        "review": {"status": "unreviewed", "source": "system"},
        "lifecycle": {"status": "active"},
    }


def test_stale_assertion_legacy_status_is_ignored_on_dump() -> None:
    state = load_assertion_state(
        {
            "_assertion": {
                "review": {
                    "status": "approved",
                    "source": "human",
                    "legacy_status": "human_approved",
                },
                "lifecycle": {"status": "active"},
            }
        }
    )

    assert dump_assertion_state(state) == {
        "review": {"status": "approved", "source": "human"},
        "lifecycle": {"status": "active"},
    }


def test_assertion_wins_over_legacy_review_status() -> None:
    state = load_assertion_state(
        {
            "_assertion": {
                "review": {"status": "approved", "source": "human"},
                "lifecycle": {"status": "active"},
            },
            "review_status": "human_rejected",
        }
    )

    assert state.review.status == "approved"
    assert state.review.source == "human"
    assert relationship_is_live(
        {
            "_assertion": dump_assertion_state(state),
            "review_status": "human_rejected",
        }
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RelationshipAssertionState(), True),
        (
            RelationshipAssertionState(
                review=RelationshipReviewState(status="approved", source="human")
            ),
            True,
        ),
        (
            RelationshipAssertionState(
                review=RelationshipReviewState(status="pending", source="human")
            ),
            False,
        ),
        (
            RelationshipAssertionState(
                review=RelationshipReviewState(status="rejected", source="agent")
            ),
            False,
        ),
        (
            RelationshipAssertionState(
                lifecycle=RelationshipLifecycleState(status="inactive")
            ),
            False,
        ),
    ],
)
def test_relationship_is_live_handles_review_and_lifecycle(
    state: RelationshipAssertionState,
    expected: bool,
) -> None:
    assert relationship_is_live({"_assertion": dump_assertion_state(state)}) is expected


def test_relationship_is_live_can_require_approved() -> None:
    assert relationship_is_live({}, require_approved=True) is False
    assert (
        relationship_is_live(
            {
                "_assertion": dump_assertion_state(
                    RelationshipAssertionState(
                        review=RelationshipReviewState(
                            status="approved",
                            source="human",
                        )
                    )
                )
            },
            require_approved=True,
        )
        is True
    )


def test_relationship_is_live_honors_effective_window() -> None:
    future = RelationshipAssertionState(
        lifecycle=RelationshipLifecycleState(
            status="active",
            effective_from=datetime(2999, 1, 1, tzinfo=timezone.utc),
        )
    )
    expired = RelationshipAssertionState(
        lifecycle=RelationshipLifecycleState(
            status="active",
            effective_until=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert relationship_is_live({"_assertion": dump_assertion_state(future)}) is False
    assert relationship_is_live({"_assertion": dump_assertion_state(expired)}) is False


def test_dumped_assertion_json_is_deterministic_and_round_trips() -> None:
    state = RelationshipAssertionState(
        review=RelationshipReviewState(
            status="approved",
            source="human",
            updated_at=datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
            updated_by="feedback:approve",
        ),
        lifecycle=RelationshipLifecycleState(
            status="inactive",
            reason="replaced",
            closed_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
            closed_by="workflow",
        ),
    )

    dumped = dump_assertion_state(state)
    assert dumped == {
        "review": {
            "status": "approved",
            "source": "human",
            "updated_at": "2026-05-17T12:00:00+00:00",
            "updated_by": "feedback:approve",
        },
        "lifecycle": {
            "status": "inactive",
            "reason": "replaced",
            "closed_at": "2026-05-18T12:00:00+00:00",
            "closed_by": "workflow",
        },
    }
    assert json.dumps(dumped, sort_keys=True) == json.dumps(
        dump_assertion_state(load_assertion_state({"_assertion": dumped})),
        sort_keys=True,
    )
