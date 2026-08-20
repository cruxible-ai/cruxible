# DP-0 destructive-convergence review guide

This is the cumulative review guide for DP-0A through DP-0E. It records the
public product left after the destructive pass, the legacy code deliberately
deleted, and the donor behavior deliberately retained for the Claims +
Procedures convergence batches. It describes the `playbill` branch at DP-0E;
later PC batches must update it when they remove donors or change the public
surface.

DP-0 does not implement first-class Claims or the successor Procedure runtime.
It establishes a Playbill-only served core and keeps only the legacy behavior
needed as a parity oracle for those transplants.

## Surviving public command inventory

The public CLI has these leaf commands and no legacy graph/config/kit command
groups:

```text
context clear
context connect
context show
context use
credential claim-bootstrap
credential list
credential mint
credential recover-admin
credential revoke
credential rotate
playbill body store
playbill claim explain
playbill claim get
playbill claim history
playbill claim list
playbill claim propose
playbill claim-type get
playbill claim-type list
playbill claim-type propose
playbill discover
playbill coverage resolve
playbill coverage status
playbill document body
playbill document get
playbill document history
playbill document list
playbill document propose
playbill expand
playbill explain
playbill floor export
playbill host create
playbill init
playbill principal list
playbill principal recover
playbill principal revoke
playbill principal rotate
playbill proposal activate
playbill proposal approve
playbill proposal inspect
playbill proposal refusal
playbill proposal review
playbill query get
playbill query list
playbill query propose
playbill query run
playbill sources check
playbill sources compile
playbill sources propose
playbill subject get
playbill subject history
playbill subject list
playbill subject propose
server info
server restart
server start
server status
```

The four top-level groups are therefore `context`, `credential`, `playbill`,
and `server`. See the [CLI reference](cli-reference.md) for arguments and
operator semantics.

## Surviving public route inventory

FastAPI also serves its standard OpenAPI endpoints (`/openapi.json`, `/docs`,
`/docs/oauth2-redirect`, and `/redoc`). The product routes are exactly:

```text
GET  /health
GET  /version
POST /api/v1/runtime/instances
GET  /api/v1/server/info
POST /api/v1/server/restart
POST /api/v1/{instance_id}/runtime/bootstrap/claim
GET  /api/v1/{instance_id}/runtime/credentials
POST /api/v1/{instance_id}/runtime/credentials
POST /api/v1/{instance_id}/runtime/credentials/{credential_id}/revoke
POST /api/v1/{instance_id}/runtime/credentials/{credential_id}/rotate
POST /api/v1/{instance_id}/playbill/init
POST /api/v1/{instance_id}/playbill/bodies
GET  /api/v1/{instance_id}/playbill/documents
POST /api/v1/{instance_id}/playbill/documents/proposals
GET  /api/v1/{instance_id}/playbill/documents/{identity}
GET  /api/v1/{instance_id}/playbill/documents/{identity}/body
GET  /api/v1/{instance_id}/playbill/documents/{identity}/history
POST /api/v1/{instance_id}/playbill/explain
GET  /api/v1/{instance_id}/playbill/principals
POST /api/v1/{instance_id}/playbill/principals/proposals
GET  /api/v1/{instance_id}/playbill/proposals/{proposal_id}
GET  /api/v1/{instance_id}/playbill/proposals/{proposal_id}/refusal
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/review
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate
GET  /api/v1/{instance_id}/playbill/sources/context
POST /api/v1/{instance_id}/playbill/sources/check
POST /api/v1/{instance_id}/playbill/sources/proposals
POST /api/v1/{instance_id}/playbill/subjects/proposals
GET  /api/v1/{instance_id}/playbill/subjects
GET  /api/v1/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}
GET  /api/v1/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}/history
POST /api/v1/{instance_id}/playbill/claim-types/proposals
GET  /api/v1/{instance_id}/playbill/claim-types
GET  /api/v1/{instance_id}/playbill/claim-types/{predicate}
POST /api/v1/{instance_id}/playbill/claims/proposals
GET  /api/v1/{instance_id}/playbill/claims
GET  /api/v1/{instance_id}/playbill/claims/{identity}
GET  /api/v1/{instance_id}/playbill/claims/{identity}/history
POST /api/v1/{instance_id}/playbill/claims/{identity}/explanation
POST /api/v1/{instance_id}/playbill/queries/proposals
GET  /api/v1/{instance_id}/playbill/queries
GET  /api/v1/{instance_id}/playbill/queries/{name}
POST /api/v1/{instance_id}/playbill/queries/{name}/run
POST /api/v1/{instance_id}/playbill/discover
POST /api/v1/{instance_id}/playbill/expand
POST /api/v1/{instance_id}/playbill/coverage/resolve
POST /api/v1/{instance_id}/playbill/floor/export
```

## Surviving public MCP tool inventory

The registered MCP tools are exactly:

```text
cruxible_version
cruxible_server_info
cruxible_playbill_host_create
cruxible_playbill_init
cruxible_playbill_store_body
cruxible_playbill_propose_document
cruxible_playbill_inspect_proposal
cruxible_playbill_inspect_refusal
cruxible_playbill_review
cruxible_playbill_prepare_approval
cruxible_playbill_submit_approval
cruxible_playbill_activate
cruxible_playbill_list_documents
cruxible_playbill_get_document
cruxible_playbill_dereference
cruxible_playbill_history
cruxible_playbill_explain
cruxible_playbill_source_context
cruxible_playbill_check_source_bundle
cruxible_playbill_propose_source_bundle
cruxible_playbill_list_principals
cruxible_playbill_propose_principal_change
cruxible_playbill_propose_subject
cruxible_playbill_list_subjects
cruxible_playbill_get_subject
cruxible_playbill_subject_history
cruxible_playbill_propose_claim_type
cruxible_playbill_list_claim_types
cruxible_playbill_get_claim_type
cruxible_playbill_propose_claim
cruxible_playbill_list_claims
cruxible_playbill_get_claim
cruxible_playbill_claim_history
cruxible_playbill_explain_claim
cruxible_playbill_propose_query_definition
cruxible_playbill_list_query_definitions
cruxible_playbill_get_query_definition
cruxible_playbill_run_query
cruxible_playbill_discover
cruxible_playbill_expand
cruxible_playbill_export_floor
cruxible_playbill_resolve_coverage
```

