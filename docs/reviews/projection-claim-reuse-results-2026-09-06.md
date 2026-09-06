# Claim compilation reuse — 2026-09-06

## Outcome

The served activation prebuild reuses validated, immutable compilation results for byte-identical Claims across generations. It still rebuilds coordinate-dependent proof rows and the complete SQLite projection. The signed ledger and accepted tree remain authority; clearing the cache or restarting reproduces the same output.

## Matched timings

Means of the two later warm writes, reported separately from the first cache-filling write:

| Operation | Before | After |
|---|---:|---:|
| Warm prepare | 0.642 s | 0.641 s |
| Warm submit | 2.910 s | 2.894 s |
| Warm accept | 5.586 s | 5.058 s |
| Warm complete loop | 10.902 s | 10.363 s |
| First accept | 5.318 s | 5.482 s |
| First complete loop | 10.871 s | 11.031 s |
| Fresh-process reopen | 4.178 s | 4.145 s |

Warm acceptance improved by **9.5%**; the full warm write loop improved by **4.9%**. The initial cache-filling acceptance adds about 0.16 seconds in this sample. This is a useful but modest gain; repeated writes remain above ten seconds. Recovery runs without the static cache and both reopens reproduced all 18 generations and the exact final coordinate. The single reopen sample is not a worst-case restart measurement.

Before code: `c85a99bbd00a909c82790faa2ab4e631502c5e0b`. After code: `5a0b81b2b08cef50ed35a13e91a4f6646178847d`. The final existing `write-loop-served.py` harness drove both isolated worktrees:

```sh
python docs/benchmarks/write-loop-served.py --repo /path/to/worktree \
  --population 1000 --history 8 --repeats 3 --claims-per-write 2 \
  --orphan-proposals 48 --world --no-server-profile --reopen-after \
  --output /tmp/projection-reuse.json
```

Workload: 1,000 seed Claims, eight fixture history generations, 100 Subjects, eight ClaimTypes, and 48 unsigned orphan proposal commits. Each measured write revises one stable Claim and creates one observation. One fresh daemon and SDK connection serve all three writes. The first acceptance fills the process-local compilation cache; the next two reuse it despite advancing the accepted coordinate. No profiler or concurrent tests ran during the measured comparisons. OS/filesystem caches are uncontrolled. These are descriptive samples, not p95/p99 estimates.

Per-write totals include lazy typed drafting, prepare, submit, status, approval challenge/sign/submit, acceptance, full Claim readback pinned to the receipt coordinate, and a fresh typed World snapshot. Fixture setup, daemon startup, connection/orientation, initial World acquisition and human review time are excluded. Reopen is separately timed in a fresh interpreter after daemon shutdown, excluding imports and trust-file parsing, and checks the exact final accepted coordinate.

Fixtures and keys are disposable and separately generated. Readbacks assert expected values/counts, exact coordinates and successful workspace advertisement. Coordinator self-sources are current/uncovered under fixture policy: this is a lawful latency diagnostic, not a supported-evidence customer proof. The attached workspace has no file floor or ledger mirror. No live instance or existing private key was used for timing. Adjacent before/after JSON files retain full phase timings, grades, setup and recovery observations.

## Implementation

- Each instance owns a bounded in-process Claim compilation cache, shared by its activation publishers. A clean verified successor handoff retains it; explicit recovery clears it. Direct reference assembly and no-coordinate parsing remain uncached by default.
- Keys bind compiler digest, codec, canonical path and exact complete input bytes. A hash collision cannot authorize a cache hit. A miss uses the existing strict Claim parser and normalization path.
- Entries retain immutable primitive metadata and normalized JSON bytes for identity, statement, backing, lifecycle and source-mapping facts. Every hit materializes fresh containers, so a consumer cannot mutate future output. The narrow private `model_construct` receives only snapshots of already validated local facts; there is no persisted or wire cache ingestion.
- Envelope revisions, historical law lookup, explanations, verdicts, evidence basis, attestation coverage and proof coordinates remain freshly computed. Outer JSON validation, duplicate detection, global pins/registry checks, CAS-dependent artifacts, exact Git tree verification and publication proofs remain active.
- The default limits are 4,096 entries and 32 MiB of accounted encoded payload, not a Python heap/RSS guarantee. Oversized or evicted entries compile normally. Complete scans beyond cache capacity can thrash; the measured gain does not imply equal gains for arbitrarily large worlds.

## Verification and review guide

Source and tests are one logical commit, `5a0b81b2`. Independent source, ownership and integration-test review approved the change with no findings; the standard review is `projection-claim-reuse-2026-09-06.md`.

34 distinct targeted tests passed in isolated worktrees: 15 cache unit cases, 12 existing Claim projection/activation-handoff cases, and 7 new integration cases. Scoped Mypy passed for all five changed source files; Ruff check/format and diff checks passed. No full suite, golden journal corpus, or canonical-checkout tests ran.

Tests cover exact key separation, nested mutation isolation, LRU/count/byte limits, oversized/disabled retention, concurrent operations, warm parsing avoidance, full parsed-row/SQLite logical export/digest parity, actual served cache retention through acceptance, fresh generation proof coordinates, retirement misses and revisions, corrupted Claim/history refusal parity, registry rejection, and recovery clearing. The initial integration oracle incorrectly compared registry objects by identity; it was corrected before the passing verification to compare rows/request and complete SQLite exports/digests.

Review in this order: private immutable cache representation; `_claim_static_facts` extraction and hit/miss handling; assembler/publisher injection; instance lifetime; then integration parity tests.

## Remaining work

Projection construction still scales with total accepted world size. Dynamic Claim facts, accepted ChangeSet history parsing, serialization, SQLite construction and publication remain on acceptance. Submission's repeated evaluation cost is untouched. This pass does not introduce a second authority store, an atomic cache-plus-ledger writer, persisted cache state, or a fully incremental projection builder.
