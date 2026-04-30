"""Shared test fixtures for cruxible-core."""

import hashlib
import json
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance

WORKFLOW_CONFIG_YAML = """\
version: "1.0"
name: promo_workflows

entity_types:
  Product:
    properties:
      sku:
        type: string
        primary_key: true
      category:
        type: string
      base_margin:
        type: float
        optional: true

relationships: []

named_queries:
  get_promo_context:
    entry_point: Product
    traversal: []
    returns: "list[Product]"

contracts:
  PromoInput:
    fields:
      sku:
        type: string
      start_date:
        type: date
      end_date:
        type: date
  PromoContext:
    fields:
      sku:
        type: string
      category:
        type: string
      start_date:
        type: date
      end_date:
        type: date
  LiftForecast:
    fields:
      predicted_lift_pct:
        type: float
      confidence_lower:
        type: float
      confidence_upper:
        type: float
      model_version:
        type: string
  MarginResult:
    fields:
      expected_margin_pct:
        type: float
      decision:
        type: string
      calculator_version:
        type: string

artifacts:
  promo_model:
    kind: model
    uri: file:///tmp/promo-model.bin
    sha256: abc123

providers:
  lift_predictor:
    kind: model
    contract_in: PromoContext
    contract_out: LiftForecast
    ref: tests.support.workflow_test_providers.lift_predictor
    version: 1.2.0
    deterministic: true
    artifact: promo_model
    runtime: python
  margin_calculator:
    kind: function
    contract_in: LiftForecast
    contract_out: MarginResult
    ref: tests.support.workflow_test_providers.margin_calculator
    version: 1.0.0
    deterministic: true
    runtime: python

workflows:
  evaluate_promo:
    contract_in: PromoInput
    steps:
      - id: context
        query: get_promo_context
        params:
          sku: $input.sku
        as: context
      - id: lift
        provider: lift_predictor
        input:
          sku: $steps.context.results[0].entity_id
          category: $steps.context.results[0].properties.category
          start_date: $input.start_date
          end_date: $input.end_date
        as: lift
      - id: margin
        provider: margin_calculator
        input:
          predicted_lift_pct: $steps.lift.predicted_lift_pct
          confidence_lower: $steps.lift.confidence_lower
          confidence_upper: $steps.lift.confidence_upper
          model_version: $steps.lift.model_version
        as: margin
      - id: margin_gate
        assert:
          left: $steps.margin.expected_margin_pct
          op: gte
          right: 0.05
          message: Margin below threshold
    returns: margin

tests:
  - name: promo_margin_smoke
    workflow: evaluate_promo
    input:
      sku: SKU-123
      start_date: "2026-03-01"
      end_date: "2026-03-07"
    expect:
      output_contains:
        decision: approve
      receipt_contains_provider:
        - lift_predictor
        - margin_calculator
"""

PROPOSAL_WORKFLOW_CONFIG_YAML = """\
version: "1.0"
name: campaign_relationship_workflows
kind: world_model

entity_types:
  Campaign:
    properties:
      campaign_id:
        type: string
        primary_key: true
      region:
        type: string
  Product:
    properties:
      sku:
        type: string
        primary_key: true
      category:
        type: string

relationships:
  - name: recommended_for
    from: Campaign
    to: Product
    properties:
      reason:
        type: string
        optional: true
    matching:
      integrations:
        catalog:
          role: required
          always_review_on_unsure: true

named_queries:
  get_campaign_context:
    entry_point: Campaign
    traversal: []
    returns: "list[Campaign]"

contracts:
  CampaignInput:
    fields:
      campaign_id:
        type: string
  CampaignContext:
    fields:
      campaign_id:
        type: string
      region:
        type: string
  RecommendationRows:
    fields:
      items:
        type: json

integrations:
  catalog:
    kind: heuristic
    contract: null

providers:
  campaign_recommendations:
    kind: function
    contract_in: CampaignContext
    contract_out: RecommendationRows
    ref: tests.support.workflow_test_providers.campaign_recommendations
    version: 1.0.0
    deterministic: true
    runtime: python

workflows:
  propose_campaign_recommendations:
    purpose: proposal
    contract_in: CampaignInput
    steps:
      - id: campaign
        query: get_campaign_context
        params:
          campaign_id: $input.campaign_id
        as: campaign
      - id: recommend
        provider: campaign_recommendations
        input:
          campaign_id: $steps.campaign.results[0].entity_id
          region: $steps.campaign.results[0].properties.region
        as: recommendations
      - id: candidates
        make_candidates:
          relationship_type: recommended_for
          items: $steps.recommendations.items
          from_type: Campaign
          from_id: $steps.campaign.results[0].entity_id
          to_type: Product
          to_id: $item.product_sku
          properties:
            reason: $item.reason
        as: candidates
      - id: catalog_signals
        map_signals:
          integration: catalog
          items: $steps.recommendations.items
          from_id: $steps.campaign.results[0].entity_id
          to_id: $item.product_sku
          evidence: $item.reason
          enum:
            path: verdict
            map:
              match: support
              fallback: unsure
              reject: contradict
        as: catalog_signals
      - id: proposal
        propose_relationship_group:
          relationship_type: recommended_for
          candidates_from: candidates
          signals_from:
            - catalog_signals
          thesis_text: Recommend products for regional campaign
          thesis_facts:
            campaign_id: $input.campaign_id
            region: $steps.campaign.results[0].properties.region
          analysis_state:
            source: campaign_recommendations
          suggested_priority: high
        as: proposal
    returns: proposal

tests:
  - name: campaign_proposal_smoke
    workflow: propose_campaign_recommendations
    input:
      campaign_id: CMP-1
    expect:
      output_contains:
        thesis_text: Recommend products for regional campaign
      receipt_contains_provider: campaign_recommendations
"""


