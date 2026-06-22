# Concepts

Cruxible Core is a deterministic state runtime with receipts. It gives
agents and humans a shared, governed substrate for domain state that should
survive beyond one prompt, one chat, or one run.

## A State Model, Not Scratch Memory

A **state model** is the governed universe exposed to an agent: entity types,
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

The recommended `0.2` deployment shape is a local Cruxible daemon, launched with
`cruxible server start`. The daemon owns state. Agents, CLI, client SDKs, and MCP
tools call into the daemon instead of editing graph state directly.

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

- A **standalone kit** can initialize a state model by itself.
- An **overlay kit** targets a published upstream state and adds local schema,
  workflows, data, and governed proposal surfaces.
- An **overlay** is a local instance tracking a published upstream state.
- A **clone** is a point-in-time copy from a snapshot.
- **Local state** is customer-owned seeded or runtime state in the overlay.

Example:

- `kev-reference` is a standalone kit that builds public Vendor, Product, and
  Vulnerability state from pinned KEV/NVD/EPSS artifacts.
- `kev-triage` is an overlay kit that targets `kev-reference` and adds customer
  assets, services, owners, controls, incidents, findings, remediation, and
  governed exposure workflows.

Kit distribution details live in [Kit Authoring And Distribution](kit-authoring.md).

## Config

The config is the schema and execution contract for a state model. It can
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

## Source Evidence

Source artifacts let agents attach governed proposal evidence to stable
document locations without putting the whole document into every proposal.
Register a local Markdown file with `cruxible source register`; Cruxible stores
the document hash, parser version, parsed chunks, and a source artifact ID.

Source-evidence locators use one of two shapes:

```yaml
source_evidence:
  - source_artifact_id: SRC-...
    chunk_id: CHK-...
```

or:

```yaml
source_evidence:
  - source_artifact_id: SRC-...
    heading_path: ["Compatibility Evidence"]
    block_selector: paragraph:1
```

Use `chunk_id` when copying a locator from the registration output. Use
`heading_path` plus `block_selector` when the source should remain readable in a
hand-authored proposal. `source_artifact_id` is always required, and one locator
form must be complete.

Retention controls whether Cruxible keeps only the parsed manifest or also a
deep copy of the source bytes:

- `manifest_only` stores chunk metadata, hashes, and the local path. Dereference
  rereads the local file and reports drift if the content no longer matches.
- `archive` stores the manifest plus source bytes in the runtime state DB.
  Dereference can use the archived copy even if the original local file moves or
  changes.

Direct relationship writes can attach `evidence_refs` or `source_evidence` so a
live edge has durable provenance. That is not the same as governed acceptance:
direct evidence-backed adds remain unreviewed relationship state. Use candidate
groups when a human or policy needs to approve the relationship judgment.

## Inline Queries

Named queries remain the canonical query contract for workflows, docs, and
repeatable operating procedures. Agents can also run bounded inline queries for
one-off filtering and candidate discovery. Inline query definitions use the same
shape as named queries plus a required `name`, persist receipts for auditability,
and are never written back into `config.named_queries`.

Promote an inline query into config once it becomes workflow-critical or
repeated enough that humans should review and name the surface.

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

Direct writes remain available for explicit state updates. When a direct
relationship write overlaps a pending proposal member, Cruxible keeps the write
permissive but annotates the affected group's `analysis_state` with
`direct_write_conflicts` and a `direct_write_conflict_summary`. Reviewers can
use that metadata to see that live state changed after the group was proposed;
the group status is not changed automatically.

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

Provenance uses a two-part vocabulary: `source` names the channel that wrote
the edge (`cli_add`, `http_api`, `mcp_add`, `group_resolve`, workflow apply
sources), and `source_ref` names the operation in snake_case operation
vocabulary (`add_relationship`, `batch_direct_write`) — never a surface
spelling, so command or tool renames cannot leak into stored provenance.
Provenance is historical record: values written by earlier versions are never
rewritten.

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

Entity change history is a receipt-derived read model. `entity history`
and the matching API/MCP surface show recorded property diffs from mutation
receipts. This is not a named query over live graph state: it only reports diffs
explicitly recorded on entity-write receipts, so receipts created before that
detail existed are treated as legacy gaps rather than inferred timeline events.

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

Evaluate findings are returned severity-first (`error`, then `warning`, then
`info`) while preserving original order within the same severity. CLI, MCP, and
HTTP callers can filter findings by severity and category before `max_findings`
is applied; summary counts remain full-state counts.

For governed relationships, `evaluate` distinguishes group-backed support from
direct evidence-backed support. Direct governed relationships with stored
evidence refs are not reported as missing group signal trails, while direct
governed relationships with no evidence refs remain weak and are flagged.
Free-text rationale alone is not evidence support.

Use repeated feedback and outcome patterns to decide when a domain rule should
become an explicit constraint or decision policy.

## Technology

Cruxible uses Pydantic for typed models, NetworkX for graph storage, Polars for
data operations, SQLite for receipts/traces/groups/decision logs, Click/Rich for
CLI, FastAPI for the daemon, and FastMCP for agent tools.
