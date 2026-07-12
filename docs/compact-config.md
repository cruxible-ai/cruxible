# Compact Config Authoring

Compact is the recommended way to author the graph-shape parts of Cruxible
config. It is YAML that expands deterministically to the explicit `CoreConfig`
schema before validation — entity types, relationships, named queries,
mutation guards, and quality checks collapse to short string grammars instead
of the fully-spelled-out engine shape. Unsupported compact keys **fail closed**
with `CompactExpansionError`; they are never silently ignored or passed through.

Compact only reduces those graph-shape keys. The other top-level keys —
`gates`, `contracts`, `providers`, `workflows`, `feedback_profiles`,
`outcome_profiles`, `decision_policies`, `artifacts`, `runtime`, `tests` — have
no compact form; a compact file carries them in their explicit shape verbatim,
so a real config mixes compact-reduced sections with explicit pass-through
ones. Author those keys from [Config Reference](config-reference.md).

The canonical worked compact config is `kits/agent-operation/config.yaml`.
Every example on this page is a real excerpt from it.

> **This page is the how-to for the keys compact reduces.** For the pass-through
> keys above, and for the precise expanded shape of any field — every default,
> every validation rule, the full quality-check kind catalogue — see
> [Config Reference](config-reference.md), the complete schema compact expands
> into. Reach for it to author a pass-through key, when you need to know exactly
> what a key defaults to, or when you're reading a validation error (errors
> speak the expanded schema — see [Coherence with the engine](#coherence-with-the-engine) below).

Expand a compact file yourself to see the shape it produces:

```bash
cruxible config expand --in kits/agent-operation/config.yaml
```

## enums

```yaml
enums:
  actor_kind: [human, agent, service_account, system]
  priority: {values: [low, medium, high, critical], ordered: low_to_high}
```

A bare list is shorthand for `{values: [...]}`. Add `ordered: low_to_high`
to make an enum referenceable from a query `order_by` clause (see
[named_queries](#named_queries) below). Both forms pass straight through to
`EnumSchema` — see [enums](config-reference.md#enums) in the config
reference for `description` and validation rules.

## entity_types

Each entity's `properties:` block uses a compact scalar grammar instead of
the explicit `PropertySchema` mapping:

```
string / int / float / number / bool / json / date / datetime   -> {type: <t>}
<type>?                                                          -> optional: true
indexed                                                           -> indexed: true
enum <ref>                                                        -> {type: string, enum_ref: <ref>}
= <value>                                                         -> default: <value>
```

Tokens combine in one string, e.g. `string indexed`, `enum actor_status =
active`, `date?`. An entity-level `id: <name>` shorthand expands to a
`<name>: {type: string, primary_key: true}` property, emitted first — you
never hand-write `primary_key: true` for the id field.

```yaml
entity_types:
  WorkItem:
    description: Execution-level item an agent or human can work, review, close, defer, or supersede.
    id: work_item_id
    properties:
      title: string indexed
      summary: string?
      type: enum work_item_type
      status: enum lifecycle_status
      priority: enum priority
      target_date: date?
```

`id: work_item_id` plus `title: string indexed` expand to:

```yaml
entity_types:
  WorkItem:
    properties:
      work_item_id: {type: string, primary_key: true}
      title: {type: string, indexed: true}
```

A property spec may also be an explicit mapping (`{type: string, optional:
true, description: "..."}`) instead of a compact string — useful for a
`description` or any field the scalar grammar doesn't express, such as
`json_schema`. It passes through unchanged:

```yaml
  WorkItem:
    properties:
      description: {type: string, optional: true, description: "Markdown (GFM) renders in UIs — headings, lists, tables; single newlines are hard breaks."}
```

### write_policy and write_tier (entity)

`write_policy` and `write_tier` are identical strings in compact and
explicit — they pass straight through onto the entity. See
[Direct-Write Governance](config-reference.md#direct-write-governance-refuse_direct_writes)
and [Config-Declared Write Tiers](config-reference.md#config-declared-write-tiers-write_tier)
for full semantics; the two real examples below cover both.

`write_policy: mint_only` — an auth-managed identity type writable only by
the internal token-mint source:

```yaml
entity_types:
  Actor:
    description: >
      Human, agent, service account, or system actor referenced by operation
      state. Auth-managed: instances materialize from runtime-credential mints.
    id: actor_id
    auth_managed: true
    write_policy: mint_only
    properties:
      label: string indexed
      kind: enum actor_kind
      status: enum actor_status = active
```

`write_tier: governed_write` — lowers the direct-write requirement below the
default `graph_write`, for a kit's designated low-trust write surface:

```yaml
entity_types:
  StateNote:
    description: >
      Durable dated note about operation state (corrections, field notes,
      rationale updates, implementation notes, review notes) plus low-trust
      scratchpad notes.
    id: note_id
    write_tier: governed_write
    properties:
      kind: enum state_note_kind
      title: string indexed
      summary: string
      body: {type: string, description: "Markdown (GFM) renders in UIs."}
      created_at: datetime
```

## relationships

A relationship is a single-key list item: `name: "From -> To"`, with a
trailing `# comment` as the one-line `description` (recovered from the YAML
comment — `yaml.safe_load` normally discards comments, so the expander
does a light line-level pre-scan just for this).

```yaml
relationships:
  - work_item_owned_by_actor: WorkItem -> Actor                # Actor accountable for a work item.
  - review_request_for_work_item: ReviewRequest -> WorkItem    # Reviews a work item for completion/acceptance.
```

expands to:

```yaml
relationships:
  - name: work_item_owned_by_actor
    from: WorkItem
    to: Actor
    description: Actor accountable for a work item.
```

A block `description:` key overrides the trailing-comment form when both
are present. `basis: <prop>` is shorthand for a `string?` rationale
property named `<prop>` — used on governed judgment edges to hold the
"why":

```yaml
  - risk_blocks_work_item: Risk -> WorkItem                    # Governed: a risk blocks/materially delays a work item.
    proposal_policy: standard
    write_policy: proposal_only
    basis: blocking_basis
```

expands `basis: blocking_basis` to `properties: {blocking_basis: {type:
string, optional: true}}`. An explicit `properties:` block (compact scalar
or explicit mapping values, same grammar as entity properties) may sit
alongside `basis` for edges that need more than one extra field:

```yaml
  - decision_constrains_work_item: Decision -> WorkItem        # Governed: a decision constrains how work proceeds.
    proposal_policy: standard
    write_policy: proposal_only
    basis: constraint_basis
    properties: {impact_type: enum decision_impact_type}
```

### proposal_policy: preset or inline

`proposal_policy` on a relationship is either a **preset name** resolved
against top-level `presets.policies`, or an inline mapping — both expand to
the same explicit `ProposalPolicyConfig` shape
([proposal_policy](config-reference.md#proposal_policy) in the config
reference documents the resulting fields).

```yaml
presets:
  policies:
    standard:
      signals:
        source_evidence:     {role: required,  always_review_on_unsure: true}
        maintainer_judgment: {role: advisory,  always_review_on_unsure: true}

relationships:
  - work_item_depends_on_work_item: WorkItem -> WorkItem       # Sequencing: from depends on to landing/deciding first.
    proposal_policy: standard
    write_policy: proposal_only
    basis: dependency_basis
```

`presets:` is authoring-only — the expander consumes it to resolve preset
references and strips it; it never appears in the expanded config or the
engine schema.

### write_policy and write_tier (relationship)

Same pass-through as entity types. `write_policy: proposal_only` forces a
relationship through the governed proposal/workflow path (shown above).
`write_tier: governed_write` lowers a relationship's direct-write
requirement — commonly paired with a `write_tier: governed_write` entity so
a governed-write actor can write the entity **and** attach it in the same
payload:

```yaml
  - state_note_about_work_item: StateNote -> WorkItem
    write_tier: governed_write
  - state_note_authored_by_actor: StateNote -> Actor           # Actor that authored/recorded the note.
    write_tier: governed_write
```

## named_queries

Every query still declares `mode` (`traversal` or `collection`) and
`returns` explicitly — these are **decision-bearing knobs** the expander
never defaults. `relationship_state` is decision-bearing too when present.
Everything else compact adds (`result_shape`, `max_paths`,
`max_paths_per_result`, `limit`) is an inert resource guard the expander
fills in with the engine's own defaults when you omit it.

### Collection query

```yaml
named_queries:
  work_queue:
    mode: collection
    description: Active work items dispatched for implementation.
    returns: WorkItem
    result_shape: entity
    where:
      result.properties.status:
        in: [active]
    select:
      work_item_id: $result.entity_id
      title: $result.properties.title
      priority: $result.properties.priority
    limit: 100
```

`where:` at collection scope may use bare-field shorthand — `{status: {in:
[active]}}` expands to `{result.properties.status: {in: [active]}}` — or an
already-scoped explicit path (`result.properties.status`, `source.`,
`target.`, `edge.`), which passes through unchanged. Both forms may mix in
the same block.

### Traversal query: traverse, where shorthand, include, select, order

```yaml
named_queries:
  actor_work_queue:
    mode: traversal
    entry_point: Actor
    returns: WorkItem
    relationship_state: reviewable
    result_shape: path
    max_paths_per_result: 100
    description: Work items owned by an actor with latest reviews, dependency counts, blockers, subjects.
    traverse:
      - relationship: work_item_owned_by_actor
        as: work_item
        where: {status: {not_in: [closed]}}
    include:
      latest_review:
        relationship: review_request_for_work_item
        limit: 1
        order: [requested_at desc datetime, resolved_at desc datetime]
    select:
      properties: [work_item_id, title, status, priority, type]
      counts:
        upstream_dependency: work_item_depends_on_work_item>
        downstream_dependent: work_item_depends_on_work_item<
        blocking_risk: risk_blocks_work_item
      latest_review_request_id: $include.latest_review.items.0.source.entity_id
      latest_review_status: $include.latest_review.items.0.source.properties.status
    order: priority desc ^priority
```

A single `traverse:` step needs no `direction:` — it's **inferred** from
which endpoint of the relationship the query's `entry_point` (or the
previous hop's landing entity, for chained steps) sits on. A step's `where:`
targets the traversed candidate (`candidate.properties.<field>` scope).

`include:` defines named bounded side-context sets — one hop off the
traversal result (or `from: $entry` to anchor on the entry node instead),
attached under each row's `includes` map without fanning out primary rows.

`select:` has three compact sub-blocks:

- `properties: [...]` — bare property names, expanded to `$result.properties.<name>`
  (or `$result.entity_id` for the declared primary key).
- `counts: {<alias>: <rel>}` — `<alias>_count: $include.<alias>.count`. Referencing
  a relationship here that isn't already an `include:` set auto-creates one.
- `items: {<alias>: <rel>}` — `<alias>: $include.<alias>.items`, same auto-include behavior.

A self-referential relationship (`WorkItem -> WorkItem`) is ambiguous
without a direction, so compact overloads a trailing marker on the
relationship reference: `work_item_depends_on_work_item>` (outgoing) /
`work_item_depends_on_work_item<` (incoming). Any other key not matching
`properties`/`counts`/`items` in `select:` passes through verbatim as a deep
projection ref, as `latest_review_request_id` does above.

`order:` is one string or a list of strings in the grammar `<field>
<asc|desc> [<type>|^<enum>]` — a bare type token sets `value_type`; `^<enum>`
sorts by a declared `ordered: low_to_high` enum's rank instead of lexical
order.

### include: all_adjacent + bound

`include: all_adjacent` includes every relationship touching the anchor
entity, depth 1, both directions where relevant — the "full context dump"
shape. `bound:` caps or filters one of those auto-generated sets by
relationship name (with a `>`/`<` marker for a self-ref set):

```yaml
named_queries:
  work_item_context:
    mode: traversal
    entry_point: WorkItem
    returns: AnyEntity
    relationship_state: reviewable
    description: >-
      From a work item, inspect dependencies, blockers, reviews, composition,
      lineage, decisions, owner, subjects.
    include: all_adjacent
    bound:
      state_note_about_work_item:
        limit: 10
        order: created_at desc datetime
        where: {kind: {not_in: [scratchpad]}}
```

`all_adjacent` resolves against the **final composed config** — on an
overlay instance it also picks up base-config relationships, so this one
query stays correct as overlays add their own edges.

### traverse_all: fan-out across an explicit relationship list

`traverse_all: [rel, rel, ...]` plus `direction:` builds one fan-out
traversal step across a fixed relationship list, useful when the individual
relationships don't share an inferable direction:

```yaml
named_queries:
  work_item_lineage_context:
    mode: traversal
    entry_point: WorkItem
    returns: WorkItem
    relationship_state: reviewable
    max_paths_per_result: 100
    description: Work item lineage/replacement context, excluding sequencing deps.
    traverse_all: [work_item_spawned_from_work_item, work_item_supersedes_work_item]
    direction: both
    max_depth: 5
```

### Query templates: for: [...] + $T

A `for: [TypeA, TypeB]` block turns one templated query body into one
concrete query per type, substituting `$T` in the query name, `returns`,
and `description`:

```yaml
named_queries:
  superseded_$T:
    for: [Decision, WorkItem]
    mode: collection
    returns: $T
    relationship_state: not-live
    description: >-
      $T retired/superseded on the canonical entity-lifecycle axis (lifecycle.status
      != live), gated out of live reads.
```

This expands to two independent named queries, `superseded_decisions` and
`superseded_work_items` (the query name pattern pluralizes+snake_cases the
type: `Decision` -> `decisions`, `WorkItem` -> `work_items`).

### Explicit engine-schema query bodies

Compact `named_queries:` entries normally use the compact query grammar
above. If a query must embed an explicit engine-schema body that compact
cannot express, add `explicit: true` inside that query body. The expander
strips the marker and passes through the remaining mapping verbatim — this
is the sanctioned escape hatch; see
[Coherence with the engine](#coherence-with-the-engine) for why it exists.

```yaml
named_queries:
  product_vulnerabilities:
    explicit: true
    mode: traversal
    entry_point: Product
    returns: Vulnerability
    traversal:
      - as: affected_by
        relationship: vulnerability_affects_product
        direction: incoming
    order_by:
      - by: $result.properties.kev_due_date
        direction: asc
        value_type: date
```

Unsupported keys without `explicit: true` always error, and the error
message points you at the marker. Do not add the marker to a compact query
body: compact-only keys such as `traverse`, `traverse_all`, `bound`,
`order`, `as`, and `max_depth` are rejected inside an `explicit: true` body
— an explicit body must be genuinely explicit-schema shaped, not a mix.

### Traversal steps: multi-relationship fan-out and required: false

A `traverse:` step written by hand (as opposed to `traverse_all`) may still
rely on direction inference from the query `entry_point`, as shown above.
When a step fans out across more than one relationship, author the explicit
relationship list and `direction` the engine should use at that hop. The
step may still use compact `where:` property shorthand; it expands to
`candidate.properties.<field>`.

```yaml
traverse:
  - relationship:
      - work_item_in_release
      - roadmap_item_in_release
    direction: incoming
    as: release_context
    where:
      status: {in: [active, planned, blocked]}
```

Use `required: false` on a traversal step when the explicit query should
preserve an optional path branch instead of dropping the row:

```yaml
traverse:
  - relationship: roadmap_item_depends_on_roadmap_item
    direction: outgoing
    as: upstream_dependency
    required: false
```

### Named includes: from, direction, required

Named includes default to `from: $result`, matching the compact behavior
for explicit traversal and collection queries. A traversal query may pin a
named include to the entry node with `from: $entry`, or state the default
explicitly with `from: $result`.

```yaml
include:
  release_lines:
    from: $entry
    relationship: roadmap_item_in_release
  roadmap_items:
    from: $result
    relationship: work_item_implements_roadmap_item
```

Only `$entry` and `$result` are compact include anchors. Add `direction:
incoming|outgoing` when the include anchor cannot be inferred, such as
`from: $result` on an `AnyEntity` query, or when an overlay references a
relationship supplied by its extended base config. Use the self-reference
direction markers when inference from the include anchor would be
ambiguous:

```yaml
include:
  downstream_dependents:
    from: $entry
    relationship: roadmap_item_depends_on_roadmap_item<
```

Use `required: true` when the explicit include should require at least one
matching edge:

```yaml
include:
  gating_milestones:
    relationship: work_item_in_milestone
    required: true
```

## mutation_guards

A mutation guard is a single-key list item: `<name>: {when:, require:,
message:}`. `when:` is a compact trigger grammar, `<Entity>.<prop> ->
<value>` (or `-> [value_a, value_b]` for multiple trigger values); `require:`
is one of three compact condition shapes, discriminated by which key it
carries. Every guard below is a real kit guard.

**`co_write`** — a companion entity must be written, linked via a named
relationship, in the same payload:

```yaml
mutation_guards:
  - review_verdict_requires_rationale_note:
      when: ReviewRequest.status -> [changes_requested, approved, withdrawn]
      require:
        co_write: StateNote via state_note_about_review_request
        kind: review_note
      message: >-
        A ReviewRequest verdict must co-write a new StateNote(kind=review_note) linked via
        state_note_about_review_request in the same write. Status can't advance without recording why.
```

**`allowed_actors`** — the authenticated actor must be one of a literal
allow-list (no identity resolution or invented actors — it's a literal
passthrough of `allowed_actor_ids`):

```yaml
  - review_request_approval_requires_authorized_actor:
      when: ReviewRequest.status -> approved
      require: {allowed_actors: [authorized-reviewer], distinct_from_creation_actor: true}
      message: >-
        ReviewRequest approvals require the authenticated reviewer actor, and the
        approver must differ from the actor recorded in the ReviewRequest's
        creation receipt.
```

**`query`** — a named query, run with the mutated entity's id bound in
`params`, must return at least `min_count` (or at most `max_count`) rows:

```yaml
  - work_item_closed_requires_approved_review:
      when: WorkItem.status -> closed
      require:
        query: approved_reviews_for_work_item
        params: {work_item_id: $entity.entity_id}
        min_count: 1
      message: >-
        Work items cannot be closed until an approved ReviewRequest reviews them.
```

Optional `where:` / `where_related:` / `where_not_related:` further scope
which mutations trigger the guard; they use the same predicate shapes as
query traversal steps and pass through unchanged. See
[mutation_guards](config-reference.md#mutation_guards) in the config
reference for the full trigger/condition schema these expand to, and the
overlay-composition and guard-exemption rules.

## quality_checks

Quality checks use a **single-key discriminated grammar**: the one key
inside the check body tells the expander which check kind to build.

`cardinality` — per-entity relationship-count bound:

```yaml
quality_checks:
  - work_items_have_owner:
      cardinality:
        entity: WorkItem
        relationship: work_item_owned_by_actor
        direction: out
        min: 1
      description: Work items should have an accountable actor.
```

`property` — `<relationship>.<field>` (snake_case subject targets a
relationship) or `<EntityType>.<field>` (CapWords subject targets an
entity), with a `rule` of `non_empty` or `required`:

```yaml
  - work_dependencies_have_basis:
      property: work_item_depends_on_work_item.dependency_basis
      rule: non_empty
  - decision_work_constraints_have_type:
      property: decision_constrains_work_item.impact_type
      rule: required
```

`work_items_have_owner`'s cardinality block expands to the explicit
discriminated shape:

```yaml
quality_checks:
  - name: work_items_have_owner
    kind: cardinality
    entity_type: WorkItem
    relationship_type: work_item_owned_by_actor
    direction: outgoing
    min_count: 1
    description: Work items should have an accountable actor.
```

Compact quality checks pass through `severity: warning|error` exactly when
present (default `warning`, matching the explicit schema). The compact
grammar only covers the `cardinality` and `property` check kinds shown
above — the other four kinds (`json_content`, `bounds`, `uniqueness`,
`relationship_property_consistency`; see
[quality_checks](config-reference.md#quality_checks) in the config
reference) have no compact reduction yet and are authored in their explicit
shape. A quality-check mapping with **both** `name` and `kind` is recognized
as that explicit form and passes through unchanged — this is how those
other kinds, and any check you want to write explicitly, sit in an
otherwise-compact `quality_checks:` list:

```yaml
quality_checks:
  - name: minimum_assets_loaded
    kind: bounds
    target: entity_count
    entity_type: Asset
    min_count: 1
```

Any single-key mapping without both `name` and `kind` is parsed as the
`cardinality`/`property` compact form above and errors if it matches
neither.

## gates

`gates:` has **no compact reduction** — it's a straight pass-through
top-level key, authored in exactly the shape
[gates](config-reference.md#gates) in the config reference documents. There
is no compact/explicit distinction to learn here; the block below is
byte-for-byte what a compact source file contains and what the expanded
config carries.

```yaml
gates:
  merge-review:
    description: >
      Every tip merged into main (all merged-in parents of each merge commit)
      must be pinned by an approved ReviewRequest (change_head).
    kind: git-pre-push
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
    adapter: {branch_pattern: refs/heads/main}
```

`feedback_profiles`, `outcome_profiles`, `decision_policies`, `contracts`,
`artifacts`, `providers`, `workflows`, `runtime`, and `tests` are
pass-through the same way — author them in their explicit shape straight
from the config reference; compact only reduces `entity_types`,
`relationships`, `named_queries`, `mutation_guards`, and `quality_checks`.

## Coherence with the engine

The engine validates and executes the **expanded** form — `CoreConfig` — not
the compact source. Compact expansion happens once, at load, entirely in
memory; there is no committed expanded artifact for the engine to see
instead. Two things follow, and both matter when something goes wrong:

- **Validation errors speak explicit.** A `CoreConfig` validation error
  names explicit fields (`entity_type`, `relationship_type`, `min_count`,
  `direction: outgoing`, ...), not the compact spelling you wrote
  (`entity`, `relationship`, `min`, `direction: out`). If a compact-only
  reader hits a schema error, translate the compact construct in question
  to its expanded shape (this page shows the mapping for every construct)
  before chasing the field name in the error.
- **Internals, receipts, and the rest of the docs speak explicit too.**
  Query execution, traversal semantics, receipts, and
  [Config Reference](config-reference.md) itself all describe the expanded
  graph-query engine, because that's the only shape that actually runs.
  Compact is purely an authoring-time convenience layer in front of it —
  it never changes engine behavior, only how much you type to get there.

`cruxible config expand --in <path>` (see
[`cruxible config expand`](cli-reference.md#cruxible-config-expand)) prints
the expanded form for any compact file, which is the fastest way to see
exactly what the engine will validate.

## When compact isn't enough

Compact is deliberately **not** trying to express 100% of the engine
schema — chasing that would mean compact perpetually racing an ever-growing
`CoreConfig`, or the engine schema being held back to what compact can say.
Two escape hatches exist instead, and both are permanent, sanctioned
fallbacks, not a "not implemented yet" workaround to migrate away from:

1. **`explicit: true` inside a `named_queries:` entry** — see
   [Explicit engine-schema query bodies](#explicit-engine-schema-query-bodies)
   above. Compact fail-closes on unknown keys (`_reject_unknown_keys` raises
   `CompactExpansionError` on any key it doesn't recognize), so an
   explicit-schema query body needs the marker to opt out of that
   fail-closed check and pass through verbatim — without it, an explicit
   query's own field names (`traversal`, `select` with raw `$path` refs,
   etc.) would themselves look like unsupported compact keys and be
   rejected.
2. **Pass-through top-level keys** — `gates`, `feedback_profiles`,
   `outcome_profiles`, `decision_policies`, `contracts`, `artifacts`,
   `providers`, `workflows`, `runtime`, `tests`, and an explicit
   `name`+`kind` quality check — all authored directly in their
   [Config Reference](config-reference.md) shape inside an otherwise-compact
   file, no marker needed, because compact never introduced a reduced
   grammar for them.

If you find yourself reaching for `explicit: true` on every query in a
kit, or hand-writing large explicit blocks that feel like they *should*
have a compact shorthand, that's a signal for the compact grammar itself to
grow a new construct — not a reason to abandon compact for the whole file.
