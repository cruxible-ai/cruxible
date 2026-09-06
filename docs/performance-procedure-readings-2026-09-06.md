# Measurement resolution and reading emission: phase timings (2026-09-06)

Source: `docs/benchmarks/procedure-readings.py`, results in
`docs/benchmarks/procedure-readings-2026-09-06.json`. One temporary
knowledge-loop instance (two accepted Claims, one accepted QueryDefinition, one
guarded query-only Procedure), explicit clocks, timed in-process at the service
layer with no HTTP. CAS body reads are counted through the shared read seam.
Medians over the stated sample counts; single-shot phases are labelled
`n=1` and are not percentiles. The synthetic instance is never published.

There is no "before" for production emission: no emitter existed. The
before/after pairs below cover only the two local kernel optimizations, the
node digest memo and the keyed replay index, measured against the unmemoized
paths on the same inputs.

## Phases

| Phase | 2 measurements, 0 retained | 2 measurements, 40 retained | 6 measurements, 0 retained | 6 measurements, 40 retained |
|---|---|---|---|---|
| Run only, no emitter (baseline, n=3) | 21.5 ms, 19 reads | 22.9 ms, 19 reads | 20.3 ms, 19 reads | 19.7 ms, 19 reads |
| Activation derivation, memos cleared (n=3) | 1.4 ms | 1.3 ms | 1.3 ms | 1.3 ms |
| Activation derivation, warm (n=5) | <0.1 ms | <0.1 ms | <0.1 ms | <0.1 ms |
| First evaluation: evidence + law + resolution append (n=1) | 30.5 ms, 7 reads | 31.8 ms, 7 reads | 52.1 ms, 13 reads | 52.7 ms, 13 reads |
| Standing answer returned, warm (n=5) | 7.2 ms, 4 reads | 7.1 ms, 4 reads | 10.9 ms, 8 reads | 11.2 ms, 8 reads |
| Credit one new run, memos cleared (n=1) | 27.7 ms, 21 reads | 173.6 ms, 61 reads | 36.5 ms, 26 reads | 206.0 ms, 66 reads |
| Same run retried, all readings replay, warm (n=5) | 20.9 ms, 19 reads | 114.2 ms, 19 reads | 26.0 ms, 23 reads | 127.1 ms, 23 reads |
| Readings inspection, index cold (n=3) | 9.2 ms, 6 reads | 31.5 ms, 46 reads | 11.1 ms, 11 reads | 35.9 ms, 51 reads |
| Readings inspection, index warm (n=5) | 6.4 ms, 4 reads | 12.6 ms, 4 reads | 8.5 ms, 8 reads | 16.0 ms, 8 reads |

The "6 measurements" batch declares two that never resolve in the window
(one pending, one expired), so a first evaluation writes four resolutions and
a credited run mints three readings (the untaken arm earns none).

## Kernel optimizations, before and after

| Kernel | Before | After |
|---|---|---|
| Node digest vector for one revision (n=20) | 0.1 ms per call, recomputed each time | <0.05 ms warm, computed once per definition digest |
| Keyed reading replay, 0 retained (n=5) | 1.1 ms, 1 CAS read (partition rescan) | 0.5 ms, 0 CAS reads (index) |
| Keyed reading replay, 40 retained (n=5) | 4.3 ms, 1 CAS read (partition rescan) | 0.5 ms, 0 CAS reads (index) |

Both preserve intent exactly: identical digest vectors and activations
(`tests/test_playbill/test_procedure_graph_digest_memo.py`), identical stored
records whether the replay is found by scan or by index, unchanged law,
grading, time, authority, idempotency, and corruption detection. The memo is
bounded (256 definitions) and keyed on the exact definition digest; the index
is per process, bounded (16 partitions), extended only over a matching record
prefix, and rebuilt from the journal's own verified bytes on any divergence.

## What still scales with history, and where

- **Run lookup, not reading emission, dominates the warm retry at 40 retained
  readings** (114 ms vs 21 ms at zero). The reading index is warm and answers
  from memory; the cost is `_records_for_run`, which walks every journal
  partition to find one run, and this world has one partition per seeded run.
  That walk is pre-existing run-lane behaviour shared with `procedure status`;
  a run-id to partition index belongs with the run lane's own indexing work.
- **Cold index parse is one CAS read per retained reading** (46 reads at 40
  retained). It is paid once per process per partition and amortized by the
  prefix extension; a daemon that restarts often, or serves many Procedures,
  pays it per Procedure once. A persisted index reconstructable from the
  partition's record digests would remove it and is deferred as storage/index
  work to coordinate with the wider indexing lane.
- **Per-activation contract state re-reads the resolution partition** (4 CAS
  reads per two measurements on the standing-answer path). Partitions are one
  record deep per answer, so this is bounded by the number of overturns, not by
  runs.
- **Evidence gathering is the real first-evaluation cost** (query execution and
  the Claim verdict), and it is deliberately not cached: a fresh external
  evidence check is what the resolution certifies.

Journal writes per first evaluation: one `resolution_activation` and one
`resolution` record per activation (each `fsync`ed by the journal backend); per
credited run: one `procedure_reading` record per grain that occurred. A retry
appends nothing.
