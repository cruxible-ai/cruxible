# Changelog

## Unreleased

Every user-visible fix or feature adds its entry here in the same change
that lands it; entries move under a version heading when the release is
tagged. Work items for these changes live on the active release line in
the project's own state instance.

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
