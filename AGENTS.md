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

**Release process:**
1. Bump version in both files
2. Rebuild kit bundles + manifest: `uv run python scripts/build_kit_bundles.py` (if a kit's config/providers changed, first `uv run cruxible lock --kit-dir kits/<kit>`); verify with `uv run python scripts/check_kit_lockfiles.py` and commit the regenerated manifest/locks
3. Commit: `Bump to vX.Y.Z`
4. Tag: `git tag vX.Y.Z`
5. Push: `git push && git push --tags` (upload `dist/kits/*.tar.gz` as release assets on the tag)

## Architecture

### Three Surface Layers, One Service Core

All interfaces delegate to the **service layer** (`service/`). Never duplicate orchestration logic in handlers.

```
MCP (mcp/)  ──┐
CLI (cli/)  ──┼──▶  Service Layer (service/)  ──▶  Core Modules
HTTP (server/) ┘
```

- **MCP** (`mcp/`) — Primary interface for AI agents via FastMCP. Handlers in `handlers.py` support dual-mode: library-mode (direct calls) or server-mode (delegates to `CruxibleClient`).
- **CLI** (`cli/`) — Click CLI. Commands in `commands.py` delegate to service functions.
- **HTTP** (`server/`) — FastAPI REST server with bearer-token auth. Routes in `server/routes/`. Supports HTTP and Unix Domain Socket transports.
- **Client** (`client/`) — `CruxibleClient` SDK for talking to HTTP servers. Mirrors all service operations.

### Service Layer (`service/`)

The source of truth for all business logic. Organized by concern:

- `queries.py` — Read operations (query, schema, inspect, list, stats, sample)
- `mutations.py` — Graph mutations (add_entities, add_relationships, ingest)
- `feedback.py` — Feedback collection and outcome recording
- `execution.py` — Workflow execution (plan, run, test, apply, propose, lock)
- `groups.py` — Candidate group proposal management with resolution/trust
- `analysis.py` — Constraint evaluation and candidate finding
- `snapshots.py` — State snapshots for branching/recovery
- `types.py` — All input/output types (typed dataclasses)

Service functions have consistent signatures: accept `instance: InstanceProtocol`, return typed result dataclasses.

### Instance Protocol (`instance_protocol.py`)

Structural protocols defining abstract instance/store interfaces:
- `InstanceProtocol` — Graph/config loading, snapshot creation, store access
- `ReceiptStoreProtocol`, `FeedbackStoreProtocol`, `GroupStoreProtocol`, `EntityProposalStoreProtocol`

This abstraction enables future non-SQLite backends (e.g., cloud storage) without coupling.

The concrete implementation is `CruxibleInstance` in `cli/instance.py`, which manages the `.cruxible/` directory:

```
.cruxible/
  instance.json     # Bootstrap metadata (config path, version, compatibility mirror)
  state.db          # SQLite live graph, audit/governance stores, snapshots, artifacts, head/origin
  snapshots/
    <snapshot_id>/
      graph.json    # Portable export/cache materialized from DB snapshot artifacts
      config.yaml
      snapshot.json
```

Snapshot metadata, snapshot artifacts, and authoritative head/origin state live
in `state.db`. `.cruxible/snapshots/` is a portable export/cache, and
`.cruxible/graph.json` is not live authority.

### Workflow System (`workflow/`)

Deterministic workflow engine with lock-file reproducibility:

- `compiler.py` — Compiles workflows to `CompiledPlan`, generates SHA256 locks (`cruxible.lock.yaml`), resolves providers and artifacts
- `executor.py` / `step_handlers.py` — Runtime execution dispatching 19 step kinds (the `StepKind` literal in `config/schema.py`; `DEFAULT_STEP_HANDLER_REGISTRY` asserts coverage of all of them): query, provider, assert, assert_not_truncated, assert_count, assert_exists, shape_items, join_items, filter_items, aggregate_items, dedupe_items, make_candidates, map_signals, propose_relationship_group, make_entities, make_relationships, apply_entities, apply_relationships, apply_all
- `contracts.py` — Payload validation against declared contracts
- `refs.py` — Step reference resolution (`$input`, `$steps.*`, `$item`)

Three execution modes: `run` (non-canonical), `preview` (canonical dry-run), `apply` (canonical with mutations). Canonical workflows create `StateSnapshot` objects with lineage tracking.

### Provider System (`provider/`)

External provider execution with tracing. Providers are callables resolved by the registry (`provider/registry.py`). Each execution produces an `ExecutionTrace` (input/output, duration, status, artifact hash) persisted to `state.db`.

### Groups and Entity Proposals

Two parallel governed-mutation systems:

- **Groups** (`group/`) — Relationship proposals using tri-state signals (support/contradict/unsure) from integrations. `CandidateGroup` tracks status: pending_review → auto_resolved/applying → resolved.

Both are persisted in the unified `state.db` via their respective stores.

### Key Design Decisions

- **Zero LLM dependencies.** Purely deterministic runtime. Codex provides all intelligence via MCP tools.
- **Pydantic for all models.** Config schema, runtime types, receipts — all validated.
- **Polars for data operations.** Ingestion and candidate detection use Polars DataFrames.
- **NetworkX for graph.** EntityGraph wraps networkx DiGraph for entity/relationship storage.
- **SQLite for persistence.** Receipts, feedback, outcomes, groups, proposals stored in SQLite.
- **YAML for config.** Defines entity types, relationships, named queries, constraints, ingestion mappings, workflows, quality checks, integrations, and provider artifacts.

### Config Schema (`config/schema.py`)

Configs define a decision domain. Beyond the basics (entity_types, relationships, named_queries, constraints, ingestion), the schema includes:

- `workflows` — Declarative step-based execution plans
- `quality_checks` — 5 types: property, json_content, uniqueness, bounds, cardinality
- `integrations` — External integration specs with contracts and guardrails
- `matching` — Per-relationship proposal rules (auto-resolve conditions, trust requirements)
- `artifacts` — External resources (models, data) referenced by workflows

### Evaluation (`evaluate.py`)

Deterministic graph quality assessment with 6 checks:
1. Orphan entities (no edges)
2. Coverage gaps (declared types missing from graph)
3. Constraint violations (rule-based)
4. Candidate opportunities (shared neighbors, missing edges)
5. Low-confidence edges
6. Unreviewed co-members

### Permission Modes

MCP tools are gated by `CRUXIBLE_MODE` env var. Four cumulative tiers
(`ADMIN ⊃ GRAPH_WRITE ⊃ GOVERNED_WRITE ⊃ READ_ONLY`), defined as
`PermissionMode` in `runtime/permissions.py`:

| Mode | Env value | Tools |
|------|-----------|-------|
| `READ_ONLY` | `read_only` | `init` (reload only), `validate`, `schema`, `query`, `receipt`, `list`, `sample`, `evaluate`, `find_candidates`, `get_entity`, `get_relationship`, inspect/lint/trace reads |
| `GOVERNED_WRITE` | `governed_write` | READ_ONLY + governed operator actions: `feedback`, `outcome`, proposal/group workflows, decision records, snapshot creation, source artifact registration, and subscribed state pulls |
| `GRAPH_WRITE` | `graph_write` | GOVERNED_WRITE + direct graph writes (`add_entity`, `add_relationship`), canonical workflow apply, and group resolution / trust updates |
| `ADMIN` | `admin` (default) | All tools: instance lifecycle, active config replacement, locks, clones, overlays, `ingest`, `add_constraint`, and published-state trust boundaries |

- `CRUXIBLE_ALLOWED_ROOTS` env var (comma-separated absolute paths) restricts which directories `cruxible_init` can access.
- Audit logging uses structlog to stderr.

### Error Handling

All errors inherit from `CoreError` in `errors.py`. Key types: `ConfigError`, `DataValidationError`, `EntityNotFoundError`, `RelationshipNotFoundError`, `QueryNotFoundError`, `PermissionDeniedError`.

### Test Organization

Tests mirror the src layout under `tests/`:
- `test_service/` — Service layer tests (primary coverage target)
- `test_mcp/` — MCP handler tests
- `test_cli/` — CLI command tests (includes server-mode testing)
- `test_config/`, `test_graph/`, `test_query/`, `test_receipt/`, `test_feedback/`, `test_workflow/` — Module-level tests
- `conftest.py` — Shared fixtures including workflow config templates and `canonical_workflow_instance`
- `support/` — Test helpers (e.g., `workflow_test_providers.py`)
