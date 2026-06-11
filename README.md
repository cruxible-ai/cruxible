<p align="center">
  <a href="https://cruxible.ai">
    <img src="assets/cruxible_logo.png" alt="Cruxible" width="400">
  </a>
</p>

# Cruxible Core

[![PyPI version](https://img.shields.io/pypi/v/cruxible-core)](https://pypi.org/project/cruxible-core/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Cruxible turns LLM knowledge bases into governed operational state.**

LLMs are getting good at compiling raw documents into markdown wikis, research
repos, and personal knowledge bases. That is useful, but it is still soft
state: later agents can reinterpret it, summaries drift, and teams cannot
safely build repeatable workflows on top of it.

Cruxible takes the parts that need to become shared truth and makes them
typed, queryable, reviewable, and attributable. Facts, relationships,
judgments, review status, and outcomes move out of prompt-local memory and
into governed state that other humans and agents can inspect and rely on.

Define entity graphs, queries, constraints, and governed workflows in YAML.
Run them locally from CLI or MCP, and get receipts proving exactly why each
result or proposal was produced.

```
┌──────────────────────────────────────────────────────────────┐
│  AI Agent (Claude Code, Cursor, Codex, ...)                  │
│  Writes configs, orchestrates workflows                      │
└──────────────────────┬───────────────────────────────────────┘
                       │ calls
┌──────────────────────▼───────────────────────────────────────┐
│  MCP Tools                                                   │
│  init · validate · workflow · query · feedback · evaluate    │
└──────────────────────┬───────────────────────────────────────┘
                       │ executes
┌──────────────────────▼───────────────────────────────────────┐
│  Cruxible Core                                               │
│  Deterministic. No LLM. No opinions. No API keys.            │
│  Config → Graph → Query → Receipt → Feedback                 │
└──────────────────────────────────────────────────────────────┘
```

## Why This Exists

Knowledge bases help agents remember. They do not, by themselves, decide what
has become operationally true.

Humans and agents do not naturally stay coherent with each other.

- Two agents can see similar material and still come away with different facts, procedures, or recommendations.
- A human can think something is settled while the model, under slightly different framing, behaves as if it is missing or softer than it really is.
- Handoffs through chat history are lossy. Important state gets re-summarized, re-interpreted, and sometimes silently changed.

Cruxible gives that important state a home outside the model:

- shared domain facts and relationships
- accepted judgments and review status
- named queries and constraints
- receipts explaining how results and proposals were produced

A simple way to think about it:

> LLMs can reason over knowledge. Cruxible governs what becomes trusted state.

More specifically, Cruxible lets humans and agents share a governed domain
state model: typed entities, relationships, workflows, named queries, review
state, and outcomes.

## How It Works

- **Ontology:** entity types, relationships, and constraints that define the
  domain.
- **Providers:** evidence producers and source adapters that read, match,
  classify, score, or enrich data.
- **Workflows:** repeatable procedures that build reference state, load
  operational state, or propose governed changes.
- **Named queries:** deterministic read surfaces over the state model.
- **Groups and resolutions:** the review boundary where uncertain proposals
  become accepted state.
- **Receipts:** structured proof of how a query result or proposal was
  produced.
- **Feedback and outcomes:** later evidence that improves future behavior.

The core loop is:

1. Build or refresh domain state through workflows.
2. Run workflows that apply or propose changes.
3. Review uncertain proposals.
4. Query and use accepted state from agents, apps, and downstream workflows.
5. Attach feedback and outcomes so later runs improve.

The output is not just an answer in chat. It is governed operational state
that later agents, humans, and software can query and act on.

## Quick Example

**1. Define a domain in YAML:**

```yaml
entity_types:
  Drug:
    properties:
      drug_id: { primary_key: true }
      name:    {}
  Enzyme:
    properties:
      enzyme_id: { primary_key: true }
      name:      {}

relationships:
  - name: same_class
    from: Drug
    to: Drug
  - name: metabolized_by
    from: Drug
    to: Enzyme

named_queries:
  suggest_alternative:
    entry_point: Drug
    returns: Drug
    traversal:
      - relationship: same_class
        direction: both
      - relationship: metabolized_by
        direction: outgoing
```

**2. Load data and run a deterministic query:**

> "Suggest an alternative to simvastatin"

**3. Get a receipt — structured proof of every answer:**

*Raw receipt DAG rendered for readability:*

```
Receipt RCP-17b864830ada

Query: suggest_alternative for simvastatin

Step 1: Entry point lookup
  simvastatin -> found in graph

Step 2: Traverse same_class (both directions)
  Found 6 statins in the same therapeutic class:
  n3  atorvastatin   n4  rosuvastatin   n5  lovastatin
  n6  pravastatin    n7  fluvastatin    n8  pitavastatin

Step 3: Traverse metabolized_by (outgoing) for each alternative
  n9   atorvastatin -> CYP3A4   (CYP450 dataset)
  n10  rosuvastatin -> CYP2C9   (CYP450 dataset, human approved)
  n11  rosuvastatin -> CYP2C19  (CYP450 dataset)
  n12  lovastatin -> CYP2C19    (CYP450 dataset)
  n13  lovastatin -> CYP3A4     (CYP450 dataset)
  n14  pravastatin -> CYP3A4    (CYP450 dataset)
  n15  fluvastatin -> CYP2C9    (CYP450 dataset)
  n16  fluvastatin -> CYP2D6    (CYP450 dataset)
  n17  pitavastatin -> CYP2C9   (CYP450 dataset)

Results: atorvastatin, rosuvastatin, lovastatin, pravastatin, fluvastatin, pitavastatin
Duration: 0.41ms | 2 traversal steps
```

## Get Started

### Choose a Mode

- **Local daemon:** recommended for the `0.2` release candidate. Run `cruxible-server` on `127.0.0.1` or a Unix socket, then point the CLI, GUI, or agent tools at that daemon.
- **Direct local runtime:** still supported as a convenience path for development and single-user exploration, but it is not the primary RC story and it is not a hard isolation boundary.

### Local Daemon Runtime

```bash
pip install "cruxible-core[server,mcp]"
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/server" cruxible-server
```

> Or use `uv tool install "cruxible-core[mcp]"` if you prefer [uv](https://docs.astral.sh/uv/).

By default the daemon is local-only and binds to `127.0.0.1:8100`. If you want a simple local hardening layer, add `CRUXIBLE_SERVER_AUTH=true` and `CRUXIBLE_SERVER_TOKEN=...`.

### Connect the CLI or GUI

```bash
cruxible --server-url http://127.0.0.1:8100 init \
  --root-dir "$(pwd)" \
  --config config.yaml
```

The returned `instance_id` is the handle the CLI, GUI, and local clients use for later queries, workflows, and mutations.

### Client-Only Agent Environment

```bash
pip install cruxible-client
```

Use `cruxible-client` when the caller only needs typed HTTP/API access to a separate Cruxible daemon.

Permission modes are enforced at the daemon boundary. If an agent can import `cruxible-core` or access the same runtime/filesystem directly, those modes are advisory rather than isolating. If trust levels matter, keep `cruxible-core` out of the agent environment and talk to a separate daemon through `cruxible-client`.

For the `0.2` RC, the daemon-backed API is the primary interface for the CLI, GUI, and local clients. Direct local runtime remains available as a convenience path.

For agent setups, prefer:

- `cruxible-client` in the agent environment
- `cruxible-core` in a separate daemon/runtime environment
- `CRUXIBLE_REQUIRE_SERVER=1` in the agent environment
- daemon state outside the agent workspace

### Prevent Direct State Edits

The biggest local risk is not just bugs in workflows. It is giving an agent
direct filesystem access to the daemon state and letting it bypass the control
plane entirely.

For hardened local usage:

- keep `CRUXIBLE_SERVER_STATE_DIR` outside the repo and outside the agent's
  writable workspace
- prefer `cruxible-client` in the agent environment so the agent talks to the
  daemon over MCP/API instead of importing local runtime code
- in Codex, use `workspace-write` or stricter sandboxing and do **not** grant
  the daemon state path via `--add-dir`
- in Claude Code / Cursor, deny direct access to the daemon state path in your
  local tool settings and expose only the MCP/client surface

This is a practical local hardening layer, not the same thing as full runtime
isolation. For stronger separation, run the daemon in a separate environment
and let the agent talk to it only through `cruxible-client` or MCP.

For a concrete hardened setup, see [Isolated Deployment](docs/isolated-deployment.md).

### MCP Setup

Add the MCP server to your AI agent:

**Claude Code / Cursor** (project `.mcp.json` or `~/.claude.json` / `.cursor/mcp.json`):

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

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.cruxible]
command = "cruxible-mcp"

[mcp_servers.cruxible.env]
CRUXIBLE_MODE = "governed_write"
CRUXIBLE_SERVER_URL = "http://127.0.0.1:8100"
```

### Try a kit

```bash
git clone https://github.com/cruxible-ai/cruxible-core
cd cruxible-core
cruxible --server-url http://127.0.0.1:8100 init --kit kev-reference
```

`kev-reference` is a standalone kit. It materializes a versioned bundle with
config, providers, data, and a bundled lock. From there, run the canonical
reference refresh, query KEV/product relationships, and inspect receipts.

For local cyber triage, create the overlay kit:

```bash
cruxible --server-url http://127.0.0.1:8100 state create-overlay \
  --state-ref kev-reference \
  --kit kev-triage \
  --root-dir "$PWD/kev-triage-workspace"
```

The overlay adds customer-owned assets, services, controls, incidents,
findings, remediation, and governed proposal workflows on top of the public KEV
reference state.

## Why Not Just Use Prompts, Docs, Memory, or a Knowledge Base?

Prompts, markdown playbooks, tickets, generic memory layers, and LLM-built
knowledge bases are good at storing context.

They are bad at storing hard state.

Hard state is the part that should not change just because a different agent saw different context, summarized things differently, or framed the problem another way.

Use memory for:

- temporary notes
- working context
- recall and retrieval
- loose guidance

Use Cruxible for:

- accepted facts and relationships
- accepted judgments and review status
- explicit operational procedure through queries, constraints, workflows, and policies
- receipts and provenance explaining how results and proposals were produced

Cruxible is for the cases where humans and agents need to coordinate around shared truth instead of around temporary summaries.

## Why Cruxible

| Prompts, docs, or memory alone | With Cruxible |
|---|---|
| Facts and decisions get re-summarized on every handoff | Accepted facts and judgments persist as hard state |
| Procedure lives in markdown, habits, and chat context | Queries, constraints, workflows, and policies are explicit |
| Different framing can produce different "truth" | The same state produces the same result |
| Review is social and informal | Review status is part of the model |
| Provenance is scattered across chats and tools | DAG-based receipts explain every result and proposal |
| Memory helps recall | Hard state supports coordination and trust |

## Features

- **Receipt-based provenance:** every query produces a DAG-structured proof showing exactly how the answer was derived.
- **Constraint system:** define validation rules that are checked by `evaluate`. Feedback patterns can be encoded as constraints.
- **Feedback loop:** approve, reject, correct, or flag individual edges. Rejected edges are excluded from future queries.
- **Governed proposal workflows:** providers and workflow steps can create candidate groups with signal-source evidence for review.
- **YAML-driven config:** define entity types, relationships, queries, constraints, workflows, and governed policies in one file.
- **Zero LLM dependencies:** purely deterministic runtime. No API keys, no token costs during execution.
- **Full MCP server:** complete lifecycle via [Model Context Protocol](docs/mcp-tools.md) for AI agent orchestration.
- **CLI mirror:** core MCP tools have [CLI equivalents](docs/cli-reference.md) for terminal workflows.
- **Permission modes:** READ_ONLY, GOVERNED_WRITE, GRAPH_WRITE, and ADMIN tiers control what tools a session can access.

## Kits

| Kit | Domain | What it models |
|-----|--------|----------------|
| [kev-triage](kits/kev-triage/) | Cybersecurity | KEV reference data, asset exposure, and service impact triage. |
| [supply-chain-blast-radius](kits/supply-chain-blast-radius/) | Supply Chain | Supplier, component, assembly, product, shipment, and incident blast radius. |
| [case-law-monitoring](kits/case-law-monitoring/) | Legal | Matter-centered case-law monitoring and authority impact. |
| [retail-catalog](kits/retail-catalog/) | Retail | Product catalog relationships, substitutes, complements, and retail planning surfaces. |

## Demos

| Demo | Domain | What it demonstrates |
|------|--------|---------------------|
| [sanctions-screening](demos/sanctions-screening/) | Fintech / RegTech | OFAC screening with beneficial ownership chain traversal. |
| [drug-interactions](demos/drug-interactions/) | Healthcare | Multi-drug interaction checking with CYP450 enzyme data. |
| [mitre-attack](demos/mitre-attack/) | Cybersecurity | Threat modeling with ATT&CK technique and group analysis. |

## Documentation

- [Quickstart](docs/quickstart.md) — 5-minute install to first query
- [Concepts](docs/concepts.md) — Architecture and primitives
- [Config Reference](docs/config-reference.md) — Every YAML field explained
- [Common Providers And Dataflow Steps](docs/common-providers.md) — Reusable providers versus built-in workflow step types
- [MCP Tools Reference](docs/mcp-tools.md) — All tools with parameters and return types
- [CLI Reference](docs/cli-reference.md) — Terminal commands
- [AI Agent Guide](docs/for-ai-agents.md) — Orchestration workflows for Claude Code, Cursor, Codex, and other MCP clients
- [Kit Walkthroughs](docs/kit-walkthroughs.md) — Author standalone and overlay kits
- [Local State And Backups](docs/local-state-and-backups.md) — SQLite, daemon state, and portability

## Technology

Built on [Pydantic](https://docs.pydantic.dev/) (validation), [NetworkX](https://networkx.org/) (graph), [Polars](https://pola.rs/) (data ops), [SQLite](https://sqlite.org/) (persistence), and [FastMCP](https://github.com/jlowin/fastmcp) (MCP server).

## License

MIT

<!-- mcp-name: io.github.cruxible-ai/cruxible-core -->
