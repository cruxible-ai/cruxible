# Cruxible Deep Dive: A Governed Domain End To End

This is the long-form companion to the
[README](https://github.com/cruxible-ai/cruxible#readme): one governed
truth built end to end in the supply-chain world, followed by the
governance model, the multi-writer rules, the workflow machinery, and how
domain state composes with agent operating state. To run everything here
against the seeded world, follow the
[Supply Chain Guide](supply-chain-guide.md).

## The Loop

1. **Model the domain.** Start from a [kit](https://cruxible.ai/kits), or
   hand the [authoring skills](https://github.com/cruxible-ai/cruxible/tree/main/skills)
   to your agent with your data: it drafts the YAML ontology, you review
   what it proposes.
2. **Pin the sources.** The exports, tables, and documents your truth
   comes from register as content-hashed artifacts; claims cite into them.
3. **Ingest the hard facts.** Deterministic ingest pipelines match
   source rows into typed entities and edges, previewed before they
   commit.
4. **Propose the judgment calls.** Claim types you declared governed can't
   be written directly, not even by an ingest pipeline; they're proposed into
   review groups carrying the evidence that matched them.
5. **Review and mint, only where you opted in.** Governed claims land in
   review: a human, or an agent under trust rules you declared, approves
   or rejects. Everything else is live the moment it's written, and
   approving or correcting it is one verb, straight from a query result.
6. **Ask, and act on the answer.** Agents work through MCP or the CLI at
   the permission tier you give them; queries return answers with receipts;
   guards refuse writes that break the rules, and gates hold outside
   actions (a merge, a deploy) until state agrees.

## What A Governed Domain Looks Like

Where this section ends up: one reviewed judgment — *this incident impacts
this supplier* — and from it, a query walks a recursive bill of materials
to name every exposed shipment, five typed hops downstream, with a receipt.
Here is how that truth gets built. A minimal slice of a supply-chain
ontology, as authored in a kit config:

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

`incident_impacts_supplier` is a judgment call, so it is governed: every
live direct write is refused — CLI, MCP, batch, at any permission tier
(a direct write can at most *stage* the edge for review). It
enters only through the governed verbs the config declares, and in this
domain that is proposal and review. The incident feed's workflow records
the incidents themselves as hard facts, but the impact edges it can
only *propose*. Those candidates land in a review group, each carrying the
signals and evidence that matched it:

```bash
cruxible propose --workflow propose_incident_impacts_supplier --input-file ./exports/incidents.json
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

Notice where the judgment went: exactly one edge — *does this incident
materially impact this supplier?* — passed through review. Everything
downstream is computation over settled truth: which components those
suppliers supply is ingested fact, so the blast radius is a traversal
across the two, not another judgment call. An agent (or app) asks the
expensive question without re-deciding anything:

```bash
cruxible query run components_exposed_by_incident \
  --param incident_id=INC-42 \
  --json
```

Results come back carrying a receipt id — the receipt is the deterministic
path from query parameters to traversed edges to returned rows:

```json
{
  "items": [
    { "entity_type": "Component", "entity_id": "component-main-board" }
  ],
  "receipt_id": "RCP-2f61a90c84d3"
}
```

```bash
cruxible explain --receipt RCP-2f61a90c84d3 --format json
```

```json
{
  "query_name": "components_exposed_by_incident",
  "parameters": { "incident_id": "INC-42" },
  "nodes": [
    { "node_type": "query", "detail": { "entry_point": "Incident" } },
    { "node_type": "edge_traversal", "relationship": "incident_impacts_supplier" },
    { "node_type": "edge_traversal", "relationship": "supplier_supplies_component" },
    { "node_type": "result", "entity_type": "Component", "entity_id": "component-main-board" }
  ]
}
```

Receipts are not logs — they are typed evidence graphs. Mutation receipts
record exactly what a write changed, and governed edges carry a reference back
to the receipt of the operation that created them. A receipt doesn't prove a
claim is *true* — it records what state, evidence, and rules produced the
result; the same query against the same state reproduces it.

And the cascade keeps going, with a sharp line through it: what gets
judged, and what flows. In the full kit, impact judgments stop at
components and assemblies; product and shipment exposure are never judged
at all. `incident_exposed_shipments` derives them by traversal — walking
the accepted impacts up a recursive bill of materials (assemblies nest
eight levels deep), into the finished products, out to the shipments
carrying them. Downstream truth is computed from upstream judgment, never
asserted alongside it. Overturn one impact edge in review and every
product and shipment answer downstream moves with it, on the next query,
for free.

To run this end to end on the seeded world — the staged cascade, the
review seats, the receipted blast radius — follow the
[supply chain guide](https://github.com/cruxible-ai/cruxible/blob/main/docs/supply-chain-guide.md).

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
| **Governed proposal** | Judgment calls: uncertain or interpretive relationships | Pending candidates land in a review group under one thesis, each carrying its matching evidence; the group resolves through declared policy, human approval, or an agent with the permission to judge — acceptance mints attributed state, rejection records why |

Guards are declared in config and enforced at a single write chokepoint.
A relationship type can refuse direct writes entirely; a work item can be
blocked from closing until an approved review is linked; a write can be
required to co-create a linked entity in the same unit of work; a claim can be
required to carry source evidence. Evidence requirements are enforced, not
decorative: the write is refused unless every reference dereferences to a
registered source chunk whose content hash matches.

The agent-operation kit ships these live: a work item cannot close without
an approved review linked, and a review verdict must co-write its rationale
note in the same unit of work, so the work itself is typed state whose
guards enforce review. Each kit README renders its declared guards as a
generated table
([agent-operation's](https://github.com/cruxible-ai/cruxible/tree/main/kits/agent-operation/)).

## One Truth, Many Writers

Cruxible is built for many writers out of the box. Every writer,
human or agent, acts under its own minted credential at one of four
cumulative permission tiers (`read_only` ⊂ `governed_write` ⊂ `graph_write`
⊂ `admin`); give each agent the least tier that does its job. Every write
is attributed to the actor that made it, and roles can be separated by
receipt: a guard can require that the actor who created a record is never
the one to approve it, anchored on the creation receipt rather than
anything a writer can forge.

Humans and agents sit in the same loops under the same rules. An agent
with the permission to judge resolves review groups exactly as you would,
and its approvals are attributed exactly like yours — while every reader,
whatever the agent, model, or session, computes the same answer from the
same accepted state. Wiring and hardening: [Agent Setup](#agent-setup).

## Workflows And Pinned Providers

Workflows orchestrate reads, providers, shaping, and writes as one declared,
reproducible procedure. Providers — deterministic transforms and data
loaders in Python, over HTTP, or as commands — are pinned, not trusted: the
kit lockfile (`cruxible.lock.yaml`) records each one's version, content
digest, and declared side effects, and every call leaves an execution trace.

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

**Domain state** is the durable model of the world an agent reasons about —
assets, vulnerabilities, suppliers, cases, controls, risks — answering what
is true, proposed, reviewed, or constrained:

<p align="center">
  <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/ui_state_graph.png" alt="Cruxible state graph: a supply-chain domain of 780 entities and 1,734 edges, dots colored by entity type, with edge strokes carrying governance review state" width="900">
</p>

**Agent operating state** is the coordination layer for the work itself —
work items, review requests, decisions, risks, open questions, actors —
tracking what's active or blocked, why, who reviewed it, and what changed.
A domain kit models the thing being worked on; an operating-state kit
tracks the work around it; typed edges compose them into one queryable
graph. The type map of the composed supply-chain instance above:

<p align="center">
  <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/ui_type_map.png" alt="Cruxible type map of a composed supply-chain instance: base agent-operation types and violet domain overlay types, with labeled relationship types carrying live edge counts" width="900">
</p>

## State That Compounds

The one real cost is the config — the types, rules, and queries that model
your domain. You don't write it from scratch: point an agent at your data,
or an existing wiki, and it drafts the model; you review
what it proposes instead of authoring it. The rules are few, static, and
reviewed once; the writes they govern are many and continuous — that
asymmetry is the point. And the cost keeps paying: knowledge no longer gets
wiped out by a context refresh, a model swap, or a handoff, and three loops
keep the state current through the same governed work that uses it:

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

You can swap vendors, upgrade models, or run several at once; the state,
evidence, review history, feedback, outcomes, and the ontology itself
accumulate in a database you own, portable down to a single file, not in
a vendor's weights or a platform's memory. The work agents do becomes
your asset.
