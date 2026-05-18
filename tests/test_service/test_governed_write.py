"""Tests for governed-write service operations."""

from __future__ import annotations

import pytest

from cruxible_core.errors import ReceiptNotFoundError, RelationshipAmbiguityError
from cruxible_core.feedback.types import FeedbackBatchItem
from cruxible_core.graph.assertion_state import load_assertion_state
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.service import (
    FeedbackItemInput,
    RelationshipTargetInput,
    service_feedback,
    service_feedback_batch,
    service_feedback_batch_inputs,
    service_query,
)


def test_service_feedback_batch_applies_atomically(populated_instance):
    query_result = service_query(
        populated_instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
    )
    receipt_id = query_result.receipt_id
    assert receipt_id is not None

    result = service_feedback_batch(
        populated_instance,
        [
            FeedbackBatchItem(
                receipt_id=receipt_id,
                action="approve",
                target=RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1001",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-CIVIC-EX",
                ),
            ),
            FeedbackBatchItem(
                receipt_id=receipt_id,
                action="reject",
                target=RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-CIVIC-EX",
                ),
                reason="bad fitment",
            ),
        ],
        source="human",
    )

    assert result.total == 2
    assert result.applied_count == 2
    assert len(result.feedback_ids) == 2
    assert result.receipt_id is not None

    graph = populated_instance.load_graph()
    approved = graph.get_relationship("Part", "BP-1001", "Vehicle", "V-2024-CIVIC-EX", "fits")
    rejected = graph.get_relationship("Part", "BP-1002", "Vehicle", "V-2024-CIVIC-EX", "fits")
    assert approved is not None
    assert approved.properties["review_status"] == "human_approved"
    approved_state = load_assertion_state(approved.properties)
    assert approved_state.review.status == "approved"
    assert approved_state.review.source == "human"
    assert rejected is not None
    assert rejected.properties["review_status"] == "human_rejected"
    rejected_state = load_assertion_state(rejected.properties)
    assert rejected_state.review.status == "rejected"
    assert rejected_state.review.source == "human"

    feedback_store = populated_instance.get_feedback_store()
    try:
        records = feedback_store.list_feedback(limit=10)
    finally:
        feedback_store.close()
    assert len(records) == 2
    assert {record.receipt_id for record in records} == {receipt_id}


def test_service_feedback_batch_input_wrapper(populated_instance):
    query_result = service_query(
        populated_instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
    )
    receipt_id = query_result.receipt_id
    assert receipt_id is not None

    result = service_feedback_batch_inputs(
        populated_instance,
        [
            FeedbackItemInput(
                receipt_id=receipt_id,
                action="approve",
                target=RelationshipTargetInput(
                    from_type="Part",
                    from_id="BP-1001",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-CIVIC-EX",
                ),
            )
        ],
        source="human",
    )

    assert result.total == 1
    assert result.applied_count == 1


def test_service_feedback_batch_invalid_receipt_rolls_back(populated_instance):
    with pytest.raises(ReceiptNotFoundError):
        service_feedback_batch(
            populated_instance,
            [
                FeedbackBatchItem(
                    receipt_id="RCP-missing",
                    action="approve",
                    target=RelationshipInstance(
                        from_type="Part",
                        from_id="BP-1001",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-CIVIC-EX",
                    ),
                )
            ],
            source="human",
        )

    feedback_store = populated_instance.get_feedback_store()
    try:
        assert feedback_store.count_feedback() == 0
    finally:
        feedback_store.close()


def test_service_feedback_ambiguous_edge_rolls_back_feedback_record(populated_instance):
    query_result = service_query(
        populated_instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
    )
    receipt_id = query_result.receipt_id
    assert receipt_id is not None

    graph = populated_instance.load_graph()
    graph.add_relationship(
        RelationshipInstance(
            from_type="Part",
            from_id="BP-1001",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-CIVIC-EX",
            properties={"verified": False, "source": "duplicate"},
        )
    )
    populated_instance.save_graph(graph)

    with pytest.raises(RelationshipAmbiguityError):
        service_feedback(
            populated_instance,
            receipt_id=receipt_id,
            action="approve",
            source="human",
            target=RelationshipInstance(
                from_type="Part",
                from_id="BP-1001",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-2024-CIVIC-EX",
            ),
        )

    feedback_store = populated_instance.get_feedback_store()
    try:
        assert feedback_store.count_feedback() == 0
    finally:
        feedback_store.close()
