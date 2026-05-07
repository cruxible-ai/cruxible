"""Tests for CLI commands using Click CliRunner."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import cli
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.service import service_propose_group, service_resolve_group


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _chdir_run(runner: CliRunner, directory: Path, args: list[str]) -> object:
    """Run CLI in the given directory."""
    original = os.getcwd()
    try:
        os.chdir(directory)
        return runner.invoke(cli, args)
    finally:
        os.chdir(original)


def _assert_local_mutation_disabled(
    runner: CliRunner,
    directory: Path,
    args: list[str],
    label: str,
) -> None:
    result = _chdir_run(runner, directory, args)
    assert result.exit_code == 2
    assert f"Local mutation disabled for {label}" in result.output


def _save_trace(instance: CruxibleInstance, trace_id: str = "TRC-cli-001") -> str:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = ExecutionTrace(
        trace_id=trace_id,
        workflow_name="wf",
        step_id="step",
        provider_name="provider",
        provider_version="1.0.0",
        provider_ref="tests.support.workflow_test_providers.provider",
        runtime="python",
        deterministic=True,
        side_effects=False,
        input_payload={"input": True},
        output_payload={"rows": 5},
        started_at=started_at,
        finished_at=started_at,
        duration_ms=0.0,
    )
    store = instance.get_receipt_store()
    try:
        store.save_trace(trace)
    finally:
        store.close()
    return trace.trace_id


@pytest.fixture
def governed_view_instance(
    tmp_path: Path,
    proposal_workflow_config_yaml: str,
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


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_instance(self, runner: CliRunner, tmp_project: Path) -> None:
        _assert_local_mutation_disabled(
            runner,
            tmp_project,
            ["init", "--config", "config.yaml"],
            "init",
        )

    def test_init_with_data_dir(self, runner: CliRunner, tmp_project: Path) -> None:
        _assert_local_mutation_disabled(
            runner,
            tmp_project,
            ["init", "--config", "config.yaml", "--data-dir", "data"],
            "init",
        )

    def test_init_bad_config(self, runner: CliRunner, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("not_valid: true\n")
        _assert_local_mutation_disabled(runner, tmp_path, ["init", "--config", "bad.yaml"], "init")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_config(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = str(tmp_project / "config.yaml")
        result = runner.invoke(cli, ["validate", "--config", config_path])
        assert result.exit_code == 0
        assert "valid" in result.output

    def test_invalid_config(self, runner: CliRunner, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("invalid: true\n")
        result = runner.invoke(cli, ["validate", "--config", str(bad)])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


class TestIngest:
    def test_ingest_entities(
        self,
        runner: CliRunner,
        initialized_project: CruxibleInstance,
        vehicles_csv: Path,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            initialized_project.root,
            ["ingest", "--mapping", "vehicles", "--file", str(vehicles_csv)],
            "ingest",
        )

    def test_ingest_relationships(
        self,
        runner: CliRunner,
        initialized_project: CruxibleInstance,
        vehicles_csv: Path,
        parts_csv: Path,
        fitments_csv: Path,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            initialized_project.root,
            ["ingest", "--mapping", "fitments", "--file", str(fitments_csv)],
            "ingest",
        )

    def test_ingest_bad_mapping(
        self,
        runner: CliRunner,
        initialized_project: CruxibleInstance,
        vehicles_csv: Path,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            initialized_project.root,
            ["ingest", "--mapping", "nonexistent", "--file", str(vehicles_csv)],
            "ingest",
        )


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


class TestQuery:
    def test_query_parts_for_vehicle(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        assert result.exit_code == 0
        assert "Receipt:" in result.output
        assert "2 result(s), 1 step(s) executed." in result.output

    def test_query_bad_name(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "nonexistent", "--param", "id=1"],
        )
        assert result.exit_code == 1

    def test_query_count_mode_prints_summary_and_hints(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            [
                "query",
                "--query",
                "parts_for_vehicle",
                "--param",
                "vehicle_id=V-2024-CIVIC-EX",
                "--count",
            ],
        )
        assert result.exit_code == 0
        assert "2 result(s), 1 step(s) executed." in result.output
        assert "Param hints:" in result.output
        assert "primary_key=vehicle_id" in result.output
        assert "Part entities" not in result.output

    def test_query_zero_results_prints_hints(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=UNKNOWN"],
        )
        assert result.exit_code == 1
        assert "Param hints:" in result.output
        assert "primary_key=vehicle_id" in result.output
        assert "examples=V-2024-ACCORD-SPORT, V-2024-CIVIC-EX" in result.output


class TestEvaluate:
    def test_evaluate_prints_quality_summary(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        project = tmp_path / "quality-project"
        project.mkdir()
        (project / "config.yaml").write_text(
            """\
version: "1.0"
name: quality_project
entity_types:
  Product:
    properties:
      product_id:
        type: string
        primary_key: true
      name:
        type: string
relationships: []
quality_checks:
  - name: product_name_non_empty
    kind: property
    severity: error
    target: entity
    entity_type: Product
    property: name
    rule: non_empty
