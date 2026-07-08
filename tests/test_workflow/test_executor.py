"""Tests for workflow execution runtime behavior."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.support.workflow_helpers import (
    compute_directory_sha256,
    json_contract_instance,
    json_contract_workflow_yaml,
    write_lock_for_instance,
)

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import (
    CandidateEvidenceSpec,
    ContractSchema,
    MakeRelationshipsSpec,
    NamedQuerySchema,
    PropertySchema,
    RelationshipSchema,
    TraversalStep,
    WorkflowSchema,
    WorkflowStepSchema,
)
from cruxible_core.errors import ConfigError, DataValidationError, QueryExecutionError
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, RelationshipMetadata
from cruxible_core.receipt.serializer import to_markdown
from cruxible_core.service import service_list, service_run
from cruxible_core.storage.sqlite import SQLiteGraphRepository
from cruxible_core.workflow import (
    build_lock,
    compile_workflow,
    execute_workflow,
    get_lock_path,
    write_lock,
)
from cruxible_core.workflow.apply import make_relationship_set
from cruxible_core.workflow.step_helpers import SOURCE_METADATA_KEY

USER_INDEX_FIELD = "_query_result_index"


def _review_metadata(status: str) -> RelationshipMetadata:
    return RelationshipMetadata(
        assertion=RelationshipAssertion(
            review=RelationshipReviewState(status=status),
        )
    )


def _envelope_input_contract_yaml(*, declare_source_metadata: bool = False) -> str:
    source_metadata_field = ""
    if declare_source_metadata:
        source_metadata_field = f"""
      {SOURCE_METADATA_KEY}:
        type: json
        optional: true"""
    return f"""\
version: "1.0"
name: envelope_input_contract

entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true

relationships: []

contracts:
  WorkflowInput:
    fields:
      items:
        type: json
{source_metadata_field}
  ProviderInput:
    fields:
      payload:
        type: json
  ProviderOutput:
    fields:
      items:
        type: json

providers:
  echo_json:
    kind: function
    contract_in: ProviderInput
    contract_out: ProviderOutput
    ref: tests.support.workflow_test_providers.echo_json_payload
    version: "1.0.0"
    deterministic: true
    runtime: python

workflows:
  inspect_input:
    contract_in: WorkflowInput
    steps:
      - id: echo
        provider: echo_json
        input:
          payload: $input
        as: echo
    returns: echo
"""


def _envelope_pipe_config_yaml() -> str:
    return """\
version: "1.0"
name: envelope_pipe

entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true
      label:
        type: string

relationships: []

named_queries:
  list_things:
    mode: collection
    returns: Thing
    result_shape: entity

contracts:
  EmptyInput:
    fields: {}
  ItemsInput:
    fields:
      items:
        type: json
  ProviderInput:
    fields:
      payload:
        type: json
  ProviderOutput:
    fields:
      items:
        type: json

providers:
  echo_json:
    kind: function
    contract_in: ProviderInput
    contract_out: ProviderOutput
    ref: tests.support.workflow_test_providers.echo_json_payload
    version: "1.0.0"
    deterministic: true
    runtime: python

workflows:
  analyze_things:
    contract_in: EmptyInput
    steps:
      - id: read
        query: list_things
        params: {}
        as: read
      - id: echoed
        provider: echo_json
        input:
          payload:
            items: $steps.read.results
        as: echoed
    returns: echoed
  apply_things:
    type: canonical
    contract_in: ItemsInput
    steps:
      - id: entities
        make_entities:
          entity_type: Thing
          items: $input.items
          entity_id: $item.entity_id
          properties:
            thing_id: $item.entity_id
            label: $item.properties.label
        as: entities
      - id: apply_entities
        apply_entities:
          entities_from: entities
        as: applied
    returns: applied
