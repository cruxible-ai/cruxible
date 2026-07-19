<p align="center">
  <a href="https://cruxible.ai">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-white.svg">
      <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-black.svg" alt="Cruxible" width="360">
    </picture>
  </a>
</p>

# Cruxible

[![PyPI version](https://img.shields.io/pypi/v/cruxible?color=blue)](https://pypi.org/project/cruxible/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green)](https://github.com/cruxible-ai/cruxible/blob/main/LICENSE)

<p align="center">
  <a href="https://cruxible.ai">cruxible.ai</a> ·
  <a href="https://github.com/cruxible-ai/cruxible/blob/main/docs/quickstart.md">quickstart</a> ·
  <a href="https://docs.cruxible.ai">docs</a> ·
  <a href="https://cruxible.ai/kits">kits</a> ·
  <a href="https://cruxible.ai/skills">skills</a>
</p>

**Cruxible is a governed state engine for AI agents.** It turns a YAML
ontology into a typed knowledge graph with write rules enforced outside
the model.

Declare the model and its rules:

```yaml
entity_types:
  Incident:
    id: incident_id
    properties:
      title: string indexed
  Supplier:
    id: supplier_id
    properties:
      name: string indexed

relationships:
  - incident_impacts_supplier: Incident -> Supplier
    write_policy: proposal_only        # judgment call: enters only through review

named_queries:
  incident_impacted_suppliers:
    mode: traversal
    entry_point: Incident
    returns: Supplier
    traversal:
      - relationship: incident_impacts_supplier
        direction: outgoing
```

Agents write; the rules run, not the prompt. The refused write, the
review that admits the judgment, and the receipted answer:

```diff
  $ cruxible relationship add incident_impacts_supplier \
      Incident INC-TW-RAIL-2026-07 Supplier S-CN-DG-HARNESS
- Error: DirectWriteRefusedError: Direct write to relationship
- 'incident_impacts_supplier' is refused (write_policy=proposal_only).
- Use 'group propose' to stage a governed proposal. (receipt: RCP-…)

  $ cruxible propose --workflow propose_incident_impacts_supplier
  $ cruxible group resolve --group <GRP-id> --action approve \
      --rationale "Confirmed against supplier geography"

  $ cruxible query run incident_impacted_suppliers \
      --param incident_id=INC-TW-RAIL-2026-07 --json
+ { "items": [ { "entity_type": "Supplier", "entity_id": "S-CN-DG-HARNESS" } ],
+   "receipt_id": "RCP-2f61a90c84d3" }
```

This repository gates its own releases with Cruxible: a push to main is
refused until state pins an approved review
([how](#the-rules-run)).

> `pip install cruxible` — the
> [Quickstart](https://github.com/cruxible-ai/cruxible/blob/main/docs/quickstart.md)
> goes install to first query; [Get Started](#get-started) below runs the
> seeded demo world in ~3 minutes, with no model calls or API keys.

No LLM inside the engine: Cruxible is a Python daemon with a CLI and MCP
server, with state in a SQLite file you own. We call the result **hard state**:
knowledge the rest of your stack can act on without re-checking it. If
your team's knowledge currently lives in markdown or a vector store,
[the comparison section](#why-not-markdown-rag-or-vector-memory) explains
exactly what changes.

Start permissive, then tighten claim types where being wrong is expensive,
one `write_policy` line at a time. Authoring skills can draft the ontology
from your data; you review and refine it as the domain teaches you what
matters. The model gets stricter where experience proves it should,
without an ontology team or a rewrite.

## Capabilities

<p align="center">
  <a href="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/cruxible_architecture.svg">
    <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/cruxible_architecture.svg" alt="Cruxible architecture: source systems are pinned as artifacts, workflows propose row-matched claims into domain state, the agent operation layer reviews and mints them, and reads come back as deterministic queries with receipts" width="1000">
  </a>
</p>

<details>
<summary><b>Model</b> — the ontology is a versioned YAML config, drafted by your agent</summary>

- Entity types, relationships, enums, and queries in one
  [Terraform-like config](https://github.com/cruxible-ai/cruxible/blob/main/docs/config-reference.md); kits package a model
  with its rules and procedures, [overlay kits](https://github.com/cruxible-ai/cruxible/blob/main/docs/kit-authoring.md)
  compose over an upstream base
- [Authoring skills](https://github.com/cruxible-ai/cruxible/tree/main/skills) draft the model from your
  exports or an existing wiki; you review instead of author
  ([Modeling State](https://github.com/cruxible-ai/cruxible/blob/main/docs/modeling-state.md))
</details>

<details>
<summary><b>Govern</b> — writes are validated, refused, or reviewed; nothing is silent</summary>

- Per-type `write_policy`: direct, proposal-only, or mint-only; guards
  enforce evidence, transitions, and co-writes at one chokepoint
  ([Concepts](https://github.com/cruxible-ai/cruxible/blob/main/docs/concepts.md))
- Judgment calls land in [review groups](https://github.com/cruxible-ai/cruxible/blob/main/docs/state-resolution-and-maintenance.md)
  carrying their matching evidence; approval mints attributed state with
  rationale on record
- Four cumulative permission tiers per credential; a guard can require the
  approving actor differs from the creating actor, anchored on receipts
  ([Auth And Agent Roles](https://github.com/cruxible-ai/cruxible/blob/main/docs/runtime-auth-and-agent-roles.md))
</details>

<details>
<summary><b>Ingest</b> — deterministic, preview-first, pinned</summary>

- Sources register as content-hashed artifacts; claims cite into them
- Pipelines preview against a clone, then `apply` re-verifies digests
  before committing; providers are version- and digest-pinned with
  execution traces ([Providers And Dataflow](https://github.com/cruxible-ai/cruxible/blob/main/docs/common-providers.md))
</details>

<details>
<summary><b>Ask</b> — receipted answers, built for agent read budgets</summary>

- Named traversal queries (blast radius, downstream impact) with a
  receipt you can `explain`; the same state returns the same answer for
  every agent
- Compact/standard profiles, bounded neighborhood reads, and a graph
  layout cut cold-start read cost by 86% on
  [our benchmark](https://github.com/cruxible-ai/cruxible/tree/main/benchmarks/read_anchor); every read
  carries a monotonic revision and truncation is always explicit
</details>

<details>
<summary><b>Operate</b> — one daemon, many doors, state you own</summary>

- [MCP server, CLI, and Python client](https://github.com/cruxible-ai/cruxible/blob/main/docs/for-ai-agents.md) against the
  same daemon, credentials, and tiers; agent setup is one
  [MCP config block](https://github.com/cruxible-ai/cruxible/blob/main/docs/quickstart.md)
- Gates hold outside actions (a merge, a deploy) until state agrees; the
  first shipped kind wires into `git` pre-push
- Snapshots, [backups](https://github.com/cruxible-ai/cruxible/blob/main/docs/local-state-and-backups.md), state
  [publishing](https://github.com/cruxible-ai/cruxible/blob/main/docs/publishing-states.md), and an
  [inspection UI](https://github.com/cruxible-ai/cruxible-app)
  over a SQLite file, portable as one artifact
  ([Isolated Deployment](https://github.com/cruxible-ai/cruxible/blob/main/docs/isolated-deployment.md))
</details>

## Get Started

```bash
pip install cruxible
```

Model your own domain with the
[authoring skills](https://github.com/cruxible-ai/cruxible/tree/main/skills)
and [Modeling State](https://github.com/cruxible-ai/cruxible/blob/main/docs/modeling-state.md),
or run the demo — a seeded supply-chain world, ~3 minutes, with no model
calls or API keys. Sandbox writes attribute to a built-in `operator`
identity:

```bash
# shell 1 — local sandbox daemon
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/sandbox" cruxible server start

# shell 2 — kit bundles are fetched from the release and digest-verified
# (agent-operation is the optional agent-ops layer; domain-only works too)
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
## The Rules Run

Cruxible never asks a model to follow the rules, because the rules run as
code. A rule declared in config runs at the write chokepoint on every
mutation, and there is no code path around the chokepoint.

| Prompted | Enforced |
|---|---|
| "The agent knows an exposure can't be closed while unremediated" | The write chokepoint refuses the transition until the remediation claim, with its evidence, is linked |
| "The model says these sources support the claim" | The write is refused unless every reference dereferences to a content-hash-verified source chunk |
| "The agent was told not to accept claims it proposed itself" | The guard compares the acting actor against the creation receipt's recorded actor and refuses, including create-as-accepted |
| "The agent remembers the ingest procedure" | The workflow is declared, previewed, and locked to pinned providers; every run leaves a receipt |

Guards face inward (the write boundary of accepted state); gates face
outward (an external action holds until state agrees).

<details>
<summary>A declared gate, wired into git pre-push</summary>

```yaml
gates:
  merge-review:
    kind: git-pre-push
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
    adapter: {branch_pattern: refs/heads/main}
```
</details>

This repository runs on that gate: it refused our own 0.2.2 release push
until the review record was corrected in state. We fixed the state, not
the hook.

## The Full Walkthrough

The [deep dive](https://github.com/cruxible-ai/cruxible/blob/main/docs/deep-dive.md) builds one governed truth end to end: a single
reviewed judgment lets a query walk a recursive bill of materials and
name every exposed shipment five typed hops downstream, with a receipt
you can `explain`.

<p align="center">
  <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/ui_group_review.png" alt="Cruxible review group page: signal matrix, proposed edges each carrying matching evidence, and a provenance rail with workflow, receipts, and provider traces" width="900">
</p>

The screenshot is the review seat in the
[inspection UI](https://github.com/cruxible-ai/cruxible-app): each
proposed edge carries the evidence that matched it, with a provenance
rail back to workflows, receipts, and traces.

## Why Not Markdown, RAG, Or Vector Memory?

We spent fifty years keeping the facts that matter in systems with schemas,
constraints, and transactions — then handed agents piles of markdown,
because prose was the only interface they spoke. What changes:

| Markdown · RAG · vector memory | Cruxible |
|---|---|
| A claim is prose; nothing refuses one without a source | Evidence-gated writes refuse references that don't verify against content-hashed sources |
| Edits are reviewed as diffs, not claims | Writes pass typed validation, guards, and review |
| Links live in prose, re-inferred on every read | Typed edges, traversed multi-hop, visibility rules at every hop |
| A rollup is a one-off summary | Counts and joins are deterministic, receipted, re-runnable |
| No record of which answer was settled on | One accepted state; the same query returns the same answer for every agent |
| No record of when a source was captured | Sources are dated and hashed; staleness is queryable |
| A correction is just more text | Feedback attaches to the exact claim; claims mature from proposed to accepted |
| A better model reads the pile better | It can't read what was never written; the record and its derivations don't move when you swap models |

If the wiki already exists (a team wiki, an Obsidian vault), the
[`wiki-to-state`](https://github.com/cruxible-ai/cruxible/tree/main/skills/wiki-to-state)
skill converts it: pages become pinned evidence, an agent proposes the
typed claims, you review what gets minted.

## Kits

A kit packages an ontology with its governance, queries, workflows, and
providers as one versioned, composable unit; per-kit
[guides](https://github.com/cruxible-ai/cruxible/tree/main/docs) run each
end to end.

| Kit | Kind | What it models |
|-----|------|----------------|
| [agent-operation](https://github.com/cruxible-ai/cruxible/tree/main/kits/agent-operation/) | Agent operating state | Work items, review requests, decisions, risks, open questions, state notes, actors, lifecycle, and dependency context. |
| [project-domain](https://github.com/cruxible-ai/cruxible/tree/main/kits/project-domain/) | Domain overlay state | Roadmap items, milestones, release lines, and product areas composed over the agent-operation base — the project state Cruxible itself runs on. |
| [agent-release](https://github.com/cruxible-ai/cruxible/tree/main/kits/agent-release/) | Domain overlay state | Agent systems, versions, eval suites and runs, with governed certification and promotion gates. |
| [kev-reference](https://github.com/cruxible-ai/cruxible/tree/main/kits/kev-reference/) | Domain reference state | Public known-exploited vulnerability reference data. Consumed as a published state release (`state create-overlay`); init the kit itself only to build offline or publish your own. |
| [kev-triage](https://github.com/cruxible-ai/cruxible/tree/main/kits/kev-triage/) | Domain overlay state | Local asset exposure, service impact, controls, incidents, findings, remediation, and governed vulnerability triage. |
| [supply-chain-blast-radius](https://github.com/cruxible-ai/cruxible/tree/main/kits/supply-chain-blast-radius/) | Domain state | Suppliers, components, assemblies, products, shipments, and incident blast radius. |
| [case-law-monitoring](https://github.com/cruxible-ai/cruxible/tree/main/kits/case-law-monitoring/) | Domain state | Matter-centered case-law monitoring and authority impact. |

## Documentation

- [Quickstart](https://github.com/cruxible-ai/cruxible/blob/main/docs/quickstart.md) — install to first query
- [Concepts](https://github.com/cruxible-ai/cruxible/blob/main/docs/concepts.md) — architecture and primitives
- [Deep Dive](https://github.com/cruxible-ai/cruxible/blob/main/docs/deep-dive.md) — a governed domain end to end
- [Modeling State](https://github.com/cruxible-ai/cruxible/blob/main/docs/modeling-state.md) — designing an ontology
- [Config Reference](https://github.com/cruxible-ai/cruxible/blob/main/docs/config-reference.md) — the YAML config schema
- [CLI Reference](https://github.com/cruxible-ai/cruxible/blob/main/docs/cli-reference.md) · [MCP Tools](https://github.com/cruxible-ai/cruxible/blob/main/docs/mcp-tools.md) · [AI Agent Guide](https://github.com/cruxible-ai/cruxible/blob/main/docs/for-ai-agents.md)
- [Kit guides](https://github.com/cruxible-ai/cruxible/tree/main/docs) — KEV, supply chain, case law, agent operation — plus deployment, auth, backups, and publishing, all under [`docs/`](https://github.com/cruxible-ai/cruxible/tree/main/docs); agent [skills](https://github.com/cruxible-ai/cruxible/tree/main/skills) for authoring state from your data

## Technology

Cruxible uses [Pydantic](https://docs.pydantic.dev/) for validation,
[NetworkX](https://networkx.org/) for in-memory graph operations,
[Polars](https://pola.rs/) for data operations, [SQLite](https://sqlite.org/)
for local durable state, [FastAPI](https://fastapi.tiangolo.com/) for the daemon,
and [FastMCP](https://github.com/jlowin/fastmcp) for MCP tools.

## Contributing

Contributions welcome — see
[CONTRIBUTING.md](https://github.com/cruxible-ai/cruxible/blob/main/CONTRIBUTING.md).
If governed agent state is a problem you're working on, star the repo or
open an issue with your use case.

## License

Apache 2.0

<!-- mcp-name: io.github.cruxible-ai/cruxible-core -->
