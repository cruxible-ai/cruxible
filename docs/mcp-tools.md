# MCP tool reference

The MCP surface is Playbill-only. All tools delegate to the same service core as
HTTP and CLI.

The unset/default MCP profile advertises the writer path: authoring, discovery,
search/list/orient, expansion, source context, coverage, floor export, proposal
approval/activation, and runtime identity/version reads. Set
`CRUXIBLE_MCP_PROFILE=expert` (aliases: `full`, `all`) to advertise the complete
catalog below. Curation changes discoverability only; permission tiers still gate
every call, and hidden expert tools remain available through the API.

`CRUXIBLE_MCP_WORKSPACE_ROOT` selects the client-owned workspace for tools that
read or write local files. The stdio MCP process is the client-side adapter; the
workspace defaults to its working directory.
All tool paths are normalized relative paths confined under that root; the
daemon receives bytes and typed observations, never a client filesystem path.

## Runtime

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_version` | Return package/runtime version information | `READ_ONLY` |
| `cruxible_server_info` | Return daemon transport and state metadata | `READ_ONLY` |

## Host and initialization

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_host_create` | Allocate an empty daemon-owned host | `ADMIN` |
| `cruxible_playbill_init` | Bootstrap a host with public principal records | `ADMIN` |

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
| `cruxible_playbill_activate` | Activate by compare-and-set and refresh any configured workspace floor | `GRAPH_WRITE` |
| `cruxible_playbill_proposal_list` | List open and terminal proposal evidence | `READ_ONLY` |
| `cruxible_playbill_proposal_readmit` | Re-admit a stale proposal at the current head | `GOVERNED_WRITE` |
| `cruxible_playbill_whoami` | Explain credential-derived actor identity and registration | `READ_ONLY` |

MCP never accepts a client private key. Signing occurs outside the server and
outside the language server/MCP process.

## Accepted reads

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_list_documents` | List accepted Documents and coordinate | `READ_ONLY` |
| `cruxible_playbill_get_document` | Read an accepted Document envelope | `READ_ONLY` |
| `cruxible_playbill_dereference` | Read permission-gated body bytes | `GOVERNED_WRITE` |
| `cruxible_playbill_history` | Read accepted history | `READ_ONLY` |
| `cruxible_playbill_explain` | Explain governance, provenance, coverage, and history | `READ_ONLY` |

## Sources

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_source_context` | Return source alignment context | `READ_ONLY` |
| `cruxible_playbill_check_source_bundle` | Validate a compiled bundle | `READ_ONLY` |
| `cruxible_playbill_propose_source_bundle` | Propose a frozen compiled bundle | `GOVERNED_WRITE` |
| `cruxible_playbill_workspace_source_compile` | Read catalog-declared workspace bytes and derive a source bundle | `READ_ONLY` |
| `cruxible_playbill_workspace_source_check` | Compile workspace sources and check accepted alignment | `READ_ONLY` |

The raw bundle tools remain for programmatic clients. Workspace tools own local
path traversal and digest construction so an agent supplies catalog paths and
root aliases, not compilation wire.

## Principals

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_list_principals` | List accepted public principals | `READ_ONLY` |
| `cruxible_playbill_propose_principal_change` | Propose rotation, revocation, or recovery | `ADMIN` |

## Subjects, ClaimTypes, and Claims

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_propose_subject` | Propose an identity-only Subject | `GOVERNED_WRITE` |
| `cruxible_playbill_list_subjects` | List accepted Subjects and coordinate | `READ_ONLY` |
| `cruxible_playbill_get_subject` | Read one accepted Subject | `READ_ONLY` |
| `cruxible_playbill_subject_history` | Read one Subject's accepted lineage | `READ_ONLY` |
| `cruxible_playbill_propose_claim_type` | Propose a governed predicate interface | `GOVERNED_WRITE` |
| `cruxible_playbill_list_claim_types` | List the accepted predicate vocabulary | `READ_ONLY` |
| `cruxible_playbill_get_claim_type` | Read one accepted ClaimType | `READ_ONLY` |
| `cruxible_playbill_claim_type_migrate` | Compose a ClaimType successor with dependent dispositions | `GOVERNED_WRITE` |
| `cruxible_playbill_propose_claim` | Propose a direct Claim and its Capture | `GOVERNED_WRITE` |
| `cruxible_playbill_propose_claims` | Propose several Claims as one change set | `GOVERNED_WRITE` |
| `cruxible_playbill_list_claims` | List accepted Claims by Subject or predicate | `READ_ONLY` |
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
| `cruxible_playbill_authoring_submit` | Idempotently submit a passing intent | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_status` | Read the causal path to acceptance | `READ_ONLY` |
| `cruxible_playbill_authoring_confirm_insertion` | Confirm a client-applied publication copy | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_abandon_insertion` | Abandon a pending publication copy | `GOVERNED_WRITE` |

