# Guide for AI Agents

This guide explains how AI agents (Claude Code, Cursor, Codex, or any MCP-capable agent) should orchestrate Cruxible Core. You provide the intelligence; Core provides deterministic execution with proof.

For the `0.2` RC, the primary execution shape is a **local `cruxible-server` daemon**. CLI, GUI, MCP, and any local integrations should talk to that daemon over HTTP or a Unix socket.

Direct local runtime remains available as a convenience path, but it is not the primary RC interface and it is not a strong boundary.

Workflow guidance should live in agent-side skills; MCP should remain a deterministic execution adapter, not the source of the workflow itself.

Starter `SKILL.md` files for the old prompt workflows now live in the repo-level [skills](../skills) folder.

## Install Boundary

- Agent/client environment: `pip install cruxible-client`
- Daemon/MCP runtime: `pip install "cruxible-core[server,mcp]"`

Permission modes are enforced at the daemon boundary. If an agent can import `cruxible-core` or access the daemon's runtime/filesystem directly, those modes are advisory rather than isolating.

If trust levels matter, keep the agent on `cruxible-client` only and run `cruxible-core` in a separate daemon environment.

For a concrete hardened setup, see [Isolated Deployment](isolated-deployment.md).

## Role Separation

**You (the AI agent):**
- Understand the user's domain by reading docs and data
- Generate YAML configs defining entity types, relationships, queries, and constraints
- Reason about data to infer relationships (classification, matching, similarity)
- Propose entities and relationships with schema-declared evidence, using governed
  candidate groups when tri-state support history matters
- Interpret query results and present them to humans
- Collect and record feedback

**Core (the runtime):**
- Validates configs against the schema
- Ingests data into the entity graph
- Executes named queries deterministically
- Produces receipts (structured proofs) for every query
- Stores and applies feedback
- Evaluates graph quality and constraint compliance

Scripts are appropriate for data cleaning and transforms, but not for inference tasks like classification or matching — use your judgment and `cruxible_add_relationship` for those.

## Modeling Stance

Use Cruxible to model shared domain facts, company-specific logic, and governed judgments.

Do not use it as a scratchpad for temporary agent notes. Free-form working memory belongs in the agent; operational state that must be queried, reviewed, approved, and replayed belongs in Cruxible.

## Multi-Agent Coherence

If multiple agents are collaborating, assume they will drift unless shared truth is made explicit.

LLM state is prompt-local and opaque. One agent cannot reliably inspect another agent's internal representation of the domain, procedure, or current decisions. That means handoffs based only on summaries or chat history are lossy.

The same warning applies to human-agent collaboration. Do not assume the model's current framing of the problem matches the human's framing just because they have seen similar material.

Use Cruxible as the shared coordination layer:

- persist accepted facts and relationships instead of leaving them in chat context
- persist approved or rejected judgments instead of relying on private agent notes
- use named queries, constraints, and receipts as the shared procedural surface
- treat agent-local notes as temporary working memory, not operational truth

When handing work from one agent to another, update the world model first if the result should affect later reasoning.

## Start Here

The fastest way to get running is with a demo directory. Each demo includes a `.mcp.json` (MCP server config), `config.yaml`, and a prebuilt graph — everything needed for a self-contained workspace.

**Recommended starting point:** `demos/drug-interactions/`

```bash
cd demos/drug-interactions
# The .mcp.json here configures cruxible-mcp automatically
```

The graph is prebuilt. Load it and start querying:

```
1. cruxible_init(root_dir=".")                          # loads existing graph
2. cruxible_query("check_interactions", params={"drug_id": "warfarin"})
3. cruxible_query("enzyme_impact", params={"drug_id": "fluoxetine"})
```

Other named queries available: `find_mechanism`, `suggest_alternative`.

Every demo directory is a self-contained MCP workspace — the `.mcp.json` points at `cruxible-mcp` so agents can discover tools automatically when opened in that directory.

## Lifecycle

The standard lifecycle follows this order:

```
validate → init → lock workflow → run/apply deterministic workflows → query → feedback → outcome → evaluate
```

### Ordering Rules

1. `cruxible_validate` before `cruxible_init` — fail fast on bad config
2. Run `cruxible_lock_workflow` before planning or executing workflows after config/provider changes
3. Apply canonical workflows only after a preview returns `apply_digest` and `head_snapshot_id`
4. For inferred relationships, use `cruxible_add_relationship` after deterministic loading
5. Legacy `cruxible_ingest` is still available for older configs, but workflows are the preferred path for new configs
6. For entities from free text, use `cruxible_add_entity` (entities must exist before adding relationships to them)
7. Use the `instance_id` from `cruxible_init` in all subsequent calls

