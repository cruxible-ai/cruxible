# KEV Triage

Localable cyber state model for vulnerability and KEV triage.

## Skills

- [skills/kev-start/SKILL.md](skills/kev-start/SKILL.md) — adapt the KEV kit
  to your own asset, inventory, and service-mapping data
- [skills/kev-triage/SKILL.md](skills/kev-triage/SKILL.md) — the packaged
  daily triage / waiver / remediation / control-effectiveness loop

The shipped [`.mcp.json`](.mcp.json) example runs at `CRUXIBLE_MODE=governed_write`,
the least-privilege tier that covers day-to-day proposals, reviews, and
feedback; initial canonical applies such as `build_local_state` need a higher
tier (`graph_write` or `admin`).

## Structure

This demo has two kit directories that represent the two layers:

- **`../kev-reference/config.yaml`** — the published upstream state model. Contains only
  public entity types (Vendor, Product, Vulnerability), deterministic reference
  relationships, plus a canonical workflow that builds accepted reference state
  from the bundled hashed KEV/NVD/EPSS artifact. This is what Cruxible hosts
  and keeps updated from public feeds. Read-only to local instances.

- **`config.yaml`** — a customer local that uses `extends: ../kev-reference/config.yaml`.
  Adds internal entity types, deterministic internal mappings, governed judgment
  relationships, feedback and outcome profiles, quality checks, and named queries
  that traverse across both layers.

Everything between `CRUXIBLE:BEGIN` / `CRUXIBLE:END` markers is regenerated
from `config.yaml` by `cruxible config views --runtime`; treat those
blocks as code-owned structural truth. Everything outside those marker blocks
is authored explanation for humans and agents reading the kit.

## Ontology Map

The runtime composed view includes inherited reference entities and relationships
plus the extension's internal and governed surfaces. Solid blue lines are
deterministic canonical state. Dashed red lines are governed proposal/review
relationships.

<!-- CRUXIBLE:BEGIN ontology -->
```mermaid
flowchart LR
  classDef canonicalEntity fill:#4a90d9,stroke:#2c5f8a,color:#fff
  classDef governedEntity fill:#e67e22,stroke:#a0521c,color:#fff
  classDef baseEntity fill:#e4e4e7,stroke:#a1a1aa,color:#3f3f46,stroke-dasharray: 4 3

  entity_Asset["Asset"]
  entity_BusinessService["Business Service"]
  entity_CompensatingControl["Compensating Control"]
  entity_Exception["Exception"]
  entity_Owner["Owner"]
  entity_PatchWindow["Patch Window"]
  entity_Product["Product"]
  entity_Vulnerability["Vulnerability"]
  entity_VulnerabilityClass["Vulnerability Class"]
  class entity_Asset,entity_BusinessService,entity_CompensatingControl,entity_Exception,entity_Owner,entity_PatchWindow,entity_VulnerabilityClass canonicalEntity
  class entity_Product,entity_Vulnerability baseEntity

  %% Deterministic canonical relationships
  entity_Asset -- "Asset Has Control" --> entity_CompensatingControl
  entity_Asset -- "Asset Has Exception" --> entity_Exception
  entity_Asset -- "Asset Owned By" --> entity_Owner
  entity_Asset -- "Asset Patch Window" --> entity_PatchWindow
  entity_CompensatingControl -- "Control Mitigates Class" --> entity_VulnerabilityClass
  entity_BusinessService -- "Service Depends On Asset" --> entity_Asset

  %% Governed proposal/review relationships
  entity_Asset -. "Asset Patch Exception For" .-> entity_Vulnerability
  entity_Asset -. "Asset Remediated Vulnerability" .-> entity_Vulnerability
  entity_Asset -. "Asset Runs Product" .-> entity_Product
  entity_Asset -. "Asset Vulnerability Posture" .-> entity_Vulnerability
  entity_Vulnerability -. "Vulnerability Classified As" .-> entity_VulnerabilityClass
  linkStyle 0,1,2,3,4,5 stroke:#2c5f8a,stroke-width:2px
  linkStyle 6,7,8,9,10 stroke:#e74c3c,stroke-width:2px
```

