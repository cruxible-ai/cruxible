# Changelog

## Unreleased

Every user-visible fix or feature adds its entry here in the same change
that lands it; entries move under a version heading when the release is
tagged. Work items for these changes live on the active release line in
the project's own state instance.

### Changed (BREAKING)

- **The self-declared `human`/`agent` axis is retired**: `FeedbackRecord.source`,
  `OutcomeRecord.source`, `GroupResolution.resolved_by`,
  `CandidateGroup.proposed_by`, `DecisionRecord.opened_by`, and
  `make_group_proposal`'s `proposed_by` were all caller-supplied, defaulted to
  `"human"`, and were never reconciled with `actor_context.actor_type`. They were
  not inert: the feedback and outcome profiles require a `reason_code` only for
  non-human writers, so an agent could skip the accountability rule written for
  it simply by declaring itself a person. Every one of those fields is removed,
  along with the matching `source` / `proposed_by` / `resolved_by` / `opened_by`
  parameters on the service functions, the runtime facade, the MCP tools, the
  HTTP request models, the CLI (`--source`, `--opened-by`), and the client.
  Readers derive the value from the actor context.

  The READ-side field names survive as DEPRECATED derived projections (see
  *Deprecated* below): `FeedbackRecord.source`, `OutcomeRecord.source`,
  `GroupResolution.resolved_by`, `CandidateGroup.proposed_by`, and
  `DecisionRecord.opened_by` are re-emitted, computed from
  `derived_actor_kind(actor_context)`. What is gone is the ability to DECLARE
  them. The retired request fields are accepted and ignored with a
  `deprecated_request_field` warning rather than rejected.

  The `reason_code` requirement now keys off the derived kind and applies to
  everything that is not a resolved human — including `"unknown"`, because an
  unattributed write is absence of evidence, not evidence of a person.
  `RelationshipReviewSource` gains `"unknown"` for the same reason.

  **Migration:** drop the retired arguments from every call; supply an
  `actor_context` instead (auth-on daemons derive one from the credential;
  auth-off daemons default to the declared local operator). Kits declaring
  `proposed_by` on a `make_group_proposal` step must remove it — the step spec
  forbids extra keys. Persisted rows are unaffected: the SQL columns survive as
  denormalized projections written from the derived value.

  **Contract fields removed:** `FeedbackFromQueryInput.source`; the
  `FeedbackSource`, `GroupProposedBy`, and `GroupResolvedBy` type aliases.
  `StateHealthGroupsSection.auto_resolved_count` is superseded by
  `withdrawn_count` but stays on the contract as a deprecated always-0
  projection.

- **`auto_resolved` is retired as a group status**: it was a dead-end label. No
  code path transitioned a group out of it, no edges were created, no resolution
  row existed, and because `find_pending_group` and the pending unique index both
  key on `pending_review`, an auto-resolved group was invisible to the next
  proposal of the same signature — which therefore inserted a DUPLICATE pending
  row instead of rewriting it (`wi-group-auto-resolve-bug`; auto-resolve is
  enabled in shipped kits). Auto-resolution now runs the real approve transition:
  same receipt, same edge provenance, same resolution row as a reviewer-driven
  approve, marked `resolution_source="auto_resolved"`. `propose_group` returns
  `status="resolved"` with a `resolution_id`.

  Applying edges is `GRAPH_WRITE` while proposing is `GOVERNED_WRITE`, so a
  proposer below that tier does not escalate itself: the group stays in
  `pending_review` and the result carries `auto_resolve_deferred_reason`. The
  same happens if the approve itself is refused (a member fails validation, a
  guard rejects it) — the proposal does not fail, and the reason travels on the
  result.

  **Contract change:** `GroupStatus` gains `withdrawn`; `GroupResolution` gains
  `resolution_source`. `auto_resolved` stays in `GroupStatus` as a DEPRECATED
  read-only member (see *Deprecated*) — shipped 0.2.x kits wrote such rows and
  they must still load. They are NOT migrated to `withdrawn`: nobody withdrew
  them, and minting that act would fabricate a governance event that never
  happened. They are terminal, and `resolve_group` refuses them.

- **An empty-delta re-propose withdraws its pending group instead of deleting
  it**: under the default `pending_refresh_mode="replace"`, a re-propose that
  produced no delta used to DELETE the pending group and every one of its
  members, erasing governance history and leaving any receipt naming that
  `group_id` joined to nothing. The group is now marked `withdrawn` with its
  members intact; `withdrawn` sits outside the pending unique index, so the
  signature is free for a later proposal. The receipt operation type is
  `group_withdraw` (was `group_clear`; the old literal stays readable, see
  *Deprecated*). `propose_group` also accepts an optional
  `expected_pending_version`, the same optimistic guard `resolve_group`
  requires — now carried on the HTTP request model, the MCP tool, and
  `cruxible group propose --expected-pending-version`, not only the client.

