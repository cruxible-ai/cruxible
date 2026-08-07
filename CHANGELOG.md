# Changelog

## Unreleased

Every user-visible fix or feature adds its entry here in the same change
that lands it; entries move under a version heading when the release is
tagged. Work items for these changes live on the active release line in
the project's own state instance.

## [0.3.2] - 2026-08-06

- **Procedure reads show their run-ledger track record.** List and detail
  surfaces now attach a `track_record` block, so dead procedures are visible
  before an agent chooses one. Its verdict buckets are exhaustive — `succeeded`,
  `failed`, `refused`, `budget_exceeded`, and `in_flight` for started but
  unfinalized runs always sum to `runs` — so a procedure that exhausts its
  budget on every invocation reads differently from one whose invocations are
  still running. The block also carries `last_succeeded_at` and
  `top_refusal_reason`, the most frequent refusal classification. The summary is
  computed once for a whole list page. `linked_outcomes` remains reserved as
  null.

- **Procedure runs now advance the instance read revision.** The run ledger was
  classified audit-only, which was defensible while runs were readable only
  through their own listing. Deriving `track_record` from those rows makes them
  read state, so starting a run and finalizing one each bump `read_revision`,
  refusals included. Without this a procedure page could be read at one
  revision, a run could land, and the next page's continuation token would
  still validate against an unchanged counter — a paginated read spanning two
  states with nothing to detect it, and working-set records reading fresh while
  their buckets were stale. Continuation tokens and working-set freshness now
  react to procedure invocations the same way they react to any other write.

- **Refused procedure runs record why they were refused.** A `refused` run now
  persists a `refusal_reason` classification (`procedure_not_live`,
  `definition_digest_changed`, `tier_not_permitted`, `preflight_refused`,
  `precondition_evaluation_failed`, or `precondition_unsatisfied`) alongside its
  receipt, and procedure reads report the most frequent one. Existing instances
  gain the column on first open through storage migration
  `0005_procedure_refusal_reason`; runs refused before the upgrade keep a null
  reason and are excluded from the most-frequent count rather than being lumped
  into an "unknown" bucket that would outvote every reason observed since.
- **Core boundary traffic is measurable per instance.** HTTP routes, MCP tools,
  and locally invoked CLI service verbs now add call, error, serialized-response-byte,
  and total/maximum duration counters to one aggregate SQLite row per surface,
  without storing per-call events. `cruxible telemetry summary` and
  `GET /api/v1/{instance_id}/telemetry/summary` expose the counters and their
  earliest recorded timestamp at the read-only tier. Which surface a call lands
  under follows the boundary it actually crossed: **against a governed daemon,
  an MCP call reaches core over HTTP and is counted under the HTTP route name,
  so the MCP-tool dimension exists in local (direct-instance) mode only.** A CLI
  command records its emitted bytes and wall time under a `cli:<command>` row,
  while each service verb it invoked keeps its own measured duration. Refusals
  count as that instance's errors — permission-tier, ownership, and direct-write
  denials included; the one exception is a credential scoped to a different
  instance, whose refusal is not the addressed instance's traffic and is not
  counted against it. Recording never touches storage on the request path:
  observations aggregate in memory and a background flusher writes them, never
  waiting on a busy or unavailable store, so the underlying request result and
  timing are unchanged either way. A batch the store refuses is retried on the
  next flush rather than lost, and whatever capture genuinely could not keep is
  published on the summary as `dropped_observations` / `dropped_events` so an
  undercount is visible instead of silent. `cruxible server start` is excluded
  from CLI collection — the daemon's traffic is counted at the HTTP boundary
  that serves it.

- **Procedure proposals catch impossible input contracts before they enter the
  library.** Definition-time authoring lint now blocks a step reference such as
  `$input.transactions_arguments` when `contract_in` does not declare that
  field, naming the step, reference, and the contract's typed required/optional
  fields. A contract that sets `allow_extra` (including the built-in
  `cruxible.JsonObject`) accepts undeclared references, since the payload may
  legitimately carry them. This deliberately changes `propose_procedure`
  behavior: statically-wrong definitions that were previously accepted are now
  refused. The same lint also runs at accept time, so a proposal that was left
  pending before this change can now fail on `resolve --action accept` and must
  be fixed and re-proposed. The existing produced-alias check still blocks
  invalid `returns`. The lint reads only the step fields the runtime resolver
  itself walks, so literal prose that happens to quote a reference — an
  `assert` message telling an operator to supply `$input.foo` — is text, not a
  reference, and no longer blocks a definition that runs correctly.
  Non-blocking proposal warnings flag declared-but-unused inputs, read-implying
  names backed by side-effecting providers, stringified JSON-object step
  inputs, a whole declared string field handed to an `arguments` parameter
  (an opaque bundle the contract cannot validate), a procedure bundling reads
  with side-effecting steps or declaring more than five provider steps, and
  `max_provider_calls` headroom above the expanded provider-call count.
  `get_procedure` now returns `contract_in_schema` — the resolved input field
  shape (per-field defaults, enums, descriptions, and the nested `json_schema`
  a `json` field is validated against), the contract description, the
  `allow_extra` flag, and `input_example`: a worked payload carrying every key
  the caller must supply, which `cruxible procedure show` prints in human mode
  too. Run-time contract refusals are covered by the same typed
  required/optional schema echo, and both surfaces now share one requiredness
  rule — a field carrying a default is optional to supply, because contract
  validation fills the default before it ever checks optionality.

