# Code Review

## Verdict

Approved

The production change preserves the single-append identity, compare-and-append, fresh corruption checks, and per-event durability boundaries while removing repeated partition replay within each bounded batch. No correctness or authority blocker was found. Final approval includes the docstring clarification and two permanent boundary/mutation regressions committed at `a1d24188e79d7c009bfe9875826a6fb2704f4af6`. This is an independent review of production source and its new regression tests; the reviewer authored the benchmark harness, so its methodology and measurements are not independently reviewed here.

## Manual Review Priority

- Priority: P1
- Reason: Shared operational persistence now holds one lock across multiple durable writes and replaces repeated linear identity lookup with a request-local index.
- Suggested Human Review Focus: First matching payload identity and idempotency-before-CAS ordering; fresh replay under the file lock; crash-prefix recovery and fsync placement; all-input serialization before mutation.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/consumption.py`, `src/cruxible_core/playbill/review_operational.py`, initially unstaged against `097d175594279f2735458de0a87a3fe58ed11e83`; final reviewed production/test head is `a1d24188e79d7c009bfe9875826a6fb2704f4af6`. The final addendum changes only documentation and tests relative to the executable source originally reviewed.
- Untracked files: `tests/test_playbill/test_consumption_batch.py` reviewed. The benchmark harness and before/after JSON are explicitly excluded from independent approval because this reviewer authored the harness and baseline.
- Tests examined: all cases in `tests/test_playbill/test_consumption_batch.py`; relevant durability, frozen digest, idempotency, and concurrent CAS cases in `tests/test_playbill/test_review_operational_store.py`.
- Commands run: `git status --short`, `git diff --stat`, targeted `git diff`, `git diff --cached --stat`, `git rev-parse HEAD`, targeted `sed`/`rg`, `shasum -a 256`, and a disposable Python reviewer probe with worktree `PYTHONPATH=src:packages/cruxible-client/src` using the canonical virtual environment executable. The probe verified malformed later input leaves the store absent and nested caller mutation after preparation cannot change persisted payloads. Its first attempt caught an incorrect exception superclass; the corrected probe caught the existing `CanonicalEncodingError` and both checks passed.

Parent-reported final validation: 44 distinct targeted tests passed (including 14 in the batch file; the original scope had 42 before the two added regressions) across consumption batch, consumption receipts, review operational store, audit fold, and HTTP claim-batch coverage; Ruff and scoped mypy passed for both production files. The reviewer did not duplicate these tests or run a full suite, goldens, benchmarks, or live instance writes.

Exact reviewed file SHA-256 values:

| File | SHA-256 |
| --- | --- |
| `src/cruxible_core/playbill/consumption.py` | `9a326f9b77d1c3ac03b2d6639cf702b95a6e3918d36e5eff3c89289054898317` |
| `src/cruxible_core/playbill/review_operational.py` | `7f1916d44c7907d31d7186d89672c0f7f7cb9041ae54bb9d42097891f2630c58` |
| `tests/test_playbill/test_consumption_batch.py` | `b545f026ab7dca37da6bd27d2516e21591b72ccfd67127be0b10719433d188e2` |

## Findings

No findings.

## Complexity Assessment

For an existing partition with H events and a batch of B <= 256 requests, the new path performs one complete partition verification and builds one O(H) identity index, then O(B) expected dictionary lookups and the necessary durable writes. Partition filename sorting remains O(H log H); canonical decoding, digest verification, and I/O still scale with total history bytes. The former scalar loop repeatedly performed those operations as the history grew. Reads above the limit use successive chunks and therefore still replay once per chunk; `ensure_consumption_epoch` retains its separate history scan.

The limit bounds requests per lock acquisition, not bytes or total memory. The implementation retains the freshly parsed history plus an O(H) index and up to 256 serialized and detached payloads; `record_consumption` also constructs all returned receipts. Nothing survives as a verified cache between calls. The instance-wide file lock remains held through each payload/event fsync, so a large batch can delay unrelated operational writers, although the 256-item limit bounds the count of writes per acquisition.

## Architecture Assessment

The change stays within service-owned consumption orchestration and the existing non-governed operational store. Accepted ledger authority, receipt identity rules, wire models, epoch semantics, and storage formats are unchanged.

Suggested implementation reading order:

1. `append` delegates a singleton to `_append_batch` and keeps the original expected-head argument. The public batch method intentionally does not add multi-event CAS semantics.
2. `_append_batch` rejects oversized input, returns immediately for empty input, and normalizes/detaches all payloads before acquiring the existing file lock. Serialization refusal therefore precedes initialization, as before for singleton append.
3. Under the lock, initialization and partition creation use the existing safety checks. Existing event files trigger the unchanged complete `_load_partition` validation, including canonical bytes, event chains, payload digests, file types, and symlink refusal.
4. The local index uses `setdefault` on persisted string payload `event_id` values. It preserves the first matching historical payload identity, including request/payload identity mismatches and payloads with no string identity. Successful duplicates return before the expected-head check, exactly as the old append did.
5. `_append_verified` preserves the original payload occupancy check, exclusive payload write, payload crash hook, event construction and digest, exclusive event write, and event crash hook. Sequence, predecessor, and index advance only after this returns. A crash after the event write is recovered by fresh replay on retry.
6. `record_consumption` preserves artifact sorting/deduplication, generation lookup, epoch initialization, receipt construction, and return order, then submits deterministic chunks under the fixed limit. Receipts are prepared before chunk writes; malformed input at this stage can leave an initialized epoch but no receipt prefix. Production callers supply already resolved typed artifacts.

The lock and file-safety model is unchanged: cooperating writers serialize through the operational lock. This patch does not claim protection against arbitrary external mutation during an active lock scope or transactional rollback across a batch.

## Test Coverage Assessment

The new tests meaningfully cover exact scalar/batch store-byte parity; historical and within-batch duplicates; divergent lookup and payload identities; a conflicting later item retaining the exact prefix; recovery after both payload and event crash boundaries; two independent store handles performing concurrent duplicate batches; fresh refusal for payload/event corruption, gaps, and symlinks; one replay per batch; empty/oversized no-initialization behavior; and chunked consumption preserving accepted state. Existing concurrent singleton CAS coverage remains applicable because the wrapper passes its expected head through unchanged.

The reviewer additionally checked malformed later input and nested payload detachment with disposable state. The final commit also includes permanent regressions for both boundaries, independently inspected in this addendum: malformed later input creates neither store nor lock file, and caller mutation of identity/nested values cannot alter the written payload or duplicate retry. The concurrency test uses actual concurrent store objects without time-dependent sleep assertions.

## Documentation Assessment

The batch docstring correctly states bounded durable-prefix behavior, per-item fsync, retry identity, and lock-scoped verification. The first-match comment explains an otherwise easy-to-miss compatibility requirement. The final docstring now explicitly distinguishes serialization before mutation from durable-prefix behavior after prevalidation. No public wire documentation or schema update is required.

## Overall Contribution

This is a cohesive performance change with a narrow storage implementation seam and meaningful integrity regressions. It removes redundant CPU and disk-read work without relaxing receipt validation or durable event publication. The remaining full history scans, per-event fsync cost, and single-item overhead are real limits and should remain visible in performance reporting.

## Open Questions

None.

## Suggested Follow-Ups

- Continue to measure single-item requests separately from batches and distinguish isolated consumption-phase timings from served SDK latency. Any future epoch/index optimization needs its own freshness and corruption proof.
