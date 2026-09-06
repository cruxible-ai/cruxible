# Write-loop latency pass

Four reviewed fixes are committed on `codex/write-loop-latency`, based on
`9f5ebb999009b725c3eb475904e437b1a2d186dd`:

| Commit | Change | Correctness boundary |
| --- | --- | --- |
| `29989c0c` | Preserve V2 reference assertions in all six authoring response wrappers. | Explicit discriminated V1/V2 parsing; V1 bytes unchanged; required V2 assertions cannot disappear. |
| `89232e92` | Reuse invariant inputs within each proposal evaluation. | Exact path/content keys; effective-time filtering repeats per view; no cross-request authority or evaluation reuse. |
| `002428f0` | Batch fresh Git review proofs and reuse exact existing advisory commits. | Fresh object hashes/types, complete grouped notes and original/advisory aliases under the existing review lock; strict settlement remains independent. |
| `283ca306` | Reject unsigned orphan commits before deriving full parent state. | Same parent-root daemon signature requirement; passing candidates still receive full successor verification before collection. |

Ledger authority, signed accepted generations, historical digest rules and
settlement conditions are unchanged. The Git change batches current work; it
does not hide it behind background completion or a retained proof cache.

## What the trace found

Acceptance includes exact candidate/note verification, settlement construction,
projection prebuild, atomic publication, recovery, workspace advertisement and
mirror enqueue. The largest unexpected cost was inside recovery: for each
unreachable proposal commit, it loaded the accepted parent tree and built its
dependency state before discovering that the proposal had no daemon signature.
Unsigned proposal residue is retained; only already-eligible replay-valid failed
generations remain eligible for targeted collection.

The separate historical review pass also recreated known advisory commits and
ran individual Git processes for note and object reads. The new snapshot verifies
fresh bytes in batches and preserves exact refs and notes. It batches at most
128 objects per read, but this is not a hard byte/RSS limit; request-local memory
still scales with the requested historical note bytes.

## Copied program-world diagnostics

| Component | Before | After |
| --- | ---: | ---: |
| Warm 22-member preflight, profiled | 4.662 s | 2.640 s |
| Warm historical review reconciliation | 7.503 / 7.637 s | 0.932 / 0.903 s |
| Reconciliation Git subprocesses, profiled | 449 | 48 |
| Instance load, wall time | 17.651 s | 6.475 s |
| Warm refresh, profiled | 27.456 s | 8.632 s |
| Orphan cleanup within refresh, profiled | 20.872 s | 2.205 s |

The preflight reproduces exact candidate `479ab725…`; its copied payload still
omits original reference assertions, as documented in the earlier diagnosis.
Reconciliation preserves the complete ref snapshot and every note byte hash.
These are component diagnostics on a public-key-only copy of generation 26,
not a decomposition of the old live checkpoint and not production percentiles.
Profiler overhead and concurrent diagnostic activity limit direct wall-time
interpretation. No private signing custody was copied.

## Served benchmark

The reproducible [SDK/HTTP harness](benchmarks/write-loop-served.py) generates
fresh signing identities and lawful disposable history. Its paired workload is
1,000 seed Claims, eight additional history generations, 18 Claims per write
(nine revisions and nine creates), an attached Git workspace and 48 unsigned
unreferenced proposal commits. No external mirror or floor refresh is configured.

Acceptance is profiled inside the actual synchronous daemon worker. Both sides
use the same instrumentation; these are instrumented timings rather than an
unprofiled production SLA. Setup, startup and SDK connection/orientation are
reported separately. The second write advances accepted state, so it is a warm
process measurement rather than a repeated request at an unchanged coordinate.
Accepted values, exact revised identities and workspace publication are checked.
This is an accepted-write performance benchmark, not a supported-evidence proof.
A separate small diagnostic verified current but **uncovered** readback grades:
the seed vocabulary admits the fixture's direct-source contract, not the SDK
coordinator's source contract. Actual supported project-state use is verified
separately below. Acceptance remains distinct from the evidence grade.

| Stage | Before, first / warm | After, first / warm |
| --- | ---: | ---: |
| Prepare | 1.378 / 1.408 s | 0.779 / 0.778 s |
| Submit | 4.955 / 4.776 s | 3.843 / 3.407 s |
| Accept (server profiler enabled) | 61.533 / 62.166 s | 17.608 / 17.955 s |
| Readback | 3.634 / 10.168 s | 3.549 / 9.768 s |
| Whole loop, including approval and SDK refresh | 75.083 / 82.518 s | 29.111 / 35.433 s |
| Initial SDK connection/orientation, separate | 32.064 s | 9.369 s |

The full instrumented loop improves by 57–61%; acceptance by about 71%.
Readback is largely unchanged, and the second loop adds contenders and revisions,
so its readback should not be described as a repeated warm read. The before code
is clean `9f5ebb99`; the after implementation is `283ca306`, with only untracked
documentation in its worktree. Each run seeds equivalent logical data with its
own fresh keys and IDs; it does not claim identical signed generation digests.

