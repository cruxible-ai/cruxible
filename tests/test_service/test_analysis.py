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
    PropertyQualityCheck,
    ProposalPolicySchema,
    SignalPolicySchema,
)
from cruxible_core.errors import ConfigError
from cruxible_core.graph.provenance import SOURCE_REF_ADD_RELATIONSHIP
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import (
    CandidateMember,
    CandidateSignal,
    QuerySourceEvidence,
)
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import (
    RelationshipWriteInput,
    service_add_relationship_inputs,
    service_analyze_feedback,
    service_analyze_outcomes,
    service_evaluate,
    service_feedback,
    service_lint,
    service_outcome,
    service_propose_group,
    service_query,
    service_resolve_group,
    service_state_health,
    service_validate,
)
from cruxible_core.workflow.compiler import resolve_lock_path
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

    def test_passes_filters_to_evaluator(self, populated_instance: CruxibleInstance) -> None:
        config = populated_instance.load_config()
        config.quality_checks.append(
            PropertyQualityCheck(
                name="part_category_non_empty",
                target="entity",
                entity_type="Part",
                property="category",
                rule="non_empty",
                severity="error",
            )
        )
        populated_instance.save_config(config)
        graph = populated_instance.load_graph()
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P-empty", properties={"category": ""})
        )
        populated_instance.save_graph(graph)

        report = service_evaluate(
            populated_instance,
            max_findings=1,
            severity_filter=["error"],
            category_filter=["quality_check_failed"],
        )

        assert len(report.findings) == 1
        assert report.findings[0].severity == "error"
        assert report.findings[0].category == "quality_check_failed"
        assert report.quality_summary["part_category_non_empty"] == 1

    def test_closes_group_store(
        self, populated_instance: CruxibleInstance, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _ClosingGroupStore()
        monkeypatch.setattr(populated_instance, "get_group_store", lambda: store)

        service_evaluate(populated_instance)

        assert store.closed is True

    def test_direct_evidence_backed_governed_edge_is_supported(
        self, populated_instance: CruxibleInstance
    ) -> None:
        config = populated_instance.load_config()
        replaces = config.get_relationship("replaces")
        assert replaces is not None
        replaces.proposal_policy = ProposalPolicySchema(
            signals={
                "catalog": SignalPolicySchema(
                    role="required",
                    always_review_on_unsure=True,
                )
            }
        )
        populated_instance.save_config(config)

        service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="replaces",
                    to_type="Part",
                    to_id="BP-1001",
                    properties={"direction": "upgrade", "confidence": 0.95},
                    evidence_refs=[
                        {
                            "source": "roadmap_doc",
                            "source_record_id": "direct-evidence-section",
                        }
                    ],
                )
            ],
            source="test",
            source_ref="direct_evidence_regression",
        )

        report = service_evaluate(populated_instance)

        governed_findings = [
            finding
            for finding in report.findings
            if finding.category == "governed_support_relationship"
        ]
        assert governed_findings == []


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