- **Approve no longer moves trust**: a new approval CARRIES the signature's trust
  posture — status, reason, and the actor who set it — forward verbatim. It used
  to launder a reviewer's receipted `invalidated` into `watch`, twice (once when
  the resolution was created, once again at confirmation), discarding the
  judgement without a receipt, an actor, or a reason. Under
  `auto_resolve_requires_prior_trust: trusted_or_watch` that also silently
  re-armed auto-resolution for the very thesis a reviewer had just invalidated;
  under the `trusted_only` default it merely lost the reason. Trust changes only
  through the receipted `update_trust_status` verb.
  `GroupStore.confirm_resolution` no longer takes a `trust_status` override.

- **Config mutations and snapshot creation move up a tier**: `add_constraint` and
  `add_decision_policy` are ACTIVE CONFIG — once saved they adjudicate every
  later query and workflow, which is the authority `reload_config` carries — so
  both require `ADMIN`. `create_snapshot` MOVES the instance head, invalidating
  every outstanding state-pull apply guarded on the previous one, so it requires
  `GRAPH_WRITE`. All three now mint receipts (`config_add_constraint` and
  `config_add_decision_policy` carry pre/post config digests; `snapshot_create`
  names the head it moved from and to) and thread the resolved actor, which the
  facades previously computed and discarded.

- **Feedback adjudication requires `graph_write`**: `feedback approve`,
  `reject`, and `correct` decide a claim's fate — they make a non-live
  edge live, or retract one — so they now require `GRAPH_WRITE` even
  though the `cruxible_feedback` / `_batch` / `_from_query` tools
  themselves stay at `GOVERNED_WRITE`. Previously a single
  `governed_write` actor could stage an edge (attestation on an absent
  claim, or a `pending` write) and then approve its own proposal, reaching
  a live approved claim on a `proposal_only` type with no reviewer above
  it. `flag` is the only feedback action still available at
  `governed_write` — it moves an edge *to* `pending`, i.e. it asks for
  review rather than granting it — alongside persisting the
  `FeedbackRecord` itself. The refusal is a receipted
  `PermissionDeniedError` (HTTP 403) naming the required tier.

  **Migration:** any caller that approves/rejects/corrects with a
  `governed_write` credential must present a `graph_write` one. This also
  overrides a type's config-declared `write_tier: governed_write` for
  those three actions — a type owner may lower who *direct-writes* their
  type, not who *adjudicates* claims on it. Auth-off local use is
  unaffected (the local default is `admin`).

- **Group resolution is gated at the service seam too**: the exported
  `service_resolve_group` now re-asserts `GRAPH_WRITE` inside its own
  mutation-receipt scope, so a direct library caller cannot reach the
  transition (and, with `stamp_existing`, bless a pending edge) below the
  tier the `cruxible_resolve_group` tool has always required. No change for
  MCP/HTTP callers; the refusal is a receipted `PermissionDeniedError`
  (HTTP 403).

- **`CRUXIBLE_REFUSE_DIRECT_WRITES` now spans the feedback channel**: while
  the kill-switch is set, the feedback actions that move an edge *into*
  accepted state (`approve` / `correct`) are refused alongside the direct
  write verbs, so freezing live writes can no longer be walked around via
  feedback. `reject` / `flag` stay available — they move edges *out* of
  live state.

### Fixed (governance)

- **A withdrawn group can no longer be resurrected.** `resolve_group` accepted
  any status that was not `resolved`, and withdrawing PRESERVES the proposal and
  its members (that is the point of withdrawing rather than deleting) — so the
  preserved proposal stayed approvable by id afterwards, including once a fresh
  pending group for the same signature existed and had been reviewed on its own
  terms. Resolve now takes an allowlist: `pending_review` only, plus `applying`
  for an approve retry. Every other status is terminal.

- **Overlapping pending groups all see a direct-write conflict.**
  `find_pending_groups_for_tuples` collapsed same-tuple matches to the newest
  group, so a newer (or decoy) group absorbed the whole interaction: it alone was
  annotated with the conflict record and had its `pending_version` bumped, while
  an older group claiming the same edge stayed at the version its reviewer had
  read. That reviewer's `expected_pending_version` guard then never tripped and
  their approve went through against state that had already moved. Every live
  group claiming the tuple is now returned, annotated, and bumped.

