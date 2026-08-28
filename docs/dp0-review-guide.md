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
playbill authoring abandon-insertion
playbill authoring bind
playbill authoring compile
playbill authoring confirm-insertion
playbill authoring create
playbill authoring get
playbill authoring list
playbill authoring prepare-publication
playbill authoring preflight
playbill authoring rebase
playbill authoring resume
playbill authoring status
playbill authoring submit
playbill audit
playbill block repin
playbill body store
playbill claim explain
playbill claim get
playbill claim history
playbill claim list
playbill claim propose
playbill claim propose-batch
playbill claim retire
playbill claim-type get
playbill claim-type list
playbill claim-type migrate
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
playbill hook post-tool-use
playbill host create
playbill init
playbill list
playbill native compile
playbill native render
playbill native review-current
playbill native stash list
playbill native stash restore
playbill native stash show
playbill native status
playbill next
playbill curation list
playbill curation overrule
playbill curation accept-fixed
playbill curation suppress
playbill orient
playbill principal add
playbill principal list
playbill principal recover
playbill principal revoke
playbill principal rotate
playbill proposal activate
playbill proposal approve
playbill proposal inspect
playbill proposal list
playbill proposal readmit
playbill proposal refusal
playbill proposal review
playbill procedure bind
playbill procedure readiness
playbill procedure run
playbill procedure status
playbill query get
playbill query list
playbill query propose
playbill query run
playbill search
playbill seed apply
playbill since
playbill sources check
playbill sources compile
playbill sources propose
playbill subject get
playbill subject history
playbill subject list
playbill subject propose
playbill whoami
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
GET  /api/v1/{instance_id}/playbill/authoring/intents
POST /api/v1/{instance_id}/playbill/authoring/intents
POST /api/v1/{instance_id}/playbill/authoring/compile
GET  /api/v1/{instance_id}/playbill/authoring/intents/{intent_id}
GET  /api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/resume
GET  /api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/status
POST /api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/preflight
POST /api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/rebase
POST /api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/submit
POST /api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/insertion/prepare
POST /api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/insertion/confirm
POST /api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/insertion/abandon
GET  /api/v1/{instance_id}/playbill/documents
POST /api/v1/{instance_id}/playbill/documents/proposals
GET  /api/v1/{instance_id}/playbill/documents/{identity}
GET  /api/v1/{instance_id}/playbill/documents/{identity}/body
GET  /api/v1/{instance_id}/playbill/documents/{identity}/history
POST /api/v1/{instance_id}/playbill/explain
GET  /api/v1/{instance_id}/playbill/principals
POST /api/v1/{instance_id}/playbill/principals/proposals
GET  /api/v1/{instance_id}/playbill/proposals/{proposal_id}
GET  /api/v1/{instance_id}/playbill/proposals
GET  /api/v1/{instance_id}/playbill/proposals/{proposal_id}/refusal
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/review
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate
POST /api/v1/{instance_id}/playbill/proposals/{proposal_id}/readmit
GET  /api/v1/{instance_id}/playbill/sources/context
POST /api/v1/{instance_id}/playbill/sources/check
POST /api/v1/{instance_id}/playbill/sources/proposals
POST /api/v1/{instance_id}/playbill/subjects/proposals
GET  /api/v1/{instance_id}/playbill/subjects
GET  /api/v1/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}
GET  /api/v1/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}/history
POST /api/v1/{instance_id}/playbill/claim-types/proposals
POST /api/v1/{instance_id}/playbill/claim-types/migrations
GET  /api/v1/{instance_id}/playbill/claim-types
GET  /api/v1/{instance_id}/playbill/claim-types/{predicate}
POST /api/v1/{instance_id}/playbill/claims/proposals
POST /api/v1/{instance_id}/playbill/claims/proposals/batch
GET  /api/v1/{instance_id}/playbill/claims
GET  /api/v1/{instance_id}/playbill/claims/{identity}
GET  /api/v1/{instance_id}/playbill/claims/{identity}/history
POST /api/v1/{instance_id}/playbill/claims/{identity}/explanation
POST /api/v1/{instance_id}/playbill/claims/{claim_id}/retire
POST /api/v1/{instance_id}/playbill/queries/proposals
GET  /api/v1/{instance_id}/playbill/queries
GET  /api/v1/{instance_id}/playbill/queries/{name}
POST /api/v1/{instance_id}/playbill/queries/{name}/run
POST /api/v1/{instance_id}/playbill/next
POST /api/v1/{instance_id}/playbill/audit
POST /api/v1/{instance_id}/playbill/curation/list
POST /api/v1/{instance_id}/playbill/curation/overrule
POST /api/v1/{instance_id}/playbill/curation/accept-fixed
POST /api/v1/{instance_id}/playbill/curation/suppress
GET  /api/v1/{instance_id}/playbill/procedures/{name}/readiness
POST /api/v1/{instance_id}/playbill/procedures/{name}/bind
POST /api/v1/{instance_id}/playbill/procedures/{name}/runs
GET  /api/v1/{instance_id}/playbill/procedure-runs/{run_id}
POST /api/v1/{instance_id}/playbill/discover
POST /api/v1/{instance_id}/playbill/expand
POST /api/v1/{instance_id}/playbill/coverage/resolve
POST /api/v1/{instance_id}/playbill/floor/export
POST /api/v1/{instance_id}/playbill/search
POST /api/v1/{instance_id}/playbill/since
GET  /api/v1/{instance_id}/playbill/whoami
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
cruxible_playbill_authoring_abandon_insertion
cruxible_playbill_authoring_bind
cruxible_playbill_authoring_compile
cruxible_playbill_authoring_confirm_insertion
cruxible_playbill_authoring_prepare_publication
cruxible_playbill_authoring_create
cruxible_playbill_authoring_example
cruxible_playbill_authoring_get
cruxible_playbill_authoring_list_pending
cruxible_playbill_authoring_preflight
cruxible_playbill_authoring_resume
cruxible_playbill_authoring_status
cruxible_playbill_authoring_submit
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
cruxible_playbill_propose_claims
cruxible_playbill_claim_retire
cruxible_playbill_list_claims
cruxible_playbill_get_claim
cruxible_playbill_claim_history
cruxible_playbill_explain_claim
cruxible_playbill_propose_query_definition
cruxible_playbill_list_query_definitions
cruxible_playbill_get_query_definition
cruxible_playbill_run_query
cruxible_playbill_procedure_readiness
cruxible_playbill_procedure_bind
cruxible_playbill_procedure_run
cruxible_playbill_procedure_run_status
cruxible_playbill_discover
cruxible_playbill_expand
cruxible_playbill_export_floor
cruxible_playbill_resolve_coverage
cruxible_playbill_workspace_coverage_resolve
cruxible_playbill_workspace_coverage_status
cruxible_playbill_workspace_floor_export
cruxible_playbill_workspace_floor_status
cruxible_playbill_workspace_source_check
cruxible_playbill_workspace_source_compile
cruxible_playbill_proposal_list
cruxible_playbill_proposal_readmit
cruxible_playbill_claim_type_migrate
cruxible_playbill_seed_apply
cruxible_playbill_seed_plan
cruxible_playbill_search
cruxible_playbill_since
cruxible_playbill_audit
cruxible_playbill_curation_list
cruxible_playbill_curation_overrule
cruxible_playbill_curation_accept_fixed
cruxible_playbill_curation_suppress
cruxible_playbill_whoami
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
re-derive it.** Every Capture the served authoring surface produced *in this
slice* is content-addressed, and a CAS reference deliberately names no logical
source. By §11.6.1 a byte match at a working occurrence is therefore a labeled
`content_equivalent` candidate, and `exact` and `drifted` -- both of which
require the accepted and observed logical source to agree -- were not reachable
end to end from the CLI here. `build_ledger_capture` and the external
acquisition path already produced the logical-source-bound Captures that unlock
them, and no served operation invoked those builders. The resolver, the cards,
and their rendering are proved against real `exact`, `drifted`, and ambiguous
coverage in the headless suites. **PC-G-H1 closes this gap** -- see below.

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