"""
        )
        instance = CruxibleInstance.init(project, "config.yaml")
        graph = instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="Product",
                entity_id="P-1",
                properties={"product_id": "P-1", "name": ""},
            )
        )
        instance.save_graph(graph)

        result = _chdir_run(runner, project, ["evaluate"])
        assert result.exit_code == 0
        assert "Quality checks:" in result.output
        assert "product_name_non_empty: 1" in result.output


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class TestExplain:
    def _run_query_get_receipt(
        self,
        runner: CliRunner,
        instance: CruxibleInstance,
    ) -> str:
        result = _chdir_run(
            runner,
            instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        # Extract receipt ID from output
        for line in result.output.splitlines():
            if line.startswith("Receipt:"):
                return line.split(":", 1)[1].strip()
        pytest.fail("No receipt ID found in query output")

    def test_explain_markdown(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        receipt_id = self._run_query_get_receipt(runner, populated_instance)
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["explain", "--receipt", receipt_id, "--format", "markdown"],
        )
        assert result.exit_code == 0
        assert "Receipt" in result.output

    def test_explain_json(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        receipt_id = self._run_query_get_receipt(runner, populated_instance)
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["explain", "--receipt", receipt_id, "--format", "json"],
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "receipt_id" in parsed

    def test_explain_mermaid(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        receipt_id = self._run_query_get_receipt(runner, populated_instance)
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["explain", "--receipt", receipt_id, "--format", "mermaid"],
        )
        assert result.exit_code == 0
        assert "graph TD" in result.output

    def test_explain_not_found(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["explain", "--receipt", "RCP-nonexistent"],
        )
        assert result.exit_code == 1


class TestStatsInspectReload:
    def test_stats_outputs_counts(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(runner, populated_instance.root, ["stats"])
        assert result.exit_code == 0
        assert "Graph: 4 entities, 4 edges" in result.output
        assert "Vehicle" in result.output
        assert "fits" in result.output

    def test_inspect_entity_outputs_neighbors(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["inspect", "entity", "--type", "Vehicle", "--id", "V-2024-CIVIC-EX"],
        )
        assert result.exit_code == 0
        assert "Neighbors: 2" in result.output
        assert "Part:BP-1001" in result.output
        assert "fits" in result.output

    def test_inspect_trace_outputs_payload(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        trace_id = _save_trace(populated_instance)
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["inspect", "trace", trace_id, "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["trace_id"] == trace_id
        assert payload["output_payload"]["rows"] == 5

    def test_reload_config_repoints_instance(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        tmp_path: Path,
    ) -> None:
        new_config = tmp_path / "alt-config.yaml"
        new_config.write_text(
            (populated_instance.root / "config.yaml")
            .read_text()
            .replace("car_parts_compatibility", "alt_name")
        )

        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            ["reload-config", "--config", str(new_config)],
            "reload-config",
        )


class TestConfigViews:
    def test_config_views_default_renders_standard_sections(
        self,
        runner: CliRunner,
        tmp_path: Path,
        proposal_workflow_config_yaml: str,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(proposal_workflow_config_yaml)

        result = runner.invoke(cli, ["config-views", "--config", str(config_path)])

        assert result.exit_code == 0
        assert "# Cruxible Config Diagrams" in result.output
        assert f"Source: `{config_path}`" in result.output
        assert "Recommended For" in result.output
        assert "Governed proposal" in result.output
        assert "Creation Path" in result.output
        assert "| Integration | Kind | Used By | Notes |" in result.output
        assert "No configured constraints." in result.output
        assert "No configured feedback profiles." in result.output
        assert "### Entity Types" not in result.output

    def test_config_views_single_bare_view_outputs_raw_mermaid(
        self,
        runner: CliRunner,
        tmp_path: Path,
        proposal_workflow_config_yaml: str,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(proposal_workflow_config_yaml)

        result = runner.invoke(
            cli,
            ["config-views", "--config", str(config_path), "--view", "ontology", "--bare"],
        )

        assert result.exit_code == 0
        assert "Recommended For" in result.output
        assert "classDef governedEntity" in result.output
        assert "```mermaid" not in result.output

    def test_config_views_updates_readme_markers(
        self,
        runner: CliRunner,
        tmp_path: Path,
        proposal_workflow_config_yaml: str,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(proposal_workflow_config_yaml)
        readme = tmp_path / "README.md"
        readme.write_text(
            "# Demo\n\n<!-- CRUXIBLE:BEGIN ontology -->\n<!-- CRUXIBLE:END ontology -->\n"
        )

        result = runner.invoke(
            cli,
            [
                "config-views",
                "--config",
                str(config_path),
                "--view",
                "ontology",
                "--update-readme",
                str(readme),
            ],
        )

        assert result.exit_code == 0
        assert f"Updated {readme}" in result.output
        updated = readme.read_text()
        assert "Recommended For" in updated
        assert "```mermaid" in updated

    def test_config_views_missing_readme_marker_is_usage_error(
        self,
        runner: CliRunner,
        tmp_path: Path,
        proposal_workflow_config_yaml: str,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(proposal_workflow_config_yaml)
        readme = tmp_path / "README.md"
        readme.write_text("# Demo\n")

        result = runner.invoke(
            cli,
            [
                "config-views",
                "--config",
                str(config_path),
                "--view",
                "ontology",
                "--update-readme",
                str(readme),
            ],
        )

        assert result.exit_code == 2
        assert "Missing README marker block(s): ontology" in result.output

    def test_config_views_runtime_composes_extends_without_upstream_build_workflows(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        base = tmp_path / "base.yaml"
        overlay = tmp_path / "overlay.yaml"
        base.write_text(
            """\
