<p align="center">
  <a href="https://cruxible.ai">
    <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/cruxible_logo.png" alt="Cruxible" width="400">
  </a>
</p>

# Cruxible

[![PyPI version](https://img.shields.io/pypi/v/cruxible?color=blue)](https://pypi.org/project/cruxible/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green)](https://github.com/cruxible-ai/cruxible/blob/main/LICENSE)

**Cruxible is hard state for AI agents** — a typed, verifiable state layer
that teams of agents and humans operate together. Work compounds into a
record of what you've determined to be true: every claim governed and linked
to its evidence. When the expensive question arrives (which assets are
exposed? what breaks downstream? is this authority still good law?), the
answer is computed over established truth, not guessed from a pile of
context.

You model your domain in a Terraform-like config: entity and relationship
types, deterministic workflows, write rules. The runtime enforces it.

<p align="center">
  <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/cruxible_architecture.svg" alt="Cruxible architecture: source systems are pinned as artifacts, workflows propose row-matched claims into domain state, the agent operation layer reviews and mints them, and reads come back as deterministic queries with receipts" width="740">
</p>

- **Ingest is deterministic.** Exports and tables from real systems
  are pinned as artifacts and matched row by row into proposals; model
  judgment is injected only where your pinned domain logic can't decide.

- **Writes are governed.** Governed relationships can only be written through
  a proposal flow that requires declared evidence, auto-resolves only under
  trust rules you set, and routes everything else to human review. Every
  accepted claim is attributed and carries a receipt.

- **The model is executable.** Recurring procedures are declared workflows in
  the same config: previewed before they apply, locked to the exact provider
  code and artifacts they compile against, replayable from receipts. State
  accumulates as the exhaust of governed work, and the model improves
  iteratively: feedback and outcomes are recorded in state, and the config
  evolves like code.

- **Reads are reproducible.** Same query, same state, same result, with a
  receipt explaining how it was derived. Queries express structure that
  retrieval can't: multi-hop traversals, review status, staleness against
  cited sources.

- **The core is deterministic.** No LLM inside, no hidden API calls. It works
  with any agent or harness, points at your existing systems, and mints into
  state only the claims worth coordinating around.

## Get Started

```bash
pip install cruxible
```

**Model your own domain**: hand your agent the authoring skills in
[`skills/`](https://github.com/cruxible-ai/cruxible/tree/main/skills)
(`prepare-data` → `create-state` → `review-state`) with your exports
(`wiki-to-state` converts an existing CLAUDE.md pile or Obsidian vault), or
start from [Modeling State](https://github.com/cruxible-ai/cruxible/blob/main/docs/modeling-state.md)
and the [config template](https://github.com/cruxible-ai/cruxible/blob/main/docs/config-template.yaml).

**Or run the demo** — a seeded supply-chain world, ~3 minutes, no tokens
(sandbox writes attribute to a built-in `operator` identity):

```bash
# shell 1 — local sandbox daemon
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/sandbox" cruxible server start

# shell 2 — kit bundles are fetched from the release and digest-verified
cruxible --server-url http://127.0.0.1:8100 init --kit agent-operation --kit supply-chain-blast-radius
cruxible context connect --server-url http://127.0.0.1:8100 --instance-id <instance-id>

# deterministic ingest: preview, then commit
cruxible run --workflow build_seed_state && cruxible apply --workflow build_seed_state --from-last-preview
cruxible run --workflow ingest_incidents && cruxible apply --workflow ingest_incidents --from-last-preview

# the incident feed can only PROPOSE impact edges; the judgment is yours, on the record
cruxible propose --workflow propose_incident_impacts_supplier
cruxible group list --status pending_review
cruxible group resolve --group <GRP-id> --action approve \
  --rationale "Confirmed against supplier geography" --expected-pending-version 1

# receipted answers through the edges you just admitted
cruxible query run open_incident_impacts --json
cruxible query run incident_impacted_suppliers --param incident_id=INC-TW-RAIL-2026-07 --json
```

When agents join, identity turns on: restart with `CRUXIBLE_SERVER_AUTH=true`,
claim the bootstrap credential, and mint each agent its own token — every
write is attributed. Details, permission tiers, and hardening:
[Quickstart](https://github.com/cruxible-ai/cruxible/blob/main/docs/quickstart.md) ·
[Runtime Auth And Agent Roles](https://github.com/cruxible-ai/cruxible/blob/main/docs/runtime-auth-and-agent-roles.md).

## Why Not Markdown, RAG, Or Vector Memory?

Markdown, retrieval, and vector memory hand a model raw text, so every
session it reconstructs what's true from scratch. For drafts, exploration,
and one-off questions, that's fine — but for the claims that are recurring,
shared, and expensive to get wrong, every fresh read re-rolls the
reconstruction, and a better model reads better, but it cannot certify its
own output. Cruxible's answer is to **model the domain instead of
engineering the context**: the durable slice of what's true becomes typed,
governed state, read instead of reconstructed. What changes:

| Markdown · RAG · vector memory | Cruxible |
|---|---|
| A claim is just text: no source, no review state | Claims carry provenance and review state; evidence-gated writes refuse references that don't dereference to content-hash-verified source chunks |
| Anything can be edited; nothing enforces what may change | Writes pass typed validation, guards, review, and lifecycle rules |
| Retrieval returns similar chunks; it can't follow exact links | Multi-hop traversal over typed relationships, with visibility rules applied at every hop |
| Counts and rollups are approximate summaries | Exact, repeatable counts and joins as deterministic workflow steps |
| Each read is fresh and can disagree with the last | One accepted state: the same answer for every agent and app |
| Freshness is unknowable: nothing says which chunks have gone stale | Claims cite dated, content-hashed sources; staleness is a queryable property, not a vibe |
| A correction is just more text; nothing ties it to the claim it corrects | Feedback and outcomes attach to the specific claim, decision, or workflow result as typed, queryable signal |
| Static text that doesn't improve from use | Claims mature from proposed to accepted; the ontology iterates with use |
| A better model reads better, but can't certify its own output | Guarantees come from a deterministic layer outside the model |

Markdown and retrieval remain the right tools for most text, and Cruxible
itself cites markdown chunks as source evidence. Version control narrows the
gap less than it seems: git reviews the diff, not the claim — nothing types
what a changed line asserts or refuses an edit that drops its evidence. And
nobody hand-tends this state: it accumulates as the exhaust of governed
work, not as a wiki someone has to maintain. If you already have the wiki
(a pile of CLAUDE.md files, a memory bank, an Obsidian vault), the
[`wiki-to-state`](https://github.com/cruxible-ai/cruxible/tree/main/skills/wiki-to-state)
skill converts it: pages become pinned evidence, an agent proposes the typed
claims, and you review what gets minted. The wiki survives as the source of
record; the graph becomes accountable to it.

## What A Governed Domain Looks Like

A minimal slice of a supply-chain ontology, as authored in a kit config:

```yaml
entity_types:
  Supplier:
    properties:
      supplier_id: { type: string, primary_key: true }
      name: { type: string, indexed: true }
      primary_geography: { type: string, optional: true }
  Component:
    properties:
      component_id: { type: string, primary_key: true }
      name: { type: string, indexed: true }
      criticality: { type: string, optional: true, enum_ref: criticality }
  Incident:
    properties:
      incident_id: { type: string, primary_key: true }
      title: { type: string, indexed: true }
      severity: { type: string, optional: true, enum_ref: incident_severity }

relationships:
  - name: supplier_supplies_component
    from: Supplier
    to: Component
  # Governed judgment: an incident materially impacts a supplier.
  - name: incident_impacts_supplier
    from: Incident
    to: Supplier

named_queries:
  # Blast radius: from an incident, traverse impacted suppliers to the
  # components they supply.
  components_exposed_by_incident:
    mode: traversal
    entry_point: Incident
    returns: Component
    traversal:
      - relationship: incident_impacts_supplier
        direction: outgoing
      - relationship: supplier_supplies_component
        direction: outgoing
```

The ontology is only part of the config: the same file declares the enum
vocabularies, guards, proposal routing, workflows, and providers, so a
domain's model, rules, and procedures ship together as one versioned,
composable kit.

Nobody types this state in by hand: it enters through the pathways the
config declares, and different state earns different treatment.

Hard facts are deterministic ingest. A BOM workflow pins the export as an
artifact and matches its rows into suppliers, components, and supply edges,
previewed before it commits:

```bash
cruxible run --workflow ingest_bom --input-file ./exports/bom-2026-07.csv    # preview
cruxible apply --workflow ingest_bom --from-last-preview                     # commit
```

`incident_impacts_supplier` is a judgment call, so it is governed: nothing
may write it directly, not even a workflow. The incident feed's workflow
records the incidents themselves as hard facts, but the impact edges it can
only *propose*. Those candidates land in a review group, each carrying the
signals and evidence that matched it:

```bash
cruxible propose --workflow propose_incident_impacts --input-file ./exports/incidents.json
```

The judgment itself stays with a human, or with an agent when the trust
rules you declared allow it. Approval is what mints the edges into accepted
state: attributed, rationale on record.

```bash
cruxible group list --status pending_review
cruxible group resolve --group GRP-7f3a --action approve \
  --rationale "Confirmed: fab flooding halts board shipments" \
  --expected-pending-version 1   # pins the decision to the state the reviewer saw
```

With the facts ingested and the impact claim approved, an agent (or app)
can ask for the blast radius of the incident (the components exposed through
its impacted suppliers) without scanning spreadsheets or tracing the bill of
materials by hand:

```bash
cruxible query run components_exposed_by_incident \
  --param incident_id=INC-42 \
  --json
```

Results come back with a receipt: the deterministic path from query parameters
to traversed edges to returned rows.

```json
{
  "items": [
    { "entity_type": "Component", "entity_id": "component-main-board" }
  ],
  "receipt_id": "RCP-...",
  "receipt": {
    "operation_type": "query",
    "query_name": "components_exposed_by_incident",
    "parameters": { "incident_id": "INC-42" },
    "nodes": [
      { "node_type": "query", "detail": { "entry_point": "Incident" } },
      { "node_type": "edge_traversal", "relationship": "incident_impacts_supplier" },
      { "node_type": "edge_traversal", "relationship": "supplier_supplies_component" },
      { "node_type": "result", "entity_type": "Component", "entity_id": "component-main-board" }
    ]
  }
}
```

Receipts are not logs — they are typed evidence graphs. Mutation receipts
record exactly what a write changed, and governed edges carry a reference back
to the receipt of the operation that created them.

This is what a pending review group looks like in the
[inspection UI](https://github.com/cruxible-ai/cruxible-app): the signal
matrix, each proposed edge with the evidence that matched it, and the
provenance rail tying the proposal back to its workflow, receipts, and
provider traces.

<p align="center">
  <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/ui_group_review.png" alt="Cruxible review group page: signal matrix, proposed edges each carrying matching evidence, and a provenance rail with workflow, receipts, and provider traces" width="900">
</p>

## Governance

Cruxible separates writing state from accepting it. State enters one of two
ways:

| Write mode | Use it for | What happens |
|---|---|---|
| **Direct write** | Asserting hard state: imports, deterministic relationships, source evidence | Live and queryable at once, with evidence when supplied, but unreviewed until a governed process approves it |
| **Governed proposal** | Judgment calls: uncertain or interpretive relationships | Candidates are grouped under one thesis with signal evidence and routed to a human or auto-resolution policy; approval writes accepted state with provenance, rejection records why |

Guards are declared in config and enforced at a single write chokepoint.
A relationship type can refuse direct writes entirely; a work item can be
blocked from closing until an approved review is linked; a write can be
required to co-create a linked entity in the same unit of work; a claim can be
required to carry source evidence. Evidence requirements are enforced, not
decorative: the write is refused unless every reference dereferences to a
registered source chunk whose content hash matches.

The agent-operation kit ships these live: a work item cannot close without
an approved review linked, and a review verdict must co-write its rationale
note in the same unit of work, so the work itself is typed state, gated on
review. Each kit README renders its declared guards as a generated table
([agent-operation's](https://github.com/cruxible-ai/cruxible/tree/main/kits/agent-operation/)).

## Workflows And Pinned Providers

Workflows orchestrate reads, providers, shaping, and writes as one declared,
reproducible procedure. Providers are the building blocks workflows call:
deterministic transforms and data loaders in Python, over HTTP, or as
commands. They are pinned, not trusted. The kit lockfile
(`cruxible.lock.yaml`) records each provider's version, content digest, and
declared side effects, and every call leaves an execution trace, so runs
replay deterministically.

Canonical workflows are **preview-first**:

```bash
cruxible run --workflow build_local_state    # executes against a clone, returns an apply digest
cruxible apply --workflow build_local_state --from-last-preview
```

`run` never touches live state. `apply` re-verifies the preview's digest
against the current config, lockfile, and head snapshot before committing.
If anything shifted underneath, it refuses. Workflows that produce governed
proposals run through `cruxible propose` and land in review instead of in
live state.

Declare → preview → apply, with a receipt at every step.

## Domain State And Operating State

Cruxible models two kinds of state, strongest together.

**Domain state** is the durable model of the world an agent reasons about:
assets, vulnerabilities, suppliers, products, cases, controls, policies,
risks. It answers what is true, proposed, reviewed, or constrained. *Which
assets are exposed to a known exploited vulnerability? Which supplier incident
affects which products and shipments?*

<p align="center">
  <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/ui_state_graph.png" alt="Cruxible state graph: a supply-chain domain of 780 entities and 1,734 edges, dots colored by entity type, with edge strokes carrying governance review state" width="900">
</p>

**Agent operating state** is the durable coordination layer for the work
itself: work items, review requests, decisions, open questions, risks,
actors, dependencies, lineage. It tracks what's active or blocked, why, who
reviewed it, and what changed.

A domain kit models the thing being worked on; an operating-state kit tracks
the work, decisions, and reviews around it. Typed operation-to-domain edges
(or `SubjectRef`s across instances) compose them into one queryable graph.
This is the type map of the supply-chain instance from the walkthrough above
— the agent-operation base layer composed under the domain overlay, every
relationship type carrying its live edge count:

<p align="center">
  <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/ui_type_map.png" alt="Cruxible type map of a composed supply-chain instance: base agent-operation types and violet domain overlay types, with labeled relationship types carrying live edge counts" width="900">
</p>

## State That Compounds

Knowledge shouldn't be wiped out by a context refresh, a model swap, or a
handoff. Three loops make the state improve with use:

1. **Feedback and outcomes.** Corrections, missing context, and policy gaps
   are recorded as feedback; outcomes record whether a decision or workflow
   result was later correct, incorrect, partial, or unknown. Repeated bad
   outcomes generate trust-demotion suggestions on the paths that produced
   them.
2. **Governed proposals.** Uncertain relationships are proposed, reviewed, and
   accepted or rejected with provenance; resolution paths carry an explicit
   trust status.
3. **Config iteration.** The ontology itself is refined as it's used (new
   entity types, relationships, guards, and queries), so the model of the
   domain matures alongside the data.

The LLM can change: swap vendors, upgrade, run several at once. What
compounds belongs to you. State, evidence, review history, feedback,
outcomes, and the ontology itself accumulate in a database you own, portable
down to a single file, not in a vendor's weights or a platform's memory. The
work agents do becomes your asset.

## Kits

A kit packages an ontology with its governance, queries, workflows, and
providers as one versioned, composable unit. Standalone kits define a full
state model; overlay kits compose local state, proposals, and workflows over
an upstream base. All seven ship working providers end to end.

Start with **agent-operation** — the domain-agnostic operating layer
Cruxible itself is developed with. The **KEV pair** runs the whole loop on
real CISA data ([KEV guide](https://github.com/cruxible-ai/cruxible/blob/main/docs/kev-guide.md));
**supply-chain-blast-radius** is the walkthrough above.

| Kit | Kind | What it models |
|-----|------|----------------|
| [agent-operation](https://github.com/cruxible-ai/cruxible/tree/main/kits/agent-operation/) | Agent operating state | Work items, review requests, decisions, risks, open questions, state notes, actors, lifecycle, and dependency context. |
| [project-domain](https://github.com/cruxible-ai/cruxible/tree/main/kits/project-domain/) | Domain overlay state | Roadmap items, milestones, release lines, and product areas composed over the agent-operation base — the project state Cruxible itself runs on. |
| [agent-release](https://github.com/cruxible-ai/cruxible/tree/main/kits/agent-release/) | Domain overlay state | Agent systems, versions, eval suites and runs, with governed certification and promotion gates. |
| [kev-reference](https://github.com/cruxible-ai/cruxible/tree/main/kits/kev-reference/) | Domain reference state | Public known-exploited vulnerability reference data. Consumed as a published state release (`state create-overlay`); init the kit itself only to build offline or publish your own. |
| [kev-triage](https://github.com/cruxible-ai/cruxible/tree/main/kits/kev-triage/) | Domain overlay state | Local asset exposure, service impact, controls, incidents, findings, remediation, and governed vulnerability triage. |
| [supply-chain-blast-radius](https://github.com/cruxible-ai/cruxible/tree/main/kits/supply-chain-blast-radius/) | Domain state | Suppliers, components, assemblies, products, shipments, and incident blast radius. |
| [case-law-monitoring](https://github.com/cruxible-ai/cruxible/tree/main/kits/case-law-monitoring/) | Domain state | Matter-centered case-law monitoring and authority impact. |

## Agent Setup

`pip install cruxible` already includes the Python client
(`import cruxible_client`); add the `[mcp]` extra for the `cruxible-mcp`
entrypoint. Nothing else is needed when the agent shares the daemon's
environment.

Mint each agent its own credential (as in Get Started) so every write is
attributed to a token, and for stronger isolation prefer a split
environment: the daemon runs in its own environment, and the agent's
environment installs **only** the slim client — no runtime, no direct
access to state files:

```bash
pip install cruxible-client   # agent environment only; ~2 dependencies
```

- `CRUXIBLE_REQUIRE_SERVER=1` keeps the agent on the daemon path.
- `CRUXIBLE_SERVER_STATE_DIR` lives outside the agent's writable workspace.

MCP example:

```json
{
  "mcpServers": {
    "cruxible": {
      "command": "cruxible-mcp",
      "env": {
        "CRUXIBLE_MODE": "governed_write",
        "CRUXIBLE_SERVER_URL": "http://127.0.0.1:8100",
        "CRUXIBLE_SERVER_BEARER_TOKEN": "<agent-token>"
      }
    }
  }
}
```

`CRUXIBLE_MODE` selects one of four cumulative permission tiers —
`read_only`, `governed_write`, `graph_write`, `admin` — and denied calls name
the tier they need. Give an agent the lowest tier that does its job:
`governed_write` (above) can run workflows, propose, and record feedback,
but cannot mutate the raw graph or resolve proposals.

Local permission modes are a practical hardening layer, not full sandboxing. If
trust levels matter, keep the daemon state outside the agent workspace and
expose only the client, HTTP, or MCP surface. See
[Isolated Deployment](https://github.com/cruxible-ai/cruxible/blob/main/docs/isolated-deployment.md).

## Documentation

**Getting started**
- [Quickstart](https://github.com/cruxible-ai/cruxible/blob/main/docs/quickstart.md) — install to first query
- [Concepts](https://github.com/cruxible-ai/cruxible/blob/main/docs/concepts.md) — architecture and primitives

**Modeling and authoring**
- [Modeling State](https://github.com/cruxible-ai/cruxible/blob/main/docs/modeling-state.md) — designing an ontology (entities, relationships, gates vs flags)
- [Config Reference](https://github.com/cruxible-ai/cruxible/blob/main/docs/config-reference.md) — the YAML config schema
- [Kit Authoring](https://github.com/cruxible-ai/cruxible/blob/main/docs/kit-authoring.md) — kit manifest, structure, and packaging
- [Kit Walkthroughs](https://github.com/cruxible-ai/cruxible/blob/main/docs/kit-walkthroughs.md) — building standalone and overlay kits
- [Common Providers And Dataflow Steps](https://github.com/cruxible-ai/cruxible/blob/main/docs/common-providers.md) — provider and workflow building blocks

**Reference**
- [CLI Reference](https://github.com/cruxible-ai/cruxible/blob/main/docs/cli-reference.md) — terminal commands
- [MCP Tools Reference](https://github.com/cruxible-ai/cruxible/blob/main/docs/mcp-tools.md) — agent tool surface
- [AI Agent Guide](https://github.com/cruxible-ai/cruxible/blob/main/docs/for-ai-agents.md) — orchestration patterns

**Operating and deploying**
- [Inspection UI](https://github.com/cruxible-ai/cruxible-app) — the read-only console in the screenshots above: state graph, review groups, workflows, traces, receipts
- [Local State And Backups](https://github.com/cruxible-ai/cruxible/blob/main/docs/local-state-and-backups.md) — SQLite, daemon state, and portability
- [Runtime Auth And Agent Roles](https://github.com/cruxible-ai/cruxible/blob/main/docs/runtime-auth-and-agent-roles.md) — credentials, permission tiers, and bootstrap
- [State Resolution And Maintenance](https://github.com/cruxible-ai/cruxible/blob/main/docs/state-resolution-and-maintenance.md) — proposal resolution, trust grading, and maintenance signals
- [Publishing And Subscribing To States](https://github.com/cruxible-ai/cruxible/blob/main/docs/publishing-states.md) — build, publish, and track reference state releases
- [Isolated Deployment](https://github.com/cruxible-ai/cruxible/blob/main/docs/isolated-deployment.md) — running the daemon with only the client/MCP surface exposed
- [Hosted Runtime Image](https://github.com/cruxible-ai/cruxible/blob/main/docs/hosted-runtime-image.md) — the runtime container image

**Guides**
- [KEV Guide](https://github.com/cruxible-ai/cruxible/blob/main/docs/kev-guide.md) — subscribe to the vulnerability reference, judge your exposures, work the queue

**Agent skills** ([`skills/`](https://github.com/cruxible-ai/cruxible/tree/main/skills))
- [prepare-data](https://github.com/cruxible-ai/cruxible/tree/main/skills/prepare-data) — profile and ready raw exports before modeling
- [create-state](https://github.com/cruxible-ai/cruxible/tree/main/skills/create-state) — staged graph, workflow, query, and review-loop design from your data
- [review-state](https://github.com/cruxible-ai/cruxible/tree/main/skills/review-state) — audit and harden a drafted state model
- [overlay-and-fit](https://github.com/cruxible-ai/cruxible/tree/main/skills/overlay-and-fit) — compose and adapt overlay kits
- [wiki-to-state](https://github.com/cruxible-ai/cruxible/tree/main/skills/wiki-to-state) — convert a CLAUDE.md pile or Obsidian vault into governed state
- [classification-at-scale](https://github.com/cruxible-ai/cruxible/tree/main/skills/classification-at-scale) — classify a catalog against a taxonomy with signals, batch review, and a trust flywheel

Kit-specific skills ship inside their kits (e.g. `kev-start` and `kev-triage`
in kev-triage, `review-thread` in agent-operation).

## Technology

Cruxible uses [Pydantic](https://docs.pydantic.dev/) for validation,
[NetworkX](https://networkx.org/) for in-memory graph operations,
[Polars](https://pola.rs/) for data operations, [SQLite](https://sqlite.org/)
for local durable state, [FastAPI](https://fastapi.tiangolo.com/) for the daemon,
and [FastMCP](https://github.com/jlowin/fastmcp) for MCP tools.

## License

Apache 2.0

<!-- mcp-name: io.github.cruxible-ai/cruxible-core -->