## PC-G-H1: the logical-source capture path

This slice closes the PC-F2-S2 gap recorded above. `exact` and `drifted` are now
reachable end to end from the served surface, for foreign knowledge sources this
instance has never governed as Documents. **No new operation lands**: the whole
surface delta is one added variant on an existing request model, so the facade
operation inventory, the route inventory, the MCP tool inventory, and every
golden in this repository are byte-identical across the slice.

**One added authoring input.** `DirectClaimAuthoringV1.source_selection` is a
tag-discriminated union, and `playbill-direct-foreign-source-selection-v1` joins
it beside the CAS-span and typed-external forms. A proposer stores the foreign
file's bytes through the ordinary body-store operation, then names the logical
source and the byte window inside those bytes; `claim propose` and `claim
propose-batch` carry it with no flag and no new command, which is what the
authoring-JSON harnesses need. Three things are bound and they are deliberately
different things: the **coordinate** names the whole snapshot the proposer
presented, by content digest and length; the **selector** names the window
inside that snapshot; and the **commitment** is over the selected bytes alone,
because that is the unit a working occurrence is later matched against.

**One contract per logical source, and it earns its acceptance.**
`logical_source_identities` is an enumerated tuple, so a shared contract would
have to be *succeeded* every time a corpus grew a file. A per-source contract is
instead always new: the first authoring against a source writes it, and every
later one writes byte-identical content that deduplicates against the accepted
base. It carries no exemption. `DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT` is
exempted from the component and rule registry checks because it is the built-in
constant; a foreign-source contract is an ordinary artifact and passes
`evaluate_capture_contract_law` on its own terms, which is possible only because
reviewed code registered the components it needs in
`PLAYBILL_CAPTURE_COMPONENTS`.

