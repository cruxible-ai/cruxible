# MCP tool reference

The MCP surface is Playbill-only. All tools delegate to the same service core as
HTTP and CLI.

`CRUXIBLE_MCP_PROFILE` takes two values. `default` (or unset) advertises the
everyday agent loop:

- orient and pick work: `cruxible_playbill_orient` (the map of accepted state),
  `cruxible_playbill_search`, `cruxible_playbill_next`, and
  `cruxible_playbill_expand`;
- any question over state, values first: `cruxible_playbill_query`;
- one thing by any reference, values first: `cruxible_playbill_get` (`detail`
  picks summary, evidence, why, history, proof, or a Document body range);
- Claim, ClaimType, and Subject reads: `cruxible_playbill_claim_values` (a status
  table for one Subject kind), `cruxible_playbill_list_claims`,
  `cruxible_playbill_get_claim`, `cruxible_playbill_explain_claim`,
  `cruxible_playbill_list_claim_types`, `cruxible_playbill_get_claim_type`,
  `cruxible_playbill_list_subjects`, `cruxible_playbill_get_subject`, and
  `cruxible_playbill_run_query`;
- the write verbs: `cruxible_playbill_set` (one value in one field, replacing
  the live value), `cruxible_playbill_retire` (end one live Claim), and
  `cruxible_playbill_write` (set, add and retire changes as one change set);
- proposals through activation: `cruxible_playbill_proposal_list`,
  `cruxible_playbill_review`, `cruxible_playbill_approve`, and
  `cruxible_playbill_activate`;
- identity and versions: `cruxible_playbill_whoami` and `cruxible_server_info`.

`full` advertises the complete catalog below, including the `authoring_*`
intent tools, curation, coverage, the
floor, sources, blocks, kits, Procedures, Lines, and the split approval pair
`cruxible_playbill_prepare_approval` and `cruxible_playbill_submit_approval` for
a signer outside the MCP process. Curation changes
discoverability only; permission tiers still gate every call. There is no
separate version tool: `cruxible_playbill_whoami` and `cruxible_server_info`
both report the MCP adapter's package version and the daemon's (`GET /version`).

Every tool that acts on one instance takes an optional `instance_id`. Omitted,
it defaults to `CRUXIBLE_INSTANCE_ID` in the MCP server's own environment (set it
in the `env` block of the MCP client config), and then to the instance the MCP
workspace's `.playbill/coverage.json` binds, provided the binding names the same
daemon the server is configured for; a binding on another daemon is refused. The
server never reads remembered CLI context. With none of these, the call fails and
names `CRUXIBLE_INSTANCE_ID`. `cruxible_playbill_whoami` returns the instance it resolved together
with the caller's identity there.

`CRUXIBLE_MCP_WORKSPACE_ROOT` selects the client-owned workspace for tools that
read or write local files. The stdio MCP process is the client-side adapter; the
workspace defaults to its working directory.
All tool paths are normalized relative paths confined under that root; the
daemon receives bytes and typed observations, never a client filesystem path.
Floor operations always target the containing Git worktree's canonical
`.playbill/floor`. With no explicit workspace root, the adapter discovers that
worktree from its working directory. An explicit `CRUXIBLE_MCP_WORKSPACE_ROOT`
must equal the worktree root for floor export, status, and activation refresh;
a nested explicit root is refused rather than allowing a write above its
configured filesystem boundary. When the root is in no Git worktree at all,
`cruxible_playbill_activate` still activates and reports
`floor_refresh.status: not_configured` with the reason.

`CRUXIBLE_MCP_KEY_DIR` names the directory of local approval keys that
`cruxible_playbill_approve` signs with: an absolute path outside the workspace
holding `<signer_id>.ed25519`, the layout `cruxible playbill principal add
--key-dir` and `cruxible playbill init --key-dir` write. Set it in the MCP
server's environment; no tool argument can name a key path. The tool signs as
`signer_id`, or as the directory's only key when `signer_id` is omitted, and
passing the reviewed `candidate_digest` makes it refuse a candidate that
changed since review. Unset, the tool refuses and says how to configure it.
Only the public attestation leaves the process; key bytes never appear in a
result or a log line.

