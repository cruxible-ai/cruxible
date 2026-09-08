# Citation owner/group deltas — batch 3

Implemented locally on `codex/citation-deltas`, based on batch 2
`5a8ab4fce`, at `75d64f50e7e2eabbb4446febfa5109825ee72cbd`.
Not merged, pushed or deployed. This branch includes the prior batch 2 foundation;
it has not been rebased onto later procedure integration on `playbill`.

## Result

Citation maintenance now follows changed owner paths and the union of their old
and new capture/external-source/version-span groups. It no longer enumerates all
prior citation facts or deletes and reinserts the entire citation slice on a warm
successor. SQLite schemas, logical row formats, governed pins, ledger commitments
and acceptance/publication order remain unchanged.

Raw conflicts remain indexed even when capture precedence hides them. Removing a
capture conflict can therefore reveal an untouched weaker source/span conflict.
A first-load check detects older lossy conflict slices and selects a full cold
successor reconstruction before any SQL mutation. Historical parent files remain
unchanged.

## Scope benchmark

Five-sample medians, one retired citation owner removed from a fixed three-use
group. Every other capture/source/version group is unrelated. This measures the
relation phase over already-loaded immutable prior facts/index; CAS, Git, SQL
execution, full export and initial index construction are outside the timer.
Both the initial and final logical row digests match across source versions.

| Total citation uses | Before | After | Span-key visits before → after |
|---:|---:|---:|---:|
| 100 | 11.920 ms | 0.454 ms | 100 → 6 |
| 1,000 | 115.322 ms | 0.475 ms | 1,000 → 6 |
| 10,000 | 1,310.577 ms | 0.625 ms | 10,000 → 6 |

At 10,000 uses the old citation slice requires 30,002 deletes and 29,997 inserts;
the new plan requires five deletes and no inserts. These are citation-row plan
counts, not a count of all SQL work in an acceptance. The production adapter
uses those exact primary keys and leaves unrelated rows in the copied database.

Index bootstrap in this synthetic external-source workload took about 30 ms,
318 ms and 3.55 seconds respectively. This remains a cold cost after restart,
clear or eviction. The 10,000-use root estimates 67.17 MB of encoded payloads;
the default private adapter budget is four roots/128 MiB, so a root can be
retained but not four roots of that size. Shared payloads are conservatively
counted more than once; these figures are not process RSS or exact allocation
accounting. No claim of constant cold initialization or universal O(changeset)
work is made: very large affected groups still require group work.

Reproduce with the Core environment interpreter, outside the canonical checkout:

```
python docs/benchmarks/citation-delta-scope.py \
  --baseline /private/tmp/playbill-derived-state-batch2 \
  --after /private/tmp/playbill-citation-deltas \
  --output /private/tmp/citation-scope.json
```

Raw samples, source fingerprints, parent/output parity digests and mutation
counts: [scope report](citation-deltas-scope-benchmark.json).

## Actual SDK / HTTP loop

Matched disposable Unix HTTP workload: 1,000 Claims with direct-capture backing,
two history steps, four Claims per write, eight orphan proposals, attached Git
workspace. Three writes per run; the last two advance accepted state while
reusing the daemon/client. No server profiler or disk-cache flush. Run order was
before / exploratory after / committed after / before. The first after run had
the same implementation except a 64 MiB citation cache budget, subsequently
raised to 128 MiB; this fixture fits both budgets. No same-process package switch
or live-instance rollout was used.

| Operation, repeated samples | Before median (range) | After median (range) |
|---|---:|---:|
| Prepare | 0.335 s (0.317–0.349) | 0.344 s (0.323–0.368) |
| Submit | 1.905 s (1.795–2.036) | 1.995 s (1.829–2.209) |
| Accept | 2.756 s (2.557–2.958) | 2.705 s (2.524–2.922) |
| Complete loop | 8.565 s (8.204–9.080) | 8.839 s (8.231–9.448) |

There is **no clear end-to-end latency improvement** in these overlapping,
order-sensitive observations. The initial pair looked faster after the change;
the reverse-order pair did not. Submit is not directly changed by this slice,
and its variation reinforces the need to avoid attributing every timing change
to the implementation. These are four repeated observations per arm, not p95 or
a controlled throughput result. The structural/scoped citation improvement is
established separately by parity, scope tests and the phase benchmark.

Exact intended values and accepted coordinates were checked through HTTP after
each write. Fresh-process recovery after the committed run reproduced its last
accepted coordinate and twelve generations in 3.37 seconds. The fixture's policy
reports current/uncovered Claims, as expected; these are lawful writes and
readbacks, not evidence of supported external truth.

Reproduce each arm with its repo and output paths:

```
python docs/benchmarks/write-loop-served.py \
  --repo /private/tmp/playbill-citation-deltas --population 1000 --history 2 \
  --claims-per-write 4 --orphan-proposals 8 --repeats 3 --no-server-profile \
  --reopen-after --output /private/tmp/citation-after.json
```

All four raw runs and aggregate samples:
[served report](citation-deltas-served-benchmark.json).

## Verification and remaining work

101 distinct focused cases passed across runs, including a 100-case regression
run, the additional witness/removal case, and a separately rerun extended real
Capture check. Ruff/format and mypy passed. The self-review and exhaustive manual
walkthrough are in [code review](citation-deltas-code-review-2026-09-07.md).
No independent reviewer, full suite, golden corpus, or canonical-checkout tests
were used for this slice.

The next planned slice is exact prepared-evaluation reuse into submission.
SQLite backup/logical export, explanation coordinate rebinding, broad Git
inventory verification, and large touched groups remain costs. Managed memory
configuration/accounting and other legacy adapters still require their later
batches. The latency gate remains open; this commit completes the citation
owner/group adapter, not the entire performance program.