"""


def test_make_relationship_set_attaches_typed_evidence_refs(
    proposal_workflow_instance: CruxibleInstance,
) -> None:
    spec = MakeRelationshipsSpec(
        relationship_type="recommended_for",
        items=[
            {
                "product_sku": "SKU-123",
                "source_record_id": "row-1",
            }
        ],
        from_type="Campaign",
        from_id="CMP-1",
        to_type="Product",
        to_id="$item.product_sku",
        properties={"reason": "catalog"},
        evidence=CandidateEvidenceSpec(
            refs=[
                {
                    "source": "catalog",
                    "source_record_id": "$item.source_record_id",
                    "label": "catalog match",
                }
            ],
            rationale="catalog recommendation",
        ),
    )

    result = make_relationship_set(
        proposal_workflow_instance.load_config(),
        "relationships",
        spec,
        {},
        {},
    )

    evidence = result.relationships[0].metadata.evidence
    assert evidence is not None
    assert evidence.evidence_refs == [
        EvidenceRef(
            source="catalog",
            source_record_id="row-1",
            label="catalog match",
        )
    ]
    assert evidence.model_dump(mode="json")["evidence_refs"] == [
        {
            "source": "catalog",
            "source_record_id": "row-1",
            "label": "catalog match",
        }
    ]


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

    def test_execute_workflow_applies_trace_payload_retention_policy(
        self,
        workflow_instance: CruxibleInstance,
    ) -> None:
        config = workflow_instance.load_config()
        config.runtime.trace_payloads = "metadata"
        write_lock(
            build_lock(config, workflow_instance.get_config_path().parent),
            get_lock_path(workflow_instance),
        )

        result = execute_workflow(
            workflow_instance,
            config,
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )
        trace = result.traces[0]

        assert trace.input_payload_metadata is not None
        assert trace.input_payload_metadata.retention == "metadata"
        assert trace.input_payload_metadata.stored_inline is False
        assert "_cruxible_payload_omitted" in trace.input_payload
        store = workflow_instance.get_receipt_store()
        try:
            loaded = store.get_trace(trace.trace_id)
        finally:
            store.close()
        assert loaded is not None
        assert loaded.input_payload == trace.input_payload
        assert loaded.input_payload_metadata is not None
        assert loaded.input_payload_metadata.retention == "metadata"

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
            mode="traversal",
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
            mode="traversal",
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
        assert USER_INDEX_FIELD not in json.dumps(result.output)
        assert USER_INDEX_FIELD not in json.dumps(result.step_outputs)
        assert USER_INDEX_FIELD not in json.dumps(result.receipt.results)

    def test_query_step_passes_relationship_rows_through(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["recommendation_edges"] = NamedQuerySchema(
            mode="traversal",
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

    def test_query_step_can_override_relationship_state_when_allowed(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.contracts["CampaignInput"].fields["relationship_state"] = PropertySchema(
            type="string",
            optional=True,
        )
        config.named_queries["reviewable_recommendation_paths"] = NamedQuerySchema(
            mode="traversal",
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
            allow_relationship_state_override=True,
        )
        config.workflows["query_reviewable_recommendations"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="paths",
                    query="reviewable_recommendation_paths",
                    params={"campaign_id": "$input.campaign_id"},
                    relationship_state="$input.relationship_state",
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
                metadata=_review_metadata("approved"),
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="recommended_for",
                from_type="Campaign",
                from_id="CMP-1",
                to_type="Product",
                to_id="SKU-456",
                properties={"reason": "candidate"},
                metadata=_review_metadata("pending"),
            )
        )
        proposal_workflow_instance.save_graph(graph)
        write_lock_for_instance(proposal_workflow_instance)

        pending = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "query_reviewable_recommendations",
            {"campaign_id": "CMP-1", "relationship_state": "pending"},
        )
        accepted = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "query_reviewable_recommendations",
            {"campaign_id": "CMP-1", "relationship_state": "accepted"},
        )
        reviewable = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "query_reviewable_recommendations",
            {"campaign_id": "CMP-1", "relationship_state": "reviewable"},
        )

        assert pending.output["relationship_state"] == "pending"
        assert [row["result"]["entity_id"] for row in pending.output["results"]] == ["SKU-456"]
        assert accepted.output["relationship_state"] == "accepted"
        assert [row["result"]["entity_id"] for row in accepted.output["results"]] == ["SKU-123"]
        assert reviewable.output["relationship_state"] == "reviewable"
        assert [row["result"]["entity_id"] for row in reviewable.output["results"]] == [
            "SKU-123",
            "SKU-456",
        ]

    def test_query_step_rejects_unauthorized_relationship_state_override(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["recommendation_paths"] = NamedQuerySchema(
            mode="traversal",
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
        config.workflows["query_pending_recommendations"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="paths",
                    query="recommendation_paths",
                    params={"campaign_id": "$input.campaign_id"},
                    relationship_state="pending",
                    **{"as": "paths"},
                )
            ],
            returns="paths",
        )
        proposal_workflow_instance.save_config(config)
        write_lock_for_instance(proposal_workflow_instance)

        with pytest.raises(QueryExecutionError, match="visibility state override"):
            execute_workflow(
                proposal_workflow_instance,
                proposal_workflow_instance.load_config(),
                "query_pending_recommendations",
                {"campaign_id": "CMP-1"},
            )

    def test_query_step_includes_projected_source_only_when_requested(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["projected_recommendations"] = NamedQuerySchema(
            mode="traversal",
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
            select={"sku": "$result.entity_id"},
        )
        config.workflows["query_projected_recommendations"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="default_rows",
                    query="projected_recommendations",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "default_rows"},
                ),
                WorkflowStepSchema(
                    id="source_rows",
                    query="projected_recommendations",
                    params={"campaign_id": "$input.campaign_id"},
                    include_source=True,
                    **{"as": "source_rows"},
                ),
            ],
            returns="source_rows",
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
            "query_projected_recommendations",
            {"campaign_id": "CMP-1"},
        )

        default_row = result.step_outputs["default_rows"]["results"][0]
        source_row = result.output["results"][0]
        assert default_row == {"values": {"sku": "SKU-123"}}
        assert source_row["values"] == {"sku": "SKU-123"}
        assert source_row["source"]["result"]["entity_id"] == "SKU-123"
        assert source_row["source"]["path"][0]["relationship_type"] == "recommended_for"
        assert USER_INDEX_FIELD not in json.dumps(result.output)
        assert USER_INDEX_FIELD not in json.dumps(result.step_outputs)
        assert USER_INDEX_FIELD not in json.dumps(result.receipt.results)

    def test_query_step_includes_query_limit_metadata(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["limited_recommendation_paths"] = NamedQuerySchema(
            mode="traversal",
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
        assert result.output["returned_results"] == 1
        assert result.output["limit"] == 1
        assert result.output["truncated"] is True
        assert result.output["limit_truncated"] is True
        assert result.output["path_truncated"] is False
        assert result.output["truncation_reasons"] == ["limit"]
        assert result.output["policy_summary"] == {}
        assert set(result.output["results"][0]) == {"values"}
        assert result.output["results"][0]["values"]["sku"] == "SKU-123"

    def test_shape_items_preserves_limited_query_read_metadata(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["limited_recommendation_paths"] = NamedQuerySchema(
            mode="traversal",
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
            limit=1,
        )
        config.workflows["shape_limited_recommendations"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="paths",
                    query="limited_recommendation_paths",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "paths"},
                ),
                WorkflowStepSchema(
                    id="shaped",
                    shape_items={
                        "items": "$steps.paths.results",
                        "fields": {
                            "sku": "$item.result.entity_id",
                            USER_INDEX_FIELD: "user-visible-value",
                        },
                    },
                    **{"as": "shaped"},
                ),
            ],
            returns="shaped",
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
            "shape_limited_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert result.output["items"] == [
            {"sku": "SKU-123", USER_INDEX_FIELD: "user-visible-value"}
        ]
        assert result.step_outputs["shaped"]["items"] == result.output["items"]
        assert result.receipt.results[0]["output"]["items"] == result.output["items"]
        metadata = result.output["source_metadata"]
        assert metadata["source_step"] == "paths"
        assert metadata["source_ref"] == "$steps.paths.results"
        assert metadata["input_ref"] == "$steps.paths.results"
        assert metadata["total_results"] == 2
        assert metadata["returned_results"] == 1
        assert metadata["limit"] == 1
        assert metadata["truncated"] is True
        assert metadata["limit_truncated"] is True
        assert metadata["path_truncated"] is False
        assert metadata["truncation_reasons"] == ["limit"]
        assert metadata["result_shape"] == "path"
        assert metadata["dedupe"] == "path"
        assert metadata["relationship_state"] == "live"
        read_metadata = result.read_metadata
        assert read_metadata["any_read_truncated"] is True
        assert read_metadata["any_query_truncated"] is True
        assert read_metadata["truncation_reasons"] == ["limit"]
        assert read_metadata["query_receipt_ids"] == result.query_receipt_ids
        assert [step["step_id"] for step in read_metadata["read_steps"]] == [
            "paths",
            "shaped",
        ]
        assert read_metadata["step_counts"]["paths"] == {
            "total_results": 2,
            "returned_results": 1,
        }
        assert read_metadata["step_counts"]["shaped"] == {
            "input_count": 1,
            "output_count": 1,
            "dropped_count": 0,
        }
        receipt_metadata = result.receipt.nodes[0].detail["read_metadata"]
        assert receipt_metadata == read_metadata
        query_step = read_metadata["read_steps"][0]
        assert query_step["step_id"] == "paths"
        assert query_step["metadata"]["receipt_id"] == result.query_receipt_ids[0]
        assert query_step["metadata"]["truncated"] is True
        assert query_step["metadata"]["limit_truncated"] is True
        assert query_step["metadata"]["path_truncated"] is False
        assert query_step["metadata"]["truncation_reasons"] == ["limit"]
        assert query_step["metadata"]["returned_results"] == 1
        assert query_step["metadata"]["total_results"] == 2

    def test_assert_can_guard_truncated_query_metadata(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["limited_recommendation_paths"] = NamedQuerySchema(
            mode="traversal",
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
            limit=1,
        )
        config.workflows["guard_complete_recommendations"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="paths",
                    query="limited_recommendation_paths",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "paths"},
                ),
                WorkflowStepSchema(
                    id="require_complete_context",
                    **{
                        "assert": {
                            "left": "$steps.paths.truncated",
                            "op": "eq",
                            "right": False,
                            "message": "Recommendation context was truncated",
                        }
                    },
                ),
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

        with pytest.raises(QueryExecutionError, match="Recommendation context was truncated"):
            execute_workflow(
                proposal_workflow_instance,
                proposal_workflow_instance.load_config(),
                "guard_complete_recommendations",
                {"campaign_id": "CMP-1"},
            )

        store = proposal_workflow_instance.get_receipt_store()
        try:
            receipts = store.list_receipts(operation_type="workflow")
            receipt = store.get_receipt(receipts[0]["receipt_id"])
        finally:
            store.close()
        assert receipt is not None
        read_metadata = receipt.nodes[0].detail["read_metadata"]
        assert read_metadata["any_read_truncated"] is True
        assert read_metadata["any_query_truncated"] is True
        assert read_metadata["truncation_reasons"] == ["limit"]
        assert len(read_metadata["query_receipt_ids"]) == 1
        assert read_metadata["read_steps"][0]["step_id"] == "paths"
        assert (
            read_metadata["read_steps"][0]["metadata"]["receipt_id"]
            == (read_metadata["query_receipt_ids"][0])
        )
        assert read_metadata["read_steps"][0]["metadata"]["truncated"] is True
        assert read_metadata["read_steps"][0]["metadata"]["limit_truncated"] is True
        assert read_metadata["read_steps"][0]["metadata"]["path_truncated"] is False
        assert read_metadata["read_steps"][0]["metadata"]["returned_results"] == 1
        assert read_metadata["read_steps"][0]["metadata"]["total_results"] == 2

    def test_assert_can_guard_transform_source_truncation(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["limited_recommendation_paths"] = NamedQuerySchema(
            mode="traversal",
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
            limit=1,
        )
        config.workflows["guard_shaped_recommendations"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="paths",
                    query="limited_recommendation_paths",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "paths"},
                ),
                WorkflowStepSchema(
                    id="shaped",
                    shape_items={
                        "items": "$steps.paths.results",
                        "fields": {"sku": "$item.result.entity_id"},
                    },
                    **{"as": "shaped"},
                ),
                WorkflowStepSchema(
                    id="require_complete_shaped_context",
                    **{
                        "assert": {
                            "left": "$steps.shaped.source_metadata.truncated",
                            "op": "eq",
                            "right": False,
                            "message": "Shaped recommendation context was truncated",
                        }
                    },
                ),
            ],
            returns="shaped",
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

        with pytest.raises(
            QueryExecutionError,
            match="Shaped recommendation context was truncated",
        ):
            execute_workflow(
                proposal_workflow_instance,
                proposal_workflow_instance.load_config(),
                "guard_shaped_recommendations",
                {"campaign_id": "CMP-1"},
            )

        store = proposal_workflow_instance.get_receipt_store()
        try:
            receipts = store.list_receipts(operation_type="workflow")
            receipt = store.get_receipt(receipts[0]["receipt_id"])
        finally:
            store.close()
        assert receipt is not None
        read_metadata = receipt.nodes[0].detail["read_metadata"]
        assert read_metadata["any_read_truncated"] is True
        assert [step["step_id"] for step in read_metadata["read_steps"]] == [
            "paths",
            "shaped",
        ]
        shaped_step = read_metadata["read_steps"][1]
        assert shaped_step["source_step"] == "paths"
        assert shaped_step["metadata"]["truncated"] is True
        assert shaped_step["metadata"]["truncation_reasons"] == ["limit"]

    def test_execute_workflow_inline_entity_query_step_returns_results(
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
                query={
                    "mode": "collection",
                    "result_shape": "entity",
                    "returns": "Product",
                    "where": {"result.properties.category": {"eq": "$input.category"}},
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

        assert result.step_outputs["products"]["total_results"] == 1
        assert len(result.step_outputs["products"]["results"]) == 1
        products_step = next(
            node
            for node in result.receipt.nodes
            if node.node_type == "plan_step" and node.detail.get("step_id") == "products"
        )
        assert products_step.detail["inline_query"]["returns"] == "Product"
        assert products_step.detail["returned_results"] == 1

    def test_execute_workflow_limited_inline_entity_query_reports_read_metadata(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        graph = workflow_instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="Product",
                entity_id="SKU-456",
                properties={"sku": "SKU-456", "category": "soda", "base_margin": 0.3},
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
                query={
                    "mode": "collection",
                    "result_shape": "entity",
                    "returns": "Product",
                    "where": {"result.properties.category": {"eq": "$input.category"}},
                    "limit": 1,
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

        products = result.step_outputs["products"]
        assert products["total_results"] == 2
        assert products["total_results"] == 2
        assert products["returned_results"] == 1
        assert products["limit"] == 1
        assert products["truncated"] is True
        assert products["limit_truncated"] is True
        assert products["path_truncated"] is False
        assert products["truncation_reasons"] == ["limit"]

    def test_assert_can_guard_complete_query_metadata_and_count(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["guard_product_reads"] = WorkflowSchema(
            contract_in="PromoInput",
            steps=[
                WorkflowStepSchema(
                    id="products",
                    query={
                        "mode": "collection",
                        "result_shape": "entity",
                        "returns": "Product",
                        "where": {"result.properties.sku": {"eq": "$input.sku"}},
                        "limit": 10,
                    },
                    **{"as": "products"},
                ),
                WorkflowStepSchema(
                    id="require_complete_products",
                    **{
                        "assert": {
                            "left": "$steps.products.truncated",
                            "op": "eq",
                            "right": False,
                            "message": "Product read was truncated",
                        }
                    },
                ),
                WorkflowStepSchema(
                    id="require_some_products",
                    **{
                        "assert": {
                            "left": "$steps.products.returned_results",
                            "op": "gt",
                            "right": 0,
                            "message": "No products found",
                        }
                    },
                ),
            ],
            returns="products",
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        result = execute_workflow(
            workflow_instance,
            workflow_instance.load_config(),
            "guard_product_reads",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert result.output["returned_results"] == 1
        assert result.output["truncated"] is False
        assert result.read_metadata["any_read_truncated"] is False

    def test_assert_not_truncated_passes_for_complete_read_output(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["guard_complete_products"] = WorkflowSchema(
            contract_in="PromoInput",
            steps=[
                WorkflowStepSchema(
                    id="products",
                    query={
                        "mode": "collection",
                        "result_shape": "entity",
                        "returns": "Product",
                        "where": {"result.properties.sku": {"eq": "$input.sku"}},
                        "limit": 10,
                    },
                    **{"as": "products"},
                ),
                WorkflowStepSchema(
                    id="require_complete_products",
                    assert_not_truncated={"step": "products"},
                ),
            ],
            returns="products",
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        result = execute_workflow(
            workflow_instance,
            workflow_instance.load_config(),
            "guard_complete_products",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert result.output["truncated"] is False
        guard_step = next(
            node
            for node in result.receipt.nodes
            if (
                node.node_type == "plan_step"
                and node.detail.get("step_id") == "require_complete_products"
            )
        )
        assert guard_step.detail["guard"] == "assert_not_truncated"
        assert guard_step.detail["step"] == "products"
        assert guard_step.detail["flags"] == {
            "truncated": False,
            "limit_truncated": False,
            "path_truncated": False,
        }

    def test_assert_not_truncated_fails_for_truncated_query_output(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["limited_recommendation_paths"] = NamedQuerySchema(
            mode="traversal",
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
            limit=1,
        )
        config.workflows["guard_complete_query"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="paths",
                    query="limited_recommendation_paths",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "paths"},
                ),
                WorkflowStepSchema(
                    id="require_complete_paths",
                    assert_not_truncated={"step": "paths"},
                ),
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

        with pytest.raises(
            QueryExecutionError,
            match="assert_not_truncated step 'require_complete_paths' failed for 'paths'",
        ):
            execute_workflow(
                proposal_workflow_instance,
                proposal_workflow_instance.load_config(),
                "guard_complete_query",
                {"campaign_id": "CMP-1"},
            )

        store = proposal_workflow_instance.get_receipt_store()
        try:
            receipts = store.list_receipts(operation_type="workflow")
            receipt = store.get_receipt(receipts[0]["receipt_id"])
        finally:
            store.close()
        assert receipt is not None
        guard_step = next(
            node
            for node in receipt.nodes
            if (
                node.node_type == "plan_step"
                and node.detail.get("step_id") == "require_complete_paths"
            )
        )
        assert guard_step.detail["flags"]["truncated"] is True
        assert guard_step.detail["flags"]["limit_truncated"] is True
        assert guard_step.detail["truncation_reasons"] == ["limit"]
        assert receipt.nodes[0].detail["read_metadata"]["any_query_truncated"] is True

    def test_assert_not_truncated_fails_without_read_metadata(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["guard_provider_metadata"] = WorkflowSchema(
            contract_in="PromoInput",
            steps=[
                WorkflowStepSchema(
                    id="context",
                    query="get_promo_context",
                    params={"sku": "$input.sku"},
                    **{"as": "context"},
                ),
                WorkflowStepSchema(
                    id="lift",
                    provider="lift_predictor",
                    input={
                        "sku": "$steps.context.results[0].entity_id",
                        "category": "$steps.context.results[0].properties.category",
                        "start_date": "$input.start_date",
                        "end_date": "$input.end_date",
                    },
                    **{"as": "lift"},
                ),
                WorkflowStepSchema(
                    id="require_complete_lift",
                    assert_not_truncated={"step": "lift"},
                ),
            ],
            returns="lift",
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        with pytest.raises(QueryExecutionError, match="no read metadata found"):
            execute_workflow(
                workflow_instance,
                workflow_instance.load_config(),
                "guard_provider_metadata",
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
        guard_step = next(
            node
            for node in receipt.nodes
            if (
                node.node_type == "plan_step"
                and node.detail.get("step_id") == "require_complete_lift"
            )
        )
        assert guard_step.detail["guard"] == "assert_not_truncated"
        assert guard_step.detail["metadata_found"] is False

    def test_assert_not_truncated_detects_transform_source_truncation(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.named_queries["limited_recommendation_paths"] = NamedQuerySchema(
            mode="traversal",
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
            limit=1,
        )
        config.workflows["guard_shaped_query"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="paths",
                    query="limited_recommendation_paths",
                    params={"campaign_id": "$input.campaign_id"},
                    **{"as": "paths"},
                ),
                WorkflowStepSchema(
                    id="shaped",
                    shape_items={
                        "items": "$steps.paths.results",
                        "fields": {"sku": "$item.result.entity_id"},
                    },
                    **{"as": "shaped"},
                ),
                WorkflowStepSchema(
                    id="require_complete_shaped",
                    assert_not_truncated={"step": "shaped"},
                ),
            ],
            returns="shaped",
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

        with pytest.raises(
            QueryExecutionError,
            match="assert_not_truncated step 'require_complete_shaped' failed for 'shaped'",
        ):
            execute_workflow(
                proposal_workflow_instance,
                proposal_workflow_instance.load_config(),
                "guard_shaped_query",
                {"campaign_id": "CMP-1"},
            )

    def test_assert_count_supports_read_and_collection_selectors(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["guard_counts"] = WorkflowSchema(
            contract_in="PromoInput",
            steps=[
                WorkflowStepSchema(
                    id="context",
                    query="get_promo_context",
                    params={"sku": "$input.sku"},
                    **{"as": "context"},
                ),
                WorkflowStepSchema(
                    id="products",
                    query={
                        "mode": "collection",
                        "result_shape": "entity",
                        "returns": "Product",
                        "where": {"result.properties.sku": {"eq": "$input.sku"}},
                    },
                    **{"as": "products"},
                ),
                WorkflowStepSchema(
                    id="require_returned_context",
                    assert_count={
                        "step": "context",
                        "count": "returned_results",
                        "op": "gt",
                        "value": 0,
                    },
                ),
                WorkflowStepSchema(
                    id="require_total_context",
                    assert_count={
                        "step": "context",
                        "count": "total_results",
                        "op": "eq",
                        "value": 1,
                    },
                ),
                WorkflowStepSchema(
                    id="require_context_results",
                    assert_count={
                        "step": "context",
                        "count": "results",
                        "op": "eq",
                        "value": 1,
                    },
                ),
                WorkflowStepSchema(
                    id="require_product_results",
                    assert_count={
                        "step": "products",
                        "count": "results",
                        "op": "eq",
                        "value": 1,
                    },
                ),
            ],
            returns="products",
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        result = execute_workflow(
            workflow_instance,
            workflow_instance.load_config(),
            "guard_counts",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        count_step = next(
            node
            for node in result.receipt.nodes
            if (
                node.node_type == "plan_step"
                and node.detail.get("step_id") == "require_context_results"
            )
        )
        assert count_step.detail["guard"] == "assert_count"
        assert count_step.detail["actual"] == 1
        assert count_step.detail["expected"] == 1

    def test_assert_count_failure_uses_clear_message(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["guard_count_failure"] = WorkflowSchema(
            contract_in="PromoInput",
            steps=[
                WorkflowStepSchema(
                    id="products",
                    query={
                        "mode": "collection",
                        "result_shape": "entity",
                        "returns": "Product",
                        "where": {"result.properties.sku": {"eq": "$input.sku"}},
                    },
                    **{"as": "products"},
                ),
                WorkflowStepSchema(
                    id="require_many_products",
                    assert_count={
                        "step": "products",
                        "count": "results",
                        "op": "gte",
                        "value": 2,
                        "message": "expected at least two products",
                    },
                ),
            ],
            returns="products",
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        with pytest.raises(QueryExecutionError, match="expected at least two products"):
            execute_workflow(
                workflow_instance,
                workflow_instance.load_config(),
                "guard_count_failure",
                {
                    "sku": "SKU-123",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-07",
                },
            )

    def test_assert_exists_accepts_present_falsey_values(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["guard_exists_falsey"] = WorkflowSchema(
            contract_in="PromoInput",
            steps=[
                WorkflowStepSchema(
                    id="products",
                    query={
                        "mode": "collection",
                        "result_shape": "entity",
                        "returns": "Product",
                        "where": {"result.properties.sku": {"eq": "$input.sku"}},
                    },
                    **{"as": "products"},
                ),
                WorkflowStepSchema(
                    id="shaped",
                    shape_items={
                        "items": "$steps.products.results",
                        "fields": {
                            "false_value": False,
                            "zero_value": 0,
                            "empty_list": [],
                            "empty_object": {},
                        },
                    },
                    **{"as": "shaped"},
                ),
                WorkflowStepSchema(
                    id="require_false",
                    assert_exists={"ref": "$steps.shaped.items[0].false_value"},
                ),
                WorkflowStepSchema(
                    id="require_zero",
                    assert_exists={"ref": "$steps.shaped.items[0].zero_value"},
                ),
                WorkflowStepSchema(
                    id="require_empty_list",
                    assert_exists={"ref": "$steps.shaped.items[0].empty_list"},
                ),
                WorkflowStepSchema(
                    id="require_empty_object",
                    assert_exists={"ref": "$steps.shaped.items[0].empty_object"},
                ),
            ],
            returns="shaped",
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        result = execute_workflow(
            workflow_instance,
            workflow_instance.load_config(),
            "guard_exists_falsey",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert result.output["items"][0] == {
            "false_value": False,
            "zero_value": 0,
            "empty_list": [],
            "empty_object": {},
        }

    @pytest.mark.parametrize(
        "ref",
        [
            "$steps.shaped.items[0].missing",
            "$steps.shaped.items[4].entity_id",
            "$steps.shaped.items[0].null_value",
            "$steps.shaped.items[0].empty_string",
        ],
    )
    def test_assert_exists_fails_with_configured_message(
        self,
        workflow_instance: CruxibleInstance,
        ref: str,
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["guard_exists_missing"] = WorkflowSchema(
            contract_in="PromoInput",
            steps=[
                WorkflowStepSchema(
                    id="products",
                    query={
                        "mode": "collection",
                        "result_shape": "entity",
                        "returns": "Product",
                        "where": {"result.properties.sku": {"eq": "$input.sku"}},
                    },
                    **{"as": "products"},
                ),
                WorkflowStepSchema(
                    id="shaped",
                    shape_items={
                        "items": "$steps.products.results",
                        "fields": {
                            "entity_id": "$item.entity_id",
                            "null_value": None,
                            "empty_string": "",
                        },
                    },
                    **{"as": "shaped"},
                ),
                WorkflowStepSchema(
                    id="require_context_value",
                    assert_exists={
                        "ref": ref,
                        "message": "required context value missing",
                    },
                ),
            ],
            returns="shaped",
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        with pytest.raises(QueryExecutionError, match="required context value missing"):
            execute_workflow(
                workflow_instance,
                workflow_instance.load_config(),
                "guard_exists_missing",
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
        guard_step = next(
            node
            for node in receipt.nodes
            if (
                node.node_type == "plan_step"
                and node.detail.get("step_id") == "require_context_value"
            )
        )
        assert guard_step.detail["guard"] == "assert_exists"
        assert guard_step.detail["present"] is False
        assert guard_step.detail["message"] == "required context value missing"

    def test_execute_workflow_inline_entity_query_matches_service_list(
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
                query={
                    "mode": "collection",
                    "result_shape": "entity",
                    "returns": "Product",
                    "where": {"result.properties.sku": {"eq": "$input.sku"}},
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

        assert result.step_outputs["products"]["total_results"] == listed.total
        assert result.step_outputs["products"]["results"][0]["properties"]["sku"] == "SKU-123"
        assert result.step_outputs["products"]["results"] == [
            item.model_dump(mode="python") for item in listed.items
        ]

    def test_execute_workflow_inline_relationship_query_step_returns_results(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.contracts["CampaignInput"].fields["status"] = PropertySchema(
            type="string",
            optional=True,
        )
        # `status` must be configured on the relationship for the edge `where` to be
        # accepted under fail-closed validation (parity with service_list).
        recommended_for = config.get_relationship("recommended_for")
        assert recommended_for is not None
        recommended_for.properties["status"] = PropertySchema(type="string", optional=True)
        config.workflows["propose_campaign_recommendations"].steps.insert(
            1,
            WorkflowStepSchema(
                id="existing_links",
                query={
                    "mode": "collection",
                    "result_shape": "relationship",
                    "returns": "recommended_for",
                    "where": {"edge.properties.status": {"eq": "$input.status"}},
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

        assert result.step_outputs["existing_links"]["total_results"] == 0
        assert result.step_outputs["existing_links"]["results"] == []
        links_step = next(
            node
            for node in result.receipt.nodes
            if node.node_type == "plan_step" and node.detail.get("step_id") == "existing_links"
        )
        assert links_step.detail["inline_query"]["returns"] == "recommended_for"
        assert links_step.detail["returned_results"] == 0

    def test_execute_workflow_limited_inline_relationship_query_reports_read_metadata(
        self,
        proposal_workflow_instance: CruxibleInstance,
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.workflows["list_campaign_links"] = WorkflowSchema(
            contract_in="CampaignInput",
            steps=[
                WorkflowStepSchema(
                    id="existing_links",
                    query={
                        "mode": "collection",
                        "result_shape": "relationship",
                        "returns": "recommended_for",
                        "limit": 1,
                    },
                    **{"as": "existing_links"},
                )
            ],
            returns="existing_links",
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
                    properties={"status": "active"},
                )
            )
        proposal_workflow_instance.save_graph(graph)
        write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "list_campaign_links",
            {"campaign_id": "CMP-1"},
        )

        links = result.step_outputs["existing_links"]
        assert links["total_results"] == 2
        assert links["total_results"] == 2
        assert links["returned_results"] == 1
        assert links["limit"] == 1
        assert links["truncated"] is True
        assert links["limit_truncated"] is True
        assert links["path_truncated"] is False
        assert links["truncation_reasons"] == ["limit"]

    def test_execute_workflow_inline_relationship_query_matches_service_list(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        config.contracts["CampaignInput"].fields["status"] = PropertySchema(
            type="string",
            optional=True,
        )
        # `status` must be a CONFIGURED property on `recommended_for` for both read
        # surfaces to accept the filter under fail-closed validation. The parity
        # contract is that, given a valid filter, the inline relationship query and
        # service_list("edges") return identical results.
        recommended_for = config.get_relationship("recommended_for")
        assert recommended_for is not None
        recommended_for.properties["status"] = PropertySchema(type="string", optional=True)
        config.workflows["propose_campaign_recommendations"].steps.insert(
            1,
            WorkflowStepSchema(
                id="existing_links",
                query={
                    "mode": "collection",
                    "result_shape": "relationship",
                    "returns": "recommended_for",
                    "where": {"edge.properties.status": {"eq": "$input.status"}},
                    "limit": 10,
                },
                **{"as": "existing_links"},
            ),
        )
        proposal_workflow_instance.save_config(config)

        graph = proposal_workflow_instance.load_graph()
        for sku, status in (("SKU-123", "human_approved"), ("SKU-456", "pending")):
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="recommended_for",
                    from_type="Campaign",
                    from_id="CMP-1",
                    to_type="Product",
                    to_id=sku,
                    properties={"status": status},
                )
            )
        proposal_workflow_instance.save_graph(graph)
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

        # Parity contract: a configured edge filter selects the SAME edges on both
        # surfaces. The two surfaces serialize rows differently on purpose (the
        # query row carries traversal context: entry/from_entity/to_entity/includes
        # and richer metadata) -- that legitimate difference is preserved, so parity
        # is asserted on the matched-edge identity + edge properties, not the raw
        # row dicts.
        def edge_identity(row: dict[str, object]) -> tuple[object, ...]:
            return (
                row["relationship_type"],
                row["from_type"],
                row["from_id"],
                row["to_type"],
                row["to_id"],
                row["edge_key"],
                tuple(sorted(row["properties"].items())),  # type: ignore[union-attr]
            )

        query_rows = result.step_outputs["existing_links"]["results"]
        assert listed.total == 1
        assert result.step_outputs["existing_links"]["total_results"] == listed.total
        assert [edge_identity(row) for row in query_rows] == [
            edge_identity(item) for item in listed.items
        ]

    def test_execute_workflow_inline_relationship_query_rejects_unconfigured_edge_property(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        # Fail-closed parity: an unconfigured edge.properties.<X> in a `where` must be
        # rejected the same way by BOTH the inline relationship query step and
        # service_list("edges"). `priority` is not a configured property of
        # `recommended_for`, so both surfaces raise ConfigError with the same shape.
        config = proposal_workflow_instance.load_config()
        config.workflows["propose_campaign_recommendations"].steps.insert(
            1,
            WorkflowStepSchema(
                id="existing_links",
                query={
                    "mode": "collection",
                    "result_shape": "relationship",
                    "returns": "recommended_for",
                    "where": {"edge.properties.priority": {"eq": "high"}},
                    "limit": 10,
                },
                **{"as": "existing_links"},
            ),
        )
        proposal_workflow_instance.save_config(config)
        write_lock_for_instance(proposal_workflow_instance)

        expected_message = (
            "Unknown where field for relationship type 'recommended_for': priority. "
            "Known fields: reason"
        )

        with pytest.raises(ConfigError, match="Unknown where field") as inline_exc:
            execute_workflow(
                proposal_workflow_instance,
                proposal_workflow_instance.load_config(),
                "propose_campaign_recommendations",
                {"campaign_id": "CMP-1"},
            )

        with pytest.raises(ConfigError, match="Unknown where field") as list_exc:
            service_list(
                proposal_workflow_instance,
                "edges",
                relationship_type="recommended_for",
                property_filter={"priority": "high"},
                limit=10,
            )

        # Same error type AND same message shape on both surfaces.
        assert type(inline_exc.value) is type(list_exc.value)
        assert str(inline_exc.value) == expected_message
        assert str(list_exc.value) == expected_message

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

    def test_execute_workflow_preserves_provider_error_subtype(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        """A typed Cruxible error raised by a provider must not be collapsed.

        The provider step previously re-raised every failure as a generic
        QueryExecutionError, hiding the original subtype. Typed CoreError
        subclasses (e.g. DataValidationError) are now preserved so callers can
        still branch on the specific failure category.
        """
        config = workflow_instance.load_config()
        config.providers[
            "margin_calculator"
        ].ref = "tests.support.workflow_test_providers.typed_error_provider"
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        with pytest.raises(DataValidationError, match="malformed upstream data"):
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

        # The receipt still records the failure with the preserved subtype.
        store = workflow_instance.get_receipt_store()
        try:
            receipts = store.list_receipts(operation_type="workflow")
            receipt = store.get_receipt(receipts[0]["receipt_id"])
        finally:
            store.close()
        assert receipt is not None
        assert receipt.committed is False
        assert receipt.nodes[0].detail["error_type"] == "DataValidationError"

    def test_execute_workflow_validates_matching_contract_out(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["evaluate_promo"].contract_out = "MarginResult"
        workflow_instance.save_config(config)
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

    def test_execute_workflow_rejects_contract_out_mismatch_before_success_receipt(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.contracts["AgentOutput"] = ContractSchema(
            fields={"decision_frame": PropertySchema(type="json")}
        )
        config.workflows["evaluate_promo"].contract_out = "AgentOutput"
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        with pytest.raises(QueryExecutionError, match="Workflow 'evaluate_promo' output"):
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
        assert receipt.results[0]["output"] is None
        assert (
            "Workflow 'evaluate_promo' output failed contract 'AgentOutput'"
            in (receipt.results[0]["error"])
        )
        assert "missing required field 'decision_frame'" in receipt.results[0]["error"]
        assert receipt.nodes[0].detail["error_type"] == "QueryExecutionError"

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

    def test_workflow_input_contract_strips_reserved_source_metadata(self, tmp_path: Path) -> None:
        instance = json_contract_instance(
            tmp_path,
            _envelope_input_contract_yaml(),
        )
        items = [{"thing_id": "thing-1"}]
        payload = {
            "items": items,
            SOURCE_METADATA_KEY: {"receipt_id": "query-1", "truncated": False},
        }

        result = execute_workflow(
            instance,
            instance.load_config(),
            "inspect_input",
            payload,
        )

        assert result.traces[0].input_payload["payload"] == {"items": items}
        assert result.receipt.parameters == {"items": items}
        assert SOURCE_METADATA_KEY in payload

    def test_workflow_input_contract_keeps_declared_source_metadata(self, tmp_path: Path) -> None:
        instance = json_contract_instance(
            tmp_path,
            _envelope_input_contract_yaml(declare_source_metadata=True),
        )
        metadata = {"receipt_id": "query-1", "truncated": False}
        items = [{"thing_id": "thing-1"}]

        result = execute_workflow(
            instance,
            instance.load_config(),
            "inspect_input",
            {"items": items, SOURCE_METADATA_KEY: metadata},
        )

        assert result.traces[0].input_payload["payload"] == {
            "items": items,
            SOURCE_METADATA_KEY: metadata,
        }
        assert result.receipt.parameters == {"items": items, SOURCE_METADATA_KEY: metadata}

    def test_workflow_input_contract_still_rejects_arbitrary_extra_key(
        self, tmp_path: Path
    ) -> None:
        instance = json_contract_instance(
            tmp_path,
            _envelope_input_contract_yaml(),
        )

        with pytest.raises(ConfigError, match="unexpected field 'bogus'"):
            execute_workflow(
                instance,
                instance.load_config(),
                "inspect_input",
                {"items": [], "bogus": True},
            )

    def test_service_run_pipes_utility_source_metadata_into_canonical_input(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_envelope_pipe_config_yaml())
        instance = CruxibleInstance.init(tmp_path, "config.yaml")
        graph = instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="Thing",
                entity_id="thing-1",
                properties={"thing_id": "thing-1", "label": "Alpha"},
            )
        )
        instance.save_graph(graph)
        write_lock_for_instance(instance)

        utility_result = service_run(instance, "analyze_things", {})

        assert utility_result.output["items"][0]["entity_id"] == "thing-1"
        assert utility_result.output["items"][0]["properties"]["label"] == "Alpha"
        assert SOURCE_METADATA_KEY in utility_result.output

        canonical_result = service_run(
            instance,
            "apply_things",
            utility_result.output,
        )

        assert canonical_result.mode == "preview"
        assert canonical_result.workflow_type == "canonical"
        assert canonical_result.receipt.parameters == {"items": utility_result.output["items"]}
        assert canonical_result.output["noop_count"] == 1

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
        graph = canonical_workflow_instance.load_graph()
        assert graph.has_entity("Vendor", "vendor-acme")
        rel = graph.get_relationship(
            "Product",
            "product-acme-widget",
            "Vendor",
            "vendor-acme",
            "product_from_vendor",
        )
        assert rel is not None
        assert rel.metadata.provenance is not None
        assert rel.metadata.provenance.receipt_id == applied.receipt.receipt_id

    def test_canonical_apply_does_not_full_save_live_graph(
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

        def fail_full_save(_self: SQLiteGraphRepository, _graph) -> None:
            raise AssertionError("canonical apply should not replace the full live graph")

        with patch.object(SQLiteGraphRepository, "save_graph", fail_full_save):
            applied = execute_workflow(
                canonical_workflow_instance,
                canonical_workflow_instance.load_config(),
                "build_reference",
                {},
                mode="apply",
            )

        assert applied.apply_digest == preview.apply_digest
        assert applied.committed_snapshot_id is not None
        restarted = CruxibleInstance.load(canonical_workflow_instance.root)
        assert restarted.load_graph().has_entity("Vendor", "vendor-acme")

    def test_canonical_apply_validates_contract_out_before_commit(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        config.contracts["ReferenceOutput"] = ContractSchema(
            fields={"decision_frame": PropertySchema(type="json")}
        )
        config.workflows["build_reference"].contract_out = "ReferenceOutput"
        canonical_workflow_instance.save_config(config)
        write_lock_for_instance(canonical_workflow_instance)

        with pytest.raises(QueryExecutionError, match="Workflow 'build_reference' output"):
            execute_workflow(
                canonical_workflow_instance,
                canonical_workflow_instance.load_config(),
                "build_reference",
                {},
                mode="apply",
            )

        assert canonical_workflow_instance.load_graph().list_entities("Vendor") == []
        store = canonical_workflow_instance.get_receipt_store()
        try:
            receipts = store.list_receipts(operation_type="workflow")
            receipt = store.get_receipt(receipts[0]["receipt_id"])
        finally:
            store.close()
        assert receipt is not None
        assert receipt.committed is False
        assert receipt.workflow_mode == "apply"
        assert receipt.nodes[0].detail["error_type"] == "QueryExecutionError"

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
    digest: {bundle_sha256}

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
        config.artifacts["canonical_bundle"].digest = compute_directory_sha256(
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

    def test_apply_all_applies_entities_before_relationships(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        workflow = config.workflows["build_reference"]
        workflow.steps = [
            step
            for step in workflow.steps
            if step.apply_entities is None and step.apply_relationships is None
        ]
        workflow.steps.insert(
            -1,
            WorkflowStepSchema(
                id="apply_reference",
                apply_all={
                    "entities_from": ["vendors", "products", "vulnerabilities"],
                    "relationships_from": ["product_vendor", "vulnerability_product"],
                },
                **{"as": "apply_reference"},
            ),
        )
        workflow.returns = "apply_reference"
        canonical_workflow_instance.save_config(config)
        write_lock_for_instance(canonical_workflow_instance)

        result = execute_workflow(
            canonical_workflow_instance,
            canonical_workflow_instance.load_config(),
            "build_reference",
            {},
            mode="apply",
        )

        preview = result.apply_previews["apply_reference"]
        assert preview["entities_from"] == ["vendors", "products", "vulnerabilities"]
        assert preview["relationships_from"] == ["product_vendor", "vulnerability_product"]
        assert preview["entity_results"]["products"]["entity_type"] == "Product"
        assert preview["relationship_results"]["product_vendor"]["relationship_type"] == (
            "product_from_vendor"
        )
        assert preview["create_count"] > 0
        graph = canonical_workflow_instance.load_graph()
        assert graph.get_relationship(
            "Product",
            "product-acme-widget",
            "Vendor",
            "vendor-acme",
            "product_from_vendor",
        )
