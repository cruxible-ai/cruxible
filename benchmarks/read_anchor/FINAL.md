# Read-anchor benchmark, final (full read-ergonomics track)

Captured 2026-07-17 on `feature/read-ergonomics` @ `f159c2b` — the complete track: compact query catalog, output profiles, bounded neighborhood inspect (now defaulting to `state=all` with `edges_hidden_by_state`), read_revision/continuation, working set, and the new `--layout graph` on query runs. Prior checkpoints: `baseline_results.json` @ `9b879f3` (0.2.5, pre-track) and `post_track_results.json` @ `bdb3555` (five WIs, pre-graph-layout). Raw data: `final_results.json`.

Instances: rb-\* against a fresh branch-served runebench instance on 8149, rebuilt from `~/Git/runebench/cruxible/kits` (composed `rs-world` + `rs-world-overlay` init; 5,838 entities / 7,257 edges, counts verified against the pinned census). op-\* in serverless local mode over a fresh same-day copy of the operation instance (rsync + sqlite `.backup`; the live 0.2.5 daemon received reads only). Method unchanged: every call twice, warm (run 2) latency headline, `est_tokens = stdout bytes / 4`, correctness asserted.

`ergonomic_v2` variants exist only where the new machinery changes the flow: `--layout graph` on the three rb query-run tasks, and op-1 dropping its `--state all` flag. Where the v2 sequence would be identical to the ergonomic one (op-2, op-3, op-4 — no query-run step, no state flag), the post-track numbers are carried forward rather than contriving changes. rb-4 is also sequence-identical but was re-measured: current code emits +29 B on the same frozen data (the additive `edges_hidden_by_state`/`state` contract fields), so the old number would misreport HEAD.

## The closing table

| Task | Baseline (calls / bytes / tok) | Post-track ergonomic (calls / bytes / tok) | Final (calls / bytes / tok) | Final vs baseline | Correct (base/post/final) |
|---|---|---|---|---:|---|
| op-1 deps of wi-runebench-pilot | 2 / 50,680 / 12,670 | 1 / 11,540 / 2,885 | 1 / 12,507 / 3,127 ¹ | **−75.3 %** | yes / yes / yes |
| op-2 milestone of wi-agent-local-working-set | 3 / 352,052 / 88,013 | 2 / 4,587 / 1,147 | 2 / 4,587 / 1,147 (carried) | **−98.7 %** | yes / yes / yes |
| op-3 owner of wi-read-output-profiles | 1 / 12,816 / 3,204 | 1 / 4,652 / 1,163 | 1 / 4,652 / 1,163 (carried) | **−63.7 %** | yes / yes / yes |
| op-4 WorkItem count + recent | 3 / 7,208 / 1,802 | 3 / 5,575 / 1,394 | 3 / 5,575 / 1,394 (carried) | **−22.7 %** | yes / yes / yes |
| rb-1 who drops cow_hide (cold) | 3 / 29,192 / 7,298 | 2 / 11,019 / 2,755 | 2 / 9,397 / 2,349 (graph) | **−67.8 %** | yes / yes / yes |
| rb-2 scenery at lumbridge (38) | 1 / 166,955 / 41,739 | 1 / 57,263 / 14,316 | 1 / 33,355 / 8,339 (graph) | **−80.0 %** | yes / yes / yes |
| rb-3 where to buy bronze_pickaxe | 2 / 101,214 / 25,304 | 1 / 31,789 / 7,947 | 1 / 22,274 / 5,568 (graph) | **−78.0 %** | yes / yes / yes |
| rb-4 everything about Npc:cow | 2 / 23,546 / 5,886 | 1 / 10,278 / 2,570 | 1 / 10,307 / 2,577 ² | **−56.2 %** | yes / yes / yes |
| **Total** | **17 / 743,663 / ~185,900** | **12 / 136,703 / ~34,200** | **12 / 102,654 / ~25,700** | **−86.2 %** | 8/8 / 8/8 / 8/8 |

