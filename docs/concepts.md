# Concepts

Cruxible is a deterministic state runtime with receipts. It gives
agents and humans a shared, governed substrate for domain state that should
survive beyond one prompt, one chat, or one run.

## A State Model, Not Scratch Memory

A **state model** is the governed universe exposed to an agent: entity types,
relationships, workflows, named queries, review state, receipts, traces, and
outcomes.

Taken as one versioned unit — the ontology, its governed claims, queries,
Procedures, and the receipts that explain them — that artifact is a **Crux**.
The runtime's own vocabulary stays mechanical (instance, state, config); *Crux*
names the thing you author, review, and ship.

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

The recommended deployment shape is a local Cruxible daemon, launched with
`cruxible server start`. The daemon owns state. Agents, CLI, client SDKs, and MCP
tools call into the daemon instead of editing graph state directly.

Permission modes are meaningful at that boundary:

| Mode | Purpose |
| --- | --- |
| `read_only` | Validate, inspect, query, and retrieve receipts |
| `governed_write` | Read-only plus receipt-persisting workflow runs, proposal workflows, and feedback |
| `graph_write` | Governed write plus raw graph mutation and group resolution |
| `admin` | Full lifecycle, including init, locks, canonical apply, ingest, and config mutation |

If an agent can import `cruxible_core`, read the daemon state directory, or
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

## Procedures

Workflows are designed; procedures are learned.

Procedures are state-held, agent-proposable compositions of operator-exported
actions. Their lifecycle is `pending → live`, `pending → rejected`,
`pending → withdrawn`, or `live → retired`; promotion requires an independently
identified reviewer, and a live definition is immutable. Propose a replacement
with a `supersedes` link instead of editing one in place.

`rejected` and `withdrawn` are deliberately distinct. `rejected` is a reviewer's
verdict on someone else's proposal and requires a reason. `withdrawn` is the
proposing actor retracting its own pending proposal — no reviewer, no required
reason, at the proposing tier:

```bash
cruxible procedure withdraw <procedure-id> --expected-version 1
```

Supersede only targets a *live* definition, so an author who changed their mind
before review has nothing to supersede; withdrawing is the move, not proposing a
renamed variant. A withdrawn definition is not live, so the one-live-per-name
law is untouched and the name is immediately free to re-propose. Withdrawing a
proposal you did not author is a review act and requires the reviewer tier.

Every definition has an explicit precondition. Use `{}` for always eligible, or
use `{entity_type, condition}` where `condition` is a property-equality mapping
evaluated against accepted graph state. It also has a required execution budget:
`wall_clock_s` is capped at 600 seconds, and `max_provider_calls` must cover the
statically computed maximum, including bounded `repeat` attempts.

Config providers are unavailable to procedures by default. An operator must set
`procedure_access` to `governed_write`, `graph_write`, or `admin`; only
timeout-enforced `http_json` and command providers are exportable. A run requires
the highest exported provider tier it references, with a `governed_write` floor.
Each invocation writes a crash-safe run record before evaluating its
precondition, so an interrupted run remains visible as `started` with a null
verdict. Run verdicts are execution telemetry; external-world results belong in
outcomes attached to the run receipt.

Acceptance pins a procedure to the config and lock the reviewer recompiled it
against, and every run compares against those pins. Recompiling at run time
proves a definition still compiles; it does not prove it compiles against the
same modelled world that was approved. A provider re-pointed at a different
endpoint, an entity type redefined, a query rewritten — each recompiles cleanly
while changing what the approved procedure does. So a run whose current
`config_digest` or `lock_digest` differs from the accepted one is refused, with
both values on the run receipt under `accepted_against` and `executed_against`:

```
Procedure 'PRC-...' is pinned to the config and lock it was accepted against,
which no longer match this instance (config_digest: accepted against sha256:...,
now sha256:...).
```

Recover by re-proposing the definition and having an independent reviewer accept
it against the current config and lock:

```bash
cruxible procedure propose <definition-file> --supersedes <live-procedure-id>
cruxible procedure resolve <new-procedure-id> --action accept --expected-version 1
```

Restoring the accepted config and re-running `cruxible lock` also clears the
refusal. There is no flag that runs a procedure against an unreviewed model.

A *missing* pin fails closed the same way, and for the same reason: with no
recorded digest there is nothing the reviewer is known to have approved, and
"no pin" must not be the one way to run a procedure unverified. Acceptance always
writes both pins, and clones and snapshots carry them across, so this only
reaches procedures accepted before pinning existed. They re-propose and
re-accept once, like any other definition change.

## The Entity Graph

Cruxible stores entities and relationships in a directed graph. Each node is an
entity with a type and typed properties. Each edge is a typed relationship with
declared properties plus system-managed review and provenance metadata.

Config-defined edge properties are domain data. Cruxible-managed relationship
metadata stores assertion review/lifecycle state and provenance separately from
domain properties; feedback and group resolution update that metadata rather
than writing domain fields.