## Onboarding a New Domain

Follow this workflow when going from raw data to a working graph.

### Step 1 — Discover the Domain

Before writing any config, understand the domain and the data:
- Explore data files: schema, columns, dtypes, row counts, sample rows
- Identify entity types, relationships, and key properties
- Brainstorm 2–4 questions the graph should answer
- Propose the domain model to the user; wait for confirmation before writing config

### Step 2 — Prepare Data

Profile and clean data files before workflow loading:
- Check row counts, column types, null counts
- Validate primary key uniqueness (duplicates = wrong grain)
- Validate foreign keys (orphan FKs = broken edges)
- Check join keys across files (zero overlap = wrong join key)
- Remove junk rows (sentinels, test data, all-null rows)
- Fix encoding issues and extract embedded structured data

Use external tools (Python, Polars, etc.) for all cleaning. Cruxible ingests and evaluates; cleaning is external.

### Step 3 — Write the YAML Config

Define all required sections:

- **entity_types**: Dict keyed by type name. Mark the ID property with `primary_key: true` (on the property, not the entity).
- **relationships**: `from`/`to` entity types, optional edge properties, cardinality. Include cross-dataset relationship types — but do NOT create ingestion mappings for them. They'll be populated via `cruxible_find_candidates` in Step 6.
- **named_queries**: Leave this section empty for now. You'll design queries in Step 7 after seeing what's actually in the graph.
- **constraints**: Validation rules with severity levels.
- Prefer **contracts**, **artifacts**, **providers**, and **workflows** for deterministic loading.
- Use **ingestion** only for legacy compatibility when you intentionally keep a mapping-based config.

See the [Config Reference](config-reference.md) for the full schema.

### Step 4 — Validate and Initialize

```
1. cruxible_validate(config_path="config.yaml")
2. cruxible_init(root_dir=".", config_path="config.yaml")
3. Save the instance_id for all subsequent calls
```

### Step 5 — Load Source Data

For new configs, prefer workflow-based deterministic loading:

```
1. cruxible_lock_workflow(instance_id)
2. cruxible_run_workflow(instance_id, "load_<dataset>", input_payload={...})
3. If canonical: cruxible_apply_workflow(instance_id, "load_<dataset>", expected_apply_digest=..., expected_head_snapshot_id=..., input_payload={...})
```

Legacy compatibility path for older configs:

```
1. cruxible_ingest(instance_id, "<entity_mapping>", file_path="data/<entities>.csv")
2. cruxible_ingest(instance_id, "<relationship_mapping>", file_path="data/<relationships>.csv")
```

Check for errors after each load step before continuing.

### Step 6 — Discover Cross-References

For entities from free text or external sources (no CSV):
- Use `cruxible_add_entity` — entities must exist before you can add relationships to them.

For each cross-dataset relationship type, use `cruxible_find_candidates`:

1. Use `cruxible_sample` to inspect entities on both sides
2. Use `property_match` with `iequals` on name fields to cross-reference entities across types/datasets
3. Use `shared_neighbors` when entities share connections through an intermediary
4. Review candidates and persist confirmed matches with `cruxible_add_relationship`
   using only schema-declared properties. For governed judgment relationships,
   use candidate group proposal flows so support/unsure/contradict signals are retained.

`cruxible_find_candidates` only does exact/iequals matching. For fuzzy matching, transliteration, or abbreviation handling, use your own tools (Python, Polars, etc.) and persist matches with `cruxible_add_relationship`.

### Step 7 — Design Named Queries

Now that you can see what entities and relationships are in the graph, design the named queries. Start from the use cases proposed in Step 1.

Key considerations:
- **Entry point**: which entity type does the user start from?
- **Traversal direction**: outgoing follows ownership chains; incoming finds who owns/controls the entry entity
- **Multi-relationship fan-out**: a single step can traverse multiple relationship types

Add the named queries to the YAML config, re-validate with `cruxible_validate`, and reload with `cruxible_init(root_dir=...)` (omit `config_path` to reload).

### Step 8 — Validate Graph Quality

`cruxible_evaluate` checks structural health — orphans, violations, coverage gaps. For deeper review including cross-dataset gap analysis and intelligence-driven discovery, use your agent-side review skill/playbook and drive the loop with `cruxible_evaluate`, `cruxible_sample`, `cruxible_find_candidates`, `cruxible_get_entity`, `cruxible_get_relationship`, and `cruxible_feedback`.

### Step 9 — Run Sample Queries

