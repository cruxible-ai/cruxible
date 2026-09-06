# Validated authoring history and current-state reads

This follows the first performance pass at `b7e40170`. Implementation and
independent review are complete. Build `17b05d297a166e01d1fc60763c1c96eaa5c8c206`
is landed locally and deployed after explicit operator-credential authorization.
The daemon serves all three registered instances; the program accepted coordinate
and sampled Claim value remain unchanged. Repeated reads of the existing
162-Claim authoring intent took 0.216 s and 0.033 s, returning the same payload
digest. These are health checks, not a full production write benchmark.
Nothing has been pushed. This report is engineering evidence, not a governed
projection or an accepted roadmap update.

Deployment exposed a restart defect: the endpoint acknowledged restart, then
the CLI exited with `environment target cannot select both server URL and server
socket`. The normal server-start command restored service after conflicting
client-target variables were cleared, using the existing state root, socket,
credentials, and capability ceiling. PID 11247 was the sole socket listener at
verification. Follow-up: make restart preserve an unambiguous daemon launch
target when both URL and socket environment values are present.
Operational receipt: `docs/world-model/performance-deployment-2026-09-05.json`.

## Consumer contracts

The store now separates three questions:

| Read | Returned state | Consumer |
| --- | --- | --- |
| `publication_states()` | Intent identity and current insertion expectations | Bound and released projection-registration lookups |
| `latest_intents()` | Latest validated complete snapshot per stream, in stream order | Internal callers explicitly needing full current state |
| `events()` | Complete validated event history | Historical attempts, curation, and explicit replay |

Current snapshots describe recorded protocol state. They do not refresh candidate
status against the accepted world or make an old preflight newly valid; the
coordinator still owns those decisions. Existing get, pending, fingerprint,
operation-retry, and transition reads copy only selected results from private
validated state. No HTTP/MCP/SDK response contract changed in this batch.

## Integrity and memory

Every load still enumerates current files, rejects invalid paths and symlinks,
reads bytes, computes their SHA-256 hashes, and checks sequence, predecessor
links, stream identity, and operation-key uniqueness. JSON parsing, nested
model validation, and canonical rendering are reused only for bytes previously
validated by those checks. Cache publication occurs only after full success.
The cache is never seeded from caller-constructed models or newly written
in-memory events. A cold process therefore retains the original full replay.

Removing a valid suffix has the same semantics as the previous cold loader:
the surviving prefix is returned. The memo never resurrects the missing suffix.
Persistent anti-rollback protection is not introduced here.

The shared LRU retains at most 128 streams and 256 MiB of serialized input
weight. This bounds input weight, **not Python heap size**. Identical payloads
within a stream share private objects only after canonical validation, keyed by
concrete type and verified create fingerprint. Missing, null, and populated
source fields retain distinct commitments and serialization. Every outward
model and transition callback input is detached, including nested mutable
containers.

The existing registration-result memo still uses its existing filesystem-stat
identity. This pass does not expand that earlier trust boundary to intent-store
validation. Concurrent cache replacement can cause extra work, but each read
checks disk bytes and cannot treat a stale entry as authority.

## Measurements

These are local diagnostic samples, not production latency guarantees. Tests
and other verification sometimes ran concurrently. The program data was copied
to scratch storage; no production signing credentials or live authoring writes
were used for benchmarks.

| Work | Result |
| --- | --- |
| Fingerprint miss over 161 events / about 142 MB, first implementation | 22.987 s cold; 0.143 s and 0.074 s warm |
| Full current snapshots after warming, 21 intents | 1.247 s, revealing unnecessary payload copying for registration consumers |
| Narrow publication fields | 0.147 s warm |
| Narrow publication fields after appending one scratch event | 0.091 s; newly appended data becomes visible |
| Lower the original 162-Claim batch after append and registration-memo reset | 1.686 s; complete candidate-tree fingerprint matches the original baseline |
| Final implementation, including shared payloads, 162-event scratch history | 20.149 s cold; 0.147 s warm; process peak RSS 456,409,088 bytes on macOS |