- **A Procedure author can withdraw their own pending proposal.** `withdraw`
  moves a pending definition to the new terminal `withdrawn` status through the
  same receipted transition as accept/reject, at the proposing
  (`governed_write`) tier — withdrawing another actor's pending proposal is a
  review act and is refused below `graph_write` with a typed
  `ProcedureWithdrawalRefusedError` naming the rule. `reject` stays distinct as
  the reviewer's verdict with its required reason; a withdrawal's reason is
  optional, and the terminal status records which of the two happened. A
  withdrawn definition is not live, so its name is immediately free to
  re-propose, and the refusal to supersede a still-pending definition now
  points at the new verb instead of leaving authors to invent renamed variants.
  Available as `cruxible procedure withdraw`, the
  `cruxible_withdraw_procedure` MCP tool, and
  `POST /procedures/{procedure_id}/withdraw`.

- **`cruxible batch-direct-write` shows identity warnings again.** The command
  printed neither a dry-run's nor an applied write's `identity_hint` matches in
  its human output, so a batch duplicating an existing entity's declared
  identity looked clean at the terminal while the `--json`, HTTP and MCP
  results all carried the warning. The command now uses the shared result
  emitter, and the preview surfaces the same warnings the apply would.

- **The empty `server` extra is gone.** `fastapi`/`uvicorn` moved into the base
  dependencies some releases ago, leaving `cruxible[server]` an extra that
  installed nothing; the runtime Dockerfile still asked for it. Nothing about
  what gets installed changes — `pip install cruxible` has shipped the daemon
  either way — but `pip install "cruxible[server]"` now warns that the extra
  does not exist instead of silently resolving. Drop the `[server]` suffix.
  Whether the server stack should move back out of the base install is a 0.4
  packaging decision and is deliberately not attempted here.
- **Rejected writes now teach the caller how to fix them.** Four authoring
  error classes became self-correcting, each measured as wasted retries in an
  agent benchmark run:
  - Datetime rejections (`observed_at` and every other typed temporal field)
    echo the accepted format with a copyable example, on both the HTTP request
    validation path and the runtime API argument checks.
  - Contract rejections naming an unexpected or missing field also list the
    contract's declared fields with type and required/optional (sorted,
    truncated past 40 with a count), so procedure and workflow inputs can be
    fixed in one edit.
  - Dangling-endpoint rejections name the recovery available at the entry
    point that raised them: a batch direct write can carry the entity, an
    attestation cannot.
  - Procedure tier refusals name the provider whose `procedure_access` forced
    the effective tier, and list the `declared_tier` values that clear it.

- **MCP tool descriptions describe the loaded kit.** Query tools name the
  config's named queries; workflow and procedure tools name its registered
  providers and contracts (with a short field preview), so an agent discovers
  the authoring vocabulary from the tool surface instead of prompt
  enumeration. Lists are truncated with a total. Tool schemas do not vary by
  kit. The kit is resolved from local state only — `CRUXIBLE_MCP_KIT_CONFIG`,
  otherwise the sole registered local instance and only in local mode — so
  `tools/list` still answers with no reachable daemon, falling back to the
  static descriptions. A server pointed at a remote daemon describes only what
  `CRUXIBLE_MCP_KIT_CONFIG` names, never a local instance that merely shares
  the host.

- **The hosted runtime image has a repeatable GHCR publish pipeline.** A
  dispatchable workflow builds `deploy/runtime/Dockerfile` from a named
  reviewed commit — refusing to continue unless the checkout is exactly that
  SHA — and pushes it under the immutable tag
  `runtime-<version>-<sha12>` with OCI source, revision, version, and created
  labels. `latest` is never published or moved: an already-published tag is
  reused rather than rebuilt, only an explicit registry "absent" authorizes a
  push, and a tag whose image was built from a different revision fails the
  job. The run summary and job outputs carry the image digest, and a
  post-push job pulls that digest and runs the runtime image suite against
  the published artifact via the new `CRUXIBLE_RUNTIME_IMAGE_REF` test
  override. Deployments pin the digest, not the tag.