The coordinator mints every identity, digest, base, timestamp, and proposal reference.
It reports approval conditions but never obtains or impersonates an approval.

## Procedures

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_procedure_readiness` | Report exact binding requirements or query-only readiness | `READ_ONLY` |
| `cruxible_playbill_procedure_bind` | Attach accepted input-plane bindings through a same-identity successor | `GOVERNED_WRITE` |
| `cruxible_playbill_procedure_run` | Execute a ready query-only Procedure at an explicit coordinate and time | `READ_ONLY` |
| `cruxible_playbill_procedure_run_status` | Read one finalized Procedure run and its receipt | `READ_ONLY` |

Read-tier Procedure runs append receipted journal records, following the same
precedent as QueryDefinition runs. They never alter accepted state or grant
themselves a governed track record; promotion remains a separate governed act.

## Queries, discovery, and the floor

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_propose_query_definition` | Propose a named entrypoint | `GOVERNED_WRITE` |
| `cruxible_playbill_list_query_definitions` | List accepted entrypoints | `READ_ONLY` |
| `cruxible_playbill_get_query_definition` | Read one entrypoint's contract | `READ_ONLY` |
| `cruxible_playbill_run_query` | Execute an entrypoint with a replay receipt | `READ_ONLY` |
| `cruxible_playbill_discover` | Find interfaces and Subjects by name | `READ_ONLY` |
| `cruxible_playbill_search` | Search, list, or orient over accepted state | `READ_ONLY` |
| `cruxible_playbill_since` | Read signed accepted ChangeSet members after a generation | `READ_ONLY` |
| `cruxible_playbill_audit` | Rank visible Claim verification work and record completed coverage | `READ_ONLY` |
| `cruxible_playbill_curation_list` | List curation patterns and ingest an explicit declared-block observation | `READ_ONLY` |
| `cruxible_playbill_curation_overrule` | Close an inapplicable detector-version item with attribution | `GOVERNED_WRITE` |
| `cruxible_playbill_curation_accept_fixed` | Link an item to an exact related accepted ChangeSet | `GOVERNED_WRITE` |
| `cruxible_playbill_curation_suppress` | Hide open work by item, pattern, or instance without resolving it | `GOVERNED_WRITE` |
| `cruxible_playbill_expand` | Expand one address into a context capsule | `READ_ONLY` |
| `cruxible_playbill_export_floor` | Export the greppable floor as base64 bytes | `READ_ONLY` |
| `cruxible_playbill_resolve_coverage` | Resolve observed working sources against accepted state | `READ_ONLY` |
| `cruxible_playbill_workspace_floor_export` | Verify and exactly replace a floor directory under the MCP workspace | `READ_ONLY` |
| `cruxible_playbill_workspace_floor_status` | Report whether the installed workspace floor is current, stale, or absent | `READ_ONLY` |
| `cruxible_playbill_workspace_coverage_resolve` | Derive observations from selected workspace files and resolve coverage | `READ_ONLY` |
| `cruxible_playbill_workspace_coverage_status` | Resolve coverage for the full declared workspace binding set | `READ_ONLY` |

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
| `cruxible_playbill_seed_plan` | Deterministically plan a local seed bundle without contacting an instance | `READ_ONLY` |
| `cruxible_playbill_seed_apply` | Submit exactly one selected seed group | `GOVERNED_WRITE` |

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
