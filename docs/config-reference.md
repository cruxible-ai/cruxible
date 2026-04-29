# Config Reference

Cruxible Core configs are YAML files that define a decision domain: entity types, relationships, named queries, constraints, ingestion mappings, and — for governed workflows — integrations, quality checks, feedback profiles, decision policies, providers, and workflows. AI agents generate these configs; Core validates and executes against them.

## Top-Level Structure

```yaml
version: "1.0"
name: "my_domain"
kind: world_model
description: "Optional description of this decision domain"
# extends: base-config.yaml  # release-backed fork composition (see below)

entity_types: { ... }
relationships: [ ... ]
named_queries: { ... }
constraints: [ ... ]
ingestion: { ... }

# Governed workflow sections (all optional)
integrations: { ... }
quality_checks: [ ... ]
feedback_profiles: { ... }
outcome_profiles: { ... }
decision_policies: [ ... ]
contracts: { ... }
artifacts: { ... }
providers: { ... }
workflows: { ... }
tests: [ ... ]
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `version` | string | no | `"1.0"` | Config schema version |
| `name` | string | **yes** | — | Unique name for this domain |
| `kind` | string | no | `"world_model"` | `"ontology"` or `"world_model"` |
| `description` | string | no | `null` | Human-readable description |
| `extends` | string | no | `null` | Path to a base config for release-backed fork composition (see [Config Composition](#config-composition)) |
| `cruxible_version` | string | no | `null` | Version of cruxible-core that produced this config (auto-stamped on save) |
| `entity_types` | dict | **yes**\* | — | Entity type definitions (\*optional when `extends` is set) |
| `relationships` | list | no | `[]` | Relationship definitions |
| `named_queries` | dict | no | `{}` | Declarative query definitions |
| `constraints` | list | no | `[]` | Validation rules |
| `ingestion` | dict | no | `{}` | Data ingestion mappings (deprecated — use workflows instead) |
| `integrations` | dict | no | `{}` | Global integration definitions for governed proposals |
| `quality_checks` | list | no | `[]` | Evaluate-time graph quality checks |
| `feedback_profiles` | dict | no | `{}` | Structured feedback vocabularies per relationship type |
| `outcome_profiles` | dict | no | `{}` | Structured outcome vocabularies for trust calibration |
| `decision_policies` | list | no | `[]` | Action-side behavior rules for queries and workflows |
| `enums` | dict | no | `{}` | Shared enum vocabularies referenced by property schemas |
| `contracts` | dict | no | `{}` | Typed payload contracts for providers/workflows |
| `artifacts` | dict | no | `{}` | Pinned external artifacts referenced by providers |
| `providers` | dict | no | `{}` | Versioned executable leaves used by workflow steps |
| `workflows` | dict | no | `{}` | Declarative step-based execution plans |
| `tests` | list | no | `[]` | Fixture-based workflow tests |

---

## Config Composition

The `extends` field enables a **fork pattern** for release-backed world publishing. A published upstream world model provides entity types, relationships, and workflows; a downstream fork adds its own internal extensions without duplicating the base.

**How it works:** `cruxible_validate` detects `extends`, resolves the base path relative to the overlay file, composes in memory, and validates the composed result. The raw `load_config()` function still parses a single file — composition happens in the service/CLI layer. For inline `config_yaml` (no file path), `extends` must use an absolute path or validation will error.

At runtime, the release-backed fork flow (`service_reload_config`) materializes the composed config to disk as the active config the instance uses.

```yaml
# overlay config — validated by composing with the base automatically
version: "1.0"
name: kev_triage
extends: kev-reference.yaml
description: >
  Fork of the KEV reference world model for internal vulnerability triage.

entity_types:
  Asset:
    description: Internal asset from CMDB.
    properties:
      asset_id: {type: string, primary_key: true}
      hostname: {type: string, indexed: true}

relationships:
  - name: asset_owned_by
    from: Asset
    to: Owner
```

**Composition rules (strict append-only):**

| Field category | Fields | Behavior |
|----------------|--------|----------|
| Metadata | `name`, `description` | Overlay overrides base |
| Safe lists | `constraints`, `quality_checks`, `tests`, `decision_policies` | Overlay appends to base |
| Relationships | `relationships` | Overlay can only add new names; redefining an upstream relationship raises `ConfigError` |
| Keyed maps | `entity_types`, `named_queries`, `enums`, `ingestion`, `integrations`, `feedback_profiles`, `outcome_profiles`, `contracts`, `artifacts`, `providers`, `workflows` | Overlay can only add new keys; redefining an upstream key raises `ConfigError` |
| Other fields | everything else | Overlay can only set if not in base, or if equal to base value |

When `extends` is set, `entity_types` may be empty — the base provides them.

## kind

`kind` controls whether the config is a reusable reference ontology or an operational world model.

- `ontology`: reference taxonomy/model only. It may define entities, relationships, queries, constraints, and other static schema, but it may not define ingestion mappings, contracts, artifacts, providers, workflows, or workflow tests.
- `world_model`: operational model. It can ingest local/company data, run workflows, use pinned artifacts/providers, and carry governed judgment state on top of the underlying schema.

Use `ontology` for reusable reference layers. Use `world_model` when the config needs to operate on real data and support company-specific execution and review flows.

---

## entity_types

A dict keyed by type name. Each value defines the entity's properties.

```yaml
entity_types:
  Vehicle:
    description: "A specific vehicle (year + make + model + trim)"
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
        indexed: true
      make:
        type: string
        indexed: true
      model:
        type: string
        indexed: true
      trim:
        type: string
        optional: true
      engine:
        type: string
        optional: true