**The provenance grade is honest, and it is honest structurally.** The
registered components are named for what actually happened: the provenance rule
is `playbill.external.proposer-asserted-v1`, not the daemon-fetched one, and the
replay policy promises only what retained bytes deliver. The grade itself now
follows a contract's declared provenance rule rather than an identity comparison
against one constant, so a second self-asserted contract cannot be graded as
though a daemon had fetched it. Only a contract pinning a registered rule can
carry one of those digests, so the derivation is exactly as narrow as the
comparison it generalizes and no existing contract's grade moves.

**What the Capture may and may not claim.** Its external reference is
`attested_only`: the daemon fetched nothing and can reach the foreign source
never, so `open_source` returns `attested_only` rather than a replay that did
not happen. What it *can* do is commit to the exact bytes it was shown, retain
them under bounded CAS materialization, and bind them to a logical source -- and
that binding is the entire point, because it is what makes an edit to that
source measurable as drift and a relocation within it not.

**The transcript F2-S2 could not write.** `tests/test_cli/test_playbill_coverage_surface.py`
now drives the whole §11.6 story through `cruxible ...` argv against a served
instance: govern a span of a foreign file, resolve the unchanged file to
`exact`, move the span inside the file and stay `exact` with the *same*
occurrence identity and a moved line overlay, edit the span and get `drifted`
carrying the complete §11.6.2 binding, and put the identical bytes under a
different logical source to get a labeled `content_equivalent` candidate that
inherits nothing. The resolver, the coverage indexes, and the render package are
unchanged; they were built for exactly this input and this slice supplies it.

## PC-G-H2: coverage delivered into a harness's tool results

Two adapters over the same operation, and the split between them is a finding of
this slice rather than a design preference.

**The maintainer-adjudicated amendment.** §11.7's Claude Code disposition asked
for transparent coverage on Read, Grep, Edit, and Write via `updatedToolOutput`.
Implementation against the shipped harness (2.1.234) established that
`updatedToolOutput` is not appended text: it is validated against each tool's
own output schema and then rendered by that tool's own mapper, which builds the
model-visible string from typed fields. Grep's content mode has a free-text
`content` field; Read's `file.content` is passed through a line numberer, so
appending would present cards as numbered file lines that do not exist in the
file; Edit and Write synthesize their result from `filePath`/`type`/
`userModified` with no free-text slot at all. The one channel reaching all four,
`additionalContext`, is rendered inside a `<system-reminder>` -- the §11.4
instruction channel §11.7 forbids for ordinary cards. The maintainer adjudicated
§11.7's disposition amended to these findings: the owned-harness middleware is
the arm-4 route (which is what §11.8 already prescribed for a benchmark owning
its tool executor), with the Claude Code plugin shipping as a Grep-only dogfood.

**`coverage/middleware.py` is the primary surface.** `before_tool`,
`after_tool`, and `after_filesystem_change` over a four-kind event model, taking
its resolve callable by *injection* -- which is what lets it embed in TauBench's
executor without the coverage package ever reaching the service layer, and is
therefore the architecture boundary paying for itself rather than costing.
Every entry point returns the original tool output and the appended coverage
text as **two separate strings**; the caller splices. That makes "original tool
output is preserved and annotated, not replaced or suppressed" structural: a
middleware returning one blob could silently drop the tool's own output and no
test could detect it.

**Bindings are declared in `.playbill/coverage.json`,** as exact entries or as
prefix rules whose normalizer is *named* rather than assumed.
`playbill-coverage-path-identity-v1` is deliberately non-lossy -- strip the
declared prefix, `/` to `.`, prepend the identity prefix, stop -- because every
lossy step (case folding, extension dropping) is a way for two working files to
collide onto one accepted source. `corpus/handbook.md` is `corpus.handbook.md`,
and that string must be exactly the `logical_source_identity` the PC-G-H1
Capture was authored against. A produced identity outside the frozen grammar
binds nothing, silently, as does any unbound path.

**The §11.8 flagship scenario** is in
`tests/test_cli/test_playbill_coverage_hook.py`: edit a governed span after
reading it, and edit one without reading it first, both exposing the affected
Claim within the same turn against a served instance, with the accepted
coordinate identical across the transcript and no compile, proposal, or
acceptance anywhere in it.

**Zero served-surface delta.** No operation, route, MCP tool, client method, or
golden moves; the only added surface is the `playbill hook post-tool-use` CLI
command, and the coverage package's no-authority import allowlist still holds
over both new modules.

## PC-G-H3: the seed bundle, the arm recipe, and one ordering defect

The last slice of the TauBench critical path. It adds one CLI group, one flag on
an existing command, a pure planning module, and a committed benchmark
directory. **Zero served operations, zero routes, zero MCP tools, zero client
methods, and zero golden diffs**; every propose call the seed command makes
existed before this slice, and `playbill/seed.py` writes the table of them out
by name so that is readable rather than asserted.

### The stash ordering defect, and the class it belongs to

