from __future__ import annotations

from pathlib import Path

from cruxible_core.canonical_views.config import (
    DEFAULT_VIEW_ORDER,
    load_config_for_rendering,
    render_config_views,
    update_readme_file,
)
from cruxible_core.config.loader import load_config_from_string


def test_update_readme_replaces_empty_marker_block(
    tmp_path: Path,
    proposal_workflow_config_yaml: str,
) -> None:
    config = load_config_from_string(proposal_workflow_config_yaml)
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Demo\n\n<!-- CRUXIBLE:BEGIN ontology -->\n<!-- CRUXIBLE:END ontology -->\n"
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
        "<!-- CRUXIBLE:BEGIN signal-policy-catalog -->\n"
        "<!-- CRUXIBLE:END signal-policy-catalog -->\n\n"
        "<!-- CRUXIBLE:BEGIN query-map -->\n"
        "<!-- CRUXIBLE:END query-map -->\n\n"
        "<!-- CRUXIBLE:BEGIN query-catalog -->\n"
        "<!-- CRUXIBLE:END query-catalog -->\n"
        "\n\n"
        "<!-- CRUXIBLE:BEGIN quality-rules -->\n"
        "<!-- CRUXIBLE:END quality-rules -->\n\n"
        "<!-- CRUXIBLE:BEGIN learning-loops -->\n"
        "<!-- CRUXIBLE:END learning-loops -->\n"
    )

    update_readme_file(readme, config, DEFAULT_VIEW_ORDER)

    updated = readme.read_text()
    assert "Recommended For" in updated
    assert "Governed proposal" in updated
    assert "### 1. Propose Campaign Recommendations" in updated
    assert "**Input context**" in updated
    assert "**Result**" in updated
    assert "**Provider source**" in updated
    assert "tests/support/workflow_test_providers.py::campaign_recommendations" in updated
    assert (
        "| Relationship | Scope | Creation Path | Signals | Auto-resolve Gate | "
        "Review Policy | Feedback | Outcomes |"
    ) in updated
    assert "Workflow: Propose Campaign Recommendations" in updated
    assert "| Signal Source | Role | Review Unsure | Used By | Notes |" in updated
    assert "No configured constraints." in updated
    assert "No configured feedback profiles." in updated
    assert "query_entity_Campaign" in updated


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