```

### EntityTypeSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `description` | string | no | `null` | Human-readable description of this entity type |
| `properties` | dict | **yes** | — | Property definitions (see below) |
| `constraints` | list[string] | no | `[]` | Constraint names that apply to this entity type |

### PropertySchema

Each property within an entity type (or relationship) is defined with:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | string | **yes** | — | Data type: `string`, `int`, `float`, `number`, `bool`, `date`, `json` |
| `primary_key` | bool | no | `false` | Mark as the entity's unique identifier |
| `indexed` | bool | no | `false` | Enable fast lookups on this property |
| `optional` | bool | no | `false` | Allow null/missing values |
| `default` | any | no | `null` | Default value when not provided |
| `enum` | list[string] | no | `null` | Restrict to allowed values |
| `enum_ref` | string | no | `null` | Reference a shared enum defined in top-level `enums` |
| `description` | string | no | `null` | Human-readable description |
| `json_schema` | dict | no | `null` | JSON Schema documentation for `json`-typed properties; write-time validation only checks JSON serializability |

**Rules:**
- Exactly one property per entity type should have `primary_key: true`.
- `primary_key` goes on the property, not the entity type.
- Properties are required by default; set `optional: true` to allow nulls.
- `enum` and `enum_ref` are mutually exclusive.
- `json_schema` is only allowed when `type: json`. Use it to document the expected structure of complex nested data (e.g., version range arrays).

---

## enums

Shared bounded vocabularies referenced by `enum_ref`. Use these when the same
allowed values appear across multiple entity, relationship, or contract fields.

```yaml
enums:
  asset_status:
    description: Lifecycle state for tracked assets.
    values: [active, retired, decommissioned]

entity_types:
  Asset:
    properties:
      asset_id: {type: string, primary_key: true}
      status: {type: string, enum_ref: asset_status, optional: true}
```

Enum values must be non-empty and unique. With `extends`, overlays may add new
enum names but may not redefine or extend upstream enum vocabularies.

---

## relationships

A list of relationship definitions connecting entity types.

```yaml
relationships:
  # Deterministic relationship — no matching config needed
  - name: product_from_vendor
    description: Deterministic product-to-vendor mapping from CPE structure.
    from: Product
    to: Vendor

  # Governed judgment relationship — uses matching + integrations
  - name: asset_affected_by_vulnerability
    description: Accepted judgment that an asset is actually affected.
    from: Asset
    to: Vulnerability
    properties:
      installed_version: {type: string, optional: true}
      rationale: {type: string, optional: true}
    matching:
      integrations:
        product_version_evidence:
          role: required
          always_review_on_unsure: true
        scanner_evidence:
          role: advisory
```

### RelationshipSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | **yes** | — | Unique relationship name |
| `from` | string | **yes** | — | Source entity type name |
| `to` | string | **yes** | — | Target entity type name |
| `cardinality` | string | no | `"many_to_many"` | Cardinality constraint |
| `properties` | dict | no | `{}` | Edge property definitions (same schema as entity properties) |
| `description` | string | no | `null` | Human-readable description |
| `inverse` | string | no | `null` | Name for the reverse traversal direction |
| `is_hierarchy` | bool | no | `false` | Mark as a hierarchical relationship |
| `matching` | MatchingConfig | no | `null` | Governed proposal policy (see [matching](#matching)) |

**Notes:**
- `from` and `to` must reference entity type names defined in `entity_types`.
- Edge `properties` use the same `PropertySchema` as entity properties.
- `inverse` enables traversing the relationship in reverse by name.
- Relationships with `matching` are **intended to be governed** — edges should be created through the proposal/group resolution flow rather than direct ingestion. The runtime does not currently enforce this; raw `add_relationship` calls will still succeed. The `matching` config controls auto-resolution behavior when proposals are used.

### matching

The `matching` block on a relationship defines how candidate group proposals are evaluated and auto-resolved. It connects relationship types to the governed proposal pipeline.

```yaml
matching:
  integrations:
    product_version_evidence:
      role: required
      always_review_on_unsure: true
    scanner_evidence:
      role: advisory
  auto_resolve_when: all_support
  auto_resolve_requires_prior_trust: trusted_only
  max_group_size: 1000
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `integrations` | dict[str, IntegrationConfig] | `{}` | Per-integration guardrails keyed by integration name (must exist in top-level `integrations`) |
| `auto_resolve_when` | string | `"all_support"` | `"all_support"` or `"no_contradict"` — when to auto-resolve proposals |
| `auto_resolve_requires_prior_trust` | string | `"trusted_only"` | `"trusted_only"` or `"trusted_or_watch"` — trust level required for auto-resolution |
| `max_group_size` | int | `1000` | Maximum candidates per group proposal |

