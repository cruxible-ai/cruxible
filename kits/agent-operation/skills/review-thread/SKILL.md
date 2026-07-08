---
name: review-thread
description: Record review verdicts and run the StateNote review thread on an agent-operation instance — every ReviewRequest verdict is one atomic batch-direct-write that advances status and co-writes a review_note explaining why, and findings are corrected by appending notes rather than editing them.
---

# Review Thread (agent-operation)

Agent skill for recording review verdicts and maintaining the review-comment
thread on an agent-operation state instance. Read this before changing any
`ReviewRequest.status` or writing a review `StateNote`.

There is no core "review" verb. Everything here is built from the generic
graph primitives — `batch-direct-write`, entities, relationships — plus the
agent-operation kit's `StateNote`, `ReviewRequest`, and
`state_note_about_review_request` types. The kit enforces the discipline with a
single `co_write` mutation guard; this skill is how you satisfy it cleanly.

## The one rule the guard enforces

The `review_verdict_requires_rationale_note` mutation guard rejects any write
that advances a `ReviewRequest.status` to `changes_requested`, `approved`, or
`withdrawn` **unless the same write also creates a new
`StateNote(kind=review_note)` linked to that ReviewRequest via
`state_note_about_review_request`.**

A verdict therefore cannot advance state without recording why. The note is the
durable rationale; the status is just the headline.

This guard is **always-on** — it applies in every mode, governed or not. That
is deliberate and is not a weaker form of governance: unlike an
actor-credential guard (which needs a privileged identity to satisfy), this
requirement is satisfiable in any mode by simply including the note in the
batch. There is no credential to hold and no mode to be in; the cost of
satisfying it is one extra entity + two edges, which this skill makes routine.

The guard only fires on a *transition* of `status` into a verdict value.
Re-asserting the same status (e.g. writing `approved` on an already-approved
request alongside an unrelated field edit) is not a transition and does not
fire. The non-verdict statuses (`requested`, `in_review`) are not guarded.

## Recording a verdict — one atomic batch-direct-write

A verdict is **one** `batch-direct-write` unit of work, producing **one**
receipt. The single batch:

1. Updates `ReviewRequest.status` to the verdict value.
2. Creates a `StateNote{kind: review_note, ...}` carrying the rationale.
3. Creates the `state_note_about_review_request` edge (StateNote → ReviewRequest).
4. Creates the `state_note_authored_by_actor` edge (StateNote → Actor).

Steps 1–3 are what the guard checks. Step 4 satisfies the kit's
`state_notes_have_author` quality check and keeps the thread attributable; do
not skip it when actor state is available.

Write it as a payload file and apply it:

```
cruxible batch-direct-write --payload-file verdict.yaml
```

Dry-run first (`--dry-run`) when you want the guard checked without mutating;
the guard runs identically in dry-run and reports the same rejection.

### Payload shape

`verdict.yaml` for a `changes_requested` verdict on `RR-128`, authored by
actor `reviewer-agent`:

```yaml
entities:
  # 1. The verdict: status transition into a guarded value.
  - entity_type: ReviewRequest
    entity_id: RR-128
    properties:
      status: changes_requested
  # 2. The rationale note (kind MUST be review_note to satisfy the guard).
  - entity_type: StateNote
    entity_id: SN-RR-128-2026-06-22-01
    properties:
      kind: review_note
      title: "Changes requested on RR-128: unhandled withdraw path"
      summary: "Verdict rationale for RR-128."
      body: >-
        The withdraw transition is not covered by a test and the guard message
        wording drifts from the config. Requesting changes before approval.
      created_at: "2026-06-22T17:30:00Z"
relationships:
  # 3. Links the note to the reviewed request — the edge the guard requires.
  - from_type: StateNote
    from_id: SN-RR-128-2026-06-22-01
    relationship_type: state_note_about_review_request
    to_type: ReviewRequest
    to_id: RR-128
  # 4. Attribution: who recorded the verdict.
  - from_type: StateNote
    from_id: SN-RR-128-2026-06-22-01
    relationship_type: state_note_authored_by_actor
    to_type: Actor
    to_id: reviewer-agent
```

For `approved` or `withdrawn`, change only `ReviewRequest.status` and the
note's `kind`-appropriate prose; the structure is identical. All three verdict
values trigger the guard, so all three need the co-written note.

> **`approved` also needs the reviewer credential.** A separate
> `actor`-condition guard (`review_request_approval_requires_authorized_actor`)
> additionally requires the authenticated reviewer actor to set status
> `approved`. Approving therefore needs *both* the rationale note (this guard)
> and the reviewer credential (that guard). `changes_requested` and `withdrawn`
> need only the note.

