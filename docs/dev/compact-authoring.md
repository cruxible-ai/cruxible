# Compact Authoring Grammar

Compact configs are YAML that expand deterministically to the explicit
`CoreConfig` schema before validation. Unsupported compact keys fail closed with
`CompactExpansionError`; they are never ignored.

The compact reference kit is `kits/agent-operation/config.yaml`.

## Explicit Engine-Schema Query Bodies

Compact `named_queries:` entries normally use the compact query grammar. If a
query must embed an explicit engine-schema body that compact cannot express, add
`explicit: true` inside that query body. The expander strips the marker and
passes through the remaining mapping verbatim.

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

Unsupported keys without `explicit: true` always error. Do not add the marker to
a compact query body: compact-only keys such as `traverse`, `traverse_all`,
`bound`, `order`, `as`, and `max_depth` are rejected in explicit bodies.

## Traversal Steps

Single-relationship traversal steps may keep relying on direction inference from
the query `entry_point`:

```yaml
traverse:
  - relationship: work_item_owned_by_actor
    as: work_item
```

When a step fans out across more than one relationship, author the explicit
relationship list and `direction` that the engine should use at each hop. The
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

Use `required:` on a traversal step when the explicit query should preserve an
optional path branch.

```yaml
traverse:
  - relationship: roadmap_item_depends_on_roadmap_item
    direction: outgoing
    as: upstream_dependency
    required: false
```

## Named Includes

Named includes default to `from: $result`, matching the existing compact
behavior for explicit traversal and collection queries. A traversal query may
pin a named include to the entry node with `from: $entry`, or state the default
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

Only `$entry` and `$result` are compact include shorthands. Other explicit
schema references should remain in explicit config until compact support is
added for them. Add `direction: incoming|outgoing` when the include anchor
cannot be inferred, such as `from: $result` on an `AnyEntity` query, or when an
overlay references a relationship supplied by its extended base config.

Use the existing self-reference direction markers when inference from the include
anchor would be ambiguous:

```yaml
include:
  downstream_dependents:
    from: $entry
    relationship: roadmap_item_depends_on_roadmap_item<
```

Use `required:` when the explicit include should require at least one matching
edge.

```yaml
include:
  gating_milestones:
    relationship: work_item_in_milestone
    required: true
```

Include and traversal `where:` blocks may use compact field shorthand or an
already scoped explicit predicate path. Shorthand is scoped by the construct;
explicit paths are preserved unchanged.

```yaml
where:
  target.properties.status: {in: [active, planned, blocked]}
```

## Quality Checks

Compact quality checks pass through `severity: warning|error` exactly.

```yaml
quality_checks:
  - work_items_have_owner:
      cardinality:
        entity: WorkItem
        relationship: work_item_owned_by_actor
        direction: out
        min: 1
      severity: error
```

Quality-check mappings with both `name` and `kind` are structural explicit
engine-schema forms and pass through unchanged. Any quality-check mapping without
both keys follows the single-key compact form above.

```yaml
quality_checks:
  - name: minimum_assets_loaded
    kind: bounds
    target: Asset
    min: 1
```