def test_config_owned_operational_sections_render() -> None:
    config = load_config_from_string(
        """\
version: "1.0"
name: operational_sections

entity_types:
  Asset:
    properties:
      asset_id:
        type: string
        primary_key: true
      hostname:
        type: string
  Product:
    properties:
      product_id:
        type: string
        primary_key: true
  VulnerabilityClass:
    properties:
      class_id:
        type: string
        primary_key: true
  Vulnerability:
    properties:
      cve_id:
        type: string
        primary_key: true

relationships:
  - name: asset_runs_product
    from: Asset
    to: Product
    proposal_policy:
      signals:
        product_match:
          role: required
  - name: asset_reviewed_for_product
    from: Asset
    to: Product
    proposal_policy:
      signals:
        review_signal:
          role: advisory
  - name: asset_remediated_vulnerability
    from: Asset
    to: Vulnerability
    proposal_policy:
        signals:
          remediation_verification:
            role: required
            note: Verify remediation evidence.
  - name: vulnerability_classified_as
    from: Vulnerability
    to: VulnerabilityClass
    proposal_policy:
      signals:
        classification_signal:
          role: required

named_queries:
  asset_review:
    mode: collection
    returns: Asset
    result_shape: entity

contracts:
  EmptyInput:
    fields: {}

constraints:
  - name: supported_products_only
    rule: asset_runs_product.to must be a known product
    severity: warning
    description: Prevents unmanaged products from entering the graph.

quality_checks:
  - name: assets_have_hostname
    kind: property
    target: entity
    entity_type: Asset
    property: hostname
    rule: non_empty
    severity: warning
  - name: remediation_has_sources
    kind: json_content
    target: relationship
    relationship_type: asset_remediated_vulnerability
    property: evidence
    rule: required_nested_keys
    keys: [source]
    match: any
    severity: error
  - name: unique_asset_hostname
    kind: uniqueness
    entity_type: Asset
    properties: [hostname]
    severity: warning
  - name: minimum_assets
    kind: bounds
    target: entity_count
    entity_type: Asset
    min_count: 1
    severity: warning
  - name: assets_have_products
    kind: cardinality
    entity_type: Asset
    relationship_type: asset_runs_product
    direction: outgoing
    min_count: 1
    max_count: 20
    severity: error

feedback_profiles:
  asset_runs_product:
    version: 1
    reason_codes:
      wrong_product_match:
        description: The matched product is wrong.
        remediation_hint: provider_fix
        required_scope_keys: [asset, product]
    scope_keys:
      asset: FROM.asset_id
      product: TO.product_id
  asset_remediated_vulnerability:
    version: 1
    reason_codes:
      remediation_not_verified:
        description: Remediation evidence is insufficient.
        remediation_hint: quality_check
        required_scope_keys: [asset]
    scope_keys:
      asset: FROM.asset_id
  vulnerability_classified_as:
    version: 1
    reason_codes:
      wrong_classification:
        description: The vulnerability was assigned to the wrong class.
        remediation_hint: decision_policy
        required_scope_keys: [class]
    scope_keys:
      class: TO.class_id

outcome_profiles:
  asset_runs_product_resolution:
    anchor_type: resolution
    relationship_type: asset_runs_product
    version: 1
    outcome_codes:
      wrong_product_match:
        description: The resolved product was wrong.
        remediation_hint: trust_adjustment
        required_scope_keys: [relationship_type]
    scope_keys:
      relationship_type: RESOLUTION.relationship_type
  asset_remediated_resolution:
    anchor_type: resolution
    relationship_type: asset_remediated_vulnerability
    version: 1
    outcome_codes:
      false_remediation:
        description: The asset was still vulnerable.
        remediation_hint: require_review
        required_scope_keys: [relationship_type]
    scope_keys:
      relationship_type: RESOLUTION.relationship_type
  vulnerability_classification_resolution:
    anchor_type: resolution
    relationship_type: vulnerability_classified_as
    version: 1
    outcome_codes:
      wrong_classification:
        description: The vulnerability was assigned to the wrong class.
        remediation_hint: provider_fix
        required_scope_keys: [relationship_type]
    scope_keys:
      relationship_type: RESOLUTION.relationship_type
  asset_review_query:
    anchor_type: receipt
    surface_type: query
    surface_name: asset_review
    version: 1
    outcome_codes:
      missing_review_context:
        description: The query missed relevant asset context.
        remediation_hint: graph_fix
        required_scope_keys: [surface]
    scope_keys:
      surface: SURFACE.name

workflows:
  propose_asset_products:
    type: proposal
    contract_in: EmptyInput
    steps:
      - id: proposal
        propose_relationship_group:
          relationship_type: asset_runs_product
          candidates_from: candidates
          signals_from: [signals]
        as: proposal
    returns: proposal
"""
    )

    rendered = render_config_views(config, view="all")

    assert "| Relationship | Scope | Creation Path | Signals |" in rendered
    assert "Workflow: Propose Asset Products" in rendered
    assert "Agent/manual group propose" in rendered
    assert "| Signal Source | Role | Review Unsure | Used By | Notes |" in rendered
    assert "`remediation_verification`" in rendered
    assert "Verify remediation evidence." in rendered
    assert "### Constraints" in rendered
    assert "`supported_products_only`" in rendered
    assert "`assets_have_hostname`" in rendered
    assert "`remediation_has_sources`" in rendered
    assert "`unique_asset_hostname`" in rendered
    assert "`minimum_assets`" in rendered
    assert "`assets_have_products`" in rendered
    assert "#### `asset_remediated_vulnerability`" in rendered
    assert "#### `vulnerability_classified_as`" in rendered
    assert "##### `asset_remediated_resolution`" in rendered
    assert "##### `vulnerability_classification_resolution`" in rendered
    assert "##### `asset_review_query`" in rendered


def test_schema_catalog_stays_available_as_explicit_view(
    proposal_workflow_config_yaml: str,
) -> None:
    config = load_config_from_string(proposal_workflow_config_yaml)

    rendered = render_config_views(config, view="schema-catalog")

    assert "### Entity Types" in rendered
    assert "#### `Campaign`" in rendered


def test_load_config_for_rendering_composes_extends(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    overlay = tmp_path / "overlay.yaml"
    base.write_text(
        """\
version: "1.0"
name: base
entity_types:
  Product:
    properties:
      product_id:
        type: string
        primary_key: true
relationships: []
workflows:
  build_reference:
    type: canonical
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
    proposal_policy:
      signals: {}
contracts:
  AssetProductOutput:
    fields: {}
workflows:
  propose_asset_products:
    type: utility
    contract_in: EmptyInput
    returns: asset_products
    steps:
      - id: asset_products
        make_candidates:
          relationship_type: asset_runs_product
          items: []
          from_type: Asset
          from_id: $item.asset_id
          to_type: Product
          to_id: $item.product_id
          properties: {}
        as: asset_products
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
    type: canonical
    contract_in: EmptyInput
    returns: apply_products
    steps:
      - id: load
        provider: load_reference
        input: {}
        as: loaded
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
relationships: []
workflows:
  build_overlay:
    type: canonical
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

    composed = load_config_for_rendering(overlay, runtime=True)

    assert sorted(composed.entity_types) == ["Asset", "Product"]
    assert sorted(composed.workflows) == ["build_overlay"]
    assert "load_reference" not in composed.providers