The permission catalog and MCP registration catalog are equality-tested. See
the [MCP reference](mcp-tools.md) for permissions and payload intent.

## PC-G-S1b: the served-surface expansion

PC-G-S1a landed the knowledge-loop services with no public surface. PC-G-S1b
serves them. The facade operation inventory moves from 19 to 38, and every new
operation delegates to a service that already existed and is untouched here: no
service, law, or wire format changed in this slice.

The 19 added operations are the knowledge loop an instance is driven through:

| Group | Operations |
|---|---|
| Subjects | `playbill_propose_subject`, `playbill_list_subjects`, `playbill_get_subject`, `playbill_subject_history` |
| ClaimTypes | `playbill_propose_claim_type`, `playbill_list_claim_types`, `playbill_get_claim_type` |
| Claims | `playbill_propose_claim`, `playbill_list_claims`, `playbill_get_claim`, `playbill_claim_history`, `playbill_explain_claim` |
| Queries | `playbill_propose_query_definition`, `playbill_list_query_definitions`, `playbill_get_query_definition`, `playbill_run_query` |
| Semantic reads | `playbill_discover`, `playbill_expand` |
| Floor | `playbill_export_floor` |

Each one is registered on all four public surfaces — facade, HTTP route,
`cruxible-client` contract plus client method, and MCP tool — and reaches the
CLI, so the whole loop is drivable from `cruxible playbill ...` alone. The
permission posture is unchanged vocabulary: the four proposal operations check
`cruxible_playbill_propose` (`GOVERNED_WRITE`) exactly as the Document and
source-bundle proposals do, `playbill_explain_claim` checks
`cruxible_playbill_explain`, and every remaining new operation is a read
checking `cruxible_playbill_read` (`READ_ONLY`). No new tier was minted, and no
operation accepts a caller-supplied actor context: writer identity is still
derived from the authenticated credential at the facade.

Two surface-shaped notes a reviewer should not have to re-derive:

- **Floor export stays filesystem-free below the CLI.** The service returns a
  path-to-bytes map; the contract carries it base64 per path beside the decoded
  coordinate manifest; only the CLI writes a directory, and it refuses a
  non-empty destination without `--force`. The daemon never receives or writes a
  client path.
- **Query receipts are returned, not journaled.** `playbill_run_query` returns
  the `playbill-query-execution-receipt-v1` with its result digest, and
  `journal_record_digest` is null at this surface. The journal backend is
  caller-owned exactly as it is for Procedure exhaust, and the daemon does not
  open one yet. Wiring a daemon-owned query-receipt journal is a PC-G seam.

### The authorized golden re-pin

Two frozen served-surface goldens and the generated client contract snapshot
were re-pinned in this slice, under explicit maintainer authorization, because
they pin the *size and shape of the served surface* and this slice deliberately
expands it:

| Golden | What changed |
|---|---|
| `tests/goldens/playbill/served-surface-dp0b-v1.json` | `facade_operations` 19 → 38; `http_delegate_count` and `mcp_delegate_count` 19 → 38 |
| `tests/goldens/http_surface/http_surface_snapshot.json` | 19 added paths, all under `/playbill/`; zero existing paths removed or changed |
| `tests/goldens/cruxible_client/contracts_snapshot.json` | 17 added models; zero existing models removed or changed |

All three diffs are strictly additive, and each was produced through that
artifact's own regeneration path rather than a blanket golden refresh. Every
other golden in this repository is byte-identical across the slice.

## PC-F2-S2: the reference coverage surface

PC-F2-S1 landed coverage headless: two disposable indexes, one pure resolver,
one local freshness manifest, and no way to call any of it. This slice serves
it as §11.7's one vendor-neutral operation and renders it as §11.6.4's reference
surface. The facade operation inventory moves from 38 to 39.

**One operation, five request forms.** `playbill_resolve_coverage` is the whole
public surface. §11.7's five forms -- a file read with a line/range selection, a
grep result batch, a set of changed filesystem paths, an explicit source
occurrence, and a working-set scope -- are not five operations; the adapter
reduces all five to observations and the spans they carry before the operation
sees them.

**The adapter reads the filesystem; the operation never does.** A request
carries *observations*: a declared logical-source binding, the bytes the caller
actually read, and the windows it asked about. Bindings are declared, never
inferred -- guessing that `handbook.md` is `documents/handbook.md` would let
identical bytes inherit governance by filename coincidence, which is the exact
mistake §11.6.1 exists to prevent -- and an undeclared path is a typed refusal.
Bytes travel because an accepted commitment is a digest rather than a needle:
finding cited content that moved means hashing windows of the working source,
and only the side holding accepted state knows which lengths to look for. The
access profile is derived from the surface's read authority and is never
accepted from the caller, so a request cannot widen its own disclosure.

**Rendering lives in the package, not the CLI.** §11.7 requires every adapter to
reproduce the reference surface's coverage semantics, so `coverage/render.py`
holds the §11.6.4 laws and the CLI is one caller of them. Governed cards are
annotated inline in canonical order; ungoverned spans are summarized once and
render nothing at all; omitted and truncated counts are stated on every
operation, including when they are zero; and a span whose health is not
`complete` prints that health with its reason codes, so `denied` and
`unavailable` can never read as a factual absence.

