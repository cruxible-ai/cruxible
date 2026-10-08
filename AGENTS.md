# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

**GitHub:** https://github.com/cruxible-ai/cruxible

Cruxible is hard state for AI agents: typed, governed, durable state that
humans and agents share. Values are Claims about Subjects under ClaimTypes;
every change is a proposal checked deterministically, accepted under an
approval policy with signed approvals, and recorded in a Git ledger whose
generations have reproducible coordinates. Procedures and Lines automate work
over that state. No LLM runs inside Cruxible.

User documentation lives in `docs/` (start at `docs/index.md`); the Python SDK
reference is `packages/cruxible-client/README.md`; agent skills are in
`skills/`.

## Commands

```bash
# Install dependencies (both workspace packages, all extras)
uv sync --all-packages --all-extras

# Run the full suite in parallel (pytest-xdist; CI and scripts/ci_parity.sh run it this way).
# --dist loadfile keeps each file on one worker, so module-scoped fixtures build once.
uv run pytest -n auto --dist loadfile

# Run a selection serially
uv run pytest tests/test_claims -v

# Provider-runtime tests need a cruxible-providers checkout, as in CI; without
# CRUXIBLE_PROVIDERS_CHECKOUT they skip (see CONTRIBUTING.md)
export CRUXIBLE_PROVIDERS_CHECKOUT=../cruxible-providers
uv pip install "$CRUXIBLE_PROVIDERS_CHECKOUT/packages/cruxible-provider-runtime"

# Run Docker image tests (requires Docker)
CRUXIBLE_RUN_DOCKER_TESTS=1 uv run pytest tests/test_image -m docker

# Lint, format, type check
uv run ruff check src packages/cruxible-client/src tests
uv run ruff format src packages/cruxible-client/src tests
uv run mypy src packages/cruxible-client/src

# Regenerate a pinned surface after an intentional change, then review the diff
uv run python scripts/update_playbill_served_surface.py
uv run python scripts/update_client_contract_snapshot.py
uv run python scripts/update_http_surface_snapshot.py
uv run python scripts/update_authoring_wire_catalog.py

# Everything CI runs, locally, before a push
scripts/ci_parity.sh

# Run the daemon and CLI from the checkout
uv run cruxible server start --socket ~/.cruxible/run/daemon.sock
uv run cruxible init
```

## Git Conventions

- Do NOT include `Co-Authored-By` lines in commit messages.
- When implementing multi-fix plans, commit each logical fix as it's completed (source + tests together). Don't defer all commits to the end — partial staging across shared files is error-prone. After all commits, prepare a review guide covering the full set.

## Strict project-state adherence

Project state is the record of what was agreed and what is done. Conversation
history, transcripts and agent memory are not: they drift, compact and get
misread. Treat state as authoritative and keep it ahead of the work.

- **Plan from state.** Before planning, starting, resuming or reporting on work,
  read the relevant project-state items. Answer "what's left", "did we do X"
  and "are we done" from accepted state, naming the items, never from memory.
- **Record agreements when they are made.** When the maintainer agrees a plan,
  batch, scope or acceptance criterion, record it as (or revise it into) a
  project-state item in the same working session, before implementation starts.
  A plan that exists only in a conversation is not agreed.
- **No silent re-scoping.** Narrowing, splitting, redefining, deferring or
  dropping an agreed item requires telling the maintainer explicitly, getting a
  ruling, and recording that ruling in state. Never keep an item's name while
  changing what it delivers; new scope is a new item, and the original stays
  open until it is closed by a recorded ruling.
- **Done means the recorded criteria are met.** Report an item or program as
  done only when every acceptance criterion recorded for it is met at an
  accepted coordinate. Otherwise list what remains, by item.
- **Disagreement is a finding.** When code, memory or a conversation disagrees
  with state, stop and reconcile: surface the discrepancy to the maintainer and
  correct whichever side is wrong. Do not proceed on the unrecorded version.

## Project-state completion requirement

A meaningful implementation, merge, deployment, review outcome, or maintainer scope
ruling is not complete until its existing project-state item is reconciled. Do
this without waiting for a reminder. Batch one coherent checkpoint at the end of
the work; do not write after every tool call or create telemetry-only updates.