1. Run `cruxible_query` on representative cases. The `params` dict must include the primary-key property of the entry_point entity type.
2. Inspect receipt traversals to confirm correctness.
3. Confirm output matches domain expectations.

### Step 10 — Provide Feedback

1. Use `cruxible_feedback` on key edges (pass `source="agent"` when you are the reviewer, `source="human"` when relaying a human's judgment)
2. Record end-to-end correctness with `cruxible_outcome`
3. Use `cruxible_find_candidates` to discover missing links
4. Use `cruxible_add_relationship` to persist confirmed candidates

### Step 11 — Handoff

Present what was built so the user knows what they have and how to use it:
- Entity type counts and sources
- Relationship counts and how they were added (source data, find_candidates, AI-inferred)
- Named queries with example `params` dicts the user can copy-paste
- Suggested next steps (query, audit, review edges, discover, health check, add rules)

## Common Workflows

### Debugging a Query

1. `cruxible_schema` — verify query and traversal definitions exist
2. `cruxible_sample` — confirm source entities are present in the graph
3. `cruxible_query` — run with focused parameters
4. `cruxible_receipt` — inspect the traversal trace for unexpected paths
5. Fix config or data, then repeat

### Edge-Level Review

1. `cruxible_query` — get a `receipt_id`
2. `cruxible_feedback` — approve, reject, flag, or correct specific edges
3. Re-run the query to confirm behavior changes

### Iterative Graph Refinement

1. `cruxible_evaluate` — get current findings
2. `cruxible_find_candidates` — discover likely missing edges
3. `cruxible_add_relationship` — persist confirmed candidates
4. Re-evaluate and compare counts

### Auditing a Decision

1. `cruxible_list(resource_type="receipts")` — locate the query run
2. `cruxible_receipt` — get traversal evidence
3. `cruxible_list(resource_type="feedback", receipt_id=...)` — see feedback
4. `cruxible_list(resource_type="outcomes", receipt_id=...)` — see outcomes

### Feedback-to-Constraint Workflow

When rejection patterns emerge from feedback:

1. List feedback records and filter for `action="reject"` on the target relationship
2. For each rejected edge, look up source and target entity properties with `cruxible_get_entity`
3. Compare rejected edges — look for shared property mismatches
4. If a pattern is strong (5+ rejections), propose a constraint
5. Call `cruxible_add_constraint` with the rule expression
6. Run `cruxible_evaluate` to verify the new constraint flags expected violations

## Config Authoring Tips

### Entity Types
- Mark the ID property with `primary_key: true` on the property itself
- Properties are required by default; use `optional: true` for nullable fields
- Use `enum: [...]` to restrict property values
- Use `indexed: true` for properties you'll filter on frequently

### Relationships
- `from`/`to` must reference existing entity type names
- Declare the relationship properties you need for inferred relationships, then
  set only those schema-owned fields. For governed judgments, preserve support
  evidence through candidate group signals instead of ad hoc confidence fields.
- Use `inverse` for bidirectional traversal

### Ingestion Mappings
- One mapping per data source (entity or relationship)
- Use `column_map` to rename CSV columns to property names: `{csv_column: property_name}`
- For inferred relationships, use `cruxible_add_relationship` instead of writing scripts to batch-produce CSVs

### Named Queries
- Keep traversals focused — one clear question per query
- Use `filter` on traversal steps to narrow results
- Use `constraint` for runtime parameter binding (e.g., `target.drug_id == $drug_id`)

### Constraints
- Rule format: `RELATIONSHIP.FROM.property <op> RELATIONSHIP.TO.property`
- Supported operators: `==`, `!=`, `>`, `>=`, `<`, `<=`
- Use `severity: warning` unless the violation rate is very high, then use `error`
- Constraints are evaluated by `cruxible_evaluate`, not during ingestion

## Anti-Patterns

- **Embedding LLM calls in scripts that batch-produce CSVs** — Use `cruxible_add_relationship` for inference tasks instead. Scripts are for cleaning, not reasoning.
- **Ingesting relationships before entities** — Edges reference entity IDs. Ingest entities first.
- **Skipping validation** — Always run `cruxible_validate` before `cruxible_init`. Fail fast.
- **Ignoring evaluate results** — Run `cruxible_evaluate` after every major change. Orphans and gaps indicate data problems.
- **Writing unsupported ad hoc properties on inferred edges** — Relationship
  properties must be declared in the config. For governed judgments, use the
  proposal/group path so Cruxible retains tri-state support signals and review
  history.
- **Overloading a single query** — Split complex questions into multiple focused named queries rather than one query with many traversal steps.