**Diagram legend:** blue node = canonical entity (deterministic writes); dashed grey node = base-kit entity shown for seam context; solid edge = deterministic relationship; dotted edge = governed relationship.
<!-- CRUXIBLE:END ontology -->

## Schema Catalog

<!-- CRUXIBLE:BEGIN schema-catalog -->
| Entity | Properties | Description |
| --- | --- | --- |
| `Asset` | `asset_id: string (pk)`, `hostname: string?`, `asset_type: asset_type?`, `criticality: criticality?`, `environment: asset_environment?`, `internet_exposed: bool?` | Internal asset from CMDB, cloud inventory, or endpoint tooling. |
| `BusinessService` | `service_id: string (pk)`, `name: string?`, `criticality: criticality?` | Internal business or technical service depending on assets. |
| `CompensatingControl` | `control_id: string (pk)`, `name: string?`, `control_type: control_type?`, `status: control_status?` | Control that can reduce or block exploitability. |
| `Exception` | `exception_id: string (pk)`, `reason: string?`, `review_due_at: date?`, `status: exception_status?` | Approved patch or remediation exception. |
| `Owner` | `owner_id: string (pk)`, `name: string?`, `team: string?`, `email: string?` | Team or person responsible for an asset or service. |
| `PatchWindow` | `patch_window_id: string (pk)`, `cadence: patch_cadence?`, `next_window_at: datetime?`, `freeze_status: patch_freeze_status?`, `emergency_patch_allowed: bool?`, `outage_allowed: bool?`, `testing_required: bool?`, `rollback_required: bool?`, `owner_id: string?` | Operational patching schedule or change window. |
| `VulnerabilityClass` | `class_id: string (pk)`, `name: string?`, `description: string?`, `attack_vector: attack_vector?` | Operational vulnerability category used by local controls, policy, and scenario analysis. This is local-layer classification rather than reference-layer public data. |

### Enums

| Enum | Values |
| --- | --- |
| `asset_environment` | production, staging, corporate |
| `asset_type` | server, laptop |
| `attack_vector` | network, adjacent, local, physical |
| `control_status` | active, inactive |
| `control_type` | waf, endpoint_detection, network_acl |
| `criticality` | low, medium, high, critical |
| `exception_status` | approved, expired, revoked |
| `patch_cadence` | weekly, biweekly, monthly |
| `patch_freeze_status` | none, partial_freeze |
<!-- CRUXIBLE:END schema-catalog -->

**Legend:** Blue = canonical/deterministic state, including the inherited KEV
reference layer | Orange = governed-only trigger/judgment entities | Solid blue
lines = deterministic | Dashed red lines = governed proposal/review.

## Workflow Summary

The generated pipeline is an inferred dependency ordering, not a guaranteed
onboarding sequence — for the authoritative walkthrough (products before
exposure), see [`docs/kev-guide.md`](../../docs/kev-guide.md). The generated
stage blocks underneath keep long context and provider provenance readable
without squeezing them into a wide table.

<!-- CRUXIBLE:BEGIN workflow-pipeline -->
```mermaid
flowchart LR
  classDef canonicalWorkflow fill:#4a90d9,stroke:#2c5f8a,color:#fff
  classDef governedWorkflow fill:#e67e22,stroke:#a0521c,color:#fff

  workflow_pipeline_build_local_state["1. Seed canonical state<br/>Canonical"]
  workflow_pipeline_propose_asset_exposure["2. Asset Vulnerability Posture<br/>Governed proposal"]
  workflow_pipeline_propose_asset_products["3. Asset Runs Product<br/>Governed proposal"]
  workflow_pipeline_propose_exposure_reconciliation["4. Asset Remediated Vulnerability<br/>Governed proposal"]
  workflow_pipeline_propose_vulnerability_classification["5. Vulnerability Classified As<br/>Governed proposal"]
  workflow_pipeline_build_local_state --> workflow_pipeline_propose_asset_exposure
  workflow_pipeline_propose_asset_exposure --> workflow_pipeline_propose_asset_products
  workflow_pipeline_propose_asset_products --> workflow_pipeline_propose_exposure_reconciliation
  workflow_pipeline_propose_exposure_reconciliation --> workflow_pipeline_propose_vulnerability_classification
  class workflow_pipeline_build_local_state canonicalWorkflow
  class workflow_pipeline_propose_asset_exposure,workflow_pipeline_propose_asset_products,workflow_pipeline_propose_exposure_reconciliation,workflow_pipeline_propose_vulnerability_classification governedWorkflow
```
<!-- CRUXIBLE:END workflow-pipeline -->

