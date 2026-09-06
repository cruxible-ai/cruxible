# Activation handoff results — 2026-09-06

## Outcome

Clean local acceptance installs the exact verified successor in memory after publication, instead of calling full recovery to rediscover it. The ledger remains authoritative; all derived state remains recoverable. Recovery is retained for explicit refresh/reopen, lost CAS, stale epochs, missing publication, and publication failures. Full projection rebuilding remains unchanged.

Median acceptance fell **8.899 → 5.452 seconds (39%)**, and median complete write time fell **14.444 → 11.017 seconds (24%)** on the matched unprofiled workload. This is progress, not closure of the repeated-write latency priority: submission still takes about three seconds, and acceptance still prepares and rebuilds a complete projection.

## Matched timings

Median of three successive two-Claim writes in one daemon and SDK connection:

| Operation | Before | After |
|---|---:|---:|
| Prepare | 0.636 s | 0.640 s |
| Submit | 3.011 s | 2.984 s |
| Accept | 8.899 s | 5.452 s |
| Pinned readback | 0.341 s | 0.320 s |
| World refresh | 0.304 s | 0.282 s |
| Complete write loop | 14.444 s | 11.017 s |

The two later, explicitly warm writes average **14.485 → 10.887 seconds**. Three total writes/two warm writes are descriptive samples, not a tail-latency distribution. No claim of p95/p99 improvement is made.

A fresh interpreter then reopened the same instance after the daemon stopped. Recovery alone took **3.218 seconds before / 4.230 seconds after**, and both runs reproduced the exact final accepted coordinate and all 18 generations including genesis. Imports, trust-file parsing, shutdown, daemon startup and SDK connection are excluded from this separately reported recovery time. It is one reopen sample per side, after three writes, not a measured worst-case restart.

Before code was `f36620bf1a33277d02140fdad96d89f1be89e637`; after code was `24f84f749320df15fccba373725c9276d34636a5`. The same final benchmark harness drove both source worktrees with:

```sh
python docs/benchmarks/write-loop-served.py --repo /path/to/worktree \
  --population 1000 --history 8 --repeats 3 --claims-per-write 2 \
  --orphan-proposals 48 --world --no-server-profile --reopen-after \
  --output /tmp/activation-handoff.json
```

Workload: 1,000 seed Claims, eight fixture history generations, 100 Subjects, eight ClaimTypes, and 48 real unsigned orphan proposal commits. Each measured write revises one stable Claim identity and creates one observation. Fixture setup, daemon startup, connect/orient and initial World acquisition are excluded from per-write totals. Every measured loop includes lazy typed drafting, prepare, submit, status, approval challenge/sign/submit, accept, bounded full Claim readback at the receipt coordinate, and a new typed World snapshot. Human review time is excluded. No profiler or concurrent tests ran during either benchmark; filesystem/OS caches were not controlled.

Fresh keys and fixtures were generated separately, so comparisons match logical work, not signed digests or newly minted identities. All readbacks assert expected value/count, coordinate and successful workspace advertisement. This fixture's coordinator self-sources are current/uncovered under its policy: these are lawful write-loop diagnostics, not a supported-evidence customer proof. There is no configured file floor or ledger mirror. No live instance or existing private key was used for timing; both disposable fixtures were removed. Exact rows, grades, source state, setup costs and reopen results are retained in the adjacent before/after JSON files.

## Implementation and proof handoff

