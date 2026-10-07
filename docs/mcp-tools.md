# MCP tool reference

The MCP surface is the governed tool set. All tools delegate to the same service core as
HTTP and CLI.

`CRUXIBLE_MCP_PROFILE` takes two values. `default` (or unset) advertises the
everyday agent loop:

- read with three verbs: `cruxible_orient` (the map of accepted state,
  one kind in full, or one paged section), `cruxible_query` (any
  question as rows of values with verdict flags) and `cruxible_get`
  (one thing by any reference; `detail` picks summary, evidence, why, history,
  proof, or a Document body range);
- the work queue: `cruxible_next`;
- the write verbs: `cruxible_set` (one value in one field, replacing
  the live value), `cruxible_retire` (end one live Claim), and
  `cruxible_write` (set, add and retire changes as one change set);
- proposals through activation: `cruxible_proposal_list`,
  `cruxible_proposal_review`, `cruxible_proposal_approve`, and
  `cruxible_proposal_activate`;
- identity and versions: `cruxible_whoami` and `cruxible_server_info`.

To find something by name, grep the floor (`.cruxible/floor/`, which
`cruxible floor export` and `cruxible_floor_export` write)
and `get` the ref a hit names; an agent without a shell searches values with
`cruxible_query` and `contains`.

`full` advertises the complete catalog below, including the `authoring_*`
intent tools, `cruxible_query_spec`, `cruxible_since`,
curation, coverage, the floor, sources, blocks, kits, Procedures, Lines, and the
split approval pair
`cruxible_proposal_approve_prepare` and `cruxible_proposal_approve_submit` for
a signer outside the MCP process. Curation changes
discoverability only; permission tiers still gate every call. There is no
separate version tool: `cruxible_whoami` and `cruxible_server_info`
both report the MCP adapter's package version and the daemon's (`GET /version`).

## The daemon

Every tool runs on a Cruxible daemon; the MCP server is a client of it and never
serves state in its own process. It picks the daemon in this order:

1. `CRUXIBLE_SERVER_SOCKET` or `CRUXIBLE_SERVER_URL` in its own environment;
2. the transport the MCP workspace's `.cruxible/coverage.json` binds together
   with an instance;
3. the default socket, `~/.cruxible/run/daemon.sock` (under `CRUXIBLE_STATE_ROOT`
   when that is set), when a daemon answers there;
4. otherwise it starts one under that state root, holding
   `<state root>/run/autostart.lock` so two MCP servers never start two: a live
   daemon already serving the state root is reused, an installed user service
   (`cruxible server install-service`) is started, and with neither it runs
   `cruxible server start --socket <default socket>` detached, writing its
   output to `<state root>/run/daemon.out`. The daemon outlives the MCP server.

A transport from 1 or 2 that does not answer is refused by name; the server never
starts a daemon in its place, whichever socket a workspace binding names (the
default one included). When no daemon can be started, the call fails with
`cruxible.mcp.daemon_unavailable` naming the failed step and the repair: start
one with `cruxible server start`, install the service, or set a transport. A
started daemon inherits an allowlist of the MCP server's environment: process
basics (`PATH`, `HOME`, user, shell, temporary directories, locale, `TZ`, the XDG
directories, `VIRTUAL_ENV`, `PYTHONPATH`), proxy and certificate settings, uv's
cache and index URLs, `GIT_SSH_COMMAND`, and daemon configuration. Nothing else
reaches it: not the server's transport, tier, instance or principal, and no
credential (Cruxible tokens and keys, API keys, cloud or forge tokens).

## Instances and the adapter environment

Every tool that acts on one instance takes an optional `instance_id`. Omitted,
it defaults to `CRUXIBLE_INSTANCE_ID` in the MCP server's own environment (set it
in the `env` block of the MCP client config), and then to the instance the MCP
workspace's `.cruxible/coverage.json` binds, provided the binding names the
server's daemon; a binding on another daemon than the environment's is refused.
The server never reads remembered CLI context. With none of these, the call fails and
names `CRUXIBLE_INSTANCE_ID`. `cruxible_whoami` returns the instance it resolved together
with the caller's identity there.

