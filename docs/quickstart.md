# Quickstart

Get from install to a governed kit-backed world model in a few minutes.

The recommended `0.2` shape is a local `cruxible-server` daemon. The daemon
owns state; the CLI, MCP server, client SDK, GUI, and agent harness talk to it
through Cruxible surfaces.

## Prerequisites

- Python 3.11 or later
- An MCP-capable AI agent if you want agent orchestration

## Install And Start The Daemon

```bash
pip install "cruxible-core[server,mcp]"
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/server" cruxible-server
```

The daemon binds locally by default. For a simple local hardening layer, start
it with:

```bash
CRUXIBLE_SERVER_AUTH=true
CRUXIBLE_SERVER_TOKEN=change-me
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/server" cruxible-server
```

Use `cruxible-client` in a separate agent environment when the agent should not
import the runtime directly:

```bash
pip install cruxible-client
```

## Create A Reference World

Initialize the standalone KEV reference kit. This materializes the kit bundle,
loads its config, and gives you an instance ID.

```bash
cruxible --server-url http://127.0.0.1:8100 init --kit kev-reference
```

Keep the returned `instance_id`; every server-backed command after init uses it.

Then lock and preview the canonical reference refresh:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> lock
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> run \
  --workflow build_public_kev_reference \
  --save-preview kev-reference-preview.json
```

Canonical workflows preview state first. Apply the preview only after checking
the `apply_digest`, changed counts, receipt ID, and trace IDs:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> apply \
  --preview-file kev-reference-preview.json
```

Run a query and inspect its receipt:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> query \
  --query vulnerability_products \
  --param cve_id=CVE-2020-1472
```

Every query returns a receipt ID. In MCP, fetch the full proof with
`cruxible_receipt(instance_id, "<receipt-id>")`. The CLI `explain` command
renders receipts for direct local instances.

## Create A Local Overlay

The KEV triage kit is an overlay kit. It tracks the published KEV reference
world and adds local assets, services, controls, exceptions, remediation,
incidents, findings, and governed proposal workflows.

```bash
cruxible --server-url http://127.0.0.1:8100 world create-overlay \
  --world-ref kev-reference \
  --kit kev-triage \
  --root-dir "$PWD/kev-triage-workspace"
```

`--world-ref kev-reference` resolves through the published world catalog. In a
source checkout before published OCI reference worlds are available, publish the
reference instance to a local `file://` transport and pass `--transport-ref`
instead of `--world-ref`.

The command returns a new overlay `instance_id`. Lock the overlay, preview the
local canonical state refresh, and apply it:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> lock
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> run \
  --workflow build_local_state \
  --save-preview kev-local-preview.json
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> apply \
  --preview-file kev-local-preview.json
```

Run a governed proposal workflow and inspect the pending group:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> propose \
  --workflow propose_asset_products

cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> group list \
  --status pending_review
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> group get \
  --group <group-id>
```

Approve or reject only after reviewing the group thesis, member signals,
receipt, trace IDs, and pending version:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <overlay-instance-id> group resolve \
  --group <group-id> \
  --action approve \
  --expected-pending-version <pending-version> \
  --rationale "Reviewed source evidence and accepted the proposed mappings"
```

## Point An Agent At Cruxible

Bootstrap and canonical apply usually require an admin surface. Day-to-day
agent work should use `governed_write` unless the agent is explicitly acting as
an administrator.

**Claude Code / Cursor**:

```json
{
  "mcpServers": {
    "cruxible": {
      "command": "cruxible-mcp",
      "env": {
        "CRUXIBLE_MODE": "governed_write",
        "CRUXIBLE_SERVER_URL": "http://127.0.0.1:8100"
      }
    }
  }
}
```

**Codex**:

```toml
[mcp_servers.cruxible]
command = "cruxible-mcp"

[mcp_servers.cruxible.env]
CRUXIBLE_MODE = "governed_write"
CRUXIBLE_SERVER_URL = "http://127.0.0.1:8100"
```

If the agent should not have direct state access, keep
`CRUXIBLE_SERVER_STATE_DIR` outside the workspace and install only
`cruxible-client` in the agent environment. See
[Isolated Deployment](isolated-deployment.md) for stronger local separation.

## Build Your Own

Use kits for repeatable work:

- A **standalone kit** creates a world model by itself.
- An **overlay kit** extends a published reference world.
- Provider refs use `kit://...::callable`.
- Deterministic state loading should be workflow-based: parse source artifacts,
  shape/filter/join/dedupe rows, make graph objects, preview, then apply.
- Inference, matching, classification, and reviewable judgment should go
  through proposal workflows and candidate groups.

For hands-on kit creation, see [Kit Walkthroughs](kit-walkthroughs.md). For the
manifest and distribution rules, see [Kit Authoring And Distribution](kit-authoring.md).

## Next Steps

- [Concepts](concepts.md) - Architecture and vocabulary
- [Guide For AI Agents](for-ai-agents.md) - Agent operating recipes
- [Kit Walkthroughs](kit-walkthroughs.md) - Build and customize kits
- [Local State And Backups](local-state-and-backups.md) - SQLite and droplet operations
- [Config Reference](config-reference.md) - YAML schema
- [MCP Tools Reference](mcp-tools.md) - MCP surface
- [CLI Reference](cli-reference.md) - Terminal commands
