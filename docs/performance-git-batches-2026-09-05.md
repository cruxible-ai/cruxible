# Submission: bounded Git object writes

The next design pass identified process fan-out as the dominant submission cost.
Implementation commit `0b7a465b` stays on `codex/state-loop-design`; the primary `playbill` branch
and running daemon are unchanged.

| Operation, median of three samples | Before | After |
|---|---:|---:|
| Prepare 162 Claims | 1.058 s | 1.016 s |
| Submit after preparation | 7.752 s | 2.889 s |
| Complete prepare + submit | 8.801 s | 3.891 s |
| `hash-object` processes per submission | 324 | 2 |

Submission uses 63% less time; the complete measured loop uses 56% less time.
The small preparation difference is not attributed to this change. Unlike the
previous lowering-only pass, this is a demonstrated complete-loop improvement
on the synthetic fixture. It is not a new measurement of the live program update.

## Before and after design

| Boundary | Before | After |
|---|---|---|
| Missing Git blobs | One `hash-object -w --stdin` process per unique new blob | Bounded `hash-object -w --stdin-paths --no-filters` batches, exact returned IDs verified |
| Existing Git blobs | Compute expected IDs and batch-check availability | Preserved |
| Proposal provenance | Admitted commit followed by evaluated commit, retaining ancestry | Preserved; only blob storage is batched |
| Ref publication | Build the exact tree, then commit and compare-and-set the proposal ref | Preserved; a failed blob batch cannot advance a ref |
| Activation recovery | Replay/verify after durable publication | Preserved; the prepared bundle is not yet a sufficient replacement proof |

System Git still computes and stores objects. Private temporary files use ordinal
names, restrictive permissions and C-quoted filesystem paths. `--no-filters`
preserves exact binary/CRLF bytes regardless of Git attributes. Each batch holds
at most 256 blobs or 32 MiB of content; one already-admissible oversized blob may
travel alone. Temporary data is removed on success or failure. No artifact,
digest, stored-event, wire or marker format changes; no surface re-pin is needed.

A partial Git failure may leave unreachable content-addressed objects, just as
before. All returned IDs and their count/order must match before index/tree
construction continues. Retry uses the ordinary existing-object check. A detected
collision between differing bytes with the same computed missing-object ID now
refuses rather than silently deduplicating those bytes.

## Evidence and validation

The diagnostic cProfile run of 162-Claim submission took 8.76 s, including 5.84 s
in two `_write_tree` calls and 346 subprocess calls overall. These profiled times
locate work and are not compared directly with the unprofiled benchmark above.

The timed comparison extracts the original `_write_tree` from `378126e0` and
substitutes only that method for the baseline. The current mode uses the new
writer. Each sample seeds a fresh temporary instance, creates the same 162-Claim
payload, and times preparation and submission. Both modes use the previous
conservative lowering cache. All six submissions retain their exact prepared
certificate and return 162 submitted members. Fixture principal/intent identities
are freshly generated, so cross-instance candidate hashes are not asserted equal.
Exact tree equality is separately tested against the former member-by-member
writer in both Git SHA-1 and SHA-256 repositories.

Baseline samples ran before implementation and current samples afterward; they
were not interleaved. These local diagnostic medians are not latency percentiles
or deployment guarantees. Setup, HTTP, review and activation are outside the
complete prepare-submit timer.

Independent source/test review found no blocker. Named verification: 26 Git
batch tests and 14 signing/bootstrap tests passed, plus the qualified-Git-format
activation parity regression (41 total). Ruff, formatting, focused Mypy and
whitespace checks passed. No full suite or golden journal corpus ran. Regression cases include both object formats,
binary/CRLF bytes, normalization and unusual paths, Git attribute bypass, duplicate
and already-present blobs, count/byte bounds, oversized singletons, malformed and
partial Git replies, cleanup/retry, and admitted/evaluated commit ancestry.

- [Machine-readable measurements](git-batch-benchmarks-2026-09-05.json)
- Diagnostic profile: `/private/tmp/playbill-design-profile/{submit,activation}.txt`
- Timing harness: `/private/tmp/playbill-git-submit-benchmark.py`
- Raw samples: `/private/tmp/playbill-git-submit-{before,after}.json`

## Activation decision

The separate diagnostic activation profile took 5.48 s: approximately 1.65 s in
settlement, 1.52 s prebuilding the projection and 1.69 s in refresh/recovery. These
nested profile entries are diagnostic, not an unprofiled activation benchmark.
The synthetic instance had no attached workspace, so this does not explain all
of the earlier live 36.75 s activation-plus-refresh sample.

Directly installing the prepared bundle would skip meaningful checks. In
particular, its principal registry is the **parent** registry; recovered state
must derive the new registry. Recovery also verifies against the parent accepted
signing key and rereads authoritative storage and mutable dependencies.

A future fast path must validate an internal proof bound to the exact instance,
base and bundle; verify stored commit/tree/record/roots, parent-key signature,
approvals/laws, fresh body and producer-receipt dependencies, canonical generation
note and serving publication; derive target-tree principals; and install state
monotonically while main still matches the proved successor under the appropriate
locks. Startup, incomplete publication, lost CAS, external successors and missing
proofs continue through ordinary recovery. The current bundle does not supply
that complete proof, so this pass preserves recovery rather than claiming its
1.69 profiled seconds are safely removable.

Compact manifests, pending-intent indexing and general authoring conveniences
remain the separate follow-ups described in the previous state-loop report.

Final integration check: `playbill` advanced independently to `24273db9` during
this pass. This branch has not been rebased onto that newer head or deployed;
benchmark and verification claims apply to the isolated branch described above.