<!-- CRUXIBLE:BEGIN workflow-summary -->
### 1. Build Local State

**Role:** Canonical seed

**Input context**
- None (seeds canonical state)

**Result**
- Canonical entities: Asset, Business Service, Compensating Control, Exception, Owner, Patch Window, Vulnerability Class
- Canonical relationships: Asset Has Control, Asset Has Exception, Asset Owned By, Asset Patch Window, Control Mitigates Class, Service Depends On Asset

**Provider source**
- Parse Local Seed Bundle (Python Function, v1.0.0); source: `src/cruxible_core/providers/common/tabular.py::load_tabular_artifact_bundle`; artifact: Local Seed Bundle

### 2. Propose Asset Exposure

**Role:** Governed proposal

**Input context**
- Query context: Asset, Compensating Control, Asset Has Control, Asset Runs Product, Control Mitigates Class, Vulnerability Affects Product, Vulnerability Classified As

**Result**
- Proposed relationships: Asset Vulnerability Posture

**Provider source**
- Assess Asset Affected (Python Function, v1.0.0); source: `kit://providers/assessment.py::assess_asset_affected`
- Assess Asset Exposure (Python Function, v1.0.0); source: `kit://providers/assessment.py::assess_asset_exposure`

### 3. Propose Asset Products

**Role:** Governed proposal

**Input context**
- Query context: Product

**Result**
- Proposed relationships: Asset Runs Product

**Provider source**
- Match Software To Products (Python Function, v1.0.0); source: `kit://providers/matching.py::match_software_to_products`
- Parse Local Seed Bundle (Python Function, v1.0.0); source: `src/cruxible_core/providers/common/tabular.py::load_tabular_artifact_bundle`; artifact: Local Seed Bundle

### 4. Propose Exposure Reconciliation

**Role:** Governed proposal

**Input context**
- Query context: Asset Remediated Vulnerability, Asset Runs Product, Asset Vulnerability Posture, Vulnerability Affects Product

**Result**
- Proposed relationships: Asset Remediated Vulnerability

**Provider source**
- Assess Asset Affected (Python Function, v1.0.0); source: `kit://providers/assessment.py::assess_asset_affected`
- Assess Exposure Reconciliation (Python Function, v1.0.0); source: `kit://providers/assessment.py::assess_exposure_reconciliation`

### 5. Propose Vulnerability Classification

**Role:** Governed proposal

**Input context**
- None

**Result**
- Proposed relationships: Vulnerability Classified As

**Provider source**
- -
<!-- CRUXIBLE:END workflow-summary -->

## Provider Contracts

<!-- CRUXIBLE:BEGIN provider-contracts -->
### `assess_asset_affected` (deterministic)

- Ref: `kit://providers/assessment.py::assess_asset_affected`
- Purpose: Assess whether joined asset-product and vulnerability-product rows place the installed version within the reference-layer affected range.

Called by workflow `propose_asset_exposure`, step `affected_assessments`:

- Input `joined_product_edges` <- join_items step `affected_product_join` (`items`)

Called by workflow `propose_exposure_reconciliation`, step `affected_assessments`:

- Input `joined_product_edges` <- join_items step `reconciliation_product_join` (`items`)

### `assess_asset_exposure` (deterministic)

- Ref: `kit://providers/assessment.py::assess_asset_exposure`
- Purpose: Assess whether an affected asset is materially exposed using internet exposure, environment, criticality, and attached active controls.

