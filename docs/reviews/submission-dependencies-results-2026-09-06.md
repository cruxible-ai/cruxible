# Served submission dependency reuse — 2026-09-06

## Outcome

Served preflight, proposal submission and settlement reuse one exact-byte-derived semantic manifest and dependency index. Changed accepted inputs advance the existing incremental evaluator state instead of rebuilding all dependencies. Claim admission constructs effective-value views only when a governing freeze policy actually consumes them. Corroboration still reads complete accepted query facts at the exact coordinate; member laws and body/approval checks remain fresh.

This is the first bounded submission slice, not a fully incremental serving projection or complete Subject-targeted policy engine. The accepted ledger remains authoritative and no wire formats or signed commitments changed.

## Matched timings

Means of the two later warm writes; first-write and restart samples are separate:

| Operation | Before | After |
|---|---:|---:|
| Warm prepare | 0.642 s | 0.420 s |
| Warm submit | 2.940 s | 2.430 s |
| Warm accept | 5.097 s | 4.889 s |
| Warm complete loop | 10.445 s | 9.603 s |
| First prepare | 0.632 s | 0.539 s |
| First submit | 3.368 s | 2.833 s |
| First complete loop | 11.130 s | 10.409 s |
| Fresh-process reopen | 4.168 s | 3.665 s |

Warm submission improved by **17.4%**, preparation by **34.6%**, and the complete warm write loop by **8.1%**. Acceptance improved by **4.1%**; its full projection rebuilding is unchanged. Both reopens reproduced all 18 generations and the exact final coordinate. These timings do not isolate the contribution of lazy policy values from dependency reuse.

Before source: `dd5ae1215ea8701b960c713de437dbf64c8069b0`. After source: `3dfed69d0c3abef0333c05cda278d1225e20ed11`.

The same existing `docs/benchmarks/write-loop-served.py` drove before/after worktrees:

```sh
python docs/benchmarks/write-loop-served.py --repo /path/to/worktree \
  --population 1000 --history 8 --repeats 3 --claims-per-write 2 \
  --orphan-proposals 48 --world --no-server-profile --reopen-after \
  --output /tmp/submission-dependencies.json
```

Workload: 1,000 seed Claims, 100 Subjects, eight ClaimTypes, eight fixture history generations and 48 unsigned orphan proposals. Each write revises one Claim and creates one observation. The governing fixture policies have no freeze requirements; the lazy-value improvement does not imply the same benefit when freeze policies demand full value views. Three writes share one fresh daemon and SDK connection; the two later writes are the warm samples. Setup, startup, SDK connection/orientation and initial World acquisition are separately timed and excluded from write totals. Prepare occurs before submit and may fill the dependency cache even on the first write. The static projection cache still fills on first acceptance.

Per-write totals include typed lazy drafting, prepare, submit, status, approval challenge/sign/submit, acceptance, full Claim readback pinned to the receipt coordinate, and fresh typed World acquisition. Human review time is excluded. No profiler or concurrent tests ran during the measured comparisons; OS/filesystem caches are uncontrolled. Samples are descriptive, not p95/p99 estimates.

Fresh-process reopen is separately measured after daemon shutdown, excluding imports and trust-file parsing, and asserts the exact final accepted coordinate. It is one recovery sample per side, not a worst-case restart bound. All fixture keys and state are disposable; no live instance or existing key was used for timing. Readbacks verify expected values/counts, exact coordinates and successful workspace advertisement. Coordinator self-sources are current/uncovered under fixture policy: lawful latency diagnostics, not a supported-evidence customer proof. Attached workspaces have no file floor or ledger mirror. Adjacent JSON retains exact rows, grades, setup costs and recovery results.

## Design and dependency audit