- **Governed write-verb names are refused at the public direct-write seam.**
  `provenance_source` is caller-supplied on `add_relationships` /
  `batch_direct_write`, and the chokepoint EXEMPTS `workflow_apply` and
  `group_resolve` from the `proposal_only` refusal — so naming one let a bare
  direct write create brand-new `proposal_only` relationships and write
  `proposal_only` entities with no proposal, no workflow, and no reviewer in the
  act. (The content-binding refusal shipped earlier in this batch only covered
  rewrites of an already-approved EDGE.) Those names are now reserved: the public
  entries raise a receipted `GovernedSourceSpoofRefusedError` (HTTP 403). The
  genuine governed paths are untouched — group resolution and workflow apply call
  `apply_entity` / `apply_relationship` directly and never route through these
  entries.

  **Migration:** a caller passing `provenance_source="workflow_apply"` or
  `"group_resolve"` to a direct-write verb must pick a source that honestly
  describes the write, or go through `group propose` / the canonical workflow.

- **`workflow_apply` marks group-approval drift too, and the marker now reports
  CURRENT divergence.** A canonical workflow apply is a legitimate governed write
  and is not refused when it changes a group-approved edge — but it never routed
  through the direct-write group-interaction detection, so it overwrote approved
  content leaving no trace on the edge at all. Detection and stamping moved to a
  shared `graph/group_drift.py` that both write paths use.

  RULING (Robert, 2026-07-25) on the marker's semantics, applied to both sites:
  `group_approval_drift` reflects divergence RIGHT NOW. It is recomputed against
  the approved content on every write and DROPPED when the content fully matches
  the approval again; a partial revert lists only the properties that still
  diverge. The approved baseline is still carried forward across writes (so the
  record says what the GROUP approved, not what the edge said last time). The
  previous accumulate-only behavior left a permanent stain: an edge that had been
  edited and then exactly restored still read as drifted forever. History of each
  excursion lives in receipts, not in live state.

- **Decision-record terminal transitions are race-safe, and the raw setter is
  private.** `update_record`'s "is it still open?" check lived only in a
  preceding SELECT, so two writers on separate connections could both read
  `open` and both UPDATE — SQLite serializes writers, not read-then-write pairs.
  The loser silently overwrote the winner's terminal state, leaving a record
  whose status contradicted its own event log. The predicate now lives in the
  UPDATE (`AND status = 'open'`) with a rowcount refusal. The method is also
  renamed `_close_record` and removed from `DecisionStoreProtocol`: it was public,
  so any holder of a store handle could flip a record's status with no matching
  terminal event. `finalize_record` / `abandon_record` are the only paths.

- **Evidence refs pin the artifact revision they were made against.**
  `EvidenceRef` retained only the LOGICAL `artifact_id`, and dereference always
  resolved to the CURRENT revision — so a citation made against revision 1
  silently returned revision 2's text once the document was re-registered, even
  though revision 1's chunks, manifest, and archived bytes were all still stored.
  `EvidenceRef` and `SourceEvidenceInput` gain an optional `artifact_revision_id`
  (`{source_artifact_id}@{revision}`), which `resolve_source_evidence_refs` now
  stamps at citation time; `dereference_source_evidence` reads revision-scoped
  when pinned. Additive: old refs carry no revision and still work, falling back
  to the current one — but the result says so via `revision_unpinned` rather than
  letting a caller infer it from a matching hash. Exposed on the HTTP route, the
  MCP tool, the client, and `cruxible source dereference --revision`.

### Deprecated

Deprecate-then-remove applies to every shipped surface: these all still work,
each is annotated `Deprecated:` at its definition, and all are scheduled for
removal in the release after 0.3.

- **`GroupStatus` keeps `auto_resolved` as a read-only member.** Nothing writes
  it any more, but shipped 0.2.x kits (auto-resolve is enabled in them) persisted
  rows with it. Dropping the literal made `_row_to_group` raise on every
  list/get that touched one, so a single legacy row bricked group reads for the
  whole instance immediately after upgrading. Legacy rows are terminal and
  filterable (`cruxible group list --status auto_resolved`) so an operator can
  find them; nothing transitions them and nothing recreates them.
- **`OperationType` keeps `group_clear`.** Renamed to `group_withdraw`, never
  written again, but 0.2.x receipt stores hold rows carrying the old value and
  `get_receipt` raised on every one of them. A rename must not make an audit
  record unreadable.
- **Derived actor-kind projections re-emitted under the old field names.**
  `FeedbackRecord.source`, `OutcomeRecord.source`, `GroupResolution.resolved_by`,
  `CandidateGroup.proposed_by`, and `DecisionRecord.opened_by` return as
  computed, read-only values from `derived_actor_kind(actor_context)` — exactly
  what the matching SQL columns already store. Declaring them is gone; reading
  them is not. Read `actor_context` instead.
