# Config Reference

Cruxible Core configs are YAML files that define a decision domain: entity
types, relationships, named queries, constraints, workflows, providers,
artifacts, quality checks, feedback profiles, outcome profiles, and decision
policies. AI agents generate these configs; Core validates and executes against
them.

## Top-Level Structure

```yaml
version: "1.0"
name: "my_domain"
kind: world_model
description: "Optional description of this decision domain"
# extends: base-config.yaml  # release-backed overlay composition (see below)

entity_types: { ... }
relationships: [ ... ]
named_queries: { ... }
constraints: [ ... ]

# Governed workflow sections (all optional)
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
| `extends` | string | no | `null` | Path to a base config for release-backed overlay composition (see [Config Composition](#config-composition)) |
| `cruxible_version` | string | no | `null` | Version of cruxible-core that produced this config (auto-stamped on save) |
| `entity_types` | dict | **yes**\* | — | Entity type definitions (\*optional when `extends` is set) |
| `relationships` | list | no | `[]` | Relationship definitions |
| `named_queries` | dict | no | `{}` | Declarative query definitions |
| `constraints` | list | no | `[]` | Validation rules |
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

The `extends` field enables an **overlay pattern** for release-backed world publishing. A published upstream world model provides entity types, relationships, and workflows; a downstream overlay adds its own internal extensions without duplicating the base.

**How it works:** `cruxible_validate` detects `extends`, resolves the base path relative to the overlay file, composes in memory, and validates the composed result. The raw `load_config()` function still parses a single file — composition happens in the service/CLI layer. For inline `config_yaml` (no file path), `extends` must use an absolute path or validation will error.

At runtime, the release-backed overlay flow (`service_reload_config`) materializes the composed config to disk as the active config the instance uses.

```yaml
# overlay config — validated by composing with the base automatically
version: "1.0"
name: kev_triage
extends: ../kev-reference/config.yaml
description: >
  Overlay of the KEV reference world model for internal vulnerability triage.

entity_types:
  Asset:
    description: Internal asset from CMDB.
    properties:
      asset_id: {primary_key: true}
      hostname: {indexed: true}

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
| Keyed maps | `entity_types`, `named_queries`, `enums`, `feedback_profiles`, `outcome_profiles`, `contracts`, `artifacts`, `providers`, `workflows` | Overlay can only add new keys; redefining an upstream key raises `ConfigError` |
| Other fields | everything else | Overlay can only set if not in base, or if equal to base value |

When `extends` is set, `entity_types` may be empty — the base provides them.

## kind

`kind` controls whether the config is a reusable reference ontology or an operational world model.

- `ontology`: schema-only reference taxonomy/model. It may define entities, relationships, queries, constraints, and other static schema, but it may not define contracts, artifacts, providers, workflows, or workflow tests.
- `world_model`: operational model. It can load local/company data through workflows, use pinned artifacts/providers, and carry governed judgment state on top of the underlying schema.

Use `ontology` for reusable reference layers. Use `world_model` when the config needs to operate on real data and support company-specific execution and review flows.

---

## entity_types

A dict keyed by type name. Each value defines the entity's properties.

```yaml
entity_types:
  Vehicle:
    description: "A specific vehicle (year + make + model + trim)"
    properties:
      vehicle_id: {primary_key: true}
      year:
        type: int
        indexed: true
      make: {indexed: true}
      model: {indexed: true}
      trim: {}
      engine: {}
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
| `type` | string | no for graph properties; yes for contract fields | `"string"` for graph properties | Data type: `string`, `int`, `float`, `number`, `bool`, `date`, `json` |
| `primary_key` | bool | no | `false` | Mark as the entity's unique identifier |
| `indexed` | bool | no | `false` | Enable fast lookups on this property |
| `optional` | bool | no | graph properties default to `true`; contract fields default to `false` | Allow null/missing values |
| `required` | bool | no | `null` | Positive alias for `optional: false`; reject conflicting `required`/`optional` values |
| `default` | any | no | `null` | Default value when not provided |
| `enum` | list[string] | no | `null` | Restrict to allowed values |
| `enum_ref` | string | no | `null` | Reference a shared enum defined in top-level `enums` |
| `description` | string | no | `null` | Human-readable description |
| `json_schema` | dict | no | `null` | JSON Schema documentation for `json`-typed properties; write-time validation only checks JSON serializability |

**Rules:**
- Exactly one property per entity type should have `primary_key: true`.
- `primary_key` goes on the property, not the entity type.
- Entity and relationship properties default to `type: string` and `optional: true`, so `field_name: {}` is valid shorthand.
- `primary_key: true` implies required and may not be combined with `optional: true`.
- Contract fields still require an explicit `type` and are required by default.
- Use `required: true` for non-primary-key graph properties that must be present.
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
      asset_id: {primary_key: true}
      status: {enum_ref: asset_status}
```

