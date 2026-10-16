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
media errors and stray edits. `get`, `query`, `orient`, coverage and write
previews take body presence from the landing index at the read's recorded
position. They do not stat or hash body files to decide availability. Capture
metadata is parsed from verified bytes once per content id; later metadata
reads reuse it while the index says the body is available. When updating a
changed Claim, the facts worker checks its consulted bodies' file identities
and verifies those that differ from their last verified identities before
persisting sweep baselines. Missing or corrupt bodies receive sequenced damage
records even when availability or metadata was reused from memory. Preview-generated
bodies are held in memory and are available to that preview. Persistent
preflight, proposal admission, generation preparation and recovery use live
operational positions even when invoked inside a read; returning restores the
enclosing read's positions. A body found
missing or damaged is recorded, and reads report the evidence that depends on it as unavailable rather than failing;
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

Each write or Line request owns an artifact carrier across lowering, evaluation
and projection. Every parser keys it by parser, exact bytes, path and codec;
nested stages join the outer request. It retains oversized and evicted process
memo entries until that request exits, so LRU budgets do not cause repeated
parsing within a large change. Sealed models are shared; unsealable parses stay
detached. Refusals are never retained. Request memory grows with the artifacts
this request touches, and all of it is released at outer scope exit.

The write pipeline shares each Claim's sealed member: exact bytes, strict
parsed model, digests and normalized intrinsic projection facts. The bounded
cache keys all of these by path, codec and exact bytes. Changed bytes derive a
new member. Generation-dependent revisions, verdicts and explanation proofs
are compiled for the accepting coordinate. Carried rows are checked against
fresh derivation in tests; cold reconstruction remains the projection oracle.
An accepting record's proof, governance basis and approval values are normalized
once per law and accepting coordinate inside the request. Final member rows
compose these owned immutable subtrees without traversing them again; standalone
reconstruction returns detached rows. Mutable record copies take the cold path.
Evidence admission similarly carries only eligible answers, keyed by the complete
policy bytes and all derived admission inputs. A changed policy, role,
subject binding, attestation grade or derivation input asks a new question;
refused answers are evaluated again. Dependency proofs and query bindings share
only their complete scalar values, and all request derivations expire together.

A successful activation also carries the evaluator's semantic-manifest and
dependency proof tries to the exact accepted Git OID. The next write updates
those roots for its own changed paths; it does not finish deferred proof work
for the previous change. Only a winning verified publication installs them,
compiler adoption stays cold, and physical and card-free snapshots share the
same immutable semantic proof roots.

Within a Capture verification request, a run's immutable journal proof closure
is checked once at each CAS landing-log head. A recorded landing, erase or
damage invalidates that proof. A separately opened verification context checks
the retained proof bodies afresh.

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
  staged in one in-process request pack/index pair. Proposal refs and notes
  publish in authority order without a durable flush. Acceptance prepares the
  generation note objects and durably seals the complete pair before publishing
  main, importing the target's pending pack when preparation used an earlier
  request, then publishes the note ref after the winning main CAS. Changed refs
  are durable before releasing the activation lock. Retention objects are
  prepared in that pack before acceptance; their refs
  still publish in retirement order. Interrupted
  pre-acceptance publications are advisory and may need re-proposal. Commit signatures use Ed25519 SSHSIG compatible with `git verify-commit`;
  ref leases use Git's files-backend lockfile protocol. Publishing a ref,
  packed-refs or config lock consumes its pathname; cleanup closes the original
  descriptor without unlinking a successor's lock. Reads support loose
  objects and Git's delta-compressed packs. Warm workspace advertisement carries
  the tree writer's changed-child frontier and visits it only after checking the
  destination or current transfer batch has its baseline, including when a
  refresh coalesces generations. Cold attachment, missing baselines and evicted
  frontiers use full reachable-object traversal. Every 64 accepted generations the
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

### Operational journal publication

A terminal publishes its item-dependency records as one fenced append group.
The records retain their individual sequence numbers, digests and hash chain.
One reservation sidecar names the group's CAS payloads before they are written;
the sidecar is removed only after the complete group is appended and indexed.
The journal remains authoritative and its SQLite indexes are rebuildable.

Operational journal frames, payload bodies and reservation sidecars are
best-effort exhaust: they have no per-item fsync. A crash can lose recent
payloads while their authenticated journal records remain. Service reads classify
those records as unavailable; run reads return `replay_material_unavailable`,
reading inspection reports an incremental `unavailable_reading_count` and at
most the requested page size of representative `unavailable_payloads`, and affected measurement results
say `unavailable`. Recovery preserves leases and does not re-drive an effect
whose completion may have been lost. A retained admission still blocks another
admission when its binding payload is lost; an unknown event binding refuses
with `replay_material_unavailable`. An incomplete inbox stops before dependent
transitions and refuses reads and dispositions with `cruxible.inbox.unavailable`;
`next` reports its unknown volume as a repair. Restore its payload and rebuild
the inbox projection to resume. Direct body-byte reads still refuse.

Before advancing main, acceptance flushes the changed artifacts' transitive CAS
citation closure (including Capture material and promoted payloads) and namespace
parents. Linux uses one `syncfs` on the instance filesystem; other platforms
flush each not-yet-durable cited body and touched directory once, alongside the
existing Git-object flushes. A failed barrier leaves main unmoved. The process
remembers durable bodies so repeated citations do not repeat body fsyncs. A new
publication of a lost body clears that digest's durability under the barrier
lock, so the recreated inode acquires durability again. This
work follows changed members and their citations, never the stored population.
After repairing missing exhaust, rebuild its incomplete derived partition index
(or restart) to restore that partition's readings and resolution view.