Called by workflow `propose_asset_exposure`, step `assessments`:

- Input `affected_asset_context` <- join_items step `affected_asset_context` (`items`)
- Input `active_control_bindings` <- filter_items step `active_control_bindings` (`items`)
- Input `vulnerability_classification_edges` <- query step `vulnerability_classifications` (`results`)
- Input `control_mitigation_edges` <- query step `control_mitigations` (`results`)
- Input `assessment_policy` <- config literal (inline in the workflow step)
- Output rows -> `make_candidates` step `candidates` (`asset_vulnerability_posture`): required row keys: `asset_id` (from id), `cve_id` (to id), `status`, `priority`, `product_id`, `installed_version`, `basis.affected` -> `affected_basis`, `basis.exposure` -> `exposure_basis`, `basis.control` -> `control_basis`.

### `assess_exposure_reconciliation` (deterministic)

- Ref: `kit://providers/assessment.py::assess_exposure_reconciliation`
- Purpose: Compare accepted exposure edges against the current product-derived affected candidate set and identify stale exposures to close.

Called by workflow `propose_exposure_reconciliation`, step `reconciliation`:

- Input `accepted_exposure_edges` <- query step `accepted_exposures` (`results`)
- Input `affected_items` <- dedupe_items step `affected_candidates` (`items`)
- Input `asset_product_edges` <- query step `asset_products` (`results`)
- Input `vulnerability_product_edges` <- query step `vulnerability_products` (`results`)
- Input `remediated_edges` <- query step `remediated_edges` (`results`)
- Output rows -> `make_candidates` step `candidates` (`asset_remediated_vulnerability`): required row keys: `asset_id` (from id), `cve_id` (to id), `remediation_type`, `rationale` -> `verification_basis`.

### `match_software_to_products` (deterministic)

- Ref: `kit://providers/matching.py::match_software_to_products`
- Purpose: Fuzzy match software inventory rows against reference-layer products using CPE name similarity and vendor matching. Returns transient match scores for signal mapping plus qualitative governed relationship rationale.

Called by workflow `propose_asset_products`, step `matches`:

- Input `inventory_items` <- shape_items step `inventory` (`items`)
- Input `reference_products` <- query step `reference_products` (`results`)
- Output rows -> `make_candidates` step `candidates` (`asset_runs_product`): required row keys: `asset_id` (from id), `product_id` (to id), `observed_software_name`, `observed_vendor`, `installed_version`, `inventory_source`, `last_seen_at`, `match_basis`.

### `parse_local_seed_bundle` (deterministic)

- Ref: `cruxible_core.providers.common.tabular.load_tabular_artifact_bundle`
- Reads artifact: `local_seed_bundle` (`kits/kev-triage/data/seed`)
- Purpose: Parse the pinned local seed artifact into generic provenance-rich tabular rows. Domain normalization happens in the next workflow step.

Called by workflow `build_local_state`, step `raw_tables`:

- Input `expected_tables` <- config literal (inline in the workflow step)
- Output rows -> `make_entities` step `assets` (`Asset`): required row keys: `asset_id` (entity id), `hostname`, `asset_type`, `criticality`, `environment`, `internet_exposed`.
- Output rows -> `make_entities` step `services` (`BusinessService`): required row keys: `service_id` (entity id), `name`, `criticality`.
- Output rows -> `make_entities` step `owners` (`Owner`): required row keys: `owner_id` (entity id), `name`, `team`, `email`.
- Output rows -> `make_entities` step `controls` (`CompensatingControl`): required row keys: `control_id` (entity id), `name`, `control_type`, `status`.
- Output rows -> `make_entities` step `vulnerability_classes` (`VulnerabilityClass`): required row keys: `class_id` (entity id), `name`, `description`, `attack_vector`.
- Output rows -> `make_entities` step `exceptions` (`Exception`): required row keys: `exception_id` (entity id), `reason`, `review_due_at`, `status`.
- Output rows -> `make_entities` step `patch_windows` (`PatchWindow`): required row keys: `patch_window_id` (entity id), `cadence`, `next_window_at`, `freeze_status`, `emergency_patch_allowed`, `outage_allowed`, `testing_required`, `rollback_required`, `owner_id`.
- Output rows -> `make_relationships` step `svc_asset_edges` (`service_depends_on_asset`): required row keys: `service_id` (from id), `asset_id` (to id).
- Output rows -> `make_relationships` step `owned_by_edges` (`asset_owned_by`): required row keys: `asset_id` (from id), `owner_id` (to id).
- Output rows -> `make_relationships` step `control_edges` (`asset_has_control`): required row keys: `asset_id` (from id), `control_id` (to id).
- Output rows -> `make_relationships` step `exception_edges` (`asset_has_exception`): required row keys: `asset_id` (from id), `exception_id` (to id).
- Output rows -> `make_relationships` step `patch_window_edges` (`asset_patch_window`): required row keys: `asset_id` (from id), `patch_window_id` (to id).
- Output rows -> `make_relationships` step `control_mitigation_edges` (`control_mitigates_class`): required row keys: `control_id` (from id), `class_id` (to id), `effect`, `validation_basis`, `verified_at`, `expires_at`.