`test_a_stash_captures_exactly_the_dirty_regions_bytes` failed roughly half the
time. The symptom was `body.region_ids != parsed.dirty_region_ids` carrying the
same two digests in swapped order; the cause was two different canonical
orderings for one list.

`NativeStashBodyV1` orders its regions by **region identity**, and its validator
enforces that — correct, because identity is path-free by §11.9.3 and a
digest-committed record must not order its fields by where a lens happened to
place a Claim. `NativeTreeParseV1.dirty_region_ids` returned the same identities
in **presentation** order: byte-sorted path, then position inside the file. Those
two orderings coincide only when the digests happen to sort the way the paths
do, and a region identity is a digest over the Claim's semantic address, so
which way that falls is decided afresh by every instance's own artifact
identifiers. Roughly a coin flip per run, which is exactly how it presented.

The fix is at the source and it is one line per accessor: `dirty_region_ids`,
`tampered_region_ids`, and `moved_region_ids` — on both the file parse and the
tree parse — return `byte_sorted`. `regions` still walks in presentation order,
because presentation order is what a person reading a file expects and what a
diagnostic should name; it is *identity lists* that may not carry presentation
coordinates. `native/sync.py` had already been wrapping `byte_sorted(dirty)`
around every commit point, which is the same defect being worked around one
caller at a time; those wrappers are now gone, so a regression in the accessor
surfaces instead of being papered over.

Two regression tests, and the split between them is deliberate. The constructed
one builds a tree parse whose first file deliberately holds the last-sorting
identity, so it fails before the fix on **every** run rather than half of them —
a coin-flip regression test is not a regression test. The rendered one asserts
the ordering as a law across all three routes to the list: the stash body's
committed order, the parse's identity list, and the render plan's
`stashed_region_ids`.

The class matters more than the instance. A digest-committed local format whose
field ordering depends on presentation is nondeterministic, and the local-format
family — checkpoint, coverage manifest, stash — exists to be reproducible. Any
future accessor returning region identities inherits the same rule, which is
written into `native/parse.py`'s module docstring rather than left to memory.

### The seed bundle is CLI orchestration, and could not have been one proposal

`playbill seed apply BUNDLE_DIR --name NAME` applies a directory of authoring
JSONs — `claim-types/`, `subjects/`, `documents/`, `claims/`,
`query-definitions/`, plus a `bodies/` subtree stored in CAS first — as the
fewest governed proposals it can legally become. The layout is the manifest:
there is no bundle manifest file, and a file outside those directories refuses
rather than being skipped, because silently applying part of a bundle makes
"this bundle was applied" untrue in a way nobody can see.

**Where the minimum comes from.** `DirectClaimAuthoringV1` already carries
dependency closures. A ClaimType or Subject that some bundle Claim declares in
`claim_type_artifact`, `subject_shell`, `dependency_claim_types`, or
`dependency_subject_shells` costs **no proposal at all**, because the batch
operation admits it in the same generation; the plan names each such entry and
the Claim that carries it. The planner never adds a closure an authoring did not
declare — deciding that a Claim should carry a Subject is an authoring decision
the admission laws adjudicate, not one a seeding convenience takes. The example
bundle's three Claims, one ClaimType, and three Subjects are therefore one
proposal, with the QueryDefinition a second because the served surface has a
singular propose operation for it and no plural one.

**Why applying is one group per invocation.** This was measured, not assumed:
opening two proposals against one accepted head and activating both fails with
`settlement base is not the current main ref`. A plan is therefore a *sequence*,
and the caller must activate each group before submitting the next. Approval is
an optional governed attestation and activation is the state-changing act; this
command performs neither. `--plan` prints the whole grouping offline and reaches
no daemon. The committed TauBench harness deliberately records a voluntary
approval while it loops plan → apply → approve → activate over the printed group
ids; a creator-suffices harness may loop plan → apply → activate instead.

**Refusals defer to the laws.** The one thing checked at plan time is the case
the propose operation would refuse anyway and that is cheaper to say early: two
entries putting different bytes at one canonical path in one change set — a
top-level ClaimType diverging from the one a Claim carries, or two Claims
carrying different copies. Everything else about admissibility is left to the
propose operation's own diagnostics, which are the authoritative answer. The
planner accordingly validates only the models it reads fields out of, and every
one of them lives inside the `playbill` package: `seed.py` imports no service
module and there is no import cycle to break.

### The floor and the native renders compose in the CLI, not the service

`playbill floor export --with-native` is the §11.8 native-surface amendment —
"the file floor in arms 3 and 4 includes the committed native knowledge renders
of §11.9". It is a **CLI** composition, deliberately: the floor service keeps
returning bytes and touching no filesystem, and the render lens keeps being a
pure function of accepted state, because the daemon having any path by which it
could write into a repository is exactly what §11.9.5's explicit-sync law
forbids.

