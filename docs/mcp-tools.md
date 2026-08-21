# MCP tool reference

The MCP surface is Playbill-only. All tools delegate to the same service core as
HTTP and CLI.

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
| `cruxible_playbill_activate` | Verify and activate by compare-and-set | `GRAPH_WRITE` |

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

Local path traversal and compilation remain client-side CLI/library concerns.
MCP receives canonical path-free bundles.

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
| `cruxible_playbill_authoring_get` | Read one authoring intent | `READ_ONLY` |
| `cruxible_playbill_authoring_resume` | Return an intent's durable continuation | `READ_ONLY` |
| `cruxible_playbill_authoring_list_pending` | List the caller's pending intents | `READ_ONLY` |
| `cruxible_playbill_authoring_compile` | Create or update an intent and preflight it | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_preflight` | Produce a binding certificate and repair frontier | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_submit` | Idempotently submit a passing intent | `GOVERNED_WRITE` |
| `cruxible_playbill_authoring_status` | Read the causal path to acceptance | `READ_ONLY` |

The coordinator mints every identity, digest, base, timestamp, and proposal reference.
It reports approval conditions but never obtains or impersonates an approval.

## Queries, discovery, and the floor

| Tool | Purpose | Permission |
|---|---|---|
| `cruxible_playbill_propose_query_definition` | Propose a named entrypoint | `GOVERNED_WRITE` |
| `cruxible_playbill_list_query_definitions` | List accepted entrypoints | `READ_ONLY` |
| `cruxible_playbill_get_query_definition` | Read one entrypoint's contract | `READ_ONLY` |
| `cruxible_playbill_run_query` | Execute an entrypoint with a replay receipt | `READ_ONLY` |
| `cruxible_playbill_discover` | Find interfaces and Subjects by name | `READ_ONLY` |
| `cruxible_playbill_expand` | Expand one address into a context capsule | `READ_ONLY` |
| `cruxible_playbill_export_floor` | Export the greppable floor as base64 bytes | `READ_ONLY` |
| `cruxible_playbill_resolve_coverage` | Resolve observed working sources against accepted state | `READ_ONLY` |

Query execution is a read: it returns the result together with its
`playbill-query-execution-receipt-v1`, and it writes nothing. The floor export
returns bytes keyed by floor path; materializing a directory is a client act.
Coverage resolution takes observations -- a declared logical-source binding and
the bytes the caller read -- rather than paths, so the daemon reads no client
filesystem. It appends no receipt: it changes no accepted state, and the
evidence-index, overlay, and manifest digests it returns reproduce the answer.

## Permission tiers

Read operations require read_only. CAS/proposal operations require
governed_write. Approval submission and activation require graph_write. Host
allocation, initialization, and principal changes require admin.

The daemon capability ceiling and bearer credential tier both apply. A
Playbill principal signature is an additional governance condition, not a
replacement for transport authorization.