**The floor carries its coverage boundary.** `floor export` gains
`coverage-manifest.json`, enumerated in the root manifest like every other floor
file: the accepted coordinate, the evidence-index generation, the access
profile, and the logical sources accepted evidence cites there. An export
observes no working snapshot, so it carries no epoch and proves no freshness --
it tells a reader what a coverage answer could be about, never what it is.

**A coverage resolve appends no receipt, and that is a recorded decision.**
§11.6 makes coverage delivery semantically side-effect-free: it changes no
accepted state, candidate, permission, verdict input, or evaluation episode, and
it adds no authority to the material it describes. Whether ordinary reads append
to a daemon-owned journal is PC-G's journal-ownership decision -- the same open
seam that leaves `journal_record_digest` null on query execution -- and settling
it here would settle it from the wrong end, by making the highest-frequency read
in the system the first journal writer. The answer stays checkable without one:
it names the evidence-index digest, the overlay digest, and the manifest digest
it resolved against, and those three reproduce it exactly.

**The one thing the operation writes** is the local coverage manifest under
`<managed root>/coverage/`, and only when the observed snapshot or the accepted
coordinate actually moved. The epoch is therefore a counter over *observations*
rather than over calls, which is what makes it usable for ordering two
snapshots. That file is the same class of artifact as a replay checkpoint:
disposable, digest-committed, deleted rather than trusted when it does not
reproduce, and costing only provable freshness when removed.

**What accepted state can reach today, stated so a reviewer does not have to
re-derive it.** Every Capture the served authoring surface produces is
content-addressed, and a CAS reference deliberately names no logical source. By
§11.6.1 a byte match at a working occurrence is therefore a labeled
`content_equivalent` candidate, and `exact` and `drifted` -- both of which
require the accepted and observed logical source to agree -- are not yet
reachable end to end from the CLI. `build_ledger_capture` and the external
acquisition path already produce the logical-source-bound Captures that unlock
them; no served operation invokes those builders, and PC-G's watcher is their
first caller. The resolver, the cards, and their rendering are proved against
real `exact`, `drifted`, and ambiguous coverage in the headless suites.

### The authorized golden re-pin

The same three served-surface pins move, additively, for the one added
operation:

| Golden | What changed |
|---|---|
| `tests/goldens/playbill/served-surface-dp0b-v1.json` | `facade_operations` 38 → 39 (`playbill_resolve_coverage`); `http_delegate_count` and `mcp_delegate_count` 38 → 39 |
| `tests/goldens/http_surface/http_surface_snapshot.json` | one added path, `POST /api/v1/{instance_id}/playbill/coverage/resolve`; zero existing paths removed or changed |
| `tests/goldens/cruxible_client/contracts_snapshot.json` | one added model, `PlaybillCoverageResult`; zero existing models removed or changed |

Each was produced through its own regeneration path, and every other golden in
this repository is byte-identical across the slice.

## Deleted directory and service inventory

DP-0 removed these legacy product packages in full:

- `cruxible_core.bindings`
- `cruxible_core.blueprint`
- `cruxible_core.canonical_views`
- `cruxible_core.decision`
- `cruxible_core.feedback`
- `cruxible_core.installs`
- `cruxible_core.kit_distribution`
- `cruxible_core.snapshot`
- `cruxible_core.telemetry`
- `cruxible_core.transport`
- `cruxible_core.ui_static`

It removed the legacy CLI modules for attestations, config views, decision
records, feedback, gates, groups, instances, kits, lifecycle verbs, list/read
surfaces, mutations, outcome contracts, Procedures, source artifacts, state,
telemetry, workflows, and working sets. The matching legacy HTTP route modules
were removed, as were the mixed `runtime.api` facade and MCP working-set/kit
surfaces. None remains registered indirectly.

These legacy service modules were deleted:

```text
service/analysis.py
service/artifact_lifecycle.py
service/bindings.py
service/config_mutations.py
service/decisions.py
service/feedback.py
service/gates.py
service/installs.py
service/lifecycle.py
service/mutation_receipts.py
service/resolution_contracts.py
service/snapshots.py
service/state.py
service/state_diff.py
service/telemetry.py
service/views.py
```

The pass also deleted `working_set.py`, `runtime/instance_manager.py`,
`mcp/kit_surface.py`, `server/telemetry.py`, all first-party `kits/`, the kit
bundle/lock/release/doc generation scripts, the snapshot/state publication
products, and their product-specific tests. Legacy config-authority, mutable
graph, snapshot/overlay, kit-distribution, provider, and deep-dive documentation
was removed or rewritten around the surviving Playbill surface.

PC-D subsequently deleted the `cruxible_core.group` and `cruxible_core.kits`
packages, workflow proposal/apply modules, and the old Procedure persistence
lifecycle. The frozen Procedure graph-format-v1/v2 readers remain as a PC-H
corpus verifier — PC-H settles whether they become a permanent non-donor
verifier package. Importing any retired module no longer initializes a retired
governance or storage path.

PC-E1 deleted the ReceiptStore and ResolutionContractStore packages, protocols,
runtime accessors, SQLite initialization, receipt-derived history/trace reads,
and the old workflow executor/service path. It rehomed only the pure receipt
tree — moved again in PC-F, out of `workflow/` and into the donor-free
`receipt_tree/` package it now outlives — and the storage-free resolution
oracle under `procedure/`. Procedure journals and promoted exhaust are now the
sole durable
operational-evidence path; retained graph mutation fixtures use a transaction
wrapper that cannot emit a receipt or write a replacement audit store.

## PC-F: the donor purge