The evaluator already has `EvaluatedTreeState`: semantic member hashes, a Merkle manifest and a dependency index with reverse references. Replay used its incremental updater; served submission repeatedly called the cold builder. The new optional provider is injected at the same old cold-build position, after candidate derivation, and is shared by one instance's preflight, submit and settlement. Low-level callers remain cold by default and replay's explicitly owned parent state takes precedence.

`EvaluationStateCache` derives its own state from exact semantic bytes. It accepts no cached state from callers and uses no coordinate-only or hash-only cache hit. It retains one semantic tree; on a new tree it applies the existing member/dependency updater. Input bounds are 16,384 members and 32 MiB of path-plus-body bytes, not a Python heap/RSS guarantee. Oversized inputs discard retention and use the cold path. Full defensive deepcopy on return detaches mappings and nested models. Refresh clears the cache; clean acceptance retains it for lazy advancement by the next evaluation. Older or concurrent requests can replace the retained tree, affecting speed but not exact-byte correctness.

Incremental derivation failures fall back to the cold builder so multiple malformed inputs retain the original refusal precedence. No new state is retained until complete derivation and detachment succeed. This fallback was added in response to independent review: the incremental updater can detect duplicate identity before a later malformed member that the cold builder reports first.

Claim admission previously constructed full parent/candidate effective-value maps before checking whether any policy consumed them. Only freeze rules read those values. The new lazy path preserves the exact policy result but skips those scans for empty and corroboration-only policies. On first freeze demand it constructs the same complete maps once per evaluation, preserving effective-time boundaries, unchanged Claims, cross-ClaimType freeze governance, absent/empty predicate distinctions, canonical value ordering and deduplication. Corroboration retains its independent full accepted-query path.

## Verification and review guide

Source and tests are one cohesive performance commit, `3dfed69d`. Independent review approved the final source and tests with no remaining findings; standard report: `submission-dependencies-2026-09-06.md`.

63 unique targeted tests passed in isolated worktrees: 12 evaluation-cache cases; 2 served integration cases; 30 existing authoring-submit/proposal cases; 10 existing incremental closure/manifest cases; and 9 lazy-policy/request-reuse/cross-type-freeze cases. Scoped Mypy passed for all five changed source files, Ruff check/format passed for all eight changed source/test files, and diff checks passed. No full suite, golden corpus or canonical-checkout tests ran.

Coverage includes add/revise/re-pin/retire/remove/rewind state parity against the cold builder; changed-target dependent re-resolution without parsing unrelated members; detached nested mutation; irrelevant-history exclusion; count/byte/disabled limits; concurrent derive/clear; failure-only cold refusal fallback preserving the previous cache; and two successive actual preflight→submit→settle→accept loops in SHA-1 and SHA-256 ledgers. Served integration poisons cold builders, compares exact candidate records and trees against uncached evaluation, then checks full accepted-history/coordinate reopen parity and refresh clearing. Policy tests forbid unnecessary scans for empty and real corroboration-only policies, and compare demanded freeze views against the full oracle across governing ClaimTypes.

Read the cache first, then evaluator provider selection, service/instance/preflight/settlement wiring, lazy policy demand, and differential/served tests. The cache is deliberately derivation-only: no candidate law result, approval, body availability proof or query result is accepted from it.

## Remaining work

- Full semantic-tree comparison, map copies, manifest traversal and defensive deepcopy still scale with world size. This removes repeated compilation, not every visit to an unrelated index entry.
- When a freeze applies, effective values still scan all Claims. The next precise targeting seam is a rebuildable index of actual Claim statement Subject paths, maintained over changed bytes; select complete parent/candidate Subject views while leaving arbitrary corroboration queries intact. Do not infer actual statement Subject solely from pins before their law has validated them.
- Preflight and submission still evaluate laws separately; Git proposal/review/workspace publication costs remain. Settlement also preserves its independent reproduction checks.
- Coordinate-bearing proof rows, accepted-history projection processing and full SQLite rebuilding remain unchanged. Separating stable evidence from snapshot context precedes truly targeted projection reconstruction.
