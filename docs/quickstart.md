# Quickstart

Get from install to first query in 5 minutes.

## Prerequisites

- Python 3.11 or later
- An MCP-capable AI agent (Claude Code, Cursor, Codex)

## Install

### Local Daemon Runtime

```bash
pip install "cruxible-core[server,mcp]"
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/server" cruxible-server
```

> Or use `uv tool install "cruxible-core[server,mcp]"` if you prefer [uv](https://docs.astral.sh/uv/).

For a simple local hardening layer, add:

```bash
CRUXIBLE_SERVER_AUTH=true
CRUXIBLE_SERVER_TOKEN=change-me
```

### Client-Only Agent Environment

```bash
pip install cruxible-client
```

Use `cruxible-client` when the agent talks to a separate Cruxible daemon over HTTP and does not need local runtime access.

If permission modes matter, do not install `cruxible-core` in that agent environment.

If you need a real runtime boundary rather than advisory local permissions, see [Isolated Deployment](isolated-deployment.md).

## Point the CLI at the Daemon

Initialize a daemon-owned instance from your local config:

```bash
cruxible --server-url http://127.0.0.1:8100 init --root-dir "$(pwd)" --config config.yaml
```

Then keep using the daemon-backed interface:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> stats
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> query --query <query-name>
```

## MCP Setup

Add the MCP server to your AI agent:

This is the convenience path for local development. Point MCP at the same local daemon the CLI uses.

**Claude Code / Cursor** (project `.mcp.json` or `~/.claude.json` / `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "cruxible": {
      "command": "cruxible-mcp",
      "env": {
        "CRUXIBLE_MODE": "admin",
        "CRUXIBLE_SERVER_URL": "http://127.0.0.1:8100"
      }
    }
  }
}
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.cruxible]
command = "cruxible-mcp"

[mcp_servers.cruxible.env]
CRUXIBLE_MODE = "admin"
CRUXIBLE_SERVER_URL = "http://127.0.0.1:8100"
```

## Try a Demo

```bash
git clone https://github.com/cruxible-ai/cruxible-core
cd cruxible-core/demos/drug-interactions
```

Each demo includes a config, prebuilt graph, and `.mcp.json`. Open your agent in a demo directory.

First, load the instance:

> "You have access to the cruxible MCP, load the cruxible instance"

Then try:

- "Suggest an alternative to simvastatin"
- "Check interactions for warfarin"
- "What's the enzyme impact of fluoxetine?"

Every query produces a receipt. Ask your agent to inspect it with `cruxible_receipt`.

## Build Your Own

### 1. Prepare your data

Make sure all your data files (CSVs, JSONs) are accessible in the repo where your agent can read them.

### 2. Explore the data with your agent

> "Explore the data files in /data. I want to build a graph that can answer: [your goals]. Here are the queries I care about: [list them]"

The agent will profile your data, propose entity types and relationships, and draft named queries. Iterate until you're happy with the domain model.

### 3. Let the agent build

Once you've agreed on entities, relationships, and queries, the agent writes the YAML config and calls Cruxible's MCP tools to validate the schema, initialize the graph instance, and ingest your data.

### 4. Query

Run your named queries and inspect the receipts. Every answer comes with a proof.

### 5. Review and refine

> "Review the graph quality" or "I want to provide feedback on edges"

The agent will run evaluations, surface pending or weakly supported governed
relationships for review, and record your approve/correct/reject decisions.
Accepted domain state compounds in the world model across sessions.

## Next Steps

- [Concepts](concepts.md) — Architecture and primitives
- [Config Reference](config-reference.md) — Every YAML field explained
- [MCP Tools Reference](mcp-tools.md) — All tools with parameters and return types
- [CLI Reference](cli-reference.md) — Terminal commands
- [AI Agent Guide](for-ai-agents.md) — Orchestration workflows and best practices
- [Isolated Deployment](isolated-deployment.md) — Advanced setup for a real runtime boundary
