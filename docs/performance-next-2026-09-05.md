# Next and coverage request reuse

This batch targets the measured SDK `next` loop on the program instance. It
follows the authoring-history pass documented in
`performance-history-state-2026-09-05.md`. This is engineering evidence, not an
accepted roadmap update or a governed projection.

Build `561b34b54da2992a8e6e8916a6de6e1bdc640f17` is landed locally and
deployed. The daemon reports three registered instances and one socket listener
(PID 61830 at verification); its checkout is clean. Nothing has been pushed.

## Result and scope

The matched isolated warm `next` service call fell from **16.957 s to 7.001 s**
(59% less time, 2.42 times faster). The preceding coverage service call fell
from **5.226 s to 2.658 s** (49% less time). Both comparisons preserve their
complete captured responses. The `next` oracle contains 45 rows: one stale
projection backing, 20 unobserved-source groups, and 24 unregistered blocks.
No repair was executed to make the queue smaller.

These are diagnostic samples on one local machine and one instance, not latency
guarantees. Cold work remains substantial. The first `next` call in a fresh
benchmark process fell from 44.564 s to 34.572 s; coverage's first call remained
about 28 s. Instance recovery is outside those timers. Tests sometimes ran
concurrently, so the measurements establish a useful magnitude rather than a
stable percentile.

## Changes

1. `playbill_query.py`: a private reader reuses parsed Claims, ClaimTypes, and
   assembled fact rows within one evaluation. A live-only read and an
   include-retired read share existing rows while assembling any missing rows.
   Public standalone fact-building calls still create a fresh reader.
2. `playbill_next.py`: claim health, citation commitments, projection backing,
   and dependency folds share that reader and the already loaded Claim
   population. Existing per-fold loading remains available if the initial
   population load fails. Visibility filtering and retirement handling stay
   with the folds that own them.
3. `playbill_coverage.py`: coverage reads Claim artifacts from the verified
   accepted tree without constructing their inspection projections, retains
   parsed capture envelopes for the remainder of the request, and decodes each
   observed source once when calculating citation windows.

All reuse introduced here ends with the request. Later calls recheck body
availability, source observations, and marker observations. There is no new
persistent cache, wire format, artifact schema, journal migration, or provider
re-pin. No SDK gathering code changed: replaying the captured coverage response
showed only about 0.10 s of local gathering work, which made server work the
better target.

## Measurement method

The actual SDK baseline was 40.644 s overall, including a 16.341 s `next` HTTP
request. Identical request repeats took 15.228 s and 15.920 s. A later warm
gathering measurement spent 5.048 s in its coverage HTTP request; that warm
measurement does not explain the entire earlier cold gathering interval.

For controlled service comparisons, the program ledger, CAS, exhaust, and
derived stores were copied to scratch storage with public verification keys
only. Recovery verified 26 generations. No production private signing keys
were copied and no live authoring writes were used. Baseline code was exported
from `d217e1c5`, whose implementation matches the deployed `17b05d29`.

The identical captured `next` request pins the accepted coordinate, attestation
head, observations, and evaluation time. Equality checks compare the entire
JSON result, not only row count or reason distribution. Its result digest is
`sha256:6dd0c5056456058f89ae4f6d6e4c9029f4f9634e5cd77235ca0926f82228e1b2`.

| Service operation | Before | After | Equality |
| --- | ---: | ---: | --- |
| Next, first call after loading a fresh process | 44.564 s | 34.572 s | Complete 45-row result |
| Next, warm call in the same process | 16.957 s | 7.001 s | Complete 45-row result |
| Coverage, first call | 27.545 s | 28.164 s | Complete captured response |
| Coverage, warm call | 5.226 s | 2.658 s | Complete captured response |

Profiling is used to locate costs; profiled times are not compared with
unprofiled timings. Before the fix, one profiled `next` evaluation built query
facts three times and listed the Claim population three times. The differential
fixture now observes one population load and two fact-row builds, versus three
loads and six builds through the original independent helper paths. On the
program coverage request, capture parsing fell from 3,260 to 1,630 calls,
content decoding from 953 to 41 calls, and inspection Claim listing from one
call to zero. The 41 decodes include the overlay's 21 sources and 20 sources
with citation windows.

## Live SDK verification after deployment

