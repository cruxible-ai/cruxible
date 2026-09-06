# Bounded consumption writes — 2026-09-06

## Outcome

Small bulk reads no longer reverify the entire consumption partition for every returned artifact. One bounded append verifies the existing partition once under the operational lock, looks up receipt identities in a local index, and preserves each receipt's original payload/event durability boundaries. The accepted ledger, frozen receipt/event digests, operational storage format and wire contracts are unchanged.

Source commit: a1d24188e79d7c009bfe9875826a6fb2704f4af6, based on 097d175594279f2735458de0a87a3fe58ed11e83. The change is on codex/consumption-batch; these measurements do not establish deployed SDK latency.

## Matched isolated-service measurements

Means of three samples per workload. Each new-record call is immediately followed by an identical retry. Each pair restores an exact seeded operational-store snapshot outside timing.

| Prior receipts | Returned artifacts | New records before | New records after | Retry before | Retry after |
|---|---:|---:|---:|---:|---:|
| 0 | 1 | 0.002097 s | 0.002179 s | 0.001840 s | 0.001830 s |
| 0 | 32 | 0.129671 s | 0.015773 s | 0.203832 s | 0.015231 s |
| 128 | 1 | 0.047239 s | 0.046028 s | 0.045029 s | 0.044739 s |
| 128 | 32 | 0.863834 s | 0.061681 s | 0.924343 s | 0.059849 s |

For 128 prior receipts and 32 returned artifacts, the complete consumption-recording call improved 14.0×; its identical retry improved 15.4×. Fresh partition loads fell from 33 to 2 (one epoch scan plus one batch scan), and validated records from 4,753 to 258. Fresh fsync calls remain 128; duplicate retries still perform zero fsyncs. This is reduced verification work, not reduced durability.

Single-item requests still verify the history twice and receive no structural speedup. Their small timing differences should not be treated as a performance result. At larger returned populations, deterministic chunks of at most 256 items each replay the then-current history; this is not a claim of history-independent reads.

The portable harness is [consumption-batch.py](../benchmarks/consumption-batch.py). [Before](consumption-batch-baseline-2026-09-06.json) and [after](consumption-batch-after-2026-09-06.json) JSON record complete samples, file hashes, source metadata, counters and workload limits. The baseline is a git archive of the exact base; after measurements used the uncommitted source bytes subsequently committed above. Only a docstring clarification changed production-file bytes after the initial implementation; the final after file hashes match the committed source.

Command, run with the repository's installed Python interpreter:

    python docs/benchmarks/consumption-batch.py --repo /path/to/isolated/source --output /tmp/consumption.json

Measurements call the real service/store on disposable accepted Subjects. Setup, fixture acceptance, seeding, snapshot copies and correctness checks are excluded. SDK/HTTP authentication, Claim evaluation, source observation and transport are not measured. Filesystem caches are uncontrolled; each pair begins from restored identical store bytes, not a reboot/cold-disk condition. Independent before/after fixtures have their own accepted coordinates and keys. Within every sample, full-service and scalar-reference stores reproduce identical complete file bytes, and retries leave them unchanged. Three samples do not establish p95 or p99.

The final harness instruments append_batch when available, in addition to the old scalar append path. Baseline scalar instrumentation is unchanged. CPU/I/O wall times include lightweight counters; no concurrent benchmark or test suite ran during measurement.

## Design and review guide

Read the existing append wrapper, the private batch helper, the unchanged durable item writer, and finally record_consumption.

- The singular append API retains its expected-head comparison and returns an existing matching receipt before that comparison, as before. Batch append does not introduce multi-event CAS semantics.
- Input count is limited to 256. All payloads are serialized and detached before mutation; invalid serialization leaves no newly written prefix.
- Under the existing file lock, initialization/path checks and complete current partition validation remain mandatory. A temporary first-event-ID map preserves legacy lookup semantics, including duplicate IDs and payloads without an ID.
- Each new payload and event is durably written exactly as before. Once writes begin, a later error can leave a durable prefix. Recovery/retry discovers that prefix from current verified bytes. No verified map survives the call.
- Consumption still sorts/deduplicates exact served artifact identities, binds reader/access profile/operation/accepted coordinate/digest, and initializes the original observation epoch. It batches the resulting receipts without changing their identity or treating a touch as governed knowledge.

This adds no dual writer for ledger and cache. Operational touch records remain non-governed; no accepted generation is written by consumption recording.

## Verification

44 unique targeted tests passed: the original 42-case scope covered the new batch file, consumption receipts, operational-store durability, audit folding and HTTP batch reads; two additional prevalidation/mutation tests passed in the final 14-case batch-file run. Scoped Ruff check/format passed for both production files, new tests and harness; mypy passed for the two production files; diff checks passed. No full suite, golden corpus or canonical-checkout tests ran.

Tests cover exact scalar/batch store bytes, sorted deduplication/chunking, historical and within-batch duplicates, first matching payload identity, conflicting later items, payload/event crash boundaries and retry, separate concurrent store handles, fresh corruption/gap/symlink refusal, empty/oversized requests, payload detachment, prevalidation failure, and unchanged accepted coordinates. Existing singleton concurrent CAS and frozen digest tests remain covered.

[Independent production review](consumption-batch-2026-09-06.md) approved the source/tests. Its author also built the benchmark harness, so that report explicitly excludes independent approval of the benchmark itself; the manager inspected the harness and checked its complete-byte assertions and phase labels.

## Limits and next work

Epoch discovery and each chunk still scan current history. Filename sorting, strict decoding and hashing remain proportional to history size/bytes. The call retains parsed history, its identity index and serialized inputs; the 256-item cap is not a byte/RSS cap. Holding one lock across the bounded writes can delay other operational writers, although expensive external work is not performed under that lock.

The real program roadmap's earlier 80.96-second prefetch has not been rerun on this branch. Measure the complete served workload before claiming its new time or deciding that consumption is now negligible. Separately profile per-Claim admission parsing and eventual history-independent operational lookup; preserve current-byte corruption checks and existing receipt semantics in any subsequent design.
