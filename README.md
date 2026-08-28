<p align="center">
  <a href="https://cruxible.ai">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-white.svg">
      <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-black.svg" alt="Cruxible" width="360">
    </picture>
  </a>
</p>

# Cruxible Playbill development core

> This branch is an intentionally breaking development line. The former
> config-authority, mutable graph product, bundled kits, snapshots, and state
> distribution interfaces have been removed. Do not treat it as a compatible
> release of the old Cruxible Core.

Playbill is a governed state substrate for humans and AI agents. It makes
accepted state behave like reviewed code: proposals are deterministic,
approvals bind to exact bytes, activation is compare-and-set, and every accepted
generation has a reproducible coordinate.

The strategic unit is not a database row or an entire prose document. It is a
semantic subject—ultimately a Claim or a Procedure—whose provenance, governance,
attestations, and history can be explained at an exact accepted coordinate.
Documents are the first implemented family and the source container from which
more granular subjects will be compiled.

No LLM runs inside the engine. Models and humans propose, review, and query;
Playbill validates and settles deterministically.

## What exists today

Family 1 is implemented end to end for governed Documents:

- exact body bytes live in content-addressed storage (CAS);
- compact envelopes live in a daemon-owned Git ledger;
- proposals freeze candidates before review;
- approvals are Ed25519 signatures created with client-held private keys;
- activation verifies approvals and advances accepted state atomically;
- SQLite projections are rebuildable indexes, not ground truth;
- history and explanation bind results to Git OID, semantic root, generation
  root, and compiler digest;
- local source catalogs compile declared files into path-free exact-byte bundles.

Claims and Playbill-native Procedures are the next implementation program. The
old Procedure, workflow, query, graph, receipt, and SQLite modules remain only as
an explicit donor island while their deterministic behavior and goldens are
transplanted. They are not served by the public Playbill API.

See [Family 1](docs/playbill-family-1.md) and
[Architecture](docs/architecture.md).

## The lifecycle

~~~text
local bytes ──store──> inert CAS object
                         │
semantic envelope ──propose──> frozen candidate
                                │
                         inspect / review
                                │
                    client-held key signs challenge
                                │
                           verify approval
                                │
                          activate by CAS
                                │
                    accepted Git generation
                                │
               projection / query / explain / history
~~~

CAS presence is not acceptance. A proposal is not accepted state. A signature
does not itself activate anything. These distinctions keep storage,
intent, judgment, and settlement from collapsing into a second source of truth.

## Authority model

The Git ledger is accepted authority. CAS stores referenced bytes. SQLite and
rendered files are disposable projections that can be rebuilt from the ledger
and pinned compiler. External systems remain authoritative for their own data:
Playbill records governed semantic references, observations, proposals, and
attestations without copying entire tables or pretending to replace the source.

Every accepted read names a coordinate:

- Git OID: exact accepted ledger tree;
- semantic root: digest of governed meaning;
- generation root: digest of the accepted generation;
- compiler digest: exact deterministic interpretation.

A hot event stream can later capture high-rate exhaust while the ledger
settles governed objects at a slower rate. That event stream is evidence and
input, not a competing accepted-state authority.

## Public surface

The currently registered CLI commands are deliberately small:

~~~text
cruxible context
cruxible credential
cruxible playbill
cruxible server
~~~

Playbill exposes host allocation, initialization, body storage, Document
proposal/review/approval/activation, principal governance, source compilation,
accepted reads, history, and explanation. HTTP, MCP, CLI, and the Python client
delegate to the same Playbill service core.

The old entity/relationship mutation, config reload, kit install, workflow,
snapshot, overlay, feedback, decision, and state-distribution commands are
absent by design.

## Developer quickstart

Requirements: Python 3.11+, Git, and uv.

~~~bash
git clone https://github.com/cruxible-ai/cruxible
cd cruxible
uv sync --all-extras
uv run pytest -q tests/test_playbill tests/test_architecture/test_playbill_dp0_boundaries.py
~~~

Start a local daemon in one shell:

~~~bash
uv run cruxible server start \
  --state-dir /tmp/cruxible-playbill-dev \
  --bootstrap-secret-file /tmp/cruxible-playbill-bootstrap
~~~

In another shell, authorize the bootstrap session, allocate a host, and create
a client-held owner key outside the workspace:

~~~bash
export CRUXIBLE_SERVER_URL=http://127.0.0.1:8100
export CRUXIBLE_SERVER_BEARER_TOKEN="$(cat /tmp/cruxible-playbill-bootstrap)"

uv run cruxible playbill host create --instance-id playbill-demo
uv run cruxible playbill init \
  --key-dir /tmp/cruxible-playbill-owner \
  --reviewer-key-dir /tmp/cruxible-playbill-reviewer \
  --principal-id bootstrap-admin
uv run cruxible playbill document list
~~~

The init command prints both private-key paths. The daemon receives only the two
public ordinary-principal records. `--reviewer-key-dir DIR` is required so the
genesis registry has independent client custody. See the
[Quickstart](docs/quickstart.md) for a complete Document proposal and activation.

## Security boundaries

Runtime bearer credentials and Playbill principals solve different problems:

- bearer credentials authorize transport operations and carry a capability tier;
- Playbill principals identify and attribute governed acts at exact coordinates;
- repository ref governance supplies organizational authorization;
- owner/reviewer/recovery private keys remain in client custody;
- the daemon has a separate instance-specific key for ledger mechanics;
- source compilation happens client-side, so the daemon never dereferences a
  client filesystem path.

See [Authentication and principals](docs/runtime-auth-and-agent-roles.md).

## Destructive convergence status

Completed in the current development line:

- isolated the Playbill-only served core;
- removed legacy CLI, HTTP, MCP, and client operations;
- removed canonical views, blueprints, bindings, decisions, feedback, installs,
  snapshots, state transport, telemetry, UI assets, and working sets;
- removed first-party kit bundles and packaged kit distribution;
- retained frozen Procedure digests, query goldens, and a byte-identical compact
  config fixture for parity work.

Temporarily retained donors include Procedure/workflow/query/graph behavior,
receipt construction, attestation and resolution semantics, provider execution,
and the old instance/SQLite harness. Their removal batches are pinned by the
donor manifest and architecture tests.

## Verification

The minimum Playbill gate is:

~~~bash
uv run pytest -q tests/test_playbill tests/test_architecture/test_playbill_dp0_boundaries.py
uv run mypy src/cruxible_core/playbill src/cruxible_core/service
uv run ruff check src packages/cruxible-client/src tests
~~~

The full legacy suite is not a compatibility target during destructive
convergence, but retained donor tests must continue to collect and the frozen
oracles must not drift.

## Documentation

- [Quickstart](docs/quickstart.md)
- [Concepts](docs/concepts.md)
- [Architecture](docs/architecture.md)
- [CLI reference](docs/cli-reference.md)
- [MCP reference](docs/mcp-tools.md)
- [Family 1 Documents](docs/playbill-family-1.md)
- [Authentication and principals](docs/runtime-auth-and-agent-roles.md)

Apache-2.0 licensed.
