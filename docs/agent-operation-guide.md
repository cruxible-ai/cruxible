# Agent Operation: Hard State For The Work Itself

The agent-operation kit is the operations layer Cruxible runs its own
development on: work items, review requests, decisions, risks, open questions,
and dated state notes as one typed, receipted graph. It is the optional layer
for teams running agents on the loop — domain state stays in its own kit; this
kit holds the work *about* it. Its discipline lives in config, which is the
point: a rule declared there is an invariant; an instruction followed by a
model is a probability.

## 1. Initialize, with identities on

The kit's review guards are actor-anchored (an approval must come from an
authorized reviewer who is not the review's creator), so run the daemon with
auth on: each credential is a distinct minted identity rather than one shared
operator.

```bash
pip install cruxible
CRUXIBLE_SERVER_AUTH=true \
CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=change-me-once \
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/ops" cruxible server start   # shell 1

# shell 2 — init with the bootstrap secret as bearer, then claim the admin credential
CRUXIBLE_SERVER_BEARER_TOKEN=change-me-once \
  cruxible --server-url http://127.0.0.1:8100 init --kit agent-operation --bootstrap

CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=change-me-once cruxible credential claim-bootstrap
export CRUXIBLE_SERVER_BEARER_TOKEN=<printed-admin-token>
```

Typical use composes it with a domain: agent-operation is a standalone base
kit, and domain overlays (e.g. project-domain's roadmap/release/milestone
layer) compose on top, adding seam relationships out to domain entities. To
compose, replace the initialization command above with:

```bash
CRUXIBLE_SERVER_BEARER_TOKEN=change-me-once \
  cruxible --server-url http://127.0.0.1:8100 init \
  --kit agent-operation --kit project-domain --bootstrap
```

Actors are mint-only — the graph refuses direct `Actor` writes; identities
materialize from credential mints.

## 2. Open work

```bash
cruxible entity add WorkItem wi-cli-retry \
  --set title="Fix flaky CLI retry test" --set type=bug \
  --set status=active --set priority=high
cruxible query run work_queue --json
```

`status` is a lifecycle (`planned`, `active`, `blocked`, `watching`,
`deferred`, `closed`); `type` is one of `feature`, `bug`, `cleanup`,
`research`, `docs`, `test`, `infrastructure`, `operations`. `work_queue` is the
pull queue for an agentic loop; `work_item_context` fans out from one item to
its owner, reviews, dependencies, blockers, lineage, and domain seam edges.

## 3. Gate completion on review

```bash
cruxible entity add ReviewRequest rr-cli-retry \
  --set title="Review the retry fix" --set status=requested
cruxible relationship add review_request_for_work_item \
  ReviewRequest rr-cli-retry WorkItem wi-cli-retry
```

Closing the work item now fails — `work_item_closed_requires_approved_review`
rejects the transition until an approved review exists:

```bash
cruxible entity update WorkItem wi-cli-retry --set status=closed
# Error: Mutation guard 'work_item_closed_requires_approved_review' rejected ...
```

## 4. The verdict, as a separate identity

Approval is actor-guarded: only the `authorized-reviewer` actor may set
`status: approved`, and the approver must differ from the review's creator
(compared against the creation receipt, not anything a writer can claim).
Mint that identity — the mint is what materializes the Actor:

```bash
cruxible credential mint --label authorized-reviewer --mode graph_write
```

A verdict is one atomic batch: the status transition must co-write a
`StateNote(kind=review_note)` in the same write — status cannot advance
without recording why. `verdict.yaml`:

```yaml
entities:
  - entity_type: ReviewRequest
    entity_id: rr-cli-retry
    properties:
      status: approved
  - entity_type: StateNote
    entity_id: sn-rr-cli-retry-01
    properties:
      kind: review_note
      title: "Approved rr-cli-retry: retry fix verified"
      summary: "Verdict rationale for rr-cli-retry."
      body: >-
        Reproduced the flake, confirmed the fix holds across 50 runs, and the
        regression test pins the schedule that used to race.
      created_at: "2026-07-13T12:00:00Z"
relationships:
  - from_type: StateNote
    from_id: sn-rr-cli-retry-01
    relationship_type: state_note_about_review_request
    to_type: ReviewRequest
    to_id: rr-cli-retry
  - from_type: StateNote
    from_id: sn-rr-cli-retry-01
    relationship_type: state_note_authored_by_actor
    to_type: Actor
    to_id: authorized-reviewer
```

Record it under the reviewer's token, then close under the working one:

```bash
export CRUXIBLE_SERVER_BEARER_TOKEN=<reviewer-token>
cruxible batch-direct-write --payload-file verdict.yaml

export CRUXIBLE_SERVER_BEARER_TOKEN=<admin-token>
cruxible entity update WorkItem wi-cli-retry --set status=closed
```

Two more invariants ride along: once approved, `change_head` — the reviewed
SHA — is frozen, and the kit's `skills/review-thread` skill gives an agent
this exact payload shape plus the append-only thread discipline (findings are
resolved or superseded by later notes, never edited).

## 5. Propose judgments, resolve them in review

Interpretive claims — dependencies, blockers, mitigations, supersessions,
decision impacts — are proposal-only: live direct writes are refused, every
proposal carries a basis and source evidence, and a reviewer resolves it:

```bash
cruxible group propose --relationship work_item_depends_on_work_item \
  --thesis "wi-a consumes the API wi-b ships" \
  --members '[{"relationship_type": "work_item_depends_on_work_item",
    "from_type": "WorkItem", "from_id": "wi-a",
    "to_type": "WorkItem", "to_id": "wi-b",
    "properties": {"dependency_basis": "wi-b ships the API wi-a consumes"},
    "signals": [{"signal_source": "source_evidence", "signal": "support",
      "evidence": "docs/plan.md names the dependency"}]}]'

cruxible group list --status pending_review
cruxible group resolve --group <group-id> --action approve \
  --rationale "Dependency verified against the plan" \
  --expected-pending-version 1
```

## 6. Run the loop from queries

The read surfaces are the coordination protocol: `review_queue` and
`changes_requested_reviews` split the two sides of review; `blocked_work_items`,
`active_risks`, `open_questions_needing_review`, and `proposed_decisions` are
the standing attention lists; `state_notes_for_review_request` replays a review
thread; `work_item_scratchpad` replays an implementer's own working notes to
pick a task back up.

```bash
cruxible query run review_queue --json
cruxible query run blocked_work_items --json
```

## Holding the repo to the state

The kit declares one repo gate: `merge-review` passes only when every tip
merged into `main` is pinned by an approved ReviewRequest via `change_head`.
Wire it into a git pre-push hook:

```bash
exec cruxible gate check merge-review
```

Guards block writes into state; the gate lets the repo act only when state
agrees — the frozen `change_head` keeps each approval pinned to the exact
commit that was reviewed.