- **Procedure blueprints have a document format.** A blueprint is a portable,
  digest-addressed document that packages a procedure library: its own fully
  qualified contracts, its reference-state/ontology dependencies, its query
  slots (read sockets that install a default named query), its compute slots
  (swappable stages declared by contract, with billing-mode compatibility
  constraints and an opt-in outcome-metric hook), and its procedures. The new
  `cruxible_core.blueprint` module parses and validates a document, computes a
  content digest over a canonical form plus an ordered attachment manifest, and
  lowers it into the artifacts an installer submits: a config-overlay fragment
  and concrete `ProcedureDefinition`s with slot references resolved from a
  caller-supplied binding map, checked against a caller-supplied provider
  catalog. Binding is fail-closed: a provider missing from the catalog is
  refused rather than assumed compatible, and a bound provider must match the
  slot's contract names, intersect its billing modes, and claim every
  capability tag it requires. Refusals are typed and field-pathed — one issue
  per violated constraint — and an unbindable slot lists the near-matching
  providers and why each failed. This
  release ships the artifact only — there is no installer, no trigger runtime,
  and no binding registry. `triggers:` and `pipelines:` parse and validate but
  refuse to lower; `invocation: manual` procedure libraries are the executable
  slice. Format reference: `docs/blueprints.md`.

## [0.3.1] - 2026-08-05

- **Entity types can declare deterministic identity keys at write time.**
  `identity_hint` returns a structured same-type duplicate warning without
  blocking the write, `unique_by` rejects normalized duplicates while naming
  the existing entity ID (including identity-changing updates), and
  `id_pattern` enforces per-type ID conventions. The shared normalization
  NFC-normalizes, case-folds, trims and collapses whitespace, and deletes
  punctuation; direct add/batch `identity_warnings` surface through both HTTP
  and MCP results. Matching scans same-type entities only and does not merge or
  perform semantic matching.

- **Ontology inspection is authoring-complete.** The canonical ontology view
  now exposes compact config-like entity and relationship property contracts,
  configured write policies, and stored instance counts, so an agent can author
  valid writes from the view alone. The request and response envelope is
  unchanged; CLI and MCP guidance updated.

- **Invalid Procedure definitions return typed validation errors.**
  `propose_procedure` surfaces definition-shape failures as structured 400
  responses with field-path messages on both the HTTP and MCP surfaces,
  instead of opaque server errors.

- **Unknown-provider Procedure errors list the registered providers.** The
  rejection names the valid provider set (sorted, truncated past 40 with a
  count), so an agent can self-correct instead of retrying blind.

- **Procedure runtime reference failures are typed and auditable.** Accepted
  definitions that cannot resolve a step reference now return a structured 400
  `QueryExecutionError` naming the failing step and reference, while atomically
  finalizing the procedure run as `failed` with its failure receipt. Failures
  that escape a step handler without an identified reference are typed the same
  way but name only the step id and kind — no reference is guessed. Procedure
  previews also reject `returns` values that are not produced output aliases.

- **Demo states publish to GHCR as immutable OCI bundles.** The hosted
  runtime image packages ORAS 1.3.2, the state-ref catalog gains the
  `banking-crux-demo` alias, and the publication recipes publish one release
  bundle under a dated immutable tag and retag that exact manifest to
  `latest` via `oras cp`, so the two references can never diverge. Recipes
  document digest-equality verification and the never-republish-a-dated-tag
  rule.

- **Registered source evidence now has compact, server-minted citation
  handles.** Registration, source-artifact list/get responses, and canonical
  `register_source_artifacts` workflow output expose stable revision and chunk
  handles. Relationship and governed-group writes accept `citation_handles`
  beside the unchanged explicit `source_evidence` form; Cruxible resolves them
  to the same full revision-pinned `EvidenceRef` before mutation guards run and
  computes artifact/chunk hashes from the registration. Handles are never
  floating aliases: superseded handles fail as `stale`, and unknown or
  digest-colliding handles fail as `unknown` or `ambiguous` rather than being
  dropped or guessed. Evidence is attached only when a write explicitly passes
  a handle.

## 0.3.0 — 2026-07-29

### Changed (BREAKING)

- **The `wiki-to-state` skill and synthetic wiki-import demo are removed.**
  Their corpus-conversion framing encouraged broad restatement of documents as
  graph state. Markdown remains a supported, content-hashed source-artifact
  format, and `scripts/import_markdown.py` remains as a deterministic batch
  registration helper; only operational claims and procedures that need an
  explicit lifecycle or executable consequence should be promoted from source
  evidence into governed state.

