# State Resolution And Maintenance

This document is for adopters who have run the [Quickstart](quickstart.md) and
now need to trust Cruxible with real state. It answers two questions from the
runtime's actual behavior: when agents and pipelines disagree, what wins — and
what happens to your graph over time.

Vocabulary (candidate groups, signals, receipts, kits) is defined in
[Concepts](concepts.md). Policy syntax is in the
[Config Reference](config-reference.md). Nothing here repeats those documents.

## 1. How Proposal Conflicts Resolve

### Signature buckets

Every governed proposal lands in a **signature bucket**: a SHA-256 of the
relationship type plus canonical `thesis_facts` (`sigv1:...`). The signature
deliberately excludes `analysis_state`, so LLM rationale and other run-varying
context never split a bucket. Workflow-authored proposals hash the workflow
name, step, proposal logic digest, signal sources, and the relationship's
policy; direct agent proposals hash the relationship, the member-derived
signal sources, and the caller's scope facts. The bucket is the unit of
precedent: resolutions and trust are stored per `(relationship_type,
signature)`, not per edge.

### What gets suppressed at proposal time

Before a group is stored, each proposed member tuple is checked (for
`proposal_identity: relationship_tuple` relationships):

- tuple already live in the graph → suppressed, reason `existing_edge`;
- tuple already sitting in a `pending_review` or `applying` group → suppressed,
  reason `pending_proposal`, with the competing group's id in the result;
- tuple already approved earlier in this same signature bucket → suppressed as
  `existing_edge`.

If everything is suppressed, no group is created — the propose result comes
back `suppressed: true` with the per-tuple reasons. Duplicate work is refused
at the door, not merged later.

### Review priorities

Each stored group carries a mechanical `review_priority` derived from policy
signals and prior trust — `cruxible group list` sorts by it:

| Priority | Set when |
|---|---|
| `critical` | any member carries a `contradict` signal from a **blocking** source, or the bucket's prior resolution was **invalidated** |
| `review` | first contact (no prior confirmed approval for this signature); an `unsure` signal where the source sets `always_review_on_unsure` or has role `blocking`/`required`; a `support` signal with no evidence under `require_evidence_on_support`; prior resolution on `watch`; a decision policy with effect `require_review` matched; or a member tuple whose live edge has an active override or pending/rejected review state |
| `normal` | none of the above — a clean repeat of an already-reviewed thesis |

Signals from sources with role `advisory` are skipped entirely in this
derivation. Priority is advisory ordering for reviewers; it does not gate who
may resolve.

### Auto-resolve: earned, per bucket, never on first contact

A fresh group is APPROVED immediately — through the real resolve rail, with
real edges, a real resolution row, and a real receipt — instead of entering
`pending_review`, only when **all** of the following hold:

1. The bucket has a prior **confirmed approval** whose trust status satisfies
   `auto_resolve_requires_prior_trust` (`trusted_only` by default;
   `trusted_or_watch` optionally). No prior resolution — or an `invalidated`
   one — means no auto-resolve. **The first run of any thesis always goes to
   review.**
2. Current signals satisfy `auto_resolve_when`: `all_support` (every
   non-advisory signal is `support`) or `no_contradict` (no blocking
   `contradict`). An `unsure` under `always_review_on_unsure`, or an
   unevidenced `support` under `require_evidence_on_support`, disqualifies
   regardless of policy.
3. Nothing forces review: no matched `require_review` decision policy, and no
   member tuple with an active edge override.

Trust does not accumulate automatically. A first approval records the
resolution at `watch`. Promotion is an explicit act:

```bash
cruxible group resolutions                 # find the resolution ID
cruxible group trust --resolution <id> --status trusted \
  --reason "Spot-checked 20 members against source documents"
```

`group trust` also revokes: `--status invalidated` makes the next matching
proposal come back `critical` and permanently blocks auto-resolve until a
human re-approves the bucket (that re-approval resets trust to `watch`, not
`trusted`). Trust can only be set on the **latest confirmed approval** for a
signature — you cannot re-trust a superseded precedent. Trust changes never
touch existing edges; demoting a precedent and retracting a wrong edge are two
separate acts.

