# Performance follow-ups and review-flow integration

All changes remain on `codex/state-loop-design` in the isolated performance
worktree. This pass merged primary `playbill` at `24273db9`, preserving the newer
review notes, ledger mirror and rationale semantics. Integration commit:
`534da597`. It fixed the new required proposal-message argument in the batching
test and regenerated the combined served-surface inventory under the existing
maintainer-authorized integration scope. No daemon deployment or push occurred.

## Accepted-history membership

Commit `8f6df4b4` adds a lazy unique-OID index bound to one immutable recovered
state. It removes repeated linear scans for accepted coordinates and evaluation
times. Unknown and duplicate OIDs still refuse; complete coordinates, strict
repository-path checks and exact root/compiler checks are still performed.
Successful recovery replaces the epoch; failed recovery adds no authority.
The index retains only references to existing metadata, up to 65,536 generations;
larger histories fall back to the complete scan. No tree or body is duplicated.

| Generations | Before, per lookup | Warm indexed | First indexed lookup |
|---:|---:|---:|---:|
| 27 | 26.97 µs | 25.55 µs | 27.29 µs |
| 100 | 28.96 µs | 25.77 µs | 33.54 µs |
| 1,000 | 49.21 µs | 25.85 µs | 79.62 µs |
| 10,000 | 240.26 µs | 26.59 µs | 565.62 µs |

These are synthetic metadata microbenchmarks, including strict path resolution
and coordinate construction. Before/after use identical histories and verify
exact coordinate equality. They exclude recovery, Git reads, HTTP and projection
checking. Benefit at the current program generation is small; the main gain is
scaling. Cold index construction remains linear.

- [Measurements](benchmarks/accepted-history-lookup-2026-09-05.json)
- [Reproducible harness](benchmarks/accepted-history-lookup.py): run from this
  worktree with its root, `src`, and client package `src` on `PYTHONPATH`.

## Pending-intent fingerprint lookup

Commit `878b09d5` adds a compact proof cache that outlives the 128-stream parsed-event cache, preventing
sequential scans from evicting and reparsing every intent once that limit is
crossed. It stores actor, create fingerprint and pending state with a
length-framed digest of the fully validated event-byte sequence. Every lookup
still enumerates and rereads/hashes all event files before filtering by actor.
Only the ordinary complete validator publishes proof entries. Changed bytes,
appends, truncation, suspicious paths and eviction fall back to that validator;
matching intents still load detached full snapshots.

| History | Before | After |
|---|---:|---:|
| 32 streams / 96 events, cold | 32.13 ms | 30.76 ms |
| Same, warm | 5.79 ms | 5.06 ms |
| 160 streams / 480 events, cold | 171.59 ms | 156.87 ms |
| Same, warm | 168.15 ms | 29.87 ms |

The larger warm lookup uses **82.2% less time**, with repeated event decodes
falling from 480 to zero. Medians use three paired samples on identical files;
cold differences are not treated as a separate demonstrated speedup. No
small-history regression was observed. This is a local store benchmark, not a
whole create/prepare/submit or live-daemon measurement.

The compact cache holds at most 4,096 streams and retains no payloads or event
models. The 160-stream fixture used 44,800 logical field bytes including paths;
this is not a Python heap measurement. Above the cache bound, eviction can still
cause repeated validation. Disk-byte work remains linear in total event history.

- [Measurements](benchmarks/authoring-fingerprint-2026-09-05.json)
- [Reproducible harness](benchmarks/authoring-fingerprint.py)

Independent review found no issues. All 52 focused history/intent/lookup tests
passed, including nine new cases covering capacity, unrelated-actor tampering,
same-size timestamp-preserving changes, valid rewrites, append/truncate, prose,
duplicate fingerprints, live insertions, first-refusal ordering and eviction.

## Combined-branch submission recheck

Three fresh 162-Claim fixtures on the integrated branch measured a median
**1.117 s prepare + 3.018 s submit = 4.135 s complete prepare/submit**. All
submissions retained the exact prepared certificate and 162 members. Git still
used two `hash-object` processes per submission; the new review flow additionally
writes an evaluation note. No mirror was configured.

The previous performance-only branch measured 3.891 s complete; this integrated
recheck is about 6% higher. It is not attributed to one change and is not a new
speedup claim. The earlier pre-batching baseline was 8.801 s, measured separately.
All are local diagnostic measurements, excluding setup, HTTP, review, activation
and remote publication; they are not live-daemon latency guarantees.

- [Raw integrated samples](benchmarks/integrated-submit-2026-09-05.json)
- Harness: `/private/tmp/playbill-git-submit-benchmark.py --label integrated-final --mode current`

The copied program-instance projection check returned byte-for-byte-equivalent
complete result objects to the prior pass: **1.836 s cold, 0.054 s warm**. Its
27-generation recovery/setup took 19.915 s and is excluded from those timers.
These are one cold/warm pair, not medians. The prior pair was 1.438 s / 0.047 s;
this recheck demonstrates retained behavior and still-fast warm reads, not a
new latency reduction. No production state was written.
[Raw projection-check results](benchmarks/integrated-projection-check-2026-09-05.json).

## Integrity and verification

The signed ledger and accepted Git tree remain authority. The accepted-history
index is rebuilt from recovered state. Pending authoring intents remain governed
by their existing operational event journals; their lookup cache never substitutes
for accepted state or durable event bytes.

Integration checks: 81 Git batching, proposal-note, ledger-mirror, authoring-binding
and public-surface cases passed; 320 proposal-prose, approval-concurrency, prose
guardrail and prepared-lowering cases passed. Seven SDK acceptance/refresh and
actual-receipt-coordinate cases passed. A new repeated-rationale-edit regression
passed (`3699897b`), ensuring equal payload digests cannot return stale prose from
lowering reuse. The accepted-history change passed six dedicated and 21 scoped
lineage, batch-read, blob and recovery cases. Ruff and focused Mypy passed for the
index and intent store. Independent integration and index source review found no remaining blocker.
No full suite or golden journal corpus ran.

## Next architectural slice

[Remote ledger publication](ledger-publication-follow-up.md) now sits in the
write request path. Moving it to a coalescing publisher should remove network
waiting from routine writes, but first requires exact ref snapshots, accurate
proposal/note freshness, serialized atomic pushes and recoverable scheduling.
This pass records that design; it does not change remote publication behavior.

Activation recovery remains intact: the prepared bundle is not a complete
replacement proof. Compact projection manifests still require accepted retention
and coordinated parser/export/evidence support; see the
[manifest design](projection-manifest-follow-up.md). General SDK revision/evidence
conveniences also remain separate work. Neither is claimed implemented here.
