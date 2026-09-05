# Performance pass: authoring and Claim reads

Status: two bounded fixes on `codex/performance-hotpaths`, based on
`da5868c4722b3439726e05955a621799bb282b4d`. No daemon deployment or push is
part of these measurements. This is an engineering report, not a governed
projection or a substitute for original measurements.

## Results

| Workload | Before | After | Evidence |
| --- | ---: | ---: | --- |
| Lower 24 new Claims against 1,627 accepted Claims (28 authored members) | 15.417 s | 0.416 s | Exact complete output-tree fingerprint matches; about 37× faster |
| Lower 162 new Claims against 1,465 accepted Claims (190 authored members) | 119.020 s | 22.820 s | Exact complete output-tree fingerprint matches; about 5.2× faster |
| Twenty single-Claim reads, two Claims across four generations | 40 historical law parses | 2 historical law parses | Fresh admission evaluations still run twenty times |

These are local, single-run, unprofiled lowering measurements using copies of
real program-instance inputs. Some other verification work ran concurrently;
these are diagnostic measurements, not production SLOs. Lowering excludes
transport, preparation, submission, activation, and signature verification.
The read fixture took 1.543 s before and 1.401 s after; its small time difference
does not establish a production read-loop speedup. The deterministic reduction
in parsing is stronger evidence.

The 24-Claim baseline profile made 78,648 `parse_claim` calls in lowering:
two population scans per new Claim, including already-staged siblings.
The index replaces this with one lazy initial scan and changed-path updates.
Lookup still filters live contenders and sorts by UTF-8 path; retirements,
revisions, disposition statement digests, and succession closure writes remain
visible to subsequent members. Nested succession reauthoring retains its own
separate lowering tree.

Single-Claim reads reuse the existing bounded, coordinate-specific history
memo directly. Bulk callers receive an owned mapping. Admission accounts,
evaluation times, and source-dependent evaluation remain fresh.

## Review guide

- `src/cruxible_core/service/playbill_claims.py`: direct history-evidence lookup
  and mapping ownership; commit `bab87ea0b5c3620c9491876176425c58405d3b69`.
- `src/cruxible_core/playbill/authoring/lowering.py`: `_ClaimPredicateIndex`,
  shared predicate/slot lookup, and advancing after staged closure writes.
- `tests/test_playbill/test_claim_read_history_reuse.py`: historical coordinate
  isolation, refresh invalidation, owned mappings, fresh accounts, and no
  whole-index copy for singleton reads.
- `tests/test_playbill/test_authoring_claim_index.py`: lazy parsing and exact
  update counts; differential comparison of complete lowered output and typed
  refusal details against uncached lookup; revisions, retirement, and ClaimType
  succession with consumed reauthor siblings.

Independent review found no blockers. Review inspected both production changes,
new tests, and relevant staging/history/admission paths; execution evidence is
from the implementation and test agents, not a second independent test run.

## Verification

All tests ran in the isolated worktree, using its source and SDK on `PYTHONPATH`.
No canonical-checkout tests, full suite, golden journal corpus, or live instance
mutations were used.

- Claim-index regressions: 8 passed.
- Change-set intents, disposition slots, Claim flows, wire succession, and wire
  succession boundary: 68 passed.
- Claim-read initial four-file scope: 34 passed. After the singleton lookup
  refinement, three owned tests and the existing receipted single-read test:
  4 passed.
- ClaimType migrations in change sets: 20 passed.
- Ruff check and formatting check: both production files and both new test
  files pass. `git diff --check` passes.
- `uv run mypy src packages/cruxible-client/src`: no issues in 282 source files.

## Remaining priorities

| Priority | Work | Why |
| --- | --- | --- |
| 1 | Validated authoring-history reuse and incremental lookup/folding | `_active_by_fingerprint` validates every stream; a cold projection-registration fold does the same. The copied history has 161 events, about 142 MB. In the optimized large lowering profile, history reconstruction accounts for 59.2 of 61.2 profiled seconds. Preserve canonical bytes, event-chain validation, actor isolation, duplicate detection, crash recovery, and visibility of changed streams. |
| 2 | Measure preparation/submission overlap and reuse safe preflight work | This pass times lowering only. A full operation can repeat lowering and validation across stages; reuse must bind all relevant accepted coordinates and mutable dependencies. |
| 3 | Bulk Claim materialization and request-local parsing reuse | `ProjectionHandle.list_claims` uses 1 + 2N SQLite statements. Query fact construction reparses Claims. Measured end-to-end benefit is still pending. |
| 4 | Batched typed reads and projection repinning | Repinning issues sequential Claim reads. First remove underlying duplicate work, then measure transport/batching gains. |
| 5 | Skip excluded search kinds before collecting rows | `_claim_rows` checks the kind filter inside the expensive loop. A small filtered-search improvement, not the primary write bottleneck. |
| Separate surface task | Compact projection markers | Existing markers embed complete backing manifests. Preserve full cryptographic commitments and historical verification while reducing visible metadata; this is not a hash-truncation performance fix. |

History work should define integrity and invalidation semantics before adding a
cache. The existing publication-registration memo already helps unchanged warm
history, but a new authoring event invalidates its whole fold. A durable index
must remain rebuildable and must not become an alternate authority. Profiles
currently point to repeated work, so they do not justify moving this work to
Rust yet.

## Local measurement artifacts

The read-only source export is `/private/tmp/playbill-performance-baseline`;
the patched worktree is `/private/tmp/playbill-performance-worktree`. The
driver is `/private/tmp/playbill-profile-hotpaths.py`. Original JSON timing
records and cProfile outputs are in `/private/tmp/playbill-performance-data`.
The data copy contains a non-hardlinked bare ledger and relevant operational
stores; no signing credentials were copied. Lowering may write only scratch CAS
bodies. The copied data was not used to claim a fresh ledger-signature audit.

Run the driver with either source tree's `src` and
`packages/cruxible-client/src` on `PYTHONPATH`, using the existing environment's
Python. Commands: `lower --case 24 --label NAME`,
`lower --case 162 --label NAME`, or `history --label NAME`; add `--profile`
for cProfile output. Do not compare profiled and unprofiled elapsed times.

The comparison fingerprint hashes sorted paths, lengths, and exact body bytes;
it is a test oracle, not a new governed encoding. Matching values:

- 24 Claims: `64892c62c4aeaad97f1f4bfbe3d3dffba37a3305cec60a966bc06e91e92303d6`.
- 162 Claims: `e1a9ad863a0ea14ee6e0212fe7697353667424beda219393d882c8a00b22700e`.
