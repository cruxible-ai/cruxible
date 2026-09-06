# Cold intent validation: measured slice and design follow-up

This branch removes duplicate calculations inside each historical event decode.
It does not change the journal format, persist a new index, defer verification,
or claim to close the foreground write-latency gate.

## Implementation and authority

Read `packages/cruxible-client/src/cruxible_client/contracts/authoring/models.py`
first. A private decode context carries member identities from payload validation
to intent binding, then the normalized payload from binding to event verification.
These are results of validators that already ran on the same objects during the
same call. The ordinary public validation path still computes independently.

Then read `src/cruxible_core/playbill/authoring/store.py`. The event validator
normalizes the remaining envelope and uses the payload normalization just
completed by binding. It still reproduces the frozen event digest and renders
the full model-derived canonical event; the store still compares that rendering
with freshly read bytes and checks paths, sequence, links and operation keys.
No supplied fingerprint serves as proof of validated contents.

The context is reset on event input and its slots are consumed before event
verification, including failure. Both are necessary: Pydantic can skip before
validators for an existing model. Independent review found that reset-on-input
alone allowed a deliberately reused private context to carry a stale normalized
payload after a nested model mutation. The final implementation consumes those
slots and includes an ordinary mutable-dictionary regression across all three
event versions. No result model retains the context.

There is no new process-wide retained cache. Transient normalization and member
identity results live until the enclosing event finishes. Work remains linear in
historical serialized bytes, with fewer repeated traversals. Frozen digests,
canonical encodings, SDK wire schema and accepted ledger authority are unchanged.

## Measurement

The reproducible harness is `docs/benchmarks/intent-cold-lookup.py`. It uses the
same public-key-only generation-27 copy as the prior diagnosis: 24 streams and
173 events. Each run resets existing in-process history proofs, measures cold
and warm lookup separately, checks the complete returned intent against the
saved SDK response, and verifies that the event-file inventory did not change.
OS filesystem caches are not flushed. No private keys or live writes are needed.

The before/after JSON files accompany this report. These are unprofiled component
measurements, not end-to-end prepare/submit/accept observations. The previously
reported live write times remain the latest live observations.

| Three-run median | Before (`c1a521bd`) | After | Change |
| --- | ---: | ---: | ---: |
| Cold fingerprint lookup | 11.532 s | 9.152 s | 20.6% less time |
| Warm fingerprint lookup | 0.081 s | 0.076 s | Essentially unchanged |

Cold ranges were 11.526–11.688 s before and 9.148–9.245 s after. Both sides have
the same history inventory digest and reproduce the full expected intent.

Final named verification: 136 tests passed across event decoding, historical
reuse, fingerprint lookup, source presence, binding, SDK wire catalog and response
versions. A prior binding/change-set run passed 52 cases, including the separate
27-case change-set integration scope. Scoped Ruff check/format and Mypy passed.
No full suite, journal goldens or canonical-checkout tests ran. The independent
[review](reviews/intent-cold-validation-2026-09-06.md) approved the final source;
the parent ran the final tests and benchmark. No wire re-pin is needed.

Two broader experiments were discarded: caching exact payload calculations
across a stream and caching entire repeated intent models. Their extra
serialization/admission work did not justify their complexity. They are absent
from the final implementation.

## Design elements to revisit

1. **Separate immutable payloads from protocol transitions in a future journal
   version.** Every stream in this copied workload repeats one unchanged payload;
   some payloads exceed 2 MiB, and one stream has 82 events, mostly changing
   publication expectations. Store a committed immutable payload once and bind
   small transition records to it. Preserve old event verifiers and bytes; do
   not rewrite historical digests under the new format. This needs a design for
   payload retention, missing-body refusal, transition reconstruction and crash
   recovery, rather than a transparent serializer substitution.
2. **Define the restart trust boundary for a persisted current-intent index.**
   Today's warm index is an in-memory result of full validation, checked against
   fresh event bytes. An unsigned persisted summary cannot simply become trusted
   after restart. A durable verification receipt would need explicit integrity
   and verifier-version binding; startup warming instead moves the same work and
   needs truthful readiness. Neither has been implemented here.
3. **Decide whether historical intent integrity must gate unrelated writes.**
   Current duplicate-active-fingerprint lookup validates every retained stream,
   including terminal work. A corrupt old stream can therefore block a new
   unrelated intent. Separating active protocol readiness from historical audit
   could narrow foreground work, but changes failure semantics and requires a
   reliable way to establish which streams are active. This pass preserves the
   existing refusal behavior.

Projection prebuild and accepted checkpoint recovery remain separate measured
acceptance targets. Rust would accelerate traversal, but the journal and readiness
decisions determine how much historical traversal a routine write must request.
