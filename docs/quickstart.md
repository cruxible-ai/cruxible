# Quickstart

Get from install to a governed kit-backed state model in a few minutes.

The recommended `0.2` shape is a local Cruxible daemon, launched with
`cruxible server start`. The daemon
owns state; the CLI, MCP server, client SDK, GUI, and agent harness talk to it
through Cruxible surfaces.

This guide assumes a **fresh daemon** with no instance yet. If you already
have a daemon from the README's Get Started, it holds that instance — leave
it running and start a second daemon alongside it, on its own port and
state directory:

```bash
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/server-quickstart" \
  cruxible server start --port 8101
```

Then use `http://127.0.0.1:8101` wherever the commands below say
`http://127.0.0.1:8100`. See
[Runtime Auth And Agent Roles](runtime-auth-and-agent-roles.md#one-daemon-one-instance-02)
for the model behind this.

## Prerequisites

- Python 3.11 or later
- [git](https://git-scm.com/) and [uv](https://docs.astral.sh/uv/)
- An MCP-capable AI agent if you want agent orchestration

## Install And Start The Daemon

```bash
pip install cruxible
```

The daemon ships in the default install, and the built-in kit aliases
(`init --kit agent-operation`, `--kit supply-chain-blast-radius`, ...)
resolve from digest-pinned release bundles — no checkout needed. To hack on
kits instead, clone the repo and run from the checkout
(`uv sync --all-extras`); a source tree's `kits/` always wins over the
published bundles.

Start the local daemon:

```bash
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/server" cruxible server start
```

Without auth, this is sandbox mode: writes are attributed to a built-in
`operator` identity, visible as such in provenance. Turn auth on (below)
when agents join and identity should be credential-backed.

The daemon runs in the foreground — run the commands below from another activated
shell, or start it in the background.

Use a durable state directory such as `~/.cruxible/server` or
`/var/lib/cruxible`. Do not put long-lived daemon state under `/tmp`,
`/var/tmp`, or macOS private temp directories; Cruxible warns at startup when
the configured server state path resolves under a known volatile temp location.

The daemon binds locally by default. For a simple local hardening layer, start
it with:

```bash
CRUXIBLE_SERVER_AUTH=true \
CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=change-me-once \
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/server" cruxible server start
```

Claim the bootstrap secret with `cruxible credential claim-bootstrap` to create
an admin runtime credential, then use that runtime credential as
`CRUXIBLE_SERVER_BEARER_TOKEN` for authenticated CLI or client calls. See
[Runtime Auth And Agent Roles](runtime-auth-and-agent-roles.md) for the full
bootstrap and agent-role flow.

Use `cruxible-client` in a separate agent environment when the agent should not
import the runtime directly:

```bash
pip install cruxible-client
```

## First Instance: The Supply-Chain Demo

Create an instance from two kits — the agent-operation base and the
supply-chain demo domain — and connect the CLI context so commands stop
needing per-call flags:

```bash
cruxible --server-url http://127.0.0.1:8100 init --kit agent-operation --kit supply-chain-blast-radius
cruxible context connect --server-url http://127.0.0.1:8100 --instance-id <instance-id>
```

Build the seeded world. Canonical workflows are preview-first: `run` executes
against a clone and returns an apply digest; `apply` re-verifies it against
the current config, lockfile, and head snapshot before committing:

```bash
cruxible run --workflow build_seed_state --save-preview seed.json
cruxible apply --preview-file seed.json
cruxible run --workflow ingest_incidents --save-preview incidents.json
cruxible apply --preview-file incidents.json
```

Incident-to-supplier impact is a governed relationship: nothing may write it
directly, not even a workflow. The proposal workflow bridges its output into
a candidate group, each member carrying the signals and evidence that
matched it:

```bash
cruxible propose --workflow propose_incident_impacts_supplier
cruxible group list --status pending_review
cruxible group get --group <group-id>
```

Review the thesis, member signals, and pending version, then resolve. The
`--expected-pending-version` flag pins your decision to the exact pending
state you reviewed — a group that changed underneath you refuses to resolve:

```bash
cruxible group resolve --group <group-id> --action approve \
  --rationale "Confirmed against supplier geography" \
  --expected-pending-version <pending-version>
```

Ask the questions those edges now answer:

```bash
cruxible query run open_incident_impacts --json
cruxible query run incident_impacted_suppliers --param incident_id=INC-TW-RAIL-2026-07 --json
```

Every query returns a receipt ID: the deterministic path from parameters to
traversed edges to rows. Render it with `cruxible explain --receipt
<receipt-id>`, or in MCP with `cruxible_receipt(instance_id, "<receipt-id>")`.

The approved supplier impacts unlock the next cascade:
`cruxible propose --workflow propose_incident_impacts_component` fills the
queue with component-level candidates, and once judged,
`single_source_components_for_incident` names exposed components with no
alternative supplier.

To consume a published reference state instead of a seeded demo (the KEV
vulnerability brain), see the [KEV Guide](kev-guide.md). To publish states
of your own, see [Publishing And Subscribing To States](publishing-states.md).

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

- A **standalone kit** creates a state model by itself.
- An **overlay kit** extends a published reference state.
- Provider refs use `kit://...::callable`.
- Deterministic state loading should be workflow-based: parse source artifacts,
  shape/filter/join/dedupe rows, make graph objects, preview, then apply.
- Inference, matching, classification, and reviewable judgment should go
  through proposal workflows and candidate groups.

For hands-on kit creation, see [Kit Walkthroughs](kit-walkthroughs.md). For the
manifest and distribution rules, see [Kit Authoring And Distribution](kit-authoring.md).

## Next Steps

- [Concepts](concepts.md) - Architecture and vocabulary
- [KEV Guide](kev-guide.md) - Subscribe to the vulnerability reference and work the triage queue
- [Publishing And Subscribing To States](publishing-states.md) - Build, publish, and track reference states
- [Guide For AI Agents](for-ai-agents.md) - Agent operating recipes
- [Kit Walkthroughs](kit-walkthroughs.md) - Build and customize kits
- [Local State And Backups](local-state-and-backups.md) - SQLite and droplet operations
- [Config Reference](config-reference.md) - YAML schema
- [MCP Tools Reference](mcp-tools.md) - MCP surface
- [CLI Reference](cli-reference.md) - Terminal commands