- **Claim feedback now uses `accept` instead of `approve`** across the CLI
  (`feedback record`, `feedback from-query`, and batch item actions), MCP tool
  schemas, HTTP request models, service inputs, and client contracts. During
  0.3, `approve` remains a deprecated input alias: it emits the standard
  structured warning and delegates to `accept`; it is removed in 0.4.0. New
  feedback rows store `accept`, while historical 0.2.x rows containing
  `approve` remain readable. The stored relationship review status remains
  `approved`; this is a public verdict rename, not a storage-status migration.

- **The `flag` feedback action is removed from the live write vocabulary** —
  from the canonical vocabulary on every surface (service, CLI
  `feedback --action`, MCP tool schema, HTTP request models, client contracts).
  As shipped it un-approved an edge to
  `pending` while storing no annotation, destroying the reviewer's actual
  signal at the moment it was given. Historical `flag` rows written by 0.2.x
  instances remain fully readable (the stored-record vocabulary still admits
  them; they render and move nothing). Submitting `flag` now refuses with a
  teaching message. During 0.3 it remains accepted as a deprecated refused
  alias so old callers receive the structured `{surface, replacement,
  removal_version}` warning rather than a schema-level unknown-value error; the
  alias never reaches a mutation and is removed in 0.4.0.
  **Migration:** record a doubt with
  `cruxible attest record --stance contradict` (MCP: `cruxible_attest`) — it stores
  the observation, its evidence refs, and its actor without touching review
  status; adjudicate with `accept`/`reject`/`correct`. Note the tier
  consequence: every remaining feedback action requires `GRAPH_WRITE`, so no
  feedback action completes at the `GOVERNED_WRITE` floor any more.

- **The self-declared `human`/`agent` axis is retired**: `FeedbackRecord.source`,
  `OutcomeRecord.source`, `GroupResolution.resolved_by`,
  `CandidateGroup.proposed_by`, `DecisionRecord.opened_by`, and
  `make_group_proposal`'s `proposed_by` were all caller-supplied, defaulted to
  `"human"`, and were never reconciled with `actor_context.actor_type`. They were
  not inert: the feedback and outcome profiles require a `reason_code` only for
  non-human writers, so an agent could skip the accountability rule written for
  it simply by declaring itself a person. Every one of those declarations now
  carries no signal: the matching `source` / `proposed_by` / `resolved_by` /
  `opened_by` parameters are dropped from the service functions and the runtime
  facade outright, while the MCP tools, the HTTP request models, the CLI
  (`--source`, `--opened-by`), and the client accept them as deprecated inputs
  that are ignored with a warning through 0.3 (removed 0.4.0 — see
  *Deprecated*). Readers derive the value from the actor context.

  The READ-side field names survive as DEPRECATED derived projections (see
  *Deprecated* below): `FeedbackRecord.source`, `OutcomeRecord.source`,
  `GroupResolution.resolved_by`, `CandidateGroup.proposed_by`, and
  `DecisionRecord.opened_by` are re-emitted, computed from
  `derived_actor_kind(actor_context)`. What is gone is the ability to DECLARE
  them. The retired request fields are accepted and ignored with the standard
  `{surface, replacement, removal_version}` warning rather than rejected.
  During 0.3 the removed parameters and hidden CLI flags remain deprecated
  input aliases across Python, CLI, MCP, HTTP, and client surfaces; their values
  are never honored, and the aliases are removed in 0.4.0.

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

- **Feedback adjudication requires `graph_write`**: `feedback accept`,
  `reject`, and `correct` decide a claim's fate — they make a non-live
  edge live, or retract one — so they now require `GRAPH_WRITE` even
  though the `cruxible_feedback` / `_batch` / `_from_query` tools
  themselves stay at `GOVERNED_WRITE`. Previously a single
  `governed_write` actor could stage an edge (attestation on an absent
  claim, or a `pending` write) and then accept its own proposal, reaching
  a live approved claim on a `proposal_only` type with no reviewer above
  it. The tools stay callable at `governed_write` so canonical actions reach a
  receipted `PermissionDeniedError` (HTTP 403) naming the required tier, but no
  feedback action completes at that floor. The former `flag` exception is now
  only a deprecated compatibility input; it refuses with the structured
  replacement warning on every write tier.

  **Migration:** any caller that accepts/rejects/corrects with a
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
  accepted state (`accept` / `correct`) are refused alongside the direct
  write verbs, so freezing live writes can no longer be walked around via
  feedback. `reject` stays available because it moves an edge *out* of live
  state. The deprecated `flag` compatibility input refuses independently and
  never reaches this kill-switch.