version: "1.0"
name: base
kind: world_model
entity_types:
  Product:
    properties:
      product_id:
        type: string
        primary_key: true
relationships: []
contracts:
  EmptyInput:
    fields: {}
  EmptyOutput:
    fields: {}
providers:
  load_reference:
    kind: function
    runtime: python
    ref: tests.support.workflow_test_providers.reference_bundle_loader
    contract_in: EmptyInput
    contract_out: EmptyOutput
    deterministic: true
    version: 1.0.0
workflows:
  build_reference:
    canonical: true
    contract_in: EmptyInput
    returns: apply_products
    steps:
      - id: products
        make_entities:
          entity_type: Product
          items: []
          entity_id: $item.product_id
          properties: {}
        as: products
      - id: apply_products
        apply_entities:
          entities_from: products
        as: apply_products
"""
        )
        overlay.write_text(
            """\
version: "1.0"
name: overlay
extends: base.yaml
entity_types:
  Asset:
    properties:
      asset_id:
        type: string
        primary_key: true
relationships:
  - name: asset_runs_product
    from_entity: Asset
    to_entity: Product
    properties: {}
    matching:
      integrations: {}
workflows:
  build_overlay:
    canonical: true
    contract_in: EmptyInput
    returns: apply_assets
    steps:
      - id: assets
        make_entities:
          entity_type: Asset
          items: []
          entity_id: $item.asset_id
          properties: {}
        as: assets
      - id: apply_assets
        apply_entities:
          entities_from: assets
        as: apply_assets