@pytest.fixture(autouse=True)
def isolate_cli_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Keep tests isolated from any user-scoped remembered CLI/server context."""

    context_dir = tmp_path_factory.mktemp("cli-context")
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_dir / "client-context.json"))
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_INSTANCE_ID", raising=False)


@pytest.fixture
def configs_dir() -> Path:
    """Path to the configs directory."""
    return Path(__file__).parent.parent / "configs"


@pytest.fixture
def car_parts_config(configs_dir: Path) -> str:
    """Raw YAML string for car parts config."""
    return (configs_dir / "car_parts.yaml").read_text()


@pytest.fixture
def workflow_config_yaml() -> str:
    """Raw YAML string for terraform-primitives workflow tests."""
    return WORKFLOW_CONFIG_YAML


@pytest.fixture
def proposal_workflow_config_yaml() -> str:
    """Raw YAML string for relationship proposal workflow tests."""
    return PROPOSAL_WORKFLOW_CONFIG_YAML


@pytest.fixture
def canonical_workflow_project(tmp_path: Path) -> Path:
    """Project root for canonical preview/apply workflow tests."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    rows = [
        {
            "vendor_id": "vendor-acme",
            "vendor_name": "Acme",
            "product_id": "product-acme-widget",
            "product_name": "Widget",
            "cve_id": "CVE-2026-0001",
            "description": "Widget issue",
        },
        {
            "vendor_id": "vendor-acme",
            "vendor_name": "Acme",
            "product_id": "product-acme-widget-pro",
            "product_name": "Widget Pro",
            "cve_id": "CVE-2026-0002",
            "description": "Widget Pro issue",
        },
    ]
    (bundle_dir / "rows.json").write_text(json.dumps(rows, indent=2, sort_keys=True))
    bundle_sha256 = _compute_directory_sha256(bundle_dir)

    config_yaml = f"""\
version: "1.0"
name: canonical_reference_workflow
kind: world_model

entity_types:
  Vendor:
    properties:
      vendor_id:
        type: string
        primary_key: true
      name:
        type: string
  Product:
    properties:
      product_id:
        type: string
        primary_key: true
      name:
        type: string
  Vulnerability:
    properties:
      cve_id:
        type: string
        primary_key: true
      description:
        type: string

relationships:
  - name: product_from_vendor
    from: Product
    to: Vendor
  - name: vulnerability_affects_product
    from: Vulnerability
    to: Product

named_queries:
  get_vendors:
    entry_point: Vendor
    traversal: []
    returns: "list[Vendor]"

contracts:
  EmptyInput:
    fields: {{}}
  BundleRows:
    fields:
      items:
        type: json

artifacts:
  canonical_bundle:
    kind: directory
    uri: ./bundle
    sha256: {bundle_sha256}

providers:
  reference_loader:
    kind: function
    contract_in: EmptyInput
    contract_out: BundleRows
    ref: tests.support.workflow_test_providers.reference_bundle_loader
    version: 1.0.0
    deterministic: true
    runtime: python
    artifact: canonical_bundle

workflows:
  build_reference:
    canonical: true
    contract_in: EmptyInput
    steps:
      - id: rows
        provider: reference_loader
        input: {{}}
        as: rows
      - id: vendors
        make_entities:
          entity_type: Vendor
          items: $steps.rows.items
          entity_id: $item.vendor_id
          properties:
            vendor_id: $item.vendor_id
            name: $item.vendor_name
        as: vendors
      - id: products
        make_entities:
          entity_type: Product
          items: $steps.rows.items
          entity_id: $item.product_id
          properties:
            product_id: $item.product_id
            name: $item.product_name
        as: products
      - id: vulnerabilities
        make_entities:
          entity_type: Vulnerability
          items: $steps.rows.items
          entity_id: $item.cve_id
          properties:
            cve_id: $item.cve_id
            description: $item.description
        as: vulnerabilities
      - id: product_vendor
        make_relationships:
          relationship_type: product_from_vendor
          items: $steps.rows.items
          from_type: Product
          from_id: $item.product_id
          to_type: Vendor
          to_id: $item.vendor_id
        as: product_vendor
      - id: vulnerability_product
        make_relationships:
          relationship_type: vulnerability_affects_product
          items: $steps.rows.items
          from_type: Vulnerability
          from_id: $item.cve_id
          to_type: Product
          to_id: $item.product_id
        as: vulnerability_product
      - id: apply_vendors
        apply_entities:
          entities_from: vendors
        as: apply_vendors
      - id: apply_products
        apply_entities:
          entities_from: products
        as: apply_products
      - id: apply_vulnerabilities
        apply_entities:
          entities_from: vulnerabilities
        as: apply_vulnerabilities
      - id: apply_product_vendor
        apply_relationships:
          relationships_from: product_vendor
        as: apply_product_vendor
      - id: apply_vulnerability_product
        apply_relationships:
          relationships_from: vulnerability_product
        as: apply_vulnerability_product
      - id: vendors_query
        query: get_vendors
        params:
          vendor_id: vendor-acme
        as: vendors_query
    returns: vendors_query
"""

    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_yaml)
    return tmp_path


@pytest.fixture
def canonical_workflow_instance(canonical_workflow_project: Path) -> CruxibleInstance:
    """Filesystem instance for canonical preview/apply workflow tests."""
    return CruxibleInstance.init(canonical_workflow_project, "config.yaml")


def _compute_directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(child.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(child.read_bytes()).hexdigest().encode())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