PC-F deleted the query-oracle spine, every harness that carried it, and the
unserved mutation/query service layer built on top of it. It is the largest
single removal since DP-0C, and the batch that leaves the codebase with no
legacy instance, no legacy storage backend, and no mutable graph write path.

### Removed in full

```text
cli/instance.py
config/composition_ownership.py
config/ownership.py
config/provenance.py
graph/claim_target.py
graph/diff.py
graph/entity_identity.py
graph/group_drift.py
graph/legacy_identity.py
graph/operations.py
graph/property_diffs.py
instance_protocol.py
provider/payloads.py
provider/registry.py
query/continuation.py
query/engine.py
query/entity_state.py
query/evaluate.py
query/filters.py
query/graph_layout.py
query/lifecycle_status.py
query/projection.py
query/read_surface.py
runtime/instance.py
server/auth_managed_entities.py
service/direct_write_policy.py
service/evidence.py
service/lifecycle_inputs.py
service/mutation_guards.py
service/mutation_proposals.py
service/mutation_transactions.py
service/mutations.py
service/queries.py
service/server.py
service/types.py
sqlite_ddl.py
storage/protocols.py
storage/sqlite.py
workflow/artifacts.py
workflow/compiler.py
workflow/contracts.py
workflow/refs.py
workflow/step_helpers.py
workflow/transforms.py
```

`sqlite_ddl.py` is not a donor in its own right: it existed only to execute the
SQLite schema script for `storage/sqlite.py` and was orphaned by that deletion.
`storage/__init__.py` lost the lazy `_EXPORT_MODULES` map, every entry of which
pointed at a deleted module, and the `workflow`, `provider`, and `query`
package initializers lost re-export catalogs that named deleted modules.

`cruxible_core.kit_defaults` was already unimported before this batch and is
left in place: it is pre-existing dead code, not PC-F residue, and removing it
belongs to the PC-H import audit.

### Deferred residue, and why each module is still here

The purge was re-batched after a classification pass proved the original PC-F
labels were dependency-wrong: `config.schema` is validated by a chain that
reaches into `query` and `graph`, and `procedure/` — a PC-H donor — is pinned
to all of it. Deleting on the original labels would have taken the Procedure
corpus verifier with it. So the following stay alive with their removal batch
moved, and the batch that owns them owns their deletion:

| Residue | New batch | Why it could not leave in PC-F |
|---|---:|---|
| `cruxible_core.config` (whole package) | PC-H | The Procedure definition digest is computed over these schemas. The package is labelled whole rather than carving `schema.py` out of it, because carving risks moving that frozen digest. |
| `cruxible_core.predicate` | PC-H | Every remaining consumer is itself PC-H residue: `procedure/guards.py`, `procedure/analysis.py`, `config/{schema, validator, constraint_rules}.py`, and `query/predicates.py`. |
| `cruxible_core.query.{enums, predicates, types, profiles, relationship_state}` | PC-H | The verified deferred-validator chain: `config/schema.py` imports `query.enums` eagerly and `query.predicates` inside `_validate_top_level_query_predicate_scopes`; `predicates` pulls `types` and `relationship_state`, and `types` pulls `profiles`. |
| `cruxible_core.graph.{types, entity_graph, assertion_state, provenance}` | PC-H | The same chain: `query.predicates` imports `graph.entity_graph`, and `query.types` imports `graph.types`, which imports `assertion_state` and `provenance`. |
| `cruxible_core.graph.evidence` | PC-H | `EvidenceRef` is a field type on `procedure/types.py`, `procedure/resolution_oracle.py`, and `graph/types.py`. |
| `cruxible_core.workflow.types` | PC-H | `procedure/pins.py` describes what a pin records in terms of `WorkflowLock`, `LockedProvider`, and `LockedArtifact`. It stayed in `workflow/` rather than being relocated: a move would be an honest-rename, which is PC-H's, and would touch a module the frozen Procedure digest corpus reads. |
| `cruxible_core.provider.{types, trace_payloads}` | PC-G | The manifest's previous rationale — "last consumers are workflow and service types" — was factually incomplete. Every reader in `providers/common/*` is written against `ProviderContext`, so the contract types leave with the readers in PC-G, not with the registry and payload donors that left here. |

The architecture suite derives this residue rather than merely listing it: the
surviving `query` and `graph` modules are asserted to be a subset of the
`config.schema` import closure, so a later batch cannot widen the residue
without the guard noticing.

### Tests removed, converted, and kept

Whole suites whose only subject was deleted code left with it:
`tests/test_workflow/`, `tests/test_storage/`, `tests/test_support/`, the eight
donor suites under `tests/test_service/` and their `conftest.py`,
`tests/test_graph/{test_edge_identity, test_entity_identity, test_graph_diff,
test_operations}.py`, `tests/test_query/{test_engine, test_graph_layout,
test_neighborhood}.py`, `tests/test_config/test_composition_ownership.py`,
`tests/test_providers/test_payloads.py`, and — per the F7 seam —
`tests/test_playbill/test_modeling_parity_donors.py` in full. The shared
donor helpers `tests/support/{state_cross_section, terminal_lifecycle,
workflow_helpers}.py` went with them.

One guardrail was deleted, and it is the only one:

```text
tests/test_guardrails/test_write_policy_asymmetry.py
```

It drove the `graph/operations.py` chokepoint and the deleted
`service/direct_write_policy.py`, so the asymmetry it policed no longer exists.
The other five legacy guardrails named in the pre-re-batch classification —
`test_mutation_guard_reference`, `test_v2_registry_is_total`,
`test_pin_payload_is_total`, `test_procedure_step_union_is_unambiguous`, and
`test_branch_predicate_grammar_is_closed` — were classified as dying with the
purge only because the original labels deleted `config.schema`, `predicate`,
and `workflow.types`. Under the re-batch every subject they police is live
residue, so all five are retained and pass unchanged.