`CRUXIBLE_PRINCIPAL_ID` in the same `env` block names the principal the MCP
server acts as; it is sent with every request, and the daemon checks it is a
registered, active principal before any write it attributes to it (reads stay
open). With daemon auth off
it is a claim of identity, not authentication (`authenticated: false` in
`whoami`): every process of the same OS user is equally trusted. `whoami` also
reports `can_author` and, when false, the `authoring_refusal` (code, detail and
repair) that authoring would return.

`CRUXIBLE_MCP_WORKSPACE_ROOT` selects the client-owned workspace for tools that
read or write local files. The stdio MCP process is the client-side adapter; the
workspace defaults to its working directory.
All tool paths are normalized relative paths confined under that root; the
daemon receives bytes and typed observations, never a client filesystem path.
Floor operations always target the containing Git worktree's canonical
`.cruxible/floor`. With no explicit workspace root, the adapter discovers that
worktree from its working directory. An explicit `CRUXIBLE_MCP_WORKSPACE_ROOT`
must equal the worktree root for floor export and status; a nested explicit
root is refused rather than allowing a write above its configured filesystem
boundary. `cruxible_proposal_activate` is a daemon act and writes nothing
locally: the daemon's floor-refresh trigger delivers the floor to a workspace it
serves, and `cruxible_floor_export mode=write` pulls it elsewhere.

`CRUXIBLE_MCP_KEY_DIR` names the directory of local approval keys that
`cruxible_proposal_approve` signs with: an absolute path outside the workspace
holding `<signer_id>.ed25519`, the layout `cruxible principal add
--key-dir` and `cruxible init --key-dir` write. Set it in the MCP
server's environment; no tool argument can name a key path. The tool signs as
`signer_id`, or as the directory's only key when `signer_id` is omitted, and
passing the reviewed `candidate_digest` makes it refuse a candidate that
changed since review. Unset, the tool refuses and says how to configure it.
Only the public attestation leaves the process; key bytes never appear in a
result or a log line.

## Which daemon operations each tool publishes

`tests/goldens/playbill/served-surface-dp0b-v1.json` is the machine-readable
inventory of the whole served surface. Its `surface.mcp_tools` rows carry
`client_operations`: the daemon client operations each tool reaches, per tool.
MCP reaches state only through the daemon, so each operation is an HTTP route,
and that route's row in `surface.http_routes` names the facade verbs it reaches.
A deployment that decides what may be reached over MCP reads this join, or
enforces at the daemon's routes, rather than inferring it from the
`cruxible_<verb>` / `handle_<verb>` spelling, which nothing guarantees.

The list is a reachability closure, not a read of the handler's own body: it
covers the operations the handler calls itself, those of a sibling handler it
delegates to, and those of the shared client-side code it hands the client to
(block repin, the next-workspace observation, source compilation). An empty list
means the tool reaches no daemon operation at all -- one tool is in that position
today, `cruxible_authoring_example`, and it is `READ_ONLY`. A mutating tool may
not publish an empty list without a declared exception naming its reason
(`tests/test_guardrails/test_playbill_v1_served_surface.py`), because an
overlay reading `[]` as "reaches nothing" would fail open.

Every row is covered by the snapshot's `succession.surface_digest`, so a tool
that starts reaching one more operation moves the pin.

## Runtime

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_server_info` | Return the adapter and daemon versions with daemon metadata; an instance-scoped credential gets its own instance's host and identity instead of a refusal | `READ_ONLY` |

## Host and providers

Setting up a host (`cruxible init`, or `cruxible host create` to allocate one
without becoming its owner), releasing its worktree (`cruxible workspace
detach`) and decommissioning an instance (`cruxible instance decommission`) are
operator acts with no MCP tool.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_provider_catalog` | List provider packages from the configured daemon repository, each with its version and the provider interface IDs it implements | `READ_ONLY` |
| `cruxible_provider_install` | Install exact package bytes and propose its definitions, without execution grants | `ADMIN` |

