# State Resolution And Maintenance

Cruxible state is durable shared state. Maintaining it means resolving review
groups, retiring stale facts, checking health signals, and preserving receipts
that explain why a graph changed.

Cruxible does not silently merge conflicting state. It preserves explicit graph
state and makes reviewer or operator actions visible through receipts,
relationship metadata, group resolutions, and lifecycle state.

## Resolution Model

Candidate groups are review buckets for governed relationship changes. A group
has a relationship type, a signature, members, thesis text, signals, review
state, and eventually a resolution.

Cruxible resolves group conflicts in three places:

- proposal time;
- review time;
- maintenance time.

At proposal time, Cruxible avoids obvious duplicate work:

- if a tuple is already live, the proposed member is suppressed as
  `existing_edge`;
- if a tuple is already in a pending or applying group, the proposed member is
  suppressed as `pending_proposal`;
- if a proposal lands in the same signature bucket as an existing pending
  group, the pending group is rewritten or refreshed rather than creating a
  second independent group.

At review time, a reviewer approves or rejects the group. Approving creates
valid missing edges. Rejecting records the decision without creating edges.

At maintenance time, reviewers can adjust trust on prior resolutions:

- `trusted` means a matching future proposal may auto-resolve when policy and
  signals allow it;
- `watch` keeps the precedent accepted but review-sensitive;
- `invalidated` means the precedent should no longer be trusted, and future
  matching proposals should come back for review.

## Pending Versions

Pending groups carry a `pending_version`. Reviewers should approve or reject the
version they inspected. If a group is rewritten while review is in progress,
the stale pending version is rejected at resolution time.

This prevents an agent from approving an older view after another agent added,
removed, or changed candidate members.

## Existing Edges

Approving a group normally creates only missing valid edges. If a member tuple
is already live when resolution runs, the member is skipped and the result
explains why.

This can happen when:

- a direct write created the edge after the group was proposed;
- legacy or imported state already contained the tuple;
- a previous operation created the edge outside the group currently being
  reviewed.

By default, Cruxible skips the existing edge rather than changing its authority
label. This is conservative: the existing edge may have different properties,
evidence, provenance, or prior review state than the proposed group member.

### `stamp_existing`

`stamp_existing` is an explicit reconciliation option on group approval. When
enabled, a skipped existing edge is blessed with the approving group's review
status and group provenance instead of remaining merely direct-written or
unreviewed.

Use it for narrow reconciliation cases:

- a pending group was reviewed, but a direct write created the same edge before
  approval;
- a small amount of trusted legacy state needs to be brought under the group
  that reviewed it;
- a reviewer intentionally wants the group to become the authority for an
  already-live edge.

Do not treat `stamp_existing` as a general merge strategy. It does not fully
solve existing-edge adoption. In particular, the complete post-0.2 design still
needs answers for:

- whether existing-edge adoption should be a group proposal mode or a separate
  command;
- how proposed member properties should be compared with current edge
  properties;
- whether group/member evidence should be merged into the existing edge or only
  referenced through lineage;
- how to handle an edge already backed by a different group;
- when adoption should require a force flag or rationale.

Track that unresolved design as an open question before making adoption the
default behavior.

## Direct Write Conflicts

Direct writes are available for explicit state updates where the domain permits
them — a governed `proposal_only` entity or relationship type (or the
instance-wide `refuse_direct_writes` kill-switch, set via the
`CRUXIBLE_REFUSE_DIRECT_WRITES` environment variable) refuses direct writes and
forces state in through the proposal/workflow path instead. When a direct
relationship write is permitted and overlaps a member of a pending or applying
group, Cruxible keeps the write permissive and annotates the affected group with
direct-write conflict metadata.

The group is not auto-approved, rejected, or mutated into a different status.
The reviewer sees that live state changed while the group was pending and can
decide whether to approve, reject, refresh, or use `stamp_existing`.

## Lifecycle Maintenance

Cruxible distinguishes domain properties from system lifecycle metadata.

Use lifecycle state when an entity or relationship should stop participating in
normal live reads. Set it with `cruxible entity update --lifecycle-status ...`
and `cruxible relationship update --lifecycle-status ...`. The status
vocabularies are distinct by kind:

- entities are `live`, `superseded`, or `retired` — retire or supersede stale
  entities instead of deleting them;
- relationships are `active`, `inactive`, `superseded`, or `retracted` —
  retract, supersede, or inactivate stale relationships instead of rewriting
  history;
- keep receipts and provenance intact so future agents can inspect what
  happened.

The typed lifecycle write touches only the lifecycle slice; it cannot approve or
reject the edge or alter group state. It is a direct-write verb, so a governed
`proposal_only` domain refuses it just as it refuses other direct writes.

Deletion should be reserved for bad imports, test data, or invalid state that
should not be preserved as operational history.

## Choosing The Right Repair

Every repair verb preserves history except deletion. Pick by symptom:

