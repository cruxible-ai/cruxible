# Cold authoring validation and coverage

This batch follows `performance-next-2026-09-05.md`. It targets the first-use
coverage pause exposed by the SDK `next` benchmark. This report is engineering
evidence, not an accepted roadmap update or a governed projection.

Build `35429b2fe81f0e74b5b3df365dc3db9fdef74905` is landed locally and
deployed. The daemon reports three registered instances and one socket listener
(PID 84102 at verification). Nothing has been pushed.

## Measured result

Matched isolated cold coverage fell from **25.510 s to 17.366 s**: 32% less
time, or 1.47 times faster. Both complete responses match the original captured
coverage result. Warm coverage remained approximately 2.4 s.

| Implementation | Cold coverage | Warm coverage |
| --- | ---: | ---: |
| Prior batch, `87fa6a64` | 25.510 s | 2.443 s |
| String fast path and intent binding reuse | 20.363 s | 2.327 s |
| Plus shared event digest/canonical decoding | 17.366 s | 2.362 s |

The benchmark loads the same public-key-only copy of the program instance,
then submits the same captured request in a fresh process. Instance recovery
(about 17 s in this harness) precedes these timers. These local samples are not
latency percentiles; some scoped tests ran concurrently.

An isolated store-only comparison measured the string fast path separately:
21 current publication states took 20.962 s before and 18.961 s after, with the
complete returned tuples equal. That comparison used baseline contract/store
code and changed only the canonical normalizer in memory.

## Why the first call was expensive

Coverage resolves bound publication observations through
`bound_publication_registrations()` and `AuthoringIntentStore.publication_states()`.
The consumer already asks for narrow current fields. Cold reconstruction still
validates all 161 historical events across 21 intent streams before returning
those fields. A corrupt older event must continue to make the registration
read unavailable; filtering to current or relevant-looking events first would
change that behavior.

The cold profile took 64.561 s with profiling enabled, including 58.951 s in
publication-state loading. Canonical normalization accounted for 48.579 s
cumulatively. Event digest verification and subsequent canonical rendering
each traversed large snapshots; intent binding also traversed the same payload
for two commitments. These cumulative costs overlap and must not be summed.
Profiled times are diagnostic, not compared directly with unprofiled latency.

## Changes and review guide

1. `canonical.py`: exact builtin ASCII strings bypass NFC normalization and the
   temporary UTF-8 encoding used to reject surrogates. Exact string values also
   bypass unrelated scalar type checks. Subclasses, enums, non-ASCII strings,
   key sorting, collision detection, escaping, and refusal paths retain their
   previous behavior. Commit: `8e934747`.
2. `contracts/authoring/models.py`: each intent validation creates one ephemeral
   normalized payload snapshot for its payload digest and create fingerprint.
   Runtime identifiers and the payload tag are separately normalized before
   composing the fingerprint. Change-set binding reuses its member identities.
   Public digest helpers remain unchanged. Commit: `0dd15063`.
3. `authoring/store.py`: a private per-parse validation context receives the
   canonical bytes derived from the same model-derived snapshot used to verify
   the event digest. Ordinary validators without that exact private context
   retain the existing public digest path. The store still compares persisted
   raw bytes with the canonical representation before accepting the event.
   Commit: `35429b2f`.

The complete implementation review range is `87fa6a64..35429b2f`.

Review the normalization proof boundaries first, then the unchanged store loop:
every load still enumerates paths, rejects symlinks and invalid sequences,
reads and hashes actual bytes, verifies predecessor links and operation-key
uniqueness, and checks stream identity. Cached history is published only after
the complete stream succeeds. This adds no persistent cache or latest-event
shortcut. No schema, stored format, digest domain, wire contract, or provider
pin changed.

The normalized payload and decode context are local to one validation. They
are never attached to a model or retained in the history memo. Subsequent model
mutations therefore cannot inherit a cached digest. The default event parser
and renderer remain available, including for create-staging recovery.

## Live restart verification

| Call after restart | Coverage HTTP | Next HTTP | SDK next total |
| --- | ---: | ---: | ---: |
| First | 15.464 s | 6.517 s | 22.096 s |
| Following warm call | 2.184 s | 6.385 s | 8.671 s |

The prior deployed batch's first SDK next call took 28.845 s, so this sample
reduced that pause by 23%. Its warm call was 8.420 s; this batch does not show
a material warm-loop improvement. The intended gain is cold reconstruction.

SDK connection/orientation was timed separately at 6.250 s. Connecting and
making the first next call therefore took about 28.346 s in this run. Earlier
connection time was not measured, so no before/after claim is made for that
combined interval.

Both fresh calls returned the same 45-row reason counts. Their digests vary
with fresh observation/evaluation times. Two additional replays of the original
fixed request took 6.304 s and 5.771 s and matched the entire original JSON
response, including its digest and accepted coordinate. No queue repair or
accepted-state write was performed.

The daemon's clean detached checkout runs the reviewed build with the existing
state root, socket, credentials, and capability ceiling. Rollout used the normal
stop/start lifecycle with conflicting client-target environment variables
cleared, avoiding the previously recorded restart-endpoint issue. Operational
receipt: `docs/world-model/cold-performance-deployment-2026-09-05.json`.

## Verification

Independent review approved all three changes without remaining blockers.
There were 199 distinct passing tests across the following scoped runs.

- Canonical fast-string, semantic coordinate, and Merkle checks: 57 passed;
  three golden tests were explicitly deselected.
- Binding reuse, source presence, and authoring wire catalog: 40 passed.
- Authoring intents, change-set intents, and program stamps: 35 passed after
  the first two changes. The eight intent/program-stamp tests also passed after
  the final decoder change.
- Decoder reuse and all existing authoring-history checks: 67 passed on the
  final source (31 new decoder tests and 36 history tests).
- Mypy passed on all 282 source files after the decoder refinement. Scoped
  Ruff, formatting, and `git diff --check` passed.

Coverage comparisons assert complete JSON equality. Focused tests cover frozen
bytes/digests, NFC and Unicode scalar boundaries, ASCII controls and escaping,
subclass hooks, malformed-value locations, missing/null/present source content,
all three event versions, binding failures, and mutable nested payloads.

No full suite, golden corpus, canonical-checkout tests, live authoring writes,
or queue repairs were used for this batch.

## Remaining cost

Cold reads still validate the complete history, and coverage still builds its
accepted evidence inputs. This pass removes repeated validation work; it does
not establish a persistent accepted-state index or O(new-event-bytes) replay.
Warm history continues to verify all current journal bytes. A stronger scaling
change needs an explicit storage/verification design, not a cache that silently
trusts timestamps or unverified projections.

## Local evidence

- `/private/tmp/playbill-coverage-cold-baseline.{json,txt,pstats}`
- `/private/tmp/playbill-cold-store-compare.{py,json}`
- `/private/tmp/playbill-benchmark-cold-coverage.py`
- `/private/tmp/playbill-coverage-reconstruction-before.json`
- `/private/tmp/playbill-coverage-reconstruction-after.json`
- `/private/tmp/playbill-coverage-reconstruction-final.json`
- `/private/tmp/playbill-cold-live-after.{py,json}`
- `/private/tmp/playbill-cold-rollout.json`

These scratch artifacts are benchmark evidence, not durable product state.
