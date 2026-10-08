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