- **Retired declared-actor REQUEST fields are accepted and ignored.** Sending
  `source` / `proposed_by` / `resolved_by` / `opened_by` to a mutating HTTP route
  logs a `deprecated_request_field` warning instead of silently dropping the
  value. It is never honored — the kind is derived from `actor_context`.
- **`StateHealthGroupsSection.auto_resolved_count` returns, always 0.** An honest
  zero: no path can grow that bucket any more. Read `withdrawn_count`.

### Fixed

- **Acceptance binds content**: a group approval accepts an edge's PROPERTIES,
  not merely its existence. A later direct write that changes a group-approved
  edge's content is now refused on `proposal_only` types (with a message naming
  the approving group and pointing at the proposal rail) and stamped with a
  receipted drift marker on ordinary types, where facts legitimately change. A
  content-identical write is neither refused nor marked.

- **Direct-write conflict records are append-only and attributed**: a second
  conflict on the same tuple used to REPLACE the first, destroying the earlier
  `detected_at` and `receipt_id` — the record of how many times live state moved
  under a proposal. Records now append and carry the acting actor context. More
  importantly, `update_group_analysis_state` now bumps `pending_version`, so the
  reviewer's `expected_pending_version` guard actually trips: a resolve issued
  against the pre-conflict view used to sail straight through the one mechanism
  that says "the group changed during your review".

- **Provenance backfill no longer claims the toucher's channel**: touching an
  edge that carried NO provenance used to stamp the touching channel as the
  edge's ORIGIN, asserting a provenance the edge never had and turning "we do
  not know where this came from" into a confident, false claim. Such edges are
  now marked `source="unknown_backfilled"` with the touching channel recorded
  separately as `touched_by`.

- **Decision records are append-only and receipted**: `save_record` was a
  full-row upsert, so a finalized record could be silently rewritten back to
  `open`; and because `append_event` refuses once a record is closed while
  finalize/abandon transitioned FIRST, the terminal event for the closing act
  itself could never be recorded. Records are now insert-only with an explicit
  reopen refusal, the terminal event is emitted before the status guard, create/
  finalize/abandon mint receipts, and a failed event append is surfaced on the
  result instead of being swallowed into a log line.

- **Execution traces and source artifacts are insert-only**: a duplicate
  `trace_id` used to silently REPLACE the evidence that a prior provider
  execution happened; traces now refuse it and carry an `actor_context`.
  Registering a source artifact under an existing id used to rewrite the
  manifest that prior evidence refs were pinned against — it now writes a new
  revision with a supersedes pointer, closes the duplicate-check TOCTOU by
  holding the guard inside the write boundary, mints a receipt, and PERSISTS
  detected content drift instead of recomputing and forgetting it on every read.

- **Pending proposals are no longer clobbered**: a plain non-pending write
  onto a tuple whose edge is still `pending` used to resolve as an update
  and silently replace the proposal's properties in place while a reviewer
  was adjudicating it. It is now refused at the single relationship
  chokepoint with `PendingEdgeWriteRefusedError`
  (`pending_edge_write_refused`, HTTP **409**), so every write path —
  single, batch, typed lifecycle write, and canonical workflow apply —
  inherits it. The message names both exits: withdraw/re-propose through
  the pending path, or resolve the proposal through the review machinery
  first. Pending-onto-pending is unchanged (still the create-only rule),
  and post-acceptance updates work exactly as before.

  **Preview boundary:** `dry_run` previews raise these refusals with
  identical semantics but are excluded from the receipt guarantee — receipts
  record what happened, not what was previewed, and a preview persists
  nothing. This is the existing dry-run convention, unchanged here.

## 0.2.8 — 2026-07-21

### Added

- **Gate evaluation receipts**: every `gate check` mints a daemon-side
  `gate_evaluation` receipt inside one write transaction — gate, kind,
  candidates, per-candidate outcomes with satisfying entity IDs, verdict,
  and the observed `(instance_id, read_revision)`; refused evaluations
  (exit-2 paths) are receipted with the reason. Evaluation observes a
  single revision (concurrent mutations wait rather than splitting the
  verdict). New server-side check endpoint
  (`POST /api/v1/{instance_id}/gates/{name}/check`) plus a typed client
  method; CLI candidate sourcing, output, and exit codes unchanged.
- **Loud write targets**: every mutating CLI verb prints a one-line
  stderr target (`instance @ transport`) with provenance markers for
  remembered-context vs explicit flags. JSON stdout stays clean; reads
  stay silent.