Auto-resolution runs the same receipted approve transition a reviewer would:
`propose_group` returns `status="resolved"` with a `resolution_id`, and the
resolution records `resolution_source="auto_resolved"` so it says honestly how
it came about. (`auto_resolved` was a group STATUS until 0.3. It was a dead-end
label — no path transitioned out of it, no edges existed, and it escaped both
`find_pending_group` and the pending unique index, so re-proposing the same
signature minted a duplicate row. It survives only as `resolution_source`, plus
a deprecated read-only `GroupStatus` member so 0.2.x rows still load.)

One honest limit remains: applying edges is `GRAPH_WRITE` while proposing is
`GOVERNED_WRITE`. A proposer below that tier does not escalate itself — the
group stays in `pending_review` and the result carries
`auto_resolve_deferred_reason`. The same happens if the approve itself is
refused. Nothing in core applies pending groups on a timer.

### Re-proposing while a group is pending

Buckets converge instead of forking. If a proposal arrives for a signature
that already has a `pending_review` group, the pending group is **rewritten in
place**: members replaced (default) or merged (`pending_refresh_mode:
retain_missing`), metadata refreshed, priority re-derived, and
`pending_version` incremented. A rewrite never auto-resolves — auto-resolve is
evaluated only for fresh buckets. If the re-proposal has no surviving members,
the default mode WITHDRAWS the now-empty pending group (with a `group_withdraw`
receipt) rather than deleting it: the group was proposed, it was reviewed
against, and its members are evidence, so it is retired in place. `withdrawn`
sits outside the pending unique index, so the signature is free for a later
proposal — but a withdrawn group is terminal and `group resolve` refuses it.
`retain_missing` leaves the group standing.

A re-propose accepts an optional `expected_pending_version`, the same optimistic
guard `group resolve` requires: pass the version you computed your delta against
and a bucket that moved underneath you is refused instead of overwritten.

`pending_version` is the reviewer's concurrency guard: resolve requires
`--expected-pending-version`, and a mismatch fails with "Group changed during
review". You approve the exact member set you inspected, or nothing.

### Approval and rejection semantics

**Approve** validates every member against the current graph and config:
already-live tuples are skipped (reason `existing_edge` — pass
`stamp_existing` to instead bless the surviving edge with the group's review
state and provenance), invalid members are skipped with the validation detail,
and relationship evidence guards can abort the whole approval. Valid members
become edges through the governed `group_resolve` write path, stamped with the
group's evidence refs, source receipt/trace/step ids, and an
`assertion.review` of `approved/group`. The resolution is confirmed and the
group moves to `resolved`. If the process dies mid-apply the group is left
`applying`; re-running approve retries the same resolution (reject is refused
in that state).