`tests/test_receipt_tree/test_receipt_tree.py` was converted rather than
deleted. It used `query.engine.execute_query` as a scenario oracle, which made
a deleted donor an input to a package that outlives it. The query-shaped
receipt DAG is now composed directly through the public `ReceiptBuilder` API,
node for node and edge for edge, and every assertion the engine used to satisfy
is asserted against it unchanged. `receipt_tree/` has no donor dependency left.

Four suites kept their subject and lost only the donor-driven case inside them:
`tests/test_config/test_compact.py` (the write-enforcement half of the identity
case), `tests/test_query/test_profiles.py` (the SQLite round-trip, restated as
the property-order invariant it was actually testing),
`tests/test_procedures/test_graph_analysis.py` (the compiler alias-walk oracle,
restated as the literal walk), and `tests/test_playbill/test_bootstrap.py` (the
legacy `CruxibleInstance` layout case). `tests/test_cli/test_startup_imports.py`
now asserts the surviving PC-H residue stays unloaded on CLI import, since the
module it used to name no longer exists.

### Known prose residue, deliberately left to PC-H

PC-F fixed the two references that were broken rather than merely historical:
the `:mod:` cross-reference in `query/relationship_state.py` to the deleted
`query.entity_state`, and the runtime `ValueError` in `graph/entity_graph.py`
that told callers to mint claim ids through the deleted
`graph.operations.apply_relationship`. What remains is narrative — docstrings
inside retained donors that describe where a rule *used* to be enforced:

```text
config/schema.py     -> service/direct_write_policy.py, graph/operations.py
errors.py            -> graph/operations.py
graph/types.py       -> graph.operations.apply_relationship
query/profiles.py    -> storage/sqlite.py
workflow/types.py    -> provider.registry.resolve_command_provider_target
```

These are left as-is on purpose: rewriting them is the honest-rename sweep PC-H
owns, and every file listed except `errors.py` is itself donor residue that
leaves in PC-G/PC-H. `errors.py` additionally still defines
`DirectWriteRefusedError` and `TerminalLifecycleWriteRefusedError`, which
nothing raises any more but which `server/errors.py` still maps to HTTP status
codes under the frozen contract; retiring them is a contract change, not a
purge, and belongs to the PC-H audit.

## Donor allowlist and removal batches

This table is the complete import-level donor manifest. Served Playbill code
may not import these modules; any Playbill transplant must go through the named
adapter. Every row has an owning removal batch.

| Donor module prefix | Removal batch | Why retained | Adapter |
|---|---:|---|---|
| `cruxible_core.procedure` | PC-H | Frozen graph-format v1/v2 corpus verifier; PC-H settles whether it becomes a permanent non-donor verifier package | — |
| `cruxible_core.config` | PC-H | Step, query, provider, and contract schema donor pinned by the Procedure definition digest; the whole package is labelled rather than `schema.py` alone because carving a module out of it risks moving that frozen digest | — |
| `cruxible_core.predicate` | PC-H | Typed comparison and coercion donor; every remaining consumer is itself PC-H residue — the Procedure guard grammar, the config schema/validator/constraint rules it validates, and the residual `query.predicates` vocabulary | — |
| `cruxible_core.query` | PC-H | Residual query vocabulary the config schema reaches when validating a named query — `enums`, `predicates`, `types`, `profiles`, `relationship_state`; the engine, evaluation, filter, projection, continuation, layout, and read-surface donors left in PC-F | — |
| `cruxible_core.graph` | PC-H | Residual graph vocabulary reached through the same validator chain — `types`, `entity_graph`, `assertion_state`, `provenance` — plus the `EvidenceRef` behavior the Procedure and workflow lock types depend on; the mutable graph operations, diff, and identity donors left in PC-F | — |
| `cruxible_core.workflow` | PC-H | Residual lock/plan types only: `procedure/pins.py` describes what a pin records in terms of `WorkflowLock`, `LockedProvider`, and `LockedArtifact`, so the module leaves with the Procedure donor; the compiler and the rest of the query-oracle spine left in PC-F and the `Receipt` tree was already rehomed to `cruxible_core.receipt_tree` | — |
| `cruxible_core.provider` | PC-G | Residual provider contract/trace types only; the last consumers are the un-transplanted readers in `cruxible_core.providers` (`providers/common/*` is written against `ProviderContext`), so it leaves with them rather than with the registry and payload donors that left in PC-F | — |
| `cruxible_core.providers` | PC-G | Un-transplanted tabular/document/identity readers; native source connectors land with the vertical slice | — |

The unserved mutation and query service layer that this table used to cover is
gone: PC-D took the group and Procedure/workflow governance services, PC-E1 the
receipt/resolution/gate/lifecycle services, and PC-F the remaining mutation,
query, evidence, write-policy, and server service modules together with the
legacy instance and SQLite backend they ran against. No donor write path
survives.

`DONOR_OPERATION_PERMISSIONS` outlives the operations it names. It is now a
frozen record of the pre-Playbill authority tiers rather than a live seam: the
architecture suite asserts it stays disjoint from both the public MCP tools and
the active HTTP/CLI runtime operations, so nothing it lists can be reachable.
PC-H settles whether the catalog is kept as history or deleted with the rest of
the donor vocabulary.

## Local operational formats, which are not wire formats

Some formats an instance writes are deliberately **not** accepted state. They
carry a version tag and a canonical encoding the way a wire format does, because
that is how this codebase writes any structured record, but nothing outside the
instance may read them as authority and no reviewer should read a change to one
as a wire change.

