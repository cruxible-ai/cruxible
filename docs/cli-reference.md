# CLI Reference

This reference is generated from the Playbill-only Click registration surface.
Removed legacy commands are intentionally absent.

## Global Options

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--version` | no | Show the version and exit. |
| `--server-url` | no | Remote Cruxible server base URL. |
| `--server-socket` | no | Local Cruxible server Unix socket path. |
| `--instance-id` | no | Opaque server-mode instance ID. Defaults to remembered CLI context. |
| `--json-compact` | no | Emit all CLI JSON as compact single-line output (also CRUXIBLE_JSON_COMPACT=1). |

## cruxible context

Manage remembered daemon and instance context.

## cruxible context clear

Clear remembered context.

## cruxible context connect

Persist daemon context.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--server-url` | no | Remote Cruxible server base URL. |
| `--server-socket` | no | Local Cruxible server Unix socket path. |
| `--instance-id` | no | Optional opaque server-mode instance ID. |

## cruxible context show

Show remembered CLI context.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--json` | no | Output as JSON. |

## cruxible context use

Set the active instance ID.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `INSTANCE_ID` | yes | Positional argument. |

## cruxible credential

Manage daemon transport credentials.

## cruxible credential claim-bootstrap

Claim the initial ADMIN token.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--secret-file` | no | File containing the runtime bootstrap secret. Defaults to CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET. |

## cruxible credential list

List runtime credentials.

## cruxible credential mint

Mint a runtime credential.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--label` | yes | Human-readable credential label. |
| `--mode` | yes | Credential permission mode. |

## cruxible credential recover-admin

Recover ADMIN from local custody.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--state-dir` | yes | Server state directory containing runtime_credentials.db. Stop the daemon first; the lock check only refuses a writer caught mid-transaction and does not detect an idle running daemon. |
| `--instance-id` | no | Target instance ID when the credentials DB contains multiple instances. |
| `--label` | no | Human-readable label for the recovered ADMIN credential. |
| `--json` | no | Output as JSON. |

## cruxible credential revoke

Revoke a runtime credential.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `CREDENTIAL_ID` | yes | Positional argument. |

## cruxible credential rotate

Rotate a runtime credential.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `CREDENTIAL_ID` | yes | Positional argument. |

## cruxible playbill

Govern state through Playbill's proposal and acceptance ledger.

## cruxible playbill body

Store inert Document body bytes.

## cruxible playbill body store

Store exact bytes without creating authority.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `PATH` | yes | Positional argument. |
| `--json` | no | Output as JSON. |

## cruxible playbill document

Propose and read governed Documents.

## cruxible playbill document body

Dereference verified body bytes.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `IDENTITY` | yes | Positional argument. |
| `--output` | no | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill document get

Read an accepted Document.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `IDENTITY` | yes | Positional argument. |
| `--json` | no | Output as JSON. |

## cruxible playbill document history

Read accepted Document history.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `IDENTITY` | yes | Positional argument. |
| `--json` | no | Output as JSON. |

## cruxible playbill document list

List accepted Documents.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--json` | no | Output as JSON. |

## cruxible playbill document propose

Propose a Document envelope.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--envelope` | yes | Command option. |
| `--name` | yes | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill explain

Explain governance at an accepted coordinate.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `IDENTITY` | yes | Positional argument. |
| `--detail` | no | Command option. |
| `--include-body` | no | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill host

Allocate daemon-owned Playbill hosts.

## cruxible playbill host create

Allocate an empty host for Playbill.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--instance-id` | no | Optional caller-selected opaque ID. |
| `--json` | no | Output as JSON. |

## cruxible playbill init

Bootstrap Playbill state.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--key-dir` | yes | Client custody directory outside the workspace. |
| `--principal-id` | no | Command option. |
| `--recovery-key-dir` | no | Optional offline recovery custody dir. |
| `--recovery-principal-id` | no | Command option. |
| `--profile` | no | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill principal

