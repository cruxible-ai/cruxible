# Concepts

Cruxible Core is a deterministic world-model runtime with receipts. This guide explains the architecture, primitives, and workflows that make it work.

## World Model, Not Agent Scratch Memory

Cruxible is for shared domain state: entities, relationships, constraints, named queries, and accepted judgments that need to persist across agents, users, and runs.

It is not a scratchpad for an individual agent's temporary notes or prompt context.

- **Agent memory** is agent-centric, heuristic, and useful for continuity.
- **Cruxible state** is domain-centric, explicit, reviewable, and meant to be operationally trusted.
- **Receipts** explain how a result or proposal was produced.
- **Feedback and outcomes** calibrate accepted judgment over time.

## Coherence and Shared Truth

LLM agents do not expose a stable, inspectable internal state. What looks like "state" is really a transient encoding of the current prompt, retrieved context, and recent tool outputs.

That becomes a coherence problem as soon as more than one agent or human is involved. Two capable agents can start from nearly the same material and still diverge because they:

- saw different slices of context
- summarized prior work differently
- retrieved different evidence
- interpreted procedure or policy differently
- carried forward different local assumptions

In practice this creates three different coherence problems:

- **factual coherence**: what is true?
- **procedural coherence**: what workflow are we following?
- **judgment coherence**: what has already been approved, rejected, corrected, or deferred?

Without an external shared substrate, each agent is effectively carrying a lossy private replica of reality. That is acceptable for lightweight tasks. It breaks down for repeated, collaborative, or high-stakes work.

The same problem shows up between humans and agents. A human may think a fact, policy interpretation, or prior decision is already settled while the model, under a different framing, acts as if it is softer, different, or missing entirely. Because LLM judgments are frame-sensitive and transient, "what the model thinks" is too unstable to serve as operational truth on its own.

Cruxible addresses this by externalizing the parts that should not live only in prompt-local context:

- accepted facts and relationships
- named queries and constraints
- review status and governed judgments
- provenance and receipts explaining how state was produced

Agents can still keep private working memory, but they coordinate against the same operational state instead of against incompatible internal summaries.

## AI Outside, Determinism Inside

Cruxible inverts the typical AI architecture. Instead of embedding an LLM inside the decision engine, it keeps the AI entirely outside:

```
┌──────────────────────────────────────────────────────────────┐
│  AI Agent (Claude Code, Cursor, Codex, ...)                  │
│                                                              │
│  1. Reads docs → understands user's domain                   │
│  2. Generates config YAML → validates via cruxible_validate  │
│  3. Reads raw data → reasons about relationships             │
│  4. Uses cruxible_find_candidates for mechanical matching    │
│  5. Calls cruxible_ingest to populate graph                  │
│  6. Calls cruxible_evaluate → self-reviews, surfaces weakly  │
│     supported governed edges to human with receipts          │
│  7. Human: review / accept all / defer / reject              │
│  8. Calls cruxible_query → presents results to human         │
│  9. Collects human feedback → calls cruxible_feedback        │
│  10. Records outcomes → calls cruxible_outcome               │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│  Cruxible Core (the runtime)                                 │
│                                                              │
│  Deterministic. No LLM. No opinions. No API keys.            │
│  Config → Graph → Query → Receipt → Feedback                 │
└──────────────────────────────────────────────────────────────┘
```

The AI agent (Claude Code, Cursor, Codex, or any MCP-capable agent) provides all intelligence: understanding domains, generating configs, inferring relationships, and interpreting results. Core provides deterministic execution with full provenance.

This means:
- **No API keys** in the runtime — no LLM calls, no token costs during execution
- **Reproducible results** — same config + same data = same output, every time
- **Auditable decisions** — every answer includes a receipt showing exactly how it was derived
- **Domain-agnostic** — the agent adapts to any vertical by reading the config

## Four Primitives

Everything in Cruxible flows through four primitives:

### Config

A YAML file that defines the decision domain: entity types with typed properties, relationships between entities, declarative named queries, validation constraints, and ingestion mappings for loading data.

The config is the single source of truth for a shared domain/world model. AI agents write it; Core validates and executes against it. See [Config Reference](config-reference.md) for the full schema.

### Ingest

Loading data into the entity graph. Two paths:

- **Deterministic ingestion** (`cruxible_ingest`): Bulk load from CSV/JSON through config-defined mappings. For data that explicitly exists in source files — entity records and known relationships.
- **Inferred proposals** (`cruxible_add_entity`, governed candidate groups, or `cruxible_add_relationship`): For entities from free text or relationships that require judgment (classification, matching, inference). The AI agent reasons about the data, proposes edges with declared properties, and uses governed group flows when tri-state support history matters.

Entity ingestion always comes before relationship ingestion — edges reference entity IDs that must exist.

### Query

Named queries are declarative traversal patterns defined in the config. Each query has an entry point (entity type), a sequence of traversal steps (follow relationships, apply filters), and a return type.

Every query produces a **receipt** — a structured proof of the traversal path, filters applied, and entities visited. Receipts are stored in SQLite and can be retrieved later for auditing.

### Feedback

Edge-level feedback tied to specific receipts:

- **approve** — Edge is correct; trusted in future traversals
- **reject** — Edge is wrong; excluded from future query results
- **correct** — Edge needs property corrections (provide a corrections dict)
- **flag** — Edge needs review; no behavior change

**Outcomes** are separate from feedback — they track whether the overall query result was correct, incorrect, partial, or unknown. Use outcomes for calibration and accuracy measurement over time.

Together, feedback and outcomes let Cruxible accumulate accepted judgment state without turning the graph into an unreviewed scratch memory layer.