## Kits

A kit is a release of definitions (ClaimTypes, CaptureContracts, QueryDefinitions)
that installs
as a diff against the consumer: one proposed change set that adds, replaces and
retires definitions. Adding or removing a kit only proposes; activation and any
approval stay the ordinary steps.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_kit_build` | Export the definitions under owned identity prefixes as one kit release | `READ_ONLY` |
| `cruxible_kit_status` | List installed kits and the kit paths edited since install | `READ_ONLY` |
| `cruxible_kit_add` | Propose installing or upgrading a kit as one change set | `GOVERNED_WRITE` |
| `cruxible_kit_remove` | Propose retiring every artifact a kit installed | `GOVERNED_WRITE` |
| `cruxible_claim_type_upgrade` | Propose moving older ClaimTypes to v7 (identity evidence rules included), stating their revision evidence | `GOVERNED_WRITE` |

## Documents and proposals

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_body_store` | Store inert body bytes in CAS | `GOVERNED_WRITE` |
| `cruxible_document_propose` | Propose a canonical Document envelope | `GOVERNED_WRITE` |
| `cruxible_proposal_review` | Render review material | `READ_ONLY` |
| `cruxible_proposal_approve_prepare` | Return the exact approval challenge | `READ_ONLY` |
| `cruxible_proposal_approve_submit` | Submit a public signed attestation | `GRAPH_WRITE` |
| `cruxible_proposal_approve` | Challenge, sign with a local key from `CRUXIBLE_MCP_KEY_DIR`, and submit in one call | `GRAPH_WRITE` |
| `cruxible_proposal_activate` | Activate by compare-and-set; returns the activation receipt (the daemon delivers the floor) | `GRAPH_WRITE` |
| `cruxible_proposal_list` | List one page of open and terminal proposal evidence (`limit`, `cursor`) | `READ_ONLY` |
| `cruxible_proposal_readmit` | Re-admit a stale proposal at the current head | `GOVERNED_WRITE` |
| `cruxible_proposal_withdraw` | Retire an open proposal that will never activate | `GOVERNED_WRITE` |
| `cruxible_whoami` | Name the resolved instance, the credential-derived actor's identity and registration there, and the adapter and daemon versions | `READ_ONLY` |

MCP never accepts a client private key. Signing occurs outside the server and
outside the language server/MCP process.

## Accepted reads