class TestStateHealth:
    """service_state_health: deterministic read-only maintenance signals."""

    def test_empty_instance_all_zero(self, initialized_instance: CruxibleInstance) -> None:
        result = service_state_health(initialized_instance)

        # Valid all-zero report, no errors, no head snapshot.
        assert result.captured_at  # ISO8601 string present
        assert result.head_snapshot_id is None

        assert result.groups.total_count == 0
        assert result.groups.pending_review_count == 0
        assert result.groups.oldest_unresolved_age_seconds is None
        assert result.groups.newest_unresolved_age_seconds is None

        assert result.signals.unevidenced_support_by_source == {}

        assert result.provenance.total_edge_count == 0
        assert result.provenance.direct_write_edge_count == 0
        assert result.provenance.group_backed_edge_count == 0
        assert result.provenance.other_source_edge_count == 0

        assert result.freshness.source_artifact_count == 0
        assert result.freshness.oldest_source_artifact_age_seconds is None
        assert result.freshness.provider_trace_count == 0
        assert result.freshness.config_compatible is True
        assert result.freshness.config_warnings == []

        assert result.integrity.orphan_entity_count == 0
        # Empty graph: every configured type is an unused coverage gap.
        assert "Vehicle" in result.integrity.unused_entity_types
        assert "fits" in result.integrity.unused_relationship_types
        assert result.integrity.configuration_locked is False

    def test_integrity_orphans_and_coverage(self, populated_instance: CruxibleInstance) -> None:
        graph = populated_instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id="ORPHAN-1",
                properties={"part_number": "ORPHAN-1", "name": "Loose Part"},
            )
        )
        populated_instance.save_graph(graph)

        result = service_state_health(populated_instance)

        # The lone unconnected part is reported as an orphan.
        assert result.integrity.orphan_entity_count == 1
        # Vehicle/Part/fits/replaces are all present -> not unused.
        assert "Vehicle" not in result.integrity.unused_entity_types
        assert "fits" not in result.integrity.unused_relationship_types

    def test_freshness_config_incompatible(self, populated_instance: CruxibleInstance) -> None:
        # Drop the 'replaces' relationship from config while edges remain in graph.
        config = populated_instance.load_config()
        config.relationships = [rel for rel in config.relationships if rel.name != "replaces"]
        populated_instance.save_config(config)

        result = service_state_health(populated_instance)

        assert result.freshness.config_compatible is False
        assert any("replaces" in warning for warning in result.freshness.config_warnings)

    def test_configuration_locked_fact(self, populated_instance: CruxibleInstance) -> None:
        # Binary deterministic fact: lock file presence flips the flag.
        assert service_state_health(populated_instance).integrity.configuration_locked is False
        resolve_lock_path(populated_instance).write_text("{}")
        assert service_state_health(populated_instance).integrity.configuration_locked is True

    def test_groups_counts_and_age(self, populated_instance: CruxibleInstance) -> None:
        # A resolved group is counted but must NOT contribute to the age span:
        # resolved groups only accumulate age and are not an actionable signal, so
        # the span is scoped to the unresolved (pending_review + applying) backlog.
        _create_resolution_anchor(populated_instance)
        resolved_only = service_state_health(populated_instance)
        assert resolved_only.groups.resolved_count >= 1
        assert resolved_only.groups.oldest_unresolved_age_seconds is None
        assert resolved_only.groups.newest_unresolved_age_seconds is None

        # A pending (unresolved) group DOES contribute a non-negative age.
        graph = populated_instance.load_graph()
        if graph.get_entity("Vehicle", "V-PENDING-1") is None:
            graph.add_entity(
                EntityInstance(
                    entity_type="Vehicle",
                    entity_id="V-PENDING-1",
                    properties={"vehicle_id": "V-PENDING-1", "make": "Honda"},
                )
            )
            populated_instance.save_graph(graph)
        propose_result = service_propose_group(
            populated_instance,
            "fits",
            members=[
                CandidateMember(
                    from_type="Part",
                    from_id="BP-1001",
                    to_type="Vehicle",
                    to_id="V-PENDING-1",
                    relationship_type="fits",
                )
            ],
            thesis_text="pending backlog group",
            thesis_facts={"vendor": "Honda"},
            source_workflow_name="propose_kev_product_links",
            source_workflow_receipt_id=_save_workflow_receipt(
                populated_instance, "propose_kev_product_links"
            ),
        )
        assert propose_result.group_id is not None

        with_pending = service_state_health(populated_instance)
        assert with_pending.groups.pending_review_count >= 1
        assert with_pending.groups.oldest_unresolved_age_seconds is not None
        assert with_pending.groups.oldest_unresolved_age_seconds >= 0
        assert with_pending.groups.newest_unresolved_age_seconds is not None

    def test_signals_count_unevidenced_support_by_source(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        config = populated_instance.load_config()
        fits = config.get_relationship("fits")
        replaces = config.get_relationship("replaces")
        assert fits is not None
        assert replaces is not None
        fits.proposal_policy = ProposalPolicySchema(
            signals={
                "scanner": SignalPolicySchema(
                    role="required",
                    require_evidence_on_support=True,
                ),
                "query_signal": SignalPolicySchema(
                    role="required",
                    require_evidence_on_support=True,
                ),
                "manual_check": SignalPolicySchema(
                    role="advisory",
                    require_evidence_on_support=True,
                ),
                "catalog": SignalPolicySchema(role="advisory"),
            }
        )
        replaces.proposal_policy = ProposalPolicySchema(
            signals={"catalog": SignalPolicySchema(role="required")}
        )
        populated_instance.save_config(config)

        graph = populated_instance.load_graph()
        for vehicle_id in (
            "V-EVIDENCE-1",
            "V-EVIDENCE-2",
            "V-EVIDENCE-RESOLVED",
        ):
            if graph.get_entity("Vehicle", vehicle_id) is None:
                graph.add_entity(
                    EntityInstance(
                        entity_type="Vehicle",
                        entity_id=vehicle_id,
                        properties={"vehicle_id": vehicle_id, "make": "Honda"},
                    )
                )
        populated_instance.save_graph(graph)

        result = service_propose_group(
            populated_instance,
            "fits",
            members=[
                CandidateMember(
                    from_type="Part",
                    from_id="BP-1001",
                    to_type="Vehicle",
                    to_id="V-EVIDENCE-1",
                    relationship_type="fits",
                    signals=[
                        CandidateSignal(signal_source="scanner", signal="support"),
                        CandidateSignal(
                            signal_source="query_signal",
                            signal="support",
                            evidence="query row carried onto the signal",
                        ),
                        CandidateSignal(
                            signal_source="manual_check",
                            signal="support",
                            evidence="reviewed by QA",
                        ),
                        CandidateSignal(signal_source="catalog", signal="support"),
                    ],
                ),
                CandidateMember(
                    from_type="Part",
                    from_id="BP-1002",
                    to_type="Vehicle",
                    to_id="V-EVIDENCE-2",
                    relationship_type="fits",
                    signals=[
                        CandidateSignal(signal_source="scanner", signal="support"),
                        CandidateSignal(signal_source="query_signal", signal="support"),
                    ],
                    source_query_evidence=[
                        QuerySourceEvidence(
                            query_receipt_id="RCP-query000001",
                            row_index=0,
                            source_step="query_signal",
                        )
                    ],
                ),
            ],
            thesis_text="signal evidence backlog",
            thesis_facts={"source": "test"},
        )
        assert result.group_id is not None

        resolved_result = service_propose_group(
            populated_instance,
            "fits",
            members=[
                CandidateMember(
                    from_type="Part",
                    from_id="BP-1001",
                    to_type="Vehicle",
                    to_id="V-EVIDENCE-RESOLVED",
                    relationship_type="fits",
                    signals=[
                        CandidateSignal(signal_source="scanner", signal="support"),
                        CandidateSignal(signal_source="query_signal", signal="support"),
                    ],
                )
            ],
            thesis_text="resolved evidence backlog",
            thesis_facts={"source": "resolved-test"},
        )
        assert resolved_result.group_id is not None
        service_resolve_group(
            populated_instance,
            resolved_result.group_id,
            action="reject",
            rationale="not part of pending health backlog",
            resolved_by="human",
            expected_pending_version=1,
        )

        unflagged_result = service_propose_group(
            populated_instance,
            "replaces",
            members=[
                CandidateMember(
                    from_type="Part",
                    from_id="BP-1001",
                    to_type="Part",
                    to_id="BP-1002",
                    relationship_type="replaces",
                    signals=[CandidateSignal(signal_source="catalog", signal="support")],
                    properties={"direction": "downgrade", "confidence": 0.8},
                )
            ],
            thesis_text="unflagged source backlog",
            thesis_facts={"source": "unflagged-test"},
        )
        assert unflagged_result.group_id is not None

        health = service_state_health(populated_instance)

        assert health.signals.unevidenced_support_by_source == {
            "query_signal": 1,
            "scanner": 2,
        }

    def test_provenance_source_ref_tally(self, populated_instance: CruxibleInstance) -> None:
        # populated_instance starts with 4 fixture edges written with NO
        # provenance source_ref -> they tally as 'other'.
        baseline = service_state_health(populated_instance)
        assert baseline.provenance.other_source_edge_count == baseline.provenance.total_edge_count
        assert baseline.provenance.direct_write_edge_count == 0
        assert baseline.provenance.group_backed_edge_count == 0

        # Add one DIRECT-write edge stamped with the canonical add_relationship ref.
        graph = populated_instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-DIRECT-1",
                properties={"vehicle_id": "V-DIRECT-1", "make": "Honda"},
            )
        )
        populated_instance.save_graph(graph)
        service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1001",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-DIRECT-1",
                    properties={"verified": True},
                )
            ],
            source="test",
            source_ref=SOURCE_REF_ADD_RELATIONSHIP,
        )

        # Add one GROUP-backed edge via propose+resolve (source_ref 'group:<id>').
        _create_resolution_anchor(populated_instance)

        result = service_state_health(populated_instance)
        assert result.provenance.direct_write_edge_count == 1
        assert result.provenance.group_backed_edge_count == 1
        assert result.provenance.other_source_edge_count == baseline.provenance.total_edge_count
        assert result.provenance.total_edge_count == (baseline.provenance.total_edge_count + 2)


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