Called by workflow `propose_asset_products`, step `inventory_tables`:

- Input `expected_tables` <- config literal (inline in the workflow step)
<!-- CRUXIBLE:END provider-contracts -->

## Governed Relationships

Each governed relationship has a `proposal_policy` block and signal sources that provide
signals, and linked feedback/outcome profiles for the Loop 1/2 flywheel.

<!-- CRUXIBLE:BEGIN governance-table -->
| Relationship | Scope | Creation Path | Signals | Auto-resolve Gate | Review Policy | Feedback | Outcomes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Asset Patch Exception For | Asset -> Vulnerability | Proposal only (direct write refused) | Policy Review | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 2 reason codes | - |
| Asset Remediated Vulnerability | Asset -> Vulnerability | Proposal only (direct write refused) | Remediation Verification | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 3 reason codes | Asset Remediated Resolution |
| Asset Runs Product | Asset -> Product | Proposal only (direct write refused) | Software Product Match | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 3 reason codes | Asset Runs Product Resolution |
| Asset Vulnerability Posture | Asset -> Vulnerability | Proposal only (direct write refused) | Control Effectiveness, Exploitability Signal, Product Version Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 4 reason codes | Asset Vulnerability Posture Resolution |
| Vulnerability Classified As | Vulnerability -> Vulnerability Class | Proposal only (direct write refused) | Vulnerability Classification | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | 2 reason codes | - |
<!-- CRUXIBLE:END governance-table -->

<!-- CRUXIBLE:BEGIN mutation-guards -->
No mutation guards declared.
<!-- CRUXIBLE:END mutation-guards -->

### Signal Policy Notes

KEV keeps proposal signal policy directly on governed relationships, so the governed relationship table
above is the source of truth for required/advisory signal labels.

<!-- CRUXIBLE:BEGIN signal-policy-catalog -->
| Signal Source | Role | Review Unsure | Evidence on Support | Used By | Notes |
| --- | --- | --- | --- | --- | --- |
| `control_effectiveness` | required | yes | no | Asset Vulnerability Posture | - |
| `exploitability_signal` | required | yes | no | Asset Vulnerability Posture | - |
| `policy_review` | required | no | no | Asset Patch Exception For | - |
| `product_version_evidence` | required | yes | no | Asset Vulnerability Posture | - |
| `remediation_verification` | required | yes | no | Asset Remediated Vulnerability | - |
| `software_product_match` | required | yes | no | Asset Runs Product | - |
| `vulnerability_classification` | required | yes | no | Vulnerability Classified As | - |
<!-- CRUXIBLE:END signal-policy-catalog -->

## Query Catalog

Use the catalog to decide which KEV surfaces survive onboarding for a user's
data. Composition, presentation, and operator summaries should happen in the
skill or agent harness, not by turning every useful traversal into a governed
relationship.

