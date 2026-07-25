"""Tests for service layer validate and evaluate functions."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
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
from cruxible_core.feedback.store import FeedbackStore
from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.graph.provenance import SOURCE_REF_ADD_RELATIONSHIP
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, mint_claim_id
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
from cruxible_core.service.analysis import _ANALYSIS_PAGE_SIZE
from cruxible_core.temporal import utc_now
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

    def _seed_two_coded_rejections(
        self,
        instance: CruxibleInstance,
        *,
        remediation_hint: str,
        version: int = 1,
    ) -> None:
        config = instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=version,
            reason_codes={
                "legacy_unsupported": FeedbackReasonCodeSchema(
                    description="Legacy environment is unsupported",
                    remediation_hint=remediation_hint,  # type: ignore[arg-type]
                    required_scope_keys=["category", "make"],
                )
            },
            scope_keys={"category": "FROM.category", "make": "TO.make"},
        )
        instance.save_config(config)

        for part_id in ("BP-1001", "BP-1002"):
            query = service_query(
                instance,
                "parts_for_vehicle",
                {"vehicle_id": "V-2024-CIVIC-EX"},
            )
            assert query.receipt_id is not None
            service_feedback(
                instance,
                receipt_id=query.receipt_id,
                action="reject",
                source="agent",
                target=_feedback_target(part_id),
                reason="Legacy unsupported",
                reason_code="legacy_unsupported",
                scope_hints={"category": "brakes", "make": "Honda"},
            )

    def test_profile_drift_is_detected_when_the_version_number_does_not_move(
        self, populated_instance: CruxibleInstance
    ) -> None:
        """Editing the profile body without bumping ``version`` still warns.

        The declared version is hand-maintained, so drift binds to a digest of
        the profile BODY instead. Before this, changing a reason code's
        remediation lane in place reinterpreted every stored row in silence.
        """
        self._seed_two_coded_rejections(
            populated_instance,
            remediation_hint="decision_policy",
            version=1,
        )

        # Same declared version, different body.
        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=1,
            reason_codes={
                "legacy_unsupported": FeedbackReasonCodeSchema(
                    description="Legacy environment is unsupported",
                    remediation_hint="constraint",
                    required_scope_keys=["category", "make"],
                )
            },
            scope_keys={"category": "FROM.category", "make": "TO.make"},
        )
        populated_instance.save_config(config)

        result = service_analyze_feedback(
            populated_instance,
            "fits",
            min_support=2,
            decision_surface_type="query",
            decision_surface_name="parts_for_vehicle",
        )

        assert any("different profile body" in warning for warning in result.warnings)
        # The stored hint still governs; the edit does not retro-reinterpret.
        assert len(result.decision_policy_suggestions) == 1
        assert result.constraint_suggestions == []

    def test_no_drift_warning_when_only_the_declared_version_moves(
        self, populated_instance: CruxibleInstance
    ) -> None:
        """Bumping ``version`` with an unchanged body is not drift."""
        self._seed_two_coded_rejections(
            populated_instance,
            remediation_hint="decision_policy",
            version=1,
        )

        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=7,
            reason_codes={
                "legacy_unsupported": FeedbackReasonCodeSchema(
                    description="Legacy environment is unsupported",
                    remediation_hint="decision_policy",
                    required_scope_keys=["category", "make"],
                )
            },
            scope_keys={"category": "FROM.category", "make": "TO.make"},
        )
        populated_instance.save_config(config)

        result = service_analyze_feedback(
            populated_instance,
            "fits",
            min_support=2,
            decision_surface_type="query",
            decision_surface_name="parts_for_vehicle",
        )

        assert not any("different profile body" in warning for warning in result.warnings)

    @staticmethod
    def _rewrite_stored_feedback_as_legacy(
        instance: CruxibleInstance,
        *,
        stored_version: int | None,
    ) -> None:
        """Rewrite stored rows the way a pre-digest-column store held them.

        Legacy rows carry a declared ``feedback_profile_version`` and no digest,
        because the digest column did not exist when they were written.
        """
        with instance.write_transaction() as uow:
            store = uow.feedback
            for row in store.list_feedback(relationship_type="fits", limit=100):
                store.save_feedback(
                    row.model_copy(
                        update={
                            "feedback_profile_digest": None,
                            "feedback_profile_version": stored_version,
                        }
                    )
                )

    def test_legacy_row_at_a_different_version_is_proven_drift(
        self, populated_instance: CruxibleInstance
    ) -> None:
        """A digestless row whose declared version moved is drift, not unknown.

        The body is unrecoverable, but the version mismatch alone proves the
        profile changed under the row. Treating that as "no digest, no drift"
        reinterpreted the row in the same silence the digest was added to end.
        """
        self._seed_two_coded_rejections(
            populated_instance,
            remediation_hint="decision_policy",
            version=1,
        )
        self._rewrite_stored_feedback_as_legacy(populated_instance, stored_version=1)

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
            scope_keys={"category": "FROM.category", "make": "TO.make"},
        )
        populated_instance.save_config(config)

        result = service_analyze_feedback(
            populated_instance,
            "fits",
            min_support=2,
            decision_surface_type="query",
            decision_surface_name="parts_for_vehicle",
        )

        drift_warnings = [w for w in result.warnings if "different profile body" in w]
        assert len(drift_warnings) == 1
        # The evidence names the versions, not a "digest None vs current ..." lie.
        assert "version 1 vs current 2" in drift_warnings[0]
        assert "None" not in drift_warnings[0]
        assert "using stored remediation hints" in drift_warnings[0]
        # Exactly as for a digest mismatch: the stored hint still governs.
        assert len(result.decision_policy_suggestions) == 1
        assert result.constraint_suggestions == []

    def test_legacy_row_at_the_same_version_stays_silent(
        self, populated_instance: CruxibleInstance
    ) -> None:
        """A digestless row at the current version leaves drift honestly unknown."""
        self._seed_two_coded_rejections(
            populated_instance,
            remediation_hint="decision_policy",
            version=1,
        )
        self._rewrite_stored_feedback_as_legacy(populated_instance, stored_version=1)

        # Body edited in place, declared version unchanged: with no stored digest
        # there is no evidence either way, so the analysis must not guess.
        config = populated_instance.load_config()
        config.feedback_profiles["fits"] = FeedbackProfileSchema(
            version=1,
            reason_codes={
                "legacy_unsupported": FeedbackReasonCodeSchema(
                    description="Legacy environment is unsupported",
                    remediation_hint="constraint",
                    required_scope_keys=["category", "make"],
                )
            },
            scope_keys={"category": "FROM.category", "make": "TO.make"},
        )
        populated_instance.save_config(config)

        result = service_analyze_feedback(
            populated_instance,
            "fits",
            min_support=2,
            decision_surface_type="query",
            decision_surface_name="parts_for_vehicle",
        )

        assert not any("different profile body" in warning for warning in result.warnings)

    def test_analysis_reports_population_and_flags_a_truncated_window(
        self, populated_instance: CruxibleInstance
    ) -> None:
        """A windowed read must not present its tallies as the population's."""
        self._seed_two_coded_rejections(populated_instance, remediation_hint="decision_policy")

        full = service_analyze_feedback(populated_instance, "fits", min_support=2)
        assert full.feedback_count == 2
        assert full.feedback_population_count == 2
        assert full.truncated is False
        assert not any("most recent feedback rows" in warning for warning in full.warnings)

        sampled = service_analyze_feedback(populated_instance, "fits", min_support=2, limit=1)
        assert sampled.feedback_count == 1
        # The population is still reported honestly alongside the sample.
        assert sampled.feedback_population_count == 2
        assert sampled.truncated is True
        assert any(
            "Analyzed the 1 most recent feedback rows of 2 matching" in warning
            for warning in sampled.warnings
        )

    @pytest.mark.parametrize("count", [499, 500, 501])
    def test_paginated_read_loses_no_row_at_the_page_boundary(
        self, populated_instance: CruxibleInstance, count: int
    ) -> None:
        """The 500-row read page must not drop or repeat a row at its seam.

        These drive the real ``_ANALYSIS_PAGE_SIZE`` rather than patching it:
        the rows are written straight to the store, so seeding 501 of them costs
        501 inserts instead of 501 queries plus receipts.
        """
        assert count in (_ANALYSIS_PAGE_SIZE - 1, _ANALYSIS_PAGE_SIZE, _ANALYSIS_PAGE_SIZE + 1)
        _seed_bulk_feedback(populated_instance, count=count)

        result = service_analyze_feedback(populated_instance, "fits", limit=count + 100)

        assert result.feedback_count == count
        assert result.feedback_population_count == count
        assert result.truncated is False
        # One distinct reason_code per row: a dropped row loses a key, and a
        # re-read page pushes some key's tally to 2.
        assert len(result.reason_code_counts) == count
        assert set(result.reason_code_counts.values()) == {1}

    def test_population_count_applies_the_same_filters_as_the_listed_rows(
        self, populated_instance: CruxibleInstance
    ) -> None:
        """The count query and the list query must share one filter set.

        A count that ignores a filter the list applies reports a population the
        sample was never drawn from, and flags truncation that never happened.
        """
        _seed_bulk_feedback(
            populated_instance,
            count=6,
            surface_name="parts_for_vehicle",
            code_prefix="matching",
        )
        _seed_bulk_feedback(
            populated_instance,
            count=4,
            surface_name="other_query",
            code_prefix="excluded",
        )

        unfiltered = service_analyze_feedback(populated_instance, "fits", limit=100)
        assert unfiltered.feedback_count == 10
        assert unfiltered.feedback_population_count == 10

        filtered = service_analyze_feedback(
            populated_instance,
            "fits",
            limit=100,
            decision_surface_type="query",
            decision_surface_name="parts_for_vehicle",
        )
        assert filtered.feedback_count == 6
        assert filtered.feedback_population_count == 6
        assert filtered.truncated is False
        assert all(code.startswith("matching_") for code in filtered.reason_code_counts)

        sampled = service_analyze_feedback(
            populated_instance,
            "fits",
            limit=2,
            decision_surface_type="query",
            decision_surface_name="parts_for_vehicle",
        )
        assert sampled.feedback_count == 2
        # The FILTERED population, not the whole table.
        assert sampled.feedback_population_count == 6
        assert sampled.truncated is True

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
        assert result.outcome_population_count == 2
        assert result.truncated is False
        assert result.outcome_code_counts["bad_result"] == 2
        assert len(result.provider_fix_candidates) == 1
        assert result.provider_fix_candidates[0].surface_name == "parts_for_vehicle"
        assert len(result.workflow_debug_packages) == 1

        # A narrowed window reports the population it did NOT cover.
        sampled = service_analyze_outcomes(
            populated_instance,
            anchor_type="receipt",
            query_name="parts_for_vehicle",
            min_support=2,
            limit=1,
        )
        assert sampled.outcome_count == 1
        assert sampled.outcome_population_count == 2
        assert sampled.truncated is True
        assert any(
            "Analyzed the 1 most recent outcome rows of 2 matching" in warning
            for warning in sampled.warnings
        )

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

    def test_paginated_read_loses_no_row_at_the_page_boundary(
        self, populated_instance: CruxibleInstance
    ) -> None:
        """One row past the 500-row page: the seam must not drop or repeat."""
        count = _ANALYSIS_PAGE_SIZE + 1
        _seed_bulk_outcomes(populated_instance, count=count)

        result = service_analyze_outcomes(
            populated_instance,
            anchor_type="receipt",
            limit=count + 100,
        )

        assert result.outcome_count == count
        assert result.outcome_population_count == count
        assert result.truncated is False
        assert len(result.outcome_code_counts) == count
        assert set(result.outcome_code_counts.values()) == {1}

    def test_population_count_applies_the_same_filters_as_the_listed_rows(
        self, populated_instance: CruxibleInstance
    ) -> None:
        """The outcome count query must carry the list query's filters."""
        _seed_bulk_outcomes(
            populated_instance,
            count=6,
            surface_name="parts_for_vehicle",
            code_prefix="matching",
        )
        _seed_bulk_outcomes(
            populated_instance,
            count=4,
            surface_name="other_query",
            code_prefix="excluded",
        )

        unfiltered = service_analyze_outcomes(
            populated_instance,
            anchor_type="receipt",
            limit=100,
        )
        assert unfiltered.outcome_count == 10
        assert unfiltered.outcome_population_count == 10

        filtered = service_analyze_outcomes(
            populated_instance,
            anchor_type="receipt",
            query_name="parts_for_vehicle",
            limit=100,
        )
        assert filtered.outcome_count == 6
        assert filtered.outcome_population_count == 6
        assert filtered.truncated is False
        assert all(code.startswith("matching_") for code in filtered.outcome_code_counts)

        sampled = service_analyze_outcomes(
            populated_instance,
            anchor_type="receipt",
            query_name="parts_for_vehicle",
            limit=2,
        )
        assert sampled.outcome_count == 2
        # The FILTERED population, not the whole table.
        assert sampled.outcome_population_count == 6
        assert sampled.truncated is True