- Read the existing project workspace's `project-state/README.md` and current
  accepted items first. Linked worktrees reuse that instance and workspace;
  do not initialize a competing project instance or invent a second roadmap.
- Use the public typed SDK and existing credentials: revise existing Claims,
  prepare, submit, inspect the exact changeset, satisfy the instance's approval
  policy, accept, and verify the changed values at the receipt's coordinate.
  Never patch ledger files or projection databases directly.
- Record the exact code range and validation, remaining failures, and task
  ownership. Keep implemented, merged, pushed, deployed, adopted, and released
  distinct. Preserve unrelated work and release/scope rulings.
- Refresh existing gitignored governed views after acceptance and check sync.
  Keep receipts/timings in the existing `.cruxible/project-state/` checkpoint
  area. Do not put operational scripts, payloads, or review guides in `docs/`.
- Measure connection, reads, prepare, submit, review/approval, acceptance and
  readback separately; record omissions, retries and workflow friction. Carry
  the previous completed timing observation in the next meaningful checkpoint,
  without a recursive measurement-only write.
- Before the final reply, check: code outcome recorded, acceptance/readback
  verified, views refreshed. If access or compatibility blocks the update,
  report the specific blocker and retained pending intent/proposal rather than
  claiming state is current. Do not silently upgrade a daemon to fix SDK skew.

This completion requirement does not authorize unrelated state changes, expanded
release scope, or sending messages to other agents or people.

## Versioning

Version lives in these places — keep them in sync:
- `pyproject.toml` (`version = "X.Y.Z"`)
- `src/cruxible_core/__init__.py` (`__version__ = "X.Y.Z"`)
- `packages/cruxible-client/pyproject.toml` and the core pin on it
- `packages/cruxible-client/src/cruxible_client/__init__.py` (`__version__`)
- `server.json` (the MCP registry listing: top-level and package `version`)

`scripts/check_version_lockstep.py` checks all of them. The MCP server name
includes the version (`cruxible vX.Y.Z`) so agents and users can confirm which
build is running.

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
1. Bump the version in every place listed above
2. Run `uv lock` (the lock records both workspace versions), then `uv lock --check` and `uv run python scripts/check_version_lockstep.py`
3. Commit: `Bump to vX.Y.Z`
4. Tag: `git tag vX.Y.Z`
5. Push: `git push && git push --tags`; on the tag, `publish.yml` verifies the lockstep and publishes both PyPI packages, and `publish-runtime-image.yml` builds and pushes the runtime image. Neither creates a GitHub release; write one by hand if the release needs notes

## Architecture

### One daemon, three clients

The daemon (`cruxible server start`, `server/`) is the only process that
touches accepted state. It serves an HTTP API over a Unix socket or TCP. The
CLI (`cli/`), the MCP server (`mcp/`) and the Python SDK
(`packages/cruxible-client/`) are all clients of that API.

```
CLI (cli/)                    ─┐
MCP server (mcp/)              ├─HTTP─▶ routes (server/routes/) ─▶ runtime facade (runtime/playbill_api.py)
SDK (packages/cruxible-client) ┘                                   ─▶ service/<domain>/ ─▶ domain packages
```

- **HTTP** (`server/`): FastAPI routes with bearer-token auth (off by default on
  a Unix socket, required on TCP), the state-root lock, credentials, and the
  host registry.
- **Runtime facade** (`runtime/playbill_api.py`): translates wire contracts and
  delegates to the service layer; `runtime/instance.py` (`PlaybillInstance`)
  manages one instance's managed root.
- **Service layer** (`service/<domain>/`): the single home for served
  orchestration (authoring, claims, evidence, proposals, procedures, discovery
  and reads, floor, kits). Never duplicate orchestration in a route, a handler
  or a CLI command.
- **CLI** (`cli/commands/`): Click commands that call the daemon through the
  SDK's `CruxibleClient`.
- **MCP** (`mcp/`): FastMCP tools named after their CLI paths
  (`cruxible_proposal_approve`), each a daemon client call; `curation.py`
  defines the default and full profiles.
- **SDK** (`packages/cruxible-client/`): typed contracts
  (`cruxible_client.contracts`), the HTTP transport (`CruxibleClient`), and the
  agent-facing `Cruxible` SDK with World, authoring drafts and handles.

Client-side work stays on the client and sends the daemon bytes, never paths:
source compilation, file-evidence observation, projection-block stamps, floor
writes, and approval signing with a principal's private key.

