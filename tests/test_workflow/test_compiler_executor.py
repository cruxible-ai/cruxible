"""Tests for workflow lock, compilation, and execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from textwrap import dedent, indent

import pytest

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
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.receipt.serializer import to_markdown
from cruxible_core.service import service_list
from cruxible_core.workflow import (
    build_lock,
    compile_workflow,
    execute_workflow,
    get_legacy_lock_path,
    get_lock_path,
    write_lock,
)


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


def _write_lock_for_instance(instance: CruxibleInstance) -> None:
    config = instance.load_config()
    write_lock(build_lock(config, instance.get_config_path().parent), get_lock_path(instance))


def _json_contract_workflow_yaml(
    *,
    workflow_payload_field: str,
    provider_payload_field: str,
    provider_items_field: str,
) -> str:
    return f"""\
version: "1.0"
name: json_contract_workflow
enums:
  verdict:
    values: [support, reject]
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
{indent(dedent(workflow_payload_field).strip(), "      ")}
  ProviderInput:
    fields:
{indent(dedent(provider_payload_field).strip(), "      ")}
  ProviderOutput:
    fields:
{indent(dedent(provider_items_field).strip(), "      ")}
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
  validate_json_payload:
    contract_in: WorkflowInput
    steps:
      - id: echo
        provider: echo_json
        input:
          payload: $input.payload
        as: echo
    returns: echo
"""


def _json_contract_instance(tmp_path: Path, config_yaml: str) -> CruxibleInstance:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_yaml)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    _write_lock_for_instance(instance)
    return instance


def _dataflow_instance(
    tmp_path: Path,
    *,
    steps_yaml: str,
    returns: str,
    contract_fields_yaml: str = "fields: {}",
) -> CruxibleInstance:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""\
version: "1.0"
name: dataflow_workflow
kind: world_model

entity_types:
  Row:
    properties:
      id:
        type: string
        primary_key: true

relationships: []

contracts:
  DataflowInput:
{indent(dedent(contract_fields_yaml).strip(), "    ")}

workflows:
  dataflow:
    contract_in: DataflowInput
    steps:
{indent(dedent(steps_yaml).strip(), "      ")}
    returns: {returns}
"""
    )
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    _write_lock_for_instance(instance)
    return instance


def _compute_directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(child.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(child.read_bytes()).hexdigest().encode())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


