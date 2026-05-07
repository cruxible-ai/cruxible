"""Tests for workflow execution service functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance
from cruxible_core.service import (
    service_apply_workflow,
    service_clone_snapshot,
    service_create_snapshot,
    service_list_snapshots,
    service_lock,
    service_plan,
    service_propose_workflow,
    service_resolve_group,
    service_run,
    service_test,
)
from cruxible_core.workflow import get_legacy_lock_path, get_lock_path
from tests.support import workflow_test_providers


@pytest.fixture
def workflow_instance(tmp_path: Path, workflow_config_yaml: str) -> CruxibleInstance:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(workflow_config_yaml)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")

    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Product",
            entity_id="SKU-123",
            properties={
                "sku": "SKU-123",
                "category": "soda",
                "base_margin": 0.2,
            },
        )
    )
    instance.save_graph(graph)
    return instance


@pytest.fixture
def proposal_workflow_instance(
    tmp_path: Path, proposal_workflow_config_yaml: str
) -> CruxibleInstance:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(proposal_workflow_config_yaml)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")

    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Campaign",
            entity_id="CMP-1",
            properties={"campaign_id": "CMP-1", "region": "north"},
        )
    )
    for sku in ("SKU-123", "SKU-456"):
        graph.add_entity(
            EntityInstance(
                entity_type="Product",
                entity_id=sku,
                properties={"sku": sku, "category": "beverages"},
            )
        )
    instance.save_graph(graph)
    return instance


class TestWorkflowExecutionServices:
    def test_service_lock_writes_lock_and_counts_dependencies(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        result = service_lock(workflow_instance)

        assert Path(result.lock_path).parent == workflow_instance.get_instance_dir()
        assert result.lock_path.endswith("cruxible.lock.yaml")
        assert result.config_digest.startswith("sha256:")
        assert result.providers_locked == 2
        assert result.artifacts_locked == 1
        assert Path(result.lock_path).exists()

    def test_service_plan_returns_compiled_plan(self, workflow_instance: CruxibleInstance) -> None:
        service_lock(workflow_instance)

        result = service_plan(
            workflow_instance,
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert result.plan.workflow == "evaluate_promo"
        assert result.plan.steps[0].kind == "query"
        assert result.plan.steps[1].provider_name == "lift_predictor"

    def test_instance_local_lock_wins_over_legacy_fallback(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        result = service_lock(workflow_instance)
        legacy_path = get_legacy_lock_path(workflow_instance)
        legacy_path.write_text("config_digest: sha256:bad\nartifacts: {}\nproviders: {}\n")

        planned = service_plan(
            workflow_instance,
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert planned.plan.workflow == "evaluate_promo"
        assert Path(result.lock_path) == get_lock_path(workflow_instance)

    def test_service_run_returns_receipt_and_trace_ids(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(workflow_instance)

        result = service_run(
            workflow_instance,
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert result.workflow == "evaluate_promo"
        assert result.output["decision"] == "approve"
        assert result.receipt_id.startswith("RCP-")
        assert result.receipt is not None
        assert result.receipt.workflow_mode == "preview"
        assert result.receipt.committed is False
        assert len(result.query_receipt_ids) == 1
        assert len(result.trace_ids) == 2
        assert all(trace_id.startswith("TRC-") for trace_id in result.trace_ids)

    def test_service_run_previews_canonical_workflow(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(canonical_workflow_instance)

        result = service_run(canonical_workflow_instance, "build_reference", {})

        assert result.mode == "preview"
        assert result.canonical is True
        assert result.apply_digest is not None
        assert result.committed_snapshot_id is None
        assert result.receipt is not None
        assert result.receipt.workflow_mode == "preview"
        assert result.receipt.committed is False
        assert canonical_workflow_instance.load_graph().list_entities("Vendor") == []

    def test_service_apply_workflow_commits_canonical_workflow(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(canonical_workflow_instance)
        preview = service_run(canonical_workflow_instance, "build_reference", {})

        applied = service_apply_workflow(
            canonical_workflow_instance,
            "build_reference",
            {},
            expected_apply_digest=preview.apply_digest or "",
            expected_head_snapshot_id=preview.head_snapshot_id,
        )

        assert applied.mode == "apply"
        assert applied.committed_snapshot_id is not None
        assert applied.receipt is not None
        assert applied.receipt.workflow_mode == "apply"
        assert applied.receipt.committed is True
        assert canonical_workflow_instance.load_graph().has_entity("Vendor", "vendor-acme")

    def test_service_apply_workflow_rejects_digest_mismatch(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(canonical_workflow_instance)
        preview = service_run(canonical_workflow_instance, "build_reference", {})

        with pytest.raises(ConfigError, match="digest mismatch"):
            service_apply_workflow(
                canonical_workflow_instance,
                "build_reference",
                {},
                expected_apply_digest="sha256:bad",
                expected_head_snapshot_id=preview.head_snapshot_id,
            )

    def test_service_apply_workflow_rejects_head_snapshot_mismatch_with_guidance(
        self,
        canonical_workflow_instance: CruxibleInstance,
    ) -> None:
        service_lock(canonical_workflow_instance)
        preview = service_run(canonical_workflow_instance, "build_reference", {})

        with pytest.raises(ConfigError) as exc_info:
            service_apply_workflow(
                canonical_workflow_instance,
                "build_reference",
                {},
                expected_apply_digest=preview.apply_digest or "",
                expected_head_snapshot_id="snap_other",
            )

        message = str(exc_info.value)
        assert "Workflow head snapshot changed between preview and apply." in message
        assert "--apply-digest AND --head-snapshot" in message
        assert "--preview-file <path>" in message

    def test_service_test_supports_expected_error_cases(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.providers[
            "margin_calculator"
        ].ref = "tests.support.workflow_test_providers.broken_provider"
        config.tests[0].expect.output_contains = None
        config.tests[0].expect.receipt_contains_provider = None
        config.tests[0].expect.error_contains = "output failed contract"
        workflow_instance.save_config(config)
        service_lock(workflow_instance)

        result = service_test(workflow_instance)

        assert result.total == 1
        assert result.passed == 1
        assert result.failed == 0
        assert result.cases[0].passed is True
        assert "output failed contract" in (result.cases[0].error or "")

    def test_service_test_rejects_unknown_test_name(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(workflow_instance)

        with pytest.raises(ConfigError, match="Test 'missing' not found in config"):
            service_test(workflow_instance, test_name="missing")

    def test_service_propose_workflow_creates_candidate_group_with_lineage(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(proposal_workflow_instance)

        result = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert result.group_id.startswith("GRP-")
        assert result.group_status == "pending_review"
        assert result.receipt_id.startswith("RCP-")
        assert result.receipt is not None
        assert result.receipt.workflow_mode == "proposal"
        assert result.receipt.committed is True
        assert result.trace_ids

        group_store = proposal_workflow_instance.get_group_store()
        try:
            group = group_store.get_group(result.group_id)
            members = group_store.get_members(result.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.source_workflow_name == "propose_campaign_recommendations"
        assert group.source_workflow_receipt_id == result.receipt_id
        assert group.source_trace_ids == result.trace_ids
        assert group.source_step_ids == ["proposal"]
        assert len(members) == 2
        assert all(member.relationship_type == "recommended_for" for member in members)

    def test_service_propose_workflow_honors_retain_missing_pending_refresh_mode(
        self,
        proposal_workflow_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        for step in config.workflows["propose_campaign_recommendations"].steps:
            if step.propose_relationship_group is not None:
                step.propose_relationship_group.pending_refresh_mode = "retain_missing"
        proposal_workflow_instance.save_config(config)

        responses = iter(
            [
                {
                    "items": [
                        {
                            "product_sku": "SKU-123",
                            "verdict": "match",
                            "reason": "north bestseller",
                        }
                    ]
                },
                {
                    "items": [
                        {
                            "product_sku": "SKU-456",
                            "verdict": "fallback",
                            "reason": "north fallback",
                        }
                    ]
                },
            ]
        )

        def dynamic_campaign_recommendations(_input_payload, _context):
            return next(responses)

        monkeypatch.setattr(
            workflow_test_providers,
            "campaign_recommendations",
            dynamic_campaign_recommendations,
        )
        service_lock(proposal_workflow_instance)

        first = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )
        second = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert second.group_id == first.group_id

        group_store = proposal_workflow_instance.get_group_store()
        try:
            group = group_store.get_group(first.group_id)
            members = group_store.get_members(first.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.pending_version == 2
        assert {(member.from_id, member.to_id) for member in members} == {
            ("CMP-1", "SKU-123"),
            ("CMP-1", "SKU-456"),
        }

    def test_service_run_rejects_proposal_workflow(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(proposal_workflow_instance)

        with pytest.raises(QueryExecutionError, match="use 'cruxible propose --workflow"):
            service_run(
                proposal_workflow_instance,
                "propose_campaign_recommendations",
                {"campaign_id": "CMP-1"},
            )

    def test_service_propose_workflow_requires_proposal_purpose(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.workflows["propose_campaign_recommendations"].purpose = "utility"  # type: ignore[assignment]
        proposal_workflow_instance.save_config(config)

        with pytest.raises(ConfigError, match="must set purpose: proposal"):
            service_propose_workflow(
                proposal_workflow_instance,
                "propose_campaign_recommendations",
                {"campaign_id": "CMP-1"},
            )

    def test_service_propose_workflow_rejects_missing_required_signals(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        for step in config.workflows["propose_campaign_recommendations"].steps:
            if step.propose_relationship_group is not None:
                step.propose_relationship_group.signals_from = []
        proposal_workflow_instance.save_config(config)
        service_lock(proposal_workflow_instance)

        with pytest.raises(
            ConfigError,
            match="missing signal from required signal source 'catalog'",
        ):
            service_propose_workflow(
                proposal_workflow_instance,
                "propose_campaign_recommendations",
                {"campaign_id": "CMP-1"},
            )

    def test_service_propose_workflow_rejects_signal_for_unknown_candidate_pair(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        for step in config.workflows["propose_campaign_recommendations"].steps:
            if step.make_candidates is not None:
                step.make_candidates.to_id = "SKU-123"
        proposal_workflow_instance.save_config(config)
        service_lock(proposal_workflow_instance)

        with pytest.raises(QueryExecutionError, match="unknown candidate pair CMP-1->SKU-456"):
            service_propose_workflow(
                proposal_workflow_instance,
                "propose_campaign_recommendations",
                {"campaign_id": "CMP-1"},
            )

    def test_snapshot_create_list_and_overlay(
        self, proposal_workflow_instance: CruxibleInstance, tmp_path: Path
    ) -> None:
        service_lock(proposal_workflow_instance)
        proposed = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )
        resolved = service_resolve_group(
            proposal_workflow_instance,
            proposed.group_id,
            "approve",
            expected_pending_version=1,
        )
        assert resolved.edges_created == 2

        created = service_create_snapshot(proposal_workflow_instance, label="baseline")
        listed = service_list_snapshots(proposal_workflow_instance)

        assert created.snapshot.snapshot_id.startswith("snap_")
        assert listed.snapshots[0].snapshot_id == created.snapshot.snapshot_id
        assert listed.snapshots[0].label == "baseline"

        overlay_root = tmp_path / "cloned"
        overlay_result = service_clone_snapshot(
            proposal_workflow_instance,
            created.snapshot.snapshot_id,
            overlay_root,
        )

        assert overlay_result.snapshot.snapshot_id == created.snapshot.snapshot_id
        assert overlay_result.instance.get_root_path() == overlay_root
        assert (
            overlay_result.instance.metadata["origin_snapshot_id"] == created.snapshot.snapshot_id
        )
        assert (overlay_root / ".cruxible" / "cruxible.lock.yaml").exists()
        overlay_graph = overlay_result.instance.load_graph()
        assert overlay_graph.edge_count("recommended_for") == 2
