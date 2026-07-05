# Agent Operation Kit

Reusable Cruxible state model for coordinating agent and human work over one or
more durable domain ontologies.

Agent operation state is an agent-native orchestration overlay, not the domain
state itself. It models work, reviews, decisions, risks, open questions, actors,
state notes, lifecycle, blockers, dependencies, composition, and lineage. Domain
entities stay in their own kits. This kit links outward through `SubjectRef` or
through relationships added by composed configs.

Documents, chats, OKF bundles, plans, review reports, and transcripts are source
evidence for operation-state claims. They are not modeled as operation-state
entities.

Everything between `CRUXIBLE:BEGIN` / `CRUXIBLE:END` markers is regenerated
from `config.yaml` by `cruxible config views`; treat those blocks as code-owned
structural truth. Everything outside those marker blocks is authored explanation.

## Modeling Notes

Work relationships need separate axes:

- `work_item_depends_on_work_item` is sequencing: B before A.
- `risk_blocks_work_item` and `open_question_blocks_work_item` are impediments.
- `work_item_part_of_work_item` is composition and roll-up, not sequencing.
- `work_item_spawned_from_work_item` is lineage, not sequencing or replacement.
- `work_item_supersedes_work_item` is replacement.
- `StateNote` is dated operational commentary: correction, field note,
  rationale update, implementation note, or review note. Keep current entity
  fields concise; put durable interpretation history in linked notes.

This distinction keeps readiness and critical-path queries from treating every
related item as a blocker.

## Ontology Map

<!-- CRUXIBLE:BEGIN ontology -->
```mermaid
flowchart LR
  classDef canonicalEntity fill:#4a90d9,stroke:#2c5f8a,color:#fff
  classDef governedEntity fill:#e67e22,stroke:#a0521c,color:#fff
  classDef baseEntity fill:#e4e4e7,stroke:#a1a1aa,color:#3f3f46,stroke-dasharray: 4 3

  entity_Actor["Actor"]
  entity_Decision["Decision"]
  entity_OpenQuestion["Open Question"]
  entity_ReviewRequest["Review Request"]
  entity_Risk["Risk"]
  entity_StateNote["State Note"]
  entity_SubjectRef["Subject Ref"]
  entity_WorkItem["Work Item"]
  class entity_Actor,entity_Decision,entity_OpenQuestion,entity_ReviewRequest,entity_Risk,entity_StateNote,entity_SubjectRef,entity_WorkItem canonicalEntity

  %% Deterministic canonical relationships
  entity_Decision -- "Decision Owned By Actor" --> entity_Actor
  entity_OpenQuestion -- "Open Question Owned By Actor" --> entity_Actor
  entity_ReviewRequest -- "Review Request Assigned To Actor" --> entity_Actor
  entity_ReviewRequest -- "Review Request For Work Item" --> entity_WorkItem
  entity_ReviewRequest -- "Review Request Requested By Actor" --> entity_Actor
  entity_Risk -- "Risk Owned By Actor" --> entity_Actor
  entity_StateNote -- "State Note About Actor" --> entity_Actor
  entity_StateNote -- "State Note About Decision" --> entity_Decision
  entity_StateNote -- "State Note About Open Question" --> entity_OpenQuestion
  entity_StateNote -- "State Note About Review Request" --> entity_ReviewRequest
  entity_StateNote -- "State Note About Risk" --> entity_Risk
  entity_StateNote -- "State Note About Subject" --> entity_SubjectRef
  entity_StateNote -- "State Note About Work Item" --> entity_WorkItem
  entity_StateNote -- "State Note Authored By Actor" --> entity_Actor
  entity_StateNote -- "State Note Resolves State Note" --> entity_StateNote
  entity_StateNote -- "State Note Supersedes State Note" --> entity_StateNote
  entity_WorkItem -- "Work Item Owned By Actor" --> entity_Actor
  entity_WorkItem -- "Work Item Part Of Work Item" --> entity_WorkItem
  entity_WorkItem -- "Work Item Spawned From Work Item" --> entity_WorkItem
  entity_WorkItem -- "Work Item Targets Subject" --> entity_SubjectRef

  %% Governed proposal/review relationships
  entity_Decision -. "Decision Affects Subject" .-> entity_SubjectRef
  entity_Decision -. "Decision Answers Open Question" .-> entity_OpenQuestion
  entity_Decision -. "Decision Constrains Work Item" .-> entity_WorkItem
  entity_Decision -. "Decision Supersedes Decision" .-> entity_Decision
  entity_OpenQuestion -. "Open Question Blocks Decision" .-> entity_Decision
  entity_OpenQuestion -. "Open Question Blocks Work Item" .-> entity_WorkItem
  entity_OpenQuestion -. "Open Question Concerns Subject" .-> entity_SubjectRef
  entity_Risk -. "Risk Attaches To Subject" .-> entity_SubjectRef
  entity_Risk -. "Risk Blocks Work Item" .-> entity_WorkItem
  entity_WorkItem -. "Work Item Answers Open Question" .-> entity_OpenQuestion
  entity_WorkItem -. "Work Item Depends On Work Item" .-> entity_WorkItem
  entity_WorkItem -. "Work Item Mitigates Risk" .-> entity_Risk
  entity_WorkItem -. "Work Item Supersedes Work Item" .-> entity_WorkItem
  linkStyle 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19 stroke:#2c5f8a,stroke-width:2px
  linkStyle 20,21,22,23,24,25,26,27,28,29,30,31,32 stroke:#e74c3c,stroke-width:2px
```

