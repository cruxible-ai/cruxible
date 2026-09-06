# Repeated SDK write latency — 2026-09-06

## Result

A fresh typed World now uses one current ClaimType listing instead of fetching and discarding a broad orientation page first. Historical accepted ChangeSets dispatch directly to their declared frozen version; malformed input retains the original ordinary-union fallback and refusal details. These changes preserve canonical bytes, settlement verification, and ledger authority. No retained model cache, storage migration, or new background work was introduced.

The meaningful end-to-end gain is the World refresh. Acceptance remains about nine seconds; this small sample does not establish a material acceptance gain from parser dispatch. The isolated parser experiment reduced decoding of 27 public historical records from approximately 1.67 to 1.14 seconds, but that must not be described as the acceptance improvement.

## Matched unprofiled benchmark

Median of three successive two-Claim writes in one daemon and SDK connection:

| Operation | Before | After |
|---|---:|---:|
| Prepare | 0.634 s | 0.642 s |
| Submit | 2.970 s | 2.923 s |
| Accept | 9.127 s | 9.054 s |
| Pinned readback | 0.332 s | 0.321 s |
| World refresh | 2.243 s | 0.281 s |
| Complete write loop | 16.676 s | 14.345 s |

The two later, explicitly warm iterations average **16.549 s before / 14.497 s after**. With only two warm samples, these are descriptive timings rather than a latency distribution. The first measured write also follows separately timed connect and initial World acquisition; it is not an OS-cache-controlled cold measurement.

- Before source: `089100609fddb60755bf3494fb0f268e4661a386`.
- After source: `c42feb24`, including parser fix `946d2d96`; only benchmark/review artifacts were uncommitted during the after run.
- Workload: 1,000 seed Claims, eight history generations, 100 Subjects, eight ClaimTypes, 48 real unsigned orphan proposal commits. Each write revises one stable identity and creates one new observation.
- Both runs used `--world --no-server-profile`, real Unix HTTP, and the same harness/workload. Each accepted generation obtains a fresh coordinate-pinned World; the old World is not mutated. Lazy Subject acquisition is charged to drafting.
- Totals include drafting, prepare, submit, status, approval challenge/sign/submit, accept, coordinate-pinned bounded full Claim readback, and World refresh. Fixture creation, daemon startup, connect/orient, initial World acquisition, and human review time are excluded.
- Fresh disposable worlds and keys were generated independently; this is matched logical work, not equal signed digests or minted identities. No live instance was benchmarked. The workspace advertisement succeeds; no floor or ledger mirror was configured.
- Readback checks identity count, value, and accepted coordinate. The fixture policy grades these coordinator self-sources current/uncovered; this is a lawful write-loop diagnostic, not a supported-evidence product proof.
- Before/after JSON files retain exact rows, grades, setup costs, and source status. The before report's generic “SDK refresh” description is disambiguated by `world: true` and each row's `world_refresh` phase.

Reproduce from an isolated checkout, substituting the source worktree and output:

```sh
python docs/benchmarks/write-loop-served.py --repo /path/to/worktree \
  --population 1000 --history 8 --repeats 3 --claims-per-write 2 \
  --orphan-proposals 48 --world --no-server-profile --output /tmp/warm-world.json
```

## Review and validation

Read SDK `world()` first: its listing captures the current accepted coordinate once server-side and returns vocabulary from that tree. The SDK advances its coordinate from that response. Existing Worlds then refuse use after the connection advances. Public `refresh()` retains its orientation-page behavior.

Next read `parse_change_set_record`: cached adapters contain schema machinery only. Valid tags select the same version-specific validator; malformed data falls back to the former union behavior. Both paths still compare the exact rendered canonical bytes. Every result is freshly parsed, including nested mutable values.

Source and tests were committed as separate logical fixes. Independent review approved both without findings. Parser/settlement scope: 51 passed in 7.16 seconds. SDK World, prefetch, server generation, and demo-world scope: 46 passed in 90.88 seconds against final source. Scoped Mypy, Ruff check/format, and diff checks passed. No full suite, journal goldens, or canonical-checkout tests were run. The earlier non-escalated disposable-daemon smoke could not bind its socket under the sandbox; the authorized retry passed.

See `change-set-parser-dispatch-2026-09-06.md` and `warm-sdk-world-2026-09-06.md` for the standard independent review reports.

## Remaining priorities

1. **Acceptance's repeated recovery.** The baseline instrumented warm acceptance spends about 5.7 seconds in refresh/recovery after publication. Design a normal successful-activation path that advances in-memory state from the exact verified, committed outcome, preserving publication/crash semantics and full recovery on restart or uncertainty. Audit the proof handoff before implementing; merely skipping refresh is not a safe solution.
2. **Projection prebuild.** The same profiled run spends about 8.0 seconds rebuilding projections. Unchanged Claim bytes can reuse byte-dependent compilation, but proof-bearing rows include the current accepted coordinate and must be refreshed. Any reuse needs immutable/private cached values and exact compiler/content bindings. Full old projection rows cannot simply be carried forward.
3. **Overlapping submission evaluation.** The profile shows two proposal-tree evaluations across preflight and submit. Investigate sharing immutable exact-tree derivations within a request while retaining fresh policy, custody, source, and base checks.
4. **Readback and initial connection.** Bound readback continues to grow across successive revisions, and initial connect/orient still has a separate startup cost. Keep repeat-write latency ahead of optimizing one-time cold journal lookup; the two costs are not interchangeable.

The profiled diagnosis (about 27 seconds total and 17 seconds acceptance) includes substantial cProfile overhead. Its nested phase times are not additive and must not be compared directly with the unprofiled table. The projection audit found repeated history scans contribute only about 0.3–0.4 seconds within that profile; linear scans alone do not explain the latency. These follow-ups are design work still to do, not completed optimizations.