The lowering baseline was 119.020 s before either performance pass and 22.820 s
after contender indexing alone. The 1.686 s sample has warm parsed history;
it is not a cold-start comparison. The final payload-sharing refinement is
covered by exact-byte tests; the listed lower-after-append sample preceded that
memory refinement.

The first parsed-cache prototype reached a process peak of 881,852,416 bytes
while running its combined lookup/append/lowering benchmark. Inspection found
about 130 MB of repeated payload JSON across snapshots. Sharing those payloads
addresses that multiplier. Its later RSS sample has a different workload, so
these two peaks must not be presented as a controlled memory-speedup ratio.

## Service-loop verification

A disposable synthetic world ran two sequential 24-Claim batches through real
coordinator and public service methods using test-only signing keys:

| Stage | First batch | Second batch |
| --- | ---: | ---: |
| Create | 0.012 s | 0.018 s |
| Preflight | 0.214 s | 0.251 s |
| Submit | 1.532 s | 1.635 s |
| Structured review | 0.074 s | 0.082 s |
| Sign and submit approval | 0.066 s | 0.028 s |
| Activate, including refresh | 1.202 s | 1.432 s |
| Read all 24 Claims | 1.702 s | 1.857 s |
| Total timed stages | 4.802 s | 5.302 s |

Both preflights passed, both complete candidates were reviewed and signed, both
activations accepted, and all 48 Claim readbacks matched their accepted
coordinates and admission accounts. Claim counts progressed 0 → 24 → 48.
This excludes HTTP, the full SDK/runtime wrapper, consumption receipts,
projection repinning, and human review time. Two small batches are not a history
scaling curve or an end-to-end production benchmark.

## Review and verification

Review focus: private cache ownership and deep copies in `authoring/store.py`;
only fully validated raw events can enter the cache; narrow publication fields
in `authoring/registrations.py` and `service/playbill_publications.py`.
Independent reviewer approved both initial reuse and payload sharing, with no
blockers. The reviewer inspected code and tests; execution was performed by the
implementation/test agents.

Named isolated-worktree scopes:

- New history regressions: 36 passed, including timestamp-restored tampering,
  cold/warm refusal equivalence, chained append checks, truncation, symlinks,
  response-loss retry, eviction, root/actor isolation, mutation isolation, and
  exact missing/null/content bytes across all three event versions.
- Intents, source presence, program stamps, and change-set intents: 53 passed.
- Submit, rebase, and preflight: 13 passed.
- Narrow publication and existing memo integration checks: 6 passed.
- Ruff and formatting checks pass on changed source/tests; `git diff --check`
  passes. Mypy reports no issues in 282 source files.

No full suite, golden corpus, canonical-checkout tests, accepted artifact schema
changes, wire re-pin, or journal migration.

## Remaining work

This is the validated-current-state foundation, not completion of every item in
the proposed performance roadmap. Full pending summaries/pagination, bounded
`since` reads, incremental audit/curation aggregates, and preflight reuse remain
separate implementation slices. Fingerprint selection still walks current
stream heads; no durable actor/fingerprint index is introduced.

Warm validation remains O(total journal bytes) in I/O and hashing. Appends avoid
revalidating old models, but do not establish O(new-event-bytes) total work. A
stronger scaling target needs an explicit immutable-storage/change-detection
contract; silently trusting timestamps would weaken the current store checks.
Cold replay, heap accounting, and full SDK/HTTP/projection timings remain
measurement targets. The full pending API still returns complete intents.

## Local artifacts

- `/private/tmp/playbill-performance-data/history-reuse.json`
- `/private/tmp/playbill-performance-data/history-reuse-append.json`
- `/private/tmp/playbill-performance-data/history-reuse-shared-payload.json`
- `/private/tmp/playbill-service-loop-benchmark.py` and matching `.json`/`.log`
- Earlier driver: `/private/tmp/playbill-profile-hotpaths.py`

The scratch history grew from 161 to 162 events through an identity transition
solely for the append benchmark. The live program history was not changed.