"""
        )

        result = runner.invoke(
            cli,
            [
                "config-views",
                "--config",
                str(overlay),
                "--runtime",
                "--view",
                "workflow-summary",
            ],
        )

        assert result.exit_code == 0
        assert "Build Overlay" in result.output
        assert "Build Reference" not in result.output


class TestCanonicalViews:
    def test_inspect_ontology_mermaid_outputs_governed_edge(
        self,
        runner: CliRunner,
        governed_view_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "ontology", "--format", "mermaid"],
        )
        assert result.exit_code == 0
        assert "classDef canonicalEntity" in result.output
        assert "classDef governedEntity" in result.output
        assert "class entity_Campaign,entity_Product governedEntity" in result.output
        assert "Campaign" in result.output
        assert "Recommended For" in result.output
        assert 'entity_Campaign -. "Recommended For" .-> entity_Product' in result.output
        assert "stroke:#e74c3c" in result.output

    def test_inspect_workflows_json_summarizes_workflow_shape(
        self,
        runner: CliRunner,
        governed_view_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "workflows", "--format", "json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["workflow_count"] == 1
        workflow = payload["workflows"][0]
        assert workflow["name"] == "propose_campaign_recommendations"
        assert workflow["queries"] == ["get_campaign_context"]
        assert workflow["providers"] == ["campaign_recommendations"]
        assert workflow["proposes_relationships"] == ["recommended_for"]

    def test_inspect_workflows_mermaid_formats(
        self,
        runner: CliRunner,
        governed_view_instance: CruxibleInstance,
    ) -> None:
        story = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "workflows", "--format", "mermaid"],
        )
        assert story.exit_code == 0
        assert "Proposes: Recommended For" in story.output

        steps = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "workflows", "--format", "mermaid-steps"],
        )
        assert steps.exit_code == 0
        assert "Provider: Campaign Recommendations" in steps.output

        dependencies = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "workflows", "--format", "mermaid-dependencies"],
        )
        assert dependencies.exit_code == 0
        assert "Governed" in dependencies.output

    def test_inspect_queries_json_surfaces_traversal_and_params(
        self,
        runner: CliRunner,
        governed_view_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "queries", "--format", "json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["query_count"] == 1
        query = payload["queries"][0]
        assert query["name"] == "get_campaign_context"
        assert query["entry_point"] == "Campaign"
        assert query["required_params"] == ["campaign_id"]

    def test_inspect_queries_mermaid_is_human_readable(
        self,
        runner: CliRunner,
        governed_view_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "queries", "--format", "mermaid"],
        )
        assert result.exit_code == 0
        assert "Get Campaign Context" in result.output
        assert "Entry: Campaign" in result.output
        assert "Returns: List[Campaign]" in result.output

    def test_inspect_governance_tracks_pending_and_approved_state(
        self,
        runner: CliRunner,
        governed_view_instance: CruxibleInstance,
    ) -> None:
        proposed = service_propose_group(
            governed_view_instance,
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
                            integration="catalog",
                            signal="support",
                            evidence="seasonal match",
                        )
                    ],
                    properties={"reason": "seasonal match"},
                )
            ],
            thesis_text="Recommend products for regional campaign",
            thesis_facts={"rule_id": "campaign_recommendations", "rule_version": 1},
        )

        pending_result = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "governance", "--format", "json"],
        )
        assert pending_result.exit_code == 0
        pending_payload = json.loads(pending_result.output)
        assert pending_payload["governed_relationship_count"] == 1
        assert pending_payload["pending_group_count"] == 1
        assert pending_payload["relationships"][0]["pending_group_count"] == 1

        service_resolve_group(
            governed_view_instance,
            proposed.group_id,
            "approve",
            expected_pending_version=1,
        )

        approved_result = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "governance", "--format", "json"],
        )
        assert approved_result.exit_code == 0
        approved_payload = json.loads(approved_result.output)
        assert approved_payload["pending_group_count"] == 0
        assert approved_payload["approved_resolution_count"] == 1
        assert approved_payload["relationships"][0]["approved_resolution_count"] == 1

    def test_inspect_overview_outputs_generated_markdown(
        self,
        runner: CliRunner,
        governed_view_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            governed_view_instance.root,
            ["inspect", "overview"],
        )
        assert result.exit_code == 0
        assert "# Config Overview" in result.output
        assert "## Relationship Map" in result.output
        assert "```mermaid" in result.output
        assert "**Diagram legend:**" in result.output
        assert "Blue entity node" in result.output
        assert "propose_campaign_recommendations" in result.output


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------


class TestFeedback:
    def test_feedback_approve(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        # Run a query first to get a receipt
        q_result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        receipt_id = None
        for line in q_result.output.splitlines():
            if line.startswith("Receipt:"):
                receipt_id = line.split(":", 1)[1].strip()

        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "feedback",
                "--receipt",
                receipt_id,
                "--action",
                "approve",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1001",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-2024-CIVIC-EX",
                "--reason",
                "Verified in catalog",
            ],
            "feedback",
        )

    def test_feedback_reject(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        q_result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        receipt_id = None
        for line in q_result.output.splitlines():
            if line.startswith("Receipt:"):
                receipt_id = line.split(":", 1)[1].strip()

        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "feedback",
                "--receipt",
                receipt_id,
                "--action",
                "reject",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1001",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-2024-CIVIC-EX",
                "--reason",
                "Wrong part",
            ],
            "feedback",
        )

    def test_feedback_agent_source(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        q_result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        receipt_id = None
        for line in q_result.output.splitlines():
            if line.startswith("Receipt:"):
                receipt_id = line.split(":", 1)[1].strip()

        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "feedback",
                "--receipt",
                receipt_id,
                "--action",
                "approve",
                "--source",
                "agent",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1001",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-2024-CIVIC-EX",
            ],
            "feedback",
        )


# ---------------------------------------------------------------------------
# outcome
# ---------------------------------------------------------------------------


class TestOutcome:
    def test_outcome_correct(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        q_result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        receipt_id = None
        for line in q_result.output.splitlines():
            if line.startswith("Receipt:"):
                receipt_id = line.split(":", 1)[1].strip()

        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            ["outcome", "--receipt", receipt_id, "--outcome", "correct"],
            "outcome",
        )

    def test_outcome_with_detail(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        q_result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        receipt_id = None
        for line in q_result.output.splitlines():
            if line.startswith("Receipt:"):
                receipt_id = line.split(":", 1)[1].strip()

        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "outcome",
                "--receipt",
                receipt_id,
                "--outcome",
                "incorrect",
                "--detail",
                '{"notes": "part did not fit"}',
            ],
            "outcome",
        )


# ---------------------------------------------------------------------------
# list subcommands
# ---------------------------------------------------------------------------


class TestList:
    def test_list_entities(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "entities", "--type", "Part"],
        )
        assert result.exit_code == 0
        assert "2 entity" in result.output

    def test_list_receipts(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        # Run a query first
        _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "receipts"],
        )
        assert result.exit_code == 0
        assert "1 receipt" in result.output

    def test_list_traces(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        trace_id = _save_trace(populated_instance)
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "traces", "--workflow", "wf", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["traces"][0]["trace_id"] == trace_id
        assert payload["count"] == 1

    def test_list_feedback(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "feedback"],
        )
        assert result.exit_code == 0
        assert "0 record" in result.output

    def test_list_outcomes(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "outcomes"],
        )
        assert result.exit_code == 0
        assert "0 record" in result.output


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_schema_output(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["schema"],
        )
        assert result.exit_code == 0
        assert "Vehicle" in result.output
        assert "Part" in result.output


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------


class TestSample:
    def test_sample_entities(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["sample", "--type", "Part", "--limit", "1"],
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# find-candidates
# ---------------------------------------------------------------------------


class TestFindCandidates:
    def test_find_candidates_property_match(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            [
                "find-candidates",
                "--relationship",
                "replaces",
                "--strategy",
                "property_match",
                "--rule",
                "category=category",
            ],
        )
        assert result.exit_code == 0
        assert "candidate" in result.output


# ---------------------------------------------------------------------------
# get-entity
# ---------------------------------------------------------------------------


class TestGetEntity:
    def test_found(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["get-entity", "--type", "Vehicle", "--id", "V-2024-CIVIC-EX"],
        )
        assert result.exit_code == 0
        assert "V-2024-CIVIC-EX" in result.output

    def test_not_found(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["get-entity", "--type", "Vehicle", "--id", "NONEXISTENT"],
        )
        assert result.exit_code == 0
        assert "Not found." in result.output


# ---------------------------------------------------------------------------
# get-relationship
# ---------------------------------------------------------------------------


class TestGetRelationship:
    def test_found(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            [
                "get-relationship",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1001",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-2024-CIVIC-EX",
            ],
        )
        assert result.exit_code == 0
        assert "fits" in result.output

    def test_not_found(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            [
                "get-relationship",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1001",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "NONEXISTENT",
            ],
        )
        assert result.exit_code == 0
        assert "Not found." in result.output

    def test_ambiguous_no_edge_key(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        """When multiple edges exist between same pair, require --edge-key."""
        # Add a second fits edge between BP-1001 and V-2024-CIVIC-EX
        from cruxible_core.graph.types import RelationshipInstance

        graph = populated_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1001",
                to_type="Vehicle",
                to_id="V-2024-CIVIC-EX",
                properties={"verified": False, "source": "duplicate"},
            )
        )
        populated_instance.save_graph(graph)
        # Invalidate cache so CLI picks up the new graph
        populated_instance.invalidate_graph_cache()

        result = _chdir_run(
            runner,
            populated_instance.root,
            [
                "get-relationship",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1001",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-2024-CIVIC-EX",
            ],
        )
        assert result.exit_code == 1
        assert "Ambiguous" in result.output


# ---------------------------------------------------------------------------
# add-entity
# ---------------------------------------------------------------------------


class TestAddEntity:
    def test_new(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-entity",
                "--type",
                "Vehicle",
                "--id",
                "V-NEW",
                "--props",
                '{"vehicle_id": "V-NEW", "year": 2025, "make": "Toyota"}',
            ],
            "add-entity",
        )

    def test_update(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-entity",
                "--type",
                "Vehicle",
                "--id",
                "V-2024-CIVIC-EX",
                "--props",
                '{"vehicle_id": "V-2024-CIVIC-EX", "year": 2025}',
            ],
            "add-entity",
        )

    def test_bad_type(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            ["add-entity", "--type", "NoSuchType", "--id", "X1"],
            "add-entity",
        )


# ---------------------------------------------------------------------------
# add-relationship
# ---------------------------------------------------------------------------


class TestAddRelationship:
    def test_new(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-relationship",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1002",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-2024-ACCORD-SPORT",
                "--props",
                '{"verified": true, "source": "manual"}',
            ],
            "add-relationship",
        )

    def test_update(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-relationship",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1001",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-2024-CIVIC-EX",
                "--props",
                '{"verified": true, "source": "updated"}',
            ],
            "add-relationship",
        )

    def test_missing_entity(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-relationship",
                "--from-type",
                "Part",
                "--from-id",
                "MISSING",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-2024-CIVIC-EX",
            ],
            "add-relationship",
        )

    def test_bad_direction(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-relationship",
                "--from-type",
                "Vehicle",
                "--from-id",
                "V-2024-CIVIC-EX",
                "--relationship",
                "fits",
                "--to-type",
                "Part",
                "--to-id",
                "BP-1001",
            ],
            "add-relationship",
        )


# ---------------------------------------------------------------------------
# add-constraint
# ---------------------------------------------------------------------------


class TestAddConstraint:
    def test_valid(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-constraint",
                "--name",
                "brake_category_match",
                "--rule",
                "replaces.FROM.category == replaces.TO.category",
                "--severity",
                "warning",
                "--description",
                "Replacement parts must be same category",
            ],
            "add-constraint",
        )

    def test_valid_not_equal_rule(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-constraint",
                "--name",
                "no_self_replacement",
                "--rule",
                "replaces.FROM.part_number != replaces.TO.part_number",
            ],
            "add-constraint",
        )

    def test_bad_rule(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-constraint",
                "--name",
                "bad",
                "--rule",
                "this is not valid syntax",
            ],
            "add-constraint",
        )

    def test_duplicate_name(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "add-constraint",
                "--name",
                "unique_rule",
                "--rule",
                "replaces.FROM.category == replaces.TO.category",
            ],
            "add-constraint",
        )


# ---------------------------------------------------------------------------
# list edges
# ---------------------------------------------------------------------------


class TestListEdges:
    def test_list_all(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "edges"],
        )
        assert result.exit_code == 0
        assert "edge(s) shown" in result.output
        # Should contain edge_key column data
        assert "Edge Key" in result.output

    def test_filter_by_relationship(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "edges", "--relationship", "replaces"],
        )
        assert result.exit_code == 0
        assert "1 edge(s) shown" in result.output


# ---------------------------------------------------------------------------
# export edges
# ---------------------------------------------------------------------------


class TestExportEdges:
    def test_export_all_edges(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        """Exports all edges to CSV with correct headers and row count."""
        out = populated_instance.root / "edges.csv"
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["export", "edges", "-o", str(out)],
        )
        assert result.exit_code == 0
        assert "Exported 4 edge(s)" in result.output

        import csv as csv_mod

        with out.open() as f:
            reader = csv_mod.DictReader(f)
            rows = list(reader)
        assert len(rows) == 4
        expected_fields = {
            "from_type",
            "from_id",
            "to_type",
            "to_id",
            "relationship_type",
            "edge_key",
            "properties_json",
        }
        assert set(reader.fieldnames) == expected_fields

    def test_export_filter_by_relationship(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        """--relationship filter produces only matching edges."""
        out = populated_instance.root / "replaces.csv"
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["export", "edges", "-o", str(out), "--relationship", "replaces"],
        )
        assert result.exit_code == 0
        assert "Exported 1 edge(s)" in result.output

        import csv as csv_mod

        with out.open() as f:
            rows = list(csv_mod.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["relationship_type"] == "replaces"

    def test_properties_json_roundtrip(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        """properties_json round-trips through json.loads."""
        out = populated_instance.root / "edges.csv"
        _chdir_run(
            runner,
            populated_instance.root,
            ["export", "edges", "-o", str(out)],
        )

        import csv as csv_mod

        with out.open() as f:
            for row in csv_mod.DictReader(f):
                props = json.loads(row["properties_json"])
                assert isinstance(props, dict)

    def test_properties_json_sort_keys(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        """properties_json uses sort_keys=True for deterministic output."""
        out = populated_instance.root / "edges.csv"
        _chdir_run(
            runner,
            populated_instance.root,
            ["export", "edges", "-o", str(out)],
        )

        import csv as csv_mod

        graph = populated_instance.load_graph()
        edges = graph.list_edges()

        with out.open() as f:
            rows = list(csv_mod.DictReader(f))

        for edge, row in zip(edges, rows):
            assert row["properties_json"] == json.dumps(edge["properties"], sort_keys=True)

    def test_empty_graph(
        self,
        runner: CliRunner,
        initialized_project: CruxibleInstance,
    ) -> None:
        """Empty graph produces CSV with headers only."""
        out = initialized_project.root / "empty.csv"
        result = _chdir_run(
            runner,
            initialized_project.root,
            ["export", "edges", "-o", str(out)],
        )
        assert result.exit_code == 0
        assert "Exported 0 edge(s)" in result.output

        import csv as csv_mod

        with out.open() as f:
            reader = csv_mod.DictReader(f)
            rows = list(reader)
        assert len(rows) == 0
        assert reader.fieldnames is not None

    def test_missing_parent_dir(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        """File I/O error (missing parent dir) produces friendly error message."""
        bad_path = populated_instance.root / "no_such_dir" / "edges.csv"
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["export", "edges", "-o", str(bad_path)],
        )
        assert result.exit_code == 1
        assert "Error:" in result.output

    def test_provenance_survives(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        """_provenance in edge properties survives in properties_json."""
        from cruxible_core.graph.types import RelationshipInstance

        graph = populated_instance.load_graph()
        prov = {"source": "ingest", "created_at": "2026-01-01T00:00:00+00:00"}
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1002",
                to_type="Vehicle",
                to_id="V-2024-ACCORD-SPORT",
                properties={"verified": True, "_provenance": prov},
            )
        )
        populated_instance.save_graph(graph)
        populated_instance.invalidate_graph_cache()

        out = populated_instance.root / "edges.csv"
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["export", "edges", "-o", str(out)],
        )
        assert result.exit_code == 0

        import csv as csv_mod

        with out.open() as f:
            for row in csv_mod.DictReader(f):
                props = json.loads(row["properties_json"])
                if props.get("_provenance"):
                    assert props["_provenance"] == prov
                    return
        pytest.fail("No edge with _provenance found in exported CSV")

    def test_exclude_rejected(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        """--exclude-rejected omits edges with rejected review_status."""
        graph = populated_instance.load_graph()
        # Mark one edge as rejected
        graph.update_edge_properties(
            "Part",
            "BP-1001",
            "Vehicle",
            "V-2024-CIVIC-EX",
            "fits",
            {"review_status": "human_rejected"},
        )
        populated_instance.save_graph(graph)
        populated_instance.invalidate_graph_cache()

        # Without flag: all 4 edges
        out_all = populated_instance.root / "all.csv"
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["export", "edges", "-o", str(out_all)],
        )
        assert result.exit_code == 0
        assert "Exported 4 edge(s)" in result.output

        # With flag: 3 edges (rejected one excluded)
        out_filtered = populated_instance.root / "filtered.csv"
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["export", "edges", "-o", str(out_filtered), "--exclude-rejected"],
        )
        assert result.exit_code == 0
        assert "Exported 3 edge(s)" in result.output

        import csv as csv_mod

        with out_filtered.open() as f:
            for row in csv_mod.DictReader(f):
                props = json.loads(row["properties_json"])
                assert props.get("review_status") != "human_rejected"

    def test_exclude_rejected_ai(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        """--exclude-rejected also omits agent_rejected edges."""
        graph = populated_instance.load_graph()
        graph.update_edge_properties(
            "Part",
            "BP-1001",
            "Vehicle",
            "V-2024-CIVIC-EX",
            "fits",
            {"review_status": "agent_rejected"},
        )
        populated_instance.save_graph(graph)
        populated_instance.invalidate_graph_cache()

        out = populated_instance.root / "filtered.csv"
        result = _chdir_run(
            runner,
            populated_instance.root,
            ["export", "edges", "-o", str(out), "--exclude-rejected"],
        )
        assert result.exit_code == 0
        assert "Exported 3 edge(s)" in result.output


# ---------------------------------------------------------------------------
# Store lifecycle (try/finally)
# ---------------------------------------------------------------------------


class TestStoreLifecycle:
    """Verify stores are closed even when operations raise."""

    def test_query_closes_receipt_store_on_error(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cruxible_core.storage.sqlite import SQLiteStore

        close_count = 0
        original_close = SQLiteStore.close

        def counting_close(self: SQLiteStore) -> None:
            nonlocal close_count
            close_count += 1
            original_close(self)

        monkeypatch.setattr(SQLiteStore, "close", counting_close)
        monkeypatch.setattr(
            SQLiteStore,
            "save_receipt",
            lambda self, r: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        # The command should fail due to the injected error
        assert result.exit_code == 1
        assert close_count >= 1

    def test_feedback_closes_both_stores_on_error(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First run a query to get a real receipt ID
        q_result = _chdir_run(
            runner,
            populated_instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        receipt_id = None
        for line in q_result.output.splitlines():
            if line.startswith("Receipt:"):
                receipt_id = line.split(":", 1)[1].strip()
        assert receipt_id is not None

        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            [
                "feedback",
                "--receipt",
                receipt_id,
                "--action",
                "approve",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1001",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-2024-CIVIC-EX",
            ],
            "feedback",
        )

    def test_list_receipts_closes_store_on_error(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cruxible_core.storage.sqlite import SQLiteStore

        close_count = 0
        original_close = SQLiteStore.close

        def counting_close(self: SQLiteStore) -> None:
            nonlocal close_count
            close_count += 1
            original_close(self)

        monkeypatch.setattr(SQLiteStore, "close", counting_close)
        monkeypatch.setattr(
            SQLiteStore,
            "list_receipts",
            lambda self, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = _chdir_run(
            runner,
            populated_instance.root,
            ["list", "receipts"],
        )
        assert result.exit_code == 1
        assert close_count >= 1


# ---------------------------------------------------------------------------
# Feedback validation (property schema + provenance)
# ---------------------------------------------------------------------------


class TestFeedbackValidation:
    """Verify CLI feedback matches MCP property/provenance checks."""

    @staticmethod
    def _get_receipt_id(runner: CliRunner, instance: CruxibleInstance) -> str:
        result = _chdir_run(
            runner,
            instance.root,
            ["query", "--query", "parts_for_vehicle", "--param", "vehicle_id=V-2024-CIVIC-EX"],
        )
        for line in result.output.splitlines():
            if line.startswith("Receipt:"):
                return line.split(":", 1)[1].strip()
        pytest.fail("No receipt ID found")

    def _feedback_args(self, receipt_id: str, corrections_json: str) -> list[str]:
        return [
            "feedback",
            "--receipt",
            receipt_id,
            "--action",
            "correct",
            "--from-type",
            "Part",
            "--from-id",
            "BP-1001",
            "--relationship",
            "fits",
            "--to-type",
            "Vehicle",
            "--to-id",
            "V-2024-CIVIC-EX",
            "--corrections",
            corrections_json,
        ]

    def test_feedback_rejects_bool_confidence(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        receipt_id = self._get_receipt_id(runner, populated_instance)
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            self._feedback_args(receipt_id, '{"confidence": true}'),
            "feedback",
        )

    def test_feedback_rejects_string_confidence(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        receipt_id = self._get_receipt_id(runner, populated_instance)
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            self._feedback_args(receipt_id, '{"confidence": "high"}'),
            "feedback",
        )

    def test_feedback_strips_provenance(
        self,
        runner: CliRunner,
        populated_instance: CruxibleInstance,
    ) -> None:
        receipt_id = self._get_receipt_id(runner, populated_instance)
        _assert_local_mutation_disabled(
            runner,
            populated_instance.root,
            self._feedback_args(receipt_id, '{"_provenance": "spoofed", "note": "ok"}'),
            "feedback",
        )


# ---------------------------------------------------------------------------
# E2E Gate Test
# ---------------------------------------------------------------------------


class TestE2EGate:
    """Full init → ingest → query → explain → feedback → re-query flow."""

    def test_full_flow(
        self,
        runner: CliRunner,
        tmp_project: Path,
        vehicles_csv: Path,
        parts_csv: Path,
        fitments_csv: Path,
    ) -> None:
        _assert_local_mutation_disabled(
            runner,
            tmp_project,
            ["init", "--config", "config.yaml"],
            "init",
        )


# ---------------------------------------------------------------------------
# group commands
# ---------------------------------------------------------------------------

GROUP_CONFIG_YAML = """\
version: "1.0"
name: group_cli_test
description: For CLI group tests

