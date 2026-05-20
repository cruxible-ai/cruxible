"""Tests for workflow execution runtime behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.support.workflow_helpers import (
    compute_directory_sha256,
    json_contract_instance,
    json_contract_workflow_yaml,
    write_lock_for_instance,
)

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import (
    NamedQuerySchema,
    PropertySchema,
    RelationshipSchema,
    TraversalStep,
    WorkflowSchema,
    WorkflowStepSchema,
)
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.receipt.serializer import to_markdown
from cruxible_core.service import service_list
from cruxible_core.workflow import build_lock, compile_workflow, execute_workflow


class TestWorkflowExecutor:
    def test_execute_workflow_success(self, workflow_instance: CruxibleInstance) -> None:
        write_lock_for_instance(workflow_instance)

        result = execute_workflow(
            workflow_instance,
            workflow_instance.load_config(),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert result.output["decision"] == "approve"
        assert result.mode == "run"
        assert result.workflow_type == "utility"
        assert result.receipt.operation_type == "workflow"
        assert result.receipt.workflow_mode == "run"
        assert len(result.query_receipt_ids) == 1
        assert len(result.traces) == 2
        trace_ids = {trace.trace_id for trace in result.traces}
        plan_steps = [node for node in result.receipt.nodes if node.node_type == "plan_step"]
        assert any(node.detail.get("receipt_id") in result.query_receipt_ids for node in plan_steps)
        assert any(node.detail.get("trace_id") in trace_ids for node in plan_steps)
        rendered = to_markdown(result.receipt)
        assert "**Workflow:** evaluate_promo" in rendered
        assert "## Plan Steps" in rendered

    def test_query_step_inherits_related_edge_exclusion(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.relationships.append(
            RelationshipSchema(
                name="suppressed_for",
                from_entity="Campaign",
                to_entity="Product",
            )
        )
        config.named_queries["get_active_recommendations"] = NamedQuerySchema(
            entry_point="Campaign",
            traversal=[
                TraversalStep(
                    relationship="recommended_for",
                    direction="outgoing",
                    exclude_if_related=[
                        {
                            "relationship": "suppressed_for",
                            "direction": "outgoing",
                        }
                    ],
                )
            ],
            returns="list[Product]",
            result_shape="entity",
        )
        config.workflows["query_active_recommendations"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="products",
                    query="get_active_recommendations",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "products"},
                )
            ],
            returns="products",
        )
        proposal_workflow_instance.save_config(config)

        graph = proposal_workflow_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="recommended_for",
                from_type="Campaign",
                from_id="CMP-1",
                to_type="Product",
                to_id="SKU-123",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="recommended_for",
                from_type="Campaign",
                from_id="CMP-1",
                to_type="Product",
                to_id="SKU-456",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="suppressed_for",
                from_type="Campaign",
                from_id="CMP-1",
                to_type="Product",
                to_id="SKU-456",
                properties={},
            )
        )
        proposal_workflow_instance.save_graph(graph)
        write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "query_active_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert result.output["total_results"] == 1
        assert [item["entity_id"] for item in result.output["results"]] == ["SKU-123"]

    def test_query_step_passes_path_rows_through(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["recommendation_paths"] = NamedQuerySchema(
            entry_point="Campaign",
            traversal=[
                TraversalStep(
                    relationship="recommended_for",
                    direction="outgoing",
                    alias="recommendation",
                )
            ],
            returns="list[Product]",
            result_shape="path",
            dedupe="path",
        )
        config.workflows["query_recommendation_paths"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="paths",
                    query="recommendation_paths",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "paths"},
                )
            ],
            returns="paths",
        )
        proposal_workflow_instance.save_config(config)
        graph = proposal_workflow_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="recommended_for",
                from_type="Campaign",
                from_id="CMP-1",
                to_type="Product",
                to_id="SKU-123",
                properties={"reason": "catalog"},
            )
        )
        proposal_workflow_instance.save_graph(graph)
        write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "query_recommendation_paths",
            {"campaign_id": "CMP-1"},
        )

        assert result.output["result_shape"] == "path"
        row = result.output["results"][0]
        assert row["entry"]["entity_type"] == "Campaign"
        assert row["result"]["entity_type"] == "Product"
        assert row["entities"][1]["entity_id"] == "SKU-123"
        assert row["path"][0]["alias"] == "recommendation"
        assert row["path"][0]["metadata"]["assertion"]["lifecycle"]["status"] == "active"

    def test_query_step_passes_relationship_rows_through(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["recommendation_edges"] = NamedQuerySchema(
            entry_point="Campaign",
            traversal=[
                TraversalStep(
                    relationship="recommended_for",
                    direction="outgoing",
                )
            ],
            returns="recommended_for",
            result_shape="relationship",
            dedupe="path",
        )
        config.workflows["query_recommendation_edges"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="edges",
                    query="recommendation_edges",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "edges"},
                )
            ],
            returns="edges",
        )
        proposal_workflow_instance.save_config(config)
        graph = proposal_workflow_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="recommended_for",
                from_type="Campaign",
                from_id="CMP-1",
                to_type="Product",
                to_id="SKU-123",
                properties={"reason": "catalog"},
            )
        )
        proposal_workflow_instance.save_graph(graph)
        write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "query_recommendation_edges",
            {"campaign_id": "CMP-1"},
        )

        assert result.output["result_shape"] == "relationship"
        row = result.output["results"][0]
        assert row["relationship_type"] == "recommended_for"
        assert row["edge_key"] is not None
        assert row["entry"]["entity_type"] == "Campaign"
        assert row["to_entity"]["entity_id"] == "SKU-123"

    def test_query_step_includes_query_limit_metadata(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["limited_recommendation_paths"] = NamedQuerySchema(
            entry_point="Campaign",
            traversal=[
                TraversalStep(
                    relationship="recommended_for",
                    direction="outgoing",
                    alias="recommendation",
                )
            ],
            returns="list[Product]",
            result_shape="path",
            dedupe="path",
            select={
                "sku": "$result.entity_id",
                "edge_key": "$path.recommendation.edge.edge_key",
            },
            limit=1,
        )
        config.workflows["query_limited_recommendations"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="paths",
                    query="limited_recommendation_paths",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "paths"},
                )
            ],
            returns="paths",
        )
        proposal_workflow_instance.save_config(config)
        graph = proposal_workflow_instance.load_graph()
        for sku in ("SKU-123", "SKU-456"):
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="recommended_for",
                    from_type="Campaign",
                    from_id="CMP-1",
                    to_type="Product",
                    to_id=sku,
                    properties={"reason": "catalog"},
                )
            )
        proposal_workflow_instance.save_graph(graph)
        write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "query_limited_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert len(result.output["results"]) == 1
        assert result.output["total_results"] == 2
        assert result.output["limit"] == 1
        assert result.output["truncated"] is True
        assert set(result.output["results"][0]) == {"values"}
        assert result.output["results"][0]["values"]["sku"] == "SKU-123"

    def test_execute_workflow_list_entities_step_returns_items(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.contracts["PromoInput"].fields["category"] = PropertySchema(
            type="string",
            optional=True,
        )
        config.workflows["evaluate_promo"].steps.insert(
            1,
            WorkflowStepSchema(
                id="products",
                list_entities={
                    "entity_type": "Product",
                    "property_filter": {"category": "$input.category"},
                    "limit": 5,
                },
                **{"as": "products"},
            ),
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        result = execute_workflow(
            workflow_instance,
            workflow_instance.load_config(),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "category": "soda",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert result.step_outputs["products"]["total"] == 1
        assert len(result.step_outputs["products"]["items"]) == 1
        products_step = next(
            node
            for node in result.receipt.nodes
            if node.node_type == "plan_step" and node.detail.get("step_id") == "products"
        )
        assert products_step.detail["entity_type"] == "Product"
        assert products_step.detail["item_count"] == 1

    def test_execute_workflow_list_entities_matches_service_list(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        graph = workflow_instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="Product",
                entity_id="SKU-123",
                properties={"category": "soda", "base_margin": 0.2},
            )
        )
        workflow_instance.save_graph(graph)
        config = workflow_instance.load_config()
        config.contracts["PromoInput"].fields["category"] = PropertySchema(
            type="string",
            optional=True,
        )
        config.workflows["evaluate_promo"].steps.insert(
            1,
            WorkflowStepSchema(
                id="products",
                list_entities={
                    "entity_type": "Product",
                    "property_filter": {"sku": "$input.sku"},
                    "limit": 5,
                },
                **{"as": "products"},
            ),
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        result = execute_workflow(
            workflow_instance,
            workflow_instance.load_config(),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "category": "soda",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )
        listed = service_list(
            workflow_instance,
            "entities",
            entity_type="Product",
            property_filter={"sku": "SKU-123"},
            limit=5,
        )

        assert result.step_outputs["products"]["total"] == listed.total
        assert result.step_outputs["products"]["items"][0]["properties"]["sku"] == "SKU-123"
        assert result.step_outputs["products"]["items"] == [
            item.model_dump(mode="python") for item in listed.items
        ]

    def test_execute_workflow_list_relationships_step_returns_items(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.contracts["CampaignInput"].fields["status"] = PropertySchema(
            type="string",
            optional=True,
        )
        config.workflows["propose_campaign_recommendations"].steps.insert(
            1,
            WorkflowStepSchema(
                id="existing_links",
                list_relationships={
                    "relationship_type": "recommended_for",
                    "property_filter": {"status": "$input.status"},
                    "limit": 10,
                },
                **{"as": "existing_links"},
            ),
        )
        proposal_workflow_instance.save_config(config)
        write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1", "status": "human_approved"},
        )

        assert result.step_outputs["existing_links"]["total"] == 0
        assert result.step_outputs["existing_links"]["items"] == []
        links_step = next(
            node
            for node in result.receipt.nodes
            if node.node_type == "plan_step" and node.detail.get("step_id") == "existing_links"
        )
        assert links_step.detail["relationship_type"] == "recommended_for"
        assert links_step.detail["item_count"] == 0

    def test_execute_workflow_list_relationships_matches_service_list(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.contracts["CampaignInput"].fields["status"] = PropertySchema(
            type="string",
            optional=True,
        )
        config.workflows["propose_campaign_recommendations"].steps.insert(
            1,
            WorkflowStepSchema(
                id="existing_links",
                list_relationships={
                    "relationship_type": "recommended_for",
                    "property_filter": {"status": "$input.status"},
                    "limit": 10,
                },
                **{"as": "existing_links"},
            ),
        )
        proposal_workflow_instance.save_config(config)
        write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1", "status": "human_approved"},
        )
        listed = service_list(
            proposal_workflow_instance,
            "edges",
            relationship_type="recommended_for",
            property_filter={"status": "human_approved"},
            limit=10,
        )

        assert result.step_outputs["existing_links"]["total"] == listed.total
        assert result.step_outputs["existing_links"]["items"] == listed.items

    def test_execute_workflow_rejects_provider_output_contract(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.providers[
            "margin_calculator"
        ].ref = "tests.support.workflow_test_providers.broken_provider"
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        with pytest.raises(QueryExecutionError, match="output failed contract"):
            execute_workflow(
                workflow_instance,
                workflow_instance.load_config(),
                "evaluate_promo",
                {
                    "sku": "SKU-123",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-07",
                },
            )

        store = workflow_instance.get_receipt_store()
        try:
            receipts = store.list_receipts(operation_type="workflow")
            receipt = store.get_receipt(receipts[0]["receipt_id"])
        finally:
            store.close()
        assert receipt is not None
        assert receipt.committed is False
        assert receipt.nodes[0].detail["mode"] == "run"
        assert receipt.nodes[0].detail["error_type"] == "QueryExecutionError"
        assert "output failed contract" in receipt.nodes[0].detail["error"]
        provider_steps = [
            node
            for node in receipt.nodes
            if node.node_type == "plan_step" and node.detail.get("kind") == "provider"
        ]
        assert any(step.detail.get("status") == "error" for step in provider_steps)

    def test_compile_workflow_rejects_missing_required_json_schema_field(
        self,
        tmp_path: Path,
    ) -> None:
        instance = json_contract_instance(
            tmp_path,
            json_contract_workflow_yaml(
                workflow_payload_field="""
                payload:
                  type: json
                  json_schema:
                    type: object
                    required: [status]
                    properties:
                      status:
                        type: string
                """,
                provider_payload_field="payload: {type: json}",
                provider_items_field="items: {type: json}",
            ),
        )

        with pytest.raises(ConfigError, match="payload: missing required property 'status'"):
            compile_workflow(
                instance.load_config(),
                build_lock(instance.load_config()),
                "validate_json_payload",
                {"payload": {}},
            )

    def test_execute_workflow_rejects_provider_input_json_schema_type(
        self,
        tmp_path: Path,
    ) -> None:
        instance = json_contract_instance(
            tmp_path,
            json_contract_workflow_yaml(
                workflow_payload_field="payload: {type: json}",
                provider_payload_field="""
                payload:
                  type: json
                  json_schema:
                    type: object
                    properties:
                      count:
                        type: integer
                """,
                provider_items_field="items: {type: json}",
            ),
        )

        with pytest.raises(QueryExecutionError, match=r"payload\.count: must be an integer"):
            execute_workflow(
                instance,
                instance.load_config(),
                "validate_json_payload",
                {"payload": {"count": "3"}},
            )

    def test_execute_workflow_rejects_provider_output_json_schema_inline_enum(
        self,
        tmp_path: Path,
    ) -> None:
        instance = json_contract_instance(
            tmp_path,
            json_contract_workflow_yaml(
                workflow_payload_field="payload: {type: json}",
                provider_payload_field="payload: {type: json}",
                provider_items_field="""
                items:
                  type: json
                  json_schema:
                    type: array
                    items:
                      type: object
                      properties:
                        verdict:
                          type: string
                          enum: [support]
                """,
            ),
        )

        with pytest.raises(QueryExecutionError, match=r"items\[1\]\.verdict"):
            execute_workflow(
                instance,
                instance.load_config(),
                "validate_json_payload",
                {"payload": {"items": [{"verdict": "support"}, {"verdict": "reject"}]}},
            )

    def test_execute_workflow_rejects_provider_output_json_schema_enum_ref(
        self,
        tmp_path: Path,
    ) -> None:
        instance = json_contract_instance(
            tmp_path,
            json_contract_workflow_yaml(
                workflow_payload_field="payload: {type: json}",
                provider_payload_field="payload: {type: json}",
                provider_items_field="""
                items:
                  type: json
                  json_schema:
                    type: array
                    items:
                      type: object
                      properties:
                        verdict:
                          type: string
                          enum_ref: verdict
                        note:
                          type: string
                """,
            ),
        )

        with pytest.raises(QueryExecutionError, match="enum_ref 'verdict'"):
            execute_workflow(
                instance,
                instance.load_config(),
                "validate_json_payload",
                {"payload": {"items": [{"verdict": "support"}, {"verdict": "other"}]}},
            )

    def test_execute_workflow_validates_defaulted_json_schema_field(
        self,
        tmp_path: Path,
    ) -> None:
        instance = json_contract_instance(
            tmp_path,
            json_contract_workflow_yaml(
                workflow_payload_field="""
                payload:
                  type: json
                  default:
                    status: bad
                  json_schema:
                    type: object
                    properties:
                      status:
                        type: string
                        enum_ref: verdict
                """,
                provider_payload_field="payload: {type: json}",
                provider_items_field="items: {type: json}",
            ),
        )

        with pytest.raises(ConfigError, match="field 'payload' default"):
            compile_workflow(
                instance.load_config(),
                build_lock(instance.load_config()),
                "validate_json_payload",
                {},
            )

    def test_execute_workflow_allows_optional_json_none_and_extra_nested_keys(
        self,
        tmp_path: Path,
    ) -> None:
        instance = json_contract_instance(
            tmp_path,
            json_contract_workflow_yaml(
                workflow_payload_field="""
                payload:
                  type: json
                  optional: true
                  json_schema:
                    type: object
                    required: [status]
                """,
                provider_payload_field="""
                payload:
                  type: json
                  optional: true
                  json_schema:
                    type: object
                    required: [status]
                """,
                provider_items_field="""
                items:
                  type: json
                  json_schema:
                    type: array
                    items:
                      type: object
                      required: [verdict]
                      properties:
                        verdict:
                          type: string
                          enum_ref: verdict
                """,
            ),
        )

        none_result = execute_workflow(
            instance,
            instance.load_config(),
            "validate_json_payload",
            {"payload": None},
        )
        assert none_result.output["items"] == []

        extra_key_result = execute_workflow(
            instance,
            instance.load_config(),
            "validate_json_payload",
            {"payload": {"status": "support", "extra": True, "items": []}},
        )
        assert extra_key_result.output["items"] == []

        nested_null_result = execute_workflow(
            instance,
            instance.load_config(),
            "validate_json_payload",
            {
                "payload": {
                    "status": "support",
                    "items": [{"verdict": "support", "note": None}],
                }
            },
        )
        assert nested_null_result.output["items"] == [{"verdict": "support", "note": None}]

    def test_execute_workflow_assert_failure_records_workflow_receipt(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        for step in config.workflows["evaluate_promo"].steps:
            if step.assert_spec is not None:
                step.assert_spec.right = 0.90
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        with pytest.raises(QueryExecutionError, match="Margin below threshold"):
            execute_workflow(
                workflow_instance,
                workflow_instance.load_config(),
                "evaluate_promo",
                {
                    "sku": "SKU-123",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-07",
                },
            )

        store = workflow_instance.get_receipt_store()
        try:
            receipts = store.list_receipts(operation_type="workflow")
            receipt = store.get_receipt(receipts[0]["receipt_id"])
        finally:
            store.close()
        assert receipts
        assert receipt is not None
        assert receipt.committed is False
        assert receipt.nodes[0].detail["mode"] == "run"
        assert receipt.nodes[0].detail["error_type"] == "QueryExecutionError"
        assert receipt.nodes[0].detail["error"] == "Margin below threshold"

    def test_execute_workflow_builds_relationship_proposal_artifact(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert result.output["relationship_type"] == "recommended_for"
        assert result.mode == "proposal"
        assert result.workflow_type == "proposal"
        assert result.receipt.workflow_mode == "proposal"
        assert len(result.output["members"]) == 2
        assert result.output["signal_sources_used"] == ["catalog"]
        assert result.output["members"][0]["signals"][0]["basis"] == {
            "mode": "enum",
            "path": "verdict",
            "value": "match",
            "matched": "match",
        }
        assert len(result.traces) == 1
        plan_steps = [node for node in result.receipt.nodes if node.node_type == "plan_step"]
        assert any(node.detail.get("relationship_type") == "recommended_for" for node in plan_steps)
        signal_step = next(
            node for node in plan_steps if node.detail.get("signal_source") == "catalog"
        )
        assert signal_step.detail["mapping"] == {
            "mode": "enum",
            "path": "verdict",
            "map": {"match": "support", "fallback": "unsure", "reject": "contradict"},
        }
        assert any(node.detail.get("signals_from") == ["catalog_signals"] for node in plan_steps)

    def test_execute_workflow_reports_duplicate_candidate_inputs(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.providers[
            "campaign_recommendations"
        ].ref = "tests.support.workflow_test_providers.duplicate_campaign_recommendations"
        workflow = config.workflows["propose_campaign_recommendations"]
        workflow.steps = [step for step in workflow.steps if step.map_signals is None]
        for step in workflow.steps:
            if step.propose_relationship_group is not None:
                step.propose_relationship_group.signals_from = []
        proposal_workflow_instance.save_config(config)
        write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        candidate_set = result.step_outputs["candidates"]
        assert len(candidate_set["candidates"]) == 2
        assert candidate_set["duplicate_input_count"] == 1
        assert candidate_set["conflicting_duplicate_count"] == 1
        example = candidate_set["duplicate_examples"][0]
        assert example["from_id"] == "CMP-1"
        assert example["to_id"] == "SKU-123"
        assert example["relationship_type"] == "recommended_for"
        assert example["conflicting"] is True
        assert example["first_properties"] == {"reason": "north bestseller"}
        assert example["duplicate_properties"] == {"reason": "north duplicate rationale"}

        candidates_step = next(
            node
            for node in result.receipt.nodes
            if node.node_type == "plan_step" and node.detail.get("step_id") == "candidates"
        )
        assert candidates_step.detail["candidate_count"] == 2
        assert candidates_step.detail["item_count"] == 3
        assert candidates_step.detail["duplicate_input_count"] == 1
        assert candidates_step.detail["conflicting_duplicate_count"] == 1
        assert candidates_step.detail["duplicate_examples"] == candidate_set["duplicate_examples"]

    def test_execute_canonical_workflow_run_mode_raises(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        write_lock_for_instance(canonical_workflow_instance)

        with pytest.raises(
            ConfigError,
            match="Canonical workflows use preview-first execution",
        ):
            execute_workflow(
                canonical_workflow_instance,
                canonical_workflow_instance.load_config(),
                "build_reference",
                {},
            )

    def test_execute_canonical_workflow_preview_does_not_mutate_graph(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        write_lock_for_instance(canonical_workflow_instance)

        result = execute_workflow(
            canonical_workflow_instance,
            canonical_workflow_instance.load_config(),
            "build_reference",
            {},
            mode="preview",
        )

        assert result.mode == "preview"
        assert result.canonical is True
        assert result.apply_digest is not None
        assert result.committed_snapshot_id is None
        assert result.receipt.committed is False
        assert result.receipt.workflow_mode == "preview"
        assert result.receipt.head_snapshot_id == result.head_snapshot_id
        assert result.output["total_results"] == 1
        assert canonical_workflow_instance.load_graph().list_entities("Vendor") == []

    def test_provider_artifact_relative_file_uri_resolves_from_config_dir(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        config.artifacts["canonical_bundle"].uri = "file:bundle"
        canonical_workflow_instance.save_config(config)
        write_lock_for_instance(canonical_workflow_instance)

        result = execute_workflow(
            canonical_workflow_instance,
            canonical_workflow_instance.load_config(),
            "build_reference",
            {},
            mode="preview",
        )

        assert result.step_outputs["rows"]["items"][0]["vendor_id"] == "vendor-acme"
        assert result.output["total_results"] == 1

    def test_execute_canonical_workflow_apply_commits_graph_and_snapshot(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        write_lock_for_instance(canonical_workflow_instance)
        preview = execute_workflow(
            canonical_workflow_instance,
            canonical_workflow_instance.load_config(),
            "build_reference",
            {},
            mode="preview",
        )

        applied = execute_workflow(
            canonical_workflow_instance,
            canonical_workflow_instance.load_config(),
            "build_reference",
            {},
            mode="apply",
        )

        assert applied.mode == "apply"
        assert applied.apply_digest == preview.apply_digest
        assert applied.committed_snapshot_id is not None
        assert applied.receipt.committed is True
        assert applied.receipt.workflow_mode == "apply"
        assert applied.receipt.head_snapshot_id == applied.head_snapshot_id
        assert canonical_workflow_instance.load_graph().has_entity("Vendor", "vendor-acme")

    def test_canonical_workflow_tabular_shape_ingest_parity(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "assets.csv").write_text(
            "\n".join(
                [
                    "asset_id,priority,internet_exposed,tags_json",
                    'ASSET-1,1,true,"[""prod"",""api""]"',
                    'ASSET-2,2,false,"[""staging""]"',
                ]
            )
            + "\n"
        )
        bundle_sha256 = compute_directory_sha256(bundle_dir)
        config_yaml = f"""\
