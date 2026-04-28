from __future__ import annotations

from pathlib import Path

from cruxible_core.config.loader import load_config_from_string
from cruxible_core.config_views import (
    DEFAULT_VIEW_ORDER,
    load_config_for_rendering,
    update_readme_file,
)


def test_update_readme_replaces_empty_marker_block(
    tmp_path: Path,
    proposal_workflow_config_yaml: str,
) -> None:
    config = load_config_from_string(proposal_workflow_config_yaml)
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Demo\n\n"
        "<!-- CRUXIBLE:BEGIN ontology -->\n"
        "<!-- CRUXIBLE:END ontology -->\n"
    )

    update_readme_file(readme, config, ("ontology",))

    updated = readme.read_text()
    assert "<!-- CRUXIBLE:BEGIN ontology -->" in updated
    assert "<!-- CRUXIBLE:END ontology -->" in updated
    assert "```mermaid" in updated
    assert "Recommended For" in updated
    assert "stroke:#e74c3c" in updated


def test_update_readme_splits_large_sections_into_titled_blocks(
    tmp_path: Path,
    proposal_workflow_config_yaml: str,
) -> None:
    config = load_config_from_string(proposal_workflow_config_yaml)
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Demo\n\n"
        "<!-- CRUXIBLE:BEGIN workflow-steps -->\n"
        "<!-- CRUXIBLE:END workflow-steps -->\n"
        "\n\n"
        "<!-- CRUXIBLE:BEGIN queries -->\n"
        "<!-- CRUXIBLE:END queries -->\n"
    )

    update_readme_file(readme, config, ("workflow-steps", "queries"))

    updated = readme.read_text()
    assert "### Propose Campaign Recommendations" in updated
    assert "### Get Campaign Context" in updated
    assert updated.count("```mermaid") == 2


def test_update_readme_default_sections_are_comprehension_views(
    tmp_path: Path,
    proposal_workflow_config_yaml: str,
) -> None:
    config = load_config_from_string(proposal_workflow_config_yaml)
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Demo\n\n"
        "<!-- CRUXIBLE:BEGIN ontology -->\n"
        "<!-- CRUXIBLE:END ontology -->\n\n"
        "<!-- CRUXIBLE:BEGIN workflow-pipeline -->\n"
        "<!-- CRUXIBLE:END workflow-pipeline -->\n\n"
        "<!-- CRUXIBLE:BEGIN workflow-summary -->\n"
        "<!-- CRUXIBLE:END workflow-summary -->\n\n"
        "<!-- CRUXIBLE:BEGIN governance-table -->\n"
        "<!-- CRUXIBLE:END governance-table -->\n\n"
        "<!-- CRUXIBLE:BEGIN query-map -->\n"
        "<!-- CRUXIBLE:END query-map -->\n\n"
        "<!-- CRUXIBLE:BEGIN query-catalog -->\n"
        "<!-- CRUXIBLE:END query-catalog -->\n"
    )

    update_readme_file(readme, config, DEFAULT_VIEW_ORDER)

    updated = readme.read_text()
    assert "Recommended For" in updated
    assert "Governed proposal" in updated
    assert "### 1. Propose Campaign Recommendations" in updated
    assert "**Input context**" in updated
    assert "**Result**" in updated
    assert "**Provider source**" in updated
    assert (
        "tests/support/workflow_test_providers.py::campaign_recommendations"
        in updated
    )
    assert (
        "| Relationship | Scope | Signals | Auto-resolve Gate | "
        "Review Policy | Feedback | Outcomes |"
    ) in updated
    assert "query_entity_Campaign" in updated
    assert "### Campaign" in updated


def test_utility_workflows_stay_out_of_main_pipeline(
    tmp_path: Path,
    proposal_workflow_config_yaml: str,
) -> None:
    config_yaml = proposal_workflow_config_yaml.replace(
        "\ntests:\n",
        """
  parse_campaign_notes:
    contract_in: CampaignInput
    steps:
      - id: markdown
        provider: campaign_recommendations
        input:
          campaign_id: $input.campaign_id
          region: west
        as: markdown
    returns: markdown

tests:
""",
    )
    config = load_config_from_string(config_yaml)
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Demo\n\n"
        "<!-- CRUXIBLE:BEGIN workflow-pipeline -->\n"
        "<!-- CRUXIBLE:END workflow-pipeline -->\n\n"
        "<!-- CRUXIBLE:BEGIN workflow-summary -->\n"
        "<!-- CRUXIBLE:END workflow-summary -->\n"
    )

    update_readme_file(readme, config, ("workflow-pipeline", "workflow-summary"))

    updated = readme.read_text()
    pipeline = updated.split("<!-- CRUXIBLE:END workflow-pipeline -->", 1)[0]
    assert "Parse Campaign Notes" not in pipeline
    assert "### 2. Parse Campaign Notes" in updated
    assert "**Role:** Utility" in updated
    assert "Provider output: Campaign Recommendations" in updated


def test_load_config_for_rendering_composes_extends(tmp_path: Path) -> None:
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
workflows:
  build_reference:
    canonical: true
    contract_in: EmptyInput
    returns: EmptyOutput
    steps: []
contracts:
  EmptyInput:
    fields: {}
  EmptyOutput:
    fields: {}
"""
    )
    overlay.write_text(
        """\
version: "1.0"
name: fork
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
contracts:
  AssetProductOutput:
    fields: {}
workflows:
  propose_asset_products:
    canonical: false
    contract_in: EmptyInput
    returns: AssetProductOutput
    steps: []
"""
    )

    composed = load_config_for_rendering(overlay)

    assert sorted(composed.entity_types) == ["Asset", "Product"]
    assert sorted(composed.workflows) == ["build_reference", "propose_asset_products"]


def test_load_config_for_rendering_runtime_strips_upstream_workflows(
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
    returns: EmptyOutput
    steps:
      - id: load
        provider: load_reference
        input: {}
        as: loaded
"""
    )
    overlay.write_text(
        """\
version: "1.0"
name: fork
extends: base.yaml
entity_types:
  Asset:
    properties:
      asset_id:
        type: string
        primary_key: true
relationships: []
workflows:
  build_fork:
    canonical: true
    contract_in: EmptyInput
    returns: EmptyOutput
    steps: []
"""
    )

    composed = load_config_for_rendering(overlay, runtime=True)

    assert sorted(composed.entity_types) == ["Asset", "Product"]
    assert sorted(composed.workflows) == ["build_fork"]
    assert "load_reference" not in composed.providers
