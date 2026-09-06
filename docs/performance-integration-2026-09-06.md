# Performance and Fable integration

The reviewed integration is locally merged into `playbill` and deployed at
`2215f370756e86af41814474333ee9a0e87c33ae`. The daemon retains its state root,
credentials, capability ceiling and three registered instances. One listener
was verified after graceful stop/start. Nothing was pushed.

## What landed

| Scope | Review basis | Result |
|---|---|---|
| Performance branch | `c0b47fa8`, integrated with current Playbill at `e5604953` | Bounded World/backing reads, accepted lineage/OID reuse, conservative lowering reuse, compact byte-verified intent lookup, batched Git blobs, explicit SDK acceptance/floor maintenance, background ledger publication |
| Fable Git review flow | `f8ce3336..24273db9`, already on primary | Corrected missing settled archives (`a03bd632`), colliding proposal notes and advisory integrity/recovery (`1ea0f40c`) |
| Fable Source procedures | `24273db9..5a2e3a7b`, already on primary | Corrected actual-capture policy eligibility and unsupported coherence (`dc029b2c`) |
| Review-index follow-up | `5203a525` | Reuse bounded immutable candidate summaries after fresh byte verification |

Fable's branches had already landed when this work began. Their presence on
primary was not treated as review approval. The integration branch received
corrective commits before the primary fast-forward and daemon rollout.

The ledger and accepted tree remain authoritative. No accepted digest rule,
compiler coordinate or stored receipt was rewritten. Review refs/notes and
performance indexes remain rebuildable derivatives; publication watermarks
remain operational request order, not accepted generations.

## Defects corrected during review

- A candidate settled before its first publication could disappear from the
  archive. Both open and settled refs now rebuild from durable evidence.
- Distinct admissions can share a Git commit, including distinct candidate
  digests whose timestamps differ only below one second. Notes now preserve
  proposal-ID-ordered admission/evaluation pairs and distinct signed approval
  payloads. Strict activation checks original and materialized advisory aliases.
- Crash recovery now repairs valid incomplete note groups across both alias
  types. Edited/unrelated records refuse. Hash-only review identity discovery
  uses Git's own identity normalization and leaves no orphan commits on refusal.
- Source execution could select a capture that its accepted replayability rule
  disallowed. Actual capture eligibility now checks replayability and age before
  binding outputs. Unsupported cross-source coherence refuses before execution.
- Candidate evidence readers now reject a canonical record stored under another
  candidate's digest filename.

## Measurements

| Measurement | Before | After |
|---|---:|---:|
| Live read of the same 41 task/finding attributes | 52.149 s sequential reads | 5.873 s first prefetch; 5.196 s repeated prefetch |
| Local typed attribute access after prefetch | — | ~0.001 s |
| Live connect + World discovery | 8.665 s | 7.535 s |
| Complete review index, matched final-code benchmark | 4.427 s without summary reuse | 4.345 s cold; 0.689 s warm median |

The live samples use the same accepted coordinate and identical claim IDs,
values and verdicts. They are single operational observations; the before
sample overlapped test activity and is not a controlled percentile benchmark.
Connection/World time is separate from the attribute-read row. No subsecond
network read is claimed.

The copied-world index benchmark covers 42 admissions, 82 groups and 27.5 MB of
candidate files. All review OIDs and complete grouped-note fingerprints match
between cache-disabled, cold and warm modes. Warm construction performs zero
candidate decodes while rereading/hashing every candidate file; 40 compact
entries account for 274,293 retained bytes. Limits are 512 entries and 8 MiB of
accounted fields, not interpreter heap. Recovery and note publication are outside
that timer. A separate pre/post integration comparison also preserved all 82
groups and note fingerprints across the Git identity refactor.

Reproduction and raw samples:
[review-index harness](benchmarks/review-candidate-summaries.py),
[measurements](benchmarks/review-candidate-summaries-2026-09-05.json).
Earlier workload-qualified results remain in the linked performance reports;
the 162-new-Claim synthetic submission is not comparable to the live replacement
checkpoint below.

## Live project-state checkpoint

The manager used the World SDK to inspect existing task completion criteria and
prepared an 18-Claim reconciliation: seven task status/note pairs plus four
integration findings. Its exact candidate has eleven Claim replacements, seven
new Claims and four new Subjects. No ClaimType, authority policy, adoption or
release field changes. Evidence is explicitly the manager's attributed
engineering assessment, not independently attested execution.