- **Write-verb flag consistency**: `relationship add/update` accept
  `--type`; `entity add --id` is optional when the props carry the schema
  primary key (conflicts fail naming both values).
- **`read_revision` on stats**: the stats surface carries the freshness
  counter as a first-class field; property-only updates provably advance
  it.
- **Compact JSON output**: `--json-compact` (or `CRUXIBLE_JSON_COMPACT=1`)
  emits single-line JSON through one central helper; pretty stays the
  default.

### Changed

- **CLI startup is ~8x faster on the read path**: lazy per-command
  loading (`--version`/`--help` ~60ms, previously ~500ms), and
  `cruxible_core.errors` no longer imports the HTTP client stack —
  exception identity across the core/client boundary is preserved via a
  shared dependency-free error base. Import-graph regression tests pin
  the win.

### Fixed

- **Kit bundles ship with every release**: the tag workflow deterministically
  rebuilds bundles, refuses digest drift from the committed manifest, and
  idempotently uploads them to the GitHub release. CI also checks every
  manifest URL once its version tag exists, preventing a published package
  from pointing all built-in kit aliases at missing release assets.

## 0.2.7 — 2026-07-20

### Added

- **Scoped daemon capability ceiling**: `cruxible server start
  --capability-ceiling <tier>` (or `CRUXIBLE_MODE`) fixes an immutable
  per-process permission ceiling using the existing tier hierarchy.
  Anonymous auth-off requests receive exactly the ceiling; bearer and
  relayed tiers are clamped to `min(token tier, ceiling)`, so a
  discovered admin token cannot exceed a lower ceiling. Config reload
  and in-place restart cannot alter it; refusals are typed (operation,
  required tier, ceiling) and identical across HTTP, CLI-against-daemon,
  and MCP; `/health` discloses the ceiling. Defaults unchanged when
  unset. Built for single-container agent deployments where the daemon
  boundary, not client curation, must carry enforcement.
- **Generic gate candidates**: gates of kind `generic` accept arbitrary
  caller-supplied candidate values from newline-delimited stdin or repeatable
  public `--candidate` arguments, enabling state-backed pre-action checks
  outside git. Empty input and terminal stdin fail closed; the hidden
  cross-kind `--value` diagnostic override is unchanged.

### Fixed

- **MCP union output schemas are object-rooted**: the four union-returning
  tools (`query`, `query_inline`, `list_queries`, `inspect_entity`) now
  publish `type: object` at the schema root alongside `anyOf`. Non-conformant
  roots made some MCP clients drop every tool on the server.
- **MCP tools/list no longer blocks on daemon reachability**: the advertised
  catalog is frozen from static metadata at server creation and server-mode
  tool calls run on worker threads, so listing answers immediately even when
  the daemon is down; individual calls fail per-tool with a clean error.
- **Dry-run validation parity**: invalid direct writes now raise the same
  `DataValidationError` in dry-run as in apply (previously dry-run buried
  validation errors in a success envelope with exit 0), across entity,
  relationship, and batch surfaces. Dry-run still mutates nothing and mints
  no receipt.

## 0.2.6 — 2026-07-18

### Added

- **Compact query catalog**: `query list` returns bounded summaries
  (name, mode, entry point, required params, result shape) instead of
  full definitions; `detail=full` preserves the previous payload and
  `query describe` stays the canonical detailed read.
- **Read output profiles**: a shared `compact`/`standard`/`full`
  serializer across query rows, inspect, get, sample, and list.
  `standard` is byte-identical to 0.2.5 and remains the HTTP default;
  MCP read tools default to compact identity cards that always preserve
  lifecycle and review markers (`CRUXIBLE_MCP_READ_PROFILE` overrides).
- **Bounded neighborhood inspection**: `entity inspect` gains multi-hop
  expansion with depth, direction, relationship/target-type filters,
  relationship-state visibility, property projection, and node/edge
  budgets with explicit truncation reasons. Expanded reads default to
  `state=all` per the inspection contract, and `edges_hidden_by_state`
  reports edges an explicit state filter suppressed.
- **Read revision and continuation**: a monotonic `read_revision`
  advances with every state-mutating commit (audit writes excluded) and
  rides every read envelope; list, catalog, and neighborhood reads
  accept opaque continuation tokens that fail with a typed 409 when
  state has moved; receipts pagination uses a keyset cursor. Silent
  truncation is gone: `sample` reports true totals, and empty pages
  with matches report `truncated`.
- **Graph layout for query output**: `layout=graph` returns each unique
  entity and relationship once with ordered result references and a
  compact path index; rows layout is unchanged and remains the default.