<!-- CRUXIBLE:BEGIN query-catalog -->
### Collection Query

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Asset Vulnerability Postures Requiring Action | collection | Asset Vulnerability Posture | reviewable |  | Return existing asset-vulnerability posture relationships that represent exposed work needing attention. This is a work-queue read surface for current posture facts, not candidate discovery. |

### Compensating Control

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Control Coverage Gap | traversal | Business Service | reviewable | Control Mitigates Class (Outgoing) -> Vulnerability Classified As (Incoming) -> Asset Vulnerability Posture (Incoming) -> Service Depends On Asset (Incoming) | Starting from a compensating control, find the business services with asset-vulnerability posture tied to classes this control covers. This broad investigation query exposes the mitigation effect for agent interpretation: blocks/compensates are stronger mitigation coverage, reduces is risk-reduction coverage, and detects is monitoring rather than blocking mitigation. It keeps accepted, unreviewed, and pending posture/classification context visible where reviewable query visibility allows. |

### Owner

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Owner Patch Queue | traversal | Vulnerability | live | Asset Owned By (Incoming) -> Asset Vulnerability Posture (Outgoing) | Starting from an owner, return approved asset-vulnerability exposures across the owner's assets, excluding pairs already closed or covered by a scoped exception, and decorated with service, broad exception, control, and patch-window context for prioritization. |

### Product

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Product Asset Context | traversal | Asset | reviewable | Asset Runs Product (Incoming) | Starting from a reference product, return assets that run that product, with product-mapping evidence and side context for affected vulnerabilities, exposure state, owners, services, exceptions, controls, and patch windows. |

### Vendor

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Vendor Service Impact | traversal | Business Service | reviewable | Product From Vendor (Incoming) -> Vulnerability Affects Product (Incoming) -> Asset Vulnerability Posture (Incoming) -> Service Depends On Asset (Incoming) | Starting from a vendor, trace through affected products, reviewable asset-vulnerability posture, and service dependencies to find business services in the blast radius. This broad investigation query keeps accepted, unreviewed, pending, remediated, and exception-covered context visible so agents can triage from the first result instead of treating it as a strict action queue. |

### Vulnerability

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Vulnerability Asset Context | traversal | Asset | reviewable | Vulnerability Affects Product (Outgoing) -> Asset Runs Product (Incoming) | Starting from a vulnerability, return internal assets that run affected products, with the relationship evidence needed to tell whether each asset is only a candidate, has pending or accepted exposure state, has remediation state, or is covered by operational context such as owners, services, exceptions, controls, and patch windows. |

### Vulnerability Class

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Vulnerability Class Context | traversal | Vulnerability | reviewable | Vulnerability Classified As (Incoming) | Starting from a vulnerability class, return reviewable vulnerability classifications in the class and include the compensating controls mapped to that class. |

Plus 4 queries inherited from the base kit — see its README.
<!-- CRUXIBLE:END query-catalog -->

`owner_patch_queue` is the strict action queue: it returns approved exposed
posture and excludes pairs already closed or covered by a scoped exception.
`vendor_service_impact` and `control_coverage_gap` are broader investigation
surfaces. They intentionally keep remediated, exception-covered, and
non-exposed posture context available so agents can explain the state rather
than losing rows too early. `product_asset_context` also includes public
affected-vulnerability context for the product before local posture rows exist.

## Rules And Learning Loops

These generated sections own the operational facts: constraints, quality
checks, feedback vocabularies, and outcome vocabularies. Authored prose should
explain how to use them, not restate the config.

<!-- CRUXIBLE:BEGIN quality-rules -->
### Constraints

No configured constraints.

### Quality Checks

| Name | Kind | Target | Severity | Rule |
| --- | --- | --- | --- | --- |
| `assets_have_hostname` | Property | Asset.hostname | Warning | Non Empty |
| `assets_have_one_owner` | Cardinality | Asset -> Asset Owned By (out) | Warning | min `1`, max `1` |
| `minimum_assets_loaded` | Bounds | Asset count | Warning | min `5` |

Plus 5 quality checks inherited from the base kit — see its README.
<!-- CRUXIBLE:END quality-rules -->