## Which verb each tool publishes

`tests/goldens/playbill/served-surface-dp0b-v1.json` is the machine-readable
inventory of the whole served surface, and its `surface.mcp_tools` rows carry
`facade_operations`: the facade verbs each tool reaches, per tool. A deployment
that decides per-verb what may be reached over MCP reads the join there rather
than inferring it from the `cruxible_<verb>` / `handle_<verb>` spelling, which
nothing guarantees. `surface.mcp_facade_operations` still bounds the whole lane.

The list is a reachability closure, not a read of the handler's own body: it
covers the verbs the handler names itself, the verbs reached through a local
adapter object it constructs, and the verbs reached through a sibling handler
it delegates to. An empty list therefore means the tool reaches no facade verb
at all -- one tool is in that position today,
`cruxible_playbill_authoring_example`, and it is `READ_ONLY`. A mutating tool may not publish an empty list without a declared
exception naming its reason
(`tests/test_guardrails/test_playbill_v1_served_surface.py`), because an
overlay reading `[]` as "reaches nothing" would fail open.

Every row is covered by the snapshot's `succession.surface_digest`, so a tool
that starts reaching one more verb moves the pin.

## Runtime

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_server_info` | Return the adapter and daemon versions with daemon metadata; an instance-scoped credential gets its own instance's host and identity instead of a refusal | `READ_ONLY` |

## Host and initialization

Allocating a host (`cruxible playbill host create`), releasing its worktree
(`cruxible playbill workspace detach`) and decommissioning an instance
(`cruxible playbill instance decommission`) are operator acts with no MCP tool.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_init` | Bootstrap a host with public principal records | `ADMIN` |
| `cruxible_playbill_provider_catalog` | List provider packages from the configured daemon repository | `READ_ONLY` |
| `cruxible_playbill_provider_install` | Install exact package bytes and propose its definitions, without execution grants | `ADMIN` |

## Kits

A kit is a release of definitions (ClaimTypes, CaptureContracts, QueryDefinitions)
that installs
as a diff against the consumer: one proposed change set that adds, replaces and
retires definitions. Adding or removing a kit only proposes; activation and any
approval stay the ordinary steps.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_kit_build` | Export the definitions under owned identity prefixes as one kit release | `READ_ONLY` |
| `cruxible_playbill_kit_status` | List installed kits and the kit paths edited since install | `READ_ONLY` |
| `cruxible_playbill_kit_add` | Propose installing or upgrading a kit as one change set | `GOVERNED_WRITE` |
| `cruxible_playbill_kit_remove` | Propose retiring every artifact a kit installed | `GOVERNED_WRITE` |
| `cruxible_playbill_evidence_rules_upgrade` | Propose moving ClaimTypes to identity evidence rules | `GOVERNED_WRITE` |

## Documents and proposals

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_store_body` | Store inert body bytes in CAS | `GOVERNED_WRITE` |
| `cruxible_playbill_propose_document` | Propose a canonical Document envelope | `GOVERNED_WRITE` |
| `cruxible_playbill_inspect_proposal` | Inspect a frozen candidate | `READ_ONLY` |
| `cruxible_playbill_inspect_refusal` | Inspect deterministic refusal evidence | `READ_ONLY` |
| `cruxible_playbill_review` | Render review material | `READ_ONLY` |
| `cruxible_playbill_prepare_approval` | Return the exact approval challenge | `READ_ONLY` |
| `cruxible_playbill_submit_approval` | Submit a public signed attestation | `GRAPH_WRITE` |
| `cruxible_playbill_approve` | Challenge, sign with a local key from `CRUXIBLE_MCP_KEY_DIR`, and submit in one call | `GRAPH_WRITE` |
| `cruxible_playbill_activate` | Activate by compare-and-set and refresh any configured workspace floor | `GRAPH_WRITE` |
| `cruxible_playbill_proposal_list` | List one page of open and terminal proposal evidence (`limit`, `cursor`) | `READ_ONLY` |
| `cruxible_playbill_proposal_readmit` | Re-admit a stale proposal at the current head | `GOVERNED_WRITE` |
| `cruxible_playbill_proposal_withdraw` | Retire an open proposal that will never activate | `GOVERNED_WRITE` |
| `cruxible_playbill_whoami` | Name the resolved instance, the credential-derived actor's identity and registration there, and the adapter and daemon versions | `READ_ONLY` |