**Diagram legend:** blue node = canonical entity (deterministic writes); solid edge = deterministic relationship; dotted edge = governed relationship.
<!-- CRUXIBLE:END ontology -->

## Schema Catalog

<!-- CRUXIBLE:BEGIN schema-catalog -->
| Entity | Properties | Description |
| --- | --- | --- |
| `Actor` | `actor_id: string (pk)`, `label: string?`, `kind: actor_kind?`, `status: actor_status?` | Human, agent, service account, or system actor referenced by operation state. Auth-managed: instances materialize from runtime-credential mints (identity source of truth = the credential store) and are writable by no other path, so every property is a credential-derived fact. label is the credential label; kind derives from the authenticated actor type. |
| `Decision` | `decision_id: string (pk)`, `title: string?`, `summary: string?`, `rationale: string?`, `status: decision_status?`, `decided_at: date?` | Durable operating decision with lifecycle, rationale, impact, and supersession context. |
| `OpenQuestion` | `question_id: string (pk)`, `title: string?`, `summary: string?`, `status: lifecycle_status?`, `due_date: date?` | Open question that can block work or decisions until answered. |
| `ReviewRequest` | `review_request_id: string (pk)`, `title: string?`, `status: review_status?`, `summary: string?`, `change_repo: string?`, `change_base: string?`, `change_head: string?`, `requested_at: datetime?`, `resolved_at: datetime?` | Review checkpoint that can gate completion of a work item. |
| `Risk` | `risk_id: string (pk)`, `title: string?`, `summary: string?`, `status: lifecycle_status?`, `priority: priority?` | Operational risk that can block or materially delay work. |
| `StateNote` | `note_id: string (pk)`, `kind: state_note_kind?`, `title: string?`, `summary: string?`, `body: string?`, `created_at: datetime?` | Durable dated note about operation state (corrections, field notes, rationale updates, implementation notes, review notes). Preserves evolving interpretation without turning current entity summaries into changelogs. |
| `SubjectRef` | `subject_ref_id: string (pk)`, `label: string?`, `subject_type: string?`, `subject_id: string?`, `state_ref: string?`, `summary: string?` | Lightweight reference to an external/cross-instance/not-yet-modeled subject. In a composed same-instance graph, prefer explicit typed operation-to-domain relationships over wrapping modeled domain entities in SubjectRef. |
| `WorkItem` | `work_item_id: string (pk)`, `title: string?`, `summary: string?`, `description: string?`, `rationale: string?`, `type: work_item_type?`, `status: lifecycle_status?`, `priority: priority?`, `target_date: date?` | Execution-level item an agent or human can work, review, close, defer, or supersede. |

