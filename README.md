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

**Cruxible is a governed state engine for AI agents.** It produces a **Crux**:
an executable artifact of your domain knowledge with a code-like lifecycle —
typed, reviewed, versioned, auditable, and tested against outcomes.

<p align="center">
  <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/crux-overview.png" alt="What agent platforms are missing — state management, reviewed reflection, shared knowledge artifacts — and the Crux that answers them: typed claims and procedures with a review lifecycle, outcome contracts and gates, versioned and receipted" width="900">
</p>

**The problem.** Code is typed, reviewed, and versioned; it executes
deterministically, and tests verify it in seconds. The judgments, observations,
reusable actions, and decisions agents produce land in prose — which doesn't
execute, has no governed path from proposed to trusted, and puts no bounds on
what gets repeated. Real decisions resolve over weeks or months, long after
anything is still tracking them. And prose can't serve as shared ground: an
organization running many agents needs them all acting on the same current
claims, rules, and reviewed ways of acting, not on each agent's own reading of
the documents.

**How it works.** You declare the domain ontology and its rules in YAML.
Reproducible pipelines turn trusted source data into typed state. When an agent
or human makes a judgment, Cruxible can require evidence and review before the
claim becomes live. When an agent learns a way of acting that works, it can
propose that too: a Procedure passes through the same review cycle as a claim
and runs only within its declared inputs, preconditions, and limits. A decision
can be required to declare, before it is accepted, what result will count as
success; the outcome contract comes due on its own schedule and records what
reality said. Queries and actions run against the same live state, and invalid
changes are refused. No LLM runs inside the engine.

**Why use it.** Every agent works against the same live claims and can invoke
the same reviewed, bounded Procedures. Invalid writes and actions are refused,
contradictions remain visible until resolved, decisions can be checked against
what happened, and computed answers, mutations, and action runs carry receipts
that explain them. Snapshots capture exact config, lock, graph state, and
procedure definitions at a revision for comparison, branching, and transfer;
backups preserve the complete audit stores for recovery. The whole artifact
outlives any single session or model.

One small example: a supplier inventory and incident feed build entities from
pinned sources. An agent concludes that an incident impacts a supplier, but the
relationship is declared a judgment call:

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
    traverse:
      - relationship: incident_impacts_supplier
        direction: outgoing
```

Now the policy is part of the runtime. The direct write is refused, review
admits the judgment, and a query computes its consequence:

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

The source facts came through deterministic ingest; the inferred edge entered
through review. Queries can now traverse its consequences, guards and gates can
act on it, and later observations can contradict it without silently rewriting
history.

<details>
<summary><b>Continue through a learned action, measured outcome, and correction</b></summary>

The complete lifecycle, abbreviated:

```text
INGEST       pinned feeds -> Incident, Supplier, Product, and Shipment state
CLAIM        incident_impacts_supplier: pending -> live (evidence + reviewer)
QUERY        incident_impacted_suppliers -> [S-CN-DG-HARNESS]

PROCEDURE    hold_exposed_shipments v1: pending -> live (config + lock pinned)
DECISION     hold exposed shipments; success = zero exposed shipments released
RUN          bounded provider calls complete -> receipt RCP-run-...
OUTCOME      pinned query at check time -> satisfied -> receipt RCP-outcome-...

OBSERVATION  new evidence says the supplier was outside the incident boundary
ATTESTATION  contradict incident_impacts_supplier (claim remains live)
REVIEW       attestation upheld; separate adjudication rejects the claim
QUERY        incident_impacted_suppliers -> []