MCP never accepts a client private key. Signing occurs outside the server and
outside the language server/MCP process.

## Accepted reads

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_list_documents` | List accepted Documents and coordinate | `READ_ONLY` |
| `cruxible_playbill_get_document` | Read an accepted Document envelope | `READ_ONLY` |
| `cruxible_playbill_read_capture` | Verify retained Capture evidence and read bounded material | `GOVERNED_WRITE` |
| `cruxible_playbill_dereference` | Read permission-gated body bytes | `GOVERNED_WRITE` |
| `cruxible_playbill_history` | Read accepted history | `READ_ONLY` |
| `cruxible_playbill_explain` | Explain governance, provenance, coverage, and history | `READ_ONLY` |

## Sources

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_source_context` | Return source alignment context | `READ_ONLY` |
| `cruxible_playbill_source_check` | Check a compiled `bundle`, or the sources a workspace `catalog_path` declares, against accepted state | `READ_ONLY` |
| `cruxible_playbill_propose_source_bundle` | Propose a frozen compiled bundle | `GOVERNED_WRITE` |
| `cruxible_playbill_workspace_source_compile` | Read catalog-declared workspace bytes and derive a source bundle | `READ_ONLY` |

`cruxible_playbill_source_check` takes exactly one of `bundle` (for programmatic
clients that compiled one) or `catalog_path`. With a catalog path the adapter
owns local path traversal and digest construction, so an agent supplies catalog
paths and root aliases, not compilation wire.