version: "1.0"
name: tabular_shape_ingest_parity
kind: world_model

entity_types:
  Asset:
    properties:
      asset_id:
        type: string
        primary_key: true
      priority:
        type: int
      internet_exposed:
        type: bool
      tags:
        type: json

relationships: []

contracts:
  EmptyInput:
    fields: {{}}
  TabularParseOptions:
    fields:
      table_names:
        type: json
        optional: true
  ParsedTabularBundle:
    fields:
      artifact:
        type: json
      tables:
        type: json
      files:
        type: json
      diagnostics:
        type: json

artifacts:
  seed_bundle:
    kind: directory
    uri: ./bundle
    sha256: {bundle_sha256}

providers:
  parse_seed_bundle:
    kind: function
    contract_in: TabularParseOptions
    contract_out: ParsedTabularBundle
    ref: cruxible_core.providers.common.tabular.load_tabular_artifact_bundle
    version: 1.0.0
    deterministic: true
    runtime: python
    artifact: seed_bundle

workflows:
  import_assets:
    type: canonical
    contract_in: EmptyInput
    steps:
      - id: parsed
        provider: parse_seed_bundle
        input:
          table_names:
            assets.csv: assets
        as: parsed
      - id: shaped
        shape_items:
          items: $steps.parsed.tables.assets.rows
          include_input: false
          rename:
            tags_json: tags
          fields:
            asset_id: $item.asset_id
            priority: $item.priority
            internet_exposed: $item.internet_exposed
          casts:
            priority: int
            internet_exposed: bool
            tags: json
          required: [asset_id]
        as: shaped
      - id: assets
        make_entities:
          entity_type: Asset
          items: $steps.shaped.items
          entity_id: $item.asset_id
          properties:
            asset_id: $item.asset_id
            priority: $item.priority
            internet_exposed: $item.internet_exposed
            tags: $item.tags
        as: assets
      - id: apply_assets
        apply_entities:
          entities_from: assets
        as: apply_assets
    returns: apply_assets
