"""Tests for service layer query, feedback, and outcome functions."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import (
    DecisionPolicyMatch,
    DecisionPolicySchema,
    FeedbackProfileSchema,
    FeedbackReasonCodeSchema,
    OutcomeCodeSchema,
    OutcomeProfileSchema,
)
from cruxible_core.errors import (
    ConfigError,
    DataValidationError,
    QueryNotFoundError,
    ReceiptNotFoundError,
)
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.service import (
    service_feedback,
    service_get_feedback_profile,
    service_get_outcome_profile,
    service_outcome,
    service_query,
    service_query_surface,
)

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
        assert result.truncated is (result.total_results > 1)
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
        assert rel.properties.get("review_status") == "human_approved"

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

    def test_strips_provenance(self, populated_instance: CruxibleInstance) -> None:
        receipt_id = self._run_query(populated_instance)
        result = service_feedback(
            populated_instance,
            receipt_id=receipt_id,
            action="correct",
            source="human",
            target=_edge_target(),
            corrections={"_provenance": {"spoofed": True}, "source": "catalog"},
        )
        assert result.applied is True

        graph = populated_instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1001", "Vehicle", "V-2024-CIVIC-EX", "fits")
        assert rel is not None
        # _provenance should not contain the spoofed value
        prov = rel.properties.get("_provenance", {})
        assert prov.get("spoofed") is None

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