- **Four MCP tools now return an object-rooted `{"result": ...}` envelope**:
  `cruxible_query`, `cruxible_query_inline`, `cruxible_list_queries`, and
  `cruxible_inspect_entity` each return a UNION of contract models, which
  derives to an `anyOf` at the schema ROOT. The MCP specification requires a
  tool's `outputSchema` to be object-rooted, and strict clients reject an
  `anyOf` root outright. The previous schema papered over this by pinning
  `"type": "object"` beside the `anyOf` root — a lie about a schema that had no
  `properties` and validated as an alternation. The union now sits under a
  required `result` property, which is both the correct shape and the shape
  FastMCP already generated for `cruxible_state_diff`, so all five union tools
  share one convention.

  The envelope applies to **both** halves of the tool response: the
  `structuredContent` object and the JSON in the text content block. Payloads
  inside `result` are byte-identical to what the handler produced before — only
  the nesting changed.

  **Migration:** read `payload["result"]` where you previously read `payload`.

### Added

- **Attestations: an observation channel separate from adjudication.**
  `cruxible attest record|list|queue|resolve` (MCP `cruxible_attest*`, HTTP
  routes, client methods) records one actor's dated, immutable observation
  about one claim tuple — stance `support`/`contradict`/`unsure`, with
  evidence refs and a note — without touching the claim's review status.
  `support` on an absent tuple creates a pending claim when both endpoints
  exist; `contradict`/`unsure` refuse to conjure the claims they dispute.
  `attest queue` surfaces live claims with open current-content
  contradictions; `attest resolve` appends a reviewer disposition
  (`upheld`/`corrected`/`invalidated`) while the original observation stays
  intact. Writes take an optional `idempotency_key` (actor + tuple scoped)
  for safe retries. This is the replacement the removed `flag` action points
  at: doubt becomes recorded signal instead of destroyed review state.

- **Resolution contracts: commit to the outcome before the acceptance.**
  `cruxible outcome open|resolve|dispose|list|due` (plus MCP/HTTP/client
  surfaces). `outcome open` declares, on a not-yet-accepted subject, what
  result would count — a free-text criterion, a check time, an expiry, and a
  pinned measurement (a named query frozen at definition digest AND execution
  options, or a set of attestations). Contracts are activated only by a
  `requires_resolution_contract`-guarded acceptance; a contract nothing
  activates expires unanswered. `outcome resolve` records
  `satisfied`/`contradicted`/`indeterminate` under evidence-clock discipline
  (the resolving receipt's or attestation's own timestamps settle timing,
  never the caller's word; a drifted measurement query leaves only
  `indeterminate`). One standing resolution per contract; `outcome dispose`
  upholds or overturns, an overturn re-opening exactly one further answer.
  `outcome due` is the attention surface (`due`/`overdue`/`contradicted`).
  The legacy `outcome record`/`outcome profile` functions are deprecated
  toward this (see *Deprecated*).

- **The loop surfaces are public HTTP contract.** All 22 previously hidden
  routes — procedures, attestations, outcome contracts, lifecycle verbs, and
  state diff — are exposed and pinned in the `http_surface` snapshot, and
  `cruxible-client` 0.3.0 ships a method for every one of them with its core
  pin aligned. What shipped as internal loop machinery during 0.2 is now
  surface area with compatibility obligations.

- **`cruxible kit repin`: first-class acceptance of intentional kit edits.**
  Editing a materialized kit used to strand the instance behind a digest
  mismatch unless `CRUXIBLE_KIT_DEV_RESOLVE=1` waved every check through.
  `kit repin` recomputes and re-records the runtime digest for a deliberate
  edit, making "I meant to change this kit" a receipted acceptance rather
  than an environment variable; the env override remains for CI.

- **Structured deprecation mechanics.** A dependency-free registry now owns the
  common `{surface, replacement, removal_version}` warning shape. CLI aliases
  emit one stderr line, MCP results use an existing `warnings` field or an
  additive `deprecation_warnings` key, and HTTP responses carry a `Deprecation`
  header (plus a body entry only for contracts that already expose `warnings`).
  `DEPRECATIONS.md` is the removal schedule, guarded against registry drift.

- **An agent-local working set: opt-in, non-authoritative read cache.**
  With capture enabled (`--ws` on supported `--json` reads, or
  `CRUXIBLE_WORKING_SET=1`), every entity and edge a read returns is also
  appended, in the compact profile, to a per-instance JSONL file under
  `~/.cruxible/working-set` — so re-finding a fact costs a grep instead of a
  re-query. Records carry the `read_revision` and config digest they were
  captured at; `cruxible ws status|verify|refresh|clear|path` manage the
  cache with honest freshness classification (fresh/stale/unknown — missing
  coordinates are never fresh). Credential-scoped instance keys keep
  different bearers' caches separate, the whole path chain is
  symlink-refusing and permission-tightened, and no write path or other
  command ever reads the cache. An opt-in prototype: records are hints to
  re-verify, never proof.