Govern owner, reviewer, and recovery public keys.

## cruxible playbill principal list

List accepted principal keys.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--json` | no | Output as JSON. |

## cruxible playbill principal recover

Recover a principal key narrowly.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `PRINCIPAL_ID` | yes | Positional argument. |
| `--key-dir` | yes | Command option. |
| `--name` | yes | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill principal revoke

Propose principal revocation.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `PRINCIPAL_ID` | yes | Positional argument. |
| `--name` | yes | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill principal rotate

Self-rotate a principal key.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `PRINCIPAL_ID` | yes | Positional argument. |
| `--key-dir` | yes | Command option. |
| `--name` | yes | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill proposal

Inspect, review, approve, and activate candidates.

## cruxible playbill proposal activate

Settle an approved candidate.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `PROPOSAL_ID` | yes | Positional argument. |
| `--json` | no | Output as JSON. |

## cruxible playbill proposal approve

Sign locally and submit an attestation.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `PROPOSAL_ID` | yes | Positional argument. |
| `--signer-id` | yes | Command option. |
| `--key` | yes | Command option. |
| `--yes` | no | Approve after rendering without an interactive prompt. |
| `--json` | no | Output as JSON. |

## cruxible playbill proposal inspect

Inspect immutable proposal evidence.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `PROPOSAL_ID` | yes | Positional argument. |
| `--json` | no | Output as JSON. |

## cruxible playbill proposal refusal

Inspect typed refusal diagnostics.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `PROPOSAL_ID` | yes | Positional argument. |
| `--json` | no | Output as JSON. |

## cruxible playbill proposal review

Render structured candidate review.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `PROPOSAL_ID` | yes | Positional argument. |
| `--include-body, --redacted` | no | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill sources

Compile declared local files into exact-byte bundles.

## cruxible playbill sources check

Compare local bytes with accepted state.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--catalog` | yes | Command option. |
| `--root-alias` | no | Repeat NAME=PATH. |
| `--local-catalog` | no | Command option. |
| `--root` | yes | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill sources compile

Compile a read-only frozen bundle.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--catalog` | yes | Command option. |
| `--root-alias` | no | Repeat NAME=PATH. |
| `--local-catalog` | no | Command option. |
| `--root` | yes | Command option. |
| `--output` | yes | Command option. |
| `--json` | no | Output as JSON. |

## cruxible playbill sources propose

Propose one exact compiled source.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--bundle` | yes | Command option. |
| `--source` | yes | Command option. |
| `--name` | yes | Command option. |
| `--json` | no | Output as JSON. |

## cruxible server

Launch and inspect the Cruxible daemon.

## cruxible server info

Show daemon metadata.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--json` | no | Output as JSON. |

## cruxible server restart

Re-exec the daemon in place.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--json` | no | Output as JSON. |
| `--no-wait` | no | Return immediately after scheduling the restart, without confirming the daemon is back. |
| `--timeout` | no | Seconds to wait for the restarted daemon to answer again. |

## cruxible server start

Launch the daemon in the foreground.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--host` | no | Bind host (default: CRUXIBLE_HOST or 127.0.0.1). Ignored when --socket is set. |
| `--port` | no | Bind port (default: CRUXIBLE_PORT or 8100). Ignored when --socket is set. |
| `--state-dir` | no | Server-owned state directory (default: CRUXIBLE_SERVER_STATE_DIR or ~/.cruxible/server). |
| `--socket` | no | Listen on this Unix socket path instead of host/port (default: CRUXIBLE_SERVER_SOCKET). |
| `--capability-ceiling` | no | Immutable daemon capability ceiling (default: CRUXIBLE_MODE or admin). Bearer credentials cannot exceed it. |
| `--bootstrap-secret-file` | no | Write an auto-generated runtime bootstrap secret to this file with mode 0600. |

## cruxible server status

Report daemon status.

**Options And Arguments:**

| Name | Required | Description |
|---|---:|---|
| `--json` | no | Output as JSON. |
