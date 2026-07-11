# Changelog

## Unreleased

Every user-visible fix or feature adds its entry here in the same change
that lands it; entries move under a version heading when the release is
tagged. Work items for these changes live on the `release-0.2.1` line in
the project's own state instance.

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