**No manifest format moved.** The two exports share one directory without
knowing about each other, because they cannot collide: every floor artifact is
`.json` under its own prefix and every rendered page is `.md`, and the manifests
keep their own names — `manifest.json`, `render-manifest.json`, and
`coverage-manifest.json` naming the §11.6.3 boundary. The floor manifest
enumerates no `.md` path and does not mention the render manifest; a test
asserts both.

The native write is the *same* function `playbill native render` uses, factored
out rather than reimplemented, so the floor export inherits the §11.9.5
dirty-region refusal by construction. A second write path that skipped that
check would be a second way to lose an author's edits.

### The arm recipe is executable and committed

`benchmarks/playbill_taubench/` holds `recipe.py`, `seed-example/`, and a README.
Each of the six steps is a function, and
`tests/test_cli/test_playbill_taubench_arms.py` imports the committed recipe and
drives all of them against a served instance — there is no second implementation
of any step in the test, which is what makes "an integrator needs only this
directory" a proof rather than a claim. It runs in the ordinary suite in seconds;
unlike the adoption-scale benchmark it is setup, not a measured gate.

**Arms 3 and 4 differ by one boolean and that is asserted.** `build_arm`
produces both records through one code path, including constructing the
middleware for arm 3, which then never calls it. The two `ArmSetupV1` records
differ in exactly one field, `deliver_coverage`, and `run_turn` reads it to
decide whether to call `after_tool`. The smoke test asserts the one differing
field, the shared middleware configuration, and byte-identical arm workspaces
after the turn — the delivery adapter changed what the model saw and nothing
about what the tools did.

**The flagship, from the committed transcript.** Same event stream, same edit,
same turn. Arm 3 receives exactly the tool's own output on all three events and
says nothing at all. Arm 4 receives that same string plus a pure addendum:
`exact external:corpus.handbook.md …` on the read, then
`drifted external:corpus.handbook.md expected … observed … claims claims/83/CLM-….yaml … [commitment_superseded]`
on the edit, naming the affected Claim in the text the agent is already reading,
with the accepted coordinate identical across the transcript and no compile,
proposal, or acceptance in it.

**The run manifest pins what §11.8 requires pinned.** Index, overlay, manifest
digests and epoch off a coverage result the hooked arm received; generation,
semantic, compiler, and floor digests off the floor manifest's own coordinate;
render digest and lens version off the render manifest; the rule set and its
digest; and the seed plan digest, which excludes the invocation's proposal name
so it answers "is this the same world?" rather than "was this the same command?".
`hook_adapter.envelope_version` is `null` and recorded rather than omitted,
because the owned-harness middleware has no vendor hook envelope and "not
applicable" must not look like "forgotten" in a pinned manifest.

### Recorded backlog: what PC-G-proper still holds

The TauBench critical path is complete at this slice. Four PC-G items are
deliberately *not* in it and are recorded here rather than left implied:

| Item | Where it stands | What closing it needs |
|---|---|---|
| Procedure expand | `playbill expand` returns a context capsule for Subjects, Claims, and interfaces; Procedures are not a facet | a Procedure facet on the capsule, once dogfood shows which fields a reader actually wants |
| Journal ownership | `run["journal_record_digest"] is None`: the daemon opens no query-receipt journal, and the knowledge-loop smoke pins that | a daemon-owned journal and the decision about who owns retention — the same open seam PC-F2-S2 recorded |
| Lineage read | an earlier proposal can be *named* and the naming is checked against the shared target ref; no read enumerates a lineage | one served read operation over admissions, which is a contract change and was out of scope for both PC-F3 and this batch |
| Native TauBench task corpora | the recipe seeds a foreign-source world and runs one scripted turn; §11.8's native-knowledge tasks want tasks *about* rendered governed spans | a task corpus authored against OKF-rendered spans, per the §11.8 note that native-knowledge tasks are meaningful only with the §11.9 surface present |

The four PC-F3 seams recorded above (draft scope, foreign renders, lineage read,
capture path for rendered occurrences) are unchanged by this slice.

## PC-F3-S1b: the multi-Claim proposal operation

A change set already settles as one indivisible generation, and the evaluator,
the candidate record, and the closure proof have always been multi-member. What
the served surface lacked was a way for an *author* to reach that atomicity:
`playbill_propose_claim` wrote exactly one `claims/...` path per proposal, so a
Claim that is only meaningful beside its siblings -- a relation and the
vocabulary it discriminates, a reading and the metric it is a reading of -- had
to be split across generations that could each be accepted without the other.
This slice adds the plural operation. The facade operation inventory moves from
39 to 40.

**The change set it produces is ordinary.** Nothing about settlement,
evaluation, admission, or the wire format distinguishes a proposal carrying five
Claims from one carrying a single Claim, and no evaluator or law changed here.
The plural entrypoint loops the existing per-Claim authoring body over one
shared candidate tree and submits it once. `service_propose_claim_attestation`
has written a successor plus N competing Claims through the same ordinary path
since PC-B; this slice generalizes the authoring side to match.