Enum values must be non-empty and unique. With `extends`, overlays may add new
enum names but may not redefine or extend upstream enum vocabularies.

---

## relationships

A list of relationship definitions connecting entity types.

```yaml
relationships:
  # Deterministic relationship — no proposal policy needed
  - name: product_from_vendor
    description: Deterministic product-to-vendor mapping from CPE structure.
    from: Product
    to: Vendor

  # Governed judgment relationship — uses proposal_policy + signals
  - name: asset_affected_by_vulnerability
    description: Accepted judgment that an asset is actually affected.
    from: Asset
    to: Vulnerability
    properties:
      installed_version: {}
      rationale: {}
    proposal_policy:
      signals:
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
| `proposal_policy` | ProposalPolicyConfig | no | `null` | Governed proposal policy (see [proposal_policy](#proposal_policy)) |
| `proposal_identity` | string | no | `"thesis_signature"` | `"thesis_signature"` groups trust by proposal thesis; `"relationship_tuple"` groups trust by edge tuple and requires `proposal_policy` |

**Notes:**
- `from` and `to` must reference entity type names defined in `entity_types`.
- Edge `properties` use the same `PropertySchema` as entity properties.
- `inverse` enables traversing the relationship in reverse by name.
- Relationships with `proposal_policy` are intended to be governed: edges should be created through the proposal/group resolution flow when they are inferred, classified, or otherwise judgment-bearing. Raw `add_relationship` calls remain available for explicit deterministic facts.

### proposal_policy

The `proposal_policy` block on a relationship defines how candidate group proposals are evaluated and auto-resolved. It connects relationship types to the governed proposal pipeline.

```yaml
proposal_policy:
  signals:
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
| `signals` | dict[str, SignalPolicyConfig] | `{}` | Per-signal-source guardrails keyed by the labels emitted from workflow `map_signals` steps |
| `auto_resolve_when` | string | `"all_support"` | `"all_support"` or `"no_contradict"` — when to auto-resolve proposals |
| `auto_resolve_requires_prior_trust` | string | `"trusted_only"` | `"trusted_only"` or `"trusted_or_watch"` — trust level required for auto-resolution |
| `max_group_size` | int | `1000` | Maximum candidates per group proposal |

