# State-update loop: branch implementation and measurements

Implemented on `codex/state-loop-design`, based on `f8ce3336`. The primary
`playbill` branch and running daemon are unchanged. This report records engineering
measurements, not newly accepted Claims in the program world.

## Results

Same program data at accepted generation 26, including the same two projection
blocks and 120 backing references. The isolated instance copy includes the ledger,
CAS, operational evidence and public verification keys; no production signing keys.
Instance recovery precedes the timers. These are service measurements, excluding
HTTP and SDK orchestration, not replacements for earlier complete live-loop times.

| Operation | Before | After | Verification |
|---|---:|---:|---|
| Check both blocks, cold lineage index | 47.797 s | 1.438 s | Complete response equality |
| Check both blocks, warm lineage index | 48.520 s | 0.047 s | Complete response equality |
| Resolve 120 backing references, warm | 16.025 s | 0.086 s | Exact ordered backings equal |
| Read 41 complete Claim views, warm | 5.460 s | 0.395 s | Complete ordered views equal |
| Repeat preflight, 162 synthetic Claims, median | 0.818 s | 0.445 s | Complete computed preflight equal |
| Prepare and submit, 162 synthetic Claims, median | 9.115 s | 9.198 s | No demonstrated overall improvement |

The first backing-resolution comparison was 16.919 → 0.086 s; the first full-view
comparison was 5.603 → 0.401 s. Each batch follows its individual-read comparison,
so these are not independent cold-process comparisons. Preflight figures use a
different synthetic fixture and cannot be combined with the program-data timings.
Scoped tests sometimes ran concurrently: these samples establish magnitude, not
percentiles or precise deployment expectations.

`pb.accept()` now makes one daemon activation call and does zero local floor
exports or projection checks. `pb.refresh_workspace(at=receipt.accepted_coordinate)`
refreshes the local floor explicitly at that coordinate. The existing `activate()`
convenience method still performs refresh. No new measured daemon-acceptance time
is claimed; its mandatory publication/verification/advertisement costs remain.

## Changes and review guide

1. `62a38353`: additive SDK acceptance and explicit, coordinate-pinned floor refresh.
   A floor receipt reports its actual coordinate. `4803eaf2` also fixes a concurrent
   activation race: the acceptance receipt names its own accepted generation,
   even if a subsequent generation lands before advertisement completes.
2. `86f87611`: bounded deterministic-lowering reuse for eligible self-source Claims
   and pure definitions. Candidate evaluation, mutable evidence checks and
   authorization remain fresh. Working selections, existing captures and procedures
   are excluded. Missing generated CAS bodies rebuild; corrupt bodies still refuse.
3. `b2f86301`: coordinate-pinned bulk Claim reads and `World.prefetch`, preserving
   contenders, bounded pagination and complete-cache installation. Full-view batches
   share projection binding and accepted-artifact reads. A separate backing-only
   endpoint reads identity, statement digest and lifecycle without admission work.
4. `aafd33c6`: batch accepted-history reads by generation and retain bounded Claim
   lineage indexes. Entries extend over new generations, rebuild after eviction,
   and cannot leak future successors into an earlier coordinate. Failed batch reads
   fall back per path so typed marker refusals retain their ordering.
5. `e4c4dc08`: projection repinning uses backing batches of at most 256 references,
   checks returned coordinate and exact identity order, and leaves authored prose
   unchanged. No renderer is introduced.

The lineage index is weakly held per instance and bounded to 512 paths, 4,096
artifact nodes and 64 MiB retained source-byte weight. The lowering cache is bounded
to four entries and a 32 MiB serialized/tree estimate per instance. Neither bound
claims to measure Python heap use. Both are disposable derivatives of their
validated inputs, never substitutes for ledger authority. Mutable observations
and evidence verdicts do not enter the lineage cache.

## Validation

Independent reviews approved activation, bulk reads, conservative lowering reuse,
and lineage/backing integration after identified issues were corrected. A typed
coordinate-model mismatch, a legitimate `.json` subject-ID normalization issue,
and malformed historical-read refusal ordering have regression coverage.

Named scopes (counts are per scope, not an aggregate test total):

- Activation/refresh SDK and real concurrent-generation receipt scopes: 57 passed.
- Bulk service, HTTP, existing World and new prefetch scopes: 47 distinct passed.
- Prepared lowering, existing preflight and change-set intent scopes: 44 passed.
- Final lineage index, existing sync service and artifact backing scopes: 12 passed.
- Existing client block-sync scope: 24 passed; new batch adapter scope: 3 passed.
- Served-surface delegation and registration inventory checks: 2 passed after the
  single coordinated snapshot update.
- Core Mypy passed in 188 source files; targeted final SDK/index Mypy passed in
  five source files. Scoped Ruff and whitespace checks passed.

No full suite or golden journal corpus ran; tests used the isolated worktree.
The served-surface inventory update is metadata verification, not a journal-corpus
rerun. No push, live daemon restart, or program-instance activation occurred.

## Served-surface succession

`2026-09-05:state-loop-bulk-reads` records this task's maintainer authorization to
implement the presented before/after design. One branch-local snapshot update
covers the two additive read endpoints/runtime methods and the additive floor
coordinate in the activation result. It does not change acceptance authority or
ratify unrelated in-flight branches. Future integration with Fable's branches
must reconcile their independent surface movements at the combined head.

## Remaining work

- Compact markers remain a separate retention/wire-format slice. See
  [the manifest follow-up](projection-manifest-follow-up.md): authored manifests
  need authoritative retention and every parser/export surface must resolve them.
  No disposable-only manifest or partially supported marker format was introduced.
- Pending-intent indexing remains deferred until its journal-integrity/recovery
  contract is explicit. Current historical corruption detection is preserved.
- The complete submit loop has not materially improved. It needs a separate
  breakdown beyond lowering; this report does not imply full preflight reuse.
- Full sync retains a per-backing linear accepted-coordinate membership lookup
  inside `blob_at`; the expensive historical blob/parsing product has been removed.
  Indexes are in-process and rebuild after restart, not persisted across daemons.
- A general revision/evidence convenience operation and uniform SDK reference
  representations remain follow-ups. The new primitives remove request fan-out;
  they do not make every management script unnecessary.

## Evidence

- [Machine-readable timing summary](state-loop-benchmarks-2026-09-05.json)
- [Independent integration review](state-loop-review.md)
- [Prepared-computation measurements](performance-prepared-lowering-2026-09-05.md)
- [Batch API usage](state-loop-batch-reads.md)

Local diagnostic scripts and full outputs remain at
`/private/tmp/playbill-loop-benchmark.py`, `/private/tmp/playbill-loop-{baseline,final}.json`,
`/private/tmp/playbill-backing-benchmark.py`, `/private/tmp/playbill-backing-benchmark.json`,
`/private/tmp/benchmark-prepared-lowering.py` and `/private/tmp/benchmark-prepared-submit.py`.
The copied instance is `/private/tmp/playbill-state-loop-instance`. These scratch
files are operational benchmark evidence, not release assets or authority.