- **Working-set capture fidelity + control-plane catalog.** The agent-local
  working set gains persisted activation (`cruxible ws enable|disable` in
  the CLI context; precedence `--ws` > `CRUXIBLE_WORKING_SET` > persisted),
  a deterministic digest-stamped `catalog.jsonl` (`ws catalog`, regenerated
  by `ws refresh`) indexing entity types, relationship types, named queries,
  and state-held governed procedures, projection-preserving capture
  (explicitly projected scalar fields survive, bounded at 64 keys; edge
  corroboration retained), and one `config_status` read per process instead
  of per capture. Supersede merges props ONLY when both records carry the
  same concrete revision and config digest — cross-coordinate supersede
  replaces wholesale so stale values are never stamped fresh. **Upgrade
  note for 0.3 pre-release dev builds only:** working-set records captured
  on a build between the initial fidelity merge and the same-coordinate
  guard could carry merged stale fields that verify fresh; run
  `cruxible ws clear` once per affected context (the verb operates on the
  current context only; no released version wrote such files).

- **The KEV kits adopt the 0.3 mechanics.** `kev-triage` gains a
  `TriageDecision` type carrying the `outcome_tracking` convention and the
  first shipped `requires_resolution_contract` mutation guard: accepting a
  decision that tracks its outcome refuses until a resolution contract has
  committed, in advance, to what result would count. `not_applicable`
  remains an explicit opt-out, and `outcome_tracking` is frozen at proposal
  time so the accepting write cannot flip it past the guard. Two named
  queries land with it — `exposed_services` (from one CVE, traverse
  product → host → service: candidate reachability/blast radius as an
  auditable PATH — posture edges decorate the rows but do not filter them,
  so the posture-filtered work queues remain
  `asset_vulnerability_postures_requiring_action` and `owner_patch_queue`)
  and `open_triage_queue` (decisions still awaiting a reviewer, the read
  that pairs with the contract queues). `kev-reference` now registers the CISA
  KEV feed snapshot as the revisioned source artifact `cisa_kev_catalog`
  and pins every reference claim's evidence to `cisa_kev_catalog@{revision}`
  with a heading-path locator, so "which settled decisions cite evidence
  that has since changed" is a lookup rather than an investigation.

- **`register_source_artifacts` reports the revisions it wrote.** The step
  output gains `revisions` (`{artifact_id: artifact_revision_id}`), which is
  what lets a later step in the same workflow stamp `{id}@{ordinal}` onto
  the evidence refs it mints. Without it a workflow could only cite the
  LOGICAL artifact id, and an unpinned ref dereferences against whatever
  revision happens to be current — the exact silent staleness the revision
  pin exists to prevent. `source_artifact_evidence_ref` gains matching
  `artifact_revision_id` and `heading_path` arguments so kits spell the pin
  and the revision-stable locator the same way.

- **Opening a resolution contract requires an outcome guard on the
  subject's type**: `outcome open` (and its MCP/HTTP equivalents) refuses
  unless the config declares a mutation guard whose condition is
  `requires_resolution_contract` (compact sugar
  `require: {resolution_contract: true}`) and whose `entity_type` is the
  subject's. A contract opened on an uncovered type was provably inert —
  activation intents are minted only inside guard evaluation, so it could
  never be activated by an acceptance, appeared in no attention queue,
  and silently expired unanswered. The receipted refusal names the guard
  to declare. Coverage is checked at the type level only (a guard's
  `where` clause reads the candidate at write time and cannot be
  evaluated at open, so a guard scoped to a subset of the type counts as
  coverage), and idempotent replays of contracts opened while a guard
  existed stay replayable if the config later drops the guard.

- **`config validate` lints outcome-guard coverage**: validation now emits
  a WARNING when an entity type declares an `outcome_tracking` property
  and no `requires_resolution_contract` guard covers that type — the
  config says the type's decisions are outcome-tracked while nothing
  enforces it. A warning rather than an error: a config with no contracts
  at all is fine, and the adoption property may legitimately land a
  release before the guard. `outcome_tracking` is the adoption
  convention this release introduces (the guard teaching messages spell
  it); a kit expressing the same adoption choice under another property
  name is out of the lint's reach by design, where the guard's `where`
  clause is the source of truth.