**Reject** writes no edges. It records a confirmed `reject` resolution (with
your rationale and the group's full thesis and analysis state) and marks the
group `resolved`. Rejection is not a tombstone: it does not count as the prior
approval that auto-resolve looks for, so a re-proposal of the same thesis
opens a fresh bucket that again forces review. If you want a rejection to
*teach* the system, pair it with structured feedback (`cruxible feedback`) or
a decision policy so the same candidates get suppressed at proposal time.

## 2. Direct Writes Vs Governed Writes

### Permission tiers

The runtime enforces four cumulative tiers via `CRUXIBLE_MODE`
(`ADMIN ⊃ GRAPH_WRITE ⊃ GOVERNED_WRITE ⊃ READ_ONLY`):

| Tier | Can do |
|---|---|
| `read_only` | queries, receipts, traces, inspect, `group list`/`get`/`status`, state health, workflow planning, `state pull-preview` |
| `governed_write` | propose groups, run/test/propose workflows, feedback and outcomes, decision records, snapshots, constraints and decision policies |
| `graph_write` | `entity add`/`update`, `relationship add`, batch direct write, **canonical workflow apply**, **group resolve**, **group trust** |
| `admin` | config reload, locks, clones, backup/restore, state publish, overlays, credentials, `state pull-apply` |

The split to notice: an agent at `governed_write` can *propose* anything but
*commit* nothing — resolving a group, applying a canonical preview, and
adjusting trust all sit at `graph_write`. When `CRUXIBLE_MODE` is unset the
local default is `admin` (deliberate, for local UX; set
`CRUXIBLE_DEFAULT_READ_ONLY=1` or an explicit mode to change it).

The two halves of a state pull sit at opposite ends of that table. `state
pull-preview` is a pure read (`read_only`), while `state pull-apply` replaces
the active config *and* the whole graph with an upstream release — the same
authority as `config reload` plus a graph rewrite — so it sits at `admin` with
the other instance-lifecycle operations, not with the governed proposal verbs.

These tiers are enforced as a boundary on the **daemon and MCP surfaces**, where
the serving process fixes its ceiling at startup and no request can raise it.
The **local CLI runs in the operator's own process and reads the operator's own
`CRUXIBLE_MODE`** — it is an operator console at operator tier by design, not a
sandbox against the person at the shell. Agents are expected to reach state
through MCP or the daemon, never through a shell on the state host. See
[Runtime Auth And Agent Roles](runtime-auth-and-agent-roles.md#where-permission-tiers-are-enforced).

### Write policies are orthogonal to tiers

Per-type `write_policy` is a hard governance constraint that no tier
overrides, including `admin`:

- `proposal_only` — direct writes (`entity add`, `relationship add`, batch
  direct write, the typed lifecycle write) are refused with
  `direct_write_refused`; state enters only through the governed verbs
  (`workflow_apply`, `group_resolve`) or, for relationships, staged with
  `pending=true`. The `CRUXIBLE_REFUSE_DIRECT_WRITES` env kill-switch forces
  this instance-wide, and while it is set the acceptance-transitioning feedback
  actions (`accept` / `correct`) are refused too.
- `mint_only` — refuses **every** writer including the governed verbs; only
  the `token_mint` source may write.

Two further rails are hard constraints at the same chokepoints:

- **Adjudication tier.** `feedback accept` / `reject` / `correct` require
  `graph_write` even though the feedback tools sit at `governed_write`:
  promoting a claim to live is the same authority as a direct write. These are
  the only feedback actions: `flag` was removed. Register a doubt with
  `cruxible attest --stance contradict`, which stays at `governed_write`.
- **Pending proposals are immutable while staged.** A non-pending write onto a
  tuple whose edge is `pending` is refused with `pending_edge_write_refused`
  (HTTP **409**) rather than silently replacing the proposal's content under a
  reviewer. Withdraw and re-propose, or resolve the proposal first.

### Mutation guards refuse with reasons and receipts

Config-defined mutation guards (actor identity, co-write requirements,
evidence floors, named-query result counts) run at the write chokepoints —
direct writes, workflow apply, and group approval alike. A refusal is a
`DataValidationError` whose errors name the guard and the offending write
(`Mutation guard '<name>' rejected write <type>:<id> <property>=<value>:
<message>`). Failed mutations still persist a receipt: the receipt records the
failed validation nodes and the error carries its `mutation_receipt_id`, so a
refusal is as auditable as a success.

### Auth-managed types

An entity type marked `auth_managed: true` + `write_policy: mint_only` (the
agent-operation kit's `Actor` is the canonical example) is materialized
through the internal `token_mint` source: auth-on daemons use runtime-
credential mints, while auth-off daemons create a declared local `operator`
identity. Config-declared workflows that target a `mint_only` type are
rejected at config load, and lifecycle updates are refused like any other
write. Facts *about* such an entity belong on notes attached to it, never on
the entity itself.

### Provenance on every edge

Every edge carries system-owned provenance: `source` (the operation),
`source_ref`, `created_at`/`last_modified_*`, actor context from either the
auth-on credential or the auth-off local operator,
and write-time `receipt_id`/`resolution_id` correlation. The `source_ref`
classes are how you read authority off an edge:

- `add_relationship` / `batch_direct_write` — direct-written;
- `group:<group_id>` — group-backed, with `resolution_id` linking to the
  approval;
- anything else (workflow apply refs, `clone_origin`-stamped snapshot/pull
  edges, legacy nulls) — "other".

Governed groups additionally record how their evidence was produced in their
signature facts: `evidence_mode: workflow_generated` (proposal built by a
locked workflow, carrying the workflow name, step, and proposal logic digest)
vs `agent_supplied` (an agent asserted the signals directly). The two modes
hash into different signatures, so agent-asserted judgments never inherit the
trust earned by a pipeline's judgments.

## 3. State Maintenance Over Time

### Lifecycle, not deletion

Entities are `live` / `superseded` / `retired`; relationships are `active` /
`inactive` / `superseded` / `retracted`. Non-live state is gated out of live
reads but stays fetchable by id, with `reason`, `closed_at`/`closed_by`, and
supersession links preserved. Lifecycle is set only through the typed channel:

```bash
cruxible relationship update --relationship SupportedBy \
  --from-type Matter --from-id M-104 --to-type Precedent --to-id P-7 \
  --lifecycle-status inactive --lifecycle-reason "no longer relied on"
```

**Terminal statuses are not writable through `add`/`update`.** Retiring or
superseding an entity, and retracting or superseding a relationship, are
governed judgements about a claim's standing rather than property edits, so
plain `entity add`/`update` and `relationship add`/`update` refuse them
(`terminal_lifecycle_write_refused`). What stays freely writable on those verbs
is the reversible half: `active`/`inactive` for relationships, `live` for
entities. The dedicated receipted lifecycle verbs land in `wi-lifecycle-verbs`;
until then, record a contradiction with `cruxible attest record --stance
contradict` or move the claim through the review machinery
(`cruxible feedback`).

**Attesting advances the instance read revision.** An attestation or a
disposition never changes a claim's trust, review, or lifecycle status — but it
does change what reads *return*, because corroboration summaries (stance counts,
`distinct_actor_count`, `last_*_at`, open-contradiction indicator) are computed
from the attestation store and attached to edge payloads on ordinary edge,
neighborhood, and single-relationship reads. So recording an observation bumps
`read_revision` exactly like a graph write does, and it invalidates any
outstanding continuation token: tokens are revision-bound, and resuming a page
after a new attestation raises a `409` `StaleContinuationError` so a paginated
read can never silently span two different states. Repeat the read from the
first page to pick up the new corroboration.

**Running a procedure advances it too, twice.** The run ledger is read state
now that `procedure list` / `procedure show` derive a `track_record` block from
it, so both ledger writes bump `read_revision`: once when the crash-visible
started run commits, and once when the run is finalized with its verdict. That
holds for refusals as well as successes — a preflight or precondition refusal
still writes both. The same continuation rule follows: a procedure page read
before an invocation cannot be resumed after it, and a working-set record
captured before it reads stale rather than fresh.

Hand-authored `metadata={"lifecycle": ...}` is inert free-form data — it can
never become the typed state. The lifecycle write is a direct-write verb, so a
`proposal_only` type refuses it too. Reserve deletion for bad imports and test
data; everything operational should retire, not vanish.

### Re-running deterministic ingest

Canonical ingest workflows are safe to re-run:

- **No-op upserts.** `apply_entities` / `apply_relationships` compare against
  current state; an upsert that changes nothing is counted as a `noop` — no
  write, no receipt write-node, no provenance churn. Re-running an unchanged
  ingest converges instead of rewriting.
- **Digest-pinned artifacts.** Canonical workflows require their file or
  directory artifacts to carry a `sha256:` digest. The digest is verified
  against disk when the lock is built and again when a plan compiles. If seed
  data changes underneath you, the run fails with the expected and actual
  hashes; `cruxible lock --force` is the explicit act of accepting the new
  content. Data cannot drift silently under a pinned workflow.
- **Preview/apply identity.** The `apply_digest` binds workflow name,
  normalized input, lock digest, head snapshot, and the previewed changes —
  apply refuses a preview that no longer matches what you inspected.

### Staleness is a kit-level idiom

Core has no decay or freshness engine — time-based maintenance is written as
kit workflows. The pattern is a **date sweep**: a canonical workflow that
queries current state, applies a deterministic date rule in a provider, and
writes back narrow status changes. The case-law kit's
`refresh_stale_deadlines` is the reference example: it closes deadlines that
lapsed or whose matter closed, and deliberately does *not* auto-close the work
items behind them — those close only through the review gate. If your domain
has a "stale after N days" rule, model it as a sweep workflow so the rule is
pinned, previewable, and receipted.

### State health

`cruxible state health` (also `GET /api/v1/{instance_id}/state/health`) is the
deterministic maintenance dashboard. It reports raw counts, ages, and binary
facts only — no scoring or severity; interpretation is left to you or your
agents. Five sections, plus `captured_at` and the current `head_snapshot_id`:

- **groups** — counts by status and the age span of the *unresolved* backlog
  (`pending_review` + `applying` only; an old pending group is a stale review
  queue, an old applying group is a stuck apply);
- **signals** — `unevidenced_support_by_source`: support signals sitting in
  pending review with no evidence, counted per source and scoped to sources
  that declare `require_evidence_on_support` — a per-source backlog of
  judgments asserted without proof;
- **provenance** — every live edge tallied as direct-write, group-backed, or
  other (watch the direct-write share on a domain you meant to govern);
- **freshness** — source-artifact and provider-trace counts and oldest ages,
  plus config/graph compatibility warnings;
- **integrity** — orphan entities, unused entity/relationship types, and
  whether the workflow configuration is locked.

## 4. Repair: When Accepted State Is Wrong

Wrong state that passed review is fixed in the open, not rewritten. The
sequence:

1. **Take the wrong fact out of live reads, with a reason.** Terminal lifecycle
   statuses (`retracted`/`superseded` on an edge, `retired`/`superseded` on an
   entity) are refused on plain `add`/`update` until the receipted lifecycle
   verbs land in `wi-lifecycle-verbs`. Today: record the contradiction
   (`cruxible attest record --stance contradict --evidence-ref '{...}'`) or
   reject the claim through `cruxible feedback`, which moves the edge out of
   live review state. `--lifecycle-status inactive` remains available for a
   reversible participation flip. Either way its history, provenance, and
   receipts remain.
2. **Demote the precedent that admitted it.**
   `cruxible group trust --resolution <id> --status invalidated --reason "..."`
   so future matches of the same thesis re-review instead of auto-resolving.
   Skipping this step means the same pipeline can re-admit the same mistake.
3. **Re-propose the correction.** Propose the corrected members through the
   normal governed path. First contact with the corrected thesis forces review
   — that is the system working, not friction.
4. **Let quality checks catch the rest.** `cruxible evaluate` and `cruxible
   lint` report constraint violations, orphans, coverage gaps, and
   quality-check failures deterministically; `cruxible lint` additionally
   turns repeated rejection feedback and negative outcomes into concrete
   suggestions (constraints, decision policies, trust demotions).

For the audit trail while you work:

```bash
cruxible entity history --type Matter --id M-104   # receipt-derived change history
cruxible explain --receipt <receipt-id>            # render any receipt
cruxible group get --group <group-id>              # thesis, members, signals, resolution
```

Every mutation — including refused ones — has a receipt; every group-backed
edge links its `resolution_id`; every resolution stores the thesis and
analysis state it was judged on. If you cannot reconstruct why an edge exists,
that is a bug worth reporting, not a gap you should paper over.

## Summary: Who Wins

- **Pipelines and agents never overwrite each other silently.** Live edges and
  pending groups suppress overlapping proposals; pending buckets converge by
  rewrite with a version guard; direct writes to governed types are refused.
- **Review wins by default.** First contact, contradictions, unsure signals,
  and unevidenced support all force a human (or `graph_write` agent) decision.
- **Automation is earned per thesis** — a confirmed approval promoted to
  `trusted`, revocable in one command.
- **Time is handled by pinned workflows, not decay** — and `state health`
  tells you when the backlog, evidence debt, or provenance mix needs
  attention.