| Format | Where it lives | What it is |
|---|---|---|
| `playbill-replay-checkpoint-v2`, inside `playbill-replay-checkpoint-file-v2` | `<managed root>/checkpoints/replay-checkpoint-v2.json` | A summary of one already-verified accepted prefix, so a reopen replays only a bounded suffix. |
| `playbill-projection-manifest-v1` and its piece builds | `<managed root>/projections/` | Disposable materialized read state, rebuilt from the accepted tree. |
| `playbill-serving-manifest-v1` | `<managed root>/projections/serving.json` | The local admission pointer at one exact accepted coordinate. |
| `playbill-coverage-manifest-v1`, inside `playbill-coverage-manifest-file-v1` | `<managed root>/coverage/coverage-manifest-v1.json` | The published freshness proof for coverage delivery: which working sources were observed, at which accepted coordinate, over which index, under which access profile, at which epoch. |

The checkpoint format is the one PC-F added. Four properties make it reviewable
as local state rather than as a new surface:

- It sits **outside** the `StorageLayout` that the `playbill-instance-v1`
  descriptor commits to, so adding it did not widen a frozen preimage. It is a
  fixed directory name resolved from the managed root, created on first write.
- It never enters the ledger, the CAS, an exhaust journal, or an export, and
  `inspect()` does not report it among the storage directories.
- Every value it carries is re-derived from the ledger when it is loaded and
  then compared against the file. A mismatch is a typed `ReplayCheckpointError`,
  the file is deleted, and recovery falls back to genesis. The module docstring
  in `src/cruxible_core/playbill/checkpoints.py` carries the full argument for
  why replaying only a suffix does not weaken tamper detection for that suffix.
- Deleting it costs replay time and nothing else. No test and no operator
  procedure may treat its presence as required.

A local format is superseded by rewriting it, never by migrating it. The wire
succession moved the checkpoint body from v1 to v2 -- it now carries the merkle
manifest root beside the flat one, because the receipt at its coordinate may sign
either -- and a v1 file left in the directory is deleted on the next load rather
than read, translated, or kept. That is the whole upgrade path a rebuildable
cache is entitled to, and the reopen it would have shortened replays from genesis
instead. The body carries the trie's *root* and not its nodes: a node map would
have to be recomputed from the members to be worth trusting, the members are
already re-derived from the coordinate's own bytes on every load, and rebuilding
the trie from them costs a hash per path where storing it would cost a node per
path on disk and prove nothing extra.

Its self-digest kind, `ReplayCheckpointDigest`, is declared in `checkpoints.py`
rather than beside the wire digest kinds in `canonical.py`, precisely so a later
reader cannot mistake it for one.

The coverage manifest is the one PC-F2 added, and it is local state by the same
four properties. It sits outside the descriptor's `StorageLayout` under a fixed
`coverage/` directory; it never enters the ledger, the CAS, a journal, or an
export; every value it carries is compared against a freshly observed index and
overlay on each resolve, and a file whose self-digest does not reproduce is
deleted rather than read; and deleting it costs only provable freshness, after
which the resolver reports `unavailable`, stops returning `exact`, and keeps
answering. Its self-digest kind, `CoverageManifestDigest`, is likewise declared
in `coverage/manifest.py` rather than in `canonical.py`. Its epoch is a
monotonic counter and never a time, and its publication time sits outside the
digest preimage, so rebuilding it over the same coordinate and snapshot
reproduces the same digest exactly.

No golden pins the *contents* of these files, and none should: a golden is how
this repository freezes a contract other parties depend on, and these are
rebuildable local caches whose presence no test may require. One golden,
`coverage-grammar-v1.json`, does pin the coverage manifest's **field grammar**
alongside the coverage request/result grammar, because the §11.5 coverage
addendum froze the manifest's identity and completeness binding fields as part
of the contract every coverage adapter reads. That is a pin on the shape a
manifest must have, never on any particular manifest existing.

## Exact frozen goldens retained

The complete tracked `tests/goldens/` inventory is:

```text
tests/goldens/cruxible_client/contracts_snapshot.json
tests/goldens/http_surface/http_surface_snapshot.json
tests/goldens/kev/asset_exposure_proposal.json
tests/goldens/kev/asset_products_proposal.json
tests/goldens/kev/auto_resolve_branches.json
tests/goldens/kev/exposure_reconciliation_proposal.json
tests/goldens/kev/intermediate_payloads/asset_exposure_workflow.json
tests/goldens/kev/intermediate_payloads/asset_products_workflow.json
tests/goldens/kev/intermediate_payloads/exposure_reconciliation_workflow.json
tests/goldens/kev/named_query_surfaces.json
tests/goldens/kev/overlay_review_state.json
tests/goldens/kev/reference_build_state.json
tests/goldens/kev/relationship_state_visibility.json
tests/goldens/playbill/attestation-v1.json
tests/goldens/playbill/candidate-v1.json
tests/goldens/playbill/candidate-v2.json
tests/goldens/playbill/capture-claim-v1.json
tests/goldens/playbill/changeset-v3.json
tests/goldens/playbill/claim-type-v1.json
tests/goldens/playbill/coverage-grammar-v1.json
tests/goldens/playbill/depgraph-v3.json
tests/goldens/playbill/discovery-index-v1.json
tests/goldens/playbill/journal_corpus/index.json
tests/goldens/playbill/journal_corpus/negative/export-duplicated-record.json
tests/goldens/playbill/journal_corpus/negative/export-false-oversized-claim.json
tests/goldens/playbill/journal_corpus/negative/export-forked-chain.json
tests/goldens/playbill/journal_corpus/negative/export-missing-record.json
tests/goldens/playbill/journal_corpus/negative/export-missing-segment.json
tests/goldens/playbill/journal_corpus/negative/export-overlapping-segments.json
tests/goldens/playbill/journal_corpus/negative/export-reordered-segments.json
tests/goldens/playbill/journal_corpus/negative/export-tampered-head-signature.json
tests/goldens/playbill/journal_corpus/negative/export-tampered-head-vector.json
tests/goldens/playbill/journal_corpus/negative/export-unknown-boundary-rule.json
tests/goldens/playbill/journal_corpus/vectors/export-alpha-a-1-3.json
tests/goldens/playbill/journal_corpus/vectors/export-alpha-a-4-6.json
tests/goldens/playbill/journal_corpus/vectors/export-segment-boundary.json
tests/goldens/playbill/journal_corpus/vectors/export-two-partitions.json
tests/goldens/playbill/journal_corpus/vectors/head-manifest-two-partitions.json
tests/goldens/playbill/journal_corpus/vectors/head-vector-two-partitions.json
tests/goldens/playbill/journal_corpus/vectors/journal-range-alpha-a-1-3.json
tests/goldens/playbill/journal_corpus/vectors/partition-head-alpha-a-3.json
tests/goldens/playbill/journal_corpus/vectors/partition-head-genesis.json
tests/goldens/playbill/journal_corpus/vectors/stored-record-alpha-a-1.json
tests/goldens/playbill/journal_corpus/vectors/stream-identity-alpha.json
tests/goldens/playbill/journal_corpus/vectors/stream-identity-beta.json
tests/goldens/playbill/merkle-manifest-v1.json
tests/goldens/playbill/oracles-v1.json
tests/goldens/playbill/projection-v1.json
tests/goldens/playbill/query-definition-v1.json
tests/goldens/playbill/semantic-genesis-v1.json
tests/goldens/playbill/served-surface-dp0b-v1.json
tests/goldens/playbill/settlement-roots-v1.json
tests/goldens/playbill/sroot-v2.json
tests/goldens/playbill/source-reference-v1.json
tests/goldens/playbill/subject-v1.json
tests/goldens/state_cross_section/car_parts_state_diff.json
```

PC-F added four goldens for the coordinated wire succession and left every
pre-existing entry byte-identical -- including through the slice that made those
four versions the ones a proposal produces. `tests/goldens/state_cross_section/`
and `tests/goldens/kev/` are frozen records whose readers left in earlier
batches; a frozen golden is deleted deliberately by the batch that retires the
contract it pins, never as a side effect of deleting its last reader.

`tests/goldens/playbill/journal_corpus/` is the frozen journal conformance
corpus: twelve positive canonical vectors, ten malformed negative fixtures, and
an index naming each fixture's law category and the stage that must refuse it.
Its bytes were produced elsewhere by calling only the pure encoders in
`cruxible_core.playbill.exhaust`, and they are committed verbatim -- this
repository holds no generator for them, and `index.json` plus every vector is
read-only test data. `tests/test_playbill/test_journal_conformance_corpus.py`
replays the committed bytes through Core's own parser, head verifier, and
`LocalJournalBackend`, and pins each file's SHA-256 against a registered digest
so an edit or a regeneration fails loudly. A positive vector that stops
verifying, or a negative fixture that starts importing, is a persisted-format
break reviewed under a new format tag, never a regeneration event.

`tests/goldens/playbill/claim-type-v1.json` preserves the canonical policy-bearing
ClaimType v1 wire and digest contract.

`tests/goldens/playbill/capture-claim-v1.json` preserves the bounded direct
CaptureContract/CaptureEnvelope wire and all three first-class Claim digest
layers.

`tests/goldens/playbill/source-reference-v1.json` preserves locator-free external
source identity and remote-state refusal behavior.

`tests/goldens/playbill/query-definition-v1.json` preserves the canonical
Claim-native QueryDefinition v1 wire, its verdict/conflict policy and budget
declarations, and its envelope digest.

`tests/goldens/playbill/discovery-index-v1.json` preserves the disposable
grep-friendly discovery index rendered from the materialized Subject view: its
`INDEX.md` and `discovery.jsonl` bytes plus the rebuild manifest a deletion and
rebuild must reproduce.

`tests/goldens/playbill/merkle-manifest-v1.json` pins the merkle manifest
primitive: the defined empty root, the leaf/interior/root preimages, every trie
node digest, and the incremental change set that must reproduce the same root as
a from-scratch build. The `merkle-sha256:` root prefix is deliberately disjoint
from the flat `sha256:` manifest root so the two structures can never be read for
one another. `playbill-candidate-v2` signs this root.

### The coordinated wire succession

Four goldens pin the succession's formats, and those formats are now what a new
proposal produces: every candidate this build validates is a
`playbill-validated-candidate-v3` carrying a `playbill-candidate-v2` and a
`playbill-closure-proof-v3`, it settles as a `playbill-changeset-v3`, and its
semantic root is derived by `playbill-sroot-v2`.

The versions they succeed are retained as **verifiers**. An accepted generation
is re-verified against the object its own receipt carries, so replay asks the one
evaluator for the wire version the receipt names rather than for the version this
build would produce today; a v1 candidate is reproduced with its file digests and
its governance vocabulary, and its acceptance is not retroactively subjected to a
dependency closure that did not exist when it settled. `PRODUCED_CANDIDATE_VERSION`
in `candidates.py` is the single place production's version is stated.

Genesis is untouched. Its semantic root is what a `playbill-sroot-v1` chain
starts from, so the first accepted generation of *every* instance -- including one
created today on an empty ledger -- is a v3 record whose `sroot-v2` preimage
names `parent_derivation="playbill-sroot-v1"`. Every ledger therefore states the
succession boundary at generation one, which is why the boundary is not special
cased anywhere. `tests/test_playbill/test_wire_succession_boundary.py` builds a
ledger with a v1/v2 prefix and a v3 suffix and requires genesis-rooted replay, a
checkpoint seeded at the boundary generation, a checkpoint on the v3 suffix, and
accepted projection to all accept it.

