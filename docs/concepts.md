# Playbill concepts

## Authority and evidence

Accepted state is the daemon-owned Git ledger. Evidence may live elsewhere:
files, APIs, databases, event streams, or content-addressed storage. Referencing
evidence does not transfer authority over the source system to Playbill.

SQLite, graph databases, search indexes, and rendered Markdown are projections.
They may be fast and useful, but they can be discarded and rebuilt.

## CAS

Content-addressed storage maps a digest to exact bytes. CAS answers “which bytes
are these?” It does not answer “are these bytes accepted?” Storing a body is
therefore inert.

## Envelope

An envelope is the small canonical governed object recorded in the ledger. A
Document envelope contains identity, media type, body digest, links, pins,
governance scope, authority, and lifecycle. Large bodies stay in CAS.

## Proposal and candidate

A proposal records authenticated intent against an accepted base. Deterministic
compilation produces an immutable candidate plus law evidence. A refused
proposal is retained and inspectable; refusal is not a partial mutation.

Candidate coordinates are provisional. They must never be presented as accepted
coordinates.

## Review, approval, and activation

Review renders the exact candidate and readable evidence. Approval signs a
frozen challenge containing the candidate digest and coordinate context.
Activation independently verifies approvals and advances accepted state by
compare-and-set.

This separation prevents review UI, signatures, or storage from becoming hidden
mutation paths.

## Accepted coordinate

An accepted coordinate contains:

- Git OID;
- semantic root;
- generation root;
- compiler digest.

The coordinate makes reads, explanations, projections, and later proofs
portable across machines.

## Principal

A principal is a governed public-key record with authority roles such as owner,
reviewer, or recovery. The private Ed25519 key stays in client custody.
Revocation and rotation change principal state prospectively while key history
keeps older signatures verifiable.

A runtime bearer credential is not a Playbill principal. It authorizes transport
operations; the principal authorizes a governed judgment.

## Document

A Document is the first implemented semantic family. It governs exact body bytes
through a small envelope. Documents are useful sources and review units, but
whole-document governance is not the final knowledge model.

## Claim

A Claim is the planned first-class proposition family. It will identify a typed
proposition and its semantic subjects independently of any one source document.
One compiler can discover several candidate Claims in a Document, and an author
can choose to propose only one.

Subjects shrink the discovery surface: agents first search stable subject
identities and ClaimType contracts, then expand provenance or evidence on demand.
Optional recall-only tags may assist fuzzy retrieval without becoming identity
or authority.

## ClaimAttestation

An attestation is an append-only observation about a Claim: support,
contradiction, or uncertainty, with evidence and attribution. It does not
silently flip accepted Claim state. Adjudication remains a separate governed
operation.

This is the knowledge-compounding loop:

~~~text
source observation
      │
      ▼
claim or attestation proposal
      │
      ▼
review and acceptance
      │
      ▼
new evidence supports or contradicts
      │
      ▼
explanation and later adjudication
~~~

Negative evidence is first-class; an agent should contradict an existing Claim
rather than merely create an inverse adjacent concept.

## Procedure

A Procedure is the planned first-class executable semantic family. Its contract
describes required input, promised output, preconditions, capabilities, pins,
budgets, and deterministic graph. An agent can discover the contract and track
record before loading implementation detail.

The old Procedure/workflow engine remains a donor oracle until this family is
implemented under Playbill authority.

## Source bundle

A source catalog declares files to index. Client-side compilation validates
roots and emits a canonical path-free bundle with exact bytes and digests.
Checking is read-only; proposing is explicit. There are no filesystem watchers,
automatic proposals, automatic approvals, or automatic activation.

## Exhaust and the hot path

Exhaust is the complete high-rate stream of observations, actions, attempts,
logs, and intermediate results. It may be appended at 10 Hz or faster without
computing a ledger digest for every event.

The cold path selects exhaust, compiles semantic candidates, and settles
governed objects. The stream says what happened; the ledger says what has been
accepted. These are complementary roles, not two accepted-state authorities.

## Explain

Any semantic subject can request a coordinate-bound explanation of its
governance, provenance, and attestation coverage.

Explanation is read-only. Deterministic actions returned by diagnostics are
references to governed operations—an invitation to propose—not embedded
authority.
