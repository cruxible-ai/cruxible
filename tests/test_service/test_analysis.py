"""Tests for service layer validate and evaluate functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import (
    ConstraintSchema,
    FeedbackProfileSchema,
    FeedbackReasonCodeSchema,
    OutcomeCodeSchema,
    OutcomeProfileSchema,
)
from cruxible_core.errors import ConfigError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import CandidateMember
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import (
    service_analyze_feedback,
    service_analyze_outcomes,
    service_evaluate,
    service_feedback,
    service_lint,
    service_outcome,
    service_propose_group,
    service_query,
    service_resolve_group,
    service_validate,
)
from tests.test_cli.conftest import CAR_PARTS_YAML


class _ClosingGroupStore:
    closed = False

    def get_group(self, group_id: str):
        return None

    def get_members(self, group_id: str):
        return []

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# service_validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_file(self, tmp_project: Path) -> None:
        result = service_validate(config_path=str(tmp_project / "config.yaml"))
        assert result.config is not None
        assert result.config.name == "car_parts_compatibility"

    def test_yaml_string(self) -> None:
        result = service_validate(config_yaml=CAR_PARTS_YAML)
        assert result.config is not None
        assert "Vehicle" in result.config.entity_types

    def test_semantic_errors(self, tmp_path: Path) -> None:
        bad_yaml = """\
version: "1.0"
name: broken
entity_types:
  Thing:
    properties:
      id:
        type: string
        primary_key: true
relationships:
  - name: links
    from: Thing
    to: Nonexistent
"""
        config_file = tmp_path / "bad.yaml"
        config_file.write_text(bad_yaml)
        with pytest.raises(ConfigError, match="cross-reference"):
            service_validate(config_path=str(config_file))

    def test_no_source_error(self) -> None:
        with pytest.raises(ConfigError, match="Provide exactly one"):
            service_validate()

    def test_extends_composes_and_validates(self, tmp_path: Path) -> None:
        """Overlay config with extends is composed in memory before validation."""
        base = tmp_path / "base.yaml"
        base.write_text(
            'version: "1.0"\n'
            "name: base\n"
            "entity_types:\n"
            "  Case:\n"
            "    properties:\n"
            "      case_id: {type: string, primary_key: true}\n"
            "relationships:\n"
            "  - name: cites\n"
            "    from: Case\n"
            "    to: Case\n"
        )
        overlay = tmp_path / "overlay.yaml"
        overlay.write_text(
            'version: "1.0"\n'
            "name: overlay\n"
            "extends: base.yaml\n"
            "entity_types: {}\n"
            "relationships:\n"
            "  - name: follows\n"
            "    from: Case\n"
            "    to: Case\n"
        )
        result = service_validate(config_path=str(overlay))
        assert result.config is not None
        assert "Case" in result.config.entity_types
        assert result.config.get_relationship("cites") is not None
        assert result.config.get_relationship("follows") is not None

    def test_extends_base_not_found(self, tmp_path: Path) -> None:
        overlay = tmp_path / "overlay.yaml"
        overlay.write_text(
            'version: "1.0"\n'
            "name: overlay\n"
            "extends: nonexistent.yaml\n"
            "entity_types: {}\n"
            "relationships: []\n"
        )
        with pytest.raises(ConfigError, match="Base config for extends not found"):
            service_validate(config_path=str(overlay))

    def test_extends_inline_relative_errors(self) -> None:
        yaml_str = (
            'version: "1.0"\n'
            "name: overlay\n"
            "extends: base.yaml\n"
            "entity_types: {}\n"
            "relationships: []\n"
        )
        with pytest.raises(ConfigError, match="relative extends path"):
            service_validate(config_yaml=yaml_str)

    def test_returns_warnings(self, tmp_path: Path) -> None:
        """Config with unverifiable constraint rule produces a warning."""
        yaml_with_constraint = """\