`tests/goldens/playbill/candidate-v2.json` pins the `playbill-candidate-v2`
preimage and digest. The candidate carries a `merkle-sha256:` manifest root in
place of the flat one, never both, and the digest domain moves with the version,
so the flat-rooted v1 sibling recorded beside it hashes to a different value.

`tests/goldens/playbill/sroot-v2.json` pins the `playbill-sroot-v2` derivation.
Its preimage hashes every input in its full tagged spelling, restoring the domain
separation v1 destroyed by hashing bare hex, and adds `parent_derivation` naming
the derivation that produced the parent. The vectors include the succession
boundary — the first v2 generation, whose parent is the last v1 semantic root —
beside the steady-state vector over the same parent value, which must differ.

`tests/goldens/playbill/depgraph-v3.json` pins the `playbill-dependency-graph-v3`
edge-set commitment: the defined empty root, the per-member edge-set preimage,
the leaf and root preimages, every trie node digest, and an incremental change
set that must reproduce the from-scratch root. The trie is the same one
implementation as the manifest merkle under a second domain family, and its
`depgraph-sha256:` root prefix is disjoint from both `sha256:` and
`merkle-sha256:`.

`tests/goldens/playbill/changeset-v3.json` pins the `playbill-changeset-v3`
receipt that carries the other two: a v2 candidate and a v3 closure proof, with
member evidence, law evidence, approvals, and actor binding unchanged from v2.

No golden was added for the succession's *production* or for the crossed-ledger
boundary, and none should be. A golden freezes a byte contract other parties
depend on; what those two need checked is that a value **derives** correctly --
that the root a candidate signs is the trie over its own members, and that a
boundary generation's root is `compute_semantic_root_v2` over its recorded
parent under a v1 `parent_derivation`. Both are asserted against the derivation
itself, which a recorded byte string could only weaken.

`tests/goldens/playbill/oracles-v1.json` pins the Family-1 oracle at
`e3fe35b360d098f14a5d59bf770ffee401224f0c` and the Procedure graph-program
oracle at `986307d56649eb51747ca227228fbe19f73e3895`.

Other deliberately frozen donor inputs are the 48-file
`tests/data/procedure_digest_corpus/` (whose exact membership is asserted by
`test_definition_digest_corpus.py`) and the byte-preserved
`tests/data/config_donors/agent-operation/config.yaml` plus
`cruxible-kit.yaml`. They remain verification fixtures, not distributed kits.

PC-F added two more, under the same DP-0C allowance to copy "only semantic
parity fixtures needed by PC-F/PC-G into test data":
`tests/data/config_donors/project-domain/` and
`tests/data/config_donors/supply-chain-blast-radius/` are trimmed parity slices
of the kit configs deleted in `b36b2a1b`, carrying only the vocabulary and the
verbatim named queries the modeling-parity cases read. They and
`tests/data/playbill_parity/modeling-parity-oracle-v1.json` — the pinned record
of what each surface answers — outlive the donor purge and are asserted present
by the DP-0 boundary test.

## Intentionally retained package dependencies

Dependency pruning is PC-H work and follows actual imports after the final
transplant. DP-0 intentionally retains:

| Dependency | Why retained after DP-0 |
|---|---|
| `cruxible-client` | Reduced Playbill HTTP client shipped with the core |
| `pydantic` | Active Playbill/client contracts and donor validation models |
| `packaging` | Requirement parsing in the architecture dependency audit |
| `networkx` | Active local Claim-query backend (`playbill/query/networkx_backend.py`) plus the residual `graph.entity_graph` the config-schema validator chain reaches, through PC-H |
| `polars` | Tabular reader `providers/common/tabular.py`, through PC-G |
| `pyyaml` | Active source-catalog CLI plus the config donor loader/expander, through PC-H |
| `structlog` | Active daemon audit/request logging and permission checks |
| `click` | Active four-group CLI |
| `cryptography` | Ed25519 principal keys, signatures, and Git verification |
| `rich` | Packaging cleanup deferred to the import-based PC-H prune; no new Playbill contract depends on it, and after PC-F nothing imports it at all |
| `httpx` | Reduced client/CLI transport plus the document reader `providers/common/documents.py`, through PC-G |
| `pypdf` | PDF reader backend in `providers/common/documents.py`, through PC-G |
| `markdown-it-py` | Deterministic Markdown source-span donor through PC-C |
| `fastapi` | Active HTTP daemon |
| `python-multipart` | Daemon dependency retained until the PC-H import audit |
| `uvicorn` | Active daemon launcher |
| `mcp` (optional extra) | Active Playbill MCP server |
| `docling` (optional `pdf` extra) | Layout-aware PDF reader backend in `providers/common/documents.py`, through PC-G |

No dependency in this table is evidence that its legacy product is still
public. The served dependency-closure guard separately proves that Playbill
transport code cannot reach legacy graph/config/storage donors.

## Verification

DP-0E is accepted only when the full surviving test suite is green, in addition
to the required focused block:

```bash
uv run pytest -q tests/test_playbill tests/test_architecture/test_playbill_*.py
uv run mypy src/cruxible_core/playbill src/cruxible_core/service
uv run ruff check src tests
```

PC-F additionally requires the surviving guardrail and target-visibility
surfaces, which the purge touched:

```bash
uv run pytest -q tests/test_guardrails tests/test_cli/test_playbill_target_visibility.py
uv run pytest -q tests/test_procedures tests/test_providers
```

The architecture suite pins the public registration catalogs, the donor
manifest, the private donor permission seam, the immutable oracle commits, and
this guide's inventories. The RR `change_head` must name the exact commit that
passed those checks.