**The singular operation is now a delegation, and that is asserted rather than
assumed.** `service_propose_playbill_claim` calls the plural service with a
one-element tuple and re-wraps the result in its unchanged contract. Its
per-authoring capture, pin, predecessor, and handoff handling is the same code
that ran before, so a single authoring produces the same candidate digest,
proposal id, and Claim artifact digest it produced at the previous head; a test
pins that identity by digest across the two entrypoints rather than by
inspection.

**Cross-authoring conflicts refuse before submit, and only cross-authoring
ones.** An authoring may always restate an artifact the accepted base already
holds -- the acceptance laws adjudicate that succession, and narrowing it would
change single-Claim behavior. What an authoring may never do is contradict a
*sibling* in the same change set, because there is no later moment at which two
byte strings at one path could be reconciled. So identical dependency artifacts
deduplicate silently across authorings, differing ones raise a typed
`ProposalIntegrityError` before the proposal service is reached, and two
authorings naming the same Claim path are refused outright. An empty authoring
set is likewise a typed refusal rather than an empty proposal.

**Deliberately unchanged: existing-statement disposition still reads the
accepted base.** The handoff law makes an author disposition every *accepted*
same-subject/predicate statement before stating an adjacent one. Sibling Claims
inside the same proposal are not accepted state, so they are not handoff
subjects -- competing Claims in one change set are exactly what the attestation
path already writes, and the resolution policy, not the author, adjudicates
them.

**The CLI adds a command rather than a flag.** `playbill claim propose` keeps
its single `--authoring` and its single-Claim result shape unchanged; the plural
form is `playbill claim propose-batch --authoring A --authoring B --name NAME`,
whose repeated flag names the set and whose distinct result shape does not
depend on how many files the caller passed. The route is a sub-resource of the
collection the Claims settle into, `POST .../playbill/claims/proposals/batch`,
rather than a sibling collection.

### The authorized golden re-pin

The same three served-surface pins move, additively, for the one added
operation:

| Golden | What changed |
|---|---|
| `tests/goldens/playbill/served-surface-dp0b-v1.json` | `facade_operations` 39 → 40 (`playbill_propose_claims`); `http_delegate_count` and `mcp_delegate_count` 39 → 40 |
| `tests/goldens/http_surface/http_surface_snapshot.json` | one added path, `POST /api/v1/{instance_id}/playbill/claims/proposals/batch`; zero existing paths removed or changed |
| `tests/goldens/cruxible_client/contracts_snapshot.json` | two added models, `PlaybillAuthoredClaim` and `PlaybillClaimBatchProposal`; zero existing models removed or changed |

Each was produced through its own regeneration path, and every other golden in
this repository is byte-identical across the slice.

## PC-F3-S3: the native surface closed, and what PC-G inherits

PC-F3 is review-complete at this slice. The batch shipped the render lens and
region grammar (S1), the compile contract and the CLI loop (S2), and here the
frozen laws, the stash, and review currency at its dogfood minimum. **No served
operation was added by any part of PC-F3.** The render is computed in the CLI
from reads that already existed, which is also what makes §11.9.5's
explicit-sync law structural: the daemon never produced the bytes, so there is
no path by which it could commit them into a repository.

### The five laws have one home

`tests/test_playbill/test_native_round_trip_laws.py` is the canonical home of
the §11.9.6 block. It quotes the spec paragraph it freezes and holds seven
tests: the five laws, each named after its law, plus the two preconditions the
paragraph states. Copies that previously lived beside the surfaces they
constrained were removed from `test_native_render_lens.py` and
`test_native_compile.py`, so weakening a law now means deleting a test that says
which law was deleted.

| Law | How it is proven |
|---|---|
| `compile(render(x, ctx))` is a no-op | a real seeded instance, rendered and compiled: zero members, drafts, refusals, and three-way rows |
| `render(parse(render(x, ctx)), ctx) == render(x, ctx)` | `native_render_from_tree` rebuilds the whole render -- manifest included -- from the rendered bytes alone, and the result is byte-equal |
| edit → compile → accept → render preserves the payload | the loop through the governed path: edit, compile, `service_propose_playbill_claims`, activate, re-render; the accepted Claim carries the new value and the fresh checkout compiles to nothing |
| edit derived field → typed refusal | one derived region edited *through the typed region structure* rather than by matching prose; tampered at parse with a regeneration instruction, refused at compile with the same instruction as its required action |
| dirty re-render refuses without stash/discard | the refusal names every dirty field and all three ways forward, and refuses both dispositions at once |
| precondition: no wall clock | observable half here (two contexts, two renders); structural half is the AST guardrail in `test_playbill_dp0_boundaries.py`, which this test names and asserts still exists |
| precondition: freshness never alters snapshot bytes | resolve coverage over a rendered tree and byte-compare the tree |

