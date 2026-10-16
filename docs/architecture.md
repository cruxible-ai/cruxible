# Cruxible architecture

Cruxible separates accepted authority from storage, projections, and high-rate
exhaust. That separation is the core invariant.

## Components

| Component | Role | Authoritative? |
|---|---|---|
| Git ledger | Accepted envelopes, principal state, proposals, approvals, generations | Yes |
| Content-addressed storage | Exact immutable body and artifact bytes referenced by digest | For those bytes only |
| SQLite | Query indexes, operational metadata, rebuildable projections | No |
| Source systems | Databases, APIs, files, and services Cruxible references | Yes, for their own records |
| Event/exhaust stream | High-rate observations, actions, and processing exhaust | Evidence/input, not accepted state |
| Compiler | Deterministically turns accepted envelopes and source bundles into semantic projections | Pinned interpreter |

A source database does not become subordinate to Cruxible. Cruxible may accept a
Claim about a row, pin a source coordinate, or record an attestation concerning
it without copying the table or claiming authority over the source record.

## Accepted coordinates

Every accepted generation is addressed by a four-part coordinate:

- Git OID identifies the exact ledger tree.
- Semantic root commits to governed meaning.
- Generation root commits to the accepted generation.
- Compiler digest commits to the deterministic interpretation.

A read that omits this context is presentation, not a portable proof of state.

## Mutation path

~~~text
bytes/source references
        │
        ▼
inert CAS or client-compiled source bundle
        │
        ▼
authenticated proposal
        │
        ▼
deterministic candidate + law evidence
        │
        ▼
review + coordinate-bound signed approvals
        │
        ▼
activation checks parent and settlement base
        │
        ▼
accepted Git generation
        │
        ├──> rebuildable projections
        ├──> history
        └──> get (why, history, proof)
~~~

Proposal creation never mutates accepted state. Approval signs a frozen
challenge. Activation independently re-verifies the candidate and advances by
compare-and-set, so concurrent settlement cannot silently overwrite a newer
generation.

## Surfaces

The daemon (`cruxible server start`) is the only process that touches
accepted state. It serves an HTTP API over a Unix socket or TCP. The CLI, the
MCP server (`cruxible mcp`) and the Python SDK (`cruxible-client`) are all
clients of that API; none of them holds state of its own. Work that needs the
client's files happens on the client and sends the daemon bytes, never paths:
compiling catalogued sources, quoting a cited passage, stamping projection
blocks, and signing approvals with a private key.

Background work runs inside the daemon as consumers driven by Triggers:
delivering the floor to a registered workspace after each accepted generation,
detecting curation patterns, and running enabled Lines.

## Keys and credentials