SNAPSHOT     exact config + lock + graph state + Procedure definitions captured
```

The Procedure is immutable once accepted and can call only capabilities the
operator exported, within its reviewed preconditions and budgets. The outcome
contract is declared before the decision becomes live and pins how success will
be measured. The attestation is an append-only observation: resolving it does
not silently change the claim, so the subsequent rejection is a separate,
receipted decision. The old claim, observation, and reasoning remain available
after live query results change.

</details>

Cruxible uses the same boundary itself: this repository refuses a push to main
until state pins an approved review ([how](#the-rules-run)).

> `pip install cruxible` — the
> [Quickstart](https://github.com/cruxible-ai/cruxible/blob/main/docs/quickstart.md)
> goes install to first query; [Get Started](#get-started) below runs the
> seeded demo world in ~3 minutes, with no model calls or API keys.

## Inside a Crux

<details>
<summary><b>Model</b> — declare the world and the rules that govern it</summary>

- Entity types, relationships, enums, contracts, queries, guards, and gates live
  in one [Terraform-like config](https://github.com/cruxible-ai/cruxible/blob/main/docs/config-reference.md)
- Kits package a reusable model with its policies, workflows, and providers;
  [overlay kits](https://github.com/cruxible-ai/cruxible/blob/main/docs/kit-authoring.md)
  compose domain and operating models without copying them
- [Authoring skills](https://github.com/cruxible-ai/cruxible/tree/main/skills)
  help draft the ontology from source systems and artifacts; the operator
  reviews the contract the runtime will enforce
  ([Modeling State](https://github.com/cruxible-ai/cruxible/blob/main/docs/modeling-state.md))
</details>

<details>
<summary><b>Settle</b> — turn evidence and judgment into live operational state</summary>

- Sources remain content-hashed artifacts; claims cite exact source locations
  instead of copying the corpus into the graph
- Deterministic facts can be built through previewed, lock-pinned workflows;
  judgment enters as an evidence-backed proposal
- Per-type `write_policy` chooses direct, proposal-only, or mint-only admission;
  guards enforce evidence, transitions, and co-writes at one chokepoint
  ([Concepts](https://github.com/cruxible-ai/cruxible/blob/main/docs/concepts.md))
- Four cumulative permission tiers per credential; a guard can require the
  reviewing actor differs from the creating actor, anchored on receipts
  ([Auth And Agent Roles](https://github.com/cruxible-ai/cruxible/blob/main/docs/runtime-auth-and-agent-roles.md))
</details>

<details>
<summary><b>Compute</b> — derive exact answers from the same live state</summary>

- Named traversals compute blast radius, dependencies, eligibility, and other
  recurring answers outside the model
- Query receipts identify the state revision and graph paths used; truncation
  and pagination are explicit rather than silently incomplete
- Compact output profiles, bounded neighborhoods, and graph-shaped results keep
  agent context proportional to the question
</details>

<details>
<summary><b>Act</b> — execute learned procedures without granting arbitrary code</summary>

- Procedures are agent-proposable compositions of capabilities explicitly
  exported by the operator, with declared preconditions and execution budgets
  ([Concepts](https://github.com/cruxible-ai/cruxible/blob/main/docs/concepts.md#procedures))
- Independent acceptance pins an immutable Procedure to the reviewed config and
  lock; stale pins fail closed and revisions supersede rather than mutate it
- Every run is bounded and receipted. Guards protect state writes; gates hold
  external actions such as merges or deploys until live state permits them
</details>

<details>
<summary><b>Observe</b> — connect action back to evidence without silent self-modification</summary>

- Attestations append dated support, contradiction, or uncertainty to an exact
  claim without changing its live status; reviewers resolve the discrepancy
- Outcome contracts declare the success criterion and pinned measurement before
  a decision is accepted, then record what reality said when the check comes due
- Corrections, rejections, and supersession remain explicit governed events.
  Outcomes inform the next revision; they do not rewrite state automatically
  ([Concepts](https://github.com/cruxible-ai/cruxible/blob/main/docs/concepts.md#attestations-observation))
</details>

<details>
<summary><b>Operate</b> — one daemon, many interfaces, state you own</summary>

- [MCP server, CLI, and Python client](https://github.com/cruxible-ai/cruxible/blob/main/docs/for-ai-agents.md) against the
  same daemon, credentials, and tiers; agent setup is one
  [MCP config block](https://github.com/cruxible-ai/cruxible/blob/main/docs/quickstart.md)
- Daemon-owned Playbill Git ledgers retain accepted state and its review history
  while SQLite remains a rebuildable projection rather than authority
</details>

## Get Started

```bash
pip install cruxible
```

Build your own Crux with the
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
| "The agent learned a reusable action sequence" | A Procedure may call only exported providers, within declared preconditions and budgets; independent acceptance pins it to the reviewed config and lock, and every run leaves a receipt |

Guards face inward (the write boundary of live state); gates face
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

## Where Cruxible Fits

Cruxible does not replace source systems, documents, or retrieval. Those layers
help agents find and interpret evidence. Cruxible holds the smaller set of
commitments that the rest of the system must query consistently, enforce, or
act through.

| Layer | Job |
|---|---|
| Source systems, artifacts, and retrieval | Preserve and find the evidence |
| Agents and humans | Interpret evidence, propose claims and Procedures, review judgment |
| Cruxible | Maintain claim lifecycle, execute exact reads and bounded actions, enforce policy, and attach receipts and outcomes |

If information is only useful to read, leave it in the artifact or retrieval
layer. When agents across sessions must rely on it as current, traverse its
consequences, enforce it, or act through it, compile that knowledge into a Crux.

## Kits

A kit is a reusable template for a Crux: it packages an ontology with its
governance, queries, workflows, and providers as one versioned, composable
unit. Per-kit
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
