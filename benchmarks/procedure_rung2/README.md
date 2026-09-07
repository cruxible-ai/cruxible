# Procedure rung-2 proposal delivery benchmark

Phase timings for one manual Line occurrence of a graph-v4 Source Procedure
whose shaped row flows into a `propose_change_set` terminal, measured before
and after the proposal bridge landed. Not collected by pytest; run explicitly.

```bash
ROOT=/some/scratch
uv run python benchmarks/procedure_rung2/benchmark.py run --root "$ROOT/after" --out "$ROOT/after.json"
uv run python benchmarks/procedure_rung2/benchmark.py report "$ROOT/after.json"
uv run python benchmarks/procedure_rung2/benchmark.py compare "$ROOT/before.json" "$ROOT/after.json"
```

## Workload

| Property | Value |
|---|---|
| Procedure | `source(workspace.file)` → `project` → `propose_change_set`, graph v4, `terminal_capability=2` |
| Line | manual trigger, `requested_terminal_rung=2`, one live rung-2 ProcedureMandate over `claims` |
| Logical input | one fixed advisory document; the Provider subprocess is the byte-faithful stub the Source tests use |
| Claim populations | 0 and 40 accepted foreign-source work-item Claims (one generation each) |
| Retained history | 0 and 30 extra single-Subject generations |
| Terminal items | 1 and 8 (qualified so they are distinct Claims) |
| Samples | one cold occurrence in a fresh process, then five warm occurrences one daemon-minute apart |