| Call | Coverage HTTP | Next HTTP | Complete SDK next |
| --- | ---: | ---: | ---: |
| First SDK call after restart | 21.712 s | 7.018 s | 28.845 s |
| Following warm SDK call | 2.002 s | 6.312 s | 8.420 s |

SDK connection setup precedes these timers. Both calls returned 45 rows with
the original reason counts. Their result digests differ because these are fresh
observations and evaluation times. Separately, two replays of the original
fixed request took **5.969 s and 6.174 s** through HTTP and matched the original
complete JSON result, including its digest and accepted coordinate.

The earlier fixed-request live repeats took 15.228 s and 15.920 s. The new
repeats therefore use approximately 61% less time, consistent with the matched
isolated comparison. The original 40.644 s full SDK sample and the new 8.420 s
warm sample have different cache conditions; they are not a controlled fivefold
speedup claim. Coverage's first request remains the largest visible pause.

Deployment used the normal stop/start lifecycle with the existing state root,
socket, credentials, and capability ceiling. Conflicting client-target
environment variables were cleared before launch, avoiding the previously
observed restart-endpoint defect. No accepted generation, proposal, or queue
repair was created. The fixed response still names program generation 25's
accepted Git object `6cccbcc35f46a04cfd1a95e593cd65f298b27cf6`.

Operational receipt:
`docs/world-model/next-performance-deployment-2026-09-05.json`.

## Review and verification

Review order: query reader ownership and parsing; `next` fold integration and
fallbacks; coverage tree/inspection parity, coordinate validation, and mutable
inputs. Independent review examined both integrations. Existing subject-gating
and dependency-repair tests were updated to inject facts through the new reader
method; their semantic assertions remain unchanged.

The three implementation commits are `9caec532` (query facts), `3caa9b79`
(`next` integration), and `561b34b5` (coverage inputs). Review the complete
implementation range `d217e1c5..561b34b5` in that order.

The coverage parser explicitly selects the verified coordinate's compiler codec.
A focused legacy test confirms the same compact Claim reconstruction as the
projection parser and the same preexisting downstream `.yaml` path refusal in
`AcceptedClaim`. This batch does not expand full historical coverage support.

Named isolated-worktree scopes:

- Query fact reuse and existing query execution: 16 tests passed.
- New next request parity/freshness/fallback regressions: 6 tests passed.
- Existing next queue, projection next, closed-loop repairs, and delta/prefix
  checks: 84 passed initially; the two test injection fixes each passed on
  targeted rerun (86 distinct tests).
- Coverage input parity and existing scanner, adapter, Claim citation, and
  retirement relation scopes: 52 distinct tests passed, including the final
  historical-codec regression. All five new tests passed after that refinement.
- Final Mypy passed in 282 source files. Scoped Ruff, formatting, and
  `git diff --check` passed.

Tests cover complete response equality, retired Claims, historical coordinates,
removed/restored capture bodies, corrupt CAS, changed source/marker observations,
caller mutation isolation, and initial-load fallback. No full suite, golden
corpus, or canonical-checkout tests were run.

## Remaining work

First-use history and evidence reconstruction still dominate cold requests.
Warm `next` still assembles a full accepted fact population. A later pass should
measure a shared accepted-state index with fresh per-request evidence inputs,
then assess whether coverage and queue evaluation can share work across their
service boundary. Neither improvement requires choosing Rust first.

Source files are currently gathered more than once, but the measured local
gathering cost is around a tenth of a second. Reducing that repetition remains
useful hygiene after the larger server costs. This batch does not complete
pending-list pagination, bounded history reads, preflight reuse, or incremental
audit/curation work.

## Local evidence

- `/private/tmp/playbill-next-benchmark.json` and `playbill-next-queue.json`
- `/private/tmp/playbill-profile-next.py`
- `/private/tmp/playbill-next-instance/matched-baseline-{first,warm}.json`
- `/private/tmp/playbill-next-instance/matched-after-{first,warm}.json`
- `/private/tmp/playbill-coverage-{baseline,after}.json`
- `/private/tmp/playbill-sdk-gather-benchmark.json`
- `/private/tmp/playbill-sdk-gather-replay.json`
- `/private/tmp/playbill-next-live-after.json`
- `/private/tmp/playbill-next-rollout.json`

The scratch files are local benchmark evidence; they are not durable product
state or release assets.