### Enums

| Enum | Values |
| --- | --- |
| `actor_kind` | human, agent, service_account, system |
| `actor_status` | active, inactive |
| `decision_status` | proposed, accepted, rejected, deferred |
| `lifecycle_status` | planned, active, blocked, watching, deferred, closed |
| `priority` | low, medium, high, critical |
| `review_status` | requested, in_review, changes_requested, approved, withdrawn |
| `state_note_kind` | correction, field_note, rationale_update, implementation_note, review_note |
| `work_item_type` | feature, bug, cleanup, research, docs, test, infrastructure, operations |
<!-- CRUXIBLE:END schema-catalog -->

## Workflow Summary

Workflows are intentionally not included in this first pass. The first useful
workflow layer should be composed by a domain kit or local overlay once the
operation ontology has real state behind it.

<!-- CRUXIBLE:BEGIN workflow-pipeline -->
This kit declares no workflows.
<!-- CRUXIBLE:END workflow-pipeline -->

<!-- CRUXIBLE:BEGIN workflow-summary -->
This kit declares no workflows.
<!-- CRUXIBLE:END workflow-summary -->

## Provider Contracts

<!-- CRUXIBLE:BEGIN provider-contracts -->
This kit declares no providers; state is written directly by operators and agents.
<!-- CRUXIBLE:END provider-contracts -->

## Governed Relationships

Governed relationships represent interpretive operational claims that should be
evidence-backed when proposed by agents.

<!-- CRUXIBLE:BEGIN governance-table -->
| Relationship | Scope | Creation Path | Signals | Auto-resolve Gate | Review Policy | Feedback | Outcomes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Decision Affects Subject | Decision -> Subject Ref | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Decision Answers Open Question | Decision -> Open Question | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Decision Constrains Work Item | Decision -> Work Item | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Decision Supersedes Decision | Decision -> Decision | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Open Question Blocks Decision | Open Question -> Decision | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Open Question Blocks Work Item | Open Question -> Work Item | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Open Question Concerns Subject | Open Question -> Subject Ref | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Risk Attaches To Subject | Risk -> Subject Ref | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Risk Blocks Work Item | Risk -> Work Item | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Work Item Answers Open Question | Work Item -> Open Question | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Work Item Depends On Work Item | Work Item -> Work Item | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Work Item Mitigates Risk | Work Item -> Risk | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
| Work Item Supersedes Work Item | Work Item -> Work Item | Agent/manual group propose | Maintainer Judgment, Source Evidence | All Support; prior trust: Trusted Only | Trust-gated auto-resolve | - | - |
<!-- CRUXIBLE:END governance-table -->

<!-- CRUXIBLE:BEGIN mutation-guards -->
| Guard | Fires On | Refused Unless | Message |
| --- | --- | --- | --- |
| `review_request_approval_requires_authorized_actor` | `ReviewRequest.status` -> `approved` | authenticated actor in: authorized-reviewer | ReviewRequest approvals require the authenticated reviewer actor (not a writer credential or spoofed body actor). |
| `review_verdict_requires_rationale_note` | `ReviewRequest.status` -> `changes_requested, approved, withdrawn` | same write creates `StateNote` (kind=review_note) linked via `state_note_about_review_request` | A ReviewRequest verdict must co-write a new StateNote(kind=review_note) linked via state_note_about_review_request in the same write. Status can't advance without recording why. |
| `work_item_closed_requires_approved_review` | `WorkItem.status` -> `closed` | query `approved_reviews_for_work_item` returns >= 1 result(s) | Work items cannot be closed until an approved ReviewRequest reviews them. |
<!-- CRUXIBLE:END mutation-guards -->