- **Agent-local working set (opt-in prototype)**: `--ws` or
  `CRUXIBLE_WORKING_SET=1` captures compact records of everything a
  JSON read returned into a grepable, credential-scoped JSONL cache;
  `cruxible ws path|status|verify|refresh|clear` manage it, `verify`
  checks freshness against the live revision and config digest, and the
  cache is never read by any write path. MCP capture is available via
  `CRUXIBLE_WORKING_SET_DIR` for co-located deployments.

### Changed

- Cold-start agent read cost on the in-repo read benchmark drops 86%
  end to end (methodology and raw results in `benchmarks/read_anchor/`).
- README restructured around a show-first fold; the full governed-domain
  walkthrough moved to `docs/deep-dive.md`.

## 0.2.5 — 2026-07-16

### Fixed

- **Tabular bundle loading tolerates optional columns**: JSON/JSONL
  reference bundles with columns that are null for the first hundred
  rows no longer crash canonical workflow ingest; schema inference now
  scans all rows.

### Changed

- MCP server instructions now document relationship truth-state
  semantics (live / accepted / pending / reviewable) so agents receive
  the review model without reading docs.

## 0.2.4 — 2026-07-16

Config composition lands: instances materialize from chains of config
layers (base kit → domain → overlay) instead of a single vendored file,
and every materialized config carries verifiable provenance.

### Added

- **Recursive N-ary config composition (`extends`)**: a config may extend
  multiple bases and bases may themselves extend, materialized with
  deterministic layering; ambiguous or conflicting layer identities in the
  chain are rejected rather than silently merged.
- **First-class default base kits**: a base kit role with an optional
  `requires_base` contract; `agent-operation` is the public init default,
  with an explicit `--bare` opt-out across CLI, MCP, HTTP, hosted runtime,
  and client surfaces. Base/domain/overlay ordering is validated and the
  composed base identity is reported.
- **Config provenance and `cruxible config status`**: every authored layer
  and its digest is recorded alongside the exact materialized bytes;
  generated active configs are stamped, source drift and hand-edits are
  detected (forged source manifests rejected), governed active configs are
  verified at daemon startup with an explicit recovery override, and
  provenance stays stable across kit repoints and checkout moves.
- **`judgment` proposal-policy preset** (agent-operation kit): planning
  judgments — e.g. work-item dependency edges — require maintainer
  rationale; source evidence is advisory rather than demanded.

### Changed

- **Overlay composition boundary preserved**: uploaded overlays keep their
  layer boundary through composition, so overlay edits cannot rewrite
  base-kit-owned config.

## 0.2.3 — 2026-07-12

Kit versions now track the release train: every bundled kit's manifest
version matches the release that ships it.

### Added

- **Frozen-property mutation guards (`type: frozen`)**: the guard grammar
  could only trigger on transitions *to* named values, so no property could
  be protected from *any* change. A frozen-property condition freezes the
  guarded property outright: updates that change it are refused while the
  entity's **stored, pre-write** state matches an optional `while`
  property=value clause — with no clause the property is immutable after
  create. Creates set the property freely and re-asserting the stored value
  is not a change. Because the clause reads before-state only, a single
  write that both leaves the freeze state and changes the frozen property
  (demote + retarget) is refused by design, and an update whose stored
  state cannot be read — or whose `while` clause value fails schema
  normalization — fails closed. Enforced at the shared guard
  chokepoint every entity write path runs through (`add_entity`,
  `batch_direct_write`, canonical workflow apply). Entity types only in
  v1 — config lint refuses freeze declarations on relationship types.
  Compact grammar: `freeze: <Entity>.<prop>` with an optional `while:`
  mapping. The agent-operation kit closes two holes with it:
  `ReviewRequest.change_head` is frozen while `status=approved` (an
  approved review's pin can no longer be retargeted to an unreviewed SHA
  under the merge-review gate) and `StateNote.kind` is immutable after
  create (a reviewer's rationale note can no longer be re-kinded to
  `scratchpad` to hide it from curated reads).
- **`gates` config view**: `cruxible config views --view gates` renders
  declared repo gates as a generated Markdown block (opt-in; not part of
  `--view all`). The agent-operation README now documents its
  `merge-review` gate with an authored Merge Gate section plus the
  generated block.

### Fixed

- **Kit catalog status is current**: `kits/README.md` now lists
  supply-chain-blast-radius and case-law-monitoring as `ready` — both ship
  working deterministic providers, pinned data, and worked demos, so the
  placeholder-provider caveat no longer applies.