1. `PlaybillInstance.settle_and_activate` owns preparation through publication under an instance RLock. It does not accept an externally supplied prepared bundle for installation.
2. Settlement retains law evaluation, approval verification, exact stored-tree checking and root derivation. The handoff adds the checks previously supplied only by recovery: exact predecessor daemon-key signature, Git-proven append-only receipt namespace, canonical contiguous new receipt, and frozen receipt-to-candidate round-trip equality. It reconstructs successor principals from the new tree and root; the preparation bundle's principal registry is the parent registry and is deliberately not reused.
3. The publisher's existing atomic main CAS and ordered generation-note/serving publication remain intact. A completion callback runs while its activation lock is still held. It verifies the full coordinate, generation note and current projection before swapping one complete instance epoch. Returned activation/projection objects and the original mutable bundle cannot mutate the installed successor.
4. Refresh uses the same instance-then-activation lock order. The callback uses an explicitly already-locked recovery helper, avoiding recursive file locking. Locks are released before review reconciliation, workspace advertisement and mirror requests. Receipt coordinates remain specific to their own activation even if later advisory work observes another accepted generation.
5. Lost CAS, missing publication, stale epochs and unavailable successor proofs recover from authority. A post-CAS exception invokes recovery but remains an error if repair succeeds; failed repair propagates its error with the publication error as context. An unverified successor is never installed to mask either failure.

Internal history is a read-only borrow. The audit found no production consumer mutating borrowed records; public transports serialize them. The new successor record/principal snapshot is detached, and no full artifact tree is retained in history. Clearing process state and reopening reproduces authority from the ledger.

## Maintenance tradeoff

Previously the routine recovery also rewrote a checkpoint at the head and swept unrelated crash residue after every acceptance. The successful handoff restores the publisher's existing checkpoint stride of **50 generations**. Restart/explicit recovery may therefore replay up to 49 generations after the latest stride checkpoint, while clean writes avoid that repeated recovery cost. Missing checkpoints still fall back to genesis-rooted recovery. Unrelated orphan cleanup remains on recovery; lost-CAS targeted cleanup is unchanged.

The checkpoint-boundary test lowers the interval to two, verifies the boundary checkpoint is written, verifies a following clean write does not rewrite it, and confirms reopen reproduces the uncheckpointed suffix. The benchmark did not cross a stride-50 boundary or measure a 49-generation suffix. No worst-case latency is inferred from the three-write reopen measurement. This implements the maintainer's priority of repeated SDK latency over one-time cold cost without changing acceptance authority.

## Verification and review guide

The implementation and tests were committed together as `24f84f74`. Independent boundary/source/ownership review approved the change without remaining findings. A review-discovered missing final publication check was corrected before the final tests and benchmark.

**72 distinct targeted tests passed**, all in the isolated worktree:

- 61 tests in 116.56 seconds: `test_activation_handoff_guards.py`, `test_activation.py`, `test_recovery.py`, `test_activation_receipt_coordinate.py`, `test_review_publication_concurrency.py`, `test_review_archive_rebuild.py`, and `test_ledger_publication_worker.py`.
- 11 independently authored tests in 32.11 seconds: `test_activation_handoff.py`.
- Scoped Mypy passed for the four changed source files. Ruff check/format and diff checks passed. No full suite, golden journal corpus, or canonical-checkout tests were run.

Coverage includes clean successive served writes without recovery in SHA-1 and SHA-256 ledgers; frozen v1/v2/v3 restart parity; principal registration/revocation; detached bundle mutation and cache invalidation; signed predecessor-receipt modification/deletion/addition/rename refusal; sequence/key guards; conservative retired-content fallback; checkpoint boundaries; lost CAS and stale epochs; missing generation note/serving pointer; post-CAS successful/failed repair; actual flock exclusion and event-controlled refresh concurrency; archived review refs and receipts under later acceptance.

Read `recovery.prepared_generation_for_handoff`, then `instance.settle_and_activate`/`refresh`, then the publisher completion callback and service call-site replacement. Standard review and the detailed invariant matrix are in `activation-handoff-2026-09-06.md` and `activation-handoff-boundary-2026-09-06.md`.

## Remaining work

Next target is complete projection prebuild. Byte-dependent compilation can be reused, while proof-bearing rows still need the current accepted coordinate. This handoff does not implement that cache, eliminate repeated proposal evaluation, or change the three-second submit path. No additional architecture ruling was needed for this change.
