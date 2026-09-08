# Same-call prepared evaluation reuse — 2026-09-07

Implementation `7165b40a379345f284fdd9055594ce334ac9c902` on `codex/prepared-evaluation-reuse`, based on `d6f24a7b6b7720e17acb2bc5ce8f3362639c6f64`. Local, self-reviewed; not merged, pushed or deployed.

## Result

Eligible authoring submit now invokes the proposal evaluator once rather than twice. The integration regression compares the persisted candidate with the full-evaluation result. This reuses the internal submit preflight, not an earlier SDK prepare request or an approval-delayed result.

| Operation | Before | After | Change |
|---|---:|---:|---:|
| prepare | 0.321 s | 0.324 s | +1.0% |
| submit | 1.842 s | 1.622 s | -11.9% |
| accept | 2.517 s | 2.571 s | +2.1% |
| refresh | 2.204 s | 2.228 s | +1.1% |
| total | 8.308 s | 8.001 s | -3.7% |

These are medians of four repeated, accepted-state-advancing observations per arm, from two fresh disposable daemon runs per arm in before/after/after/before order. Each run also includes a separately recorded first loop. This is a small local sample, not p95 or a production service-level claim. Submit is the directly changed stage; variation in other stages is not attributed to this patch.

## Workload and reproduction

1,000 seeded Claims, two history writes, four Claims per measured write, eight orphan proposal commits, three complete SDK-to-Unix-HTTP loops per run. No profiling instrumentation, live instances or existing credentials. Each write proceeds through prepare, submit, challenge, signing, approval, acceptance, readback and refresh. Every run and readback succeeded. Fixture claims are current/uncovered under its admission policy; this is accepted write parity, not a supported-evidence customer demonstration.

Run the same harness against the baseline and implementation checkouts:

```sh
python docs/benchmarks/write-loop-served.py --repo /path/to/checkout --population 1000 --history 2 --claims-per-write 4 --orphan-proposals 8 --repeats 3 --no-server-profile --output /tmp/result.json
```

Exact heads, run order, all phase timings, first-loop rows and readback counts are in `prepared-evaluation-benchmark.json`. The after runs' only untracked file is this slice's review documentation; production was committed before measurement. Fresh-process recovery was not benchmarked in this slice, and the handoff does not survive its submit call.

## Boundary

The instance's DerivedState registry owns the adapter. An opaque single-use scope retains the immutable evaluated tree and detached candidate/diagnostic/account models, bound to the authored operation, descriptor, accepted coordinate/compiler, actor, request, limits and timestamp. Submission revalidates receive bounds, current authority and main, then freshly replays distinct CAS observations with their original access contexts. Changed observations or bindings select full evaluation. Operational query facts, producer receipts and promotion verification select fallback when consulted; unknown body operations, writes and observation-budget exhaustion do likewise.

No ledger, wire, candidate digest, pinned-law, approval or publication-order change. No cache token crosses the API. Explicit SDK preflight and acceptance retain their current independent work.

## Validation and remaining work

127 focused passing cases across three groups (58 + 67 + 2), one golden-named test excluded. Ruff check/format, mypy for five production modules and diff whitespace checks passed. Tests include exact candidate parity, evaluation counts, expiry, invalidation, changed bindings, mutable-result isolation, fresh CAS failure and fresh writable refusal. Review is an implementation self-review; see `prepared-evaluation-code-review-2026-09-07.md`.

The end-to-end latency gate remains open. Receive validation, candidate-card filtering/snapshot construction, Git tree inventory/publication, explicit prepare and approval/acceptance are still real costs. Broader write work needs verified delta interfaces rather than more independent caches. Operational evidence needs revision bindings before it can safely participate in this reuse path. Managed-instance accounting for concurrent active scopes remains a later resource-management slice.
