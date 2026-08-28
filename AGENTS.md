# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

**GitHub:** https://github.com/cruxible-ai/cruxible

Cruxible Core is a deterministic decision engine with receipts. AI agents (Codex, etc.) write configs and orchestrate workflows. Core executes deterministically with proof — no LLM inside.

Four primitives: **Config**, **Ingest**, **Query**, **Feedback**.

## Commands

```bash
# Install dependencies
uv sync --all-extras

# Run tests
uv run pytest

# Run Docker image tests (requires Docker)
CRUXIBLE_RUN_DOCKER_TESTS=1 uv run pytest tests/test_image -m docker

# Run single test file
uv run pytest tests/test_config/test_schema.py -v

# Lint
uv run ruff check src tests

# Format
uv run ruff format src tests

# Type check
uv run mypy src
```

## Git Conventions

- Do NOT include `Co-Authored-By` lines in commit messages.
- When implementing multi-fix plans, commit each logical fix as it's completed (source + tests together). Don't defer all commits to the end — partial staging across shared files is error-prone. After all commits, prepare a review guide covering the full set.

## Review Request Conventions

- For code-change ReviewRequests in the agent-operation kit, include structured
  `change_repo`, `change_base`, and `change_head` fields. `change_head` is the
  exact reviewed commit SHA; reviewers and merge tooling should not infer it
  from the branch tip.
- Keep `ReviewRequest.summary` implementer-owned: scope, verification evidence,
  known failures, and review context. Reviewers should put requested changes and
  approval notes in `ReviewRequest.review_notes`.

## Versioning

Version lives in two places — keep them in sync:
- `pyproject.toml` (`version = "X.Y.Z"`)
- `src/cruxible_core/__init__.py` (`__version__ = "X.Y.Z"`)

The MCP server name includes the version (`cruxible-core v0.2.0`) so agents and users can confirm which build is running.

**When to bump:**
- **Patch (0.2.x):** Bug fixes, doc/prompt wording changes, test additions
- **Minor (0.x.0):** New features (tools, evaluate checks, config capabilities), breaking prompt changes
- **Major (x.0.0):** Breaking API changes (tool signatures, config schema, storage format)

**Recorded ruling — maintainer, 2026-08-06 (`dd-compat-sacrificeable-for-product`).** The 0.4.x
line may break 0.3 compatibility; staging within 0.4.x is allowed. This carves out the rows
above for 0.4.x: a config-schema or storage-format change that would otherwise be MAJOR ships
inside the minor. Provenance integrity is **not** compatibility and may never be sacrificed —
stored digests are never recomputed under a different rule, and receipts stay verifiable
forever, including by the frozen verifiers of retired formats.

**Release process:**
1. Bump version in both files
2. Run `uv lock --check` and `uv run python scripts/check_version_lockstep.py`
3. Commit: `Bump to vX.Y.Z`
4. Tag: `git tag vX.Y.Z`
5. Push: `git push && git push --tags`; the tag workflow publishes both PyPI packages and creates or updates the GitHub release

## Architecture

### Four Surfaces, One Playbill Core

All interfaces delegate to the Playbill service layer. Never duplicate orchestration logic in handlers or transports.

```
SDK (cruxible_client.authoring) ─┐
MCP (mcp/)                       ├──▶ service/playbill_*.py ──▶ playbill/
CLI (cli/)                       │
HTTP (server/)                  ─┘
```

- **SDK** (`packages/cruxible-client/`) — typed contracts, HTTP transport, and agent-oriented authoring/reading adapters.
- **MCP** (`mcp/`) — FastMCP tools delegating through the same runtime/client surfaces.
- **CLI** (`cli/`) — Click commands; Playbill commands live in `cli/commands/playbill.py`.
- **HTTP** (`server/`) — FastAPI routes with bearer-token authentication.

### Playbill service layer (`service/playbill_*.py`)

The source of truth for served orchestration. It is organized by concern:

- claims/evidence/proposals and proposal review;
- coverage, discovery, search, since, and the deterministic floor;
- procedure authoring/execution and `next` repair rows;
- operational curation and audit worklists.

Service functions accept a `PlaybillInstance` and return typed Pydantic results.

### Playbill instance and accepted state

`PlaybillInstance` in `playbill/instance.py` manages the daemon-owned repository and stores:

```
.cruxible-playbill/
  instance.json       # instance descriptor
  repository.git/     # accepted and proposal trees
  bodies/             # content-addressed bodies
  journal/            # signed generation/procedure journals
  projections/        # rebuildable served indexes
  operational/        # non-governed curation/audit/authoring state
```

The signed generation ledger and accepted Git tree are authority. Projections,
file floors, and operational stores are derived or explicitly non-governed.

### Procedure system (`playbill/procedures/`)

Procedures compile to the frozen graph-v3 representation and execute
deterministically. Admission binds inputs and coordinates before execution;
the exhaust journal records node outcomes, dependency manifests, effects, and
typed terminal egress receipts. Line specs add recurring triggers and retained
line-grained track records through accepted exhaust promotions.

### Key Design Decisions

- **Zero LLM dependencies.** Purely deterministic runtime. Codex provides all intelligence via MCP tools.
- **Pydantic for contracts and receipts.** Canonical values reject ambiguous encodings.
- **Git plus signed ledgers for governed authority.** SQLite indexes and operational stores do not replace the accepted tree.
- **Content-addressed bodies and deterministic projection.** Served reads reproduce from accepted coordinates.
- **Pointer-model knowledge.** Claims bind ordinary source substrates; the ledger remains the singular truth plane.

### Permission Modes

MCP tools are gated by `CRUXIBLE_MODE` env var. Four cumulative tiers
(`ADMIN ⊃ GRAPH_WRITE ⊃ GOVERNED_WRITE ⊃ READ_ONLY`), defined as
`PermissionMode` in `runtime/permissions.py`:

| Mode | Env value | Tools |
|------|-----------|-------|
| `READ_ONLY` | `read_only` | Playbill reads, receipted query runs, coverage, curation/audit reads |
| `GOVERNED_WRITE` | `governed_write` | READ_ONLY + authoring/proposal and attributed operational actions |
| `GRAPH_WRITE` | `graph_write` | Retained tier boundary; no legacy graph-write product surface |
| `ADMIN` | `admin` (default) | Instance/principal lifecycle and published-state trust boundaries |

- `CRUXIBLE_ALLOWED_ROOTS` env var (comma-separated absolute paths) restricts which directories `cruxible_init` can access.
- Audit logging uses structlog to stderr.

### Error Handling

All errors inherit from `CoreError` in `errors.py`. Playbill wire and execution
refusals are typed in `cruxible_client.contracts.errors`.

### Test Organization

Tests mirror the live surfaces under `tests/test_playbill`, `test_client`,
`test_server`, `test_cli`, and `test_mcp`. `tests/test_architecture` and
`tests/test_guardrails` pin DP-0 boundaries, public snapshots, and contract
catalogs. Golden journal-corpus tests are intentionally expensive and should
only run when their dispatch explicitly permits them.
