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

## Permission tiers

Read operations require read_only. CAS/proposal operations require
governed_write. Approval submission and activation require graph_write. Host
allocation, initialization, and principal changes require admin.

The daemon capability ceiling and bearer credential tier both apply. A
Playbill principal signature is an additional governance condition, not a
replacement for transport authorization.
