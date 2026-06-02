# Concepts

Cruxible Core is a deterministic world-model runtime with receipts. It gives
agents and humans a shared, governed substrate for domain state that should
survive beyond one prompt, one chat, or one run.

## World Model, Not Scratch Memory

A **world model** is the governed universe exposed to an agent: entity types,
relationships, workflows, named queries, review state, receipts, traces, and
outcomes.

Cruxible state is not private agent memory. Agent memory is prompt-local,
heuristic, and useful for continuity. Cruxible state is domain-centric,
explicit, reviewable, queryable, and intended to be operationally trusted.

Use Cruxible for:

- accepted facts and relationships
- governed judgments and review status
- deterministic workflow outputs
- reusable named queries and constraints
- receipts, traces, decision records, feedback, and outcomes

Use agent-local notes for temporary reasoning that should not become shared
truth.

## The Runtime Boundary

The recommended `0.2` deployment shape is a local `cruxible-server` daemon. The
daemon owns state. Agents, CLI, client SDKs, and MCP tools call into the daemon
instead of editing graph state directly.

Permission modes are meaningful at that boundary:

| Mode | Purpose |
| --- | --- |
| `read_only` | Validate, inspect, query, and retrieve receipts |
| `governed_write` | Read-only plus receipt-persisting workflow runs, proposal workflows, and feedback |
| `graph_write` | Governed write plus raw graph mutation and group resolution |
| `admin` | Full lifecycle, including init, locks, canonical apply, ingest, and config mutation |

If an agent can import `cruxible-core`, read the daemon state directory, or
control the daemon runtime, these modes are advisory. For stronger local
separation, see [Isolated Deployment](isolated-deployment.md).

## Kits, Overlays, Clones, And Local State

A **kit** is a versioned bundle with `cruxible-kit.yaml`, `config.yaml`,
provider code, optional data, and a bundled `cruxible.lock.yaml`.

- A **standalone kit** can initialize a world model by itself.
- An **overlay kit** targets a published upstream world and adds local schema,
  workflows, data, and governed proposal surfaces.
- An **overlay** is a local instance tracking a published upstream world.
- A **clone** is a point-in-time copy from a snapshot.
- **Local state** is customer-owned seeded or runtime state in the overlay.

Example:

- `kev-reference` is a standalone kit that builds public Vendor, Product, and
  Vulnerability state from pinned KEV/NVD/EPSS artifacts.
- `kev-triage` is an overlay kit that targets `kev-reference` and adds customer
  assets, services, owners, controls, incidents, findings, remediation, and
  governed exposure workflows.

Kit distribution details live in [Kit Authoring And Distribution](kit-authoring.md).
The meaning of `kind: ontology | world_model` is documented in
[World Models And Config Kind](world-model-kind.md).

## Config

The config is the schema and execution contract for a world model. It can
declare:

- entity types and typed properties
- relationships and edge properties
- named queries
- validation constraints
- artifacts and contracts
- providers and workflows
- governed relationship policies, feedback profiles, and outcome profiles

Use workflow-based loading for source artifacts. Providers parse external data,
dataflow steps shape it, and canonical apply steps write accepted graph state.

## Workflows

Workflows are repeatable procedures declared in config.

Canonical workflows build or refresh accepted state. They preview first and
return an `apply_digest` and `head_snapshot_id`; applying the preview commits
only if those identities still match.

Proposal workflows produce candidate groups for governed review. They preserve
tri-state signals from relationship-local signal sources:

- `support`
- `unsure`
- `contradict`

Accepted proposal groups create reviewed edges. Rejected groups preserve the
decision without mutating the graph.

Use built-in step types for generic deterministic dataflow mechanics:
`shape_items`, `join_items`, `filter_items`, `dedupe_items`, graph object
construction, and canonical apply steps. Use providers for source adapters,
external services, model calls, and domain policy.

## The Entity Graph

Cruxible stores entities and relationships in a directed graph. Each node is an
entity with a type and typed properties. Each edge is a typed relationship with
declared properties plus system-managed review and provenance metadata.

Config-defined edge properties are domain data. Cruxible-managed relationship
metadata stores assertion review/lifecycle state and provenance separately from
domain properties; feedback and group resolution update that metadata rather
than writing domain fields.

## Named Queries

Named queries are deterministic read surfaces over the graph. Each query has an
entry point, traversal steps, optional filters, and a return type. Every query
returns a receipt that explains the traversal path and evidence used.

Agents should use named queries as the stable read API for downstream work
instead of spelunking graph storage. Named queries package a stable primary
traversal and evidence path, and can attach bounded one-hop side context with
`include` when related facts such as owners, services, exceptions, controls, or
patch windows are part of the query contract. Use read tools for ad hoc context
that is not stable enough to belong in the named query surface.

## Receipts, Traces, And Decision Records

A **receipt** is a structured proof for a query, workflow run, canonical apply,
group resolution, feedback operation, or other state transition. It records the
operation and evidence chain.

An **execution trace** proves what provider ran: provider ref, version, runtime,
artifact hash, retained input/output payload evidence, status, error, and
timing. Full provider payload bodies are retained only when allowed by the
instance config's `runtime.trace_payloads` policy.

A **decision record** groups receipts, traces, and events around a higher-level
question so an agent or reviewer can reconstruct the decision history.

These are different proofs. Receipts explain how Cruxible decided or changed
state. Traces explain what executable provider produced evidence.

## Feedback And Outcomes

Feedback is edge-level review tied to a receipt:

| Action | Effect |
| --- | --- |
| `approve` | Mark the edge trusted by the reviewer source |
| `reject` | Exclude the edge from future query results |
| `correct` | Apply declared property corrections and approve |
| `flag` | Mark for review without changing behavior |

Outcomes record whether a result, proposal, or resolution was correct,
incorrect, partial, or unknown. Feedback and outcomes let Cruxible accumulate
accepted judgment state without relying on agent memory.

Query receipts with relationship or path results can be used as evidence for
edge feedback via `feedback-from-query`: the user selects one relationship row
or one path segment, and Cruxible applies the normal feedback path to that
existing assertion. This is separate from group resolution. Use `group get` and
`group resolve` when the decision is about a candidate group thesis or member
set rather than one existing edge.

## Constraints And Evaluation

Constraints encode validation rules over relationships. `evaluate` checks
orphan entities, coverage gaps, constraint violations, governed support state,
candidate opportunities, and weakly reviewed co-members.

Use repeated feedback and outcome patterns to decide when a domain rule should
become an explicit constraint or decision policy.

## Technology

Cruxible uses Pydantic for typed models, NetworkX for graph storage, Polars for
data operations, SQLite for receipts/traces/groups/decision logs, Click/Rich for
CLI, FastAPI for the daemon, and FastMCP for agent tools.
