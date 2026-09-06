# Code Review

## Verdict

Approved

The snapshot replaces repeated Git probes with fresh, verified object reads while keeping the evidence-to-note comparison and repair rules intact. Existing review commits are reused only when their actual Git bytes reproduce the exact evidence-derived OID and their required tree/parent objects are present with the expected types. Snapshot reads and use remain within the existing review lock; no source correctness blockers were found.

## Manual Review Priority

- Priority: P1
- Reason: This is shared integrity-sensitive review projection code, although it changes no governed bytes or acceptance law.
- Suggested Human Review Focus: Batch object framing/hash/type proofs; original versus advisory alias absence; collision-group note comparison and repair; review/approval lock ordering; request-local snapshot lifetime.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/git.py`, `src/cruxible_core/playbill/instance.py`, `src/cruxible_core/playbill/proposal_note_projection.py`, reviewed uncommitted against `89232e926e584ff62ef193a34371aaef6445c15b`, then verified unchanged in exact commit **`002428f093cb507d688c2bc39c523ffd1a3e2945`**. Combined scoped source diff SHA-256: `a0baf361422cd8c92d0f971545d31d68b308920e0bfe15bdae4a403e0ccd9450`.
- Untracked files: `tests/test_playbill/test_review_projection_snapshot.py` during review, subsequently included in the same commit, including the final replacement-ref, corrupt-note-blob, and partial-snapshot fallback tests.
- Tests examined: New snapshot tests; existing grouped proposal notes, archive rebuild, review publication concurrency, and approval note concurrency tests.
- Commands run: `git status --short`, scoped `git diff`, `git diff --stat`, `git rev-parse HEAD`, scoped diff hashing, and targeted `rg`/source reads in the isolated worktree `/private/tmp/playbill-write-loop-latency`.

No tests were repeated by this reviewer because the implementer was already running the exact relevant scopes concurrently. Implementer reports **51 distinct cases** across the combined named scopes. Results: snapshot plus review/approval concurrency **27 passed in 19.94 s**; final three added test functions across both formats where applicable **5 passed in 3.71 s**; archive/group coverage **19 passed** in the earlier mixed run. The earlier run's two failures were read-only loose-object fixture permissions, fixed by making the temporary corruption target writable before mutation; the corrected tests passed. Implementer reports Ruff/format clean on four files and mypy clean on three source files. These are attributed results, not independent reviewer executions. No full suite, golden corpus, canonical-checkout testing, live mutation, or source edits were performed by this review.

## Findings

No findings.

## Complexity Assessment

Git process count changes from per-commit/per-note probing to 128-object batches, two note listings, and remaining per-admission identity derivation. Existing alias objects no longer require commit-tree plus repeated metadata probes. The supplied copied-generation benchmark reports 449 to 48 Git subprocesses, warm reconciliation 7.503/7.637 s to 0.932/0.903 s, and exact ref/note fingerprints; this is implementer evidence, not a reviewer rerun.

The 128-object limit bounds objects per subprocess, **not bytes per subprocess or total retained memory**. The fresh snapshot retains requested object and note bodies proportional to the historical review inventory for one reconciliation. Full note-ref listings also remain proportional to their ref contents. No cache survives the request; scaling risk is transient O(total requested object/note bytes), not accumulating process-global state. A hard byte bound is not claimed.

## Architecture Assessment

The implementation stays in Git transport and the existing instance-owned derived review projection. There are no new public wires, frozen digest rules, authority stores, or network operations. The instance builds the existing evidence-derived alias index, asks Git for a fresh snapshot, materializes only missing advisory aliases through the original checked method, retains open/settled refs, and publishes complete grouped notes.

The batch parser validates requested OIDs, positional response identity, expected object type, nonnegative size, exact payload framing, trailing-byte absence, and SHA-1/SHA-256 object hashes. `--no-replace-objects` ensures hash verification sees actual addressed objects. Trees and parent commits required by the original materialization path are independently included and verified. Missing advisory aliases remain rebuildable; a missing original admission commit still reaches the existing integrity refusal.

Lock review confirms reconciliation holds the outer review projection lock across evidence/index/snapshot/read-modify-write. The approval and strict settlement note writers take that same lock before candidate locks, so their updates cannot stale the snapshot between read and use. Per-candidate approval locks still enclose evidence-derived approval rendering and writes. Distinct alias groups publish once each; updating one note ref does not invalidate snapshotted note bodies for other target OIDs. Strict activation continues its direct-read comparison.

## Test Coverage Assessment

The tests meaningfully cover both object formats; exact note bytes and explicit absent entries; fresh reads after note changes; missing/wrong-type dependencies; corrupt loose commits and note blobs with unchanged filenames; malformed, truncated, wrong-type, and trailing batch output; 128-object batching; replacement refs; existing-commit reuse; absent-note rebuilding; note tamper refusal; and uncovered snapshot entries falling back to direct reads.

Existing collision tests exercise original/advisory alias grouping, multiple admissions sharing an OID, distinct signed candidates sharing an OID, and valid incomplete note repair. Existing archive and concurrency scopes exercise settled-ref rebuilding, stale handles, and competing approval writers. Additional snapshots do not weaken canonical subset or exact-equality checks. No essential gap remains for this bounded change.

## Documentation Assessment

Docstrings accurately require a fresh snapshot under the caller's review lock and explain hash/type/dependency proofs. Optional snapshot fallback semantics are documented on `ProposalNoteIndex.publish`. No public API documentation changes are needed. Future benchmark summaries should preserve the object-count versus byte-memory distinction noted above.

## Overall Contribution

This is a cohesive optimization of a measured foreground bottleneck that preserves the ledger/evidence authority model. It trades transient snapshot storage for substantially fewer Git processes, while detecting actual object corruption rather than trusting existence or previously remembered OIDs. The implementation leaves stricter settlement checks and collision-group semantics intact.

## Open Questions

None.

## Suggested Follow-Ups

If historical note volume becomes large, stream or byte-budget snapshot batches and avoid retaining unrelated object bodies after their proof is consumed. Preserve the same outer-lock snapshot semantics and exact group-note validation when doing so; this is future scalability work, not a blocker for the current change.
