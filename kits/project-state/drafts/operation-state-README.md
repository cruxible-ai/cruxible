# Agent Operation-State Ontology — Draft

> **Status: DRAFT (2026-06-17).** First version of the operation-state ontology,
> derived from the approved design table on `wi-agent-operation-kit-lifecycle-semantics`.
> The schema additions it documents live in
> [`operation-state-additions.yaml`](./operation-state-additions.yaml) and have **not**
> been spliced into the live `kits/project-state/config.yaml`. This README is the
> human-facing design spec for that draft, not generated wiki output.

## What this is

**Agent operation state** is the agent-native *operating* layer that sits over durable
domain state. It models the work itself — work items, decisions, risks, open
questions, review requests, and the actors and lifecycle behind them — so a team of
agents can plan, review, and account for what they are doing.

It is deliberately **not** domain hard state. Operation state **links outward** to
domain entities (a work item targets a product area, a decision affects a roadmap item)
but it does **not absorb** them. (`dd-agent-operation-state-vs-domain-state`.)

In the project-state kit today the operating layer is five entity types —
`WorkItem`, `Decision`, `Risk`, `OpenQuestion`, `ReviewRequest` — distinct from
the project-domain types (`ProductArea`, `Capability`, `RoadmapItem`, `ReleaseLine`,
`Milestone`). This draft adds the relationship axes and fields that the operating layer
was missing.

## Relationship axes

A recurring failure was cramming every "these relate" into `depends_on`, which corrupts
the readiness / critical-path query with **false blockers** and makes roll-up and
provenance impossible. There are four distinct axes; the kit only had two.

| Axis | Meaning | Realized by | Status |
|------|---------|-------------|--------|
| **Sequencing** | "B before A" — prerequisite, drives critical path | `work_item_depends_on_work_item` | present |
| **Impediment** | gated by an unresolved threat/uncertainty; resolved by mitigate/answer, not by prerequisite work | `risk_blocks_work_item`, `open_question_blocks_work_item` | present |
| **Composition** | order-independent scope + roll-up (parent rolls up children) | **`work_item_part_of_work_item`** | **added (O1)** |
| **Lineage** | provenance — "A came out of B"; not a prerequisite | **`work_item_spawned_from_work_item`** (same self-edge *pattern* as `decision_supersedes_decision`, but provenance — **not** replacement) | **added (O2)** |

The two added axes are **provenance/scope only**: readiness and critical-path queries
must ignore them, so adding `part_of` / `spawned_from` edges never injects a false
blocker. (`blocks` between two work items is just the inverse of `depends_on`, not a new
axis, so it is intentionally not added.)

**Enforcement.** That invariant currently rests on the readiness queries naming exact
relationships — they enumerate only `work_item_depends_on_work_item`,
`risk_blocks_work_item`, and `open_question_blocks_work_item`, never the two new edges. A
regression test should pin that, so a future query edit can't silently pull composition or
lineage into a blocker surface. (Mechanical exclusion via a relationship-level `axis:` tag
would be a separate schema-language change — not taken here.)

**Governance.** Both new edges are **direct** operational facts (structure / provenance),
like the existing containment edges (`work_item_in_milestone`, `work_item_in_release`) —
no `proposal_policy`/basis. They are *not* interpretive judgments like `depends_on` or
`supersedes`, which do require one.

**Shape guards.** Composition is a **tree** (`part_of` warns above one parent); lineage is
**single-origin** (`spawned_from` warns above one origin); both forbid direct self-edges
(error-level constraints). Multi-hop cycles aren't expressible as constraints, so roll-up
relies on the query engine's cycle-pruned traversal to stay safe.

## Fields: where *what / detail / why* live

The operating layer had only a one-line `summary`, so "why" had no home. This draft
separates the three:

| Field | Holds | On | Required |
|-------|-------|----|----------|
| `summary` | the one-line *what* | all operating types (existing) | optional |
| `description` | long-form *detail* / notes | `WorkItem` (O4) | optional |
| `rationale` | the *why* — why this exists / why it is prioritized | `WorkItem` + `Decision` (O3) | **optional, never required** |

`description` is intentionally long-form here, distinct from the short `description` that
already exists on the domain types (`ProductArea`, `Capability`).

**Scoping is deliberate** (per review): `rationale` is on work + decisions, `description`
on work only, and the composition/lineage axes above are WorkItem-only — roll-up and
critical-path are work-item concerns, and decisions already have supersession. Extending
any of these to other operating types is a later additive step, not an oversight.

**Markdown.** No per-field presentation hint. Prose string fields render as markdown by
**default** at the read-only UI layer; the schema stays plain strings (so validation,
filtering, and named-query predicates are unaffected). Tracked by
`wi-markdown-presentation-fields`. (An earlier `presentation: markdown` per-field hint was
dropped — `PropertySchema` silently ignored it, so it added nothing.)

## Deferred from this draft: the query/workflow reference seam (O6)

Operating machinery — named queries and workflows — **stays in config** for 0.2; it is
not moved into operational state (`oq-machinery-config-vs-operational-state`, decided).
That decision also records a **typed reference seam** (a `surface_ref_*` property letting
an operating entity point at a query/workflow by name) as the 0.2 middle ground.

It is **pulled from this first ontology pass.** Its only real consumers — a "run-spec"
that wires providers + hyperparameters, and a calibration record — are post-0.2, and the
seam is additive, so it can land when they need it. Keeping it out now also drops the
draft's single highest-risk item (an unvalidated three-field reference tuple). The seam
remains the recorded plan on `oq-machinery-config-vs-operational-state`; nothing is lost.

## Lifecycle and guards (unchanged, for context)

- Statuses come from `lifecycle_status` (work/risk/question), `decision_status`, and
  `review_status`. `closed` is the **terminal** work status — agents must not invent
  "done"/"completed".
- Completion is **review-mediated**: `work_item_closed_requires_approved_review` rejects
  any write that lands `WorkItem.status=closed` until an approved `ReviewRequest` reviews
  it. Approvals are actor-gated by `review_request_approval_requires_authorized_actor`.

## Out of scope for 0.2

- **Claim / lease / listener** coordination mechanics (post-0.2, per
  `dd-mutation-guards-before-agent-queues`).
- A first-class **node for a provider's operational state** (last-run / freshness /
  owner / lease) and the `produced_by` capability link — post-0.2 overlay work. Provider
  *definitions* stay config-canonical and reachable via introspection
  (`wi-operation-state-provider-introspection`); they are never node-ified.

## How the rest of the design splits (code vs ontology)

This README + the YAML draft cover the **ontology** half. The **code/engine** half is
tracked as work items spawned from `wi-agent-operation-kit-lifecycle-semantics`:

| Item | Work item |
|------|-----------|
| Ergonomic write verbs (add/update/link/note/set-status) | `wi-operation-state-ergonomic-verbs` |
| Agent-reachable relational introspection | `wi-operation-state-agent-introspection` |
| One daemon-routed read surface (kill split-brain) | `wi-operation-state-unified-read-surface` |
| Wire-as-you-go write defaults | `wi-operation-state-wire-as-you-go` |
| Relational provider/capability introspection (code half) | `wi-operation-state-provider-introspection` |
| Operational durability (snapshot/restore, safe relocate) | `wi-instance-snapshot-restore`, `wi-instance-relocate-safe` |
| Markdown-by-default rendering at the read-only UI layer | `wi-markdown-presentation-fields` |

All of the above are **additive** — none is a 0.2 freeze gate.
