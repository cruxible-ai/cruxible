# Playbill architecture

Playbill separates accepted authority from storage, projections, and high-rate
exhaust. That separation is the core invariant.

## Components

| Component | Role | Authoritative? |
|---|---|---|
| Git ledger | Accepted envelopes, principal state, proposals, approvals, generations | Yes |
| Content-addressed storage | Exact immutable body and artifact bytes referenced by digest | For those bytes only |
| SQLite | Query indexes, operational metadata, rebuildable projections | No |
| Source systems | Databases, APIs, files, and services Playbill references | Yes, for their own records |
| Event/exhaust stream | High-rate observations, actions, and processing exhaust | Evidence/input, not accepted state |
| Compiler | Deterministically turns accepted envelopes and source bundles into semantic projections | Pinned interpreter |

A source database does not become subordinate to Playbill. Playbill may accept a
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
        └──> explain
~~~

Proposal creation never mutates accepted state. Approval signs a frozen
challenge. Activation independently re-verifies the candidate and advances by
compare-and-set, so concurrent settlement cannot silently overwrite a newer
generation.

## Keys and credentials

Runtime bearer credentials authenticate API transport and impose a capability
ceiling. Playbill principals represent governance authority. Their Ed25519
private keys stay with the client; the ledger stores public keys and key
history. A separate daemon key signs ledger mechanics. Recovery authority can
repair principal state but cannot approve ordinary content.

## Documents, Claims, and Procedures

Documents are implemented first. Their bodies remain outside the ledger while a
small canonical envelope is governed.

Claims and Procedures are the target semantic families:

- a Claim governs a typed proposition about one or more subjects;
- a ClaimAttestation records support, contradiction, or uncertainty without
  silently changing the Claim;
- a Procedure governs a deterministic, bounded way of acting, including its
  contracts, pins, and track record.

A deterministic compiler may extract one or many candidate Claims from a
Document. Only selected candidates need be proposed. Whole-document governance
is therefore a useful bootstrap and source boundary, not the final ontology.

## Hot and cold paths

High-rate systems should append exhaust to a fast event stream. A slower
assembler selects, compiles, and proposes governed objects to the ledger. The
event stream records what happened; the ledger records what has been accepted.
Neither is a shadow copy of the other.

Scaling work is demand-gated. Initial cloud witnesses can be ordinary VMs with
Git and SQLite. Query accelerators or graph databases may later project accepted
state, but they remain disposable indexes.

## Donor island

The development branch temporarily retains old Procedure, workflow, query,
graph, receipt, attestation, provider, instance, and SQLite code. It exists only
to preserve deterministic semantics and frozen oracles during transplantation.
The served Playbill dependency closure cannot import it except through named
Playbill donor adapters, enforced by architecture tests.