Phases are measured by wrapping the exact functions at the seams: `admission`
(service work before the executor), `execute` (the executor), `source_read`,
`provider`, `item_closure`, `lowering` (shared authoring), `proposal_submit`
(the proposal door), `receipt_persist` (the terminal's journal records), then
the manager-side `manager_inspect`, `manager_activate`, `manager_readback`
performed after each run that produced a proposal. Counters record Git
invocations, blobs read, journal appends and bytes, proposal submits,
lowerings, and peak RSS.

Warm samples share one process and one instance; each is a new occurrence, so
the Line's journal partition and the proposal evidence store grow with every
sample. Medians over five warm samples are reported; no p95 is claimed.

## Baseline (base commit `097d1755`, pristine worktree)

Every cell refused `terminal_not_available` at the terminal; the proposal and
manager phases were unavailable. Results: `results/baseline-097d1755.json` (run from a pristine worktree at the base commit, benchmark script copied in). `line_total` rows and cold counters:

| cell | gens | status | phase | cold s | warm median s | warm max s | calls |
|---|---:|---|---|---:|---:|---:|---:|
| p0-h0-i1 | 4 | node_refused/terminal_not_available | line_total | 0.1338 | 0.2915 | 0.4996 | 1 |
| p0-h0-i1 | 4 | node_refused/terminal_not_available | counters (cold) | git=2 blobs=26 journal=14 (24191 B) | submits=0 lowerings=0 | rss=138 MiB | - |
| p0-h0-i8 | 4 | node_refused/terminal_not_available | line_total | 0.1924 | 0.6677 | 0.8420 | 1 |
| p0-h0-i8 | 4 | node_refused/terminal_not_available | counters (cold) | git=2 blobs=26 journal=21 (39171 B) | submits=0 lowerings=0 | rss=131 MiB | - |
| p0-h30-i1 | 34 | node_refused/terminal_not_available | line_total | 0.1458 | 0.2974 | 0.4416 | 1 |
| p0-h30-i1 | 34 | node_refused/terminal_not_available | counters (cold) | git=2 blobs=116 journal=14 (24191 B) | submits=0 lowerings=0 | rss=131 MiB | - |
| p0-h30-i8 | 34 | node_refused/terminal_not_available | line_total | 0.2063 | 0.6064 | 0.9140 | 1 |
| p0-h30-i8 | 34 | node_refused/terminal_not_available | counters (cold) | git=2 blobs=116 journal=21 (39171 B) | submits=0 lowerings=0 | rss=126 MiB | - |
| p40-h0-i1 | 45 | node_refused/terminal_not_available | line_total | 0.1453 | 0.2689 | 0.4091 | 1 |
| p40-h0-i1 | 45 | node_refused/terminal_not_available | counters (cold) | git=2 blobs=231 journal=14 (24191 B) | submits=0 lowerings=0 | rss=147 MiB | - |
| p40-h0-i8 | 45 | node_refused/terminal_not_available | line_total | 0.1935 | 0.5166 | 0.8237 | 1 |
| p40-h0-i8 | 45 | node_refused/terminal_not_available | counters (cold) | git=2 blobs=231 journal=21 (39171 B) | submits=0 lowerings=0 | rss=149 MiB | - |
| p40-h30-i1 | 75 | node_refused/terminal_not_available | line_total | 0.1606 | 0.2719 | 0.5243 | 1 |
| p40-h30-i1 | 75 | node_refused/terminal_not_available | counters (cold) | git=2 blobs=321 journal=14 (24191 B) | submits=0 lowerings=0 | rss=155 MiB | - |
| p40-h30-i8 | 75 | node_refused/terminal_not_available | line_total | 0.2275 | 0.5378 | 0.8030 | 1 |
| p40-h30-i8 | 75 | node_refused/terminal_not_available | counters (cold) | git=2 blobs=321 journal=21 (39171 B) | submits=0 lowerings=0 | rss=152 MiB | - |

All other baseline phases (admission, execute, source_read, provider, item_closure) are in the JSON and unchanged by the bridge; the after table below repeats the comparable rows.

## After (this branch)

Results: `results/after-90dd2781.json`, run at commit `90dd2781` in the branch
worktree. Every cold sample and every warm sample delivered a proposal
(`submits=1` per sample); the last warm sample of each cell also measured
manager activation and readback once, because activating earlier would occupy
the Claim slot and turn the remaining samples into disposition refusals rather
than deliveries. Seconds; warm columns are medians and maxima over five
samples.

| cell | phase | before cold | before warm median | after cold | after warm median | after warm max |
|---|---|---:|---:|---:|---:|---:|
| p0-h0-i1 | line_total | 0.1338 | 0.2915 | 1.0482 | 1.3499 | 1.6491 |
| p0-h0-i1 | lowering | unavailable | unavailable | 0.0106 | 0.0254 | 0.0369 |
| p0-h0-i1 | proposal_submit | unavailable | unavailable | 0.8757 | 0.9133 | 1.0798 |
| p0-h0-i1 | receipt_persist | 0.0056 | 0.0172 | 0.0138 | 0.0371 | 0.0539 |
| p0-h0-i1 | manager_activate | unavailable | unavailable | unavailable | 1.1393 | 1.1393 |
| p0-h0-i1 | manager_readback | unavailable | unavailable | unavailable | 0.0408 | 0.0408 |
| p0-h0-i1 | after cold counters (succeeded/ok) | - | - | git=31 blobs=78 journal=15 (31663 B) | submits=1 lowerings=1 rss=131 MiB | - |
| p0-h0-i8 | line_total | 0.1924 | 0.6677 | 1.2203 | 1.9429 | 2.6937 |
| p0-h0-i8 | lowering | unavailable | unavailable | 0.0941 | 0.2649 | 0.4090 |
| p0-h0-i8 | proposal_submit | unavailable | unavailable | 0.8952 | 1.0467 | 1.2703 |
| p0-h0-i8 | receipt_persist | 0.0074 | 0.0258 | 0.0177 | 0.0534 | 0.1172 |
| p0-h0-i8 | manager_activate | unavailable | unavailable | unavailable | 1.2644 | 1.2644 |
| p0-h0-i8 | manager_readback | unavailable | unavailable | unavailable | 0.0358 | 0.0358 |
| p0-h0-i8 | after cold counters (succeeded/ok) | - | - | git=31 blobs=78 journal=22 (59342 B) | submits=1 lowerings=1 rss=132 MiB | - |
| p0-h30-i1 | line_total | 0.1458 | 0.2974 | 2.1207 | 2.3713 | 2.6749 |
| p0-h30-i1 | lowering | unavailable | unavailable | 0.0107 | 0.0259 | 0.0360 |
| p0-h30-i1 | proposal_submit | unavailable | unavailable | 1.9346 | 1.9943 | 2.1214 |
| p0-h30-i1 | receipt_persist | 0.0059 | 0.0174 | 0.0135 | 0.0372 | 0.0605 |
| p0-h30-i1 | manager_activate | unavailable | unavailable | unavailable | 1.2975 | 1.2975 |
| p0-h30-i1 | manager_readback | unavailable | unavailable | unavailable | 0.0480 | 0.0480 |
| p0-h30-i1 | after cold counters (succeeded/ok) | - | - | git=91 blobs=348 journal=15 (31664 B) | submits=1 lowerings=1 rss=131 MiB | - |
| p0-h30-i8 | line_total | 0.2063 | 0.6064 | 2.2770 | 3.0591 | 3.5097 |
| p0-h30-i8 | lowering | unavailable | unavailable | 0.0908 | 0.2590 | 0.3832 |
| p0-h30-i8 | proposal_submit | unavailable | unavailable | 1.9555 | 2.1575 | 2.3666 |
| p0-h30-i8 | receipt_persist | 0.0075 | 0.0246 | 0.0179 | 0.0599 | 0.0773 |
| p0-h30-i8 | manager_activate | unavailable | unavailable | unavailable | 1.4015 | 1.4015 |
| p0-h30-i8 | manager_readback | unavailable | unavailable | unavailable | 0.0507 | 0.0507 |
| p0-h30-i8 | after cold counters (succeeded/ok) | - | - | git=91 blobs=348 journal=22 (59342 B) | submits=1 lowerings=1 rss=134 MiB | - |
| p40-h0-i1 | line_total | 0.1453 | 0.2689 | 2.5250 | 2.8249 | 3.0799 |
| p40-h0-i1 | lowering | unavailable | unavailable | 0.0267 | 0.0339 | 0.0444 |
| p40-h0-i1 | proposal_submit | unavailable | unavailable | 2.2993 | 2.4320 | 2.4829 |
| p40-h0-i1 | receipt_persist | 0.0050 | 0.0154 | 0.0136 | 0.0370 | 0.0538 |
| p40-h0-i1 | manager_activate | unavailable | unavailable | unavailable | 1.5949 | 1.5949 |
| p40-h0-i1 | manager_readback | unavailable | unavailable | unavailable | 0.0635 | 0.0635 |
| p40-h0-i1 | after cold counters (succeeded/ok) | - | - | git=113 blobs=693 journal=15 (31664 B) | submits=1 lowerings=1 rss=149 MiB | - |
| p40-h0-i8 | line_total | 0.1935 | 0.5166 | 2.8606 | 3.6909 | 4.0657 |
| p40-h0-i8 | lowering | unavailable | unavailable | 0.1232 | 0.3453 | 0.3978 |
| p40-h0-i8 | proposal_submit | unavailable | unavailable | 2.4865 | 2.6030 | 2.7294 |
| p40-h0-i8 | receipt_persist | 0.0066 | 0.0223 | 0.0182 | 0.0551 | 0.0759 |
| p40-h0-i8 | manager_activate | unavailable | unavailable | unavailable | 1.7655 | 1.7655 |
| p40-h0-i8 | manager_readback | unavailable | unavailable | unavailable | 0.0659 | 0.0659 |
| p40-h0-i8 | after cold counters (succeeded/ok) | - | - | git=113 blobs=693 journal=22 (59342 B) | submits=1 lowerings=1 rss=148 MiB | - |
| p40-h30-i1 | line_total | 0.1606 | 0.2719 | 3.7539 | 4.0073 | 4.1389 |
| p40-h30-i1 | lowering | unavailable | unavailable | 0.0419 | 0.0353 | 0.0855 |
| p40-h30-i1 | proposal_submit | unavailable | unavailable | 3.4280 | 3.5632 | 3.7000 |
| p40-h30-i1 | receipt_persist | 0.0051 | 0.0153 | 0.0139 | 0.0372 | 0.0551 |
| p40-h30-i1 | manager_activate | unavailable | unavailable | unavailable | 1.8789 | 1.8789 |
| p40-h30-i1 | manager_readback | unavailable | unavailable | unavailable | 0.0725 | 0.0725 |
| p40-h30-i1 | after cold counters (succeeded/ok) | - | - | git=173 blobs=963 journal=15 (31664 B) | submits=1 lowerings=1 rss=155 MiB | - |
| p40-h30-i8 | line_total | 0.2275 | 0.5378 | 3.8763 | 4.6285 | 5.0487 |
| p40-h30-i8 | lowering | unavailable | unavailable | 0.1363 | 0.2894 | 0.4007 |
| p40-h30-i8 | proposal_submit | unavailable | unavailable | 3.4629 | 3.6475 | 3.7095 |
| p40-h30-i8 | receipt_persist | 0.0080 | 0.0233 | 0.0186 | 0.0535 | 0.0765 |
| p40-h30-i8 | manager_activate | unavailable | unavailable | unavailable | 2.0244 | 2.0244 |
| p40-h30-i8 | manager_readback | unavailable | unavailable | unavailable | 0.0774 | 0.0774 |
| p40-h30-i8 | after cold counters (succeeded/ok) | - | - | git=173 blobs=963 journal=22 (59342 B) | submits=1 lowerings=1 rss=153 MiB | - |

Reading the table:

- The bridge's own phases are small and bounded: `lowering` 0.01–0.04 s for one
  item and 0.09–0.35 s for eight (shared change-set lowering, scaling with the
  item count, not the world); `receipt_persist` 0.014–0.06 s for the two extra
  journal records (`prepared` + `delivered`, ~7 KB per item).
- The dominant cost is the proposal door itself, `proposal_submit`: 0.88 s at
  the smallest world and 3.5 s at 76 generations / 40 Claims, cold and warm
  alike, with Git invocations and blobs read growing with the world (31/78 →
  173/963 per cold run). This is `ProposalService.submit` evaluating the
  candidate tree against the accepted tree: shared acceptance machinery, not
  part of this slice, and it is the same cost an SDK change set of one Claim
  pays. It is the remaining cost proportional to unrelated world size.
- Manager activation (1.1–2.0 s) is the existing settlement path, measured once
  per cell; inspect and readback are milliseconds.
- Warm samples are slower than cold in both baseline and after because
  `ProcedureExecutor.execute` rebuilds the run index from the Line partition's
  whole record history on every occurrence (card 162); the bridge adds two
  records per occurrence to that history.
- An intermediate after-run that activated the cold sample first is kept out of
  `results/`: its warm samples measured the occupied-slot refusal, not delivery.