- **Claims have a stable identity (`claim_id`)**: edges now carry a minted,
  opaque, immutable `claim_id` that survives pull-apply, snapshot/clone,
  publish→pull, and backup/restore. `edge_key` is demoted to what it always
  was — a per-load key, the wire disambiguator, and the ordering token — and
  is no longer identity. `claim_id` is exposed additively on edge payloads and
  accepted as an optional target disambiguator on attestation and feedback
  targets, where it takes precedence over `edge_key`; supplying both with
  disagreeing values is refused rather than silently resolved.

  **Upgrade notes.** A storage migration (`0004_claim_identity`) rebuilds
  `graph_relationships` around the new key on first open; it is atomic and
  lock-serialized, so a concurrent reader sees the old schema or the new one,
  never a half-upgraded table.

  **One-time working-set duplicate.** The working set is a persistent JSONL
  cache that used to dedupe on `edge_key`. It now dedupes on `claim_id` when
  present. Entries cached BEFORE the upgrade carry no `claim_id`, so an edge
  that is re-added after the upgrade can appear twice in the working set until
  its next refresh — once under the old `edge_key` identity and once under its
  claim identity. This is cosmetic, affects only the local cache (never graph
  state, receipts, or query results), and self-heals on the next working-set
  refresh; `cruxible ws refresh` clears it immediately.

  **Backup format 2.** New backups write their manifest as
  `backup-manifest-v2.json` rather than `manifest.json`, so a pre-identity
  Cruxible refuses the artifact at verification (as a missing required file)
  instead of installing a state database it cannot read. Backups written by
  earlier versions still restore normally.

  **Repairing a damaged upstream.** Re-applying the release an overlay already
  tracks is now refused as a no-op, so the documented repair for a locally
  damaged materialized upstream moved behind an explicit flag:
  `cruxible state pull-preview --repair` then
  `cruxible state pull-apply --repair --apply-digest ...`. Repair preserves
  claim ids.

- **Compact query payloads shed derivable include bytes.** Under
  `profile="compact"` only: configured-but-empty include aliases are omitted
  from query rows, retained include envelopes drop fields derivable from the
  map key, item list, or defaults (`exists`, null `limit`, false
  `truncated` — cardinality and counts stay explicit), and the graph layout
  interns repeated non-empty include maps into a top-level `include_sets`
  table that result refs index by integer. A deterministic five-result
  equivalent shrank 77.8% (7,895 → 1,751 bytes). Standard and full profiles
  are byte-identical to 0.2.x; the only shared-contract change widens graph
  `includes` to `dict | int`, which still accepts every prior payload.

### Security

- **MCP `tools/call` bypassed tool curation AND permission mode at the protocol
  seam.** The advertised surface was filtered only where in-process callers
  looked: `tools/list` returned the curated catalog, but the low-level
  `tools/call` handler dispatched straight into the FastMCP tool manager. Any
  client that knew a tool name — the names are public — could invoke a tool the
  server had excluded, regardless of `CRUXIBLE_MCP_PROFILE`,
  `CRUXIBLE_MCP_TOOLS`, or `CRUXIBLE_MODE`, because
  `advertised_tool_names()` is the only place either filter is applied to the
  tool surface.

  **Precise escalation.** LOCAL execution was still refused in depth:
  `runtime.api` calls `check_permission` inside every gated operation, so a
  local-mode call landed on that floor and a read-only server stayed read-only.
  The REMOTE dispatch path had no equivalent floor. All 92
  `_dispatch_remote_or_local` call sites forward to the HTTP client without any
  local permission check — there is not a single `check_permission` call in
  `mcp/handlers.py` or `mcp/tools.py`, and the remote branch never enters
  `runtime.api`, which is where the mode is enforced. The daemon authorizes
  what its own credential permits. So an MCP server started at
  `CRUXIBLE_MODE=read_only` and pointed at a `graph_write` or `admin` daemon
  could execute writes over the wire; the client-side mode that was supposed to
  hold that line was never consulted.

  The gate now sits on `ToolManager.call_tool`, the single chokepoint both the
  protocol handler and `FastMCP.call_tool()` reach, so the two seams cannot
  drift apart. Refusals name the tool, the reason (profile / allowlist /
  permission mode), and the environment variable that widens the surface.
  Regression coverage drives a real `ClientSession` over the wire rather than
  calling `list_tools()` in process, which is precisely why the original bug
  went unseen. Because the gate wraps private FastMCP internals,
  `validate_runtime_tools()` now pins those seams' signatures and asserts the
  wrappers are installed, so an `mcp` package bump fails at startup with a
  named reason instead of silently un-wrapping a security gate.

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

  RULING (maintainer, 2026-07-25) on the marker's semantics, applied to both sites:
  `group_approval_drift` reflects divergence RIGHT NOW. It is recomputed against
  the approved content on every write and DROPPED when the content fully matches
  the approval again; a partial revert lists only the properties that still
  diverge. The approved baseline is still carried forward across writes (so the
  record says what the GROUP approved, not what the edge said last time). The
  previous accumulate-only behavior left a permanent stain: an edge that had been
  edited and then exactly restored still read as drifted forever. History of each
  excursion lives in receipts, not in live state.