Law 2 is the one that needed new code. Parsing previously produced typed regions
and diagnostics, which is what compile needs and not enough to re-render from.
`native/inverse.py` adds the total decomposition: every byte of a rendered file
is a marker line, a region body line, or prose, and re-emitting rebuilds the
markers **from their parsed models** rather than copying them, so the
canonical-JSON payload has to round-trip. Every per-file baseline in the
reconstructed manifest -- content digest, byte length, disposition, region
identity, kind, address, artifact digest, body digest -- is recomputed from the
files rather than read out of the committed manifest. No accepted state is in
scope for the reconstruction.

What law 2 deliberately does **not** do is re-derive Claims from prose. Prose is
carried through verbatim, because inverting the semantic rendering would be a
second lens coupled to spellings §11.9 keeps class-3 through the dogfood. The
semantic direction is covered by laws 1 and 3 instead. Totality is claimed for
clean trees; a tree with broken region structure raises rather than guessing,
which is the compiler's business and not this law's.

**No spelling moved in this slice.** `NATIVE_LENS_VERSION` stays at 2 and
`native_renderer_digest()` is unchanged. Its history: v1 was the S1 grammar; v2
added the draft marker in S2. The version exists so a render can say which
grammar produced it, not so old spellings are preserved.

### The stash

`playbill native render --stash` captures the dirty regions' exact bytes before
a byte of the re-render lands, then proceeds. `--discard` is unchanged, the bare
dirty re-render still refuses, and the refusal now names all three answers --
compile, stash, discard -- because "stash or discard" hides the one an author
usually wants. Passing both dispositions is a contradiction and refuses.

`playbill native stash list | show | restore` read and re-apply those entries
with no daemon involved. A restore lands **by region identity**, which carries
no path, so a stashed edit lands correctly after its field moved to another
file; a stashed field the current render no longer has, or one that no longer
binds unambiguously, is reported and left in the stash rather than placed
somewhere it might not belong.

The format itself is pure: `native/stash.py` produces and consumes bytes and the
CLI writes them, exactly as the lens produces a tree and the CLI writes that.
The native package still imports no `os`, no `shutil`, and no `subprocess`, and
the architecture guardrail still asserts it.

### Review currency, and the read that is missing

`review-current` keeps `--bound DIGEST` as the explicit form and adds
`--superseded-proposal PROPOSAL_ID`, which reads the earlier proposal's
candidate digest *and* its signers through the ordinary review operation and
fills `superseded_signer_ids`, so the report can say whose approval no longer
verifies rather than only that some approval must be collected. It refuses a
proposal admitted against a different proposal ref, checked through the ordinary
proposal inspection: proposals sharing a target ref are successive attempts at
one change, and a proposal from another lineage is not evidence about this one.

Neither form can *discover* the earlier proposal. Admissions in one lineage
share a `target_ref`, and every served read resolves a proposal by identity
rather than enumerating them, so "which proposals preceded this one" is a
question the current surface cannot ask. Closing that gap wants one read
operation over admissions; adding a served operation was out of scope for this
batch, and the gap is recorded here and in the command's own docstring rather
than worked around.

### Recorded seams PC-G inherits

Four limits are deliberate, each shipped with the behavior that makes it safe:

| Seam | What holds today | What PC-G would add |
|---|---|---|
| Draft scope | one unlocated draft per file, and drafts bind Subjects only; a second draft marker in one file refuses | multi-draft files and non-Subject draft targets, once dogfood shows which is actually written |
| Foreign renders | every rendered file is `native_editable` or `orientation`; the `foreign_observed` disposition exists in the manifest, grammar, and compile guard, and nothing renders one | a lens that renders foreign-source material as observed-only pages |
| Lineage read | an earlier proposal is *named*, and naming it is checked against the shared target ref | a read operation over admissions, so a lineage can be enumerated |
| Capture path for rendered occurrences | drift cards over a rendered tree cite the region's own locator handle, computed from a disposable index built from the render baseline; no Capture is invented and nothing accepted references render output | if a rendered occurrence should become accepted evidence, an explicit Capture through the ordinary evidence path |

Also unchanged and intentional: subscription slicing is deferred (single-repo
whole scope, stated explicitly in `RenderContextV1` rather than left implicit),
there is no forge or LSP implementation, and there is no multi-lens rendering.

### The PC-G handoff inventory, in one list

1. **No served operation exists for the native surface, and none should be added
   casually.** The render, the parse, the compile, the stash, and review
   currency are all CLI-side over reads that already exist. A new operation here
   is a contract change, not a convenience.
2. **The lens is class-3.** Spellings may move; `NATIVE_LENS_VERSION` and
   `native_renderer_digest()` must move with them. The five laws and the typed
   editable/derived split are the contract, and the law block is where a change
   to them has to be argued.
3. **The law block is `tests/test_playbill/test_native_round_trip_laws.py`.** Do
   not add a sixth law elsewhere and do not re-prove one of the five beside a
   surface; that is what this slice consolidated.
4. **Every law test asserts a law, never a spelling.** The one test that must
   reach inside a derived region does so through the typed region structure. New
   tests in that file follow the same rule.
