"""Tests for relationship assertion helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
    dump_assertion,
    relationship_is_live,
)
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import RelationshipMetadata


def test_default_assertion_is_unreviewed_active() -> None:
    assertion = RelationshipAssertion()

    assert assertion.review.status == "unreviewed"
    assert assertion.review.source == "system"
    assert assertion.lifecycle.status == "active"


def test_relationship_metadata_contains_typed_assertion_and_provenance() -> None:
    metadata = RelationshipMetadata(
        provenance=RelationshipProvenance(source="ingest", source_ref="feed-1"),
        assertion=RelationshipAssertion(
            review=RelationshipReviewState(status="approved", source="human")
        ),
    )

    assert metadata.provenance is not None
    assert metadata.provenance.source == "ingest"
    assert metadata.provenance.source_ref == "feed-1"
    assert metadata.assertion.review.status == "approved"
    assert metadata.assertion.review.source == "human"
    assert metadata.assertion.lifecycle.status == "active"


@pytest.mark.parametrize(
    ("assertion", "expected"),
    [
        (RelationshipAssertion(), True),
        (
            RelationshipAssertion(
                review=RelationshipReviewState(status="approved", source="human")
            ),
            True,
        ),
        (
            RelationshipAssertion(
                review=RelationshipReviewState(status="pending", source="human")
            ),
            False,
        ),
        (
            RelationshipAssertion(
                review=RelationshipReviewState(status="rejected", source="agent")
            ),
            False,
        ),
        (
            RelationshipAssertion(lifecycle=RelationshipLifecycleState(status="inactive")),
            False,
        ),
    ],
)
def test_relationship_is_live_handles_review_and_lifecycle(
    assertion: RelationshipAssertion,
    expected: bool,
) -> None:
    assert relationship_is_live(assertion) is expected
    assert relationship_is_live(RelationshipMetadata(assertion=assertion)) is expected


def test_relationship_is_live_can_require_approved() -> None:
    assert relationship_is_live(RelationshipAssertion(), require_approved=True) is False
    assert (
        relationship_is_live(
            RelationshipAssertion(
                review=RelationshipReviewState(
                    status="approved",
                    source="human",
                )
            ),
            require_approved=True,
        )
        is True
    )


def test_relationship_is_live_honors_effective_window() -> None:
    future = RelationshipAssertion(
        lifecycle=RelationshipLifecycleState(
            status="active",
            effective_from=datetime(2999, 1, 1, tzinfo=timezone.utc),
        )
    )
    expired = RelationshipAssertion(
        lifecycle=RelationshipLifecycleState(
            status="active",
            effective_until=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert relationship_is_live(future) is False
    assert relationship_is_live(expired) is False


def test_invalid_assertion_timestamp_is_not_silently_downgraded() -> None:
    with pytest.raises(ValueError):
        RelationshipAssertion.model_validate(
            {
                "review": {"status": "approved", "source": "human"},
                "lifecycle": {
                    "status": "active",
                    "effective_from": "not-a-datetime",
                },
            }
        )


def test_dumped_assertion_json_is_deterministic_and_round_trips() -> None:
    assertion = RelationshipAssertion(
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

    dumped = dump_assertion(assertion)
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
        dump_assertion(RelationshipAssertion.model_validate(dumped)),
        sort_keys=True,
    )