- **Re-approving an edge makes the newly blessed content the drift baseline.**
  The third write path for the marker is `resolve_group --stamp-existing`, which
  blesses a surviving edge with the approving group's review and provenance. It
  copied the assertion with only `review` replaced, so a marker raised under
  group A survived group B's approval verbatim: the edge reported drift against
  a group that no longer owned it, over content B had just signed off on. The
  marker is now cleared on re-approval, which is the same ruling as above —
  divergence is measured against the NEWEST approval. (Approval never applies a
  proposed property set over a surviving edge; a member whose tuple is already
  live is skipped, so the blessed baseline is always the edge's current content.)

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

- **Config mutations are undone if their receipt does not commit.**
  `add_constraint` / `add_decision_policy` replaced the YAML immediately, while
  the receipt only became durable when the mutation-receipt boundary committed on
  exit — so a commit failure rolled back SQLite and left the ACTIVE rules changed
  with nothing naming who changed them. The prior bytes (and config provenance)
  are captured and restored on any failure inside the boundary.

- **Source-artifact drift history is no longer erasable by restoring the file.**
  `record_content_drift` cleared both stored fields on a clean read, so an
  artifact that was altered and then put back read as pristine — invisible to
  exactly the reader who needs it, someone auditing whether the evidence behind a
  decision was tampered with. Current drift state still clears (a stale marker on
  a restored file would misreport the evidence base), but a sticky
  `first_drift_observed_hash` / `first_drift_observed_at` pair is written once on
  the first drift and never cleared. Additive columns, migrated in place.

- **Replaying a pinned citation no longer manufactures a tamper record.** A
  revision-pinned dereference of a SUPERSEDED revision under the default
  `manifest_only` retention fell through to the artifact's local path — which now
  holds the NEWER revision's bytes. The hash mismatch was guaranteed and meant
  nothing, but the read reported `drifted` and recorded it, permanently stamping
  the sticky `first_drift_observed_hash` / `_at` pair on a revision nobody had
  touched. `DereferenceStatus` gains `revision_bytes_not_retained` for this case
  and no drift is recorded. Archived revisions are unaffected: their bytes are
  retained and still replay as `available`.

  **Migration:** a caller switching on `status` should treat
  `revision_bytes_not_retained` as "cannot serve this revision's bytes" (like
  `unavailable`), NOT as evidence of tampering. Register with
  `source_retention="archive"` when pinned citations must stay replayable.

### Documented

- **Under auth-on, every credentialed actor derives to `agent`** (maintainer
  ruling, 2026-07-25). A runtime credential is a `service_account`, so there is no way to
  be a human on an auth-on daemon today — and that is not an exemption: an actor
  deriving to `agent` owes a `reason_code` wherever a feedback or outcome profile
  requires one of non-human writers. Human-typed credentials (established at mint
  time, not declared per request) are the future path; the retired self-declared
  `human`/`agent` axis is not reopened. Recorded in
  `docs/runtime-auth-and-agent-roles.md`.

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
  `source` / `proposed_by` / `resolved_by` / `opened_by` to a mutating surface
  emits the standard structured deprecation warning instead of rejecting or
  silently dropping the value. It is never honored — the kind is derived from
  `actor_context`.
- **`StateHealthGroupsSection.auto_resolved_count` returns, always 0.** An honest
  zero: no path can grow that bucket any more. Read `withdrawn_count`.

### Fixed

- **Two KEV goldens were permanently unstable.** The golden
  cross-section's generated-id normalizer matched a 12-hex-character
  suffix, but claim ids mint 16, so raw `CLM-<uuid4>` values passed
  straight through into byte-compared golden files
  (`asset_exposure_workflow.json`,
  `exposure_reconciliation_workflow.json`). Those two files could not
  match on any re-run, and the resulting churn read as real drift on
  every regeneration. The pattern now accepts a 12-16 character suffix
  and claim ids tokenize as `<CLAIM_N>`. Test-support only; no runtime
  behaviour changes.

- **The MCP tool listing no longer depends on the daemon.** A missing or
  invalid transport (`CRUXIBLE_REQUIRE_SERVER` set with neither
  `CRUXIBLE_SERVER_URL` nor `CRUXIBLE_SERVER_SOCKET`, or both set at once)
  aborted `create_server()`, so the MCP process died before it could answer
  `tools/list` and agent hosts saw an empty surface or hung waiting for one.
  The listing is now static — built from local metadata at construction, never
  touching a call path — and the transport failure is carried to the call that
  actually needs the daemon. Those refusals teach: what to set, how to start a
  daemon, and that a static listing is not evidence the daemon is reachable.

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