version: "1.0"
name: with_constraints
entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
relationships:
  - name: fits
    from: Part
    to: Vehicle
constraints:
  - name: weird_rule
    rule: "some_unparseable_thing"
    severity: warning
"""
        config_file = tmp_path / "constraints.yaml"
        config_file.write_text(yaml_with_constraint)
        result = service_validate(config_path=str(config_file))
        assert len(result.warnings) >= 1
        assert any("could not verify" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# service_evaluate
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_basic(self, populated_instance: CruxibleInstance) -> None:
        report = service_evaluate(populated_instance)
        assert report.entity_count >= 4
        assert report.edge_count >= 3
        assert isinstance(report.findings, list)
        assert isinstance(report.summary, dict)
        assert isinstance(report.quality_summary, dict)

    def test_constraint_summary_includes_zero_count_constraints(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.constraints.append(
            ConstraintSchema(
                name="replaces_category_match",
                rule="replaces.FROM.category == replaces.TO.category",
            )
        )
        populated_instance.save_config(config)

        report = service_evaluate(populated_instance)
        assert report.constraint_summary["replaces_category_match"] == 0

    def test_exclude_orphan_types(self, populated_instance: CruxibleInstance) -> None:
        report_all = service_evaluate(populated_instance)
        report_excl = service_evaluate(populated_instance, exclude_orphan_types=["Vehicle", "Part"])
        orphans_all = sum(1 for f in report_all.findings if f.category == "orphan_entity")
        orphans_excl = sum(1 for f in report_excl.findings if f.category == "orphan_entity")
        assert orphans_excl <= orphans_all

    def test_closes_group_store(
        self, populated_instance: CruxibleInstance, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _ClosingGroupStore()
        monkeypatch.setattr(populated_instance, "get_group_store", lambda: store)

        service_evaluate(populated_instance)

        assert store.closed is True


class TestAnalyzeFeedback:
    def test_decision_policy_suggestion_and_uncoded_feedback(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=2,
            reason_codes={
                "legacy_unsupported": FeedbackReasonCodeSchema(
                    description="Legacy environment is unsupported",
                    remediation_hint="decision_policy",
                    required_scope_keys=["category", "make"],
                )
            },
            scope_keys={
                "category": "FROM.category",
                "make": "TO.make",
            },
        )
        populated_instance.save_config(config)

        query_one = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        query_two = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query_one.receipt_id is not None
        assert query_two.receipt_id is not None

        service_feedback(
            populated_instance,
            receipt_id=query_one.receipt_id,
            action="reject",
            source="agent",
            target=_feedback_target("BP-1001"),
            reason="Legacy unsupported",
            reason_code="legacy_unsupported",
            scope_hints={"category": "brakes", "make": "Honda"},
        )
        service_feedback(
            populated_instance,
            receipt_id=query_two.receipt_id,
            action="reject",
            source="agent",
            target=_feedback_target("BP-1002"),
            reason="Legacy unsupported",
            reason_code="legacy_unsupported",
            scope_hints={"category": "brakes", "make": "Honda"},
        )
        service_feedback(
            populated_instance,
            receipt_id=query_two.receipt_id,
            action="reject",
            source="human",
            target=_feedback_target("BP-1002"),
            reason="freeform uncoded reason",
        )

        result = service_analyze_feedback(
            populated_instance,
            "fits",
            min_support=2,
            decision_surface_type="query",
            decision_surface_name="parts_for_vehicle",
        )

        assert result.feedback_count == 3
        assert result.uncoded_feedback_count == 1
        assert len(result.coded_groups) == 1
        assert result.coded_groups[0].reason_code == "legacy_unsupported"
        assert len(result.decision_policy_suggestions) == 1
        suggestion = result.decision_policy_suggestions[0]
        assert suggestion.applies_to == "query"
        assert suggestion.effect == "suppress"
        assert suggestion.query_name == "parts_for_vehicle"
        assert suggestion.match["from"] == {"category": "brakes"}
        assert suggestion.match["to"] == {"make": "Honda"}
        assert result.constraint_suggestions == []

    def test_analysis_uses_stored_remediation_hint_across_profile_versions(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=1,
            reason_codes={
                "legacy_unsupported": FeedbackReasonCodeSchema(
                    description="Legacy environment is unsupported",
                    remediation_hint="decision_policy",
                    required_scope_keys=["category", "make"],
                )
            },
            scope_keys={
                "category": "FROM.category",
                "make": "TO.make",
            },
        )
        populated_instance.save_config(config)

        query_one = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        query_two = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query_one.receipt_id is not None
        assert query_two.receipt_id is not None

        service_feedback(
            populated_instance,
            receipt_id=query_one.receipt_id,
            action="reject",
            source="agent",
            target=_feedback_target("BP-1001"),
            reason="Legacy unsupported",
            reason_code="legacy_unsupported",
            scope_hints={"category": "brakes", "make": "Honda"},
        )
        service_feedback(
            populated_instance,
            receipt_id=query_two.receipt_id,
            action="reject",
            source="agent",
            target=_feedback_target("BP-1002"),
            reason="Legacy unsupported",
            reason_code="legacy_unsupported",
            scope_hints={"category": "brakes", "make": "Honda"},
        )

        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=2,
            reason_codes={
                "legacy_unsupported": FeedbackReasonCodeSchema(
                    description="Legacy environment is unsupported",
                    remediation_hint="constraint",
                    required_scope_keys=["category", "make"],
                )
            },
            scope_keys={
                "category": "FROM.category",
                "make": "TO.make",
            },
        )
        populated_instance.save_config(config)

        result = service_analyze_feedback(
            populated_instance,
            "fits",
            min_support=2,
            decision_surface_type="query",
            decision_surface_name="parts_for_vehicle",
        )

        assert len(result.decision_policy_suggestions) == 1
        assert result.constraint_suggestions == []
        assert any("using stored remediation hints" in warning for warning in result.warnings)

    def test_constraint_suggestions_use_feedback_snapshot_not_current_graph(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=1,
            reason_codes={
                "fitment_mismatch": FeedbackReasonCodeSchema(
                    description="Part category mismatches vehicle make",
                    remediation_hint="constraint",
                    required_scope_keys=["category", "make"],
                )
            },
            scope_keys={
                "category": "FROM.category",
                "make": "TO.make",
            },
        )
        populated_instance.save_config(config)

        query_one = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        query_two = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query_one.receipt_id is not None
        assert query_two.receipt_id is not None

        service_feedback(
            populated_instance,
            receipt_id=query_one.receipt_id,
            action="reject",
            source="agent",
            target=_feedback_target("BP-1001"),
            reason="Mismatch",
            reason_code="fitment_mismatch",
            scope_hints={"category": "brakes", "make": "Honda"},
        )
        service_feedback(
            populated_instance,
            receipt_id=query_two.receipt_id,
            action="reject",
            source="agent",
            target=_feedback_target("BP-1002"),
            reason="Mismatch",
            reason_code="fitment_mismatch",
            scope_hints={"category": "brakes", "make": "Honda"},
        )

        graph = populated_instance.load_graph()
        part = graph.get_entity("Part", "BP-1001")
        vehicle = graph.get_entity("Vehicle", "V-2024-CIVIC-EX")
        assert part is not None
        assert vehicle is not None
        part.properties["category"] = "Honda"
        vehicle.properties["make"] = "Honda"
        populated_instance.save_graph(graph)

        result = service_analyze_feedback(
            populated_instance,
            "fits",
            min_support=2,
            decision_surface_type="query",
            decision_surface_name="parts_for_vehicle",
            property_pairs=[("category", "make")],
        )

        assert len(result.constraint_suggestions) == 1
        assert result.constraint_suggestions[0].rule == "fits.FROM.category == fits.TO.make"


class TestAnalyzeOutcomes:
    def test_receipt_outcomes_produce_provider_fix_candidates(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.outcome_profiles["query_quality"] = OutcomeProfileSchema(
            anchor_type="receipt",
            version=1,
            surface_type="query",
            surface_name="parts_for_vehicle",
            outcome_codes={
                "bad_result": OutcomeCodeSchema(
                    description="Bad query result",
                    remediation_hint="provider_fix",
                    required_scope_keys=["surface"],
                )
            },
            scope_keys={"surface": "SURFACE.name"},
        )
        populated_instance.save_config(config)

        query = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        service_outcome(
            populated_instance,
            receipt_id=query.receipt_id,
            outcome="incorrect",
            source="agent",
            outcome_code="bad_result",
            scope_hints={"surface": "parts_for_vehicle"},
        )
        service_outcome(
            populated_instance,
            receipt_id=query.receipt_id,
            outcome="incorrect",
            source="agent",
            outcome_code="bad_result",
            scope_hints={"surface": "parts_for_vehicle"},
        )

        result = service_analyze_outcomes(
            populated_instance,
            anchor_type="receipt",
            query_name="parts_for_vehicle",
            min_support=2,
        )

        assert result.outcome_count == 2
        assert result.outcome_code_counts["bad_result"] == 2
        assert len(result.provider_fix_candidates) == 1
        assert result.provider_fix_candidates[0].surface_name == "parts_for_vehicle"
        assert len(result.workflow_debug_packages) == 1

    def test_outcome_analysis_uses_stored_hint_across_profile_versions(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.outcome_profiles["query_quality"] = OutcomeProfileSchema(
            anchor_type="receipt",
            version=1,
            surface_type="query",
            surface_name="parts_for_vehicle",
            outcome_codes={
                "bad_result": OutcomeCodeSchema(
                    description="Bad query result",
                    remediation_hint="provider_fix",
                    required_scope_keys=["surface"],
                )
            },
            scope_keys={"surface": "SURFACE.name"},
        )
        populated_instance.save_config(config)

        query = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query.receipt_id is not None

        for _ in range(2):
            service_outcome(
                populated_instance,
                receipt_id=query.receipt_id,
                outcome="incorrect",
                source="agent",
                outcome_code="bad_result",
                scope_hints={"surface": "parts_for_vehicle"},
            )

        config = populated_instance.load_config()
        config.outcome_profiles["query_quality"] = OutcomeProfileSchema(
            anchor_type="receipt",
            version=2,
            surface_type="query",
            surface_name="parts_for_vehicle",
            outcome_codes={
                "bad_result": OutcomeCodeSchema(
                    description="Bad query result",
                    remediation_hint="decision_policy",
                    required_scope_keys=["surface"],
                )
            },
            scope_keys={"surface": "SURFACE.name"},
        )
        populated_instance.save_config(config)

        result = service_analyze_outcomes(
            populated_instance,
            anchor_type="receipt",
            query_name="parts_for_vehicle",
            min_support=2,
        )

        assert len(result.provider_fix_candidates) == 1
        assert result.query_policy_suggestions == []
        assert any("using stored remediation hints" in warning for warning in result.warnings)

    def test_resolution_outcomes_produce_trust_adjustment_suggestions(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.outcome_profiles["resolution_quality"] = OutcomeProfileSchema(
            anchor_type="resolution",
            version=1,
            relationship_type="fits",
            outcome_codes={
                "false_positive": OutcomeCodeSchema(
                    description="Approved link was wrong",
                    remediation_hint="trust_adjustment",
                    required_scope_keys=["vendor"],
                )
            },
            scope_keys={"vendor": "THESIS.vendor"},
        )
        populated_instance.save_config(config)

        resolution_id = _create_resolution_anchor(populated_instance)
        for _ in range(2):
            service_outcome(
                populated_instance,
                outcome="incorrect",
                anchor_type="resolution",
                anchor_id=resolution_id,
                source="agent",
                outcome_code="false_positive",
                scope_hints={"vendor": "Honda"},
            )

        result = service_analyze_outcomes(
            populated_instance,
            anchor_type="resolution",
            relationship_type="fits",
            min_support=2,
        )

        assert len(result.trust_adjustment_suggestions) == 1
        suggestion = result.trust_adjustment_suggestions[0]
        assert suggestion.resolution_id == resolution_id
        assert suggestion.suggested_trust_status in {"watch", "invalidated"}

    def test_resolution_outcomes_produce_workflow_review_suggestions(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.outcome_profiles["resolution_review"] = OutcomeProfileSchema(
            anchor_type="resolution",
            version=1,
            relationship_type="fits",
            outcome_codes={
                "needs_review": OutcomeCodeSchema(
                    description="Needs future review",
                    remediation_hint="require_review",
                    required_scope_keys=["vendor"],
                )
            },
            scope_keys={"vendor": "THESIS.vendor"},
        )
        populated_instance.save_config(config)

        resolution_id = _create_resolution_anchor(populated_instance)
        for _ in range(2):
            service_outcome(
                populated_instance,
                outcome="incorrect",
                anchor_type="resolution",
                anchor_id=resolution_id,
                source="agent",
                outcome_code="needs_review",
                scope_hints={"vendor": "Honda"},
            )

        result = service_analyze_outcomes(
            populated_instance,
            anchor_type="resolution",
            relationship_type="fits",
            min_support=2,
        )

        assert len(result.workflow_review_policy_suggestions) == 1
        suggestion = result.workflow_review_policy_suggestions[0]
        assert suggestion.workflow_name == "propose_kev_product_links"
        assert suggestion.match["context"]["vendor"] == "Honda"


class TestLint:
    def test_clean_instance_returns_no_issues(self, populated_instance: CruxibleInstance) -> None:
        graph = populated_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="BP-1001",
                to_type="Part",
                to_id="BP-1002",
                properties={"direction": "downgrade", "confidence": 0.95},
            )
        )
        populated_instance.save_graph(graph)

        result = service_lint(populated_instance)

        assert result.config_name == "car_parts_compatibility"
        assert result.has_issues is False
        assert result.summary.config_warning_count == 0
        assert result.summary.compatibility_warning_count == 0
        assert result.summary.evaluation_finding_count == 0
        assert result.feedback_reports == []
        assert result.outcome_reports == []

    def test_closes_evaluation_group_store(
        self, populated_instance: CruxibleInstance, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores: list[_ClosingGroupStore] = []

        def get_group_store() -> _ClosingGroupStore:
            store = _ClosingGroupStore()
            stores.append(store)
            return store

        monkeypatch.setattr(populated_instance, "get_group_store", get_group_store)

        service_lint(populated_instance)

        assert stores
        assert all(store.closed for store in stores)

    def test_includes_compatibility_warnings(self, populated_instance: CruxibleInstance) -> None:
        graph = populated_instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="UnknownEntity",
                entity_id="UNK-1",
                properties={"unknown_id": "UNK-1"},
            )
        )
        populated_instance.save_graph(graph)

        result = service_lint(populated_instance)

        assert result.has_issues is True
        assert result.summary.compatibility_warning_count == 1
        assert any("UnknownEntity" in warning for warning in result.compatibility_warnings)

    def test_returns_only_actionable_feedback_and_outcome_reports(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=1,
            reason_codes={
                "fitment_mismatch": FeedbackReasonCodeSchema(
                    description="Part category mismatches vehicle make",
                    remediation_hint="quality_check",
                    required_scope_keys=["category", "make"],
                )
            },
            scope_keys={
                "category": "FROM.category",
                "make": "TO.make",
            },
        )
        config.outcome_profiles["query_quality"] = OutcomeProfileSchema(
            anchor_type="receipt",
            version=1,
            surface_type="query",
            surface_name="parts_for_vehicle",
            outcome_codes={
                "bad_result": OutcomeCodeSchema(
                    description="Bad query result",
                    remediation_hint="provider_fix",
                    required_scope_keys=["surface"],
                )
            },
            scope_keys={"surface": "SURFACE.name"},
        )
        populated_instance.save_config(config)

        query_one = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        query_two = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query_one.receipt_id is not None
        assert query_two.receipt_id is not None

        service_feedback(
            populated_instance,
            receipt_id=query_one.receipt_id,
            action="reject",
            source="agent",
            target=_feedback_target("BP-1001"),
            reason="Mismatch",
            reason_code="fitment_mismatch",
            scope_hints={"category": "brakes", "make": "Honda"},
        )
        service_feedback(
            populated_instance,
            receipt_id=query_two.receipt_id,
            action="reject",
            source="agent",
            target=_feedback_target("BP-1002"),
            reason="Mismatch",
            reason_code="fitment_mismatch",
            scope_hints={"category": "brakes", "make": "Honda"},
        )
        service_outcome(
            populated_instance,
            receipt_id=query_one.receipt_id,
            outcome="incorrect",
            source="agent",
            outcome_code="bad_result",
            scope_hints={"surface": "parts_for_vehicle"},
        )
        service_outcome(
            populated_instance,
            receipt_id=query_one.receipt_id,
            outcome="incorrect",
            source="agent",
            outcome_code="bad_result",
            scope_hints={"surface": "parts_for_vehicle"},
        )

        result = service_lint(populated_instance, min_support=2)

        assert result.has_issues is True
        assert result.summary.feedback_report_count == 1
        assert result.summary.outcome_report_count == 1
        assert len(result.feedback_reports) == 1
        assert result.feedback_reports[0].relationship_type == "fits"
        assert len(result.feedback_reports[0].quality_check_candidates) == 1
        assert len(result.outcome_reports) == 1
        assert result.outcome_reports[0].anchor_type == "receipt"
        assert len(result.outcome_reports[0].provider_fix_candidates) == 1


def _feedback_target(part_id: str) -> RelationshipInstance:
    return RelationshipInstance(
        from_type="Part",
        from_id=part_id,
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-2024-CIVIC-EX",
    )


def _save_workflow_receipt(instance: CruxibleInstance, workflow_name: str) -> str:
    receipt = Receipt(
        query_name=workflow_name,
        parameters={"vehicle_id": "V-2024-CIVIC-EX"},
        nodes=[],
        edges=[],
        operation_type="workflow",
    )
    with instance.write_transaction() as uow:
        uow.receipts.save_receipt(receipt)
    return receipt.receipt_id


def _create_resolution_anchor(instance: CruxibleInstance) -> str:
    workflow_receipt_id = _save_workflow_receipt(instance, "propose_kev_product_links")
    graph = instance.load_graph()
    if graph.get_entity("Vehicle", "V-OUTCOME-1") is None:
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-OUTCOME-1",
                properties={"vehicle_id": "V-OUTCOME-1", "make": "Honda"},
            )
        )
        instance.save_graph(graph)
    propose_result = service_propose_group(
        instance,
        "fits",
        members=[
            CandidateMember(
                from_type="Part",
                from_id="BP-1001",
                to_type="Vehicle",
                to_id="V-OUTCOME-1",
                relationship_type="fits",
            )
        ],
        thesis_text="KEV suggests this part affects the vehicle",
        thesis_facts={"vendor": "Honda"},
        source_workflow_name="propose_kev_product_links",
        source_workflow_receipt_id=workflow_receipt_id,
    )
    assert propose_result.group_id is not None
    resolve_result = service_resolve_group(
        instance,
        propose_result.group_id,
        action="approve",
        rationale="accepted",
        resolved_by="human",
        expected_pending_version=1,
    )
    assert resolve_result.resolution_id is not None
    return resolve_result.resolution_id