<!-- CRUXIBLE:BEGIN learning-loops -->
### Feedback Profiles (Loop 1)

#### `asset_patch_exception_for`
- Version: `1`
- Reason codes:
  - `exception_expired` (`constraint`): Exception review date has passed without renewal.
  - `scope_mismatch` (`decision_policy`): Exception does not cover this specific vulnerability.
- Scope keys:
  - `cve`: `TO.cve_id`
  - `exception_id`: `EDGE.exception_id`

#### `asset_remediated_vulnerability`
- Version: `1`
- Reason codes:
  - `insufficient_verification` (`quality_check`): Evidence was too weak to claim verified remediation.
  - `regression_after_fix` (`provider_fix`): The issue reappeared after remediation was recorded.
  - `wrong_closure` (`provider_fix`): The asset-vulnerability pair was not actually remediated.
- Scope keys:
  - `asset`: `FROM.asset_id`
  - `cve`: `TO.cve_id`

#### `asset_runs_product`
- Version: `1`
- Reason codes:
  - `stale_inventory` (`provider_fix`): CMDB or software inventory data was outdated at match time.
  - `version_mismatch` (`quality_check`): Matched correct product but installed version is wrong.
  - `wrong_product_match` (`provider_fix`): Fuzzy match linked asset to the wrong reference product.
- Scope keys:
  - `hostname`: `FROM.hostname`
  - `inventory_source`: `EDGE.inventory_source`
  - `product`: `TO.product_name`

#### `asset_vulnerability_posture`
- Version: `1`
- Reason codes:
  - `control_mitigates` (`decision_policy`): A compensating control effectively blocks this exploit path.
  - `epss_score_stale` (`provider_fix`): EPSS score has changed since the exposure judgment.
  - `not_internet_reachable` (`constraint`): Asset is not reachable from the attack vector.
  - `version_not_in_range` (`constraint`): Installed version is not within the NVD affected range.
- Scope keys:
  - `criticality`: `FROM.criticality`
  - `cve`: `TO.cve_id`
  - `environment`: `FROM.environment`
  - `product`: `EDGE.product_id`

#### `control_mitigates_class`
- Version: `1`
- Reason codes:
  - `control_not_validated` (`quality_check`): Curated local control coverage has not been tested against this vulnerability class; correct the local seed/config evidence.
  - `wrong_vulnerability_class` (`constraint`): Curated local control coverage maps this control to the wrong vulnerability class; correct the local seed/config mapping.
- Scope keys:
  - `class`: `TO.class_id`
  - `control_type`: `FROM.control_type`

#### `vulnerability_classified_as`
- Version: `1`
- Reason codes:
  - `classification_too_broad` (`decision_policy`): Classification is too broad for control or scenario analysis.
  - `wrong_class` (`provider_fix`): Vulnerability was assigned to the wrong operational class.
- Scope keys:
  - `class`: `TO.class_id`
  - `cve`: `FROM.cve_id`

### Outcome Profiles (Loop 2)

#### Resolution-Anchored

##### `asset_remediated_resolution`
- Version: `1`
- Target: Relationship `asset_remediated_vulnerability`
- Outcome codes:
  - `premature_closure` (`trust_adjustment`): The remediation was accepted before the asset was actually closed.
  - `reopened_after_regression` (`require_review`): The asset later became exposed again after remediation.
  - `verified_remediation` (`unknown`): The remediation decision was correct and the asset remained closed.
- Scope keys:
  - `relationship_type`: `RESOLUTION.relationship_type`

##### `asset_runs_product_resolution`
- Version: `1`
- Target: Relationship `asset_runs_product`
- Outcome codes:
  - `version_drift` (`provider_fix`): Match was correct at resolution time but the asset has since been patched.
  - `wrong_product_match` (`trust_adjustment`): The fuzzy match resolved to the wrong reference product.
- Scope keys:
  - `relationship_type`: `RESOLUTION.relationship_type`

##### `asset_vulnerability_posture_resolution`
- Version: `1`
- Target: Relationship `asset_vulnerability_posture`
- Outcome codes:
  - `overestimated_exposure` (`trust_adjustment`): Control was effective but was not credited during resolution.
  - `underestimated_exposure` (`require_review`): An attack path was missed during exposure assessment.