### Why one batch and not two writes

If you split this into "update status" then "add note", the status update is
its own unit of work and the guard rejects it — there is no co-written note in
that write's delta. A *pre-existing* note from an earlier write does not
satisfy the guard either; the note and its linking edge must be created in the
**same** write as the transition. Keeping it atomic is also what makes the
receipt a faithful record: one receipt shows the verdict and its reason
together, and a failed guard rolls back the whole batch (the status never moves
without the note).

## The thread is append-only

Never edit a note to change what a verdict said. Notes are durable; corrections
are new notes that point back at the ones they revise. Two edges express the
two kinds of follow-up:

- **`state_note_supersedes_state_note`** (StateNote → StateNote): the later note
  *replaces* an earlier one. Use this when the earlier note was wrong or has
  been rewritten — the superseded note drops out of the current thread.
- **`state_note_resolves_state_note`** (StateNote → StateNote): the later note
  *answers* an earlier finding without erasing it. The original finding still
  stands as raised; this records that a response addressed it. Use this for the
  normal "you flagged X / here is how X was handled" exchange, where the finding
  remains part of the durable history.

Pick `resolves` when the original observation should remain visible (most
review back-and-forth); pick `supersedes` only when the earlier note should no
longer count as current.

### Reading the thread

- **Current thread** for a request = its `review_note` StateNotes with **no
  incoming `state_note_supersedes_state_note`** edge.
- **Full history** = follow the supersession chain backward from each current
  note.
- **Unresolved findings** = `review_note` StateNotes on the request with **no
  incoming `state_note_resolves_state_note`** edge. This makes "does this
  ReviewRequest still have anything unaddressed?" a plain graph query rather
  than a judgment call.

Use the kit's existing read surfaces to inspect a thread:

```
cruxible query run recent_state_notes
cruxible query run state_note_context --param note_id=<note_id>
```

`recent_state_notes` already projects `supersedes_count` and
`superseded_by_count` per note; `state_note_context` walks the note's targets,
author, and supersession links. To list the notes on one request, traverse
`state_note_about_review_request` incoming from the `ReviewRequest`.

## Appending a correction or resolution

A correction is, again, one `batch-direct-write`: create the new StateNote, link
it to the same ReviewRequest with `state_note_about_review_request`, attribute
it with `state_note_authored_by_actor`, and add either
`state_note_supersedes_state_note` or `state_note_resolves_state_note` to the
note it revises. If the correction also changes the verdict (e.g. moving from
`changes_requested` to `approved` after fixes land), include the
`ReviewRequest.status` transition in the *same* batch — the new note doubles as
that verdict's required rationale note, so one write covers both.

## When to stop and ask

Stop and ask the user (don't guess) when:

- The verdict you're about to record contradicts the current thread (e.g. you'd
  approve a request whose newest unresolved `review_note` requested changes)
  without an explaining note. Record the reasoning, don't paper over it.
- You hit the guard rejection
  (`review_verdict_requires_rationale_note ... Status can't advance without
  recording why`). That means the batch was missing the co-written
  `review_note` or its link. Add the note + `state_note_about_review_request`
  edge to the **same** batch; do not split the write or retry the bare status
  change.
- You hit `review_request_approval_requires_authorized_actor` on an `approved`
  verdict. That is the reviewer-credential guard, not this one — approving
  requires the authenticated reviewer identity. Surface it; do not try to
  spoof the actor in the request body.
- A ReviewRequest or Actor id in your payload doesn't resolve to an existing
  entity. Writing a verdict against the wrong request corrupts the thread.

## Troubleshooting

| Symptom | Check |
|---|---|
| `batch-direct-write` rejected with `review_verdict_requires_rationale_note` | The same batch is missing a new `StateNote(kind=review_note)` linked via `state_note_about_review_request`. Add the note + link to this batch; a pre-existing note does not count. |
| Guard still fires after adding a note | Confirm `kind: review_note` (not another `state_note_kind` value) and that the `state_note_about_review_request` edge points the note at the *same* ReviewRequest whose status you're changing, both created in this write. |
| `approved` rejected with `review_request_approval_requires_authorized_actor` | Approval needs the authenticated reviewer credential in addition to the note. Run under the reviewer runtime credential. |
| Verdict status didn't change but no error | You re-asserted the existing status; that isn't a transition and the guard doesn't fire. Confirm the prior status was actually different. |
| "Current thread" includes a note you corrected | A corrected note still appears until it has an incoming `state_note_supersedes_state_note`. Add that edge if it should drop from the current view; use `resolves` instead if it should remain as a raised-but-addressed finding. |
