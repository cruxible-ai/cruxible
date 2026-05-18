"""Tests for built-in workflow dataflow steps."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.workflow_helpers import dataflow_instance

from cruxible_core.errors import QueryExecutionError
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