## The Entity Graph

Cruxible stores entities and relationships in a directed graph (NetworkX DiGraph). Each node is an entity with a type and properties. Each edge is a typed relationship with its own properties.

The graph is persisted to disk and loaded on init. All mutations (ingest, add_entity, add_relationship, feedback) update the graph deterministically.

### Edge Properties

Every edge carries a `properties` dict. Properties come from three sources:

**Config-defined properties** are declared in the relationship schema and set at creation time — either via ingestion mappings or explicit `add_relationship` calls. Examples: `verified`, `source`, `evidence`, or any domain-specific field declared by the config. See [Config Reference](config-reference.md) for property type definitions.

**`review_status`** is set by feedback actions, not declared in the config schema:

| Feedback action | Source | review_status |
|-----------------|--------|---------------|
| approve / correct | human | `human_approved` |
| approve / correct | agent | `agent_approved` |
| reject | human | `human_rejected` |
| reject | agent | `agent_rejected` |
| flag | any | `pending_review` |

Absent until the first feedback action is applied to an edge. `correct` additionally merges a corrections dict into the edge properties before setting approved status.

**`_provenance`** is internal metadata tracking edge origin. Stamped automatically on any relationship creation or update — via ingestion, MCP `add_relationship`, or CLI `add-relationship`. Fields:

- `source` — origin system (e.g. `"ingest"`, `"mcp_add"`, `"cli_add"`)
- `created_at` — ISO 8601 timestamp of edge creation
- `source_ref` — reference identifier (e.g. mapping name, tool name)
- `last_modified_at` — updated on edge replacement or feedback (absent until first modification)
- `last_modified_by` — what modified it (e.g. `"feedback:approve"`, `"ingest"`)

Prefixed with `_` to signal it is system-managed — do not set it manually.

Tools like `cruxible list edges` strip `_provenance` from display. Export tools like `cruxible export edges` include it raw.

## Receipts: Provenance as a DAG

Every query produces a receipt — a directed acyclic graph of evidence nodes showing:

- Which entity was the entry point
- Which traversal steps were executed
- Which filters were applied at each step
- Which entities were visited and returned
- Timestamps for the entire operation

Receipts are stored in SQLite and can be exported as JSON, Markdown, or Mermaid diagrams. They enable full auditability: given a receipt ID, you can reconstruct exactly why a particular answer was returned.

## Constraints and Evaluation

Constraints are validation rules that check relationships against business logic:

```yaml
constraints:
  - name: replacement_same_category
    rule: "replaces.FROM.category == replaces.TO.category"
    severity: warning
    description: "Replacement parts should be in the same category"
```

Run `cruxible_evaluate` to check the graph for:
- **Orphan entities** — entities with no relationships
- **Coverage gaps** — expected relationships that are missing
- **Constraint violations** — edges that violate defined rules
- **Governed support findings** — pending or weakly supported governed relationships derived from tri-state proposal signals

### Feedback-to-Constraint Workflow

When rejection patterns emerge from feedback, encode them as constraints:

1. Review feedback records directly or use an agent-side analysis skill/playbook
2. Identify repeated property mismatches in rejected edges
3. Call `cruxible_add_constraint` to encode the pattern as a rule
4. Run `cruxible_evaluate` to verify constraints flag expected violations

This creates a virtuous cycle: human feedback trains the constraint system, which then catches similar issues automatically.

## Permission Modes

The MCP server runs in one of three cumulative permission tiers, controlled by the `CRUXIBLE_MODE` environment variable:

| Mode | Tools Available |
|------|----------------|
| **READ_ONLY** | `validate`, `init` (reload only), `schema`, `query`, `receipt`, `list`, `sample`, `evaluate`, `find_candidates`, `get_entity`, `get_relationship` |
| **GRAPH_WRITE** | Everything in READ_ONLY + `add_entity`, `add_relationship`, `feedback`, `outcome` |
| **ADMIN** | Everything including `init` (create), `ingest`, `add_constraint` |

Default is `ADMIN`. Use `CRUXIBLE_ALLOWED_ROOTS` to restrict which directories `cruxible_init` can access.

These modes are enforced at the daemon boundary. They are meaningful when agents talk to Cruxible through the daemon/API surface, not when an agent can import `cruxible-core` runtime modules directly in the same environment.

## Candidate Detection

Two strategies for discovering missing relationships at scale:

### Property Match

Rules-based matching on entity properties. Define match rules that compare properties between potential source and target entities:

- `equals` — Type-strict hash-join (O(n+m))
- `iequals` — Case-insensitive hash-join (O(n+m))
- `contains` — Substring match (brute-force, fails fast on large sets)

Set `min_confidence` to control the minimum fraction of rules that must match.

### Shared Neighbors

Graph-structure matching via common connections. Finds entity pairs that share neighbors through a specified relationship. Set `min_overlap` to control the minimum neighbor overlap ratio.

**Bootstrapping pattern:** Create initial edges with `cruxible_add_relationship`, then use `shared_neighbors` to discover more entities sharing those same neighbors.

## Technology Stack

- **Pydantic** for all models — config schema, runtime types, receipts, MCP contracts
- **Polars** for data operations — ingestion and candidate detection use DataFrames
- **NetworkX** for the entity graph — DiGraph for entity/relationship storage
- **SQLite** for persistence — receipts, feedback, and outcomes
- **YAML** for config — the single source of truth for a decision domain
- **Click + Rich** for the CLI — terminal interface with formatted tables
- **FastMCP** for the MCP server — primary interface for AI agents
- **structlog** for audit logging — JSON-formatted logs to stderr
