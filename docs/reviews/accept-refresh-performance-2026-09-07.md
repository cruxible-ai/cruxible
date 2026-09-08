# Acceptance and orientation batch reads — 2026-09-07

Local branch `codex/accept-refresh-deltas`, implementation through `53684b5dc7adbb5eda1c87c3121ac52e86e71f5b`, baseline `8cfe40b6b4dbd4da34dbccb6c9c31d9eb35a8fd6`. Self-reviewed, not independently reviewed, merged, pushed or deployed.

## Measured result

| Operation | Before | After | Change |
|---|---:|---:|---:|
| prepare | 0.321 s | 0.269 s | -16.4% |
| submit | 1.636 s | 1.597 s | -2.4% |
| accept | 2.587 s | 2.119 s | -18.1% |
| refresh | 2.240 s | 0.830 s | -63.0% |
| total | 8.042 s | 6.336 s | -21.2% |

Four repeated accepted-state-advancing observations per arm, from two independent disposable daemon runs per arm in before/after/after/before order. Each run also records a first loop. These are small local samples, not p95 or a production SLO. Acceptance and orientation are directly changed; changes in unrelated stages are not attributed to this patch.

The complete benchmark loop includes explicit SDK `refresh()`, which returns a full orientation summary. SDK acceptance already advances a live connection's observed coordinate, and `pb.world()` reads vocabulary without orientation. Workflows that omit orientation gain the acceptance improvement, not the entire loop saving. Discovery/list/orient and connection-time orientation use the new batch input path. Public result shapes and semantics are unchanged.

## What changed

- Discovery reads Claim envelopes from the verified immutable accepted tree rather than constructing full projected fact views it discards. One request-local context shares parsed Claims, the provider catalog and group ClaimType lookups. Current verdict computation, CAS replay checks, attestations, effective times and freshness boundaries remain live. There is no new persistent cache.
- Acceptance reads the proposed tree and verifies the newly signed generation using the proven accepted parent plus Git's complete physical delta. Changed blobs are read; unchanged bytes retain their prior proof. All physical paths participate, including daemon changesets, derivative cards and unrelated files. The final full expected/stored tree equality check remains; injected payloads and unsupported file modes refuse.
- A development measurement exposed group lookups that depended on the old full-tree memo warmup. That run was interrupted and excluded. The final code uses the batch tree for those lookups, and a regression forbids extra per-group blob reads.

## Why these targets

The baseline warm orientation worker profile spent 5.885s with profiler overhead: full projected Claim views took 2.179s; 1,006 individual verdict evaluations took 3.053s, including repeated provider-tree scans. These nested times must not be added. The acceptance profile took 2.971s, with 58 subprocess calls taking 1.807s cumulatively, including two full-tree reads totaling 0.531s. Profiling selected targets; only uninstrumented served runs determine the table above. Details are in `accept-refresh-profile.json`.

## Reproduction and validation

```sh
python docs/benchmarks/write-loop-served.py --repo /path/to/checkout --population 1000 --history 2 --claims-per-write 4 --orphan-proposals 8 --repeats 3 --no-server-profile --output /tmp/result.json
```

Workload: 1,000 seeded Claims, two history writes, four Claims per measured write, eight orphan proposals; SDK over Unix HTTP through prepare, submit, challenge, signing, approval, acceptance, readback and explicit orientation refresh. No live instance or existing keys. All twelve final measured writes and exact readbacks succeeded. The fixture's Claims are current/uncovered under its admission policy; this is accepted-write parity, not a supported-evidence customer demonstration. Exact heads, dirty-path descriptions, per-phase rows and ranges are in `accept-refresh-benchmark.json`. Uncommitted after-run files were diagnostic harness/documentation only; production code was committed.

55 distinct focused cases passed across runs, with the affected batch/search cases repeated after the lookup fix. Coverage includes deterministic search/pagination, independent verdict parity, freshness and replay changes, no unused fact construction, one provider scan, no per-group Git reads, exact physical tree parity and changed-blob counts in SHA-1/SHA-256, mode refusals, injected signed payload refusal, frozen candidate versions and restart parity. Ruff, format, mypy on seven production modules and diff checks passed. No full suite, golden corpus or canonical-checkout tests. See `accept-refresh-code-review-2026-09-07.md` for the walkthrough.

## Remaining work

Orientation still enumerates all requested Claims and resolves every live Claim on a resolution-memo miss. Making that incremental across accepted generations requires exact dependency coverage and reliable mutable-input revisions. Physical tree mappings are still copied/sorted/compared; generation construction, signing, workspace advertisement and database digest/export remain real costs. The next write-latency target should come from a fresh profile of those stages rather than assuming more indexes solve them. Ledger authority, frozen laws/digests, approvals, publication order and cold recovery are preserved.
