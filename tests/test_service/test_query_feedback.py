"""Tests for service layer query, feedback, and outcome functions."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import (
    DecisionPolicyMatch,
    DecisionPolicySchema,
    FeedbackProfileSchema,
    FeedbackReasonCodeSchema,
    NamedQuerySchema,
    OutcomeCodeSchema,
    OutcomeProfileSchema,
    TraversalStep,
)
from cruxible_core.errors import (
    ConfigError,
    DataValidationError,
    QueryNotFoundError,
    ReceiptNotFoundError,
)
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.query.types import ProjectedQueryRow, QueryPathRow, QueryRelationshipRow
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service import (
    FeedbackItemInput,
    RelationshipTargetInput,
    service_feedback,
    service_feedback_from_query_result,
    service_feedback_input,
    service_get_feedback_profile,
    service_get_outcome_profile,
    service_outcome,
    service_query,
    service_query_surface,
)
from cruxible_core.service.queries import service_evaluate_query, service_evaluate_query_surface

# ---------------------------------------------------------------------------
# service_query
# ---------------------------------------------------------------------------


class TestQuery:
    def test_basic(self, populated_instance: CruxibleInstance) -> None:
        result = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert result.total_results >= 1
        assert result.receipt_id is not None
        assert result.steps_executed >= 1
        assert result.result_shape == "path"
        assert result.dedupe == "path"

    def test_path_query_returns_structured_rows(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        config = populated_instance.load_config()
        config.named_queries["part_paths_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            dedupe="path",
        )
        populated_instance.save_config(config)

        result = service_query(
            populated_instance,
            "part_paths_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )

        assert result.result_shape == "path"
        assert result.dedupe == "path"
        assert result.results
        row = result.results[0]
        assert isinstance(row, QueryPathRow)
        assert row.entry.entity_type == "Vehicle"
        assert row.result.entity_type == "Part"
        assert row.path[0].alias == "fit"
        assert row.path[0].metadata.assertion.lifecycle.status == "active"

    def test_relationship_query_returns_structured_rows(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        config = populated_instance.load_config()
        config.named_queries["fit_edges_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"verified": True},
                )
            ],
            returns="fits",
            result_shape="relationship",
            dedupe="path",
        )
        populated_instance.save_config(config)

        result = service_query(
            populated_instance,
            "fit_edges_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )

        assert result.result_shape == "relationship"
        assert result.results
        row = result.results[0]
        assert isinstance(row, QueryRelationshipRow)
        assert row.relationship_type == "fits"
        assert row.edge_key is not None
        assert row.metadata.assertion.review.status == "unreviewed"

    def test_persists_receipt(self, populated_instance: CruxibleInstance) -> None:
        result = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert result.receipt_id is not None
        store = populated_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None

    def test_evaluate_query_does_not_persist_receipt(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = service_evaluate_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )

        assert result.total_results >= 1
        assert result.receipt_id is not None
        store = populated_instance.get_receipt_store()
        try:
            assert store.get_receipt(result.receipt_id) is None
        finally:
            store.close()

    def test_evaluate_query_surface_applies_limit_without_persisting_receipt(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = service_evaluate_query_surface(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
            limit=1,
        )

        assert len(result.results) == 1
        assert result.total_results >= 1
        assert result.limit == 1
        assert result.truncated is (result.total_results > 1)
        assert result.limit_truncated is (result.total_results > 1)
        if result.total_results > 1:
            assert "response_limit" in result.truncation_reasons
        assert result.receipt_id is not None
        store = populated_instance.get_receipt_store()
        try:
            assert store.get_receipt(result.receipt_id) is None
        finally:
            store.close()

    def test_stamps_receipt_with_head_snapshot_id(
        self, populated_instance: CruxibleInstance
    ) -> None:
        snapshot = populated_instance.create_snapshot()
        result = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )

        assert result.receipt is not None
        assert result.receipt.head_snapshot_id == snapshot.snapshot_id
        assert result.receipt.committed is False
        assert "head_snapshot_id" not in result.receipt.nodes[0].detail
        assert result.receipt_id is not None
        store = populated_instance.get_receipt_store()
        try:
            persisted = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert persisted is not None
        assert persisted.head_snapshot_id == snapshot.snapshot_id
        assert persisted.committed is False

    def test_bad_name(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(QueryNotFoundError):
            service_query(populated_instance, "nonexistent_query", {})

    def test_query_policy_suppresses_results(self, populated_instance: CruxibleInstance) -> None:
        config = populated_instance.load_config()
        config.decision_policies.append(
            DecisionPolicySchema(
                name="suppress_brake_parts",
                applies_to="query",
                query_name="parts_for_vehicle",
                relationship_type="fits",
                effect="suppress",
                match=DecisionPolicyMatch(
                    **{
                        "from": {"category": "brakes"},
                        "to": {"make": "Honda"},
                    }
                ),
            )
        )
        populated_instance.save_config(config)

        result = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert result.total_results == 0
        assert result.policy_summary == {"suppress_brake_parts": 2}

    def test_expired_query_policy_is_ignored(self, populated_instance: CruxibleInstance) -> None:
        config = populated_instance.load_config()
        config.decision_policies.append(
            DecisionPolicySchema(
                name="expired_suppress_brake_parts",
                applies_to="query",
                query_name="parts_for_vehicle",
                relationship_type="fits",
                effect="suppress",
                match=DecisionPolicyMatch(**{"from": {"category": "brakes"}}),
                expires_at="2020-01-01T00:00:00Z",
            )
        )
        populated_instance.save_config(config)

        result = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert result.total_results >= 1
        assert result.policy_summary == {}

    def test_surface_query_applies_limit_and_truncation(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = service_query_surface(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
            limit=1,
        )

        assert len(result.results) == 1
        assert result.total_results >= 1
        assert result.limit == 1
        assert result.truncated is (result.total_results > 1)
        assert result.limit_truncated is (result.total_results > 1)
        if result.total_results > 1:
            assert "response_limit" in result.truncation_reasons
        assert result.receipt_id is not None

    def test_surface_query_rejects_invalid_limit(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(ConfigError, match="limit must be a positive integer"):
            service_query_surface(
                populated_instance,
                "parts_for_vehicle",
                {"vehicle_id": "V-2024-CIVIC-EX"},
                limit=0,
            )


# ---------------------------------------------------------------------------
# service_feedback
# ---------------------------------------------------------------------------


def _edge_target() -> RelationshipInstance:
    return RelationshipInstance(
        from_type="Part",
        from_id="BP-1001",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-2024-CIVIC-EX",
    )


def _persist_receipt(instance: CruxibleInstance, receipt) -> str:
    with instance.write_transaction() as uow:
        return uow.receipts.save_receipt(receipt)


def _get_receipt(instance: CruxibleInstance, receipt_id: str):
    store = instance.get_receipt_store()
    try:
        return store.get_receipt(receipt_id)
    finally:
        store.close()


def _set_review_status(
    instance: CruxibleInstance,
    target: RelationshipInstance,
    status: str,
) -> None:
    graph = instance.load_graph()
    rel = graph.get_relationship(
        target.from_type,
        target.from_id,
        target.to_type,
        target.to_id,
        target.relationship_type,
        edge_key=target.edge_key,
    )
    assert rel is not None
    rel.metadata.assertion.review.status = status  # type: ignore[assignment]
    rel.metadata.assertion.review.source = "system"
    updated = graph.update_relationship_state(
        rel.from_type,
        rel.from_id,
        rel.to_type,
        rel.to_id,
        rel.relationship_type,
        metadata=rel.metadata,
        edge_key=rel.edge_key,
    )
    assert updated is True
    instance.save_graph(graph)


def _add_relationship_query(
    instance: CruxibleInstance,
    *,
    name: str = "fit_edges_for_vehicle",
    relationship_state: str = "live",
) -> None:
    config = instance.load_config()
    config.named_queries[name] = NamedQuerySchema(
        mode="traversal",
        entry_point="Vehicle",
        traversal=[
            TraversalStep(
                relationship="fits",
                direction="incoming",
                filter={"source": "catalog"},
            )
        ],
        returns="fits",
        result_shape="relationship",
        dedupe="path",
        relationship_state=relationship_state,  # type: ignore[arg-type]
    )
    instance.save_config(config)


def _add_single_hop_path_query(instance: CruxibleInstance) -> None:
    config = instance.load_config()
    config.named_queries["fit_path_for_vehicle"] = NamedQuerySchema(
        mode="traversal",
        entry_point="Vehicle",
        traversal=[
            TraversalStep(
                relationship="fits",
                direction="incoming",
                filter={"source": "catalog"},
                alias="fit",
            )
        ],
        returns="list[Part]",
        result_shape="path",
        dedupe="path",
    )
    instance.save_config(config)


def _add_multi_hop_path_query(instance: CruxibleInstance) -> None:
    config = instance.load_config()
    config.named_queries["replacement_path_for_vehicle"] = NamedQuerySchema(
        mode="traversal",
        entry_point="Vehicle",
        traversal=[
            TraversalStep(
                relationship="fits",
                direction="incoming",
                filter={"source": "catalog"},
                alias="fit",
            ),
            TraversalStep(
                relationship="replaces",
                direction="incoming",
                alias="replacement",
            ),
        ],
        returns="list[Part]",
        result_shape="path",
        dedupe="path",
    )
    instance.save_config(config)


class TestFeedback:
    def _run_query(self, instance: CruxibleInstance) -> str:
        """Run a query and return the receipt_id."""
        result = service_query(
            instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert result.receipt_id is not None
        return result.receipt_id

    def test_approve(self, populated_instance: CruxibleInstance) -> None:
        receipt_id = self._run_query(populated_instance)
        result = service_feedback(
            populated_instance,
            receipt_id=receipt_id,
            action="approve",
            source="human",
            target=_edge_target(),
        )
        assert result.feedback_id.startswith("FB-")
        assert result.applied is True

        graph = populated_instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1001", "Vehicle", "V-2024-CIVIC-EX", "fits")
        assert rel is not None
        assert rel.metadata.assertion.review.status == "approved"
        assert rel.metadata.assertion.review.source == "human"

    def test_input_wrapper(self, populated_instance: CruxibleInstance) -> None:
        receipt_id = self._run_query(populated_instance)
        result = service_feedback_input(
            populated_instance,
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
            ),
            source="human",
        )

        assert result.feedback_id.startswith("FB-")
        assert result.applied is True

    def test_validates_correction_property_schema(
        self, populated_instance: CruxibleInstance
    ) -> None:
        receipt_id = self._run_query(populated_instance)
        with pytest.raises(DataValidationError, match="must be a bool"):
            service_feedback(
                populated_instance,
                receipt_id=receipt_id,
                action="correct",
                source="human",
                target=_edge_target(),
                corrections={"verified": "yes"},
            )

    def test_rejects_metadata_keys_in_corrections(
        self, populated_instance: CruxibleInstance
    ) -> None:
        receipt_id = self._run_query(populated_instance)
        with pytest.raises(DataValidationError, match="unexpected property '_provenance'"):
            service_feedback(
                populated_instance,
                receipt_id=receipt_id,
                action="correct",
                source="human",
                target=_edge_target(),
                corrections={"_provenance": {"spoofed": True}, "source": "catalog"},
            )

    def test_missing_receipt(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ReceiptNotFoundError):
            service_feedback(
                populated_instance,
                receipt_id="nonexistent-receipt",
                action="approve",
                source="human",
                target=_edge_target(),
            )

    def test_store_lifecycle(self, populated_instance: CruxibleInstance) -> None:
        """Verify stores are closed even on error."""
        with pytest.raises(ReceiptNotFoundError):
            service_feedback(
                populated_instance,
                receipt_id="bad-id",
                action="approve",
                source="human",
                target=_edge_target(),
            )
        # Should be able to open stores again without issues
        store = populated_instance.get_receipt_store()
        store.close()

    def test_invalid_action(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="Invalid action"):
            service_feedback(
                populated_instance,
                receipt_id="any",
                action="bogus",  # type: ignore[arg-type]
                source="human",
                target=_edge_target(),
            )

    def test_invalid_source(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="Invalid source"):
            service_feedback(
                populated_instance,
                receipt_id="any",
                action="approve",
                source="bogus",  # type: ignore[arg-type]
                target=_edge_target(),
            )

    def test_invalid_corrections_type(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="corrections must be an object"):
            service_feedback(
                populated_instance,
                receipt_id="any",
                action="correct",
                source="human",
                target=_edge_target(),
                corrections="not a dict",  # type: ignore[arg-type]
            )

    def test_profile_requires_reason_code_for_system(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=1,
            reason_codes={
                "vendor_mismatch": FeedbackReasonCodeSchema(
                    description="Vendor mismatch",
                    remediation_hint="constraint",
                )
            },
            scope_keys={},
        )
        populated_instance.save_config(config)
        receipt_id = self._run_query(populated_instance)

        with pytest.raises(ConfigError, match="requires reason_code"):
            service_feedback(
                populated_instance,
                receipt_id=receipt_id,
                action="reject",
                source="agent",
                target=_edge_target(),
            )

    def test_get_feedback_profile(self, populated_instance: CruxibleInstance) -> None:
        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=1,
            reason_codes={
                "vendor_mismatch": FeedbackReasonCodeSchema(
                    description="Vendor mismatch",
                    remediation_hint="constraint",
                )
            },
            scope_keys={},
        )
        populated_instance.save_config(config)

        profile = service_get_feedback_profile(populated_instance, "fits")

        assert profile is not None
        assert "vendor_mismatch" in profile.reason_codes


class TestFeedbackFromQuery:
    def test_relationship_row_can_be_approved_from_query_receipt(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        _add_relationship_query(populated_instance)
        query = service_query(
            populated_instance,
            "fit_edges_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        result = service_feedback_from_query_result(
            populated_instance,
            receipt_id=query.receipt_id,
            result_index=0,
            action="approve",
            source="human",
            reason="catalog evidence accepted",
        )

        assert result.applied is True
        row = query.results[0]
        assert isinstance(row, QueryRelationshipRow)
        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1001",
            "Vehicle",
            "V-2024-CIVIC-EX",
            "fits",
            edge_key=row.edge_key,
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "approved"
        assert result.receipt_id is not None
        feedback_receipt = _get_receipt(populated_instance, result.receipt_id)
        assert feedback_receipt is not None
        detail = feedback_receipt.nodes[0].detail["parameters"]["feedback_from_query"]
        assert detail["receipt_id"] == query.receipt_id
        assert detail["result_index"] == 0
        assert detail["result_shape"] == "relationship"
        assert detail["resolved_target"]["relationship_type"] == "fits"
        assert detail["action"] == "approve"
        assert detail["reason"] == "catalog evidence accepted"

    def test_feedback_from_query_supports_profiled_agent_feedback(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=1,
            reason_codes={
                "vendor_mismatch": FeedbackReasonCodeSchema(
                    description="Vendor mismatch",
                    remediation_hint="constraint",
                )
            },
            scope_keys={},
        )
        populated_instance.save_config(config)
        _add_relationship_query(populated_instance)
        query = service_query(
            populated_instance,
            "fit_edges_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        result = service_feedback_from_query_result(
            populated_instance,
            receipt_id=query.receipt_id,
            result_index=0,
            action="reject",
            source="agent",
            reason_code="vendor_mismatch",
            scope_hints={},
        )

        assert result.applied is True
        row = query.results[0]
        assert isinstance(row, QueryRelationshipRow)
        rel = populated_instance.load_graph().get_relationship(
            row.from_type,
            row.from_id,
            row.to_type,
            row.to_id,
            row.relationship_type,
            edge_key=row.edge_key,
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "rejected"
        assert rel.metadata.assertion.review.source == "agent"
        assert result.receipt_id is not None
        feedback_receipt = _get_receipt(populated_instance, result.receipt_id)
        assert feedback_receipt is not None
        detail = feedback_receipt.nodes[0].detail["parameters"]["feedback_from_query"]
        assert detail["reason_code"] == "vendor_mismatch"
        assert detail["scope_hints"] == {}

    def test_projected_relationship_row_can_be_approved_from_source_evidence(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        config = populated_instance.load_config()
        config.named_queries["projected_fit_edges_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"source": "catalog"},
                )
            ],
            returns="fits",
            result_shape="relationship",
            select={
                "part_id": "$from_entity.entity_id",
                "edge_key": "$relationship.edge_key",
            },
        )
        populated_instance.save_config(config)
        query = service_query(
            populated_instance,
            "projected_fit_edges_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None
        row = query.results[0]
        assert isinstance(row, ProjectedQueryRow)
        assert isinstance(row.source, QueryRelationshipRow)

        result = service_feedback_from_query_result(
            populated_instance,
            receipt_id=query.receipt_id,
            result_index=0,
            action="approve",
        )

        assert result.applied is True
        rel = populated_instance.load_graph().get_relationship(
            row.source.from_type,
            row.source.from_id,
            row.source.to_type,
            row.source.to_id,
            row.source.relationship_type,
            edge_key=row.source.edge_key,
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "approved"

    def test_projected_path_row_can_be_approved_from_source_evidence(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        config = populated_instance.load_config()
        config.named_queries["projected_fit_path_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    filter={"source": "catalog"},
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            select={
                "part_id": "$result.entity_id",
                "review_status": "$path.fit.edge.metadata.assertion.review.status",
            },
        )
        populated_instance.save_config(config)
        query = service_query(
            populated_instance,
            "projected_fit_path_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None
        row = query.results[0]
        assert isinstance(row, ProjectedQueryRow)
        assert isinstance(row.source, QueryPathRow)

        result = service_feedback_from_query_result(
            populated_instance,
            receipt_id=query.receipt_id,
            result_index=0,
            action="approve",
        )

        assert result.applied is True
        segment = row.source.path[0]
        rel = populated_instance.load_graph().get_relationship(
            segment.from_type,
            segment.from_id,
            segment.to_type,
            segment.to_id,
            segment.relationship_type,
            edge_key=segment.edge_key,
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "approved"

    def test_path_row_with_one_segment_can_be_approved_without_selector(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        _add_single_hop_path_query(populated_instance)
        query = service_query(
            populated_instance,
            "fit_path_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        result = service_feedback_from_query_result(
            populated_instance,
            receipt_id=query.receipt_id,
            result_index=0,
            action="approve",
        )

        assert result.applied is True
        row = query.results[0]
        assert isinstance(row, QueryPathRow)
        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1001",
            "Vehicle",
            "V-2024-CIVIC-EX",
            "fits",
            edge_key=row.path[0].edge_key,
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "approved"

    def test_multi_hop_path_requires_selector(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        _add_multi_hop_path_query(populated_instance)
        query = service_query(
            populated_instance,
            "replacement_path_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        with pytest.raises(ConfigError, match="requires path_index or path_alias"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=query.receipt_id,
                result_index=0,
                action="approve",
            )

    def test_multi_hop_path_can_select_by_index(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        _add_multi_hop_path_query(populated_instance)
        query = service_query(
            populated_instance,
            "replacement_path_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        result = service_feedback_from_query_result(
            populated_instance,
            receipt_id=query.receipt_id,
            result_index=0,
            action="approve",
            path_index=1,
        )

        assert result.applied is True
        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Part",
            "BP-1001",
            "replaces",
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "approved"

    def test_multi_hop_path_can_select_by_alias(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        _add_multi_hop_path_query(populated_instance)
        query = service_query(
            populated_instance,
            "replacement_path_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        result = service_feedback_from_query_result(
            populated_instance,
            receipt_id=query.receipt_id,
            result_index=0,
            action="approve",
            path_alias="fit",
        )

        assert result.applied is True
        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1001",
            "Vehicle",
            "V-2024-CIVIC-EX",
            "fits",
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "approved"

    def test_entity_row_is_rejected(self, populated_instance: CruxibleInstance) -> None:
        config = populated_instance.load_config()
        config.named_queries["entity_parts_for_vehicle"] = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
            result_shape="entity",
            dedupe="entity",
        )
        populated_instance.save_config(config)
        query = service_query(
            populated_instance,
            "entity_parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        with pytest.raises(ConfigError, match="Entity query rows do not contain"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=query.receipt_id,
                result_index=0,
                action="approve",
            )

    def test_invalid_receipt_and_result_selection_errors(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        _add_relationship_query(populated_instance)
        query = service_query(
            populated_instance,
            "fit_edges_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        with pytest.raises(ReceiptNotFoundError):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id="missing",
                result_index=0,
                action="approve",
            )
        with pytest.raises(ConfigError, match="out of range"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=query.receipt_id,
                result_index=99,
                action="approve",
            )
        with pytest.raises(ConfigError, match="do not accept path_index"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=query.receipt_id,
                result_index=0,
                action="approve",
                path_index=0,
            )

    def test_non_query_receipt_is_rejected(self, populated_instance: CruxibleInstance) -> None:
        receipt = ReceiptBuilder(operation_type="feedback", parameters={}).build(results=[])
        receipt_id = _persist_receipt(populated_instance, receipt)

        with pytest.raises(ConfigError, match="not 'query'"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=receipt_id,
                result_index=0,
                action="approve",
            )

    def test_invalid_path_alias_and_index_errors(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        _add_multi_hop_path_query(populated_instance)
        query = service_query(
            populated_instance,
            "replacement_path_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        with pytest.raises(ConfigError, match="Provide either path_index or path_alias"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=query.receipt_id,
                result_index=0,
                action="approve",
                path_index=0,
                path_alias="fit",
            )
        with pytest.raises(ConfigError, match="out of range"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=query.receipt_id,
                result_index=0,
                action="approve",
                path_index=9,
            )
        with pytest.raises(ConfigError, match="was not found"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=query.receipt_id,
                result_index=0,
                action="approve",
                path_alias="missing",
            )

    def test_duplicate_path_alias_errors(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        _add_multi_hop_path_query(populated_instance)
        query = service_query(
            populated_instance,
            "replacement_path_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt is not None
        assert query.receipt_id is not None
        row = query.receipt.results[0]
        row["path"][0]["alias"] = "duplicate"
        row["path"][1]["alias"] = "duplicate"
        receipt = query.receipt.model_copy(update={"results": [row]})
        receipt_id = _persist_receipt(populated_instance, receipt)

        with pytest.raises(ConfigError, match="duplicated"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=receipt_id,
                result_index=0,
                action="approve",
                path_alias="duplicate",
            )

    def test_selected_edge_missing_errors(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        _add_relationship_query(populated_instance)
        query = service_query(
            populated_instance,
            "fit_edges_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None
        row = query.results[0]
        assert isinstance(row, QueryRelationshipRow)
        graph = populated_instance.load_graph()
        assert graph.remove_relationship(
            row.from_type,
            row.from_id,
            row.to_type,
            row.to_id,
            row.relationship_type,
            edge_key=row.edge_key,
        )
        populated_instance.save_graph(graph)

        with pytest.raises(ConfigError, match="not found in the graph"):
            service_feedback_from_query_result(
                populated_instance,
                receipt_id=query.receipt_id,
                result_index=0,
                action="approve",
            )

    def test_pending_relationship_becomes_approved_from_query_feedback(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        target = _edge_target()
        _set_review_status(populated_instance, target, "pending")
        _add_relationship_query(
            populated_instance,
            name="pending_fit_edges_for_vehicle",
            relationship_state="pending",
        )
        _add_relationship_query(
            populated_instance,
            name="live_fit_edges_for_vehicle",
            relationship_state="live",
        )
        query = service_query(
            populated_instance,
            "pending_fit_edges_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None
        assert query.results

        result = service_feedback_from_query_result(
            populated_instance,
            receipt_id=query.receipt_id,
            result_index=0,
            action="approve",
            source="human",
        )

        assert result.applied is True
        rel = populated_instance.load_graph().get_relationship(
            target.from_type,
            target.from_id,
            target.to_type,
            target.to_id,
            target.relationship_type,
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "approved"

        accepted = service_query(
            populated_instance,
            "live_fit_edges_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        pending = service_query(
            populated_instance,
            "pending_fit_edges_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert accepted.results
        assert pending.results == []


# ---------------------------------------------------------------------------
# service_outcome
# ---------------------------------------------------------------------------


class TestOutcome:
    def _run_query(self, instance: CruxibleInstance) -> str:
        result = service_query(
            instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert result.receipt_id is not None
        return result.receipt_id

    def test_basic(self, populated_instance: CruxibleInstance) -> None:
        receipt_id = self._run_query(populated_instance)
        result = service_outcome(
            populated_instance,
            receipt_id=receipt_id,
            outcome="correct",
        )
        assert result.outcome_id.startswith("OUT-")

    def test_receipt_profile_requires_outcome_code_for_system(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.outcome_profiles["query_quality"] = OutcomeProfileSchema(
            anchor_type="receipt",
            surface_type="query",
            surface_name="parts_for_vehicle",
            outcome_codes={
                "bad_result": OutcomeCodeSchema(
                    description="Bad result",
                    remediation_hint="provider_fix",
                )
            },
            scope_keys={"surface": "SURFACE.name"},
        )
        populated_instance.save_config(config)
        receipt_id = self._run_query(populated_instance)

        with pytest.raises(ConfigError, match="requires outcome_code"):
            service_outcome(
                populated_instance,
                receipt_id=receipt_id,
                outcome="incorrect",
                source="agent",
            )

    def test_human_receipt_outcome_may_omit_code(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.outcome_profiles["query_quality"] = OutcomeProfileSchema(
            anchor_type="receipt",
            surface_type="query",
            surface_name="parts_for_vehicle",
            outcome_codes={
                "bad_result": OutcomeCodeSchema(
                    description="Bad result",
                    remediation_hint="provider_fix",
                )
            },
            scope_keys={"surface": "SURFACE.name"},
        )
        populated_instance.save_config(config)
        receipt_id = self._run_query(populated_instance)

        result = service_outcome(
            populated_instance,
            receipt_id=receipt_id,
            outcome="partial",
            source="human",
        )
        assert result.outcome_id.startswith("OUT-")

    def test_get_outcome_profile_returns_matching_receipt_profile(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.outcome_profiles["query_quality"] = OutcomeProfileSchema(
            anchor_type="receipt",
            surface_type="query",
            surface_name="parts_for_vehicle",
            outcome_codes={
                "bad_result": OutcomeCodeSchema(
                    description="Bad result",
                    remediation_hint="provider_fix",
                )
            },
            scope_keys={"surface": "SURFACE.name"},
        )
        populated_instance.save_config(config)

        profile_key, profile = service_get_outcome_profile(
            populated_instance,
            anchor_type="receipt",
            surface_type="query",
            surface_name="parts_for_vehicle",
        )
        assert profile_key == "query_quality"
        assert profile is not None

    def test_missing_receipt(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ReceiptNotFoundError):
            service_outcome(
                populated_instance,
                receipt_id="nonexistent-receipt",
                outcome="correct",
            )

    def test_invalid_value(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="Invalid outcome"):
            service_outcome(
                populated_instance,
                receipt_id="any",
                outcome="bogus",  # type: ignore[arg-type]
            )

    def test_invalid_detail_type(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="detail must be an object"):
            service_outcome(
                populated_instance,
                receipt_id="any",
                outcome="correct",
                detail="not a dict",  # type: ignore[arg-type]
            )