**IntegrationConfig** (per-integration within `matching.integrations`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `role` | string | `"required"` | `"blocking"`, `"required"`, or `"advisory"` — how the signal affects resolution |
| `always_review_on_unsure` | bool | `false` | Force manual review when this integration returns `unsure` |
| `note` | string | `""` | Human-readable note about this integration's role |

**Role semantics:**
- `blocking`: A `contradict` signal from this integration blocks auto-resolution entirely.
- `required`: The signal is factored into the auto-resolve decision; `unsure` may trigger review.
- `advisory`: The signal is recorded but does not affect auto-resolution.

---

## named_queries

A dict of declarative traversal patterns. Each query defines an entry point and a sequence of traversal steps.

```yaml
named_queries:
  parts_for_vehicle:
    description: "Find all parts that fit a specific vehicle"
    entry_point: Vehicle
    traversal:
      - relationship: fits
        direction: incoming
        filter:
          verified: true
    returns: "list[Part]"

  compatible_replacements:
    description: "Find replacement parts that also fit the same vehicle"
    entry_point: Part
    traversal:
      - relationship: replaces
        direction: both
        filter:
          direction: [equivalent, upgrade]
      - relationship: fits
        direction: outgoing
        constraint: "target.vehicle_id == $vehicle_id"
    returns: "list[Part]"
```

### NamedQuerySchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `description` | string | no | `null` | Human-readable description |
| `entry_point` | string | **yes** | — | Entity type to start the traversal from |
| `traversal` | list | **yes** | — | Sequence of traversal steps |
| `returns` | string | **yes** | — | Description of the return type |

### TraversalStep

Each step in the traversal sequence:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `relationship` | string or list[string] | **yes** | — | Relationship name(s) to traverse. A list fans out across all listed types and merges results. |
| `direction` | string | no | `"outgoing"` | `outgoing`, `incoming`, or `both` |
| `filter` | dict | no | `null` | Property filters on edges or target entities |
| `constraint` | string | no | `null` | Constraint expression to apply during traversal |
| `max_depth` | int | no | `1` | BFS depth for this step (1 = direct neighbors only). Results include all entities from depth 1 through max_depth. |

**Direction semantics:**
- `outgoing`: Follow edges from entry point (source -> target)
- `incoming`: Follow edges into entry point (target -> source)
- `both`: Follow edges in either direction

---

## constraints

A list of validation rules evaluated during `cruxible_evaluate`. Constraints check **graph state** — they flag suspicious or invalid data already in the graph.

```yaml
constraints:
  - name: replacement_same_category
    rule: "replaces.FROM.category == replaces.TO.category"
    severity: warning
    description: "Replacement parts should be in the same category"
```

### ConstraintSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | **yes** | — | Unique constraint name |
| `rule` | string | **yes** | — | Rule expression (see syntax below) |
| `severity` | string | no | `"warning"` | `warning` or `error` |
| `description` | string | no | `null` | Human-readable description |

### Rule Syntax

Constraints compare properties across relationship endpoints:

```
RELATIONSHIP.FROM.property <op> RELATIONSHIP.TO.property
```

- `RELATIONSHIP`: The relationship name (e.g., `replaces`)
- `FROM`: The source entity's property
- `TO`: The target entity's property
- `<op>`: One of `==`, `!=`, `>`, `>=`, `<`, `<=`
- Identifiers may contain letters, digits, underscores, and hyphens

**Examples:**
- `replaces.FROM.category == replaces.TO.category` — flags any `replaces` edge where the source and target parts have different categories.
- `replaces.FROM.priority > replaces.TO.priority` — flags any `replaces` edge where the source priority does not exceed the target priority.

---

## ingestion

> **Deprecation notice:** The `ingestion` section is being deprecated. Workflows with `make_entities` / `make_relationships` / `apply_entities` / `apply_relationships` steps are the preferred path for loading data — they produce receipts, support canonical snapshots, and compose with the governed proposal flow. New configs should use workflows instead of ingestion mappings. Existing ingestion mappings will continue to work but will be removed in a future release.

A dict of named mappings that tell Core how to load CSV/JSON data into entities and relationships.

```yaml
ingestion:
  vehicles:
    entity_type: Vehicle
    file_pattern: "vehicles*.csv"
    id_column: vehicle_id

  parts:
    entity_type: Part
    file_pattern: "parts*.csv"
    id_column: part_number

  fitments:
    relationship_type: fits
    file_pattern: "fitments*.csv"
    from_column: part_number
    to_column: vehicle_id
```

### IngestionMapping

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `description` | string | no | `null` | Human-readable description of what data this mapping expects |
| `entity_type` | string | conditional | `null` | Entity type to ingest (mutually exclusive with `relationship_type`) |
| `relationship_type` | string | conditional | `null` | Relationship type to ingest (mutually exclusive with `entity_type`) |
| `file_pattern` | string | no | `null` | Glob pattern for matching data files |
| `id_column` | string | conditional | `null` | Column containing entity IDs (required for entity mappings) |
| `from_column` | string | conditional | `null` | Column containing source entity IDs (required for relationship mappings) |
| `to_column` | string | conditional | `null` | Column containing target entity IDs (required for relationship mappings) |
| `column_map` | dict | no | `{}` | Rename CSV columns to property names: `{csv_column: property_name}` |

**Rules:**
- Exactly one of `entity_type` or `relationship_type` must be set.
- Entity mappings require `id_column`.
- Relationship mappings require both `from_column` and `to_column`.
- `column_map` renames CSV columns to match property names in the schema.
- Columns not in `column_map` map by name if they match a schema property.

---

## integrations

Global integration definitions that declare external signal sources for governed proposals. Integration specs are **immutable by convention** — any semantic change (different model, different metric) requires a new key (e.g., `cosine_similarity_v2`).

Integrations are referenced by name in relationship `matching` blocks.

```yaml
integrations:
  product_version_evidence:
    kind: product_version_match
    contract:
      output: support|unsure|contradict

  scanner_evidence:
    kind: scanner_presence
    contract:
      output: support|unsure|contradict
    notes: "Advisory signal from vulnerability scanner findings"
```

### IntegrationSpec

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `kind` | string | **yes** | — | Integration kind identifier (e.g., `product_version_match`, `engineering_review`) |
| `contract` | dict | no | `{}` | Stable contract describing the integration's input/output |
| `notes` | string | no | `""` | Human-readable notes |

**Convention:** Integration output is a tri-state signal: `support`, `unsure`, or `contradict`. The contract should document this as `output: support|unsure|contradict`.

**Validation:** If any relationship has a `matching.integrations` block, every integration key referenced there must exist in the top-level `integrations` dict.

---

## quality_checks

Evaluate-time graph quality checks run during `cruxible_evaluate`. Five check kinds are available, distinguished by the `kind` field.

### 1. property

Check a top-level property on entities or relationships.

```yaml
quality_checks:
  - name: cve_id_format
    kind: property
    severity: error
    target: entity
    entity_type: Vulnerability
    property: cve_id
    rule: pattern
    pattern: "^CVE-\\d{4}-\\d{4,}$"
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"property"` | |
| `target` | `"entity"` or `"relationship"` | What to check |
| `entity_type` | string | Required when `target: entity` |
| `relationship_type` | string | Required when `target: relationship` |
| `property` | string | Property name to check |
| `rule` | string | `"required"`, `"non_empty"`, `"type"`, or `"pattern"` |
| `expected_type` | string | Required when `rule: type` |
| `pattern` | string | Regex pattern, required when `rule: pattern` |

### 2. json_content

Check JSON array-of-object content on a `json`-typed property.

```yaml
  - name: affected_versions_have_useful_keys
    kind: json_content
    severity: warning
    target: relationship
    relationship_type: vulnerability_affects_product
    property: affected_versions
    rule: required_nested_keys
    keys: [version_start_including, version_end_excluding, version_exact, fixed_version]
    match: any

  - name: no_empty_affected_version_objects
    kind: json_content
    severity: error
    target: relationship
    relationship_type: vulnerability_affects_product
    property: affected_versions
    rule: no_empty_objects_in_array
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"json_content"` | |
| `target` | `"entity"` or `"relationship"` | What to check |
| `entity_type` / `relationship_type` | string | Target type |
| `property` | string | JSON property name to check |
| `rule` | string | `"no_empty_objects_in_array"` or `"required_nested_keys"` |
| `keys` | list[string] | Required when `rule: required_nested_keys` — keys to look for |
| `match` | string | `"any"` or `"all"` — required when `rule: required_nested_keys` |

### 3. uniqueness

Check entity-property uniqueness, optionally across compound keys.

```yaml
  - name: unique_vendor_product_pair
    kind: uniqueness
    severity: error
    entity_type: Product
    properties: [vendor_name, product_name]
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"uniqueness"` | |
| `entity_type` | string | Entity type to check |
| `properties` | list[string] | One or more property names that must be unique together |

### 4. bounds

Check entity or relationship counts against a numeric range.

```yaml
  - name: minimum_products
    kind: bounds
    severity: warning
    target: entity_count
    entity_type: Product
    min_count: 10
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"bounds"` | |
| `target` | `"entity_count"` or `"relationship_count"` | What to count |
| `entity_type` / `relationship_type` | string | Target type |
| `min_count` | int | Optional lower bound |
| `max_count` | int | Optional upper bound (at least one of min/max required) |

### 5. cardinality

Check per-entity relationship counts in one direction.

```yaml
  - name: products_have_exactly_one_vendor
    kind: cardinality
    severity: error
    entity_type: Product
    relationship_type: product_from_vendor
    direction: outgoing
    min_count: 1
    max_count: 1
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"cardinality"` | |
| `entity_type` | string | Entity type to check |
| `relationship_type` | string | Relationship type to count |
| `direction` | `"incoming"` or `"outgoing"` | Edge direction relative to the entity |
| `min_count` | int | Optional lower bound |
| `max_count` | int | Optional upper bound (at least one of min/max required) |

**Common fields across all quality check kinds:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | **yes** | — | Unique check name |
| `kind` | string | **yes** | — | Check kind discriminator |
| `description` | string | no | `null` | Human-readable description |
| `severity` | string | no | `"warning"` | `"warning"` or `"error"` |

---

## feedback_profiles

Structured feedback vocabularies scoped to a relationship type. Feedback profiles define the **reason codes** an agent or human can attach to feedback, and the **scope keys** that enable grouping and analysis. This is the foundation of Loop 1: feedback drives constraint and decision policy suggestions.

```yaml
feedback_profiles:
  fits:
    version: 2
    reason_codes:
      legacy_unsupported:
        description: "Legacy environment is unsupported"
        remediation_hint: decision_policy
        required_scope_keys: [category, make]
      fitment_mismatch:
        description: "Part category mismatches vehicle make"
        remediation_hint: constraint
        required_scope_keys: [category, make]
    scope_keys:
      category: FROM.category
      make: TO.make
```

### FeedbackProfileSchema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `version` | int | `1` | Profile version — bump when reason codes or scope keys change semantically |
| `reason_codes` | dict[str, FeedbackReasonCodeSchema] | `{}` | Named reason codes agents can attach to feedback |
| `scope_keys` | dict[str, FeedbackPathRef] | `{}` | Named scope dimensions extracted from graph state at feedback time |

### FeedbackReasonCodeSchema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `description` | string | **required** | What this reason code means |
| `remediation_hint` | string | `"unknown"` | `"constraint"`, `"decision_policy"`, `"quality_check"`, `"provider_fix"`, or `"unknown"` — guides `analyze_feedback` to produce the right kind of suggestion |
| `required_scope_keys` | list[string] | `[]` | Scope keys that must be present when this code is used |

### FeedbackPathRef

Scope key paths follow the pattern `(FROM|TO|EDGE).<property>`:
- `FROM.category` — extracts the `category` property from the source entity
- `TO.make` — extracts the `make` property from the target entity
- `EDGE.confidence` — extracts the `confidence` property from the edge

**How it works:** When an agent submits structured feedback with a `reason_code` and `scope_hints`, `analyze_feedback` groups matching feedback records and produces suggestions:
- Reason codes with `remediation_hint: constraint` produce constraint suggestions
- Reason codes with `remediation_hint: decision_policy` produce decision policy suggestions
- Other hints produce quality check or provider fix candidates

---

## outcome_profiles

Structured outcome vocabularies for trust calibration and debugging (Loop 2). Outcome profiles define the **outcome codes** and **scope keys** attached to recorded outcomes, scoped to either a resolution anchor (proposal outcomes) or a receipt anchor (query/workflow outcomes).

```yaml
outcome_profiles:
  fits_resolution:
    anchor_type: resolution
    relationship_type: fits
    version: 1
    outcome_codes:
      wrong_match:
        description: "The resolved match was incorrect"
        remediation_hint: trust_adjustment
        required_scope_keys: [category]
      stale_data:
        description: "Source data was outdated at resolution time"
        remediation_hint: provider_fix
    scope_keys:
      category: RESOLUTION.relationship_type

  parts_query:
    anchor_type: receipt
    surface_type: query
    surface_name: parts_for_vehicle
    version: 1
    outcome_codes:
      missing_results:
        description: "Expected results were not returned"
        remediation_hint: workflow_fix
    scope_keys:
      query: SURFACE.name
```

### OutcomeProfileSchema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `anchor_type` | string | **required** | `"resolution"` or `"receipt"` |
| `version` | int | `1` | Profile version |
| `relationship_type` | string | `null` | Required for `anchor_type: resolution` |
| `workflow_name` | string | `null` | Optional for resolution anchors |
| `surface_type` | string | `null` | Required for `anchor_type: receipt` — `"query"`, `"workflow"`, or `"operation"` |
| `surface_name` | string | `null` | Required for `anchor_type: receipt` |
| `outcome_codes` | dict[str, OutcomeCodeSchema] | `{}` | Named outcome codes |
| `scope_keys` | dict[str, OutcomePathRef] | `{}` | Named scope dimensions |

### OutcomeCodeSchema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `description` | string | **required** | What this outcome code means |
| `remediation_hint` | string | `"unknown"` | `"trust_adjustment"`, `"require_review"`, `"decision_policy"`, `"provider_fix"`, `"workflow_fix"`, `"graph_fix"`, or `"unknown"` |
| `required_scope_keys` | list[string] | `[]` | Scope keys that must be present |

### OutcomePathRef

Scope key paths depend on anchor type. Valid fields per prefix:

**Resolution anchors:**

| Prefix | Valid fields |
|--------|-------------|
| `RESOLUTION` | `resolution_id`, `relationship_type`, `action`, `trust_status`, `resolved_by` |
| `GROUP` | `group_signature` |
| `WORKFLOW` | `name`, `receipt_id`, `trace_ids` |
| `THESIS` | _(any thesis_facts key)_ |

**Receipt anchors:**

| Prefix | Valid fields |
|--------|-------------|
| `RECEIPT` | `receipt_id`, `operation_type` |
| `SURFACE` | `type`, `name` |
| `TRACESET` | `trace_ids`, `provider_names`, `trace_count` |

**Validation:** Resolution profiles require `relationship_type` and must not set `surface_type`/`surface_name`. Receipt profiles require `surface_type` and `surface_name` and must not set `relationship_type`/`workflow_name`.

---

## decision_policies

Action-side behavior rules applied during query execution or workflow proposal. Decision policies are the **action controls** that complement state-side constraints. While constraints flag bad data in the graph, decision policies change what queries return or what workflows propose.

```yaml
decision_policies:
  - name: suppress_legacy_honda_brakes
    description: "Don't return legacy brake parts for Honda vehicles"
    applies_to: query
    query_name: parts_for_vehicle
    relationship_type: fits
    effect: suppress
    match:
      from:
        category: brakes
      to:
        make: Honda
    rationale: "Legacy brake fitments for Honda are unreliable — see feedback batch 2026-03"

  - name: review_substitutes_plant_b
    description: "Require manual review for substitute proposals at Plant B"
    applies_to: workflow
    workflow_name: propose_substitutes
    relationship_type: safe_to_substitute
    effect: require_review
    match:
      context:
        scope_plant_id: PLANT-B
    expires_at: "2026-06-30"
```

### DecisionPolicySchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | **yes** | — | Unique policy name |
| `description` | string | no | `null` | Human-readable description |
| `rationale` | string | no | `""` | Why this policy exists (reference to feedback, incident, etc.) |
| `applies_to` | string | **yes** | — | `"query"` or `"workflow"` |
| `query_name` | string | conditional | `null` | Required when `applies_to: query` |
| `workflow_name` | string | conditional | `null` | Required when `applies_to: workflow` |
| `relationship_type` | string | **yes** | — | Relationship type this policy applies to |
| `effect` | string | **yes** | — | `"suppress"` (query only) or `"require_review"` |
| `match` | DecisionPolicyMatch | no | `{}` | Exact-match selectors (see below) |
| `expires_at` | string | no | `null` | Optional expiry date (ISO 8601) |

### DecisionPolicyMatch

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `from` | dict | `{}` | Exact-match on source entity properties |
| `to` | dict | `{}` | Exact-match on target entity properties |
| `edge` | dict | `{}` | Exact-match on edge properties |
| `context` | dict | `{}` | Exact-match on workflow context (e.g., scope keys) |

**Validation:**
- Query policies require `query_name` and only support `effect: suppress`.
- Workflow policies require `workflow_name` and support both effects.

**Keep the distinction clean:**
- **Constraints** = suspicious or invalid graph state (evaluated by `cruxible_evaluate`)
- **Decision policies** = query/workflow behavior changes (enforced at execution time)

---

## contracts

Typed payload contracts for provider inputs and outputs. Contracts define the fields a provider expects to receive and the shape of what it returns.

```yaml
contracts:
  EmptyInput:
    fields: {}

  PublicKevRows:
    description: "Rows of joined KEV + NVD + EPSS data"
    fields:
      items:
        type: json
        json_schema:
          type: array
          items:
            type: object
            properties:
              cve_id: {type: string}
              vendor_id: {type: string}
              product_id: {type: string}
              cvss_score: {type: number}
```

### ContractSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `description` | string | no | `null` | Human-readable description |
| `fields` | dict[str, PropertySchema] | **yes** | — | Field definitions using the same PropertySchema as entity properties |

---

## artifacts

Pinned external artifacts referenced by providers. Artifacts represent data bundles, models, or other resources that providers depend on. The `sha256` hash enables reproducible builds — the workflow lock verifies the live artifact matches the hash at lock time.

```yaml
artifacts:
  public_kev_bundle:
    kind: directory
    uri: ./data
    sha256: sha256:f884e5f8fad66c6bba54face97863137833ab26035d7a4cda333063d0ab224f9
```

### ProviderArtifactSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `kind` | string | **yes** | — | Artifact kind (e.g., `directory`, `file`, `model`) |
| `uri` | string | **yes** | — | Location (relative path, URL, etc.) |
| `sha256` | string | no | `null` | Content hash for reproducibility verification |
| `metadata` | dict | no | `{}` | Arbitrary metadata |

---

## providers

Versioned executable leaves used by workflow steps. A provider is a callable that takes a typed input, produces a typed output, and generates an execution trace for the receipt chain.

```yaml
providers:
  load_public_kev_rows:
    kind: function
    description: >
      Load KEV catalog, EPSS scores, and NVD CPE configurations.
      Emit one row per (CVE, CPE product) pair.
    contract_in: EmptyInput
    contract_out: PublicKevRows
    ref: providers.load_public_kev_rows
    version: "1.0.0"
    deterministic: true
    runtime: python
    artifact: public_kev_bundle
```

### ProviderSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `kind` | string | **yes** | — | `"function"`, `"model"`, or `"tool"` |
| `description` | string | no | `null` | What this provider does |
| `contract_in` | string | **yes** | — | Name of the input contract (must exist in `contracts`) |
| `contract_out` | string | **yes** | — | Name of the output contract (must exist in `contracts`) |
| `ref` | string | **yes** | — | Callable reference (e.g., `module.function_name`) |
| `version` | string | **yes** | — | Semantic version for lock-file reproducibility |
| `deterministic` | bool | no | `true` | Whether the provider produces identical output for identical input |
| `artifact` | string | no | `null` | Name of artifact this provider depends on (must exist in `artifacts`) |
| `runtime` | string | no | `"python"` | Execution runtime |
| `side_effects` | bool | no | `false` | Whether the provider has side effects |
| `config` | dict | no | `{}` | Provider-specific configuration |

---

## workflows

Declarative step-based execution plans. Workflows compose queries, providers, and graph mutations into reproducible pipelines. A workflow can be **canonical** (creates accepted world state with snapshot tracking) or non-canonical (produces output without mutating graph state).

```yaml
workflows:
  build_public_kev_reference:
    canonical: true
    description: >
      Build the canonical public KEV reference layer from bundled data.
    contract_in: EmptyInput
    steps:
      - id: rows
        provider: load_public_kev_rows
        input: {}
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

      - id: product_vendor
        make_relationships:
          relationship_type: product_from_vendor
          items: $steps.rows.items
          from_type: Product
          from_id: $item.product_id
          to_type: Vendor
          to_id: $item.vendor_id
        as: product_vendor

      - id: apply_vendors
        apply_entities:
          entities_from: vendors
        as: apply_vendors

      - id: apply_product_vendor
        apply_relationships:
          relationships_from: product_vendor
        as: apply_product_vendor
    returns: apply_product_vendor
```

### WorkflowSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `description` | string | no | `null` | What this workflow does |
| `canonical` | bool | no | `false` | Whether this workflow creates accepted canonical state |
| `contract_in` | string | **yes** | — | Name of the input contract |
| `steps` | list[WorkflowStepSchema] | **yes** | — | Ordered list of steps |
| `returns` | string | **yes** | — | ID of the step whose output is the workflow result |

### Workflow Step Types

Each step must define exactly one of these operations:

| Step type | Purpose | Key fields |
|-----------|---------|------------|
| `provider` | Call a registered provider | `provider`, `input`, `as` |
| `query` | Run a named query | `query`, `params`, `as` |
| `assert` | Guard condition — fail the workflow if not met | `assert: {left, op, right, message}` |
| `list_entities` | Read entity rows from current graph state into a workflow payload | `list_entities: {entity_type, property_filter?, limit?}`, `as` |
| `list_relationships` | Read relationship rows from current graph state into a workflow payload | `list_relationships: {relationship_type, property_filter?, limit?}`, `as` |
| `make_entities` | Build an entity set from list data | `make_entities: {entity_type, items, entity_id, properties}`, `as` |
| `make_relationships` | Build a relationship set from list data | `make_relationships: {relationship_type, items, from_type, from_id, to_type, to_id, properties}`, `as` |
| `apply_entities` | Apply a built entity set to graph state | `apply_entities: {entities_from}`, `as` |
| `apply_relationships` | Apply a built relationship set to graph state | `apply_relationships: {relationships_from}`, `as` |
| `make_candidates` | Build relationship candidates for governed proposals | `make_candidates: {relationship_type, items, from_type, from_id, to_type, to_id, properties}`, `as` |
| `map_signals` | Convert provider output to tri-state integration signals | `map_signals: {integration, items, from_id, to_id, score/enum}`, `as` |
| `propose_relationship_group` | Assemble a governed group proposal from candidates + signals | `propose_relationship_group: {relationship_type, candidates_from, signals_from}`, `as` |

### Step Reference Syntax

Steps reference data from prior steps and the current item in list iterations:

| Reference | Meaning |
|-----------|---------|
| `$input` | Workflow input payload |
| `$steps.<step_id>` | Output of a prior step (by its `as` alias) |
| `$steps.<step_id>.<field>` | A specific field from a prior step's output |
| `$item` | Current item when iterating over a list (used inside `make_*` and `map_signals`) |
| `$item.<field>` | A specific field on the current item |

**Read-step outputs:**
- `list_entities` returns `{items: [...], total: N}` where each item is an entity object with `entity_type`, `entity_id`, and `properties`.
- `list_relationships` returns `{items: [...], total: N}` where each item is a relationship object with `from_id`, `to_id`, `relationship_type`, and `properties`.

### Governed Proposal Steps

For workflows that produce governed proposals (fuzzy matching, judgment calls), the three-step pattern is:

1. **`make_candidates`** — build candidate (from, to) pairs with properties
2. **`map_signals`** — convert provider scores/enums to tri-state signals per integration
3. **`propose_relationship_group`** — assemble candidates + signals into a group proposal

The group then enters the resolution lifecycle (auto-resolve or manual review) based on the relationship's `matching` config.

**map_signals mapping modes** (exactly one required):

- `score`: Map a numeric value to signals using thresholds
  ```yaml
  score:
    path: similarity_score
    support_gte: 0.8
    unsure_gte: 0.5
  ```
  The `path` is a field name on each item — the executor prepends `$item.` automatically, so write `similarity_score` not `$item.similarity_score`. Values >= `support_gte` produce `support`, >= `unsure_gte` produce `unsure`, below produce `contradict`.

- `enum`: Map string values to signals using a lookup table
  ```yaml
  enum:
    path: verdict
    map:
      exact: support
      partial: unsure
      none: contradict
  ```

---

## tests

Fixture-based workflow tests defined in the config. These are run by `cruxible test` to verify workflow behavior.

```yaml
tests:
  - name: kev_reference_builds
    workflow: build_public_kev_reference
    input: {}
    expect:
      receipt_contains_provider: load_public_kev_rows
```

### WorkflowTestSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | **yes** | — | Test name |
| `workflow` | string | **yes** | — | Workflow to execute (must exist in `workflows`) |
| `input` | dict | no | `{}` | Input payload for the workflow |
| `expect` | WorkflowTestExpectSchema | no | `{}` | Assertions on the result |

### WorkflowTestExpectSchema

| Field | Type | Description |
|-------|------|-------------|
| `output_equals` | any | Exact match on the workflow output |
| `output_contains` | dict | Subset match on the workflow output |
| `receipt_contains_provider` | string or list[string] | Provider name(s) that must appear in the execution receipt |
| `error_contains` | string | Expected error substring (for negative tests) |

---

## Full Example

The KEV triage fork config (`kits/kev-triage/config.yaml`) demonstrates a release-backed fork overlay that extends a reference layer with governed judgment relationships. **Note:** This config requires composition with its base (`kev-reference.yaml`) before it can be validated or loaded — `Vulnerability`, `Product`, and other reference types are defined in the base, not here:

```yaml
version: "1.0"
name: kev_triage
kind: world_model
extends: kev-reference.yaml
description: >
  Fork of the KEV reference world model for internal vulnerability triage.

entity_types:
  Asset:
    description: Internal asset from CMDB, cloud inventory, or endpoint tooling.
    properties:
      asset_id: {type: string, primary_key: true}
      hostname: {type: string, indexed: true}
      criticality: {type: string, optional: true}
      environment: {type: string, optional: true}
      internet_exposed: {type: bool, optional: true}

  Owner:
    description: Team or person responsible for an asset.
    properties:
      owner_id: {type: string, primary_key: true}
      name: {type: string}
      team: {type: string, optional: true}

relationships:
  - name: asset_owned_by
    description: Ownership mapping for assets.
    from: Asset
    to: Owner

  - name: asset_affected_by_vulnerability
    description: Accepted judgment that an asset is affected by a vulnerability.
    from: Asset
    to: Vulnerability
    properties:
      installed_version: {type: string, optional: true}
      rationale: {type: string, optional: true}
    matching:
      integrations:
        product_version_evidence:
          role: required
          always_review_on_unsure: true
        scanner_evidence:
          role: advisory

named_queries:
  kev_assets:
    description: Find internal assets accepted as affected by a vulnerability.
    entry_point: Vulnerability
    returns: Asset
    traversal:
      - relationship: asset_affected_by_vulnerability
        direction: incoming

  owner_patch_queue:
    description: Find vulnerabilities affecting an owner's assets.
    entry_point: Owner
    returns: Vulnerability
    traversal:
      - relationship: asset_owned_by
        direction: incoming
      - relationship: asset_affected_by_vulnerability
        direction: outgoing

ingestion:
  assets:
    description: Internal asset inventory import from CMDB.
    entity_type: Asset
    id_column: asset_id
    column_map:
      asset_id: asset_id
      hostname: hostname
      criticality: criticality
      environment: environment
      internet_exposed: internet_exposed

  asset_owned_by_edges:
    description: Asset ownership mapping import.
    relationship_type: asset_owned_by
    from_column: asset_id
    to_column: owner_id

integrations:
  product_version_evidence:
    kind: product_version_match
    contract:
      output: support|unsure|contradict

  scanner_evidence:
    kind: scanner_presence
    contract:
      output: support|unsure|contradict
```

See also the reference layer config (`kits/kev-triage/kev-reference.yaml`) for a complete example with workflows, providers, contracts, artifacts, and quality checks.
