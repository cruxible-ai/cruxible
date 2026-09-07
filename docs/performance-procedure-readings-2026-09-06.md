# Measurement resolution and reading emission: phase timings (2026-09-06)

Source: `docs/benchmarks/procedure-readings.py`, results in
`docs/benchmarks/procedure-readings-2026-09-06.json`. One temporary
knowledge-loop instance (two accepted Claims, one accepted QueryDefinition, one
guarded query-only Procedure), explicit clocks, timed in-process at the service
layer with no HTTP. CAS body reads are counted through the shared read seam.
Medians over the stated sample counts; single-shot phases are labelled
`n=1` and are not percentiles. The synthetic instance is never published.

Timings measure in-process service calls, not end-to-end SDK or HTTP latency.
They are taken after the review-fix pass: reading and resolution appends are
compare-and-sets on their partition heads, every served or replayed reading is
re-read through its content address, reservation recovery scans both journals
this lane writes (the Procedure journal and the query-receipt journal, which
share one reservation store), and Claim verdict observations are retained as
their own records. Concurrent credit and concurrent first resolution are
covered by regression tests, not timings.

There is no "before" for production emission: no emitter existed. The
before/after pairs below cover only the two local kernel optimizations, the
node digest memo and the keyed replay index, measured against the unmemoized
paths on the same inputs.

## Phases

| Phase | 2 measurements, 0 retained | 2 measurements, 40 retained | 6 measurements, 0 retained | 6 measurements, 40 retained |
|---|---|---|---|---|
| Run only, no emitter (baseline, n=3) | 23.6 ms, 19 reads | 24.3 ms, 19 reads | 26.5 ms, 19 reads | 31.9 ms, 19 reads |
| Activation derivation, memos cleared (n=3) | 1.4 ms | 1.4 ms | 1.3 ms | 1.4 ms |
| Activation derivation, warm (n=5) | <0.1 ms | <0.1 ms | <0.1 ms | <0.1 ms |
| First evaluation: evidence + law + resolution append (n=1) | 40.5 ms, 7 reads | 39.3 ms, 7 reads | 65.4 ms, 13 reads | 72.0 ms, 13 reads |
| Standing answer returned, warm (n=5) | 9.6 ms, 4 reads | 9.8 ms, 4 reads | 14.3 ms, 8 reads | 14.1 ms, 8 reads |
| Credit one new run, memos cleared (n=1) | 37.1 ms, 21 reads | 248.2 ms, 61 reads | 51.6 ms, 26 reads | 278.7 ms, 66 reads |
| Same run retried, fixed attribution, warm (n=5) | 31.2 ms, 21 reads | 196.0 ms, 21 reads | 39.7 ms, 26 reads | 205.0 ms, 26 reads |
| Same run retried, fresh attribution per request, warm (n=5) | 32.2 ms, 21 reads | 189.3 ms, 21 reads | 41.4 ms, 26 reads | 205.7 ms, 26 reads |
| Readings inspection (limit 200), index cold (n=3) | 11.1 ms, 8 reads | 50.5 ms, 88 reads | 14.8 ms, 14 reads | 54.0 ms, 94 reads |
| Readings inspection (limit 200), index warm (n=5) | 9.0 ms, 6 reads | 30.9 ms, 46 reads | 13.0 ms, 11 reads | 33.4 ms, 51 reads |

The "6 measurements" batch declares two that never resolve in the window
(one pending, one expired), so a first evaluation writes four resolutions and
a credited run mints three readings (the untaken arm earns none). The
fresh-attribution retry re-mints operation id, request id, and attribution
instant on every sample, exactly as an authenticated client does; it replays
at the same cost as the fixed-attribution retry.

## Kernel optimizations, before and after

| Kernel | Before | After |
|---|---|---|
| Node digest vector for one revision (n=20) | 0.2 ms per call, recomputed each time | <0.05 ms warm, computed once per definition digest |
| Keyed reading replay, 0 retained (n=5) | 1.3 ms, 1 CAS read (partition rescan) | 0.5 ms, 0 CAS reads (index) |
| Keyed reading replay, 40 retained (n=5) | 4.7 ms, 1 CAS read (partition rescan) | 0.5 ms, 0 CAS reads (index) |

Both preserve intent exactly: identical digest vectors and activations
(`tests/test_playbill/test_procedure_graph_digest_memo.py`), identical stored
records whether the replay is found by scan or by index, unchanged law,
grading, time, authority, and idempotency. The memo is bounded (256
definitions) and keyed on the exact definition digest; the index is per
process, bounded (16 partitions), extended only over a matching record prefix,
and rebuilt from the journal's own verified bytes on any divergence. The
kernel-level zero-read replay is a lookup cost only: the service re-reads the
matched reading's body through CAS before replaying it (one read per replayed
reading, visible in the retry rows above), so a warm process refuses a missing
or corrupt body exactly as a cold one does.

## What still scales with history, and where

- **Run lookup and reservation recovery dominate the warm retry at 40
  retained readings** (196 ms vs 31 ms at zero). Two full journal walks
  precede the credit: `_records_for_run` finds the run by walking every
  partition, and reservation recovery scans every partition of both journals
  this lane writes, as the run and settlement writers do, because a partial
  scan would release another partition's crashed lease. This world has one
  partition per seeded run. A
  run-id to partition index and a bounded recovery scan belong with the run
  lane's own indexing work; the measurement lane will not shortcut them.
- **Served readings cost one CAS read each** (46 reads for 42 readings at
  limit 200, warm). That is the page-bounded integrity check, paid per page
  served, and it is the price of a warm daemon never vouching for a body it
  cannot show. A smaller `limit` bounds it; the warm index still avoids
  re-parsing.
- **Cold index parse is one further CAS read per retained reading** (88 at 40
  retained). It is paid once per process per partition and amortized by the
  prefix extension. A persisted index reconstructable from the partition's
  record digests would remove it and is deferred as storage/index work.
- **Per-activation contract state re-reads the resolution partition** (4 CAS
  reads per two measurements on the standing-answer path). Partitions are one
  record deep per answer, so this is bounded by the number of overturns, not by
  runs.
- **Evidence gathering is the real first-evaluation cost** (query execution,
  the Claim verdict plus its retained observation record, the complete
  attestation history), and it is deliberately not cached: a fresh external
  evidence check is what the resolution certifies.

Journal writes per first evaluation: one `resolution_activation` and one
`resolution` record per activation (each `fsync`ed by the journal backend),
plus one `query_executed` receipt per accepted-query measurement and one
`claim_verdict_observed` record per Claim-statement measurement in the
query-receipt journal; per credited run: one `procedure_reading` record per
grain that occurred. A retry appends nothing.