Accepted state is read through `cruxible_orient`,
`cruxible_query` and `cruxible_get` (see
[Queries, orient and get](#queries-orient-and-get)). Documents are listed by
`orient(section="documents")` and read by `get("Document:<name>")`, its
`why`, `history` and `body` details; a body read needs `GOVERNED_WRITE` and
names the whole body's `body_digest`.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_capture_read` | Verify retained Capture evidence and read bounded material; `capture_digest` may be the full digest, a `CAP-<12 hex>` handle or a 12+ hex prefix unique among the Captures the write verbs resolve (cited, or retained and verifying) | `GOVERNED_WRITE` |

## Sources

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_source_context` | Return source alignment context | `READ_ONLY` |
| `cruxible_source_check` | Check a compiled `bundle`, or the sources a workspace `catalog_path` declares, against accepted state | `READ_ONLY` |
| `cruxible_propose_source_bundle` | Propose a frozen compiled bundle | `GOVERNED_WRITE` |
| `cruxible_workspace_source_compile` | Read catalog-declared workspace bytes and derive a source bundle | `READ_ONLY` |

`cruxible_source_check` takes exactly one of `bundle` (for programmatic
clients that compiled one) or `catalog_path`. With a catalog path the adapter
owns local path traversal and digest construction, so an agent supplies catalog
paths and root aliases, not compilation wire.

## Principals

The principal registry is `orient(section="principals")`; one record is
`get("Principal:<id>")`.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_compiler_upgrade` | Propose an exact compiler transition; signed approval and activation use the ordinary proposal workflow. | `ADMIN` |
| `cruxible_principal_propose` | Propose rotation, revocation, or recovery | `ADMIN` |

## Subjects, ClaimTypes, and Claims

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_claim_type_propose` | Propose a governed predicate interface | `GOVERNED_WRITE` |
| `cruxible_claim_type_migrate` | Compose a ClaimType successor with dependent dispositions | `GOVERNED_WRITE` |
| `cruxible_claim_attest` | Sign and append a support, contradict, or unsure observation of the current exact Claim; pass `capture_digests` (and optionally `referent_coordinate`) to attest on new Captures you examined instead of the Claim's own citations | `GOVERNED_WRITE` |
| `cruxible_set` | Put one value in one field of one Subject (`kind/id`), replacing the live value without its Claim ID; a missing Subject of a known kind is added; `evidence` defaults to `because` as self evidence (an exact-content value is its own evidence); accepts in the same call when policy and tier allow it, else answers `awaiting_approval` with the eligible approvers and the approve call; `dry_run` writes nothing; `at` refuses `cruxible.write.slot_changed` if the field moved since; each change carries its `verdict`, and a verdict other than `supported` comes with a warning and its repair | `GOVERNED_WRITE` |
| `cruxible_retire` | End one live Claim, by Claim ID or by Subject and single-value field, with its dependent Claims, in one change set | `GOVERNED_WRITE` |
| `cruxible_write` | Apply `set`, `add` (one more value in a many-valued field) and `retire` changes as one change set, accepted or refused together | `GOVERNED_WRITE` |
| `cruxible_get` | Read one thing by any reference (Claim id or prefix, `kind/id`, predicate, `ClaimType:`/`Document:`/`Procedure:`/`query:`/`CaptureContract:`/`Trigger:`/`Principal:`/`ProviderInterface:`/`SourceAcquisitionPolicy:<name>`, `ApprovalPolicy:instance`, `ProcedureRuntimePolicy:instance`, artifact path, proposal id, or an operational reference: `Line:<name>` or the Line identity digest `next` names, `CAP-<12+ hex>`/`Capture:<digest>`, `ResolutionContract:<name>`, `Mandate:<name>`; their operational parts are read live at the head whatever `at` names, and the answer marks them with `live: {as_of, fields}`); `detail` is `summary` (values-first card with verdict flags; a string value over 500 characters is cut to `{value, truncated: true, length}`, and Subject rows name each value's `claim`), `evidence` (with the whole value), `why` (a Claim's verdict and law evidence, or a Subject's or Document's governance and provenance), `history` (newest first, paged by `limit` and `cursor`), `proof` (the full accepted envelope and facts, with the full `accepted_coordinate`), or `body` with a byte `range` and the whole `body_digest`; other answers carry a compact `coordinate` (12-hex git oid prefix and `generation`), either of which `at` accepts back (a history row carries both; an all-digit `at` of 11 or fewer characters is always a generation); evidence names Captures by `CAP-<12 hex>` handles; a wrong name refuses with the nearest names | `READ_ONLY` |

A proposal is not accepted state. A Claim's verdict is computed at read time
from accepted law evidence, never carried forward from acceptance.

## Authoring intents

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_authoring_example` | Return a model-generated ClaimType/Claim/Procedure input | `READ_ONLY` |
| `cruxible_authoring_get` | Read one authoring intent | `READ_ONLY` |
| `cruxible_authoring_list` | List the caller's in-progress intents | `READ_ONLY` |
| `cruxible_authoring_compile` | Stage a payload as an intent (new, or revising `intent_id`) and run every check | `GOVERNED_WRITE` |
| `cruxible_authoring_bind` | Read an anchored workspace selection, derive commitments, and compile | `GOVERNED_WRITE` |
| `cruxible_authoring_preflight` | Produce a binding certificate and repair frontier | `GOVERNED_WRITE` |
| `cruxible_authoring_rebase` | Rebase a stale intent onto the current accepted coordinate | `GOVERNED_WRITE` |
| `cruxible_authoring_submit` | Compile and submit a `payload` in one call, submit a staged `intent_id`, or both (revise, then submit); idempotent | `GOVERNED_WRITE` |
| `cruxible_authoring_status` | Read the causal path to acceptance | `READ_ONLY` |
| `cruxible_block_repin` | Stamp or refresh one projection block; the adapter computes the stamp and registers the block | `GOVERNED_WRITE` |
| `cruxible_block_sync` | Check each projection block's backings; edits no page | `READ_ONLY` |
| `cruxible_block_detach` | Remove retired blocks' markers from pages, keeping the prose; `dry_run` previews, `at` pins the commit to the pages' bytes | `GOVERNED_WRITE` |
| `cruxible_block_depublish` | Release the declaration that registers one page block | `GOVERNED_WRITE` |

The coordinator mints every identity, digest, base, timestamp, and proposal reference.
It reports approval conditions but never obtains or impersonates an approval.

The flow is `compile` (or `bind`), then `rebase` or `preflight` as needed, then
`submit` and `status`, with `get` and `list` to find work again; a one-shot write
is `cruxible_authoring_submit` with a `payload`. `cruxible_authoring_example`
prints a template for any input kind.

Every payload is one tagless input, and the
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
`cruxible_authoring_example` serves `change-set` and
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
| `cruxible_procedure_run` | Execute a directly runnable Procedure (`cruxible_get` shows `runnable: direct`) -- accepted-state reads, deterministic computation, and `source`/`call` occurrences through exactly pinned Providers -- at an explicit coordinate and time | `READ_ONLY` |
| `cruxible_procedure_measure` | Evaluate due Procedure measurements from real evidence, persist the resolution, and credit one run's exact grain | `GOVERNED_WRITE` |
| `cruxible_procedure_readings` | Inspect measurement standing and retained exact-grain readings (read-only, paginated) | `READ_ONLY` |
| `cruxible_line_enable` | Enable a Line forward-only: its Triggers do nothing until then. The daemon admits what they match under the caller's credential, rechecked before each run, pinned to the current Line and Trigger versions. Needs governed write even for an observe-only Line; a proposing or settling Line needs a covering mandate. Repeating it unchanged returns `outcome: already_enabled`. Read it with `cruxible_get(ref="Line:NAME")`. | `GOVERNED_WRITE` |
| `cruxible_line_disable` | Stop a Line admitting work on its own; admitted runs keep going. A stopped enablement returns `outcome: already_disabled`. | `GOVERNED_WRITE` |
| `cruxible_line_run` | Run a Line once now as a manual occurrence under its own inputs, budgets, authority and mandate; it never consumes a Trigger. `event` is the event input for a Line whose Procedure takes one; an event its Triggers already admitted needs `repeat`. A Line that can propose or settle needs a mandate, an observe-only one none | `READ_ONLY` |
| `cruxible_line_evaluate` | Evaluate a historical range (`since`, `until`) into pending work; never executes. `dry_run` only reports what the range makes eligible (a read, no range needed); enqueueing needs governed write. | `READ_ONLY` |
| `cruxible_line_dispatch` | Admit retained pending occurrences under the current caller's authority, up to `limit` (default 100). | `READ_ONLY` |

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
| `cruxible_prediction_list` | Find governed tests of a Claim, by `claim_id` | `READ_ONLY` |
| `cruxible_prediction_propose` | Propose a governed resolution contract whose hypothesis is a Claim ID | `GOVERNED_WRITE` |
| `cruxible_prediction_settle` | Settle one prediction (`prediction_id`) from the Claim ID of an accepted observation (`observation`) | `GOVERNED_WRITE` |

Every Claim version these tools need can be a plain Claim ID (`CLM-...` or
`Claim:CLM-...`); the daemon resolves its digests and accepting coordinate. The
exact reference, and settle's full `request` (exact contract reference, anchor
event, or terminal evidence), remain as the advanced form.

Prediction settlement records the activation and resolution in operational
exhaust; it does not create or mutate Claims, and it does not create a second
authority plane beside accepted state.

## Queries, orient and get

`cruxible_query` takes exactly one mode: compact or a query `name`
(a full spec runs through `cruxible_query_spec`, in the `full`
profile, so the default tool stays small). Compact mode names a Subject
`kind` (or `ClaimType` / `Procedure` for definitions, or `Trigger` / `Line`
to list Triggers by name, schedule, target and lifecycle and Lines by
enabled) and/or free text
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
`contested`, `contradicted`, `uncovered`, `unsure_hold`); without `select` a kind
shows up to 12 predicates and names the rest in `notes`. Cells show each slot's
answer as `get` shows it (its accepted and conflicted Claims, or every live
Claim when resolution accepted none); `status` adds Claims resolution set aside
(`overturned`, `refused`) or withdrew (`retired`), and `claims: true` answers
each cell's Claims under `rows[].claims[<column>]` with `claim`, `value`,
`verdict`, `status` and `role`, so a slot's winner reads apart from its losers.
`subject`, `subject_id`, `flags` and `claims` are row metadata; a column with one
of those names is served as `value.<name>`. ClaimType rows name the
CaptureContracts their evidence rules admit, never digests. `receipt` records the
mode, the definition digest, the coordinate and the evaluation time (default
now). A named query takes `budgets` up to its definition's maximum, and
`receipt: "full"` adds `receipt.replay`: the engine result (the Claims each row
read, traversal paths, bound parameters, verdict) and its execution receipt.

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_query` | Answer any question over accepted state in one call: compact (`kind` and/or `contains`, with `where` filters shaped by operator, `select`, one-hop `follow`, `order_by`, `status`, `claims`) or a query `name` with `params` (`budgets`, `receipt`); rows of values with `flags`, paged by `limit` and `cursor` | `READ_ONLY` |
| `cruxible_query_spec` | Run one full `QueryDefinitionSpec` inline (`spec`, `limit`, `cursor`, `at`, `evaluation_time`) with the same evaluation, rows, flags and paging as `cruxible_query`; `full` profile only | `READ_ONLY` |
| `cruxible_orient` | Map accepted state in one call: each Subject kind with its live count and predicates (type, cardinality, enum members, accepted evidence as CaptureContract names), artifact counts, named queries, `you` (can this caller author, and why not), `attention` from the `next` queue (with `enablements`: the instance's Line enablements by state and the stalled or stopped Lines by name, no daemon scope needed), and `next` suggestions written as MCP tool calls; `kind` reads one kind in full with sample Subject IDs, `section` pages `documents`, `procedures`, `claim_types`, `queries`, `interfaces` (each with its interface digest, operation contract and implementing Providers' implementation digests), `principals`, `policies` (every live standalone or embedded governed policy), or an operational family -- `runs` (Procedure runs, newest admission first, paged by an immutable key) or `running` (only the runs still running; read one with `cruxible_get(ref="ProcedureRun:RUN-...")` for its live progress), `lines`, `captures`, `capture_contracts`, `predictions`, `mandates` -- which the map only counts under `artifacts` (`limit`, `cursor`); when the MCP workspace holds this instance's floor, `floor: {at, generations_behind}` says how far behind the head it is | `READ_ONLY` |
| `cruxible_since` | Read signed accepted ChangeSet members after a generation | `READ_ONLY` |
| `cruxible_next` | Rank outstanding repair work, each row with its exact next operation; observes the MCP workspace's floor and declared sources as `cruxible next` does | `READ_ONLY` |
| `cruxible_audit` | Rank visible Claim verification work and record completed coverage | `READ_ONLY` |
| `cruxible_curation_list` | List one page of curation patterns (`limit`, `cursor`) and ingest an explicit declared-block observation | `READ_ONLY` |
| `cruxible_curation_overrule` | Close an inapplicable detector-version item with attribution | `GOVERNED_WRITE` |
| `cruxible_curation_accept_fixed` | Link an item to an exact related accepted ChangeSet | `GOVERNED_WRITE` |
| `cruxible_curation_suppress` | Hide open work by item, pattern, or instance without resolving it | `GOVERNED_WRITE` |
| `cruxible_floor_export` | `mode=bytes` returns the greppable floor as base64 bytes; `mode=write` verifies and exactly replaces `.cruxible/floor` under the MCP workspace (status `unchanged` when it already holds this floor) and records the workspace `floor_output` profile, `include` too, exactly as `cruxible floor export` does, so the daemon's delivery exports the same parts; `mode=status` reports whether that floor is current, stale, or absent. The floor is `current/<kind>/<id>.yaml` (values first, one header line naming the ref and coordinate), `current/<kind>/INDEX`, readable `documents/` and `provenance/`; digests stay in `provenance/` and the manifest. `include=["discovery"]` adds the discovery cards and `coverage-manifest.json`. Grep it, then `cruxible_get` the ref for live verdicts; an agent without a shell uses `cruxible_query` with `contains` | `READ_ONLY` |
| `cruxible_coverage` | Resolve working sources against accepted state, from `observations` you built or from workspace `bindings` plus a file selection (`files`, `ranges`, inline `grep_results` text, or `whole_working_set`) | `READ_ONLY` |

`cruxible_next` renders each repair's `command` as the MCP tool call
that performs it (for example `cruxible_prediction_settle(prediction_id="RSC-...")`,
adding the observation's Claim ID), or none when its operands are local files.
A row or nested finding whose repair the session cannot perform -- its profile
does not advertise the tool that performs it, or its tier is too low -- stays in
the queue with `repair: null` and `repair_requires: {tool, tier, because,
profile?}` naming what running it needs, so `orient` attention and the queue
count it for every caller. A status facet keeps its
state either way, but drops a repair the session cannot perform and says
`repair_hidden: true` with the same `repair_requires`. The `default` profile
advertises neither `cruxible_prediction_settle` nor the Line tools, for example:
a stopped Line enablement still shows as `consumer_stalled`, its repair withheld with
`because: ["profile"]`. A session that cannot author on the instance at all (an
unbound credential, or a principal that is not configured, registered or
active) has every writing repair withheld with `because` including
`"authoring"` and `authoring_refusal` carrying the same code, detail and repair
`whoami` reports.

Lists that can outgrow one answer are paged. `query`, `orient` sections,
`proposal_list` and `curation_list` take `limit` and `cursor`; a cut page
carries top-level `truncated: true` and a `next_cursor` to pass back as `cursor`.
A cursor whose listing changed since its first page is refused as
`cruxible.list.cursor_stale`; list again without it.

Query execution is a read: a named query's `receipt: "full"` returns its
`playbill-query-execution-receipt-v1`. Qualifying direct reads, named query
runs, coverage delivery, and Procedure dependency resolution also append
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
Cruxible principal signature is an additional governance condition, not a
replacement for transport authorization.

Workspace source tools take `root_aliases` as a list of `{alias, path}` records.
Coverage takes `bindings` as a list of `{path, source_id}` records. Duplicate aliases
or paths are refused. Named-query `params` and Procedure `input` use the vocabulary
and input contracts declared in accepted state, which the daemon validates. A `params`
value may be `null`, which binds an optional parameter explicitly; omitting it takes the
parameter's default.
