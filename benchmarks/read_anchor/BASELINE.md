# Read-anchor ergonomics baseline (cruxible 0.2.5, pre-change)

Captured 2026-07-17 by `capture.py` against two instances:

- **operation** — live project-state daemon, `http://127.0.0.1:8121` (426 entities / 461 edges). Strictly read-only.
- **runebench** — throwaway instance composed from `runebench` kits `rs-world` + `rs-world-overlay` on port 8149 (5838 entities / 7257 edges: Item 2535, Npc 1357, Scenery 1592, Place 136, Shop 197, Skill 21). Torn down after capture.

Method: each task is the deterministic call sequence a naive cold-start agent would run (discovery — `query list`, `query describe` — included where a cold agent needs it). Every call executed twice; run 2 (warm daemon) is the headline latency. `est_tokens = stdout bytes / 4`. Correctness asserted against known-good values. Raw per-call data in `baseline_results.json`.

## Per-task summary

| Task | Question | Calls | Total bytes | Est tokens | Warm latency (s) | Correct |
|---|---|---:|---:|---:|---:|---|
| op-1 | Deps of wi-runebench-pilot | 2 | 50,680 | 12,670 | 1.15 | yes |
| op-2 | Milestone of wi-agent-local-working-set | 3 | 352,052 | 88,013 | 1.85 | yes |
| op-3 | Owner of wi-read-output-profiles | 1 | 12,816 | 3,204 | 0.58 | yes |
| op-4 | WorkItem count + recent activity | 3 | 7,208 | 1,802 | 1.77 | yes |
| rb-1 | Who drops cow_hide? (cold, incl. discovery) | 3 | 29,192 | 7,298 | 2.08 | yes |
| rb-2 | Scenery at lumbridge (38) | 1 | 166,955 | 41,739 | 0.70 | yes |
| rb-3 | Where to buy bronze_pickaxe (10 places) | 2 | 101,214 | 25,304 | 1.35 | yes |
| rb-4 | Everything about Npc:cow | 2 | 23,546 | 5,886 | 1.06 | yes |
| **Total** | | **17** | **743,663** | **~185,900** | **10.5** | 8/8 |

Cold (run 1) task latencies were within ±0.2 s of warm — per-call wall time is dominated by CLI process startup: `uv run cruxible --version` alone is **0.43 s** (median), i.e. ~60-75 % of a typical 0.55-0.8 s call. The daemon itself is fast.

## Where the bytes go

Channel breakdown for two representative `query run --json` responses (stripped-and-reserialized):

| Channel | rb-2 `scenery_at_place` (166,955 B) | op-2 `work_item_context` (276,628 B) |
|---|---:|---:|
| Pretty-print whitespace (stdout − compact JSON) | 71,696 (42.9 %) | 97,436 (35.2 %) |
| `metadata` / `actor_context` / `provenance` blobs | 59,546 (35.7 %) | 43,820 (15.8 %) |
| `entities` array duplicating `entry`/`result` | 12,739 (7.6 %) | 13,004 (4.7 %) |
| Remaining structured payload | 22,974 (13.8 %) | 122,368 (44.2 %) |
| Minimal correct answer (total + result ids) | **492 (0.3 %)** | — |

- **Whitespace is the single biggest uniform channel**: `--json` output is pretty-printed with indentation; 35-43 % of every large response is spaces/newlines.
- **Metadata share** (compact-JSON basis, per `capture.py`): 53-67 % on runebench responses (small entity properties, so per-edge provenance + per-entity actor_context dominate), 24-26 % on operation entity/inspect responses. Every entity carries a full `actor_context`; every edge carries full `provenance` (created_actor_context, receipt_id, resolution/clone fields, all-null lifecycle) + `assertion` blocks.
- **Row-level duplication**: each query-result item repeats the entry entity, the result entity, *and* both again inside an `entities` array, per path row. The same anchor entity (e.g. `lumbridge`, full properties + metadata) is re-serialized in all 38 rb-2 rows.
- **Full neighbor properties**: op-2's 276 KB for *one* work item's context is 44 % genuine payload only because neighbors include Strategy entities with multi-KB thesis/success_criteria text, serialized in full for every path that touches them. An agent wanting "which milestone" pays ~88 k tokens for a 1-line answer.
- **Discovery is heavy**: `query list --json` on the operation instance is 68,214 B (~17 k tokens) for 38 queries because it inlines full query definitions (entry_point, select, include, order_by, result_shape...). This is the direct evidence for `wi-query-list-compact-catalog`.

## Cheapest vs costliest anchor paths

- Cheapest correct anchor: op-4 (`stats` + bounded `list entities` + `list receipts`) — 7.2 KB / ~1.8 k tokens for counts + recency. Bounded list commands with `--limit` are well-behaved.
- Costliest: op-2 — the *recommended* path (named context query) cost 88 k tokens; the "dumb" path (op-1/op-3, `entity inspect`) answered equivalent single-edge questions in 3-13 k tokens. The context query is the right abstraction but its serialization penalizes using it.

## Friction log (hit during capture)

1. **Inconsistent arg style**: `query run QUERY_NAME` is positional but `query describe` requires `--query NAME`. First `query describe work_item_context` attempt failed with `Error: Missing option '--query'` — a naive agent burns a retry call here.
2. **`entity get` is a dead end for relational questions**: dependencies/owner/milestone are edges, not properties, so the natural first call whiffs (op-1 call 1: 5.2 KB spent to learn nothing). Nothing in the `get` output points the agent to `inspect`.
3. **No recency ordering on `list entities`**: "most recently touched" has no direct flag; fell back to `list receipts` (fine, but discoverable only by luck). Item `metadata` in list output is an empty object — cost with no content.
4. **`entity inspect` has no field selection**: neighbors always arrive with full properties; on the operation graph one Strategy neighbor's thesis blob is ~4 KB inside a "who owns this?" question.
5. **`query run` has no compact/ids-only output mode**: `--count` exists (summary only) but there is nothing between "count" and "everything including per-row provenance".
6. Envelope keys (`param_hints`, `policy_summary`, `truncation_reasons`, `retained_path_count`...) appear on every query response even when empty/irrelevant — small individually, noise for an agent parsing cold.

## Repro

```
uv run python benchmarks/read_anchor/capture.py            # all tasks
uv run python benchmarks/read_anchor/capture.py --only rb-2-scenery-at-lumbridge
```

Operation-instance auth is read at runtime from `~/.cruxible/auth/operation-daemon-tokens.json` into the child env only (never stored/printed). The runebench instance must be rebuilt first (kits in `~/Git/runebench/cruxible/kits`, port 8149, isolated `HOME` — see `tasks.json` `instances.runebench`).
