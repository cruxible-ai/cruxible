# Read-anchor benchmark, post read-ergonomics track

Captured 2026-07-17 on `feature/read-ergonomics` @ `bdb3555` (all five track WIs landed: compact query catalog, output profiles, bounded neighborhood inspect, read_revision/continuation, working set). Baseline: `baseline_results.json` @ `9b879f3` (pre-track, 0.2.5). Raw data: `post_track_results.json`.

Two measurements:

- **A — old flows on new code**: the baseline call sequences, unchanged. op-\* against the live 8121 daemon (released 0.2.5 server, branch CLI), rb-\* against a fresh branch-served runebench instance on 8149 (same kits, identical dataset: 5,838 entities / 7,257 edges).
- **B — ergonomic flows**: parallel variants per task (same question, same asserted fact) using the new machinery. rb-\* on 8149; op-\* in **serverless local mode over a same-day copy of the operation instance**, because the 0.2.5 daemon lacks the server-side features (see hazards below).

Method as baseline: every call run twice, warm (run 2) latency is headline, `est_tokens = stdout bytes / 4`.

## Per-task comparison

| Task | Baseline (calls / bytes / tok / s) | A: old flows (calls / bytes / tok / s) | B: ergonomic (calls / bytes / tok / s) | B vs baseline bytes | Correct (base/A/B) |
|---|---|---|---|---:|---|
| op-1 deps of wi-runebench-pilot | 2 / 50,680 / 12,670 / 1.15 | 2 / 53,459 / 13,365 / 1.61 | 1 / 11,540 / 2,885 / 0.78 | **−77.2 %** | yes / yes / yes |
| op-2 milestone of wi-agent-local-working-set | 3 / 352,052 / 88,013 / 1.85 | 3 / 380,295 / 95,074 / 2.32 | 2 / 4,587 / 1,147 / 1.54 | **−98.7 %** | yes / yes / yes |
| op-3 owner of wi-read-output-profiles | 1 / 12,816 / 3,204 / 0.58 | 1 / 14,827 / 3,707 / 0.72 | 1 / 4,652 / 1,163 / 0.73 | **−63.7 %** | yes / yes / yes |
| op-4 WorkItem count + recent | 3 / 7,208 / 1,802 / 1.77 | 3 / 7,265 / 1,816 / 2.12 | 3 / 5,575 / 1,394 / 2.15 | **−22.7 %** | yes / no\* / yes |
| rb-1 who drops cow_hide (cold) | 3 / 29,192 / 7,298 / 2.08 | 3 / 22,065 / 5,516 / 2.41 | 2 / 11,019 / 2,755 / 1.51 | **−62.3 %** | yes / yes / yes |
| rb-2 scenery at lumbridge (38) | 1 / 166,955 / 41,739 / 0.70 | 1 / 166,955 / 41,739 / 0.96 | 1 / 57,263 / 14,316 / 0.89 | **−65.7 %** | yes / yes / yes |
| rb-3 where to buy bronze_pickaxe | 2 / 101,214 / 25,304 / 1.35 | 2 / 101,214 / 25,304 / 1.75 | 1 / 31,789 / 7,947 / 0.93 | **−68.6 %** | yes / yes / yes |
| rb-4 everything about Npc:cow | 2 / 23,546 / 5,886 / 1.06 | 2 / 23,546 / 5,886 / 1.41 | 1 / 10,278 / 2,570 / 0.74 | **−56.4 %** | yes / yes / yes |
| **Total** | **17 / 743,663 / ~185,900 / 10.5** | **17 / 769,626 / ~192,400 / 13.3** | **12 / 136,703 / ~34,200 / 9.3** | **−81.6 %** | 8/8 / 7/8\* / 8/8 |

\* op-4 A "failure" is data drift, not code: the task pins WorkItem total = 162 from the pre-track capture; the live instance is now at 164 (the track itself landed state there). stats and list still agree with each other (164 == 164). The B variant pins the current 164 and passes.

**Headline: the ergonomic flows answer the same 8 questions correctly with 12 calls instead of 17 and ~34 k tokens instead of ~186 k — an 81.6 % byte/token reduction** (−82.2 % vs A on same-day data; op-\* B vs A alone is −94.2 %).

## Measurement A — what agents get for free