Provenance uses a two-part vocabulary: `source` names the channel that wrote
the edge (`cli_batch_direct_write`, `http_api`, `mcp_add`, `group_resolve`, workflow apply
sources), and `source_ref` names the operation — a snake_case operation name
(`add_relationship`, `batch_direct_write`) or a structured ref for governed and
workflow writes (`group:<group_id>`, `workflow:<workflow>:<step>`) — never a
surface spelling, so command or tool renames cannot leak into stored provenance.
Provenance is historical record: values written by earlier versions are never
rewritten.

## Claim Identity

The graph is a multigraph: several parallel edges can share one relationship
tuple (`relationship_type`, `from_type`, `from_id`, `to_type`, `to_id`). Three
keys with different jobs address this, and they are not interchangeable:

| Key | What it identifies | Stability |
| --- | --- | --- |
| `claim_id` | One claim (edge), as a minted opaque identity | Stable — survives pull-apply, snapshot/clone, publish→pull, backup/restore |
| `edge_key` | One stored edge within a tuple, for this load | Unstable — a per-load disambiguation hint, never identity |
| `idempotency_key` | One *write request*, for retry safety | Caller-supplied, scoped to the resolved actor and subject |

Tuple coordinates remain authoritative for naming a claim. When a tuple is
ambiguous (parallel edges), `claim_id` is the preferred disambiguator and takes
precedence over `edge_key`; supplying both with disagreeing values is refused.
`edge_key` survives for legacy records only: images materialized before claim
identity existed carry no `claim_id`, and those images can be re-materialized
forever (old snapshots, overlays tracking never-upgraded upstreams), so the
per-load key remains the only way to address their parallel edges.

`idempotency_key` identifies nothing in the graph. Retrying a write with the
same key applies it once and returns the original result; reusing a key with a
different declaration is refused.

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

## Feedback, Attestations, And Outcome Contracts

Three verbs carry judgment about claims, and they answer three different
questions. **Feedback** adjudicates: a reviewer changes a claim's review
status. **Attestation** observes: an actor records what they saw, without
moving review status. **Outcome contracts** commit in advance to what result
would prove a decision right, then record what reality said.

### Feedback (adjudication)

Feedback is edge-level review tied to a receipt. Every action requires
`GRAPH_WRITE` — adjudication is a reviewer-tier act:

| Action | Effect |
| --- | --- |
| `accept` | Mark the edge trusted by the reviewer (the stored review status remains `approved`) |
| `reject` | Exclude the edge from future query results |
| `correct` | Apply declared property corrections and accept (requires a non-empty `corrections` object) |

The former `flag` action was removed: it un-approved the edge to `pending`
while storing no annotation, so it destroyed the reviewer's signal instead of
recording it. Record a doubt with an attestation instead.

Query receipts with relationship or path results can be used as evidence for
edge feedback via `feedback from-query`: the user selects one relationship row
or one path segment, and Cruxible applies the normal feedback path to that
existing assertion. This is separate from group resolution. Use `group get` and
`group resolve` when the decision is about a candidate group thesis or member
set rather than one existing edge.

### Attestations (observation)

An attestation records one actor's dated observation about one claim tuple:
stance `support`, `contradict`, or `unsure`, with optional evidence refs and a
note. Attestations are immutable and append-only; they attach to the claim
without touching its review status, so a `governed_write` agent that cannot
adjudicate can still put what it saw on the record. `support` on an absent
tuple creates a pending claim when both endpoints exist; `contradict` and
`unsure` refuse to conjure claims they dispute.

`attest queue` lists live claims with open current-content contradictions —
the reviewer's inbox of disputed state. A reviewer answers with
`attest resolve` (`upheld` / `corrected` / `invalidated`), appending a
disposition while the original observation stays intact. The split matters:
observations are data about whether state is *true*; adjudications are
decisions about whether state is *accepted*.

### Outcome Contracts (resolution contracts)

A resolution contract is opened on a subject *before* it is accepted:
`outcome open` declares a free-text success criterion, a check time, an
expiry, and a pinned measurement (a named query with frozen definition digest
and execution options, or a set of attestations). The subject is never
mutated.

A contract only becomes answerable through governance: a
`requires_resolution_contract` mutation guard on the accepting transition
activates it, and accepting an outcome-tracked decision refuses until a
contract exists. A prepared contract that no guarded acceptance activates
expires unanswered.

`outcome resolve` records the verdict — `satisfied`, `contradicted`, or
`indeterminate` — under evidence-clock discipline: the resolving receipt's or
attestation's own timestamps settle timing, not the caller's claim, and if the
measurement query changed since opening, only `indeterminate` remains. One
standing resolution per contract; `outcome dispose` upholds or overturns it,
and an overturn re-opens the contract for exactly one further answer.
`outcome due` is the attention surface (`due` / `overdue` / `contradicted`).

Together the three verbs let Cruxible accumulate judgment state — and a
calibration record of how those judgments fared — without relying on agent
memory.

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

Cruxible uses Pydantic for typed models, Polars for data operations, Click/Rich
for CLI, FastAPI for the daemon, and FastMCP for agent tools. Persistence is a
single per-instance SQLite `state.db` that holds graph state plus
receipts/traces/groups/feedback/outcomes/decisions/snapshots/source-artifacts; a
NetworkX `MultiDiGraph` is the in-memory representation of that graph state.
