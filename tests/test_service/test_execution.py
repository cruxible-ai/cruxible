"""Tests for workflow execution service functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.service import (
    service_apply_workflow,
    service_clone_snapshot,
    service_create_snapshot,
    service_find_apply_preview,
    service_list_snapshots,
    service_lock,
    service_plan,
    service_propose_group,
    service_propose_workflow,
    service_resolve_group,
    service_run,
    service_test,
)
from cruxible_core.workflow import get_legacy_lock_path, get_lock_path
from tests.support import workflow_test_providers

QUERY_EVIDENCE_PROPOSAL_CONFIG_YAML = """\
version: "1.0"
name: query_evidence_proposals
kind: world_model

entity_types:
  Campaign:
    properties:
      campaign_id:
        type: string
        primary_key: true
  Product:
    properties:
      sku:
        type: string
        primary_key: true

relationships:
  - name: candidate_product
    from: Campaign
    to: Product
    properties:
      reason:
        type: string
  - name: recommended_for
    from: Campaign
    to: Product
    properties:
      reason:
        type: string
        optional: true

named_queries:
  candidate_product_relationships:
    mode: traversal
    entry_point: Campaign
    returns: candidate_product
    result_shape: relationship
    traversal:
      - as: candidate
        relationship: candidate_product
        direction: outgoing
  candidate_product_paths:
    mode: traversal
    entry_point: Campaign
    returns: Product
    result_shape: path
    traversal:
      - as: candidate
        relationship: candidate_product
        direction: outgoing
  all_products:
    mode: collection
    returns: Product
    result_shape: entity

contracts:
  CampaignInput:
    fields:
      campaign_id:
        type: string