- **kev-triage README no longer misstates the pipeline diagram**: the
  generated workflow-pipeline diagram is an inferred dependency ordering,
  not the onboarding order; the README says so and points at
  `docs/kev-guide.md` for the actual sequence.
- **kev-triage ships least-privilege MCP config**: `.mcp.json` now sets
  `CRUXIBLE_MODE=governed_write` instead of `admin`, with a README note
  that `group resolve` and initial canonical applies need a higher tier.

## 0.2.2 — 2026-07-12

### Added

- **`cruxible gate`: declared merge gates enforced from state**: a `gates:`
  config element declares named, kind-based gates — `{kind, entity_type,
  match_property, condition}`, where `kind` selects a source adapter that
  supplies the candidate values to check. `cruxible gate check <name>`
  evaluates a gate; the only v1 kind, `git-pre-push`, reads git's pre-push
  protocol and requires every parent of every pushed merge commit to be
  pinned by a matching entity in state, refusing the push otherwise (fail
  closed on any error). The agent-operation kit ships a `merge-review` gate
  (ReviewRequest / change_head / approved) so a repo can gate merges on
  approved reviews with a one-line pre-push hook. Doctrine: a *guard* blocks
  a write into state; a *gate* lets the world act only if state agrees.
- **Approval actor separation (`distinct_from_creation_actor`)**: mutation
  guards can now require that the acting actor differ from the actor that
  created the target entity — anchored on the creation receipt's
  server-derived actor identity, never on writable properties. Fail-closed:
  entities with no committed creation receipt or no recorded creation actor
  refuse the guarded transition, and create-with-guarded-value is always
  refused. The agent-operation kit's review-approval guard now combines its
  allow-list with separation, so the actor that files a ReviewRequest can
  no longer approve it. Consequence: importing records in an
  already-approved state is refused — land reviews as `requested` and
  approve under a second credential.

### Security

- **Feedback channel now honors write-tier boundaries**
  (wi-feedback-write-tier-bypass): a `governed_write` feedback `correct`
  could apply arbitrary edge property corrections to relationship types
  whose direct-write surface requires `graph_write`, and `reject`/`flag`
  could move an edge out of live review state with no actor identity under
  server auth. Corrections are now gated at the corrected relationship
  type's config-declared `write_tier` (default `graph_write`) across
  `feedback`, `feedback_batch` (strictest corrected type in a mixed batch),
  and `feedback_from_query` (target resolved from the receipt before the
  check), refusing with the same `PermissionDeniedError` as the direct-write
  facades. Under server auth, **every** feedback action (`approve` /
  `correct` / `reject` / `flag`) now requires a resolved actor identity —
  anonymous retraction ends alongside anonymous promotion. Auth-off local
  behavior is unchanged, as are governed corrections on types that declare
  `write_tier: governed_write`.

## 0.2.1 — 2026-07-11

### Added

- **Config-declared write tiers (`write_tier`)**: entity and relationship
  types may declare `write_tier: governed_write` to open their direct-write
  surface (`add_entity` / `add_relationship` / `batch_direct_write`) to
  `governed_write` actors. Undeclared types keep requiring `graph_write`;
  mixed payloads are gated at the strictest touched type; mutation guards
  and `write_policy` run unchanged after the tier check. Config lint
  rejects non-write tiers (`read_only`, `admin`) and tier declarations on
  `proposal_only`/`mint_only` types. See "Config-Declared Write Tiers"
  in the config reference.
- **agent-operation kit: scratchpad notes + Decision acceptance guard**:
  `state_note_kind` gains `scratchpad` — an implementer's mid-flight
  working state. StateNote and its attachment edges declare
  `write_tier: governed_write`, so implementer agents can write notes
  without `graph_write`. Curated note reads (`recent_state_notes`,
  `state_notes_for_work_item`, `state_notes_for_review_request`, and the
  bounded note sets of the context queries) exclude scratchpad notes; the
  new `work_item_scratchpad` query replays a work item's scratchpad notes
  in created order for mid-flight pickup. A new
  `decision_acceptance_requires_authorized_actor` mutation guard requires
  the `authorized-reviewer` actor to move a Decision to `accepted` —
  including create-with-accepted (proposed decisions stay writable at the
  normal tier). Trust boundary, on the record: the note surface (all
  kinds, creates and updates) is now governed_write territory — note
  content is governed_write-trust while verdicts and lifecycle stay
  actor-guarded; see the kit README's Note-Surface Trust Boundary.

### Fixed

- **Config reload refuses to strand stored graph records**: reloading a
  config that no longer declares entity or relationship types present in
  the stored graph used to succeed silently and break every read of
  those records. Reload now refuses before any write, listing the
  stranded types with counts; `--allow-orphans` proceeds explicitly and
  the response carries the stranding report. Every successful reload now
  reports its type delta, and a reload with a corrupted current config
  still works as the repair path (delta reported as unknown).