class TestProfileDigestMigration:
    """The ALTER TABLE migration must not disturb a populated legacy store.

    Lives beside the analysis tests because the columns it adds are what the
    drift checks above read; a migration that dropped or rewrote legacy rows
    would change those verdicts silently.
    """

    _OLD_SCHEMA = """\
CREATE TABLE feedback (
    feedback_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_json TEXT NOT NULL,
    target_relationship TEXT NOT NULL DEFAULT '',
    target_from_type TEXT NOT NULL DEFAULT '',
    target_from_id TEXT NOT NULL DEFAULT '',
    target_to_type TEXT NOT NULL DEFAULT '',
    target_to_id TEXT NOT NULL DEFAULT '',
    target_edge_key INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    reason_code TEXT,
    reason_remediation_hint TEXT,
    scope_hints TEXT NOT NULL DEFAULT '{}',
    feedback_profile_key TEXT,
    feedback_profile_version INTEGER,
    decision_context TEXT NOT NULL DEFAULT '{}',
    context_snapshot TEXT NOT NULL DEFAULT '{}',
    decision_surface_type TEXT,
    decision_surface_name TEXT,
    source TEXT NOT NULL DEFAULT 'human',
    model_id TEXT,
    corrections TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE feedback_entities (
    feedback_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY (feedback_id, entity_id)
);

CREATE TABLE outcomes (
    outcome_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    anchor_type TEXT NOT NULL DEFAULT 'receipt',
    anchor_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    outcome_code TEXT,
    outcome_remediation_hint TEXT,
    scope_hints TEXT NOT NULL DEFAULT '{}',
    outcome_profile_key TEXT,
    outcome_profile_version INTEGER,
    decision_context TEXT NOT NULL DEFAULT '{}',
    lineage_snapshot TEXT NOT NULL DEFAULT '{}',
    relationship_type TEXT,
    decision_surface_type TEXT,
    decision_surface_name TEXT,
    source TEXT NOT NULL DEFAULT 'human',
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

    def _build_legacy_db(self, path: Path) -> None:
        """Write a populated store at the schema that predates the digest columns."""
        conn = sqlite3.connect(path)
        try:
            conn.executescript(self._OLD_SCHEMA)
            conn.execute(
                "INSERT INTO feedback (feedback_id, receipt_id, action, target_json, "
                "target_relationship, target_from_type, target_from_id, target_to_type, "
                "target_to_id, reason, reason_code, reason_remediation_hint, scope_hints, "
                "feedback_profile_key, feedback_profile_version, decision_context, "
                "context_snapshot, decision_surface_type, decision_surface_name, source, "
                "corrections, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "FB-legacy-1",
                    "RCPT-legacy-1",
                    "reject",
                    _feedback_target("BP-1001").model_dump_json(),
                    "fits",
                    "Part",
                    "BP-1001",
                    "Vehicle",
                    "V-2024-CIVIC-EX",
                    "Legacy unsupported",
                    "legacy_unsupported",
                    "decision_policy",
                    '{"category": "brakes"}',
                    "fits",
                    3,
                    '{"surface_type": "query", "surface_name": "parts_for_vehicle"}',
                    "{}",
                    "query",
                    "parts_for_vehicle",
                    "agent",
                    "{}",
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO outcomes (outcome_id, receipt_id, anchor_type, anchor_id, outcome, "
                "outcome_code, outcome_remediation_hint, scope_hints, outcome_profile_key, "
                "outcome_profile_version, decision_context, lineage_snapshot, relationship_type, "
                "decision_surface_type, decision_surface_name, source, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "OUT-legacy-1",
                    "RCPT-legacy-1",
                    "receipt",
                    "RCPT-legacy-1",
                    "incorrect",
                    "bad_result",
                    "provider_fix",
                    '{"surface": "parts_for_vehicle"}',
                    "query_quality",
                    5,
                    '{"surface_type": "query", "surface_name": "parts_for_vehicle"}',
                    "{}",
                    "fits",
                    "query",
                    "parts_for_vehicle",
                    "human",
                    "{}",
                    "2026-01-02T00:00:00Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_migration_preserves_legacy_rows_and_leaves_digests_null(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy-state.db"
        self._build_legacy_db(db_path)

        store = FeedbackStore(db_path)
        try:
            feedback = store.get_feedback("FB-legacy-1")
            outcome = store.get_outcome("OUT-legacy-1")
        finally:
            store.close()

        assert feedback is not None
        assert feedback.receipt_id == "RCPT-legacy-1"
        assert feedback.action == "reject"
        assert feedback.reason == "Legacy unsupported"
        assert feedback.reason_code == "legacy_unsupported"
        assert feedback.reason_remediation_hint == "decision_policy"
        assert feedback.scope_hints == {"category": "brakes"}
        assert feedback.feedback_profile_key == "fits"
        assert feedback.feedback_profile_version == 3
        assert feedback.source == "agent"
        assert feedback.target.from_id == "BP-1001"
        assert feedback.decision_context == {
            "surface_type": "query",
            "surface_name": "parts_for_vehicle",
        }
        # The added column is NULL on rows written before it existed: the body
        # they were coded under is unrecoverable, not "the current one".
        assert feedback.feedback_profile_digest is None

        assert outcome is not None
        assert outcome.receipt_id == "RCPT-legacy-1"
        assert outcome.anchor_type == "receipt"
        assert outcome.anchor_id == "RCPT-legacy-1"
        assert outcome.outcome == "incorrect"
        assert outcome.outcome_code == "bad_result"
        assert outcome.outcome_remediation_hint == "provider_fix"
        assert outcome.scope_hints == {"surface": "parts_for_vehicle"}
        assert outcome.outcome_profile_key == "query_quality"
        assert outcome.outcome_profile_version == 5
        assert outcome.relationship_type == "fits"
        assert outcome.source == "human"
        assert outcome.outcome_profile_digest is None

    def test_migration_keeps_every_legacy_row(self, tmp_path: Path) -> None:
        """The feedback table is rebuilt to relax receipt_id; nothing may be lost."""
        db_path = tmp_path / "legacy-state.db"
        self._build_legacy_db(db_path)

        store = FeedbackStore(db_path)
        try:
            assert store.count_feedback() == 1
            assert store.count_outcomes() == 1
            assert [row.feedback_id for row in store.list_feedback()] == ["FB-legacy-1"]
            assert [row.outcome_id for row in store.list_outcomes()] == ["OUT-legacy-1"]
        finally:
            store.close()


class TestLint:
    def test_clean_instance_returns_no_issues(self, populated_instance: CruxibleInstance) -> None:
        graph = populated_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                claim_id=mint_claim_id(),
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


def _seed_bulk_feedback(
    instance: CruxibleInstance,
    *,
    count: int,
    surface_name: str = "parts_for_vehicle",
    code_prefix: str = "bulk",
) -> None:
    """Write ``count`` feedback rows straight to the store, one code each.

    Direct store writes keep page-boundary coverage cheap enough to drive the
    real page size: 500-plus rows through ``service_feedback`` would mean as
    many queries and receipts.
    """
    base = utc_now()
    records = [
        FeedbackRecord(
            # 'flag' keeps these out of the reject-only grouping path, so the
            # rows are pure sampling fixtures with no suggestion side effects.
            action="flag",
            target=_feedback_target("BP-1001"),
            reason="bulk sampling fixture",
            reason_code=f"{code_prefix}_{index:04d}",
            source="agent",
            decision_context={"surface_type": "query", "surface_name": surface_name},
            created_at=base + timedelta(seconds=index),
        )
        for index in range(count)
    ]
    with instance.write_transaction() as uow:
        uow.feedback.save_feedback_batch(records)


def _seed_bulk_outcomes(
    instance: CruxibleInstance,
    *,
    count: int,
    surface_name: str = "parts_for_vehicle",
    code_prefix: str = "bulk",
) -> None:
    """Write ``count`` receipt-anchored outcome rows straight to the store."""
    base = utc_now()
    records = [
        OutcomeRecord(
            receipt_id=f"RCPT-{code_prefix}-{index:04d}",
            anchor_type="receipt",
            outcome="incorrect",
            outcome_code=f"{code_prefix}_{index:04d}",
            relationship_type="fits",
            decision_context={"surface_type": "query", "surface_name": surface_name},
            created_at=base + timedelta(seconds=index),
        )
        for index in range(count)
    ]
    with instance.write_transaction() as uow:
        for record in records:
            uow.feedback.save_outcome(record)


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