**SignalPolicyConfig** (per signal source within `proposal_policy.signals`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `role` | string | `"required"` | `"blocking"`, `"required"`, or `"advisory"` — how the signal affects resolution |
| `always_review_on_unsure` | bool | `false` | Force manual review when this signal source returns `unsure` |
| `note` | string | `""` | Human-readable note about this signal source's role |

**Role semantics:**
- `blocking`: A `contradict` signal from this source blocks auto-resolution entirely.
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
| `result_shape` | string | no | `"path"` | Output shape: `entity`, `path`, or `relationship` |
| `dedupe` | string | no | shape-dependent | Result dedupe mode: `entity`, `path`, or `none`. Entity queries default to `entity`; path and relationship queries default to `path`. |
| `relationship_state` | string | no | `"live"` | Relationship visibility: `live`, `accepted`, `pending`, or `reviewable` |
| `allow_relationship_state_override` | bool | no | `false` | Whether runtime callers may override `relationship_state` |
| `select` | dict | no | `null` | Projection map from output field name to query reference or literal value. When present, user-facing rows return `{values}` while receipts preserve source evidence for audit and feedback. |
| `order_by` | list | no | `[]` | Deterministic ordering rules. Each item uses `by`, optional `direction` (`asc` or `desc`), and optional `value_type` (`string`, `int`, `integer`, `float`, `number`, `bool`, `date`, or `datetime`). |
| `include` | dict | no | `{}` | One-hop side-context includes keyed by alias. Includes decorate each primary row without advancing traversal or fanning out primary rows. |
| `limit` | int | no | `null` | Query-level output cap applied after traversal, dedupe, path budgets, ordering, and before projection. Result metadata reports pre-limit `total_results`, effective `limit`, and `limit_truncated`. |
| `max_paths` | int | no | `null` | Traversal-time retained-path frontier budget. It caps retained path states for each traversal step, limiting memory and receipt growth. It is not a total candidate-evaluation budget. |
| `max_paths_per_result` | int | no | `null` | Post-traversal final retained-path-per-result cap applied after traversal/dedupe, before ordering and `limit`. It does not bound traversal work. |

Validation rules:
- `result_shape: entity` requires `dedupe: entity`.
- `result_shape: relationship` requires `dedupe: path` or `none`.
- `relationship_state: pending` requires `result_shape: path` or `relationship`, and does not allow `dedupe: entity`.
- `relationship_state: reviewable` requires `result_shape: path`, and does not allow `dedupe: entity`.
- `required: false` traversal steps are optional continuations, not independent context enrichment. They require `result_shape: path` or `relationship`.
- `result_shape: relationship` may use `required: false` optional-continuation steps only when the final returned relationship step is still required.
- `max_paths` and `max_paths_per_result` require `result_shape: path` or `relationship`, and must be positive integers when set.
- `max_paths` is the retained-path frontier safety control. Once reached for a traversal step, the engine stops retaining/enqueuing more path states and avoids recording traversal receipts for the skipped frontier. Candidates that fail filters before any path is retained can still be evaluated; use a future candidate/work budget if total edge evaluation needs a separate cap.
- `max_paths_per_result` is a result-time evidence cap. It trims retained paths per final result entity after traversal, when result identity is known. It is distinct from `limit`: `max_paths_per_result` controls evidence fanout per result, while `limit` controls how many ordered rows are returned.
- `order_by` runs after traversal, dedupe, and path budgets, before `limit`.
- Path budget truncation is reported separately with `path_truncated`, `retained_path_count`, and `truncation_reasons`.
- `path_truncated` means traversal was cut short by a path budget before the engine could prove completeness. It does not guarantee that every skipped frontier item would have produced a returned row.
- `total_path_count` is populated only when traversal completes. If traversal-time `max_paths` cuts exploration short, `total_path_count` is `null` because the full possible path count was intentionally not computed.
- Missing projected property or metadata refs resolve to `null`; missing `$input.*` refs fail execution.
- Missing `$path.<alias>...` refs for a non-required traversal alias resolve to `null` when that step did not match. Unknown aliases still fail validation/execution.
- Missing order values sort last, with stable graph-identity tie-breakers added automatically.
- Query-level `limit` is part of the named query contract. Runtime/API caller limits are only a caller-facing response cap.
- Projected query receipts retain source path/relationship evidence. User-facing projected results intentionally omit that source payload by default.
- `include` aliases must not collide with traversal aliases. Include anchors support `$entry`, `$result`, `$path.<alias>.source`, and `$path.<alias>.target`.
- Includes are one-hop side context. They do not advance the traversal frontier and do not fan out primary rows.
- `include.required: true` filters out a primary row when that include has no matches. `required: false` retains the row with `exists: false`, `count: 0`, and empty `items`.
- `include.many: false` expects at most one match and fails execution if multiple matches are found. Use `many: true` for repeated side context.
- Include `limit` is per include per primary row. It sets that include's `truncated` flag and is separate from query `limit`, `max_paths`, and `max_paths_per_result`.
- Include `order_by` refs may use `$edge`, `$source`, `$target`, or `$input`.

Projection refs:
- All shapes: `$input.<name>`, `$entry.entity_type`, `$entry.entity_id`, `$entry.properties.<name>`, `$entry.metadata.<path>`, `$result.entity_type`, `$result.entity_id`, `$result.properties.<name>`, `$result.metadata.<path>`.
- `result_shape: path`: `$path.<alias>.edge.*`, `$path.<alias>.source.*`, and `$path.<alias>.target.*`. Path refs require a traversal `as` alias.
- `result_shape: relationship`: `$relationship.*`, `$from_entity.*`, and `$to_entity.*`.
- Include refs: `$include.<alias>.exists`, `$include.<alias>.count`, `$include.<alias>.truncated`, `$include.<alias>.items`. Singular includes also support `$include.<alias>.edge.*`, `$include.<alias>.source.*`, and `$include.<alias>.target.*`; `many: true` includes require selecting `items`, `count`, or existence flags.

Projection and ordering example:

```yaml
named_queries:
  remediation_exposure_context:
    entry_point: Vulnerability
    returns: Asset
    result_shape: path
    dedupe: path
    traversal:
      - as: affected_product
        relationship: vulnerability_affects_product
        direction: outgoing
      - as: exposure
        relationship: asset_runs_product
        direction: incoming
    select:
      vulnerability_id: $entry.entity_id
      asset_id: $result.entity_id
      hostname: $result.properties.hostname
      exposure_edge_key: $path.exposure.edge.edge_key
      due_by: $path.exposure.edge.properties.due_by
      review_status: $path.exposure.edge.metadata.assertion.review.status
    order_by:
      - by: $path.exposure.edge.properties.due_by
        direction: asc
        value_type: date
      - by: $result.entity_id
        direction: asc
    max_paths: 500
    max_paths_per_result: 20
    limit: 50
```

Include example:

```yaml
named_queries:
  vulnerability_exposure_context:
    entry_point: Vulnerability
    returns: Asset
    result_shape: path
    relationship_state: reviewable
    traversal:
      - as: exposure
        relationship: asset_exposed_to_vulnerability
        direction: incoming
    include:
      owner:
        from: $result
        relationship: asset_owned_by
        direction: outgoing
      services:
        from: $result
        relationship: service_depends_on_asset
        direction: incoming
        many: true
        limit: 10
      exceptions:
        from: $result
        relationship: asset_has_exception
        direction: outgoing
        many: true
        where:
          target.properties.status:
            in: [active, approved]
    select:
      vulnerability_id: $entry.entity_id
      asset_id: $result.entity_id
      exposure_edge_key: $path.exposure.edge.edge_key
      owner_id: $include.owner.target.entity_id
      service_count: $include.services.count
      services: $include.services.items
      has_exception: $include.exceptions.exists
```

### TraversalStep

Each step in the traversal sequence:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `relationship` | string or list[string] | **yes** | — | Relationship name(s) to traverse. A list fans out across listed types in declared order and merges results. Candidates within each relationship type are stable-sorted when path budgets apply. |
| `direction` | string | no | `"outgoing"` | `outgoing`, `incoming`, or `both` |
| `filter` | dict | no | `null` | Property filters on edges or target entities |
| `target_filter` | dict | no | `null` | Exact-match property filters on candidate entities |
| `where` | dict | no | `null` | Structured traversal predicates. Top-level paths must start with `edge`, `source`, `target`, `current`, `candidate`, or `entry`. |
| `where_related` | list | no | `[]` | Related-edge predicates; at least one matching related edge must exist for each item |
| `where_not_related` | list | no | `[]` | Related-edge predicates; no matching related edge may exist for any item |
| `constraint` | string | no | `null` | Constraint expression to apply during traversal |
| `constraint_value_type` | string | no | `null` | Optional typed constraint comparison: `string`, `int`, `integer`, `float`, `number`, `bool`, `date`, or `datetime` |
| `exclude_if_related` | list | no | `[]` | Legacy related-edge exclusion checks |
| `max_depth` | int | no | `1` | BFS depth for this step (1 = direct neighbors only). Results include all entities from depth 1 through max_depth. |
| `required` | bool | no | `true` | Optional continuation. When `false`, preserves the incoming path if no edge passes relationship state, filters, predicates, related predicates, constraints, and policies. Matching edges still continue to the matched neighbor, which becomes the current `$result`. |
| `as` | string | no | `null` | Alias for the traversed path segment in path/relationship outputs |

**Optional continuation semantics:**

`required: false` makes a traversal step optional, but it does not attach
independent neighbor context to the same result row. When a non-required step
matches, traversal continues to the matched neighbor and that neighbor becomes
the current `$result`. When no candidate passes relationship state, filters,
predicates, related predicates, constraints, and policies, the incoming path is
preserved and `$result` remains the prior current entity.

Use `required: false` for optional successor, replacement, or follow-on paths.
Do not use it when the desired shape is "return this same asset, but attach
owner/service/control facts as additional row context." For that workflow, use
the named query to return the primary operational set and evidence path, then
inspect the returned entity and nearby relationships with read tools.

Future consideration: add attached related context for named queries if
workflows repeatedly need single-row projections containing non-advancing
neighbor facts.

**Direction semantics:**
- `outgoing`: Follow edges from entry point (source -> target)
- `incoming`: Follow edges into entry point (target -> source)
- `both`: Follow edges in either direction

**Structured predicate example:**

```yaml
named_queries:
  pending_exposures:
    entry_point: Vulnerability
    returns: asset_exposed_to_vulnerability
    result_shape: relationship
    relationship_state: pending
    allow_relationship_state_override: true
    traversal:
      - relationship: asset_exposed_to_vulnerability
        direction: incoming
        as: exposure
        where:
          edge.metadata.assertion.lifecycle.status:
            eq: active
          target.properties.environment:
            eq: production
        where_not_related:
          - relationship: asset_remediated_vulnerability
            direction: outgoing
            edge:
              properties.verification_status:
                eq: verified
            target:
              entity_id:
                eq: $entry.entity_id
```

Supported structured predicate operators are `eq`, `ne`, `in`, `not_in`, `lt`, `lte`, `gt`, `gte`, and `exists`. Predicate values may reference query inputs with `$input.<name>` and graph context values such as `$entry.entity_id`.

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

Common plumbing contracts are built in and do not need to be declared:

- `cruxible.EmptyInput`: no input fields and no extras.
- `cruxible.JsonObject`: any JSON-serializable object payload.
- `cruxible.JsonItems`: `{items: <json>}`.
- `cruxible.ParsedTabularBundle`: `{artifact, tables, files, diagnostics}` from the common tabular parser.

```yaml
contracts:
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
| `fields` | dict[str, PropertySchema] | **yes** | — | Field definitions. Contract fields must define `type` explicitly and are required by default. |
| `allow_extra` | bool | no | `false` | Allow undeclared JSON-serializable fields; used by `cruxible.JsonObject` |

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
    contract_in: cruxible.EmptyInput
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
| `contract_in` | string or inline ContractSchema | **yes** | — | Input contract reference. May be config-defined, built-in (`cruxible.*`), or an inline contract object. |
| `contract_out` | string or inline ContractSchema | **yes** | — | Output contract reference. May be config-defined, built-in (`cruxible.*`), or an inline contract object. |
| `ref` | string | **yes** | — | Callable reference (e.g., `module.function_name`) |
| `version` | string | **yes** | — | Semantic version for lock-file reproducibility |
| `deterministic` | bool | no | `true` | Whether the provider produces identical output for identical input |
| `artifact` | string | no | `null` | Name of artifact this provider depends on (must exist in `artifacts`) |
| `runtime` | string | no | `"python"` | Execution runtime |
| `side_effects` | bool | no | `false` | Whether the provider has side effects |
| `config` | dict | no | `{}` | Provider-specific configuration |

---

## workflows

Declarative step-based execution plans. Workflows compose queries, providers, and graph mutations into reproducible pipelines. A workflow `type` declares whether it is `utility`, `canonical`, `proposal`, or `decision_support`.

```yaml
workflows:
  build_public_kev_reference:
    type: canonical
    description: >
      Build the canonical public KEV reference layer from bundled data.
    contract_in: cruxible.EmptyInput
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
| `type` | `utility`, `canonical`, `proposal`, or `decision_support` | no | `utility` | Workflow contract for execution and agent-facing lifecycle |
| `contract_in` | string or inline ContractSchema | **yes** | — | Workflow input contract reference. May be config-defined, built-in (`cruxible.*`), or an inline contract object. |
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
| `shape_items` | Project, rename, require, and cast list-shaped rows | `shape_items: {items, include_input?, rename?, fields?, casts?, required?}`, `as` |
| `join_items` | Indexed inner join over two item sets | `join_items: {left_items, right_items, left_key, right_key, fields}`, `as` |
| `filter_items` | Filter rows with exact filters and comparisons | `filter_items: {items, where?, comparisons?}`, `as` |
| `dedupe_items` | Deterministically deduplicate rows | `dedupe_items: {items, keys, strategy?, rank?}`, `as` |
| `make_entities` | Build an entity set from list data | `make_entities: {entity_type, items, entity_id, properties}`, `as` |
| `make_relationships` | Build a relationship set from list data | `make_relationships: {relationship_type, items, from_type, from_id, to_type, to_id, properties}`, `as` |
| `apply_entities` | Apply a built entity set to graph state | `apply_entities: {entities_from}`, `as` |
| `apply_relationships` | Apply a built relationship set to graph state | `apply_relationships: {relationships_from}`, `as` |
| `make_candidates` | Build relationship candidates for governed proposals | `make_candidates: {relationship_type, items, from_type, from_id, to_type, to_id, properties}`, `as` |
| `map_signals` | Convert provider output to tri-state signal-source evidence | `map_signals: {signal_source, items, from_id, to_id, score/enum}`, `as` |
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
- `shape_items` returns `{items, input_count, output_count, dropped_count, drop_examples}`.
- `join_items` returns `{items, left_count, right_count, skipped_right_count, matched_left_count, output_count}`.
- `filter_items` returns `{items, input_count, output_count, filtered_count}`.
- `dedupe_items` returns `{items, input_count, output_count, duplicate_count, duplicate_examples}`.

### Dataflow Steps

Use dataflow steps for deterministic row mechanics that should be visible in
the workflow receipt rather than hidden inside a provider.

`shape_items` applies operations in this order: `rename -> fields -> casts ->
required`. Rename keys are top-level only. `fields` resolves against the
post-rename item and may overwrite projected keys. Casts are explicit and
support `str`, `int`, `float`, `bool`, and `json`; missing and `null` values are
left for `required` handling.

`join_items` currently supports `join_type: inner`. It indexes the right side by
the canonical JSON form of `right_key`, skips right rows with `null` keys, and
preserves left-row order with right-match order for one-to-many fanout.

`filter_items` uses exact-match/list-membership `where` rules plus comparison
predicates. `where` reads top-level item keys and may use literals or `$input.*`
refs only. Comparisons may use normal workflow refs, including `$item`,
`$input`, and prior `$steps`.

`dedupe_items` requires one or more keys and supports `first`, `last`, `max`,
and `min`. Ranked strategies require `rank`; missing ranks lose to present
ranks, and ties keep the earlier item.

Common providers and step types have different jobs: step types are
engine-owned deterministic workflow/dataflow mechanics, while common providers
remain reusable but opaque adapters, external services, model calls, or
domain-policy modules.

### Governed Proposal Steps

For `type: proposal` workflows that produce governed proposals (fuzzy matching, judgment calls), the three-step pattern is:

1. **`make_candidates`** — build candidate (from, to) pairs with properties
2. **`map_signals`** — convert provider scores/enums to tri-state signals per signal source
3. **`propose_relationship_group`** — assemble candidates + signals into a group proposal

The group then enters the resolution lifecycle (auto-resolve or manual review) based on the relationship's `proposal_policy` config.

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

The KEV triage overlay config (`kits/kev-triage/config.yaml`) demonstrates a release-backed overlay that extends a reference layer with governed judgment relationships. **Note:** This config requires composition with its base (`kits/kev-reference/config.yaml`) before it can be validated or loaded — `Vulnerability`, `Product`, and other reference types are defined in the base, not here:

```yaml
version: "1.0"
name: kev_triage
kind: world_model
extends: ../kev-reference/config.yaml
description: >
  Overlay of the KEV reference world model for internal vulnerability triage.

entity_types:
  Asset:
    description: Internal asset from CMDB, cloud inventory, or endpoint tooling.
    properties:
      asset_id: {primary_key: true}
      hostname: {indexed: true}
      criticality: {}
      environment: {}
      internet_exposed: {type: bool}

  Owner:
    description: Team or person responsible for an asset.
    properties:
      owner_id: {primary_key: true}
      name: {}
      team: {}

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
      installed_version: {}
      rationale: {}
    proposal_policy:
      signals:
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

# Operational configs load local state through workflows with providers,
# dataflow steps, make_entities/make_relationships, and apply_* steps.

```

See also the reference layer config (`kits/kev-reference/config.yaml`) for a complete example with workflows, providers, contracts, artifacts, and quality checks. Relationship-level `proposal_policy.signals` defines governed proposal policy.