"""
        (tmp_path / "config.yaml").write_text(config_yaml)
        instance = CruxibleInstance.init(tmp_path, "config.yaml")
        write_lock_for_instance(instance)

        execute_workflow(instance, instance.load_config(), "import_assets", {}, mode="apply")

        asset = instance.load_graph().get_entity("Asset", "ASSET-1")
        assert asset is not None
        assert asset.properties["priority"] == 1
        assert isinstance(asset.properties["priority"], int)
        assert asset.properties["internet_exposed"] is True
        assert isinstance(asset.properties["internet_exposed"], bool)
        assert asset.properties["tags"] == ["prod", "api"]
        assert isinstance(asset.properties["tags"], list)

    def test_canonical_preview_reports_duplicate_inputs(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        rows_path = canonical_workflow_instance.root / "bundle" / "rows.json"
        rows = json.loads(rows_path.read_text())
        rows.append(
            {
                "vendor_id": "vendor-acme",
                "vendor_name": "Acme",
                "product_id": "product-acme-widget",
                "product_name": "Widget Renamed",
                "cve_id": "CVE-2026-0001",
                "description": "Widget issue",
            }
        )
        rows_path.write_text(json.dumps(rows, indent=2, sort_keys=True))
        config = canonical_workflow_instance.load_config()
        config.artifacts["canonical_bundle"].sha256 = compute_directory_sha256(
            canonical_workflow_instance.root / "bundle"
        )
        canonical_workflow_instance.save_config(config)
        write_lock_for_instance(canonical_workflow_instance)

        result = execute_workflow(
            canonical_workflow_instance,
            canonical_workflow_instance.load_config(),
            "build_reference",
            {},
            mode="preview",
        )

        product_preview = result.apply_previews["apply_products"]
        assert product_preview["duplicate_input_count"] == 1
        assert product_preview["conflicting_duplicate_count"] == 1
        assert product_preview["duplicate_examples"][0]["entity_id"] == "product-acme-widget"

        rel_preview = result.apply_previews["apply_product_vendor"]
        assert rel_preview["duplicate_input_count"] == 1
        assert rel_preview["conflicting_duplicate_count"] == 0