workflows:
  propose_from_relationship_query:
    type: proposal
    contract_in: CampaignInput
    steps:
      - id: candidates_query
        query: candidate_product_relationships
        params:
          campaign_id: $input.campaign_id
        as: candidates_query
      - id: candidates
        make_candidates:
          relationship_type: recommended_for
          items: $steps.candidates_query.results
          from_type: Campaign
          from_id: $item.from_id
          to_type: Product
          to_id: $item.to_id
          properties:
            reason: $item.properties.reason
          evidence:
            refs:
              - source: candidate_query
                source_record_id: $item.to_id
                detail: $item.properties.reason
            rationale: $item.properties.reason
        as: candidates
      - id: proposal
        propose_relationship_group:
          relationship_type: recommended_for
          candidates_from: candidates
          signals_from: []
          thesis_text: Recommend query-derived products
        as: proposal
    returns: proposal
  propose_from_path_query:
    type: proposal
    contract_in: CampaignInput
    steps:
      - id: candidates_query
        query: candidate_product_paths
        params:
          campaign_id: $input.campaign_id
        as: candidates_query
      - id: candidates
        make_candidates:
          relationship_type: recommended_for
          items: $steps.candidates_query.results
          from_type: Campaign
          from_id: $item.path[0].from_id
          to_type: Product
          to_id: $item.path[0].to_id
          properties:
            reason: $item.path[0].properties.reason
        as: candidates
      - id: proposal
        propose_relationship_group:
          relationship_type: recommended_for
          candidates_from: candidates
          signals_from: []
          thesis_text: Recommend query-derived products
        as: proposal
    returns: proposal
  propose_from_filtered_relationship_query:
    type: proposal
    contract_in: CampaignInput
    steps:
      - id: candidates_query
        query: candidate_product_relationships
        params:
          campaign_id: $input.campaign_id
        as: candidates_query
      - id: filtered
        filter_items:
          items: $steps.candidates_query.results
          comparisons:
            - left: $item.to_id
              op: eq
              right: SKU-456
        as: filtered
      - id: candidates
        make_candidates:
          relationship_type: recommended_for
          items: $steps.filtered.items
          from_type: Campaign
          from_id: $item.from_id
          to_type: Product
          to_id: $item.to_id
          properties:
            reason: $item.properties.reason
          evidence:
            refs:
              - source: candidate_query
                source_record_id: $item.to_id
                detail: $item.properties.reason
            rationale: $item.properties.reason
        as: candidates
      - id: proposal
        propose_relationship_group:
          relationship_type: recommended_for
          candidates_from: candidates
          signals_from: []
          thesis_text: Recommend filtered query-derived products
        as: proposal
    returns: proposal
  propose_with_query_signals:
    type: proposal
    contract_in: CampaignInput
    steps:
      - id: candidates_query
        query: candidate_product_relationships
        params:
          campaign_id: $input.campaign_id
        as: candidates_query
      - id: candidates
        make_candidates:
          relationship_type: recommended_for
          items: $steps.candidates_query.results
          from_type: Campaign
          from_id: $item.from_id
          to_type: Product
          to_id: $item.to_id
          properties:
            reason: $item.properties.reason
          evidence:
            refs:
              - source: candidate_query
                source_record_id: $item.to_id
                detail: $item.properties.reason
            rationale: $item.properties.reason
        as: candidates
      - id: signal_query
        query: candidate_product_paths
        params:
          campaign_id: $input.campaign_id
        as: signal_query
      - id: query_signals
        map_signals:
          signal_source: query_signal
          items: $steps.signal_query.results
          from_id: $item.path[0].from_id
          to_id: $item.path[0].to_id
          evidence: $item.path[0].properties.reason
          evidence_refs:
            - source: signal_query
              source_record_id: $item.path[0].to_id
              detail: $item.path[0].properties.reason
          enum:
            path: path[0].properties.reason
            map:
              query evidence: support
        as: query_signals
      - id: proposal
        propose_relationship_group:
          relationship_type: recommended_for
          candidates_from: candidates
          signals_from:
            - query_signals
          thesis_text: Recommend query-derived products with query signal
        as: proposal
    returns: proposal
  propose_from_joined_query_rows:
    type: proposal
    contract_in: CampaignInput
    steps:
      - id: candidates_query
        query: candidate_product_relationships
        params:
          campaign_id: $input.campaign_id
        as: candidates_query
      - id: product_query
        query: all_products
        as: product_query
      - id: joined
        join_items:
          left_items: $steps.candidates_query.results
          right_items: $steps.product_query.results
          left_key: $item.to_id
          right_key: $item.entity_id
          fields:
            relationship: $item.left
            product: $item.right
            reason: $item.left.properties.reason
        as: joined
      - id: candidates
        make_candidates:
          relationship_type: recommended_for
          items: $steps.joined.items
          from_type: Campaign
          from_id: $item.relationship.from_id
          to_type: Product
          to_id: $item.product.entity_id
          properties:
            reason: $item.reason
        as: candidates
      - id: proposal
        propose_relationship_group:
          relationship_type: recommended_for
          candidates_from: candidates
          signals_from: []
          thesis_text: Recommend joined query-derived products
        as: proposal
    returns: proposal
  propose_from_shaped_joined_query_rows:
    type: proposal
    contract_in: CampaignInput
    steps:
      - id: candidates_query
        query: candidate_product_relationships
        params:
          campaign_id: $input.campaign_id
        as: candidates_query
      - id: product_query
        query: all_products
        as: product_query
      - id: joined
        join_items:
          left_items: $steps.candidates_query.results
          right_items: $steps.product_query.results
          left_key: $item.to_id
          right_key: $item.entity_id
          fields:
            relationship: $item.left
            product: $item.right
            reason: $item.left.properties.reason
        as: joined
      - id: shaped
        shape_items:
          items: $steps.joined.items
          fields:
            from_id: $item.relationship.from_id
            to_id: $item.product.entity_id
            reason: $item.reason
        as: shaped
      - id: candidates
        make_candidates:
          relationship_type: recommended_for
          items: $steps.shaped.items
          from_type: Campaign
          from_id: $item.from_id
          to_type: Product
          to_id: $item.to_id
          properties:
            reason: $item.reason
        as: candidates
      - id: proposal
        propose_relationship_group:
          relationship_type: recommended_for
          candidates_from: candidates
          signals_from: []
          thesis_text: Recommend shaped joined query-derived products
        as: proposal
    returns: proposal