class TestWorkflowCompiler:
    def test_compile_workflow_success(self, workflow_instance: CruxibleInstance) -> None:
        _write_lock_for_instance(workflow_instance)
        config = workflow_instance.load_config()

        plan = compile_workflow(
            config,
            build_lock(config),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert plan.workflow == "evaluate_promo"
        assert plan.contract_in == "PromoInput"
        assert plan.steps[0].kind == "query"
        assert plan.steps[0].params_preview["sku"] == "SKU-123"
        assert plan.steps[1].provider_version == "1.2.0"
        assert plan.steps[1].artifact_sha256 == "abc123"

    def test_compile_workflow_includes_list_entities_step(
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
        _write_lock_for_instance(workflow_instance)

        plan = compile_workflow(
            workflow_instance.load_config(),
            build_lock(workflow_instance.load_config()),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "category": "soda",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert plan.steps[1].kind == "list_entities"
        assert plan.steps[1].list_entities_spec is not None
        assert plan.steps[1].list_entities_spec.entity_type == "Product"

    def test_compile_workflow_rejects_bad_input_contract(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        _write_lock_for_instance(workflow_instance)
        config = workflow_instance.load_config()

        with pytest.raises(ConfigError, match="missing required field 'end_date'"):
            compile_workflow(
                config,
                build_lock(config),
                "evaluate_promo",
                {"sku": "SKU-123", "start_date": "2026-03-01"},
            )

    def test_compile_workflow_empty_input_error_mentions_cli_flags(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        _write_lock_for_instance(workflow_instance)
        config = workflow_instance.load_config()

        with pytest.raises(ConfigError, match="empty input payload provided"):
            compile_workflow(
                config,
                build_lock(config),
                "evaluate_promo",
                {},
            )

        with pytest.raises(ConfigError, match="Use --input or --input-file"):
            compile_workflow(
                config,
                build_lock(config),
                "evaluate_promo",
                {},
            )

    def test_compile_workflow_rejects_lock_digest_mismatch(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        lock = build_lock(config)
        lock.config_digest = "sha256:bad"

        with pytest.raises(ConfigError, match="Lock file config digest does not match"):
            compile_workflow(
                config,
                lock,
                "evaluate_promo",
                {
                    "sku": "SKU-123",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-07",
                },
            )

    def test_compile_workflow_includes_built_in_proposal_steps(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        _write_lock_for_instance(proposal_workflow_instance)
        config = proposal_workflow_instance.load_config()

        plan = compile_workflow(
            config,
            build_lock(config),
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert [step.kind for step in plan.steps] == [
            "query",
            "provider",
            "make_candidates",
            "map_signals",
            "propose_relationship_group",
        ]
        assert plan.steps[2].make_candidates_spec is not None
        assert plan.steps[2].make_candidates_spec.relationship_type == "recommended_for"
        assert plan.steps[3].map_signals_spec is not None
        assert plan.steps[3].map_signals_spec.integration == "catalog"
        assert plan.steps[4].propose_relationship_group_spec is not None
        assert plan.steps[4].propose_relationship_group_spec.signals_from == ["catalog_signals"]

    def test_compile_workflow_preserves_pending_refresh_mode(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        for step in config.workflows["propose_campaign_recommendations"].steps:
            if step.propose_relationship_group is not None:
                step.propose_relationship_group.pending_refresh_mode = "retain_missing"
        proposal_workflow_instance.save_config(config)
        _write_lock_for_instance(proposal_workflow_instance)

        plan = compile_workflow(
            proposal_workflow_instance.load_config(),
            build_lock(proposal_workflow_instance.load_config()),
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert plan.steps[4].propose_relationship_group_spec is not None
        assert (
            plan.steps[4].propose_relationship_group_spec.pending_refresh_mode
            == "retain_missing"
        )

    def test_compile_canonical_workflow_carries_canonical_metadata(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        _write_lock_for_instance(canonical_workflow_instance)
        config = canonical_workflow_instance.load_config()
        lock = build_lock(config, canonical_workflow_instance.get_config_path().parent)

        plan = compile_workflow(
            config,
            lock,
            "build_reference",
            {},
            config_base_path=canonical_workflow_instance.get_config_path().parent,
        )

        assert plan.canonical is True
        assert plan.lock_digest == lock.lock_digest
        assert plan.steps[0].provider_entrypoint_sha256 is not None
        assert "apply_entities" in [step.kind for step in plan.steps]

    def test_compile_rejects_apply_steps_in_non_canonical_workflow(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        config.workflows["build_reference"].canonical = False
        canonical_workflow_instance.save_config(config)
        _write_lock_for_instance(canonical_workflow_instance)

        with pytest.raises(ConfigError, match="must be canonical to use apply_entities"):
            compile_workflow(
                canonical_workflow_instance.load_config(),
                build_lock(canonical_workflow_instance.load_config()),
                "build_reference",
                {},
                config_base_path=canonical_workflow_instance.get_config_path().parent,
            )

    def test_build_lock_rejects_stale_canonical_artifact_hash(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        config.artifacts["canonical_bundle"].sha256 = "sha256:bad"
        canonical_workflow_instance.save_config(config)

        with pytest.raises(ConfigError) as exc_info:
            build_lock(
                canonical_workflow_instance.load_config(),
                canonical_workflow_instance.get_config_path().parent,
            )
        message = str(exc_info.value)
        assert "Artifact 'canonical_bundle' sha256 mismatch." in message
        assert "expected (config): sha256:bad" in message
        assert "actual (on disk):" in message
        assert "cruxible lock --force" in message

    def test_build_lock_force_accepts_live_canonical_artifact_hash(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        config.artifacts["canonical_bundle"].sha256 = "sha256:bad"
        canonical_workflow_instance.save_config(config)

        lock = build_lock(
            canonical_workflow_instance.load_config(),
            canonical_workflow_instance.get_config_path().parent,
            force=True,
        )

        assert lock.artifacts["canonical_bundle"].sha256 != "sha256:bad"
        assert lock.artifacts["canonical_bundle"].sha256.startswith("sha256:")

    def test_executor_uses_legacy_lock_path_as_fallback(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        legacy_path = get_legacy_lock_path(workflow_instance)
        write_lock(build_lock(config, workflow_instance.get_config_path().parent), legacy_path)

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

        assert result.output["decision"] == "approve"


class TestWorkflowDataflowSteps:
    def test_execute_shape_items_casts_and_drops_missing_required(
        self, tmp_path: Path
    ) -> None:
        instance = _dataflow_instance(
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
        cast_instance = _dataflow_instance(
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

        collision_instance = _dataflow_instance(
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
        empty_instance = _dataflow_instance(
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

        null_instance = _dataflow_instance(
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

        required_instance = _dataflow_instance(
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

    def test_execute_join_items_inner_join_fanout_and_stable_order(
        self, tmp_path: Path
    ) -> None:
        instance = _dataflow_instance(
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

    def test_execute_join_items_composite_key_shape_must_match(
        self, tmp_path: Path
    ) -> None:
        instance = _dataflow_instance(
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
        instance = _dataflow_instance(
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

    def test_execute_filter_items_where_comparisons_and_passthrough(
        self, tmp_path: Path
    ) -> None:
        instance = _dataflow_instance(
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

    def test_execute_filter_items_where_accepts_input_refs(self, tmp_path: Path) -> None:
        instance = _dataflow_instance(
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

    def test_execute_dedupe_items_ranked_and_positional_strategies(
        self, tmp_path: Path
    ) -> None:
        instance = _dataflow_instance(
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

    def test_execute_dedupe_items_incomparable_rank_raises(
        self, tmp_path: Path
    ) -> None:
        instance = _dataflow_instance(
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

    def test_execute_dedupe_items_empty_first_and_min_strategies(
        self, tmp_path: Path
    ) -> None:
        instance = _dataflow_instance(
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


class TestWorkflowExecutor:
    def test_execute_workflow_success(self, workflow_instance: CruxibleInstance) -> None:
        _write_lock_for_instance(workflow_instance)

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
        assert result.receipt.operation_type == "workflow"
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
        _write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "query_active_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert result.output["total_results"] == 1
        assert [item["entity_id"] for item in result.output["results"]] == ["SKU-123"]

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
        _write_lock_for_instance(workflow_instance)

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
        _write_lock_for_instance(workflow_instance)

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
                    "property_filter": {"review_status": "$input.status"},
                    "limit": 10,
                },
                **{"as": "existing_links"},
            ),
        )
        proposal_workflow_instance.save_config(config)
        _write_lock_for_instance(proposal_workflow_instance)

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
                    "property_filter": {"review_status": "$input.status"},
                    "limit": 10,
                },
                **{"as": "existing_links"},
            ),
        )
        proposal_workflow_instance.save_config(config)
        _write_lock_for_instance(proposal_workflow_instance)

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
            property_filter={"review_status": "human_approved"},
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
        _write_lock_for_instance(workflow_instance)

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

    def test_compile_workflow_rejects_missing_required_json_schema_field(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _json_contract_instance(
            tmp_path,
            _json_contract_workflow_yaml(
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
        instance = _json_contract_instance(
            tmp_path,
            _json_contract_workflow_yaml(
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
        instance = _json_contract_instance(
            tmp_path,
            _json_contract_workflow_yaml(
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
        instance = _json_contract_instance(
            tmp_path,
            _json_contract_workflow_yaml(
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
        instance = _json_contract_instance(
            tmp_path,
            _json_contract_workflow_yaml(
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
        instance = _json_contract_instance(
            tmp_path,
            _json_contract_workflow_yaml(
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
        assert nested_null_result.output["items"] == [
            {"verdict": "support", "note": None}
        ]

    def test_execute_workflow_assert_failure_records_workflow_receipt(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        for step in config.workflows["evaluate_promo"].steps:
            if step.assert_spec is not None:
                step.assert_spec.right = 0.90
        workflow_instance.save_config(config)
        _write_lock_for_instance(workflow_instance)

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
        finally:
            store.close()
        assert receipts

    def test_execute_workflow_builds_relationship_proposal_artifact(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        _write_lock_for_instance(proposal_workflow_instance)

        result = execute_workflow(
            proposal_workflow_instance,
            proposal_workflow_instance.load_config(),
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert result.output["relationship_type"] == "recommended_for"
        assert len(result.output["members"]) == 2
        assert result.output["integrations_used"] == ["catalog"]
        assert len(result.traces) == 1
        plan_steps = [node for node in result.receipt.nodes if node.node_type == "plan_step"]
        assert any(node.detail.get("relationship_type") == "recommended_for" for node in plan_steps)
        assert any(node.detail.get("integration") == "catalog" for node in plan_steps)
        assert any(node.detail.get("signals_from") == ["catalog_signals"] for node in plan_steps)

    def test_execute_canonical_workflow_runs_in_preview_mode_without_mutating_graph(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        _write_lock_for_instance(canonical_workflow_instance)

        result = execute_workflow(
            canonical_workflow_instance,
            canonical_workflow_instance.load_config(),
            "build_reference",
            {},
        )

        assert result.mode == "preview"
        assert result.canonical is True
        assert result.apply_digest is not None
        assert result.committed_snapshot_id is None
        assert result.receipt.committed is False
        assert result.output["total_results"] == 1
        assert canonical_workflow_instance.load_graph().list_entities("Vendor") == []

    def test_execute_canonical_workflow_apply_commits_graph_and_snapshot(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        _write_lock_for_instance(canonical_workflow_instance)
        preview = execute_workflow(
            canonical_workflow_instance,
            canonical_workflow_instance.load_config(),
            "build_reference",
            {},
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
        assert canonical_workflow_instance.load_graph().has_entity("Vendor", "vendor-acme")

    def test_canonical_workflow_tabular_shape_ingest_parity(
        self, tmp_path: Path
    ) -> None:
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
        bundle_sha256 = _compute_directory_sha256(bundle_dir)
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
    canonical: true
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
        _write_lock_for_instance(instance)

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
        config.artifacts["canonical_bundle"].sha256 = _compute_directory_sha256(
            canonical_workflow_instance.root / "bundle"
        )
        canonical_workflow_instance.save_config(config)
        _write_lock_for_instance(canonical_workflow_instance)

        result = execute_workflow(
            canonical_workflow_instance,
            canonical_workflow_instance.load_config(),
            "build_reference",
            {},
        )

        product_preview = result.apply_previews["apply_products"]
        assert product_preview["duplicate_input_count"] == 1
        assert product_preview["conflicting_duplicate_count"] == 1
        assert product_preview["duplicate_examples"][0]["entity_id"] == "product-acme-widget"

        rel_preview = result.apply_previews["apply_product_vendor"]
        assert rel_preview["duplicate_input_count"] == 1
        assert rel_preview["conflicting_duplicate_count"] == 0