¹ op-1 final is +967 B over post-track: **all data drift** (the live operation instance grew as the track's own work landed; the same-day control below proves the code delta is 0 B). ² rb-4 is +29 B on frozen data: the additive `edges_hidden_by_state` + `state` fields.

**Cumulative: the same 8 questions, answered correctly, now cost 12 calls and ~25.7 k tokens instead of the baseline's 17 calls and ~185.9 k — an 86.2 % byte/token reduction.** Warm latency 9.27 s (post-track 9.3 s; still call-count-bound). The v2 step alone is −24.9 % bytes on top of the post-track flows.

## Attribution of the v2 delta (−34,049 B vs post-track, exact reconciliation)

| Mechanism | Tasks | Bytes |
|---|---|---:|
| `--layout graph` on query runs (frozen data, per-call: rb-1 run 4,977→3,355, rb-2 57,263→33,355, rb-3 31,789→22,274; `query list` byte-identical) | rb-1, rb-2, rb-3 | **−35,045** |
| `state=all` default on expanded inspect | op-1 | **0** (flag removed, bytes identical) |
| Additive honesty fields (`edges_hidden_by_state`, `state`) on expanded inspect | rb-4 | +29 |
| Live-instance data drift (not code) | op-1 | +967 |
| **Net** | | **−34,049** |

The state-default change is a correctness/ergonomics win, not a byte win: on the same fresh copy, the v2 call with **no** `--state` flag and the old call with `--state all` return byte-identical output (12,507 B) including all six pending dependency edges, each carrying its `review.status: "pending"` marker, plus the always-present `edges_hidden_by_state: 0`. The sharpest hazard POST_TRACK surfaced — a default-flow agent confidently answering "no dependencies" — is gone; op-1's v2 sequence is now the naive sequence.

## rb-2 deep-dive: rows-compact vs graph-compact, real bytes

| Measure | rows compact | graph compact | ratio |
|---|---:|---:|---:|
| stdout bytes | 57,263 | 33,355 | **1.72×** |
| compact-JSON bytes (whitespace removed) | 29,951 | 19,183 | 1.56× |
| payload sections only (`items` vs `nodes+edges+results+paths`) | 29,422 | 18,647 | 1.58× |

Graph-side channel split: edges 12,784 B (38 unique edges × ~336 B: coords properties + assertion metadata + alias), nodes 3,723 B, results 1,920 B, paths 181 B, envelope ~240 B, **pretty-print whitespace 14,172 B (42.5 % of stdout)**.

**The fixture's 5.7× did not transfer to the live-shaped data; the real ratio is 1.72× stdout.** Two structural reasons: (1) rs-world node cards are tiny (`{"name": ...}` — the per-row duplication graph layout eliminates was small to begin with; the fixture's cards were fatter), and (2) all 38 edges are distinct, so edge dedup contributes nothing — graph layout's win scales with node-card size and edge reuse, and rb-2 has neither. The 10-14 KB target was **not reached**: 33.4 KB stdout, 19.2 KB even fully de-whitespaced.

## Honest notes

1. **Graph layout helps everywhere it applies, but modestly on this data**: −41.8 % (rb-2), −32.6 % (rb-1 run call), −29.9 % (rb-3) per call — real, free at read time, and lossless, but nowhere near the fixture multiple. Where cards are small and paths short, the per-edge assertion metadata and the results/paths indirection eat much of the dedup win. It did NOT help (and was not applied to): inspect flows (op-1/2/3, rb-4 — layout is a query-output feature), and `query list` (byte-identical, as designed).
2. **Pretty-print whitespace is now the single largest remaining byte hog**: 42.5 % of rb-2's final answer is indentation. It has survived every track item because it is orthogonal to all of them (baseline friction note 1, still open). A `--json compact` emit mode would take rb-2 to 19.2 KB with zero information loss — a bigger win on this task than graph layout itself delivered.
3. **Per-edge governance metadata is the second hog**: 38 copies of `{"assertion": {"review": ..., "lifecycle": ...}}` (3.7 KB) + properties on rb-2's edges. Compact profile deliberately keeps governance markers; a marker-summary form (e.g. edge-level `"review": "unreviewed"` string) would shed most of it without losing honesty.
4. **Small regression, accepted**: the always-present `edges_hidden_by_state` + `state` fields cost +29 B on every expanded inspect (rb-4, op-2's second call). This is the honesty contract paying its way; it is what makes the state-default change safe.
5. **Carried forward, not re-measured**: op-2/op-3/op-4 final numbers are the post-track measurements (identical sequences). Same-day rechecks on the fresh copy (in `final_results.json` `cross_checks`) confirm no code regressions: op-2 4,617 B (+30, the additive fields), op-3 5,741 B (+1,089, all instance growth around that work item), op-4 5,876 B and internally consistent (stats == list == 167) but failing its stale 164 pin — pure data drift, the third re-pin this suite has needed. Pinned totals on a live instance are a standing maintenance tax; the assertion that matters (stats/list agreement) never broke.
6. **Latency is unchanged** (9.27 s vs 9.3 s): byte wins don't move wall time; call count and ~0.4-0.7 s CLI startup dominate, as at every checkpoint.
7. **Version-skew hazard from POST_TRACK still stands**: none of the v2 features exist server-side in released 0.2.5, which is why op-\* runs in local mode. `--layout graph` against an old daemon would fail loudly (unknown request field) rather than silently — better than the expanded-inspect empty-result failure mode, but the handshake gap remains.

## Repro

```
# rb-* need the 8149 instance rebuilt (composed overlay init per ~/Git/runebench/cruxible/README.md),
# op-* need a fresh local copy at instances.operation_local.cwd (rsync + sqlite .backup).
RB_BASELINE_HOME=<scratch>/rb-final/home uv run python benchmarks/read_anchor/capture.py --flow ergonomic_v2 --out <out>/final_v2_raw.json
```

`final_results.json` merges that run with the carried-forward post-track entries and the attribution cross-checks (same-day ergonomic control runs, rb-2 channel split).