- Scope keys:
  - `relationship_type`: `RESOLUTION.relationship_type`

#### Receipt-Anchored

##### `owner_patch_queue_query`
- Version: `1`
- Target: Query `owner_patch_queue`
- Outcome codes:
  - `missing_exposure` (`workflow_fix`): An exposed vulnerability was not returned for this owner.
  - `stale_priority` (`graph_fix`): Returned vulnerability that has already been patched or mitigated.
- Scope keys:
  - `query`: `SURFACE.name`

##### `vulnerability_asset_context_query`
- Version: `1`
- Target: Query `vulnerability_asset_context`
- Outcome codes:
  - `false_positive_result` (`graph_fix`): Query returned an asset that is not actually affected.
  - `missing_results` (`graph_fix`): A known-affected asset was not returned by the query.
- Scope keys:
  - `query`: `SURFACE.name`
<!-- CRUXIBLE:END learning-loops -->

## Maintenance

Regenerate the structural sections after changing ontology, workflows,
governed relationships, or named queries:

```bash
uv run cruxible config views --config kits/kev-triage/config.yaml --runtime --update-readme kits/kev-triage/README.md
```

To inspect the same generated bundle without editing the README:

```bash
uv run cruxible config views --config kits/kev-triage/config.yaml --runtime --view all
```

## Seed Data

Synthetic test data lives in `data/seed/`. These CSVs represent what a business
would have readily available from internal systems — CMDB exports, software
inventory, service catalogs, and operations data — using the business's own
naming conventions, not CPE identifiers. The gap between internal names and
reference-layer product IDs is the fuzzy matching problem that the
`asset_runs_product` governed relationship solves through the proposal flow.

See `data/seed/software_inventory.csv` for the key file — it contains software
names and versions as the business knows them, which need to be matched to
reference-layer products through `software_product_match` proposals.

The seed bundle now includes a richer internal environment: multiple owners,
services, internet-facing Apache hosts on different versions, patch windows,
active controls, and one legacy exception record from a source-of-record
system.

Source material for governed agent actions lives under
`data/seed/review_material/`. Those files are not loaded by
`build_local_state`; they are synthetic reports, waiver requests, and control
reviews meant to support governed relationship proposals.

## Evidence Artifacts

The KEV graph stores accepted operational conclusions, not raw observation
records. Scanner findings, EDR detections, SIEM alerts, reports, and
postmortems remain evidence inputs. Providers, proposal traces, tri-state
signals, receipts, and structured evidence metadata preserve those pointers
while the graph stays focused on durable facts such as product matching,
asset-vulnerability posture, remediation, scoped exceptions, control coverage,
and vulnerability classification.

Relationship properties hold accepted domain facts: status, scope, product
IDs, version details, basis fields, ticket IDs, and review dates. Supporting
evidence for accepted relationships lives under
`metadata.evidence.evidence_refs` and `metadata.evidence.rationale`.
Provider and workflow payload rows may still carry top-level `evidence_refs`;
workflow evidence mappings route those refs into relationship metadata when a
proposal is accepted or deterministic relationships are applied.

The asset exposure workflow keeps mechanical collection work in config:
product-path deduplication, affected-asset joins, active-control joins, and
signal projection are workflow steps. `assessment.py` owns only the remaining
domain judgment, with exposure, priority, and control-effect knobs passed as
`assessment_policy` in the workflow input.

When an evidence artifact says a host was affected by a CVE, use it to support
or challenge a governed relationship proposal. For example, cite the report in
proposal member evidence for `asset_vulnerability_posture`,
`asset_remediated_vulnerability`, `asset_patch_exception_for`, or
`vulnerability_classified_as` rather than creating a separate graph object for
the source report itself.

`control_mitigates_class` is curated local state loaded by the canonical local
build, not an agent-governed proposal. Agents should inspect it as context for
posture assessment and report missing or wrong mappings as local data/config
corrections.
