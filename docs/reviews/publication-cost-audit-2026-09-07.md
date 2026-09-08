# Remaining publication costs — 2026-09-07

Decision: pause production performance changes for v1 and return to unfinished product loops. This is a manager recommendation, not a change to adopted roadmap priorities. No production code changed in this slice. The audit measures the completed acceptance/orientation implementation at `6ff49a45f`.

## Wall-clock evidence

A fresh SDK/Unix HTTP run used 1,000 seeded Claims, two history writes, four Claims per write and eight orphan proposals. Three complete writes and exact readbacks succeeded. The first loop is recorded separately; the following table gives medians of the two repeated observations. Counters wrap the actual synchronous acceptance worker with `perf_counter_ns`, without cProfile. The full loop including explicit orientation was 6.334–6.429s; acceptance was 2.093–2.139s.

| Nested acceptance activity | Median seconds | Interpretation |
|---|---:|---|
| Entire acceptance worker | 2.116 | Current measured baseline, not an improvement |
| Ledger Git commands, 45 calls | 1.094 | Includes necessary Git execution and I/O, not just process startup |
| Workspace Git commands, 13 calls | 0.287 | Included in advertisement below |
| Preparation | 0.566 | Includes evaluation and generation-tree writing |
| Settlement evaluation | 0.174 | Fresh law/evidence/approval-delay boundary |
| Generation-tree writing | 0.222 | Includes 0.089s checking blob presence; still handles the full mapping |
| Projection prebuild | 0.615 | Includes inventory, database update and build digest below |
| Two projection inventory reads | 0.188 | Whole physical inventories with size checks |
| Database update | 0.174 | Staged successor database plus existing validation |
| Logical digest during build | 0.141 | One export/hash; observed range 0.106–0.176s |
| Logical digest during bind verification | 0.104 | Separate verification boundary |
| Workspace advertisement | 0.421 | 0.133s review-ref reconciliation plus workspace work |

Rows overlap and must not be summed. The 58 command calls together occupy about 65% of the acceptance wall time, but this is not a claim that 65% is removable subprocess overhead. It includes actual Git work, object transfer and verification. A separate cProfile run found the same distributed shape; its timings include substantial instrumentation overhead and are not substituted for the wall timings.

## Remaining opportunities and why they are deferred

1. **Delta-based generation writing and inventory validation.** Generation construction still normalizes/hashes the full mapping, asks Git which blobs exist, and builds a temporary index. Projection preparation lists both full inventories. Carrying complete physical member metadata and limits through the verified delta could reduce these two blocks, but their combined measured cost is about 0.41s, not the entire acceptance path. This is worthwhile for larger-world scaling; it needs an end-to-end physical manifest contract, including byte limits, modes, daemon records and derivative cards.
2. **Database serialization.** The flat frozen logical digest requires visiting all logical rows; a changeset cannot simply update a SHA-256 digest of an arbitrarily edited canonical document. Streaming can reduce allocations while preserving exact canonical bytes. Avoiding the second export requires a build-to-bind verification handoff bound to exact file identity and mutation detection. The second hash is about 0.10s here. Neither should weaken independent cold verification or silently change stored digest rules.
3. **Git publication execution.** There are opportunities to reduce repeated processes and configuration writes, but the profile does not show one removable call dominating the transaction. A persistent executor, batched protocol or tree writer is a broader implementation with its own concurrency, cancellation and corruption boundaries. Assess it separately with per-command workloads and an explicit latency target.
4. **Asynchronous workspace advertisement.** Returning before the 0.42s block finishes could expose a material latency reduction, but changes when a successful response guarantees review refs and notes are ready. That is a product/publication contract decision. Without an attached workspace, the current manager still reconciles review refs, but `advertise_workspace_refs` skips the workspace fetch block; the local attached-workspace benchmark is not every deployment's cost floor.

The likely next local fixes are tenths of a second each, not another known reduction comparable to the 1.4s orientation saving in the preceding slice. The recommendation is to keep these as measured follow-ups and stop expanding the current performance pass. This is not proof that larger improvements are impossible. Full write-loop latency and larger-world scaling remain open concerns.

## Reproduction and scope

```sh
python docs/benchmarks/publication_phase_probe.py --repo /path/to/checkout \
  --population 1000 --history 2 --claims-per-write 4 --orphan-proposals 8 \
  --repeats 3 --output /tmp/publication-wall.json
```

The diagnostic launcher installs request-local phase counters in a temporary copy of the existing disposable harness. It defaults to no server profiling and emits `<output-stem>.server.phases.jsonl`. Production functions execute normally; no existing instance or key is used. `publication-cost-audit-data.json` retains exact code head, per-loop timings, readback counts, nested wall observations and the separate profile summary. Claims in this fixture are current/uncovered; success establishes accepted-write/readback behavior, not a supported-evidence customer proof.

No production tests were rerun because production code is unchanged. The disposable served runs succeeded; the committed diagnostic launcher passes Ruff and its help/argument path. No full suite, golden corpus, canonical-checkout test, merge, push or deployment occurred.