| Symptom | Repair | Why this verb |
|---|---|---|
| A governed edge that passed review turns out to be wrong | Retract the edge (`relationship update --lifecycle-status retracted`), then invalidate the precedent (`group trust --status invalidated`) | The edge stops participating in live reads, and future matching proposals come back for review instead of auto-resolving on the bad precedent |
| A fact was true and no longer is | Supersede: set lifecycle `superseded` and, where the kit models it, add a `*_supersedes_*` edge to the replacement | The old fact remains inspectable as history; queries follow live state |
| A fact is temporarily suspended, not wrong | Relationship lifecycle `inactive` | Reversible without erasing or re-reviewing anything |
| The interpretation or summary is wrong, but the record matters | A note with `kind: correction`, plus a supersedes edge to the note it corrects (in kits that model notes) | Current summaries stay current; the correction chain carries the history instead of a rewritten description |
| A direct write duplicated a member of a reviewed group | `stamp_existing` at group approval | The reviewed group becomes the authority for the already-live edge (see the constraints above) |
| Repeated bad outcomes on a resolution path | Record outcomes against the claims, set the resolution to `watch` or `invalidated` | Trust demotion is the feedback loop's job; don't hand-retract edges that are individually defensible |
| Bad import, test data, invalid state | Delete | The one case where preserving history is wrong — junk is not history |

When in doubt: lifecycle transitions and corrections are cheap and visible;
deletion and trust invalidation are the two verbs that change what future
operations are allowed to assume, so they deserve a rationale.

## Adjusting Trust On Precedents

Group resolutions are precedents: an accepted resolution can let matching
future proposals auto-resolve when the kit's policy allows it. Trust is
adjusted per resolution:

```bash
cruxible group resolutions                # find the resolution ID
cruxible group trust --resolution <id> --status invalidated \
  --reason "Upstream mapping changed; re-review matches"
```

- `trusted` — matching future proposals may auto-resolve under policy;
- `watch` — the precedent stands, but matches come back for review;
- `invalidated` — the precedent is no longer trusted; matches always re-review.

Trust changes never touch existing edges. Retracting a wrong edge and
demoting the precedent that admitted it are two separate, deliberate acts.

## Auth-Managed Entities

Entity types declared `auth_managed` (for example the agent-operation kit's
`Actor`) are outside normal maintenance: they materialize from
runtime-credential mints, and `write_policy: mint_only` refuses every other
writer — including lifecycle updates. Their status transitions come from the
credential lifecycle (revocation materializes the status change), not from
maintenance verbs. Editorial facts about such an entity belong on notes
attached to it (e.g. `state_note_about_actor`), never on the entity itself.

## Archiving

There is no archive verb yet. The working pattern for retiring a whole
instance's worth of history: stop writing to it, keep the daemon (or a
restored snapshot) available read-only, and start the successor instance
clean rather than porting closed items — receipts and review threads stay
queryable where they happened, and the new instance carries only live work.

## Evaluate And Health

`evaluate` is the low-level graph and config quality checker. It reports facts
such as orphan entities, coverage gaps, constraint violations, quality check
failures, governed support warnings, and unreviewed co-member prompts.

State health is broader. `cruxible state health` (also `GET
/api/v1/{instance_id}/state/health`) aggregates deterministic, read-only
maintenance signals into four sections, alongside a `captured_at` timestamp and
the current `head_snapshot_id`:

- **groups** — candidate-group counts by status (pending_review, applying,
  auto_resolved, resolved, total) plus the age span of the *unresolved* backlog
  (`oldest_unresolved_age_seconds` / `newest_unresolved_age_seconds`, scoped to
  pending_review and applying groups; resolved groups only accumulate age and are
  not an actionable signal);
- **provenance** — every live edge tallied by the class of its provenance
  source: direct-write, group-backed, or other;
- **freshness** — source-artifact and provider-trace counts and oldest ages,
  plus `config_compatible` and any config-compatibility warnings;
- **integrity** — orphan entity count, unused entity and relationship types, and
  whether the configuration is locked.

Like `evaluate`, health reports raw metrics (counts, ages, timestamps) and
binary deterministic facts only — there is no scoring, grading, ranking, or
severity. The core signals are deterministic and defensible; agents interpret
those signals, rank maintenance work, and propose repairs.

Some maintenance signals the surface does not yet aggregate, and which remain
future work, include source-artifact drift versus the tracked upstream, deeper
provider and trace staleness, lock or generated-view or seed-data drift, and
per-entity-type orphan rates.

## Maintenance Workflow

Use this loop for regular state maintenance:

1. Run health or evaluate checks.
2. Inspect pending groups, direct-write conflicts, and stale review queues.
3. Resolve groups with explicit approve/reject decisions.
4. Use lifecycle state to retire stale entities or relationships.
5. Use direct writes only when the state change is explicit and should be live
   immediately.
6. Use candidate groups when a relationship judgment needs review.
7. Preserve receipts and source evidence so another agent can reconstruct the
   decision later.

When in doubt, prefer a visible reviewed transition over a silent rewrite.