- **Snapshot clones are reachable on auth-enabled daemons**: cloning used
  to mint a new instance with no credentials at all — instance-scoped
  source credentials couldn't reach it and nothing could be claimed or
  recovered. The clone response now returns a one-time ADMIN credential
  for the new instance (same conventions as `credential claim-bootstrap`);
  auth-disabled daemons are unchanged.
- **Heterogeneous query returns are labeled correctly**: queries returning
  `AnyEntity` now project `entity_type` and `entity_id` for every row
  instead of mislabeling rows under the entry point's key, and composed
  configs that select keys from unresolved return types fail config lint
  instead of silently disabling the check.

## 0.2.0 — 2026-07-07

The first broadly usable release: hard state for AI agents — typed, governed,
receipted — with composable starter kits and a complete evidence loop.

### Added

- **Multi-kit compose at init**: `cruxible init --kit <base> --kit <overlay>`
  composes overlay kits over a base state in one instance under a unified
  `kits/<kit_id>/` layout; overlay resolution comes from kit manifests
  (`target_state`), with fail-closed namespacing and merged locks.
- **Evidence guard** (`require_evidence_on_support`): opt-in per signal
  source — a support signal carrying no evidence escalates to review and can
  never auto-resolve. All bundled kits opt in: every support verdict the
  shipped kits emit is evidence-backed by construction.
- **Source artifact loop, end to end**: caller-supplied deterministic ids on
  registration (`--id`, HTTP, MCP); a `register_source_artifacts` workflow
  step (canonical-only, content-is-data, idempotent re-runs); read routes for
  browsing registered documents and their chunks; CI-grade evidence
  discipline (quoted evidence locators are recomputed against pinned source
  texts on every test run).
- **Local admin recovery**: `cruxible credential recover-admin` ends the
  permanent-lockout failure when an admin token is lost — local-only, rooted
  in filesystem ownership, fully audited.
- **Case-law monitoring kit**: real Chevron-cluster corpus (11 public-domain
  opinion texts, digest-pinned, with verbatim-quote evidence locators),
  synthetic law firm, two-act bad-law demo, governed citator treatment edges.
- **Supply-chain blast-radius kit**: real VORON 2.4 BOM traced to pinned
  upstream artifacts, incident cascade with alternate-sourcing-aware
  verdicts, buffer-coverage arithmetic, differential product exposure.
- **LLM wiki import**: `scripts/import_markdown.py` plus a recipe
  (`docs/recipes/llm-wiki-to-instance.md`) — wiki pages register as pinned
  source artifacts, an agent proposes the typed state, every migrated claim
  keeps a citation into the page it came from.
- **Provider SDK**: blessed evidence-locator constructors, artifact JSON
  access, tri-state verdict vocabulary (`cruxible_core.provider.payloads`).
- Generated kit READMEs: provider contracts, schema catalog, overlay-scoped
  views, signal-policy catalog (including the evidence-guard column).
- State health: unevidenced-support counts scoped to guarded sources.
- `docs/state-resolution-and-maintenance.md`: how conflicts resolve, what
  each permission tier can touch, how state ages and gets repaired.

### Changed

- **The package is now `cruxible`** (was `cruxible-core`): `pip install
  cruxible`. The import remains `cruxible_core` for 0.2. Existing 0.1.x
  installs of `cruxible-core` are unaffected; a compatibility stub will
  follow.

- Utility workflow outputs pipe into strict contracts: core strips its own
  `source_metadata` envelope at workflow-input validation (undeclared extras
  are still refused).
- Providers never fetch: live acquisition moved out of kit providers into
  standalone scripts at the artifact seam; all bundled providers are pure
  functions over workflow data.
- Signal-policy config refuses unknown keys — a typo'd enforcement flag is a
  config error, not a silently disabled guard.
- `READ_ONLY` includes browsing registered source documents (list + full
  read), consistent with the existing dereference tier.

### Fixed

- Server-mode `relationship get` no longer drops trust metadata — approved,
  group-provenanced edges rendered as unreviewed/unattributed over HTTP.
- Seed evidence chunk pins recomputed with the artifact parser; drift is now
  a CI failure.

### Security

- Admin recovery reviewed adversarially (uid-rooted, lock-guarded, audited;
  recovery grants nothing filesystem ownership didn't already grant).
- Evidence guard reviewed adversarially, including fabricated-evidence
  attacks; workflow artifact registration is provably preview-safe (nothing
  persists before apply).