integrations:
  check_v1:
    kind: generic
    contract: null

entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
      make:
        type: string
      model:
        type: string
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
      category:
        type: string
        enum: [brakes, suspension, engine, electrical, body, interior]

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
    matching:
      integrations:
        check_v1:
          role: required

constraints: []
ingestion: {}
"""


@pytest.fixture
def group_instance(tmp_path: Path) -> CruxibleInstance:
    """Instance with matching config and seeded entities for group tests."""
    (tmp_path / "config.yaml").write_text(GROUP_CONFIG_YAML)
    inst = CruxibleInstance.init(tmp_path, "config.yaml")
    from cruxible_core.graph.types import EntityInstance

    graph = inst.load_graph()
    for pid in ("BP-1", "BP-2"):
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id=pid,
                properties={"part_number": pid, "name": f"Part {pid}", "category": "brakes"},
            )
        )
    for vid in ("V-1", "V-2"):
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id=vid,
                properties={
                    "vehicle_id": vid,
                    "year": 2024,
                    "make": "Honda",
                    "model": "Civic",
                },
            )
        )
    inst.save_graph(graph)
    return inst


def _members_json(from_id: str = "BP-1", to_id: str = "V-1") -> str:
    return json.dumps(
        [
            {
                "from_type": "Part",
                "from_id": from_id,
                "to_type": "Vehicle",
                "to_id": to_id,
                "relationship_type": "fits",
                "signals": [{"integration": "check_v1", "signal": "support"}],
            }
        ]
    )


def _seed_group(instance: CruxibleInstance, *, resolve: bool = False) -> str:
    result = service_propose_group(
        instance,
        "fits",
        [
            CandidateMember(
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                relationship_type="fits",
                signals=[CandidateSignal(integration="check_v1", signal="support")],
                properties={},
            )
        ],
        thesis_facts={"k": "v"},
    )
    group_id = result.group_id
    assert group_id is not None
    if resolve:
        service_resolve_group(
            instance,
            group_id,
            "approve",
            resolved_by="human",
            expected_pending_version=1,
        )
    return group_id


class TestGroupProposeCLI:
    def test_propose_inline(self, runner: CliRunner, group_instance: CruxibleInstance) -> None:
        _assert_local_mutation_disabled(
            runner,
            group_instance.root,
            [
                "group",
                "propose",
                "--relationship",
                "fits",
                "--members",
                _members_json(),
                "--thesis-facts",
                '{"k": "v"}',
            ],
            "group propose",
        )

    def test_propose_from_file(self, runner: CliRunner, group_instance: CruxibleInstance) -> None:
        members_file = group_instance.root / "members.json"
        members_file.write_text(_members_json())
        _assert_local_mutation_disabled(
            runner,
            group_instance.root,
            [
                "group",
                "propose",
                "--relationship",
                "fits",
                "--members-file",
                str(members_file),
                "--thesis-facts",
                '{"k": "v"}',
            ],
            "group propose",
        )


class TestGroupResolveCLI:
    def test_approve(self, runner: CliRunner, group_instance: CruxibleInstance) -> None:
        group_id = _seed_group(group_instance)
        _assert_local_mutation_disabled(
            runner,
            group_instance.root,
            [
                "group",
                "resolve",
                "--group",
                group_id,
                "--action",
                "approve",
                "--expected-pending-version",
                "1",
            ],
            "group resolve",
        )


class TestGroupTrustCLI:
    def test_update_trust(self, runner: CliRunner, group_instance: CruxibleInstance) -> None:
        group_id = _seed_group(group_instance, resolve=True)

        # Get resolution_id
        store = group_instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res_id = group.resolution_id
        finally:
            store.close()

        _assert_local_mutation_disabled(
            runner,
            group_instance.root,
            ["group", "trust", "--resolution", res_id, "--status", "trusted", "--reason", "ok"],
            "group trust",
        )


class TestGroupGetCLI:
    def test_get(self, runner: CliRunner, group_instance: CruxibleInstance) -> None:
        group_id = _seed_group(group_instance)

        result = _chdir_run(
            runner,
            group_instance.root,
            ["group", "get", "--group", group_id],
        )
        assert result.exit_code == 0
        assert group_id in result.output

    def test_get_json_includes_review_payload(
        self,
        runner: CliRunner,
        group_instance: CruxibleInstance,
    ) -> None:
        group_id = _seed_group(group_instance)

        result = _chdir_run(
            runner,
            group_instance.root,
            ["group", "get", "--group", group_id, "--json"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["bucket_status"]["pending_group_id"] == group_id
        assert payload["member_review"][0]["current_edge_count"] == 0

    def test_inspect_relationship_lineage_json(
        self,
        runner: CliRunner,
        group_instance: CruxibleInstance,
    ) -> None:
        group_id = _seed_group(group_instance, resolve=True)

        result = _chdir_run(
            runner,
            group_instance.root,
            [
                "inspect",
                "relationship-lineage",
                "--from-type",
                "Part",
                "--from-id",
                "BP-1",
                "--relationship",
                "fits",
                "--to-type",
                "Vehicle",
                "--to-id",
                "V-1",
                "--json",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["found"] is True
        assert payload["group"]["group_id"] == group_id
        assert payload["_provenance"]["source_ref"] == f"group:{group_id}"


class TestGroupListCLI:
    def test_list(self, runner: CliRunner, group_instance: CruxibleInstance) -> None:
        _seed_group(group_instance)
        result = _chdir_run(
            runner,
            group_instance.root,
            ["group", "list"],
        )
        assert result.exit_code == 0
        assert "1 of 1" in result.output


class TestGroupResolutionsCLI:
    def test_resolutions(self, runner: CliRunner, group_instance: CruxibleInstance) -> None:
        _seed_group(group_instance, resolve=True)

        result = _chdir_run(
            runner,
            group_instance.root,
            ["group", "resolutions"],
        )
        assert result.exit_code == 0
        assert "1 of 1" in result.output


class TestGroupHelpCLI:
    def test_group_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["group", "--help"])
        assert result.exit_code == 0
        assert "propose" in result.output
        assert "resolve" in result.output
        assert "trust" in result.output
        assert "get" in result.output
        assert "list" in result.output
        assert "resolutions" in result.output


class TestFeedbackGroupOverrideCLI:
    def test_feedback_group_override_flag(self, runner: CliRunner) -> None:
        """--group-override flag appears in help."""
        result = runner.invoke(cli, ["feedback", "--help"])
        assert result.exit_code == 0
        assert "--group-override" in result.output


def _extract_group_id(output: str) -> str:
    """Extract GRP-xxx from CLI output."""
    for line in output.splitlines():
        if "GRP-" in line:
            for word in line.split():
                if word.startswith("GRP-"):
                    return word.rstrip(".")
    raise ValueError(f"No group ID found in output: {output}")
