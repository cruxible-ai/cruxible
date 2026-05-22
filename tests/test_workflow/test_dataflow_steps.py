"""Tests for built-in workflow dataflow steps."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.workflow_helpers import dataflow_instance

from cruxible_core.errors import QueryExecutionError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.workflow import execute_workflow


class TestWorkflowDataflowSteps:
    def test_execute_shape_items_casts_and_drops_missing_required(self, tmp_path: Path) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: shaped
              shape_items:
                items:
                  - asset_id: ASSET-1
                    priority: " 1 "
                    internet_exposed: "true"
                    tags_json: '["prod","api"]'
                    unused: ignored
                  - asset_id: ""
                    priority: "2"
                    internet_exposed: "false"
                    tags_json: '["staging"]'
                include_input: false
                rename:
                  tags_json: tags
                fields:
                  asset_id: $item.asset_id
                  priority: $item.priority
                  internet_exposed: $item.internet_exposed
                  source: seed
                casts:
                  priority: int
                  internet_exposed: bool
                  tags: json
                required: [asset_id]
                on_missing_required: drop
              as: shaped
            """,
            returns="shaped",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["input_count"] == 2
        assert result.output["output_count"] == 1
        assert result.output["dropped_count"] == 1
        assert result.output["items"] == [
            {
                "asset_id": "ASSET-1",
                "priority": 1,
                "internet_exposed": True,
                "tags": ["prod", "api"],
                "source": "seed",
            }
        ]

    def test_execute_shape_items_rejects_cast_failure_and_rename_collision(
        self, tmp_path: Path
    ) -> None:
        cast_instance = dataflow_instance(
            tmp_path / "cast",
            steps_yaml="""
            - id: shaped
              shape_items:
                items:
                  - id: A
                    priority: 42px
                fields:
                  id: $item.id
                  priority: $item.priority
                casts:
                  priority: int
              as: shaped
            """,
            returns="shaped",
        )
        with pytest.raises(QueryExecutionError, match="could not cast field 'priority'"):
            execute_workflow(cast_instance, cast_instance.load_config(), "dataflow", {})

        collision_instance = dataflow_instance(
            tmp_path / "collision",
            steps_yaml="""
            - id: shaped
              shape_items:
                items:
                  - tags: existing
                    tags_json: '["new"]'
                rename:
                  tags_json: tags
              as: shaped
            """,
            returns="shaped",
        )
        with pytest.raises(QueryExecutionError, match="rename collision"):
            execute_workflow(
                collision_instance,
                collision_instance.load_config(),
                "dataflow",
                {},
            )

    def test_execute_shape_items_empty_input_missing_cast_and_required_error(
        self, tmp_path: Path
    ) -> None:
        empty_instance = dataflow_instance(
            tmp_path / "empty",
            steps_yaml="""
            - id: shaped
              shape_items:
                items: []
                include_input: true
                casts:
                  missing: int
              as: shaped
            """,
            returns="shaped",
        )
        empty_result = execute_workflow(
            empty_instance,
            empty_instance.load_config(),
            "dataflow",
            {},
        )
        assert empty_result.output["items"] == []
        assert empty_result.output["input_count"] == 0

        null_instance = dataflow_instance(
            tmp_path / "null",
            steps_yaml="""
            - id: shaped
              shape_items:
                items:
                  - id: A
                    priority:
                fields:
                  id: $item.id
                  priority: $item.priority
                casts:
                  priority: int
                required: [id]
              as: shaped
            """,
            returns="shaped",
        )
        null_result = execute_workflow(
            null_instance,
            null_instance.load_config(),
            "dataflow",
            {},
        )
        assert null_result.output["items"] == [{"id": "A", "priority": None}]

        required_instance = dataflow_instance(
            tmp_path / "required",
            steps_yaml="""
            - id: shaped
              shape_items:
                items:
                  - id:
                fields:
                  id: $item.id
                required: [id]
              as: shaped
            """,
            returns="shaped",
        )
        with pytest.raises(QueryExecutionError, match="missing required field"):
            execute_workflow(
                required_instance,
                required_instance.load_config(),
                "dataflow",
                {},
            )

    def test_execute_join_items_inner_join_fanout_and_stable_order(self, tmp_path: Path) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: joined
              join_items:
                left_items:
                  - asset_id: A1
                    product_id: P1
                  - asset_id: A2
                    product_id: P2
                  - asset_id: A3
                    product_id: P3
                right_items:
                  - cve_id: CVE-1
                    product_id: P1
                  - cve_id: CVE-2
                    product_id: P1
                  - cve_id: CVE-skip
                    product_id:
                  - cve_id: CVE-3
                    product_id: P2
                left_key: $item.product_id
                right_key: $item.product_id
                fields:
                  asset_id: $item.left.asset_id
                  product_id: $item.join_key
                  cve_id: $item.right.cve_id
              as: joined
            """,
            returns="joined",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["skipped_right_count"] == 1
        assert result.output["matched_left_count"] == 2
        assert result.output["items"] == [
            {"asset_id": "A1", "product_id": "P1", "cve_id": "CVE-1"},
            {"asset_id": "A1", "product_id": "P1", "cve_id": "CVE-2"},
            {"asset_id": "A2", "product_id": "P2", "cve_id": "CVE-3"},
        ]

    def test_execute_join_items_composite_key_shape_must_match(self, tmp_path: Path) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: joined
              join_items:
                left_items:
                  - a: A
                    b: B
                right_items:
                  - a: A
                    b: B
                left_key: [$item.a, $item.b]
                right_key:
                  a: $item.a
                  b: $item.b
                fields:
                  a: $item.left.a
              as: joined
            """,
            returns="joined",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["output_count"] == 0
        assert result.output["items"] == []

    def test_execute_join_items_handles_empty_inputs(self, tmp_path: Path) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: joined
              join_items:
                left_items: []
                right_items: []
                left_key: $item.id
                right_key: $item.id
                fields:
                  id: $item.left.id
              as: joined
            """,
            returns="joined",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["left_count"] == 0
        assert result.output["right_count"] == 0
        assert result.output["items"] == []

    def test_execute_join_items_preserves_left_and_right_read_metadata(
        self, tmp_path: Path
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: left_rows
              list_entities:
                entity_type: Row
                limit: 1
              as: left_rows
            - id: right_rows
              list_entities:
                entity_type: Row
                limit: 1
              as: right_rows
            - id: joined
              join_items:
                left_items: $steps.left_rows.items
                right_items: $steps.right_rows.items
                left_key: $item.entity_id
                right_key: $item.entity_id
                fields:
                  id: $item.join_key
              as: joined
            """,
            returns="joined",
        )
        graph = instance.load_graph()
        for row_id in ("ROW-1", "ROW-2"):
            graph.add_entity(
                EntityInstance(
                    entity_type="Row",
                    entity_id=row_id,
                    properties={"id": row_id},
                )
            )
        instance.save_graph(graph)

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        metadata = result.output["source_metadata"]
        assert metadata == {
            "truncated": True,
            "limit_truncated": True,
            "path_truncated": False,
            "truncation_reasons": ["limit"],
        }
        assert result.output["left_source_metadata"]["source_step"] == "left_rows"
        assert result.output["left_source_metadata"]["source_ref"] == "$steps.left_rows.items"
        assert result.output["left_source_metadata"]["total_results"] == 2
        assert result.output["left_source_metadata"]["returned_results"] == 1
        assert result.output["right_source_metadata"]["source_step"] == "right_rows"
        assert result.output["right_source_metadata"]["source_ref"] == "$steps.right_rows.items"
        assert result.output["right_source_metadata"]["total_results"] == 2
        assert result.output["right_source_metadata"]["returned_results"] == 1

    def test_execute_filter_items_where_comparisons_and_passthrough(self, tmp_path: Path) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: filtered
              filter_items:
                items:
                  - id: A
                    severity: high
                    score: 0.9
                  - id: B
                    severity: low
                    score: 0.95
                  - id: C
                    severity: critical
                    score: 0.7
                where:
                  severity: [high, critical]
                comparisons:
                  - left: $item.score
                    op: ">="
                    right: 0.8
              as: filtered
            - id: passthrough
              filter_items:
                items: $steps.filtered.items
              as: passthrough
            """,
            returns="passthrough",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["input_count"] == 1
        assert result.output["items"] == [{"id": "A", "severity": "high", "score": 0.9}]

    def test_execute_filter_items_honors_typed_temporal_comparisons(
        self, tmp_path: Path
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: filtered
              filter_items:
                items:
                  - id: A
                    published_at: "2026-05-17T08:00:00-04:00"
                    business_date: "2026-05-17"
                  - id: B
                    published_at: "2026-05-18T00:00:00Z"
                    business_date: "2026-05-18"
                  - id: C
                    published_at: "not-a-datetime"
                    business_date: "2026-05-17"
                comparisons:
                  - left: $item.published_at
                    op: on_or_before
                    right: "2026-05-17T12:00:00+00:00"
                    value_type: datetime
                  - left: $item.business_date
                    op: before
                    right: "2026-05-18"
                    value_type: date
              as: filtered
            """,
            returns="filtered",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["items"] == [
            {
                "id": "A",
                "published_at": "2026-05-17T08:00:00-04:00",
                "business_date": "2026-05-17",
            }
        ]

    def test_execute_filter_and_dedupe_items_preserve_read_metadata(
        self, tmp_path: Path
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: rows
              list_entities:
                entity_type: Row
                limit: 1
              as: rows
            - id: filtered
              filter_items:
                items: $steps.rows.items
                where:
                  entity_type: Row
              as: filtered
            - id: deduped
              dedupe_items:
                items: $steps.filtered.items
                keys: [$item.entity_id]
              as: deduped
            """,
            returns="deduped",
        )
        graph = instance.load_graph()
        for row_id in ("ROW-1", "ROW-2"):
            graph.add_entity(
                EntityInstance(
                    entity_type="Row",
                    entity_id=row_id,
                    properties={"id": row_id},
                )
            )
        instance.save_graph(graph)

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        filtered_metadata = result.step_outputs["filtered"]["source_metadata"]
        assert filtered_metadata["source_step"] == "rows"
        assert filtered_metadata["source_ref"] == "$steps.rows.items"
        assert filtered_metadata["input_ref"] == "$steps.rows.items"
        assert filtered_metadata["total_results"] == 2
        assert filtered_metadata["returned_results"] == 1
        assert filtered_metadata["truncated"] is True
        assert filtered_metadata["truncation_reasons"] == ["limit"]
        deduped_metadata = result.output["source_metadata"]
        assert deduped_metadata["source_step"] == "rows"
        assert deduped_metadata["source_ref"] == "$steps.rows.items"
        assert deduped_metadata["input_ref"] == "$steps.filtered.items"
        assert deduped_metadata["total_results"] == 2
        assert deduped_metadata["returned_results"] == 1
        assert deduped_metadata["truncated"] is True
        assert deduped_metadata["truncation_reasons"] == ["limit"]

    def test_execute_aggregate_items_grouped_counts_and_ordering(
        self, tmp_path: Path
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: summary
              aggregate_items:
                items:
                  - owner_id: OWNER-B
                    asset_id:
                    priority: low
                    tags: [dev]
                  - owner_id: OWNER-A
                    asset_id: ASSET-1
                    priority: critical
                    tags: [prod, api]
                  - owner_id: OWNER-A
                    asset_id: ASSET-1
                    priority: high
                    tags: [prod, api]
                group_by:
                  owner_id: $item.owner_id
                measures:
                  exposure_count:
                    count: true
                  affected_assets:
                    count_distinct:
                      value: $item.asset_id
                  unique_contexts:
                    count_distinct:
                      value:
                        asset_id: $item.asset_id
                        tags: $item.tags
                  critical_count:
                    count_where:
                      left: $item.priority
                      op: eq
                      right: critical
              as: summary
            """,
            returns="summary",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["input_count"] == 3
        assert result.output["group_count"] == 2
        assert result.output["items"] == [
            {
                "owner_id": "OWNER-A",
                "exposure_count": 2,
                "affected_assets": 1,
                "unique_contexts": 1,
                "critical_count": 1,
            },
            {
                "owner_id": "OWNER-B",
                "exposure_count": 1,
                "affected_assets": 0,
                "unique_contexts": 1,
                "critical_count": 0,
            },
        ]

    def test_execute_aggregate_items_global_count_on_empty_and_non_empty(
        self, tmp_path: Path
    ) -> None:
        non_empty = dataflow_instance(
            tmp_path / "non_empty",
            steps_yaml="""
            - id: summary
              aggregate_items:
                items:
                  - id: A
                  - id: B
                measures:
                  row_count:
                    count: true
              as: summary
            """,
            returns="summary",
        )

        non_empty_result = execute_workflow(
            non_empty,
            non_empty.load_config(),
            "dataflow",
            {},
        )
        assert non_empty_result.output["items"] == [{"row_count": 2}]

        empty = dataflow_instance(
            tmp_path / "empty",
            steps_yaml="""
            - id: summary
              aggregate_items:
                items: []
                measures:
                  row_count:
                    count: true
                  max_score:
                    max:
                      value: $item.score
                      value_type: number
              as: summary
            """,
            returns="summary",
        )

        empty_result = execute_workflow(empty, empty.load_config(), "dataflow", {})

        assert empty_result.output["items"] == [{"row_count": 0, "max_score": None}]

    def test_execute_aggregate_items_rollups_and_typed_coercion(
        self, tmp_path: Path
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: summary
              aggregate_items:
                items:
                  - owner_id: OWNER-1
                    score: "1.5"
                    due_by: "2026-05-18T00:00:00Z"
                  - owner_id: OWNER-1
                    score: "2.25"
                    due_by: "2026-05-17T00:00:00Z"
                  - owner_id: OWNER-1
                    score:
                    due_by:
                group_by:
                  owner_id: $item.owner_id
                measures:
                  total_score:
                    sum:
                      value: $item.score
                      value_type: number
                  earliest_due_by:
                    min:
                      value: $item.due_by
                      value_type: datetime
                  max_score:
                    max:
                      value: $item.score
                      value_type: number
              as: summary
            """,
            returns="summary",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["items"] == [
            {
                "owner_id": "OWNER-1",
                "total_score": 3.75,
                "earliest_due_by": "2026-05-17T00:00:00Z",
                "max_score": "2.25",
            }
        ]

    def test_execute_aggregate_items_rejects_invalid_numeric_values(
        self, tmp_path: Path
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: summary
              aggregate_items:
                items:
                  - score: high
                measures:
                  total_score:
                    sum:
                      value: $item.score
              as: summary
            """,
            returns="summary",
        )

        with pytest.raises(QueryExecutionError, match="sum requires numeric values"):
            execute_workflow(instance, instance.load_config(), "dataflow", {})

    @pytest.mark.parametrize("score", ["nan", "inf"])
    def test_execute_aggregate_items_rejects_non_finite_typed_sum(
        self,
        tmp_path: Path,
        score: str,
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml=f"""
            - id: summary
              aggregate_items:
                items:
                  - score: "{score}"
                measures:
                  total_score:
                    sum:
                      value: $item.score
                      value_type: number
              as: summary
            """,
            returns="summary",
        )

        with pytest.raises(QueryExecutionError, match="sum requires finite numeric values"):
            execute_workflow(instance, instance.load_config(), "dataflow", {})

    def test_execute_aggregate_items_rejects_non_finite_typed_min_max(
        self, tmp_path: Path
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: summary
              aggregate_items:
                items:
                  - score: "inf"
                measures:
                  max_score:
                    max:
                      value: $item.score
                      value_type: number
              as: summary
            """,
            returns="summary",
        )

        with pytest.raises(QueryExecutionError, match="min/max requires finite numeric values"):
            execute_workflow(instance, instance.load_config(), "dataflow", {})

    def test_execute_aggregate_items_preserves_read_metadata_and_receipt_detail(
        self, tmp_path: Path
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: rows
              list_entities:
                entity_type: Row
                limit: 1
              as: rows
            - id: summary
              aggregate_items:
                items: $steps.rows.items
                measures:
                  row_count:
                    count: true
              as: summary
            """,
            returns="summary",
        )
        graph = instance.load_graph()
        for row_id in ("ROW-1", "ROW-2"):
            graph.add_entity(
                EntityInstance(
                    entity_type="Row",
                    entity_id=row_id,
                    properties={"id": row_id},
                )
            )
        instance.save_graph(graph)

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["items"] == [{"row_count": 1}]
        metadata = result.output["source_metadata"]
        assert metadata["source_step"] == "rows"
        assert metadata["returned_results"] == 1
        assert metadata["total_results"] == 2
        assert metadata["truncated"] is True
        aggregate_step = next(
            node
            for node in result.receipt.nodes
            if node.node_type == "plan_step"
            and node.detail.get("step_id") == "summary"
        )
        assert aggregate_step.detail["kind"] == "aggregate_items"
        assert aggregate_step.detail["input_count"] == 1
        assert aggregate_step.detail["group_count"] == 1
        assert aggregate_step.detail["measures"] == {"row_count": "count"}
        assert aggregate_step.detail["source_metadata"]["truncated"] is True

    def test_execute_assert_honors_typed_datetime_comparison(
        self, tmp_path: Path
    ) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: rows
              filter_items:
                items:
                  - id: A
              as: rows
            - id: date_guard
              assert:
                left: "2026-05-17T08:00:00-04:00"
                op: on_or_before
                right: "2026-05-17T12:00:00+00:00"
                value_type: datetime
                message: published timestamp must be in range
            """,
            returns="rows",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.output["items"] == [{"id": "A"}]
        assert any(
            node.node_type == "plan_step"
            and node.detail.get("value_type") == "datetime"
            for node in result.receipt.nodes
        )

    def test_execute_filter_items_where_accepts_input_refs(self, tmp_path: Path) -> None:
        instance = dataflow_instance(
            tmp_path,
            contract_fields_yaml="""
            fields:
              allowed:
                type: json
            """,
            steps_yaml="""
            - id: filtered
              filter_items:
                items:
                  - id: A
                    severity: high
                  - id: B
                    severity: low
                where:
                  severity: $input.allowed
              as: filtered
            """,
            returns="filtered",
        )

        result = execute_workflow(
            instance,
            instance.load_config(),
            "dataflow",
            {"allowed": ["high", "critical"]},
        )

        assert result.output["items"] == [{"id": "A", "severity": "high"}]

    def test_execute_dedupe_items_ranked_and_positional_strategies(self, tmp_path: Path) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: maxed
              dedupe_items:
                items:
                  - asset_id: A1
                    product_id: P1
                    confidence:
                  - asset_id: A1
                    product_id: P1
                    confidence: 0.9
                  - asset_id: A1
                    product_id: P1
                    confidence: 0.8
                  - asset_id: A2
                    product_id: P2
                    confidence: 0.4
                  - asset_id: A2
                    product_id: P2
                    confidence: 0.4
                keys: [$item.asset_id, $item.product_id]
                strategy: max
                rank: $item.confidence
              as: maxed
            - id: lasted
              dedupe_items:
                items: $steps.maxed.items
                keys: [$item.asset_id]
                strategy: last
                rank: $item.confidence
              as: lasted
            """,
            returns="lasted",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.step_outputs["maxed"]["duplicate_count"] == 3
        assert result.step_outputs["maxed"]["items"] == [
            {"asset_id": "A1", "product_id": "P1", "confidence": 0.9},
            {"asset_id": "A2", "product_id": "P2", "confidence": 0.4},
        ]
        assert result.output["items"] == [
            {"asset_id": "A1", "product_id": "P1", "confidence": 0.9},
            {"asset_id": "A2", "product_id": "P2", "confidence": 0.4},
        ]

    def test_execute_dedupe_items_incomparable_rank_raises(self, tmp_path: Path) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: deduped
              dedupe_items:
                items:
                  - id: A
                    confidence: 0.8
                  - id: A
                    confidence: high
                keys: [$item.id]
                strategy: max
                rank: $item.confidence
              as: deduped
            """,
            returns="deduped",
        )

        with pytest.raises(QueryExecutionError, match="rank values are incomparable"):
            execute_workflow(instance, instance.load_config(), "dataflow", {})

    def test_execute_dedupe_items_empty_first_and_min_strategies(self, tmp_path: Path) -> None:
        instance = dataflow_instance(
            tmp_path,
            steps_yaml="""
            - id: empty
              dedupe_items:
                items: []
                keys: [$item.id]
              as: empty
            - id: firsted
              dedupe_items:
                items:
                  - id: A
                    rank: 2
                  - id: A
                    rank: 1
                keys: [$item.id]
                strategy: first
                rank: $item.rank
              as: firsted
            - id: mined
              dedupe_items:
                items:
                  - id: B
                    rank:
                  - id: B
                    rank: 3
                  - id: B
                    rank: 2
                keys: [$item.id]
                strategy: min
                rank: $item.rank
              as: mined
            """,
            returns="mined",
        )

        result = execute_workflow(instance, instance.load_config(), "dataflow", {})

        assert result.step_outputs["empty"]["items"] == []
        assert result.step_outputs["firsted"]["items"] == [{"id": "A", "rank": 2}]
        assert result.output["items"] == [{"id": "B", "rank": 2}]