Runtime bearer credentials authenticate transport and cap what a caller may do
with a tier. Cruxible principals represent governance authority. Their Ed25519
private keys stay with the client; the ledger stores public keys and key
history. A separate daemon key signs ledger mechanics. Recovery authority can
repair principal state but cannot approve ordinary content. See [Principals and
credentials](concepts.md#principals-and-credentials).

## Integrity and isolation

On-disk integrity checks (content hashes, read-back verification on store, a
periodic evidence sweep) protect against accidents: crashes, partial writes,
media errors and stray edits. A body found missing or damaged is recorded, and
reads report the evidence that depends on it as unavailable rather than failing;
reading the damaged bytes themselves still refuses. These checks are not a
security boundary, since a process running as the daemon's user can rewrite its
files. Security against agents comes from isolation (agents talk to the daemon,
never to its state directory) and from the API: authenticated transport,
permission tiers and signed governance authority.

## Semantic families

The ledger holds typed artifacts, each checked by its own law at acceptance:

- Subjects, ClaimTypes and Claims: the typed values, their vocabulary, and
  attestations that support, contradict or hold them unsure without silently
  changing them;
- Documents: whole files governed by exact bytes, with bodies in
  content-addressed storage and a small envelope in the ledger;
- CaptureContracts and Captures: what retained evidence must look like, and
  the evidence itself;
- QueryDefinitions: named queries;
- Procedures and Blueprints: deterministic graphs with pinned providers, and
  their skeletons with open slots; ProviderInterfaces and Providers;
- Lines, Triggers and ProcedureMandates: when Procedures run and what they may
  propose or settle;
- ResolutionContracts (predictions), the approval policy, the Procedure runtime
  policy, source acquisition policies, and principals.

## Hot and cold paths

High-rate activity (Procedure runs, attempts, intermediate results) is
retained as operational exhaust, outside the ledger. Procedures and Lines
select from it and propose governed objects to the ledger. The exhaust records
what happened; the ledger records what has been accepted. Neither is a shadow
copy of the other.

Indexes, search engines or graph databases that accelerate reads are
projections of accepted state and can be rebuilt from it.

## Retention

One setting (`RetentionSettings`: 64 generations, 14 days) bounds what is kept
of rebuildable and raw operational state; nothing a signed or authoritative
record references is deleted under it.

- **Projection pieces.** Each accepted generation's projection is a full SQLite
  piece. The serving head and the last 64 accepted generations are kept (an
  open proposal is evaluated at the head, so its base is among them), and so
  is any piece a reader holds open in any process. Each activation evicts the
  generation that left the window, and the daemon's open evicts the backlog,
  each manifest before its piece. A piece skipped because a reader in the
  daemon held it is evicted by the pass after that reader closes it; one
  another process held then waits for the daemon's next open. Binding an
  evicted coordinate (`--at`, a pinned read, a stale proposal's base)
  rebuilds it from the ledger and the body store, including when the
  coordinate is evicted while the bind is reading it and another reader
  rebuilds it before that read's failure is classified.
- **Accepted proposals' candidates and approvals.** An accepted proposal's
  change-set record carries every field of its candidate and the approvals
  settlement verified, so after activation the candidate file and the
  approval files the record holds byte for byte are deleted, and `proposal
  inspect`, review, status and the proposal index read them from the record;
  an approval submitted later is read beside the record's.
  Withdrawn, rejected and refused proposals' evidence, and any approval the
  record does not hold, are kept.
- **Consumption receipts.** Raw receipts are kept 14 days, then fold into
  counters (touches per artifact and operation, and the audit's distinct
  reader keys) that remember every receipt they counted, so a read served
  again is never counted twice; only folded receipts are removed. Folds run
  one at a time and each lands only if no other fold landed since it chose
  its receipts.
- **Line coverage.** An idle Line's coverage that moved only in time advances
  the dispatch projection and is not journaled.
- **The ledger.** Every ref, note and archived proposal is kept. Objects are
  written in in-process pack/index batches, flushed before publishing their
  refs. Commit signatures use Ed25519 SSHSIG compatible with `git verify-commit`;
  ref leases use Git's files-backend lockfile protocol. Reads support loose
  objects and Git's delta-compressed packs. Every 64 accepted generations the
  daemon consolidates packs (`repack -a -d`), packs refs and writes the
  commit-graph, off the
  acceptance path, and at start, before serving, it also prunes unreachable
  loose objects, the one step that is safe only while no writer is in flight.

## Repository ownership

The daemon implementation lives directly under `src/cruxible_core/`, grouped by
responsibility. `ledger/` owns Git publication and accepted generations;
`compiler/` owns deterministic compilation; `indexes/` owns rebuildable SQLite
read models. Claims, evidence, documents, procedures, providers, governance,
coverage, and curation each have their own package.

`service/<domain>/` is the single home for served orchestration. CLI, HTTP, MCP,
and SDK-facing runtime adapters delegate there. `runtime/` owns instance
lifecycle and process configuration; `derived/` coordinates derived reads;
`exhaust/` retains operational records; `storage/` contains body storage and
staging primitives. The separately packaged SDK and typed contracts remain in
`packages/cruxible-client/`.

Tests follow the same ownership under `tests/test_<domain>/`; shared fixtures
live in `tests/core_support/`. Frozen format fixtures remain in `tests/goldens/`.
Internal package relocation does not rename wire operations, managed-state
paths, or signed format identifiers.
