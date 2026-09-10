# Narrow C accepted-history foundation

Implementation: `6a7fb1358d3cdab1850686948acd3053ee2a8083`  
Base: `4bf2c910c418b097defb507172239279eacd0206`  
Scope: the initial C dependencies for A proposal/review and B/F typed state/dependencies.

## Result and review entry points

Proposal listing now checks accepted candidate membership through a cutoff-bound SQL
lookup instead of building a set from every accepted generation. Claim retirement
resolves the historical input digests used by its dependency graph instead of
reconstructing every historical Claim digest and loading those generations' trees.
The existing closure traversal, proposal evidence reduction and acceptance rules remain.

- `src/cruxible_core/playbill/history_index.py`: the derived SQL schema, typed locations,
  transactional update/read boundary, source reconciliation and file/schema guards.
- `src/cruxible_core/playbill/instance.py`, `accepted_history_reader`: the sole adapter
  from replay-verified history and verified generation projections into the index.
- `src/cruxible_core/storage/playbill_projection.py`, `artifact_envelopes`: typed metadata
  reads over all genesis artifacts or an exact changed-path set.
- `src/cruxible_core/service/playbill_proposals.py`, `service_list_playbill_proposals`:
  candidate membership pilot, retaining the initially requested coordinate.
- `src/cruxible_core/playbill/claim_retirement.py`, `claim_retirement_inventory`, and
  `closure.py`, `reverse_pin_closure`: historical dependency pilot. The closure accepts
  a resolver for missing historical input digests; existing map callers still work.
- `tests/test_playbill/test_history_index.py`: the new contract and work-bound checks.

## Storage and lifecycle

`projections/history.sqlite3` is disposable derived storage. It has:

1. `accepted_generations`: sequence, complete coordinate, compiler/schema binding,
   parent sequence, verified candidate/actor, and retained changeset path/digest.
   Genesis alone has no candidate, actor or changeset.
2. `artifact_versions`: identity, exact artifact digest, occurrence sequence, path,
   predecessor and projected revision. Only physically changed artifact paths create
   successor rows. Reinstatement and rename produce new occurrences; unchanged
   artifacts do not receive a row per generation.
3. `history_progress`: one instance/genesis-bound processed position, committed
   atomically with the generation and artifact rows. This is a recovery cursor,
   never an independent proof of readiness.

The index is registered with the shared derived owner. It retains verified prefix
readiness across ordinary root eviction and successful instance refresh, provided
that prefix still matches the recovered generation root, instance and compiler.
Explicit `invalidate()` discards that readiness. Unexpected file changes do likewise.

The first use builds or reconciles the index. Subsequent use after acceptance catches
up the missing suffix. This first slice is synchronous and serializes reader scopes
with publication; it does not introduce daemon scheduling or a background worker.
A reader holds a SQLite transaction and a fixed maximum accepted sequence. Updates
and processed position commit together; interrupted work rolls back. Read handles
expire when their context closes.

On process restart, persisted rows are compared against verified source inputs before
being trusted. Matching generation/artifact rows are retained without rewriting them.
The cursor alone does not skip source verification. Missing historical publications
use the frozen compiler's existing row derivation over the verified Git tree, without
moving main or publishing a historical commit as current state. Existing publications
are verified before their metadata is consumed.

The schema is checked before DML, including refusal of unexpected triggers/views.
Ordinary acquisitions check file identity and change metadata; replacements revoke
readiness. Malformed schemas require rebuilding this disposable file. No signing,
accepted-ledger, frozen compiler schema, receipt format or SDK contract changed.

## Interfaces for the parallel tracks

Use `with instance.accepted_history_reader(at=coordinate) as history:`. Omitting
`at` selects the captured recovered head. The coordinate type is the existing
`cruxible_client.contracts.projection.AcceptedCoordinate`.

| Reader method | Promise / downstream use |
| --- | --- |
| `resolve(coordinate)` | Exact OID + semantic root + generation root + compiler match within the reader's cutoff. Missing or ambiguous coordinates refuse. A can locate verified generation/candidate/actor metadata. |
| `generation(sequence)` | One generation's verified metadata; refuses sequences beyond this reader's cutoff. |
| `candidate_accepted(digest)` | Indexed existence at or before the cutoff. It does not claim a unique accepted proposal ID. |
| `artifact(digest, identity=...)` | Latest accepted occurrence of that exact artifact version within the cutoff, or `None`. Digest-only lookup refuses multiple identities. B/F can locate the exact retained version for a historical dependency. |
| `occurrences(identity)` | Accepted occurrences for that identity through the cutoff. |

Artifact locations identify historical bytes, **not current liveness or the latest
change to a path**. Deletion does not erase a historical version. Read the located
path at `generation(location.occurrence_sequence).git_oid`; do not replace a missing
exact version with today's artifact. Full path deletion/rename event history remains
a later C slice.

## Verification

Overlapping targeted runs completed successfully:

- History foundation, proposal inventory and Claim retirement: **32 passed**.
- Expanded history foundation, existing accepted-history lookup, projection and
  derived runtime: **25 passed**.
- History foundation and incremental closure: **12 passed**.
- Final history foundation (including file replacement) plus proposal status pilot:
  **10 passed**.
- `.venv/bin/mypy src`: **209 source files passed**.
- Ruff on all changed Python files and `git diff --check`: passed.

The checks exercise exact coordinate refusal, duplicate OIDs, digest ambiguity,
occurrence recurrence, rename/deletion semantics, historical cutoffs, failed-source
rollback, restart reconciliation, SQL deletion/rebuild, schema and file replacement,
expired handles, and a real accepted successor. A cold compiler-derived oracle matches
the indexed artifact metadata. Query-plan checks show indexed candidate and digest
searches. Warm lookups and proposal listing run with history walking, Git diffing and
historical projection opens disabled; the real successor test observes one diff and
one generation update, with no full historical tree reconstruction.

Repository-wide `.venv/bin/ruff check src tests` reports one unchanged import-order
violation in `tests/test_playbill/test_floor_export_service.py`. It is outside this
commit. The environment's default `uv` cache was not writable, so verification used
the repository's existing `.venv` executables. No golden journal-corpus run, full-suite
run, deployment or end-to-end latency claim is included.

## Remaining limits and subsequent slices

- Restart/source reconciliation still walks history. Missing historical projections
  can require full tree/compiler derivation; this is not a faster recovery claim.
- Existing projection binding can still count tables and verify whole files. This
  slice does not remove that earlier publication/verification cost.
- Reader scopes are serialized in this first implementation. Concurrent serving
  throughput and large-history wall-clock benchmarks remain to be measured.
- The preexisting OID membership memo and verified recovery history remain. Removing
  them would widen this pilot and create a bootstrap dependency on the derived index.
- Member-level law-evidence locators and path-change history remain later C work.
  Do not delete assessment copies or floor history readers before those locators land.
- G/H scheduling, worker recovery, authoring trajectories and independent resolution
  contracts retain their separate implementation scopes.

A and B/F can now build against these shared reader contracts. Their larger caller
migrations and table deletions remain separate work.