### Signal Policy Notes

<!-- CRUXIBLE:BEGIN signal-policy-catalog -->
| Signal Source | Role | Review Unsure | Evidence on Support | Used By | Notes |
| --- | --- | --- | --- | --- | --- |
| `maintainer_judgment` | advisory | yes | no | Decision Affects Subject, Decision Answers Open Question, Decision Constrains Work Item, Decision Supersedes Decision, Open Question Blocks Decision, Open Question Blocks Work Item, Open Question Concerns Subject, Risk Attaches To Subject, Risk Blocks Work Item, Work Item Answers Open Question, Work Item Depends On Work Item, Work Item Mitigates Risk, Work Item Supersedes Work Item | - |
| `source_evidence` | required | yes | no | Decision Affects Subject, Decision Answers Open Question, Decision Constrains Work Item, Decision Supersedes Decision, Open Question Blocks Decision, Open Question Blocks Work Item, Open Question Concerns Subject, Risk Attaches To Subject, Risk Blocks Work Item, Work Item Answers Open Question, Work Item Depends On Work Item, Work Item Mitigates Risk, Work Item Supersedes Work Item | - |
<!-- CRUXIBLE:END signal-policy-catalog -->

## Query Catalog

<!-- CRUXIBLE:BEGIN query-catalog -->
### Actor

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Actor Work Queue | traversal | Work Item | reviewable | Work Item Owned By Actor (Incoming) | Work items owned by an actor with latest reviews, dependency counts, blockers, subjects. |

### Collection Query

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Active Risks | collection | Risk | live |  | Active operational risks. |
| Blocked Work Items | collection | Work Item | reviewable |  | Work items marked blocked, with risk/open-question blocker context. |
| Changes Requested Reviews | collection | Review Request | reviewable |  | Review requests sent back with changes requested -- the implementer's rework queue, distinct from the reviewer-facing review_queue. |
| Open Questions Needing Review | collection | Open Question | live |  | Planned/active open questions needing review. |
| Proposed Decisions | collection | Decision | live |  | Proposed decisions awaiting acceptance/rejection/deferral. |
| Recent State Notes | collection | State Note | reviewable |  | Recent operation-state notes, corrections, rationale/implementation/review notes. |
| Review Queue | collection | Review Request | reviewable |  | Review requests awaiting a reviewer -- requested or in review. Reviews sent back for rework live in changes_requested_reviews. |
| Superseded Decisions | collection | Decision | not-live |  | Decision retired/superseded on the canonical entity-lifecycle axis (lifecycle.status != live), gated out of live reads. Supersession is not a domain status value. |
| Superseded Work Items | collection | Work Item | not-live |  | WorkItem retired/superseded on the canonical entity-lifecycle axis (lifecycle.status != live), gated out of live reads. Supersession is not a domain status value. |

### Review Request

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| State Notes For Review Request | traversal | State Note | reviewable | State Note About Review Request (Incoming) | The review thread: verdict and finding notes attached to a review request, newest first. This is the read that replaces scrolling a notes blob. |

### State Note

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| State Note Context | traversal | Any Entity | reviewable | State Note Authored By Actor \| State Note About Work Item \| State Note About Review Request \| State Note About Decision \| State Note About Risk \| State Note About Open Question \| State Note About Subject \| State Note About Actor \| State Note Supersedes State Note \| State Note Resolves State Note (Both) | Full context for a state note (targets, author, supersession). |

### Subject Ref

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Subject Operation Context | traversal | Any Entity | reviewable | State Note About Subject \| Work Item Targets Subject \| Decision Affects Subject \| Risk Attaches To Subject \| Open Question Concerns Subject (Both) | Work, decisions, risks, open questions attached to a subject ref. |

### Work Item

