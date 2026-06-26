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

Direct writes are available for explicit state updates. When a direct
relationship write overlaps a member of a pending or applying group, Cruxible
keeps the write permissive and annotates the affected group with direct-write
conflict metadata.

The group is not auto-approved, rejected, or mutated into a different status.
The reviewer sees that live state changed while the group was pending and can
decide whether to approve, reject, refresh, or use `stamp_existing`.

## Lifecycle Maintenance

Cruxible distinguishes domain properties from system lifecycle metadata.

Use lifecycle state when an entity or relationship should stop participating in
normal live reads:

- retire or supersede stale entities instead of deleting them;
- retract, supersede, or inactivate stale relationships instead of rewriting
  history;
- keep receipts and provenance intact so future agents can inspect what
  happened.

Deletion should be reserved for bad imports, test data, or invalid state that
should not be preserved as operational history.

## Evaluate And Health

`evaluate` is the low-level graph and config quality checker. It reports facts
such as orphan entities, coverage gaps, constraint violations, quality check
failures, governed support warnings, and unreviewed co-member prompts.

State health is broader. A health surface should aggregate deterministic
maintenance signals across:

- graph evaluation findings;
- pending and applying group counts and age;
- direct-write versus group-backed edge ratios;
- source artifact drift and stale evidence;
- provider and trace freshness;
- config, lock, generated-view, or seed-data drift;
- unused relationship types or high orphan rates by entity type.

Core health signals should be deterministic and defensible. Agents can then
interpret those signals, rank maintenance work, and propose repairs.

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