"""


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


@pytest.fixture
def query_evidence_proposal_instance(tmp_path: Path) -> CruxibleInstance:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(QUERY_EVIDENCE_PROPOSAL_CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")

    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Campaign",
            entity_id="CMP-1",
            properties={"campaign_id": "CMP-1"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Product",
            entity_id="SKU-123",
            properties={"sku": "SKU-123"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="candidate_product",
            from_type="Campaign",
            from_id="CMP-1",
            to_type="Product",
            to_id="SKU-123",
            properties={"reason": "query evidence"},
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
        assert result.mode == "run"
        assert result.workflow_type == "utility"
        assert result.output["decision"] == "approve"
        assert result.receipt_id.startswith("RCP-")
        assert result.receipt is not None
        assert result.receipt.workflow_mode == "run"
        assert result.receipt.committed is False
        assert len(result.query_receipt_ids) == 1
        assert len(result.trace_ids) == 2
        assert all(trace_id.startswith("TRC-") for trace_id in result.trace_ids)
        assert result.read_metadata["query_receipt_ids"] == result.query_receipt_ids
        assert result.read_metadata["any_read_truncated"] is False
        assert result.read_metadata["any_query_truncated"] is False
        assert [step["step_id"] for step in result.read_metadata["read_steps"]] == [
            "context"
        ]

    def test_service_run_previews_canonical_workflow(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(canonical_workflow_instance)

        result = service_run(canonical_workflow_instance, "build_reference", {})

        assert result.mode == "preview"
        assert result.workflow_type == "canonical"
        assert result.canonical is True
        assert result.apply_digest is not None
        assert result.committed_snapshot_id is None
        assert result.receipt is not None
        assert result.receipt.workflow_mode == "preview"
        assert result.receipt.committed is False
        assert canonical_workflow_instance.load_graph().list_entities("Vendor") == []

    def test_service_find_apply_preview_returns_latest_preview(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(canonical_workflow_instance)
        preview = service_run(canonical_workflow_instance, "build_reference", {})

        reference = service_find_apply_preview(canonical_workflow_instance, "build_reference")

        assert reference.workflow == "build_reference"
        assert reference.input_payload == {}
        assert reference.apply_digest == preview.apply_digest
        assert reference.head_snapshot_id == preview.head_snapshot_id
        assert reference.receipt_id == preview.receipt_id
        assert reference.apply_previews

    def test_service_find_apply_preview_rejects_missing_preview(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(canonical_workflow_instance)

        with pytest.raises(ConfigError, match="No stored canonical preview"):
            service_find_apply_preview(canonical_workflow_instance, "build_reference")

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
        assert applied.workflow_type == "canonical"
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
        assert "apply digest and head snapshot id" in message

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

    def test_service_test_rejects_unknown_workflow_name(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.tests[0].workflow = "missing_workflow"
        workflow_instance.save_config(config)

        with pytest.raises(
            ConfigError,
            match="Test 'promo_margin_smoke' references unknown workflow 'missing_workflow'",
        ):
            service_test(workflow_instance)

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
        assert result.mode == "proposal"
        assert result.workflow_type == "proposal"
        assert result.canonical is False
        assert result.receipt_id.startswith("RCP-")
        assert result.receipt is not None
        assert result.receipt.workflow_mode == "proposal"
        assert result.receipt.committed is True
        assert result.trace_ids
        assert result.read_metadata["query_receipt_ids"] == result.query_receipt_ids
        assert result.receipt.nodes[0].detail["read_metadata"] == result.read_metadata

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
        assert group.source_step_ids == ["proposal", "catalog_signals"]
        assert group.thesis_facts == result.output["thesis_facts"]
        assert group.thesis_facts["origin"]["proposal_logic_digest"].startswith(
            "sha256:"
        )
        expected_thesis_facts = {
            "origin": {
                "kind": "workflow",
                "evidence_mode": "workflow_generated",
                "workflow_name": "propose_campaign_recommendations",
                "step_id": "proposal",
                "proposal_logic_digest": group.thesis_facts["origin"][
                    "proposal_logic_digest"
                ],
            },
            "relationship": {
                "type": "recommended_for",
                "from_type": "Campaign",
                "to_type": "Product",
            },
            "candidates": {"from_alias": "candidates"},
            "signals": {
                "used": ["catalog"],
                "required": ["catalog"],
                "blocking": [],
            },
            "policy": {
                "auto_resolve_when": "all_support",
                "proposal_identity": "thesis_signature",
            },
        }
        assert group.thesis_facts == expected_thesis_facts
        receipt_store = proposal_workflow_instance.get_receipt_store()
        try:
            saved_receipt = receipt_store.get_receipt(result.receipt_id)
        finally:
            receipt_store.close()
        assert saved_receipt is not None
        assert saved_receipt.results[0]["output"]["thesis_facts"] == group.thesis_facts
        assert len(members) == 2
        assert all(member.relationship_type == "recommended_for" for member in members)
        assert members[0].signals[0].basis is not None
        assert members[0].signals[0].basis.model_dump(mode="json") == {
            "mode": "enum",
            "path": "verdict",
            "value": "match",
            "matched": "match",
        }

    def test_direct_proposal_does_not_collide_with_workflow_signature(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(proposal_workflow_instance)

        workflow_result = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )
        direct_result = service_propose_group(
            proposal_workflow_instance,
            "recommended_for",
            [
                CandidateMember(
                    from_type="Campaign",
                    from_id="CMP-1",
                    to_type="Product",
                    to_id="SKU-123",
                    relationship_type="recommended_for",
                    signals=[
                        CandidateSignal(
                            signal_source="catalog",
                            signal="support",
                            evidence="agent supplied",
                        )
                    ],
                )
            ],
            thesis_text="Agent direct recommendation",
            thesis_facts={"origin": {"kind": "workflow"}},
        )

        assert workflow_result.group_id is not None
        assert direct_result.group_id is not None
        assert direct_result.group_id != workflow_result.group_id
        group_store = proposal_workflow_instance.get_group_store()
        try:
            direct_group = group_store.get_group(direct_result.group_id)
            workflow_group = group_store.get_group(workflow_result.group_id)
        finally:
            group_store.close()
        assert direct_group is not None
        assert workflow_group is not None
        assert direct_group.signature != workflow_group.signature
        assert direct_group.thesis_facts["origin"]["kind"] == "agent"
        assert direct_group.thesis_facts["agent_scope"] == {"origin": {"kind": "workflow"}}
        assert workflow_group.thesis_facts["origin"]["kind"] == "workflow"

    def test_workflow_signal_source_change_changes_proposal_signature(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(proposal_workflow_instance)
        first = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        config = proposal_workflow_instance.load_config()
        relationship = next(
            rel for rel in config.relationships if rel.name == "recommended_for"
        )
        assert relationship.proposal_policy is not None
        relationship.proposal_policy.signals["catalog_v2"] = (
            relationship.proposal_policy.signals.pop("catalog")
        )
        for step in config.workflows["propose_campaign_recommendations"].steps:
            if step.map_signals is not None:
                step.map_signals.signal_source = "catalog_v2"
        proposal_workflow_instance.save_config(config)
        service_lock(proposal_workflow_instance)

        second = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert first.group_id is not None
        assert second.group_id is not None
        group_store = proposal_workflow_instance.get_group_store()
        try:
            first_group = group_store.get_group(first.group_id)
            second_group = group_store.get_group(second.group_id)
        finally:
            group_store.close()
        assert first_group is not None
        assert second_group is not None
        assert first_group.signature != second_group.signature
        assert first_group.thesis_facts["signals"]["used"] == ["catalog"]
        assert second_group.thesis_facts["signals"]["used"] == ["catalog_v2"]

    def test_workflow_logic_change_changes_signature_without_signal_name_change(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(proposal_workflow_instance)
        first = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        config = proposal_workflow_instance.load_config()
        for step in config.workflows["propose_campaign_recommendations"].steps:
            if step.map_signals is not None:
                step.map_signals.enum.map["fallback"] = "support"
        proposal_workflow_instance.save_config(config)
        service_lock(proposal_workflow_instance)

        second = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert first.group_id is not None
        assert second.group_id is not None
        assert second.group_id != first.group_id
        assert (
            second.output["thesis_facts"]["origin"]["proposal_logic_digest"]
            != first.output["thesis_facts"]["origin"]["proposal_logic_digest"]
        )
        assert second.output["thesis_facts"]["signals"]["used"] == ["catalog"]

    def test_workflow_human_context_changes_do_not_change_signature(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        service_lock(proposal_workflow_instance)
        first = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        config = proposal_workflow_instance.load_config()
        for step in config.workflows["propose_campaign_recommendations"].steps:
            if step.propose_relationship_group is not None:
                step.propose_relationship_group.thesis_text = "Updated human summary"
                step.propose_relationship_group.analysis_state = {
                    "debug_note": "changed only for reviewers"
                }
                step.propose_relationship_group.suggested_priority = "critical"
        proposal_workflow_instance.save_config(config)
        service_lock(proposal_workflow_instance)

        second = service_propose_workflow(
            proposal_workflow_instance,
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert first.group_id is not None
        assert second.group_id == first.group_id
        assert second.output["thesis_facts"] == first.output["thesis_facts"]

    @pytest.mark.parametrize(
        ("workflow_name", "row_shape"),
        [
            ("propose_from_relationship_query", "relationship"),
            ("propose_from_path_query", "path"),
        ],
    )
    def test_service_propose_workflow_preserves_query_evidence_on_members(
        self,
        query_evidence_proposal_instance: CruxibleInstance,
        workflow_name: str,
        row_shape: str,
    ) -> None:
        service_lock(query_evidence_proposal_instance)

        result = service_propose_workflow(
            query_evidence_proposal_instance,
            workflow_name,
            {"campaign_id": "CMP-1"},
        )

        assert result.group_id is not None
        assert result.query_receipt_ids
        assert result.output["query_receipt_ids"] == result.query_receipt_ids

        group_store = query_evidence_proposal_instance.get_group_store()
        try:
            group = group_store.get_group(result.group_id)
            members = group_store.get_members(result.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.source_query_receipt_ids == result.query_receipt_ids
        assert len(members) == 1

        evidence = members[0].source_query_evidence
        assert len(evidence) == 1
        assert evidence[0].query_receipt_id == result.query_receipt_ids[0]
        assert evidence[0].row_index == 0
        assert evidence[0].row_shape == row_shape
        if row_shape == "relationship":
            assert evidence[0].relationship is not None
            assert evidence[0].relationship["relationship_type"] == "candidate_product"
            assert evidence[0].relationship["from_id"] == "CMP-1"
            assert evidence[0].relationship["to_id"] == "SKU-123"
            assert evidence[0].relationship["properties"] == {
                "reason": "query evidence"
            }
        else:
            assert evidence[0].path is not None
            assert evidence[0].path[0]["alias"] == "candidate"
            assert evidence[0].path[0]["relationship_type"] == "candidate_product"
            assert evidence[0].path[0]["from_id"] == "CMP-1"
            assert evidence[0].path[0]["to_id"] == "SKU-123"

    def test_service_propose_workflow_uses_original_query_row_index_after_filter(
        self,
        query_evidence_proposal_instance: CruxibleInstance,
    ) -> None:
        graph = query_evidence_proposal_instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="Product",
                entity_id="SKU-456",
                properties={"sku": "SKU-456"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="candidate_product",
                from_type="Campaign",
                from_id="CMP-1",
                to_type="Product",
                to_id="SKU-456",
                properties={"reason": "second query evidence"},
            )
        )
        query_evidence_proposal_instance.save_graph(graph)
        service_lock(query_evidence_proposal_instance)

        result = service_propose_workflow(
            query_evidence_proposal_instance,
            "propose_from_filtered_relationship_query",
            {"campaign_id": "CMP-1"},
        )

        assert result.group_id is not None
        group_store = query_evidence_proposal_instance.get_group_store()
        try:
            members = group_store.get_members(result.group_id)
        finally:
            group_store.close()

        assert len(members) == 1
        assert members[0].to_id == "SKU-456"
        evidence = members[0].source_query_evidence
        assert evidence[0].query_receipt_id == result.query_receipt_ids[0]
        assert evidence[0].row_index == 1
        assert evidence[0].relationship is not None
        assert evidence[0].relationship["to_id"] == "SKU-456"
        receipt_store = query_evidence_proposal_instance.get_receipt_store()
        try:
            query_receipt = receipt_store.get_receipt(evidence[0].query_receipt_id)
        finally:
            receipt_store.close()
        assert query_receipt is not None
        assert evidence[0].row_index is not None
        receipt_row = query_receipt.results[evidence[0].row_index]
        assert receipt_row["to_id"] == "SKU-456"

    def test_service_propose_workflow_preserves_query_evidence_from_signal_rows(
        self,
        query_evidence_proposal_instance: CruxibleInstance,
    ) -> None:
        service_lock(query_evidence_proposal_instance)

        result = service_propose_workflow(
            query_evidence_proposal_instance,
            "propose_with_query_signals",
            {"campaign_id": "CMP-1"},
        )

        assert result.group_id is not None
        assert len(result.query_receipt_ids) == 2
        group_store = query_evidence_proposal_instance.get_group_store()
        try:
            group = group_store.get_group(result.group_id)
            members = group_store.get_members(result.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.source_query_receipt_ids == result.query_receipt_ids
        assert len(members) == 1
        assert members[0].signals[0].signal_source == "query_signal"
        assert [ref.source for ref in members[0].evidence_refs] == ["candidate_query"]
        assert members[0].evidence_rationale == "query evidence"
        assert [ref.source for ref in members[0].signals[0].evidence_refs] == [
            "signal_query"
        ]
        evidence_by_step = {
            evidence.source_step: evidence
            for evidence in members[0].source_query_evidence
        }
        assert set(evidence_by_step) == {"candidates_query", "signal_query"}
        signal_evidence = evidence_by_step["signal_query"]
        assert signal_evidence.query_receipt_id in result.query_receipt_ids
        assert signal_evidence.row_shape == "path"
        assert signal_evidence.path is not None
        assert signal_evidence.path[0]["alias"] == "candidate"
        assert signal_evidence.path[0]["relationship_type"] == "candidate_product"
        receipt_store = query_evidence_proposal_instance.get_receipt_store()
        try:
            for evidence in members[0].source_query_evidence:
                assert receipt_store.get_receipt(evidence.query_receipt_id) is not None
        finally:
            receipt_store.close()

        service_resolve_group(
            query_evidence_proposal_instance,
            result.group_id,
            "approve",
            expected_pending_version=1,
        )
        graph = query_evidence_proposal_instance.load_graph()
        relationship = graph.get_relationship(
            "Campaign",
            "CMP-1",
            "Product",
            "SKU-123",
            "recommended_for",
        )
        assert relationship is not None
        assert relationship.metadata.evidence is not None
        assert relationship.metadata.evidence.rationale == "query evidence"
        assert relationship.metadata.evidence.source_group_id == result.group_id
        assert [
            ref.source for ref in relationship.metadata.evidence.evidence_refs
        ] == ["candidate_query", "signal_query"]

    def test_service_propose_workflow_preserves_query_evidence_from_joined_rows(
        self,
        query_evidence_proposal_instance: CruxibleInstance,
    ) -> None:
        service_lock(query_evidence_proposal_instance)

        result = service_propose_workflow(
            query_evidence_proposal_instance,
            "propose_from_joined_query_rows",
            {"campaign_id": "CMP-1"},
        )

        assert result.group_id is not None
        assert len(result.query_receipt_ids) == 2
        group_store = query_evidence_proposal_instance.get_group_store()
        try:
            members = group_store.get_members(result.group_id)
        finally:
            group_store.close()

        assert len(members) == 1
        evidence_by_step = {
            evidence.source_step: evidence
            for evidence in members[0].source_query_evidence
        }
        assert set(evidence_by_step) == {"candidates_query", "product_query"}
        relationship_evidence = evidence_by_step["candidates_query"]
        assert relationship_evidence.query_receipt_id in result.query_receipt_ids
        assert relationship_evidence.row_index == 0
        assert relationship_evidence.feedback_addressable is True
        assert relationship_evidence.row_shape == "relationship"
        assert relationship_evidence.relationship is not None
        assert relationship_evidence.relationship["to_id"] == "SKU-123"

        product_evidence = evidence_by_step["product_query"]
        assert product_evidence.query_receipt_id in result.query_receipt_ids
        assert product_evidence.row_index == 0
        assert product_evidence.feedback_addressable is True
        assert product_evidence.row_shape == "entity"
        assert product_evidence.entity == {
            "entity_type": "Product",
            "entity_id": "SKU-123",
        }

    def test_service_propose_workflow_preserves_query_evidence_after_join_shape(
        self,
        query_evidence_proposal_instance: CruxibleInstance,
    ) -> None:
        service_lock(query_evidence_proposal_instance)

        result = service_propose_workflow(
            query_evidence_proposal_instance,
            "propose_from_shaped_joined_query_rows",
            {"campaign_id": "CMP-1"},
        )

        assert result.group_id is not None
        assert len(result.query_receipt_ids) == 2
        group_store = query_evidence_proposal_instance.get_group_store()
        try:
            members = group_store.get_members(result.group_id)
        finally:
            group_store.close()

        assert len(members) == 1
        evidence_by_step = {
            evidence.source_step: evidence
            for evidence in members[0].source_query_evidence
        }
        assert set(evidence_by_step) == {"candidates_query", "product_query"}
        for evidence in evidence_by_step.values():
            assert evidence.query_receipt_id in result.query_receipt_ids
            assert evidence.row_index == 0
            assert evidence.feedback_addressable is True
        assert evidence_by_step["candidates_query"].row_shape == "relationship"
        assert evidence_by_step["product_query"].row_shape == "entity"

    def test_retain_missing_preserves_query_receipts_for_retained_members(
        self,
        query_evidence_proposal_instance: CruxibleInstance,
    ) -> None:
        config = query_evidence_proposal_instance.load_config()
        for step in config.workflows["propose_from_relationship_query"].steps:
            if step.propose_relationship_group is not None:
                step.propose_relationship_group.pending_refresh_mode = "retain_missing"
        query_evidence_proposal_instance.save_config(config)
        service_lock(query_evidence_proposal_instance)

        first = service_propose_workflow(
            query_evidence_proposal_instance,
            "propose_from_relationship_query",
            {"campaign_id": "CMP-1"},
        )

        graph = query_evidence_proposal_instance.load_graph()
        assert graph.remove_relationship(
            "Campaign",
            "CMP-1",
            "Product",
            "SKU-123",
            "candidate_product",
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Product",
                entity_id="SKU-456",
                properties={"sku": "SKU-456"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="candidate_product",
                from_type="Campaign",
                from_id="CMP-1",
                to_type="Product",
                to_id="SKU-456",
                properties={"reason": "second query evidence"},
            )
        )
        query_evidence_proposal_instance.save_graph(graph)

        second = service_propose_workflow(
            query_evidence_proposal_instance,
            "propose_from_relationship_query",
            {"campaign_id": "CMP-1"},
        )

        assert second.group_id == first.group_id
        group_store = query_evidence_proposal_instance.get_group_store()
        try:
            group = group_store.get_group(second.group_id)
            members = group_store.get_members(second.group_id)
        finally:
            group_store.close()

        assert group is not None
        expected_receipt_ids = [*first.query_receipt_ids, *second.query_receipt_ids]
        assert group.source_query_receipt_ids == expected_receipt_ids
        assert {(member.from_id, member.to_id) for member in members} == {
            ("CMP-1", "SKU-123"),
            ("CMP-1", "SKU-456"),
        }
        member_receipts = {
            member.to_id: member.source_query_evidence[0].query_receipt_id
            for member in members
        }
        assert member_receipts == {
            "SKU-123": first.query_receipt_ids[0],
            "SKU-456": second.query_receipt_ids[0],
        }

    def test_replace_rewrite_drops_stale_query_receipts_for_removed_members(
        self,
        query_evidence_proposal_instance: CruxibleInstance,
    ) -> None:
        service_lock(query_evidence_proposal_instance)

        first = service_propose_workflow(
            query_evidence_proposal_instance,
            "propose_from_relationship_query",
            {"campaign_id": "CMP-1"},
        )

        graph = query_evidence_proposal_instance.load_graph()
        assert graph.remove_relationship(
            "Campaign",
            "CMP-1",
            "Product",
            "SKU-123",
            "candidate_product",
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Product",
                entity_id="SKU-456",
                properties={"sku": "SKU-456"},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="candidate_product",
                from_type="Campaign",
                from_id="CMP-1",
                to_type="Product",
                to_id="SKU-456",
                properties={"reason": "second query evidence"},
            )
        )
        query_evidence_proposal_instance.save_graph(graph)

        second = service_propose_workflow(
            query_evidence_proposal_instance,
            "propose_from_relationship_query",
            {"campaign_id": "CMP-1"},
        )

        assert second.group_id == first.group_id
        group_store = query_evidence_proposal_instance.get_group_store()
        try:
            group = group_store.get_group(second.group_id)
            members = group_store.get_members(second.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.source_query_receipt_ids == second.query_receipt_ids
        assert first.query_receipt_ids[0] not in group.source_query_receipt_ids
        assert {(member.from_id, member.to_id) for member in members} == {
            ("CMP-1", "SKU-456"),
        }
        evidence = members[0].source_query_evidence
        assert evidence[0].query_receipt_id == second.query_receipt_ids[0]
        assert evidence[0].relationship is not None
        assert evidence[0].relationship["to_id"] == "SKU-456"

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

    def test_service_propose_workflow_requires_proposal_type(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.workflows["propose_campaign_recommendations"].type = "utility"
        proposal_workflow_instance.save_config(config)

        with pytest.raises(ConfigError, match="must set type: proposal"):
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

    def test_service_propose_workflow_empty_candidates_strict_by_default(
        self, query_evidence_proposal_instance: CruxibleInstance
    ) -> None:
        service_lock(query_evidence_proposal_instance)

        with pytest.raises(QueryExecutionError, match="produced no candidates"):
            service_propose_workflow(
                query_evidence_proposal_instance,
                "propose_from_filtered_relationship_query",
                {"campaign_id": "CMP-1"},
            )

    def test_service_propose_workflow_completes_empty_candidates_when_allowed(
        self, query_evidence_proposal_instance: CruxibleInstance
    ) -> None:
        config = query_evidence_proposal_instance.load_config()
        proposal_step = config.workflows[
            "propose_from_filtered_relationship_query"
        ].steps[-1]
        assert proposal_step.propose_relationship_group is not None
        proposal_step.propose_relationship_group.on_empty = "complete"
        query_evidence_proposal_instance.save_config(config)
        service_lock(query_evidence_proposal_instance)

        result = service_propose_workflow(
            query_evidence_proposal_instance,
            "propose_from_filtered_relationship_query",
            {"campaign_id": "CMP-1"},
        )

        assert result.group_id is None
        assert result.group_status == "no_candidates"
        assert result.review_priority == "normal"
        assert result.output["status"] == "no_candidates"
        assert result.output["candidate_count"] == 0
        assert result.output["on_empty"] == "complete"
        assert result.output["group_created"] is False
        assert result.output["thesis_facts"]["origin"]["proposal_logic_digest"].startswith(
            "sha256:"
        )
        assert result.receipt is not None
        assert result.receipt.committed is False
        assert result.receipt.results[0]["output"] == result.output
        assert result.receipt.nodes[0].detail["group_status"] == "no_candidates"
        proposal_step_node = next(
            node
            for node in result.receipt.nodes
            if node.node_type == "plan_step"
            and node.detail.get("step_id") == "proposal"
        )
        assert proposal_step_node.detail["candidate_count"] == 0
        assert proposal_step_node.detail["group_created"] is False
        assert proposal_step_node.detail["on_empty"] == "complete"
        group_store = query_evidence_proposal_instance.get_group_store()
        try:
            groups = group_store.list_groups()
        finally:
            group_store.close()
        assert groups == []
        receipt_store = query_evidence_proposal_instance.get_receipt_store()
        try:
            saved_receipt = receipt_store.get_receipt(result.receipt_id)
        finally:
            receipt_store.close()
        assert saved_receipt is not None
        assert saved_receipt.results[0]["output"] == result.output

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
        assert overlay_result.instance.metadata.origin_snapshot_id == created.snapshot.snapshot_id
        assert (overlay_root / ".cruxible" / "cruxible.lock.yaml").exists()
        overlay_graph = overlay_result.instance.load_graph()
        assert overlay_graph.edge_count("recommended_for") == 2