| Query | Mode | Returns | State | Traversal | Purpose |
| --- | --- | --- | --- | --- | --- |
| Approved Reviews For Work Item | traversal | Review Request | live | Review Request For Work Item (Incoming) | Approved review requests for a work item. Used by the closed-transition guard. |
| State Notes For Work Item | traversal | State Note | reviewable | State Note About Work Item (Incoming) | State notes attached to a work item, newest first. |
| Work Item Context | traversal | Any Entity | reviewable | Work Item Owned By Actor \| Review Request For Work Item \| State Note About Work Item \| Work Item Depends On Work Item \| Work Item Part Of Work Item \| Work Item Spawned From Work Item \| Work Item Supersedes Work Item \| Risk Blocks Work Item \| Open Question Blocks Work Item \| Work Item Mitigates Risk \| Work Item Answers Open Question \| Decision Constrains Work Item \| Work Item Targets Subject (Both) | From a work item, inspect dependencies, blockers, reviews, composition, lineage, decisions, owner, subjects. all_adjacent expands against the final composed config, so on a composed instance this query also traverses overlay seam edges (e.g. project-domain's roadmap, release, milestone, and area relationships). |
| Work Item Lineage Context | traversal | Work Item | reviewable | Work Item Spawned From Work Item \| Work Item Supersedes Work Item (Both, depth=5) | Work item lineage/replacement context, excluding sequencing deps. |
| Work Item Rollup Context | traversal | Work Item | reviewable | Work Item Part Of Work Item (Incoming, depth=5) | Child/descendant work items under a parent. |
<!-- CRUXIBLE:END query-catalog -->

## Quality Rules

<!-- CRUXIBLE:BEGIN quality-rules -->
### Constraints

No configured constraints.

### Quality Checks

| Name | Kind | Target | Severity | Rule |
| --- | --- | --- | --- | --- |
| `decision_supersessions_have_basis` | Property | Decision Supersedes Decision.supersession_basis | Warning | Non Empty |
| `decision_work_constraints_have_type` | Property | Decision Constrains Work Item.impact_type | Warning | Required |
| `open_question_work_blockers_have_basis` | Property | Open Question Blocks Work Item.blocking_basis | Warning | Non Empty |
| `review_requests_review_work` | Cardinality | Review Request -> Review Request For Work Item (out) | Warning | min `1` |
| `risk_work_blockers_have_basis` | Property | Risk Blocks Work Item.blocking_basis | Warning | Non Empty |
| `state_note_supersessions_have_basis` | Property | State Note Supersedes State Note.supersession_basis | Warning | Non Empty |
| `state_notes_have_author` | Cardinality | State Note -> State Note Authored By Actor (out) | Warning | min `1` |
| `work_dependencies_have_basis` | Property | Work Item Depends On Work Item.dependency_basis | Warning | Non Empty |
| `work_item_part_of_single_parent` | Cardinality | Work Item -> Work Item Part Of Work Item (out) | Warning | max `1` |
| `work_item_spawned_from_single_origin` | Cardinality | Work Item -> Work Item Spawned From Work Item (out) | Warning | max `1` |
| `work_items_have_owner` | Cardinality | Work Item -> Work Item Owned By Actor (out) | Warning | min `1` |
| `work_supersessions_have_basis` | Property | Work Item Supersedes Work Item.supersession_basis | Warning | Non Empty |
<!-- CRUXIBLE:END quality-rules -->

## Learning Loops

<!-- CRUXIBLE:BEGIN learning-loops -->
### Feedback Profiles (Loop 1)

No configured feedback profiles.

### Outcome Profiles (Loop 2)

#### Resolution-Anchored

No configured resolution-anchored outcome profiles.

#### Receipt-Anchored

No configured receipt-anchored outcome profiles.
<!-- CRUXIBLE:END learning-loops -->

## Regeneration

```bash
uv run cruxible config views --config kits/agent-operation/config.yaml --update-readme kits/agent-operation/README.md
```