### Accepted state

Each instance's managed root under the daemon state root
(`<state-root>/instances/<id>/`) holds:

```
instance.json   # instance descriptor
ledger.git/     # accepted generations, proposals, and their Git notes
cas/            # content-addressed bodies
exhaust/        # journals, triggers, proposal evidence, operational stores
projections/    # rebuildable served indexes
credentials/    # daemon signing custody
leases/         # local writer leases
```

The signed generation ledger is authority; every accepted generation has a
coordinate (Git OID, semantic root, generation root, compiler digest).
Indexes, the workspace floor and projection blocks are derived and
rebuildable. The compiler (`compiler/`) is the pinned, versioned
interpretation of the ledger; adopting a new compiler revision is a governed
proposal.

### Semantic families

Domain packages own the artifact kinds and their acceptance laws: `claims/`
(Subjects, ClaimTypes, Claims, attestations), `evidence/` (Captures and
CaptureContracts), `documents/`, `query/` (named queries), `procedures/`
(graph-format-6 Procedures, Blueprints, Lines, measurements), `providers/`
(ProviderInterfaces, Providers, installation and the built-in
`workspace.file`), `triggers/`, `governance/` (principals, approval policy,
genesis seeds), `curation/`, `coverage/` and `floor/`. `consumers/` runs the
daemon's background workers (floor delivery, the `next` findings worker, and
enabled Lines), driven by Triggers and accepted generations; curation
detection runs as the `curation.detect` internal action.

### Two write lanes

Values change through the write verbs (`set`, `add`, `retire`, `write`), which
lower into an ordinary authoring change set and accept it when the approval
policy and the caller's tier allow. Definitions go through authoring
(`authoring submit`, staged intents). ClaimTypes keep their own `claim-type`
group, because changing vocabulary disposes dependent Claims.

### Key design decisions

- **Zero LLM dependencies.** Deterministic runtime; agents supply judgment
  through MCP, the CLI and the SDK, and providers supply contracted
  computation.
- **Pydantic for contracts and receipts.** Canonical values reject ambiguous
  encodings; new surfaces are typed models and discriminated unions, never
  loose dicts.
- **Git plus signed ledgers for governed authority.** SQLite indexes and
  operational stores never replace the accepted tree.
- **Frozen format tags.** Stored and served format tags keep their
  `playbill-*-vN` spelling, because digests and signatures cover them; public
  names say Cruxible.

### Permission tiers

Four cumulative tiers (`ADMIN ⊃ GRAPH_WRITE ⊃ GOVERNED_WRITE ⊃ READ_ONLY`),
defined as `PermissionMode` in `runtime/permissions.py`, with the tier of
every MCP tool in `TOOL_PERMISSIONS`:

| Tier | Env value | Allows |
|------|-----------|--------|
| `READ_ONLY` | `read_only` | Reads, queries, coverage, work queues |
| `GOVERNED_WRITE` | `governed_write` | Also authoring, proposals, value writes, attestations, Line enablement |
| `GRAPH_WRITE` | `graph_write` | Also approving and activating proposals |
| `ADMIN` | `admin` (default) | Also credentials, hosts, principals, compiler upgrades, provider installs, ledger mirrors |

A bearer credential carries a tier; `--capability-ceiling` (or `CRUXIBLE_MODE`)
caps the daemon, and `CRUXIBLE_MODE` caps an MCP server. Principals (governed
signing keys) are separate from credentials: they attribute acts and sign
approvals. Audit logging uses structlog to stderr.

### Error handling

All errors inherit from `CoreError` in `errors.py`. Wire and execution refusals
are typed in `cruxible_client.contracts.errors`, with codes
`cruxible.<family>.<name>`, and every refusal names its repair.

### Test organization

Tests mirror domain ownership under `tests/test_<domain>/` (`test_claims`,
`test_procedures`, `test_ledger`, ...), plus `test_client`, `test_server`,
`test_cli` and `test_mcp`; shared fixtures live in `tests/core_support/`.
`tests/test_architecture` and `tests/test_guardrails` pin boundaries, public
snapshots, contract catalogs, the reference docs and the public vocabulary.
Golden journal-corpus tests under `tests/goldens/` are intentionally expensive
and should only run when a change touches what they pin.