## Principals

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_list_principals` | List accepted public principals | `READ_ONLY` |
| `cruxible_playbill_compiler_upgrade` | Propose an exact compiler transition; signed approval and activation use the ordinary proposal workflow. | `ADMIN` |
| `cruxible_playbill_propose_principal_change` | Propose rotation, revocation, or recovery | `ADMIN` |

## Subjects, ClaimTypes, and Claims

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_list_subjects` | One page (`limit`, default 50; `cursor`) of Subject rows (`subject_kind`, `subject_id`, `lifecycle`, live Claim count), optionally of one `subject_kind`; `get_subject` reads one | `READ_ONLY` |
| `cruxible_playbill_get_subject` | Read one accepted Subject | `READ_ONLY` |
| `cruxible_playbill_subject_history` | Read one Subject's accepted lineage | `READ_ONLY` |
| `cruxible_playbill_propose_claim_type` | Propose a governed predicate interface | `GOVERNED_WRITE` |
| `cruxible_playbill_list_claim_types` | List the accepted predicate vocabulary | `READ_ONLY` |
| `cruxible_playbill_get_claim_type` | Read one accepted ClaimType | `READ_ONLY` |
| `cruxible_playbill_claim_type_migrate` | Compose a ClaimType successor with dependent dispositions | `GOVERNED_WRITE` |
| `cruxible_playbill_claim_attest` | Sign and append a support, contradict, or unsure observation of the current exact Claim; pass `capture_digests` (and optionally `referent_coordinate`) to attest on new Captures you examined instead of the Claim's own citations | `GOVERNED_WRITE` |
| `cruxible_playbill_list_claims` | List accepted Claims by Subject, `subject_kind` or predicate | `READ_ONLY` |
| `cruxible_playbill_claim_values` | Status table: each live Claim's `subject_id`, value and verdict for every Subject of one kind (or named `subject_ids`) and the given predicates | `READ_ONLY` |
| `cruxible_playbill_set` | Put one value in one field of one Subject (`kind/id`), replacing the live value without its Claim ID; a missing Subject of a known kind is added; `evidence` defaults to `because` as self evidence (an exact-content value is its own evidence); accepts in the same call when policy and tier allow it, else answers `awaiting_approval` with the eligible approvers and the approve call; `dry_run` writes nothing; `at` refuses `playbill.write.slot_changed` if the field moved since; each change carries its `verdict`, and a verdict other than `supported` comes with a warning and its repair | `GOVERNED_WRITE` |
| `cruxible_playbill_retire` | End one live Claim, by Claim ID or by Subject and single-value field, with its dependent Claims, in one change set | `GOVERNED_WRITE` |
| `cruxible_playbill_write` | Apply `set`, `add` (one more value in a many-valued field) and `retire` changes as one change set, accepted or refused together | `GOVERNED_WRITE` |
| `cruxible_playbill_get` | Read one thing by any reference (Claim id or prefix, `kind/id`, predicate, `Document:`/`Procedure:`/`query:`/`CaptureContract:<name>`, artifact path, proposal id, or an operational reference: `Line:<name>` or the Line identity digest `next` names, `CAP-<12+ hex>`/`Capture:<digest>`, `ResolutionContract:<name>`, `Mandate:<name>`); `detail` is `summary` (values-first card with verdict flags; a string value over 500 characters is cut to `{value, truncated: true, length}`, and Subject rows name each value's `claim`), `evidence` (with the whole value), `why`, `history` (newest first, paged by `limit` and `cursor`), `proof` (with the full `accepted_coordinate`), or `body` with a byte `range`; other answers carry a compact `coordinate` (12-hex git oid prefix and `generation`); a wrong name refuses with the nearest names | `READ_ONLY` |
| `cruxible_playbill_get_claim` | Read one accepted Claim | `READ_ONLY` |
| `cruxible_playbill_claim_history` | Read one Claim's accepted lineage | `READ_ONLY` |
| `cruxible_playbill_explain_claim` | Explain a Claim's verdict and evidence | `READ_ONLY` |

A proposal is not accepted state. A Claim's verdict is computed at read time
from accepted law evidence, never carried forward from acceptance.

## Authoring intents

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_authoring_create` | Create or recover a durable authoring intent | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_example` | Return a model-generated ClaimType/Claim/Procedure input | `READ_ONLY` |
| `cruxible_playbill_authoring_get` | Read one authoring intent | `READ_ONLY` |
| `cruxible_playbill_authoring_resume` | Return an intent's durable continuation | `READ_ONLY` |
| `cruxible_playbill_authoring_list_pending` | List the caller's pending intents | `READ_ONLY` |
| `cruxible_playbill_authoring_compile` | Create or update an intent and preflight it | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_bind` | Read an anchored workspace selection, derive commitments, and compile | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_preflight` | Produce a binding certificate and repair frontier | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_rebase` | Rebase a stale intent onto the current accepted coordinate | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_submit` | Idempotently submit a passing intent | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_status` | Read the causal path to acceptance | `READ_ONLY` |
| `cruxible_playbill_authoring_abandon_insertion` | Release a publication expectation an instance already holds | `GOVERNED_WRITE` |
| `cruxible_playbill_block_declare` | Register one projection block a workspace just stamped into its page | `GOVERNED_WRITE` |
| `cruxible_playbill_block_depublish` | Release the registration that demands one page block, whichever road declared it | `GOVERNED_WRITE` |

The coordinator mints every identity, digest, base, timestamp, and proposal reference.
It reports approval conditions but never obtains or impersonates an approval.

`cruxible_playbill_authoring_create` takes one tagless input, and the
`change_set` kind carries any mix of members -- `claim`, `claim_type`,
`claim_retirement`, `subject`, `query_definition`, `procedure`,
`procedure_mandate`, `acquisition_policy`, `line` -- as one intent that admits
or refuses whole, typed to the
offending member index. `approval_policy` and `procedure_runtime_policy` parse
as members but a change set refuses them; send each as its own singleton input.
There is no second batch tool.
A `claim_type_succession` member succeeds an accepted ClaimType and dispositions
its whole reverse-pin closure in the same generation, so vocabulary evolution
needs no second tool and no second generation either.
`cruxible_playbill_authoring_example` serves `change-set` and
`claim-type-succession` as starting points, and `procedure`, `line`,
`acquisition-policy` and `procedure-mandate` templates that are accepted
together. A `line` input's `parameters` is checked against its Procedure's input
contract at authoring; its `acquisition_policy_name` is needed only when the
Procedure has Source nodes.
The publication tools take an `expectation_id` because a set that publishes
several Claims owns one expectation per publishing member; an intent that owns
exactly one may omit it.

## Procedures

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_procedure_readiness` | Report exact binding requirements or run readiness | `READ_ONLY` |
| `cruxible_playbill_procedure_bind` | Attach accepted input-plane bindings through a same-identity successor | `GOVERNED_WRITE` |
| `cruxible_playbill_procedure_run` | Execute a ready Procedure -- accepted-state reads, deterministic computation, and graph-v4 `source` reads through an accepted Provider -- at an explicit coordinate and time | `READ_ONLY` |
| `cruxible_playbill_procedure_run_status` | Read one finalized Procedure run and its receipt | `READ_ONLY` |
| `cruxible_playbill_procedure_measure` | Evaluate due Procedure measurements from real evidence, persist the resolution, and credit one run's exact grain | `GOVERNED_WRITE` |
| `cruxible_playbill_procedure_readings` | Inspect measurement standing and retained exact-grain readings (read-only, paginated) | `READ_ONLY` |
| `cruxible_playbill_line_check` | Read trigger eligibility, exact matches, and admitted occurrences without queuing or running. | `READ_ONLY` |
| `cruxible_playbill_line_arm` | Arm a Line forward-only: the daemon admits what it matches under the caller's credential, rechecked before each run. Repeating it unchanged returns `outcome: already_armed`. | `GOVERNED_WRITE` |
| `cruxible_playbill_line_disarm` | Stop a Line admitting work on its own; admitted runs keep going. A stopped arm returns `outcome: already_disarmed`. | `GOVERNED_WRITE` |
| `cruxible_playbill_line_status` | Read a Line's arm, its pending work, and why an arm stopped. | `READ_ONLY` |
| `cruxible_playbill_line_evaluate` | Evaluate an explicit historical range into pending work; never executes. | `GOVERNED_WRITE` |
| `cruxible_playbill_line_dispatch` | Admit retained pending occurrences under the current caller’s authority. | `READ_ONLY` |
| `cruxible_playbill_line_run` | Trigger one due accepted Line occurrence; a Line that can propose or settle needs a mandate, an observe-only one none | `READ_ONLY` |

`procedure_run`, `line_run` and `line_dispatch` are read-tier only for targets
that observe. A Procedure whose terminals can propose or settle, or a Line whose
runs can (its Procedure's capability capped by its `max_authority`), needs
`GOVERNED_WRITE` to run or dispatch, whichever door triggers it; the daemon
decides this per target, and a read-only caller is refused with
`PermissionDeniedError`.

Read-tier Procedure runs append receipted journal records, following the same
precedent as QueryDefinition runs. They never alter accepted state or grant
themselves a governed track record; promotion remains a separate governed act.

## Predictions

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_resolution_contracts` | Find governed tests of a Claim, by `claim_id` | `READ_ONLY` |
| `cruxible_playbill_predict` | Propose a governed resolution contract whose hypothesis is a Claim ID | `GOVERNED_WRITE` |
| `cruxible_playbill_settle` | Settle one prediction (`prediction_id`) from the Claim ID of an accepted observation (`observation`) | `GOVERNED_WRITE` |

Every Claim version these tools need can be a plain Claim ID (`CLM-...` or
`Claim:CLM-...`); the daemon resolves its digests and accepting coordinate. The
exact reference, and settle's full `request` (exact contract reference, anchor
event, or terminal evidence), remain as the advanced form.

Prediction settlement records the activation and resolution in operational
exhaust; it does not create or mutate Claims, and it does not create a second
authority plane beside accepted state.

## Queries, discovery, and the floor

`cruxible_playbill_query` takes exactly one mode: compact or a query `name`
(a full spec runs through `cruxible_playbill_query_spec`, in the `full`
profile, so the default tool stays small). Compact mode names a Subject
`kind` (or `ClaimType` / `Procedure` for definitions) and/or free text
`contains`. Each `where` filter is `{field, <operator>: value}` with one of `eq`,
`ne`, `lt`, `lte`, `gt`, `gte`, `in` (a list), `exists` (a boolean) or
`contains` (case-insensitive text); filters combine as all-of. A field is a
predicate's full name, its name after the `KIND.` prefix (`adoption_state`),
`subject_id`, or `alias.field` after `follow: [{field, as}]`; columns show that
short name unless it is itself another predicate's full name or a reserved name
(`subject_id`, `subject`, `kind`, `predicate`, `claim`, `flags`, `value.*`). Values are checked against the
ClaimType first: an unknown kind, field or enum member, or an operator that does
not apply, refuses with a code, the nearest valid names and a repair. `ne` means
no value equals, so a Subject without the value matches. `contains` alone
searches every live Claim value across kinds. Rows lead with values (an array
for a many-valued predicate or a contested slot) and carry `flags` (`stale`,
`contested`, `contradicted`, `unsure_hold`); without `select` a kind shows up to
12 predicates and names the rest in `notes`. `subject`, `subject_id` and `flags` are row metadata; a column with one of those names is served as `value.<name>`. ClaimType rows name the
CaptureContracts their evidence rules admit, never digests. `receipt` records the
mode, the definition digest, the coordinate and the evaluation time (default
now).

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_list_query_definitions` | List accepted entrypoints | `READ_ONLY` |
| `cruxible_playbill_get_query_definition` | Read one entrypoint's contract | `READ_ONLY` |
| `cruxible_playbill_run_query` | Execute an entrypoint with a replay receipt | `READ_ONLY` |
| `cruxible_playbill_query` | Answer any question over accepted state in one call: compact (`kind` and/or `contains`, with `where` filters shaped by operator, `select`, one-hop `follow`, `order_by`) or a query `name` with `params`; rows of values with `flags`, paged by `limit` and `cursor` | `READ_ONLY` |
| `cruxible_playbill_query_spec` | Run one full `QueryDefinitionSpecV1` inline (`spec`, `limit`, `cursor`, `at`, `evaluation_time`) with the same evaluation, rows, flags and paging as `cruxible_playbill_query`; `full` profile only | `READ_ONLY` |
| `cruxible_playbill_discover` | Find interfaces and Subjects by name | `READ_ONLY` |
| `cruxible_playbill_orient` | Map accepted state in one call: each Subject kind with its live count and predicates (type, cardinality, enum members, accepted evidence as CaptureContract names), artifact counts, named queries, `you` (can this caller author, and why not), `attention` from the `next` queue (with `arms`: the instance's Line arms by state and the stalled or stopped Lines by name, no daemon scope needed), and `next` suggestions written as MCP tool calls; `kind` reads one kind in full with sample Subject IDs, `section` pages `documents`, `procedures`, `claim_types`, `queries`, `interfaces`, or an operational family -- `runs` (Procedure runs, running first then newest, keyset-paged; read one with `cruxible_playbill_get(ref="ProcedureRun:RUN-...")` for its live progress), `lines`, `captures`, `capture_contracts`, `predictions`, `mandates` -- which the map only counts under `artifacts` (`limit`, `cursor`) | `READ_ONLY` |
| `cruxible_playbill_search` | Search, list, or orient over accepted state | `READ_ONLY` |
| `cruxible_playbill_since` | Read signed accepted ChangeSet members after a generation | `READ_ONLY` |
| `cruxible_playbill_next` | Rank outstanding repair work, each row with its exact next operation; observes the MCP workspace's floor and declared sources as `cruxible playbill next` does | `READ_ONLY` |
| `cruxible_playbill_policies_in_force` | List one page of live standalone and embedded governed policies (`limit`, `cursor`) | `READ_ONLY` |
| `cruxible_playbill_audit` | Rank visible Claim verification work and record completed coverage | `READ_ONLY` |
| `cruxible_playbill_curation_list` | List one page of curation patterns (`limit`, `cursor`) and ingest an explicit declared-block observation | `READ_ONLY` |
| `cruxible_playbill_curation_overrule` | Close an inapplicable detector-version item with attribution | `GOVERNED_WRITE` |
| `cruxible_playbill_curation_accept_fixed` | Link an item to an exact related accepted ChangeSet | `GOVERNED_WRITE` |
| `cruxible_playbill_curation_suppress` | Hide open work by item, pattern, or instance without resolving it | `GOVERNED_WRITE` |
| `cruxible_playbill_expand` | Expand one address into a context capsule | `READ_ONLY` |
| `cruxible_playbill_floor_export` | `mode=bytes` returns the greppable floor as base64 bytes; `mode=write` verifies and exactly replaces `.playbill/floor` under the MCP workspace (status `unchanged` when it already holds this floor); `mode=status` reports whether that floor is current, stale, or absent | `READ_ONLY` |
| `cruxible_playbill_coverage` | Resolve working sources against accepted state, from `observations` you built or from workspace `bindings` plus a file selection (`files`, `ranges`, `grep_results_path`, or `whole_working_set`) | `READ_ONLY` |

`cruxible_playbill_next` renders each repair's `command` as the MCP tool call
that performs it (for example `cruxible_playbill_settle(prediction_id="RSC-...")`,
adding the observation's Claim ID), or none when its operands are local files.
A row or nested finding whose repair the session cannot perform -- its profile
does not advertise the tool that performs it, or its tier is too low -- stays in
the queue with `repair: null` and `repair_requires: {tool, tier, because,
profile?}` naming what running it needs, so `orient` attention and the queue
count it for every caller and `status.hidden` stays 0. A status facet keeps its
state either way, but drops a repair the session cannot perform and says
`repair_hidden: true` with the same `repair_requires`. The `default` profile
advertises neither `cruxible_playbill_settle` nor the Line tools, for example:
a stopped Line arm still shows as `consumer_stalled`, its repair withheld with
`because: ["profile"]`.

Lists that can outgrow one answer are paged. `proposal_list`,
`policies_in_force` and `curation_list` take `limit` and `cursor`; a cut page
carries top-level `truncated: true` and a `next_cursor` to pass back as `cursor`.
A cursor whose listing changed since its first page is refused as
`playbill.list.cursor_stale`; list again without it.
`search` pages the same way with its structured cursor. `discover` has no
cursor; its top-level `truncated` says a budget clipped the hits.

Query execution is a read: it returns the result together with its
`playbill-query-execution-receipt-v1`. Qualifying direct reads, query/search
matches, coverage delivery, and Procedure dependency resolution also append
idempotent per-artifact touches to the daemon-local operational store. A
`READ_ONLY` actor can therefore grow that store, but these records never alter
accepted state or any semantic/generation root. Audit likewise appends an
idempotent completed-run record, but audit and curation never create
qualifying consumption touches and never execute Procedures or emit repair
recommendations. The floor export returns bytes keyed by floor path;
materializing a directory is a client act.
Coverage resolution takes observations -- a declared logical-source binding and
the bytes the caller read -- rather than paths, so the daemon reads no client
filesystem. It appends no receipt: it changes no accepted state, and the
evidence-index, overlay, and manifest digests it returns reproduce the answer.

## Seed bundles

| Tool | Purpose | Permission |
|---|---|---|

Seed application stores referenced bodies and composes only existing proposal
and authoring operations. It never approves or activates. Plan and operation
digests are adapter-owned outputs; callers choose the bundle, label, and group.

## Permission tiers

Read operations require read_only. CAS/proposal operations require
governed_write. Approval submission and activation require graph_write. Host
allocation, initialization, and principal changes require admin.

The daemon capability ceiling and bearer credential tier both apply. A
Playbill principal signature is an additional governance condition, not a
replacement for transport authorization.