Observed preparation took 15.736 s; submission took 29.831 s; exact review
retrieval took 0.858 s. These are still too slow for routine small writes.
An unnecessary all-attribute prefetch in the checkpoint script was also narrowed
to the two fields needed for future updates; that script improvement is not
included in the recorded first-checkpoint timing.

Independent review approved exact candidate
`sha256:479ab7252fedd09f4d629674257f096b778a9103551a58c8f25d7b344b22ea12`.
It was activated as accepted Git coordinate
`bc46256433c8bcfd68c6bcbc17c4879a55185123`. All 18 Claims read back with the
expected subject, predicate, value and supported verdict at that coordinate.
Activation took 37.778 s; the bounded readback took 2.652 s, excluding its
6.902 s connection setup. These are operational samples, not isolated benchmarks.
The accepted checkpoint preserves one completed and six partial implementation
assessments without adopting new priorities. Detailed operational receipts and
the exact independent review are retained locally in `docs/world-model/`.

## Verification and review

All tests ran in isolated worktrees/state roots. No canonical-checkout tests,
full suite, journal golden corpus, parity milestone or push was performed.
The served-surface inventory was reconciled under
`2026-09-05:performance-effectful-integration`.

- SDK/authoring integration: 64 passing cases, including actual provider fixtures.
- Source correction: 41 passing post-fix cases; original Fable baseline separately
  passed 78 cases.
- Git identity/grouping/no-orphan scope: 66 passing cases across both Git formats.
- Notes/archive/approval/prose integration: 37 passing cases.
- Candidate-summary/group/archive/prose scopes: 48 passing cases.
- Final mirror-worker/failure/HTTP barrier scope: 37 passing cases.
- Complete CLI/guardrail run: 782 passed, nine failures resolved through affected
  reruns and process-table access for four unchanged recovery checks.
- Final guardrail/architecture run: 465 passed, four stale architecture inventory
  expectations corrected; all ten affected tests then passed. The enqueue and
  blocking-barrier distinction is now explicitly checked.
- Scoped Ruff, formatting, Mypy and whitespace checks passed. No outstanding
  test failure is known in these named scopes.

Independent reports:
[Git flow](reviews/fable-git-flow-2026-09-06.md),
[Source slice](reviews/fable-source-2026-09-06.md),
[SDK integration](reviews/performance-sdk-integration-2026-09-06.md),
[archive/Source fixes](reviews/archive-source-fixes-2026-09-06.md),
[grouped notes](reviews/grouped-review-notes-2026-09-06.md),
[summary cache](reviews/candidate-review-summary-2026-09-06.md).
The final clock, route and publication-test changes also received independent
review; discovery rules and the async-route blocking check remain intact.

## Remaining work

This release completes the workspace-file Source observation slice, not the
whole effectful procedure loop. It exposes read receipts and selected capture
digests on direct and Line runs, with independent acquisition coherence.
`emit_capture`, `propose_change_set`, inbox/settlement terminals and reading or
SDK derivation carry remain separate work.

Live checkpoint latency, cold recovery/orientation, broad Claim-view work and
historical Git/note work need further measured reduction. Compact projection
manifests remain the documented retention/export design slice. The managed
instance can amortize warm state, but those remaining costs are not hidden by
calling the current workflow low-friction.

A bounded profile on a copied generation-26 instance identifies two next scopes:

1. Attached workspace advertisement still performs full historical review-ref
   reconciliation. A warm profiled reconciliation took 8.830 s and issued 449
   Git subprocesses: note checks cost 4.916 s and historical review-commit
   materialization 2.999 s. The summary cache only accelerates index construction.
   Make reconciliation incremental or move recoverable history work off the
   foreground path, and batch note/object reads while retaining strict activation
   checks and grouped-note integrity.
2. Warm checkpoint preflight took 4.586 s under profiling, with 4.416 s in proposal
   evaluation. Repeated effective-Claim scans cost 1.572 s, 22 accepted-referent
   scans cost 1.416 s, and parent dependency-state construction cost 1.017 s.
   Reuse immutable accepted-coordinate state and avoid redundant evaluation of
   unchanged inputs while checking current authority and settlement conditions.

These are diagnostic copied-instance profiles, not a decomposition of the live
37.778 s acceptance or a promised end-to-end speedup. The copied preflight
reproduced the exact candidate digest, but omitted reference assertions because
the public intent view did not return them; reference-validation and transport
costs are therefore outside that experiment. The intent-view round-trip gap
also needs a separate contract check.