The first baseline acceptance profile attributes 48.602 s to refresh, including
43.504 s to orphan cleanup. Afterward, refresh costs 5.858 s, while projection
prebuild remains 8.188 s and generation preparation 2.358 s. These nested
profiled times locate the remaining work; they are not independent additive rows.

Raw complete runs: [before](benchmarks/write-loop-served-before-2026-09-06.json),
[after](benchmarks/write-loop-served-after-2026-09-06.json).

## Live rollout and project-state use

The reviewed implementation `283ca306cfeebabc84becb271079a02266059ba7` was
fast-forwarded into local `playbill` and deployed through a graceful daemon
restart. Its checkout is clean, one socket listener was verified (PID 94948),
and the same three registered instances, state root and custody remain in use.
Nothing was pushed. Subsequent documentation commits do not change deployed code.

The manager prepared and submitted a meaningful eight-Claim checkpoint: four
existing task reconciliation-note replacements and four new findings/Subjects.
No implementation-status, adoption, release, ClaimType or authority-policy fields
changed. Independent review approved exact candidate
`sha256:bf54cc803e21f5c7489802f25f816d0f3b8b8f3448b121d03dddca16f771a178`.
It was accepted at `b386f6eaf126063ccfc03d2c7193a683765941f7`; all eight expected
Claims read back with supported verdicts at the receipt coordinate.

| Live stage, eight Claims | Seconds |
| --- | ---: |
| Prepare | 12.786 |
| Submit | 9.904 |
| Review retrieval | 0.458 |
| Accept | 17.398 |
| Bounded readback | 1.284 |

These are unprofiled operational observations, excluding separately timed
connection/World/prefetch costs. The prior live checkpoint had 18 Claims and a
different replacement mix, so the two live writes are not a matched benchmark.
This real supported-evidence workflow still takes too long; the synthetic
improvement does not close the performance gate. Operational receipts and the
exact checkpoint review are retained in local `docs/world-model/`.

The [exact-workload follow-up](benchmarks/exact-project-checkpoint-diagnosis-2026-09-06.md)
preserved the pre-activation generation in a public-key-only copy and retrieved
the complete V2 intent, including all 12 original reference assertions. Both
preflights reproduced the exact 12-member candidate above. Profiled preflight
took 3.672 s cold / 2.318 s warm. Separately, the first historical intent lookup
took **11.741 s unprofiled**, versus **0.109 s warm**: it validates 173 events
across 24 retained intent streams. This identifies a substantial first-write
after restart cost absent from the small-history synthetic authoring fixture.
These independent component timings are not additive to the live request or a
claim that every one of its seconds has been assigned.

## Verification and review guide

Read the response-contract fix first, then the evaluator, Git reconciliation and
recovery changes. The last two remove different historical costs: ref/note
publication versus inspection of unreachable commits during recovery.

- Wire/version scope: 26 tests passed, including public round trips and existing
  stale-reference checks. The independent authoring catalog was updated under
  `2026-09-06:write-loop-latency`; historical record digests were not changed.
- Evaluator scope: 53 tests passed in the initial named run; all three new tests
  passed after the removal-only refusal assertion was strengthened. A reviewer
  independently ran three QueryDefinition consumer tests.
- Git proof/reconciliation scope: 51 distinct affected tests passed, including
  both object formats, corruption, missing objects, grouping, archives and
  publication/approval concurrency.
- Recovery scope: 28 tests passed, including unsigned refusal, signed-but-invalid
  retention, eligible orphan collection and activation crash boundaries.
- Served inventory, primitives, publication event-loop and clock-taxonomy scope:
  31 tests passed. Scoped Ruff, formatting, Mypy and whitespace checks passed.

All implementation tests used isolated worktrees and state roots. No canonical
checkout tests, full suite or journal golden corpus was run. Counts above are
separate scopes and should not be summed as a unique total.

Independent reviews:
[response versions](reviews/intent-response-versions-2026-09-06.md),
[evaluation reuse](reviews/evaluation-request-reuse-2026-09-06.md),
[Git snapshots](reviews/review-projection-snapshot-2026-09-06.md),
[orphan prefilter](reviews/recovery-prefilter-2026-09-06.md),
[integrated range](reviews/write-loop-integration-2026-09-06.md).

## Remaining work

Keep performance as the gate before broader procedure features. The next cold
write target is historical intent-index initialization: preserve readiness or
reduce work needed to discover current intent state without trusting derivatives
as authority. Steady-state residual costs include full accepted checkpoint-record parsing, full projection prebuild,
repeated evaluation across protocol stages, and broad Claim readback/resolution.
The fresh Git index still derives each historical review identity individually.

Do not add a full historical-record model cache casually: nested models contain
mutable containers, and privately cached/deep-copied records would duplicate
history already retained by the instance. A later design needs measured retention
and copy costs, exact-byte validation, bounded capacity and isolation from caller
mutation. Reusing a verified prefix safely may be a better ownership model.

The subsequent product sequence remains procedure outputs/proposals/provenance,
delegated proposal bundles, Line freshness/repair, then the KEV demonstration.