5. **The stash is local and disposable.** Deleting `.playbill-stash/` under a
   render root loses only stashed local edits. No test and no operator procedure
   may treat its presence as required.
6. **The native package may not touch a filesystem or a clock**, and may not
   import the service layer, the ledger, or the server. The guardrail is
   `test_pc_f3_native_render_adds_no_authority_and_reads_no_clock`, and it
   enumerates the package's modules, so adding one there is a deliberate act.
7. **Region identity is path-free.** Moves and renames preserve it, coverage
   reports it, and the stash restores by it. Anything that starts matching on a
   path is a regression against §11.9.3.
8. **Deletion is never inferred** -- of text, a locator, a file, or the whole
   rendered directory. No compile path produces a retirement, and
   `NativeCompileResultV1.retirements` is asserted empty.
9. **The four seams above are the known gaps.** Each has a test pinning today's
   behavior, so closing one means changing a test that says what changed.
10. **Verification scope for anything touching this surface:**
    `tests/test_playbill/test_native_*`, `tests/test_cli/test_playbill_native_*`,
    `tests/test_guardrails`, and `tests/test_architecture`. Adding a CLI command
    also touches `docs/cli-reference.md`, the command inventory in this guide,
    and `CLI_COMMANDS` in `cli/main.py` -- all three are guardrail-enforced.

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
lifecycle. PC-DEL1 later removed the remaining old graph-format readers and
their corpus. Importing any retired module no longer initializes a retired
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

### PC-DEL1 completion of the donor cut

PC-DEL1 removed the deferred `config`, `graph`, old `procedure`, old
`query`, `receipt_tree`, `workflow`, provider/reader, in-core client, and
legacy governance families together with their isolated tests and corpora. It
also retired the Playbill donor manifest because no adapter or served path
still consumes those packages. The final fix round removed the standalone
`cruxible_core.predicate` module after its last consumers disappeared.

The active Procedure and query contracts live in `cruxible_client.contracts`;
the active execution and read paths live below `cruxible_core.playbill`.
Architecture tests now assert the deleted family directories contain no source
files and that served dependency closure cannot reintroduce them.

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
| `playbill-native-stash-v1`, inside `playbill-native-stash-file-v1` | `<render root>/.playbill-stash/stash-<digest>.json` | Local edits a re-render would otherwise have overwritten: the exact bytes of each dirty region, with the region identity, address, and baseline digest needed to put them back. |

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

The native stash is the one PC-F3 added, and it is local state by the same four
properties with one difference in *where*. It sits under the **render root**
rather than the instance's managed root, because the material it holds is the
repository's: a render may be produced against a daemon whose filesystem the CLI
cannot write to at all, and edits to a checkout belong beside the checkout. The
dot-prefixed directory keeps it out of the rendered tree the render manifest
describes, so no stash entry is ever read as rendered material. It never enters
the ledger, the CAS, a journal, or an export; every entry re-verifies its own
digest on load and an entry that does not reproduce is deleted rather than
restored; and deleting the directory costs exactly the local edits somebody
chose to stash, which is the same loss §11.9 already makes the whole risk
surface of a rendered tree. Its `written_at` sits outside the digest preimage,
so stashing the same edits twice produces one identity rather than two entries
that differ only in when somebody typed the command. Its self-digest kind,
`NativeStashDigest`, is declared in `native/stash.py`. The module itself touches
no filesystem -- it renders and parses bytes, and the CLI writes them
atomically -- because the native package structurally may not import `os`.

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

The semantic parity recording at
`tests/data/playbill_parity/modeling-parity-oracle-v1.json` is historical
comparison data only, not a live verification oracle. Its original donor
configurations and Procedure digest corpus are recoverable from repository
history, not duplicated in the live verification tree.

## Intentionally retained package dependencies

PC-DEL1 pruned only dependencies made unreachable by its approved families.
The remaining dependency inventory is:

| Dependency | Why retained after DP-0 |
|---|---|
| `cruxible-client` | Reduced Playbill HTTP client shipped with the core |
| `pydantic` | Active Playbill/client contracts and donor validation models |
| `packaging` | Requirement parsing in the architecture dependency audit |
| `pyyaml` | Active source-catalog CLI and packaged client source-catalog paths |
| `structlog` | Active daemon audit/request logging and permission checks |
| `click` | Active four-group CLI |
| `cryptography` | Ed25519 principal keys, signatures, and Git verification |
| `httpx` | Active reduced client/CLI HTTP transport |
| `fastapi` | Active HTTP daemon |
| `python-multipart` | Retained daemon multipart-form dependency |
| `uvicorn` | Active daemon launcher |
| `mcp` (optional extra) | Active Playbill MCP server |

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
uv run pytest -q tests/test_guardrails tests/test_architecture \
  tests/test_cli/test_playbill_target_visibility.py
```

The architecture suite pins the public registration catalogs, immutable oracle
commits, deleted-family absence laws, and this guide's inventories. The RR
`change_head` must name the exact commit that passed those checks.
