# Config Reference

Cruxible configs are YAML files that define a decision domain: entity
types, relationships, named queries, constraints, workflows, providers,
artifacts, quality checks, feedback profiles, outcome profiles, and decision
policies, plus mutation guards for configured state writes. This page
documents the explicit `CoreConfig` schema — the shape the engine actually
parses, validates, and executes.

> **For the graph-shape keys, author in [Compact](compact-config.md) first.**
> Compact provides short YAML grammars for entity types, relationships, named
> queries, mutation guards, and quality checks that expand deterministically
> into the schema on this page; it's what kits ship and what
> `kits/agent-operation/config.yaml` is written in. For those keys, reach for
> compact. This page stays the **complete schema reference**: every field's
> defaults and validation, the shape validation errors and internals speak,
> and the **long-tail fallback** (via compact's `explicit: true` escape hatch)
> for constructs compact doesn't reduce. It is also the **authoring how-to for
> the top-level keys compact does not reduce** — `gates`, `contracts`,
> `providers`, `workflows`, `feedback_profiles`, `outcome_profiles`,
> `decision_policies`, `artifacts`, `runtime`, `tests` — which are written in
> this explicit shape whether or not the rest of your file is compact.

## Top-Level Structure

```yaml
version: "1.0"
name: "my_domain"
description: "Optional description of this decision domain"
# extends: base-config.yaml  # release-backed overlay composition (see below)

entity_types: { ... }
relationships: [ ... ]
named_queries: { ... }
constraints: [ ... ]
gates: { ... }

# Governed workflow sections (all optional)
quality_checks: [ ... ]
feedback_profiles: { ... }
outcome_profiles: { ... }
mutation_guards: [ ... ]
decision_policies: [ ... ]
contracts: { ... }
artifacts: { ... }
providers: { ... }
workflows: { ... }
runtime:
  trace_payloads: preview
tests: [ ... ]
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `version` | string | no | `"1.0"` | Config schema version |
| `name` | string | **yes** | — | Unique name for this domain |
| `description` | string | no | `null` | Human-readable description |
| `extends` | string | no | `null` | Path to a base config for release-backed overlay composition (see [Config Composition](#config-composition)) |
| `cruxible_version` | string | no | `null` | Version of cruxible-core that produced this config (auto-stamped on save) |
| `entity_types` | dict | **yes**\* | — | Entity type definitions (\*optional when `extends` is set) |
| `relationships` | list | no | `[]` | Relationship definitions |
| `named_queries` | dict | no | `{}` | Declarative query definitions |
| `constraints` | list | no | `[]` | Validation rules |
| `gates` | dict | no | `{}` | Named repo gates evaluated by `cruxible gate check` |
| `quality_checks` | list | no | `[]` | Evaluate-time graph quality checks |
| `feedback_profiles` | dict | no | `{}` | Structured feedback vocabularies per relationship type |
| `outcome_profiles` | dict | no | `{}` | Structured outcome vocabularies for trust calibration |
| `mutation_guards` | list | no | `[]` | Reject direct graph mutations unless configured state-side conditions pass |
| `decision_policies` | list | no | `[]` | Action-side behavior rules for queries and workflows |
| `enums` | dict | no | `{}` | Shared enum vocabularies referenced by property schemas |
| `contracts` | dict | no | `{}` | Typed payload contracts for providers/workflows |
| `artifacts` | dict | no | `{}` | Pinned external artifacts referenced by providers |
| `providers` | dict | no | `{}` | Versioned executable leaves used by workflow steps |
| `workflows` | dict | no | `{}` | Declarative step-based execution plans |
| `runtime` | dict | no | `{trace_payloads: preview}` | Local runtime behavior options, including provider trace payload retention |
| `tests` | list | no | `[]` | Fixture-based workflow tests |

---

## Runtime Options

`runtime` controls local execution and audit-capture behavior that is not part
of the state model itself.

```yaml
runtime:
  trace_payloads: preview
  default_write_policy: direct
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `trace_payloads` | string | no | `"preview"` | Provider trace payload retention: `"full"`, `"preview"`, or `"metadata"` |
| `default_write_policy` | string | no | `"direct"` | Instance-wide default direct-write governance: `"direct"` or `"proposal_only"`. Applies to entity/relationship types whose own `write_policy` is unset. See [Direct-Write Governance](#direct-write-governance-refuse_direct_writes). |

Trace payload retention controls what is persisted in provider execution traces:

- `full` stores full provider `input_payload` and `output_payload` bodies inline
  in local SQLite trace rows.
- `preview` stores small payload bodies inline, but replaces large payloads with
  bounded deterministic previews plus digest/byte-count metadata.
- `metadata` stores no full provider payload bodies; trace payload fields contain
  omission placeholders plus digest/byte-count metadata.

Local SQLite does not provide cold storage or later hydration for omitted
payload bodies. Choose `full` only when local full-body provider provenance is
more important than trace database size.

---

## Direct-Write Governance (`refuse_direct_writes`)

The `CRUXIBLE_MODE` permission tiers (`read_only` ⊂ `governed_write` ⊂
`graph_write` ⊂ `admin`) are **cumulative**: a `graph_write` actor can both
direct-add a fact *and* propose one. The tiers therefore cannot express "this
domain is proposal-only" — a per-domain governance axis. `refuse_direct_writes`
adds that axis.

A type marked **`proposal_only`** refuses bare direct graph-write verbs
(`add_entity` / `add_relationship` / `batch_direct_write` / the typed lifecycle
write) and forces state in only through the governed proposal/workflow path. It
is a **hard constraint, independent of permission tier** — even `admin` is
refused. The refusal raises `DirectWriteRefusedError` (HTTP **403**,
`error_code: direct_write_refused`).

An entity type marked **`mint_only`** (entity types only — not relationships) is
stricter still: it is writable **only** by the internal `token_mint` source and
refuses *all* other sources, including the governed verbs `workflow_apply` /
`group_resolve`. Because a workflow `make_entities` step would later apply through
`workflow_apply` and bypass the chokepoint refusal, a config whose `make_entities`
targets a `mint_only` entity type is **rejected at config load** (fail-closed).
Use it for auth-managed identity types: auth-on daemons materialize them from runtime credentials; auth-off daemons materialize a declared local `operator` identity through the same internal `token_mint` source.

**Scope.** `refuse_direct_writes` governs how state is *created* — it forces the
direct-write verbs above through the proposal/workflow path. It does **not**
govern the `feedback` review channel: promoting an already-staged (`pending`)
edge to live, or correcting an existing edge, goes through `feedback` — a
separate path gated by **reviewer identity** (credential-backed when server auth
is on; attributed to the declared local `operator` when auth is off). So
`proposal_only` guarantees that
*creation* is governed; *promotion* of a staged edge is exactly as strong as the
feedback review-gate, no stronger.

Three knobs control it:

| Knob | Where | Values | Effect |
|------|-------|--------|--------|
| `write_policy` (per type) | `entity_types.<T>` / `relationships[]` | `direct` \| `proposal_only` \| `mint_only` (entity types only) \| unset | Per-type policy. Unset inherits the instance default. An explicit `direct` opts out of the instance default (but **not** the env kill-switch). `mint_only` (entity types only) is stricter than `proposal_only`: the type is writable **only** by the internal `token_mint` source and refuses all other sources, including the governed verbs `workflow_apply` / `group_resolve`. A config whose workflow `make_entities` step targets a `mint_only` entity type is rejected at load (fail-closed). |
| `default_write_policy` | `runtime` | `direct` (default) \| `proposal_only` | Instance-wide default for types whose own `write_policy` is unset. |
| `CRUXIBLE_REFUSE_DIRECT_WRITES` | process env (daemon) | truthy (`1`/`true`/`yes`/`on`) | Daemon-wide **kill-switch** for the direct-write verbs: forces `proposal_only` for every type *at the write chokepoint*, overriding every per-type opt-out and the default. (Chokepoint only — the feedback review/promotion path is separate; see Scope above.) |

**Effective policy (union — any path to `proposal_only` wins):** a write is
refused when the env kill-switch is set **OR** the type's explicit `write_policy`
is `proposal_only` **OR** the type's `write_policy` is unset and
`runtime.default_write_policy` is `proposal_only`.

| `CRUXIBLE_REFUSE_DIRECT_WRITES` | type `write_policy` | `default_write_policy` | Effective |
|---|---|---|---|
| unset | unset | `direct` | direct |
| unset | unset | `proposal_only` | proposal_only |
| unset | `direct` | `proposal_only` | **direct** (opts out) |
| unset | `proposal_only` | `direct` | proposal_only |
| set | `direct` | `direct` | **proposal_only** (env wins) |
| set | unset | `direct` | proposal_only |

**Always permitted, regardless of policy:**

- Relationship writes with **`pending: true`** — they stage an edge for review
  and are not live. (Entities have no pending path; a direct entity add of a
  `proposal_only` type is refused outright — add it through a canonical
  `apply_entities` workflow.)
- Governed verbs: proposal **group resolution** (`group propose` → resolve) and
  **canonical workflow apply** (`apply_entities` / `apply_relationships`).

The default (everything unset) is byte-identical to pre-`refuse_direct_writes`
behavior — all direct writes succeed.

---

## Config-Declared Write Tiers (`write_tier`)

`write_tier` is the tier-ladder complement of `write_policy`. Where
`write_policy` answers "may this type be direct-written **at all**?" (a hard,
tier-independent constraint), `write_tier` answers "**which permission tier**
may direct-write it?". By default the direct-write verbs (`add_entity` /
`add_relationship` / `batch_direct_write`) require `graph_write`; a type may
declare a lower requirement:

```yaml
entity_types:
  StateNote:
    write_tier: governed_write   # governed_write actors may direct-write notes

relationships:
  - state_note_about_work_item: StateNote -> WorkItem
    write_tier: governed_write   # ...and attach them, in the same payload
```

Semantics:

- **Allowed values:** `governed_write` or `graph_write` (an explicit
  restatement of the default). `read_only` is rejected — it is not a write
  tier. `admin` is rejected — a declared tier only ever *lowers* the
  requirement below `graph_write`; restricting writes harder than the tier
  ladder is `write_policy`'s job (`proposal_only` / `mint_only`).
- **Per-payload requirement = max over touched types.** A direct write whose
  payload touches only types declared at-or-below the caller's tier is
  permitted; any type without `write_tier` keeps requiring `graph_write`, so a
  mixed payload is gated at its strictest member. An empty payload keeps the
  classic `graph_write` requirement.
- **Relationship writes contribute only the relationship type.** The edge is
  the thing mutated; endpoint entities are referenced, not written — a
  governed-tier edge may attach to entities the caller could not write
  directly.
- **Creates and updates are one surface.** The tier declares who may
  direct-write the type, not which verb flavor they use.
- **Feedback corrections honor the same tier.** A feedback `correct` with
  property corrections applies edge `property_updates` — the same mutation a
  direct relationship write performs — so it is gated at the corrected
  relationship type's `write_tier` (default `graph_write`) across `feedback`,
  `feedback_batch` (strictest corrected type in the batch), and
  `feedback_from_query` (the receipt-selected edge is resolved before the
  check). Review-state transitions themselves (`approve` / `reject` / `flag`
  and a `correct` without corrections) stay at the tools' `governed_write`
  tier; under server auth every feedback action must carry a resolved actor
  identity — review state cannot be promoted *or* retracted anonymously.
- **Everything downstream is unchanged.** Mutation guards, `write_policy`
  refusals, and validation all run after the tier check. Declaring
  `write_tier` together with an explicit `proposal_only`/`mint_only`
  `write_policy` is rejected at config load (the declared tier could never
  take effect).

Core stays domain-agnostic: it never knows what a "note" is — kits decide
which types form their low-trust write surfaces.

---

## Config Composition

The `extends` field enables an **overlay pattern** for release-backed state publishing. A published upstream state model provides entity types, relationships, and workflows; a downstream overlay adds its own internal extensions without duplicating the base.

**How it works:** `cruxible_validate` resolves `extends` recursively, linearizes
the chain base-first, deduplicates shared file layers by resolved path, composes
in memory, and validates the result. Cycles are rejected with the full path
chain. The raw `load_config()` function still parses one file; composition
happens in the service/CLI layer. For inline `config_yaml` (no file path),
`extends` must use an absolute path or validation will error.

At runtime, reload materializes the composed config to the instance-owned active
file. The file is stamped `MATERIALIZED - DO NOT EDIT`. Instance metadata records
the exact digest and root-relative label of every authored layer, semantic digests of the
last-reloaded source composition and current active config, and the exact-byte
digest of the generated active file.

For uploaded configs, the daemon verifies the reported composed digest against
the uploaded effective config. Source labels and per-layer digests describe the
client-side authored files for later drift comparison; they are not independent
authentication of files the daemon cannot read.

`cruxible config status --config <authored-root>` distinguishes source changes
that need a reload from hand edits to the active materialization. Governed daemon
startup refuses a recorded active-file mismatch. For recovery only, start with
`CRUXIBLE_ALLOW_CONFIG_INTEGRITY_MISMATCH=true`, reload from the authored source,
then remove the override.

```yaml
# overlay config — validated by composing with the base automatically
version: "1.0"
name: kev_triage
extends: ../kev-reference/config.yaml
description: >
  Overlay of the KEV reference state for internal vulnerability triage.

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
| Runtime options | `runtime` | Overlay runtime options override base runtime options |
| Safe lists | `constraints`, `quality_checks`, `mutation_guards`, `tests`, `decision_policies` | Overlay appends to base |
| Relationships | `relationships` | Overlay can only add new names; redefining an upstream relationship raises `ConfigError` |
| Keyed maps | `entity_types`, `named_queries`, `enums`, `feedback_profiles`, `outcome_profiles`, `contracts`, `artifacts`, `providers`, `workflows` | Overlay can only add new keys; redefining an upstream key raises `ConfigError` |
| Other fields | everything else | Overlay can only set if not in base, or if equal to base value |

When `extends` is set, `entity_types` may be empty — the base provides them.

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
| `write_policy` | string | no | `null` | `"direct"`, `"proposal_only"`, or `"mint_only"`. Governs direct entity adds for this type — see [Direct-Write Governance](#direct-write-governance-refuse_direct_writes). `"mint_only"` is stricter than `"proposal_only"`: writable only by the internal `token_mint` source, refusing all other sources including `workflow_apply` / `group_resolve` (a config wiring a `mint_only` type into a workflow `make_entities` step is rejected at load). `null` inherits `runtime.default_write_policy`. |
| `write_tier` | string | no | `null` | `"governed_write"` or `"graph_write"`. Minimum permission tier allowed to direct-write this type — see [Config-Declared Write Tiers](#config-declared-write-tiers-write_tier). `null` keeps the default `graph_write` requirement. Rejected together with an explicit `proposal_only`/`mint_only` `write_policy`. |

### PropertySchema

Each property within an entity type (or relationship) is defined with:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | string | no for graph properties; yes for contract fields | `"string"` for graph properties | Data type: `string`, `int`, `integer`, `float`, `number`, `bool`, `date`, `datetime`, `json` |
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
  criticality:
    description: Shared rank from lowest to highest.
    values: [low, medium, high, critical]
    ordered: low_to_high

entity_types:
  Asset:
    properties:
      asset_id: {primary_key: true}
      status: {enum_ref: asset_status}
```

Only an enum declared with `ordered: low_to_high` (values listed lowest to
highest) may be referenced as an `order_by` `enum_ref` in a named query;
referencing an unordered enum there is a config validation error.

Enum values must be non-empty and unique. With `extends`, overlays may add new
enum names but may not redefine or extend upstream enum vocabularies.

> **Authoring note — domain `status` vs. entity lifecycle.** A domain `status`
> enum should model **progress / workflow** states (e.g. `planned`, `active`,
> `closed`). Entity **retirement / deletion** is a *different axis* — "does this
> entity still exist / is it live" — and is the canonical way to "delete" an
> entity. It lives in the **canonical core entity lifecycle**
> `lifecycle.status` (uniform across all entities the way relationship lifecycle
> already is), **not** a per-kit `status` value. When authoring a kit, keep
> retirement-flavored values (`retired`, `decommissioned`, `superseded`) **out**
> of your `status` enum — the `asset_status` example above mixes the two for
> illustration, but the canonical soft-delete is the entity lifecycle, not a
> status value. This keeps "where is this in its workflow" separate from "is this
> entity still live."

### Entity `lifecycle.status` (read visibility)

Every entity carries an optional, **typed** lifecycle state. It is the typed
`lifecycle` field of the `EntityMetadata` envelope (an `EntityLifecycleState`,
validated on write), the entity analogue of the relationship's
`RelationshipMetadata.assertion.lifecycle`. It serializes onto the stored entity
metadata under the `lifecycle` key:

```yaml
# Stored/serialized shape — NOT hand-authored; written via the typed channel.
metadata:
  lifecycle:
    status: live   # one of: live | superseded | retired (default live)
    reason: "replaced by WI-204"       # optional
    closed_at: "2026-06-23T00:00:00Z"  # optional (shared closed_at/closed_by audit pair)
```

The lifecycle shares its structure with the relationship lifecycle (same
`reason`, effective window, `closed_at`/`closed_by` audit pair, and supersession
links); only the `status` vocabulary differs (`live|superseded|retired`
for entities vs `active|inactive|superseded|retracted` for relationships).
`orphaned` is **not** an authorable entity lifecycle status — an orphaned entity
is a derived evaluate/health finding (surfaced as `integrity.orphan_entity_count`),
not a state you set.

- **Default is `live`.** An entity with no `lifecycle` metadata is treated as
  live, so existing data needs no migration to keep current behavior.
- Set it through the **typed lifecycle write channel** — `entity update
  --lifecycle-status retired [--lifecycle-reason "…"]`, or `batch-direct-write`
  with the typed `lifecycle` field on the entity input. The status is validated
  against the entity lifecycle vocabulary; lifecycle is a **typed field**, not a
  free-form metadata blob. A `lifecycle` key inside free-form `metadata` is **not**
  a way to set it — it is carried inertly as ordinary metadata (under the typed
  envelope's free-form slot) and never changes the entity's lifecycle. There is no
  reserved metadata key and no special retire verb.
- **Read gating is uniform.** Every read path (`query`, `list entities`,
  traversal/relationship reads, and the MCP/HTTP equivalents) defaults to
  **live-only**: a `retired`/`superseded` entity is hidden. The one
  exception is an explicit **by-id `entity get`**, which always returns the
  entity and shows its `lifecycle.status` (the recovery/inspection path).
- The `--state` selector (config field `relationship_state`) controls
  visibility: `live` (default), `not-live` (only the gated-out set), `all`
  (everything). For entities the review-only values (`accepted`/`pending`/
  `reviewable`) resolve to `live`, since an entity has no review axis.

`ordered: low_to_high` marks a shared enum as semantically ranked. The order of
`values` is the rank order from lowest to highest. Query `order_by` clauses can
reference ordered enums with `enum_ref` to sort by rank instead of lexical string
order; `direction: asc` means low-to-high and `direction: desc` means
high-to-low.

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
      affected_basis: {}
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
| `write_policy` | string | no | `null` | `"direct"` or `"proposal_only"`. Governs direct edge writes for this type — see [Direct-Write Governance](#direct-write-governance-refuse_direct_writes). `null` inherits `runtime.default_write_policy`. |
| `write_tier` | string | no | `null` | `"governed_write"` or `"graph_write"`. Minimum permission tier allowed to direct-write edges of this type — see [Config-Declared Write Tiers](#config-declared-write-tiers-write_tier). Endpoint entity types are not part of the check. `null` keeps the default `graph_write` requirement. Rejected together with an explicit `proposal_only` `write_policy`. |

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

A dict of declarative query definitions. Every query declares an explicit
`mode`: `traversal` starts from an entry entity and walks relationship steps;
`collection` enumerates one entity type or relationship type directly.

```yaml
named_queries:
  parts_for_vehicle:
    mode: traversal
    description: "Find all parts that fit a specific vehicle"
    entry_point: Vehicle
    traversal:
      - relationship: fits
        direction: incoming
        filter:
          verified: true
    returns: "list[Part]"

  compatible_replacements:
    mode: traversal
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

  all_active_fitments:
    mode: collection
    description: "List live fitment relationships"
    result_shape: relationship
    returns: fits
    where:
      edge.properties.verified:
        eq: true
```

### NamedQuerySchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `mode` | string | **yes** | — | Query mode: `traversal` or `collection` |
| `description` | string | no | `null` | Human-readable description |
| `entry_point` | string | for traversal | — | Entity type to start a traversal query from; invalid for collection queries |
| `traversal` | list | for traversal | — | Non-empty sequence of traversal steps; invalid for collection queries |
| `returns` | string | **yes** | — | Description of the return type |
| `result_shape` | string | no | `"path"` | Output shape: `entity`, `path`, or `relationship` |
| `dedupe` | string | no | shape-dependent | Result dedupe mode: `entity`, `path`, or `none`. Entity queries default to `entity`; path and relationship queries default to `path`. |
| `relationship_state` | string | no | `"live"` | Read-visibility state: `live`, `accepted`, `all`, `not-live`, `pending`, or `reviewable`. Gates entities by lifecycle and edges by review+lifecycle (see Read visibility below). The runtime/CLI selector for this is the `--state` flag (`state` on MCP/HTTP). |
| `allow_relationship_state_override` | bool | no | `false` | Whether runtime callers may override the visibility state |
| `where` | dict | no | `null` | Top-level predicate map for collection queries; invalid for traversal queries |
| `select` | dict | no | `null` | Projection map from output field name to query reference or literal value. When present, user-facing rows return `{values}` while receipts preserve source evidence for audit and feedback. |
| `order_by` | list | no | `[]` | Deterministic ordering rules. Each item uses `by`, optional `direction` (`asc` or `desc`), optional `value_type` (`string`, `int`, `integer`, `float`, `number`, `bool`, `date`, or `datetime`), and optional ordered `enum_ref`. |
| `include` | dict | no | `{}` | One-hop side-context includes keyed by alias. Includes decorate each primary row without advancing traversal or fanning out primary rows. |
| `limit` | int | no | `null` | Query-level output cap applied after traversal, dedupe, path budgets, ordering, and before projection. Result metadata reports pre-limit `total_results`, effective `limit`, and `limit_truncated`. |
| `max_paths` | int | no | `null` | Traversal-time retained-path frontier budget. It caps retained path states for each traversal step, limiting memory and receipt growth. It is not a total candidate-evaluation budget. |
| `max_paths_per_result` | int | no | `null` | Post-traversal final retained-path-per-result cap applied after traversal/dedupe, before ordering and `limit`. It does not bound traversal work. |

Validation rules:
- `mode: collection` queries omit `entry_point`, `traversal`, `include`, `max_paths`, and `max_paths_per_result`.
- Collection queries support `result_shape: entity` with `returns` set to an entity type, or `result_shape: relationship` with `returns` set to a canonical relationship name. Reverse aliases are rejected so direction is unambiguous.
- `mode: traversal` queries require `entry_point` and at least one traversal step. Put filters on traversal steps, include blocks, or related predicates; top-level `where` is reserved for collection queries.

Collection `where` scopes:
- `result_shape: entity`: paths start with `result` — e.g. `result.properties.status: {in: [active]}`.
- `result_shape: relationship`: `edge.properties.<field>` filters the edge itself (field names are validated against the relationship's configured schema), and `source.` / `target.` filter the endpoint entities — e.g. `source.properties.status: {eq: open}` keeps only edges whose from-entity is an open incident. `entry`, `current`, and `candidate` are accepted aliases (entry and current resolve to the from-entity, candidate to the to-entity); prefer `source`/`target` for readability.
- Traversal queries that intentionally return mixed entity types can set `returns: AnyEntity`; this skips homogeneous entity-type validation for entity/path rows.
- `result_shape: entity` requires `dedupe: entity`.
- For a traversal query with `result_shape: entity`, a concrete entity `returns` type, and `max_depth` on the final step, the engine may traverse through intermediate entity types and collect only the declared return type when at least one relationship in that final step can reach it. This is read-time typed collection, not a virtual or materialized relationship.
- `result_shape: relationship` requires `dedupe: path` or `none`.
- `relationship_state: pending` requires `result_shape: path` or `relationship`, and does not allow `dedupe: entity`.
- `relationship_state: reviewable` requires traversal `result_shape: path` or collection `result_shape: relationship`, and does not allow `dedupe: entity`.
- `required: false` traversal steps are optional continuations, not independent context enrichment. They require `result_shape: path` or `relationship`.
- `result_shape: relationship` may use `required: false` optional-continuation steps only when the final returned relationship step is still required.
- `max_paths` and `max_paths_per_result` require `result_shape: path` or `relationship`, and must be positive integers when set.
- `max_paths` is the retained-path frontier safety control. Once reached for a traversal step, the engine stops retaining/enqueuing more path states and avoids recording traversal receipts for the skipped frontier. Candidates that fail filters before any path is retained can still be evaluated; use a future candidate/work budget if total edge evaluation needs a separate cap.
- `max_paths_per_result` is a result-time evidence cap. It trims retained paths per final result entity after traversal, when result identity is known. It is distinct from `limit`: `max_paths_per_result` controls evidence fanout per result, while `limit` controls how many ordered rows are returned.
- `order_by` runs after traversal, dedupe, and path budgets, before `limit`.
- `order_by.value_type` and `order_by.enum_ref` are mutually exclusive.
- `order_by.enum_ref` must reference a top-level enum with `ordered: low_to_high`.
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
- Traversal queries with `result_shape: entity` may use includes only with `select`, so include values are projected explicitly while raw entity rows remain unchanged.
- `include.required: true` filters out a primary row when that include has no matches. `required: false` retains the row with `exists: false`, `count: 0`, and empty `items`.
- `include.many: false` expects at most one match and fails execution if multiple matches are found. Use `many: true` for repeated side context.
- Include `limit` is per include per primary row. It sets that include's `truncated` flag and is separate from query `limit`, `max_paths`, and `max_paths_per_result`.
- Include `order_by` refs may use `$edge`, `$source`, `$target`, or `$input`.

Relationship state modes:
- `live` includes active relationships whose review state is neither `pending` nor `rejected`. This includes deterministic/unreviewed state and approved state.
- `accepted` includes active relationships whose review status is `approved`.
- `pending` includes active relationships whose review status is `pending`.
- `reviewable` includes `live` relationships plus pending relationships. Use this for triage/context queries where an agent should see both accepted state and still-reviewable proposals in one evidence path.

Projection refs:
- All shapes: `$input.<name>`, `$entry.entity_type`, `$entry.entity_id`, `$entry.properties.<name>`, `$entry.metadata.<path>`, `$result.entity_type`, `$result.entity_id`, `$result.properties.<name>`, `$result.metadata.<path>`.
- `result_shape: path`: `$path.<alias>.edge.*`, `$path.<alias>.source.*`, and `$path.<alias>.target.*`. Path refs require a traversal `as` alias.
- `result_shape: relationship`: `$relationship.*`, `$from_entity.*`, and `$to_entity.*`.
- Include refs: `$include.<alias>.exists`, `$include.<alias>.count`, `$include.<alias>.truncated`, `$include.<alias>.items`. Singular includes also support `$include.<alias>.edge.*`, `$include.<alias>.source.*`, and `$include.<alias>.target.*`; `many: true` includes require selecting `items`, `count`, or existence flags.

Projection and ordering example:

```yaml
named_queries:
  remediation_exposure_context:
    mode: traversal
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
      priority: $path.exposure.edge.properties.priority
      review_status: $path.exposure.edge.metadata.assertion.review.status
    order_by:
      - by: $result.properties.criticality
        direction: desc
        enum_ref: criticality
      - by: $path.exposure.edge.properties.priority
        direction: desc
        enum_ref: criticality
      - by: $result.entity_id
        direction: asc
    max_paths: 500
    max_paths_per_result: 20
    limit: 50
```

Include example:

```yaml
named_queries:
  vulnerability_asset_context:
    mode: traversal
    entry_point: Vulnerability
    returns: Asset
    result_shape: path
    relationship_state: reviewable
    traversal:
      - as: affected_product
        relationship: vulnerability_affects_product
        direction: outgoing
      - as: installed_product
        relationship: asset_runs_product
        direction: incoming
    include:
      exposure:
        from: $result
        relationship: asset_vulnerability_posture
        direction: outgoing
        many: true
        where:
          edge.properties.status:
            eq: exposed
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
```

This returns the primary vulnerability-to-asset path once per row, with the
configured owner, service, exposure, and exception context attached under the
row's `includes` map. Include context can also be selected with
`$include.<alias>...` projection refs.

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
| `max_depth` | int | no | `1` | BFS depth for this step (1 = direct neighbors only). By default, results include entities from depth 1 through max_depth; final-step typed collection on entity-shaped traversal queries may traverse intermediates while emitting only the declared `returns` type. |
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
owner/service/control facts as additional row context." Use `include` for that:
it attaches bounded one-hop side context to each primary result row without
changing the traversal result or fanning out rows. Use read tools for ad hoc
context that is not worth baking into the named query contract.

**Direction semantics:**
- `outgoing`: Follow edges from entry point (source -> target)
- `incoming`: Follow edges into entry point (target -> source)
- `both`: Follow edges in either direction

**Structured predicate example:**

```yaml
named_queries:
  pending_exposures:
    mode: traversal
    entry_point: Vulnerability
    returns: asset_vulnerability_posture
    result_shape: relationship
    relationship_state: pending
    allow_relationship_state_override: true
    traversal:
      - relationship: asset_vulnerability_posture
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

Supported structured predicate operators are `eq`, `ne`, `in`, `not_in`,
`lt`, `lte`, `gt`, `gte`, `exists`, `contains`, and `icontains`. `contains`
and `icontains` require string values; `icontains` compares case-insensitively.
Predicate values may reference:

- `$input.<field>`
- `$entry.<field>`
- `$current.<field>`
- `$candidate.<field>`
- `$edge.<field>`
- `$source.<field>`
- `$target.<field>`
- `$path.<alias>.edge.<field>`
- `$path.<alias>.source.<field>`
- `$path.<alias>.target.<field>`

Use `$path` references when filtering an include or predicate against an
already-retained traversal path. Unknown path aliases fail unless the alias
belongs to an absent `required: false` traversal segment, where the missing
path behaves like a missing value and ordinary predicates fail. `$path`
references target existing traversal aliases, not include aliases.

```yaml
include:
  remediations:
    from: $path.exposure.source
    relationship: asset_remediated_vulnerability
    direction: outgoing
    many: true
    where:
      target.entity_id:
        eq: $path.exposure.target.entity_id
```

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

## gates

Named repo gate declarations evaluated by [`cruxible gate check`](cli-reference.md#cruxible-gate-check). Doctrine: a **guard** blocks a write INTO state (inbound — see `mutation_guards`); a **gate** lets the world act only if state agrees (outbound). Gates are outbound exclusively.

A gate is **kind-based**: `kind` names the source adapter that supplies candidate values. `generic` accepts caller-supplied values from newline-delimited stdin or repeatable `--candidate` arguments. `git-pre-push` reads git's pre-push hook protocol and yields every merged-in parent of each pushed merge commit. A candidate is **satisfied** when at least one entity of `entity_type` carries the candidate in `match_property` AND matches the declared `condition`. Core knows no ontology — the declaration supplies it, so kit evolution updates the declaration while the verb and invocation never hardcode domain knowledge.

```yaml
gates:
  merge-review:
    description: Merges to main need an approved review pinning the merged tip.
    kind: git-pre-push
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
    adapter: {branch_pattern: refs/heads/main}
```

For a non-git pre-action check, declare a `generic` gate without an `adapter`.
There are two styles, and the difference is what the gate's condition reads.

**Derivational gating — prefer it whenever permissibility is derivable.** The
condition queries world facts that exist independently of any one action, so
one set of facts answers many future questions. Here a deploy pipeline asks
whether the service it is about to ship is clear of live incidents:

```yaml
gates:
  deploy-clear-of-incidents:
    description: A service deploys only while no live incident names it.
    kind: generic
    entity_type: Service
    match_property: service_id
    condition: {incident_status: clear}
```

```bash
cruxible gate check deploy-clear-of-incidents --candidate "$SERVICE_ID" \
  && ./deploy "$SERVICE_ID"
```

**Decisional gating — for commitments, not for missing facts.** The only
decisions that belong in state as entities are *constitutive* acts — sign-offs,
risk acceptance, authorization — where the act creates the fact rather than
reports it. No world model derives a signature; the commitment is itself a
world fact, so model it fully: actor, rationale, evidence, timestamps — an
entity whose meaning outlives the action it gates — never a bare
`{action_id, status}` token (a write-once approval token turns the graph into
a message queue and should be a red flag in review).

If a human is approving because the *state could not answer a derivable
question*, that is not a decisional gate — it is an ontology gap. Minting
verdict entities there relieves exactly the pressure that should improve the
model. Treat each such judgment as debt: record what the decider consulted,
and when those facts land in state, promote the gate to a derivational
condition and retire the verdict type. A mature instance trends toward
derivational gates everywhere except its genuine commitments.

```yaml
gates:
  irreversible-action-approved:
    description: Each irreversible action needs a reviewed, live decision.
    kind: generic
    entity_type: Decision
    match_property: approves_action_id
    condition: {status: approved}
```

A decisional gate is only as strong as the write rules behind its entity:
declare the decision type `governed` (or guard it) so approvals reach `live`
through review, not through the acting agent's own direct write.

An external process can emit one candidate value per line (blank lines are
ignored; surrounding whitespace is trimmed):

```bash
verdict-layer pending-actions --format lines \
  | cruxible gate check irreversible-action-approved
```

For direct invocation, pass the same values as repeatable arguments:

```bash
cruxible gate check irreversible-action-approved \
  --candidate "$ACTION_ID" \
  --candidate "$SECOND_ACTION_ID"
```

`--candidate` is supported only by `generic` gates and replaces stdin when present. An empty or blank-only generic input cannot establish a verdict and fails closed with exit 2.

### GateSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `kind` | string | **yes** | — | Source adapter that supplies candidate values. Supported kinds: `generic`, `git-pre-push`; lint rejects unknown kinds |
| `entity_type` | string | **yes** | — | Entity type to consult; must be declared in `entity_types` |
| `match_property` | string | **yes** | — | Property a candidate value is matched against (for `git-pre-push`, the pinned commit SHA); must exist on `entity_type` |
| `condition` | dict | **yes** | — | Property=value pairs meaning *satisfied* (ANDed); each property must exist on `entity_type`, may not include `match_property`, and at least one pair is required. The key `query` is reserved for a future named-query condition variant |
| `adapter` | dict | for `git-pre-push` | `null` | Kind-specific adapter config. `git-pre-push` requires `branch_pattern`: a glob over remote ref names (e.g. `refs/heads/main`, `refs/heads/release-*`) selecting which pushed refs are gated. `generic` does not accept adapter config |
| `description` | string | no | `null` | Human-readable description |

Unknown keys are refused, and config lint rejects unknown kinds and undeclared entity types or properties. In overlay composition, an overlay may add new gates but cannot redefine an upstream gate.

---

## quality_checks

Evaluate-time graph quality checks run during `cruxible_evaluate`. Six check kinds are available, distinguished by the `kind` field.

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

### 6. relationship_property_consistency

Check that an entity property agrees with a related entity reached through a
specific relationship. Use this when configs intentionally keep denormalized
inspection fields but still need the canonical relationship to stay aligned.

```yaml
  - name: product_vendor_id_matches_vendor_edge
    kind: relationship_property_consistency
    severity: error
    entity_type: Product
    relationship_type: product_from_vendor
    direction: outgoing
    source_property: vendor_id
    target_property: vendor_id
    allow_missing_source: false

  - name: product_vendor_name_matches_vendor_edge
    kind: relationship_property_consistency
    severity: warning
    entity_type: Product
    relationship_type: product_from_vendor
    direction: outgoing
    source_property: vendor_name
    target_property: name
    allow_missing_source: true
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"relationship_property_consistency"` | |
| `entity_type` | string | Source entity type to check |
| `relationship_type` | string | Relationship connecting source to related entity |
| `direction` | `"incoming"` or `"outgoing"` | Edge direction relative to the source entity |
| `source_property` | string | Source entity property to compare |
| `target_property` | string | Related entity property to compare; omit or use `entity_id` to compare against the related entity id |
| `allow_missing_source` | bool | Skip rows where the source property is absent or empty |

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
| `surface_type` | string | `null` | Required for `anchor_type: receipt` — `"query"`, `"workflow"`, or `"operation"`. Only `"query"` and `"workflow"` are validated and bound to a surface; `"operation"` is accepted but inert (no binding). |
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

## mutation_guards

Mutation guards reject configured state writes when a configured state-side
condition does not pass. They are enforced on direct entity writes, batch direct
writes, and canonical workflow apply: entity-property guards on entity writes;
relationship-evidence guards (the evidence-requirement condition, scoped to a
relationship type) on relationship writes. Release-backed `state pull`
re-materialization is exempt, since it replays already-accepted upstream state
rather than authoring new writes.
They are appendable in overlay composition and are not allowed in `kind:
ontology` configs.

Guards run on *writes*, not on state reconciliation. The upstream pull-apply on a
release-backed overlay (`cruxible state pull`) re-materializes the new upstream
release plus the overlay's existing local state and is deliberately guard-exempt:
the local side is a re-materialization of state that already passed its guards
when authored (re-materializing an unchanged value is not a transition and never
fires a guard), the upstream side is governed/published state that this overlay
must not re-litigate, and there is no write actor at merge time for actor-identity
guards to evaluate. The one merge-time risk that is genuinely novel — local edges
dangling onto upstream entities removed in the new release — is enforced
separately as a blocking pull conflict before the merge is materialized.

Entity-property guards fire on any direct write that **results in** the guarded
property value — creating an entity with the value and changing an existing
entity to the value are both covered. Updates that re-assert the value an entity
already holds are not transitions and do not fire. The one exception is the
`frozen` condition, which has no `new_value`: it fires on **any change** to the
guarded property (updates only — creates set it freely; see
[FrozenPropertyGuardCondition](#frozenpropertyguardcondition-type-frozen)).

Relationship evidence guards fire on writes to the configured relationship type
and require the resulting relationship evidence to meet the configured floor.
Use them for observation-style relationships whose claims must cite
dereferenceable source material. Decision-style relationships should usually
declare no evidence floor and rely on ambient attribution: provenance, receipts,
actor context, and review history. Every guard field is load-bearing.

The `condition` is a **discriminated union** keyed by an explicit `condition.type`
field — one of `query`, `actor`, `co_write`, `evidence`, or `frozen`. The type is
required; guard shape is never inferred from which keys are present. The
guard-level `operation` / `effect` discriminator fields deliberately do not exist.

```yaml
mutation_guards:
  - name: work_item_closed_requires_review
    entity_type: WorkItem
    property: status
    new_value: closed
    condition:
      type: query
      query_name: approved_review_for_work_item
      params:
        work_item_id: "$entity.entity_id"
      min_count: 1
    message: "Work item cannot be closed until approved review exists."

  - name: review_request_approval_requires_authorized_actor
    entity_type: ReviewRequest
    property: status
    new_value: approved
    condition:
      type: actor
      allowed_actor_ids: [authorized-reviewer]
      distinct_from_creation_actor: true   # optional: approver must not be the entity's creator
    message: "ReviewRequest approvals require an authorized actor distinct from the review's creator."

  - name: work_item_closed_requires_co_written_review
    entity_type: WorkItem
    property: status
    new_value: closed
    condition:
      type: co_write
      requires:
        entity_type: ReviewRequest
        via_relationship: review_request_for_work_item
        kind: approval        # optional: filter the co-written entity's `kind` property
    message: "Closing requires a review created in the same write."

  - name: finding_support_requires_source_evidence
    relationship_type: finding_supports_work_item
    condition:
      type: evidence
      require_evidence: source_evidence
      min_count: 1
    message: "Observation claims require source evidence."

  - name: review_request_change_head_frozen_after_approval
    entity_type: ReviewRequest
    property: change_head
    condition:
      type: frozen
      while: {status: approved}   # omit `while` for immutable-after-create
    message: "An approved review pins the exact SHA that was reviewed."
```

### MutationGuardSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | **yes** | — | Unique guard name |
| `entity_type` | string | entity guards | — | Entity type the write applies to |
| `property` | string | entity guards | — | Property that must be present in the incoming write |
| `new_value` | any or list | entity guards except `frozen` | — | Guarded resulting value(s) after config property normalization. A scalar guards one value; a list guards several (the guard fires when the write results in any listed value). Forbidden on `frozen` guards, which fire on any change |
| `relationship_type` | string | relationship evidence guards | — | Relationship type the write applies to |
| `condition` | discriminated union on `type` | **yes** | — | Condition that must pass (see types below) |
| `message` | string | no | `null` | Optional user-facing rejection detail |
| `where` | predicate map | no | `null` | Entity-property guards only (not `frozen`): scopes the trigger so the guard fires only when the mutated entity matches (candidate scope) |
| `where_related` | list | no | `[]` | Entity-property guards only: related-edge predicates; the guard fires only when every listed edge exists (and matches its inner predicates) on the mutated entity |
| `where_not_related` | list | no | `[]` | Entity-property guards only: related-edge predicates; the guard fires only when no listed edge exists on the mutated entity |

The optional `where` predicate scopes *when* an entity-property guard fires. It
uses the same predicate vocabulary as query `where` (`eq`/`in`/`not_in`/`lt`/`gt`/…)
but is restricted to the `candidate` scope — both predicate paths and any
`$`-reference operands must start with `candidate.` and read only the mutated
(proposed) entity. The guard fires only when the proposed entity matches;
otherwise the write is allowed regardless of the condition. `where` is rejected
on relationship evidence guards.

Guards may also carry `where_related` / `where_not_related` (related-edge trigger
scoping, the same `RelatedPredicateSpec` shape as query traversal steps —
`relationship`/`direction` plus per-scope `edge`/`source`/`target`/`current`/
`candidate`/`entry` predicates). They are anchored on the mutated entity and
evaluated against the proposed graph at `live` visibility (the canonical visible
state; there is no per-guard visibility knob). The guard fires only when every
`where_related` edge exists (and matches its inner predicates) and no
`where_not_related` edge exists. Like `where`, they are entity-property guards
only and are rejected on relationship evidence guards.

The `condition.type` discriminator selects the condition variant:

| `type` | Condition | Applies to |
|--------|-----------|------------|
| `query` | NamedQueryResultCountGuardCondition | entity guards |
| `actor` | ActorIdentityGuardCondition | entity guards |
| `co_write` | CoWriteGuardCondition | entity guards |
| `evidence` | EvidenceRequirementGuardCondition | relationship guards |
| `frozen` | FrozenPropertyGuardCondition | entity guards |

### NamedQueryResultCountGuardCondition (`type: query`)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | `query` | **yes** | — | Condition discriminator |
| `query_name` | string | **yes** | — | Named query to execute against the proposed graph state |
| `params` | dict | no | `{}` | Query params; values may reference write context |
| `min_count` | int | conditional | `null` | Minimum result count; at least one of `min_count` or `max_count` is required |
| `max_count` | int | conditional | `null` | Maximum result count; at least one of `min_count` or `max_count` is required |

Supported param references:

- `$entity.entity_type`
- `$entity.entity_id`
- `$entity.properties.<name>`
- `$current.properties.<name>`
- `$new_value`
- `$old_value`

On entity creation there is no prior state: `$old_value` resolves to `null`,
and `$current.properties.<name>` cannot resolve, so a guard using `$current`
refs fails closed on creation. Prior-state refs therefore make a guard
transition-only in practice.

### ActorIdentityGuardCondition (`type: actor`)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | `actor` | **yes** | — | Condition discriminator |
| `allowed_actor_ids` | list[string] | **yes** | — | Actor ids allowed to perform the guarded transition |
| `distinct_from_creation_actor` | bool (strict) | no | `false` | When `true`, the acting actor must additionally differ from the actor recorded in the target entity's committed creation receipt |

Actor identity conditions compare the current write's
`GovernedActorContext.actor_id` to `allowed_actor_ids`. Missing actor context
fails the guard. This condition is useful for guarded approval transitions where
the authority comes from authenticated runtime credential identity or a Cloud
control-plane supplied actor context.

`distinct_from_creation_actor: true` layers creator/approver separation on top
of the allow-list: the transition is refused when the acting actor is the actor
who **created** the target entity. The comparison anchors on the entity's
committed creation receipt — system-stamped from the authenticated credential —
never on a writable property or the last-writer metadata stamp, so an agent
cannot launder self-approval by rewriting a `requested_by`-style property or by
having another actor touch the entity afterwards. Actors compare by actor id
(the credential label): rotating or re-minting a credential keeps the label, so
creator identity survives rotation, and minting a new label is an admin-tier
operation.

The condition passes only on positive proof of separation. Everything else
fails closed:

- no actor context on the write, or the actor is not in `allowed_actor_ids`
- the guarded value is set in the same write that creates the entity
  (creator == actor trivially, so create-with-approved is always refused)
- the entity has no committed creation receipt (e.g. records materialized from
  a clone/import bundle, which carries no receipts)
- the creation receipt records no actor (pre-auth records)
- the creation-provenance lookup fails
- the creation actor equals the acting actor

The key is strictly boolean (`true`/`false`); string or integer values are
refused, as are unknown fields on the condition.

In compact kit configs the key rides alongside `allowed_actors`:

```yaml
require: {allowed_actors: [authorized-reviewer], distinct_from_creation_actor: true}
```

### CoWriteGuardCondition (`type: co_write`)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | `co_write` | **yes** | — | Condition discriminator |
| `requires` | CoWriteRequirement | **yes** | — | The entity that must be co-created in the same write |

#### CoWriteRequirement

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `entity_type` | string | **yes** | — | Entity type that THIS write must create |
| `via_relationship` | string | **yes** | — | Relationship that must link the created entity to the guarded `$entity` |
| `kind` | string | no | `null` | When set, the co-written entity's `kind` property must equal this value |

Co-write conditions pass only when the **current write delta** both creates an
entity of `requires.entity_type` (optionally `kind`-filtered) AND creates a
`requires.via_relationship` edge linking it to the guarded `$entity`. "Created in
THIS write" means present in the write delta — a stale pre-existing linked entity
or a pre-existing edge does not satisfy the requirement; the entity and its
linking edge must both be new in this write. The required edge may attach the
co-written entity to `$entity` in either direction; the relationship's configured
endpoints determine the valid direction. Because the entity and edge must arrive
together, co-write conditions are satisfiable through the batch direct-write path
(which writes entities and edges in one delta) but not through entity-only writes
or step-by-step workflow apply, where they fail closed.

### EvidenceRequirementGuardCondition (`type: evidence`)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | `evidence` | **yes** | — | Condition discriminator |
| `require_evidence` | `source_evidence` | **yes** | — | Require dereferenceable source-evidence refs resolved from registered source artifacts |
| `min_count` | int | no | `1` | Minimum number of source-evidence refs required; must be at least `1` |

Evidence requirement guards are relationship-scoped: they require
`relationship_type` and must not define `entity_type`, `property`, or
`new_value`. The guard counts resolved `source_evidence` locators, not free-text
`evidence_rationale` alone. Generic `evidence_refs` only satisfy the floor when
they are dereferenceable `source_artifact` refs with chunk identity and content
hash metadata, as produced by source artifact registration.

### FrozenPropertyGuardCondition (`type: frozen`)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | `frozen` | **yes** | — | Condition discriminator |
| `while` | map of property → value | no | `null` | Freeze-state clause evaluated against the entity's **stored, pre-write** state; every entry must match. Omitted = the property is immutable after create |

Every other condition triggers on transitions *to* named values; the frozen
condition protects the guard-level `property` from **any** change. An update
whose post-write value for the property differs from the stored value —
changed, newly set, or unset — is refused while the freeze is active.
Semantics, all evaluated against the stored (pre-write) entity:

- **Creates set the property freely.** Only updates can trip the freeze;
  re-asserting the stored value is not a change.
- **`while` reads before-state only.** The freeze is active when the stored
  entity matches every `while` entry (values compare after property-schema
  normalization; a stored property that is absent matches no named value).
  Because the clause never looks at the incoming write, a single write that
  both moves the entity out of the freeze state and changes the frozen
  property (demote + retarget in one write) is refused by design — leave the
  freeze state in its own write first, then change the property.
- **Fail-closed.** An update whose stored pre-write state is missing or
  unreadable is refused.
- **Entity types only (v1).** `relationship_type` is rejected on the guard,
  and config lint refuses freeze declarations whose `entity_type` names a
  relationship type. Lint also requires the frozen property and every
  `while` property to exist on the entity type, and `while` values to
  normalize against their property schemas.

Frozen guards must not define `new_value` (they fire on any change) and do not
support `where` / `where_related` / `where_not_related` trigger scoping — those
predicates evaluate the *proposed* entity, which would reopen the one-write
demote+retarget hole the before-state clause exists to close. State scoping is
exclusively the condition's `while` clause.

Enforcement runs at the same guard chokepoint as every other entity-property
condition, so all entity write paths are covered: `add_entity`,
`batch_direct_write`, and canonical workflow `apply_entities`. Feedback
`correct` cannot reach entity freezes at all — corrections validate against
relationship (edge) properties only. Release-backed `state pull`
re-materialization stays guard-exempt as described above.

In compact kit configs the freeze form replaces `when`/`require`:

```yaml
mutation_guards:
  - review_request_change_head_frozen_after_approval:
      freeze: ReviewRequest.change_head
      while: {status: approved}
      message: "An approved review pins the exact SHA that was reviewed."
  - state_note_kind_immutable:
      freeze: StateNote.kind
      message: "A StateNote's kind is fixed at creation."
```

For batch direct writes, guards evaluate against the proposed batch graph, so
valid same-batch entities and relationships can satisfy the named query before
anything is committed.

Dry-run (and real) `added`/`updated` counts cover **explicit writes only** —
when derived relationships ship, derived-edge effects will be reported in a
separate additive field, never folded into the write counts. Guard conditions
already see query-time derived edges in dry-runs by construction, since guards
evaluate the named-query engine against the proposed graph.

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

Pinned external artifacts referenced by providers. Artifacts represent data bundles, models, or other resources that providers depend on. The `digest` hash enables reproducible builds — the workflow lock verifies the live artifact matches the hash at lock time.

```yaml
artifacts:
  public_kev_bundle:
    kind: directory
    uri: ./data
    digest: sha256:f884e5f8fad66c6bba54face97863137833ab26035d7a4cda333063d0ab224f9
```

### ProviderArtifactSchema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `kind` | string | **yes** | — | Artifact kind (e.g., `directory`, `file`, `model`) |
| `uri` | string | **yes** | — | Location (relative path, URL, etc.) |
| `digest` | string | no | `null` | Content hash for reproducibility verification (`sha256:`-prefixed) |
| `metadata` | dict | no | `{}` | Arbitrary metadata |

---

## providers

Versioned executable leaves used by workflow steps. A provider is a callable that takes a typed input, produces a typed output, and generates an execution trace for the receipt chain.

```yaml
providers:
  parse_public_kev_bundle:
    kind: function
    description: >
      Parse the pinned public KEV artifact into generic tables.
    contract_in: cruxible.JsonObject
    contract_out: cruxible.ParsedTabularBundle
    ref: cruxible_core.providers.common.tabular.load_tabular_artifact_bundle
    version: "1.0.0"
    deterministic: true
    runtime: python
    artifact: public_kev_bundle

  normalize_public_kev_reference:
    kind: function
    description: >
      Normalize explicit KEV, EPSS, and NVD rows selected by workflow config.
    contract_in: PublicKevReferenceInput
    contract_out: cruxible.JsonItems
    ref: providers.normalize_public_kev_reference
    version: "1.0.0"
    deterministic: true
    runtime: python
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
| `runtime` | string | no | `"python"` | Execution runtime: `"python"`, `"http_json"`, or `"command"` |
| `side_effects` | bool | no | `false` | Whether the provider has side effects |
| `config` | dict | no | `{}` | Provider-specific configuration |

Only providers whose job is to load or parse a source artifact should declare
`artifact`. Domain transform providers should receive the least information
they need through explicit workflow input fields. Required table selection and
source-table mapping belong in workflow config:

```yaml
- id: raw_tables
  provider: parse_public_kev_bundle
  input:
    expected_tables:
      - known_exploited_vulnerabilities
  as: raw_tables

- id: rows
  provider: normalize_public_kev_reference
  input:
    kev_rows: $steps.raw_tables.tables.known_exploited_vulnerabilities.rows
  as: rows
```

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
      - id: raw_tables
        provider: parse_public_kev_bundle
        input:
          expected_tables:
            - known_exploited_vulnerabilities
        as: raw_tables

      - id: rows
        provider: normalize_public_kev_reference
        input:
          kev_rows: $steps.raw_tables.tables.known_exploited_vulnerabilities.rows
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
| `contract_out` | string or inline ContractSchema | no | `null` | Optional final output contract reference. Validates only the payload selected by `returns` after all workflow steps have run. |
| `steps` | list[WorkflowStepSchema] | **yes** | — | Ordered list of steps |
| `returns` | string | **yes** | — | ID of the step whose output is the workflow result |

`contract_out` is an agent-facing boundary check over the assembled workflow
output. It does not validate every provider, query, transform, or intermediate
step output; those steps keep their own validation rules. Omit `contract_out` to
preserve the current unvalidated final-output behavior.

### Workflow Step Types

Each step must define exactly one of these operations:

| Step type | Purpose | Key fields |
|-----------|---------|------------|
| `provider` | Call a registered provider | `provider`, `input`, `as` |
| `query` | Run a named query or inline query definition | `query`, `params?`, `relationship_state?`, `as` |
| `assert` | Guard condition — fail the workflow if not met | `assert: {left, op, right, message}` |
| `assert_not_truncated` | Guard that read/query context was not truncated | `assert_not_truncated: {step}` |
| `assert_count` | Guard a read/result collection count | `assert_count: {step, count, op, value}` |
| `assert_exists` | Guard that one intermediate reference resolves to a present value | `assert_exists: {ref, message?}` |
| `shape_items` | Project, rename, require, and cast list-shaped rows | `shape_items: {items, include_input?, rename?, fields?, casts?, required?}`, `as` |
| `join_items` | Indexed inner join over two item sets | `join_items: {left_items, right_items, left_key, right_key, fields}`, `as` |
| `filter_items` | Filter rows with exact filters and comparisons | `filter_items: {items, where?, comparisons?}`, `as` |
| `aggregate_items` | Deterministically summarize rows with grouped measures | `aggregate_items: {items, group_by?, measures}`, `as` |
| `dedupe_items` | Deterministically deduplicate rows | `dedupe_items: {items, keys, strategy?, rank?}`, `as` |
| `make_entities` | Build an entity set from list data | `make_entities: {entity_type, items, entity_id, properties}`, `as` |
| `make_relationships` | Build a relationship set from list data | `make_relationships: {relationship_type, items, from_type, from_id, to_type, to_id, properties, evidence?}`, `as` |
| `apply_entities` | Apply a built entity set to graph state | `apply_entities: {entities_from}`, `as` |
| `apply_relationships` | Apply a built relationship set to graph state | `apply_relationships: {relationships_from}`, `as` |
| `apply_all` | Apply explicit entity sets, then relationship sets | `apply_all: {entities_from, relationships_from}`, `as` |
| `register_source_artifacts` | Register source artifacts from workflow row data (canonical only; content is workflow data, never files or URLs; identical-content re-runs noop, conflicting content errors) | `register_source_artifacts: {items, artifact_id, content, kind, label?, original_uri?, retention?}`, `as` |
| `make_candidates` | Build relationship candidates for governed proposals | `make_candidates: {relationship_type, items, from_type, from_id, to_type, to_id, properties, evidence?}`, `as` |
| `map_signals` | Convert provider output to tri-state signal-source evidence | `map_signals: {signal_source, items, from_id, to_id, evidence?, evidence_refs?, score/enum}`, `as` |
| `propose_relationship_group` | Assemble a governed group proposal from candidates + signals | `propose_relationship_group: {relationship_type, candidates_from, signals_from, on_empty?}`, `as` |

### Step Reference Syntax

Steps reference data from prior steps and the current item in list iterations:

| Reference | Meaning |
|-----------|---------|
| `$input` | Workflow input payload |
| `$steps.<step_id>` | Output of a prior step (by its `as` alias) |
| `$steps.<step_id>.<field>` | A specific field from a prior step's output |
| `$item` | Current item when iterating over a list (used inside `make_*` and `map_signals`) |
| `$item.<field>` | A specific field on the current item |

Use `evidence` on `make_candidates`/`make_relationships` and
`evidence_refs` on `map_signals` for provenance pointers that should follow a
proposal or deterministic relationship into `relationship.metadata.evidence`.
Keep relationship `properties` for domain facts such as basis, status, version,
or scope fields.

**Read-step outputs:**
- `query` returns `{results: [...], total_results, returned_results, ...}` using the same result rows and metadata as the query engine.
- `shape_items` returns `{items, input_count, output_count, dropped_count, drop_examples}`.
- `join_items` returns `{items, left_count, right_count, skipped_right_count, matched_left_count, output_count}`.
- `filter_items` returns `{items, input_count, output_count, filtered_count}`.
- `aggregate_items` returns `{items, input_count, group_count, output_count}`.
- `dedupe_items` returns `{items, input_count, output_count, duplicate_count, duplicate_examples}`.

Read steps also expose consistent completeness metadata: `total_results`,
`returned_results`, `limit`, `truncated`, `limit_truncated`, `path_truncated`,
and `truncation_reasons`. Query steps additionally expose `result_shape`,
`dedupe`, `relationship_state`, `policy_summary`, and the child query
`receipt_id`. Transform steps that consume read output preserve that metadata in
`source_metadata`.

Workflow graph reads are query-engine-backed. Reusable product/API surfaces
should normally be named queries. Workflow-local collection reads can use an
inline `mode: collection` query definition:

```yaml
- id: production_assets
  query:
    mode: collection
    result_shape: entity
    returns: Asset
    where:
      result.properties.environment:
        eq: production
    order_by:
      - by: $result.entity_id
        direction: asc
  as: production_assets

- id: accepted_asset_products
  query:
    mode: collection
    result_shape: relationship
    returns: asset_runs_product
    relationship_state: accepted
  as: asset_products
```

Collection queries omit `entry_point` and do not define
`traversal`. `result_shape: entity` enumerates entities of `returns`;
`result_shape: relationship` enumerates relationships of `returns` using the
query engine's relationship-state semantics. `result_shape: path` is invalid
for collection queries. Downstream steps consume query rows through
`$steps.<alias>.results`, not `items`.

Older workflow-specific `list_entities` and `list_relationships` read steps
are not supported. Moving collection reads into `query` keeps filtering,
ordering, relationship visibility, receipts, truncation metadata, and query
evidence in one engine instead of duplicating graph-read semantics in
workflow code.

### Guarding Partial Read Context

Agent-facing workflows should fail explicitly when a limited or path-budgeted
read would make the output incomplete. Use `assert_not_truncated` and
`assert_count` for common completeness checks:

```yaml
workflows:
  exposure_context:
    contract_in: ExposureInput
    contract_out: ExposureContext
    steps:
      - id: exposed_assets
        query: exposed_assets_for_vulnerability
        params:
          vulnerability_id: $input.vulnerability_id
        as: exposed_assets

      - id: require_complete_exposure_context
        assert_not_truncated:
          step: exposed_assets

      - id: require_some_exposures
        assert_count:
          step: exposed_assets
          count: returned_results
          op: gt
          value: 0

    returns: exposed_assets
```

The same pattern works after shaping or filtering because transforms preserve
read metadata:

```yaml
- id: shaped_exposures
  shape_items:
    items: $steps.exposed_assets.results
    fields:
      asset_id: $item.values.asset_id
      priority: $item.values.priority
  as: shaped_exposures

- id: require_complete_shaped_context
  assert_not_truncated:
    step: shaped_exposures
```

Use `assert_exists` for required intermediate context refs where a missing nested
path should produce an author-controlled message instead of a low-level
reference-resolution error:

```yaml
- id: require_first_asset_id
  assert_exists:
    ref: $steps.exposures.results[0].values.asset_id
    message: first exposure must include an asset id
```

`assert_count.count` supports `returned_results`, `total_results`, `items`, and
`results`. `assert_exists` treats `null` and empty strings as missing; `false`,
`0`, empty lists, and empty objects are present values. General `assert` remains
available for arbitrary comparisons and is equivalent to the longer explicit
forms of these common checks.

`contract_out` validates the final output shape selected by `returns`. Read
metadata guards validate whether the workflow had complete enough source
context to support that output.

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

`aggregate_items` groups already-materialized rows and computes deterministic
summary rows. Omit `group_by` for one global aggregate row; global aggregates
return one row even when the input is empty, so downstream steps can rely on a
stable summary object. Supported measures are `count`, `count_where`,
`count_distinct`, `sum`, `min`, and `max`. `count_distinct` ignores `null`
values and uses canonical JSON identity for structured values. `sum`, `min`,
and `max` can declare `value_type` (`number`, `date`, `datetime`, etc.) to use
the shared typed comparison/coercion rules. Aggregation preserves source
truncation metadata when it summarizes read/query-derived rows.

Grouped count example:

```yaml
- id: exposure_counts
  aggregate_items:
    items: $steps.exposures.results
    group_by:
      priority: $item.values.priority
    measures:
      exposure_count:
        count: true
      affected_assets:
        count_distinct:
          value: $item.values.asset_id
      critical_count:
        count_where:
          left: $item.values.priority
          op: eq
          right: critical
  as: exposure_counts
```

Global count example:

```yaml
- id: exposure_total
  aggregate_items:
    items: $steps.exposures.results
    measures:
      exposure_count:
        count: true
  as: exposure_total
```

`dedupe_items` requires one or more keys and supports `first`, `last`, `max`,
and `min`. Ranked strategies require `rank`; missing ranks lose to present
ranks, and ties keep the earlier item.

### apply_all

`apply_all` is a canonical workflow step for reducing repetitive apply
boilerplate while keeping writes explicit. It applies entity sets first in the
listed order, then relationship sets in the listed order, reusing the same
validation and write semantics as `apply_entities` and `apply_relationships`.
It does not infer "all previous steps"; every source alias must be listed.

```yaml
- id: apply_local_state
  apply_all:
    entities_from:
      - assets
      - owners
      - controls
    relationships_from:
      - owned_by_edges
      - control_edges
  as: apply_local_state
```

The output contains `entity_results`, `relationship_results`, top-level
`create_count`, `update_count`, `noop_count`, and duplicate-input totals.

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

Workflow proposal signatures are Cruxible-generated. Config authors provide
`thesis_text` for human explanation and `analysis_state` for review/debug
context, but workflow `propose_relationship_group` steps do not author
`thesis_facts`. Cruxible builds the stored signature facts from executable
structure: workflow name, proposal step id, relationship shape, candidate
alias, actual consumed signal batches, the relationship proposal policy, and a
Cruxible-controlled proposal logic digest. `thesis_text`, `analysis_state`, and
`suggested_priority` are not hashed.

Direct agent-authored group proposals may provide optional caller
`thesis_facts` as signature scope. Cruxible stores that scope under
`agent_scope` in generated `thesis_facts`; generated top-level fields such as
`origin`, `relationship`, and `signals` remain Cruxible-owned. The origin is
marked `agent` with `evidence_mode: agent_supplied`, and signal-source facts
come from member signals supplied on the proposal. Agent-supplied facts cannot
impersonate workflow/provider-backed evidence; use a configured workflow when
evidence must be provider-backed.

`propose_relationship_group` is strict by default: if `candidates_from` resolves
to an empty candidate set, the workflow fails. Set `on_empty: complete` only when
"no candidates" is a valid terminal outcome for that workflow. In that case no
candidate group is created, the workflow succeeds with `status: no_candidates`,
and the workflow receipt records `group_created: false`.

```yaml
- id: proposal
  propose_relationship_group:
    relationship_type: asset_remediated_vulnerability
    candidates_from: candidates
    signals_from: [remediation_signals]
    on_empty: complete
    thesis_text: Close stale exposure edges
  as: proposal
```

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
      receipt_contains_provider: parse_public_kev_bundle
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
extends: ../kev-reference/config.yaml
description: >
  Overlay of the KEV reference state for internal vulnerability triage.

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
      affected_basis: {}
    proposal_policy:
      signals:
        product_version_evidence:
          role: required
          always_review_on_unsure: true
        scanner_evidence:
          role: advisory

named_queries:
  affected_assets_for_vulnerability:
    mode: traversal
    description: Find internal assets accepted as affected by a vulnerability.
    entry_point: Vulnerability
    returns: Asset
    traversal:
      - relationship: asset_affected_by_vulnerability
        direction: incoming

  owner_patch_queue:
    mode: traversal
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