On the frozen runebench dataset, **every standard-profile call is byte-for-byte identical to baseline** (rb-2, rb-3, rb-4, and rb-1's describe + run all match exactly). The only code-driven change in A is `query list` returning summaries by default:

- op-2 `query list`: 68,214 → 15,962 B (**−76.6 %**, free)
- rb-1 `query list`: 13,169 → 6,042 B (**−54.1 %**, free)

All other op-\* deltas in A are live-instance drift (426 → 441 entities since baseline): op-1 inspect +6.1 %, op-2 `work_item_context` run +29.1 % (the context query grew with the track's own work items/notes), op-3 +15.7 %, op-4 list +54 B. **No code regressions in A**; nothing on frozen data moved at all. A's total is *up* 3.5 % purely because op drift (+85 KB, of which +80 KB is the op-2 context query alone) outweighed the free catalog win (−59 KB).

## Where the B wins came from

| Mechanism | Tasks | Effect |
|---|---|---|
| Summary catalog (default `query list`) | rb-1, rb-3, op-2 | Discovery drops ~54-77 %; `required_params` in the summary makes every `query describe` call unnecessary (rb-1, rb-3 each drop a call; rb-3 becomes a single call) |
| `--profile compact` | op-3, op-4, rb-1, rb-2, rb-3, rb-4 | 52-69 % off query/inspect/list responses (per-call, same data: op-3 inspect −68.6 %, rb-3 run −68.3 %, rb-1 run −67.5 %, rb-2 run −65.7 %, op-4 list −51.8 %); identity + governance markers retained, per-row metadata/actor_context/provenance gone. Pure serialization win, zero extra knowledge needed |
| Bounded neighborhood inspect (one call) | op-1, op-2, rb-4 | `get`+`inspect` and query-flow sequences collapse to one depth-1 read. op-2 is the poster child: `stats` (3,083 B, which lists the relationship vocabulary) + one `--relationship work_item_in_milestone` inspect (1,504 B) replaces a 357 KB context-query flow: **−98.8 %** |
| Working set (`--ws` + rg) | — | Not used in any measured flow — honest call, see below |

## Honest notes / where B is not better

1. **op-1 needed `--state all` to be correct.** The operation instance's dependency edges are pending direct adds. The legacy single-hop inspect shows them; the expanded bounded read defaults to `live` and **silently returns zero dependency edges** — a default-flow agent would confidently answer "no dependencies". The B flow only passes because the variant widens state explicitly. This default-visibility asymmetry between legacy and expanded inspect is the sharpest product hazard this benchmark surfaced.
2. **Version skew fails silent, not loud.** The branch CLI's expanded inspect against the released 0.2.5 daemon (8121) returns a well-formed nodes/edges shape with `nodes: 0, edges: 0, truncated: false` — indistinguishable from "entity has no neighbors". This is why B op-\* ran in local mode over a copy. A version handshake or hard error would prevent a nasty class of wrong answers.
3. **Profiles alone don't fix `work_item_context`.** `--profile compact` on the op-2 context query still emits 158,354 B (−55.7 % vs the same-day standard run of 357,123 B, measured separately) for a one-line answer — the per-path row duplication survives compaction. op-2's 98.7 % win came from *not needing the query*, not from compacting it.
4. **op-4 gains little (−23 %).** Already-bounded flows (`stats`, `--limit 3` lists, receipts) were never the problem; `list receipts` has no `--profile` flag but is small anyway.
5. **Working set unused — deliberately.** No task in this suite genuinely re-reads state within a sequence once the summary catalog carries `required_params` (the only baseline re-read, rb-3's `describe` after rb-1's `list`, is eliminated by the catalog itself). Forcing `--ws` + rg into a flow would have been contrived. Mechanics were smoke-tested separately and work: `--ws` on a query run captured 7 records (4 entities, 3 edges), `ws path` + `rg cow_hide` hit 4 records at zero CLI token cost, `ws verify` reported all fresh at read revision 3. A benchmark task shaped like "answer 3 questions about the same neighborhood" would exercise it honestly.
6. **rb-2 compact is still 57 KB** for "38 scenery ids" (492 B minimal answer). Compact removes metadata but keeps the full entry/result/entities/path row structure ×38, with the anchor place repeated in every row. Row-shape dedupe (baseline friction note 3) remains the open opportunity on query results.
7. **Latency is call-count-bound.** B's 9.3 s vs A's 13.3 s warm tracks the 17→12 call drop; per-call wall time is still dominated by ~0.4-0.7 s CLI startup, not the daemon or payload size.

## Repro

```
# Measurement A (baseline sequences; rb needs the 8149 instance up, op needs the 8121 daemon)
RB_BASELINE_HOME=<scratch>/rb-post/home uv run python benchmarks/read_anchor/capture.py --out <out>/measurement_a.json

# Measurement B (ergonomic variants; op-* additionally needs the local instance copy in instances.operation_local.cwd)
RB_BASELINE_HOME=<scratch>/rb-post/home uv run python benchmarks/read_anchor/capture.py --flow ergonomic --out <out>/measurement_b.json
```

The operation local copy = rsync of `~/.cruxible/operation-daemon/instances/inst_d9dd589039054a1a` (excluding `state.db`) + `sqlite3 .backup` of `state.db`, run from the copied root in serverless mode with an isolated `HOME`. Baseline task definitions in `tasks.json` are unchanged; ergonomic variants live under each task's `ergonomic` key.
