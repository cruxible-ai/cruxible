# Code Review

## Verdict

Approved with comments.

The accumulated performance branch has completed independent reviews of accepted-index construction and proposal/review machinery. One advisory alias-parity finding was corrected in `4b72c3ba` and reviewed by the integration manager. This report records the exact implementation and installed-package validation; deployment completion is recorded separately below.

## Manual Review Priority

- Priority: P1
- Reason: Shared acceptance and proposal review paths, incremental derived-state construction, and daemon rollout.
- Suggested Human Review Focus: Verified parent/delta provenance, row ownership and cold fallback, exact evidence freshness, interrupted admissions and alias groups, deployment head preservation.

## Scope Reviewed

- Implementation range: `caa174dde80677a9bffab245a23b2bd33eb15016..4b72c3ba` on `codex/write-reconstruction-performance`; later commits in this closeout contain evidence only.
- Changed code: thirteen Core files covering accepted-index assembly/storage, Git inventory reuse, proposal evidence/cache/index and instance/service wiring. SDK sources are unchanged between comparison arms.
- Independent reviews: [accepted-index review](batch0-index-independent-review-2026-09-07.md) and [proposal review, finding and fix](batch0-note-review-and-fix-2026-09-07.md). Both initial reviews examined `caa174dd..612be6c2`; the proposal reviewer then authored the bounded fix, which the manager inspected before commit.
- Untracked files reviewed: these three reports and installed benchmark/deployment evidence produced for this batch. Existing unrelated canonical-workspace files are preserved.
- Commands run: focused tests described below, changed-file static checks, Git diff checks, offline wheel builds, wheel/source/installed-byte comparisons, authenticated Unix HTTP SDK replay and restart validation. No tests in the canonical checkout, full suite or golden corpus.

## Findings

No unresolved findings.

The initial note review found that duplicate admission IDs under foreign evidence filenames could preserve more original/advisory aliases in the cold builder's groups than the new candidate lookup returned. Fix `4b72c3ba` derives the inverse from actual groups, preserving all aliases and copied-return isolation. Two regressions cover the tolerated duplicate shape and mutation of a detached inverse. This does not change normal writer output or tighten historical evidence admission.

## Complexity Assessment

The accepted index now compiles supported changed artifacts and carries unrelated citation rows. The proposal note cache reuses exact-byte validation and updates changed alias relationships. Whole-world Git inventories, SQLite snapshot/digest work, explanation rebinding and some relation/evaluation work remain; attached review reconciliation also remains broad. The installed benchmark confirms material acceptance gains without establishing a constant-time write path.

## Architecture Assessment

The ledger remains authority. No new public format, SDK wire schema or historical digest rule is introduced. Immutable successor publication, current-base checks, signed generation construction, approval verification and acceptance CAS remain in the common service layer. Standalone proposal services and unsupported projection ownership shapes retain cold reconstruction.

Normal delta activation does not reopen unchanged CAS bodies; it carries verified parent-derived facts. This is not a claim that every CAS object is freshly checked on every acceptance. Cold reconstruction still reads them, and touched inputs pass ordinary validation.

The next architecture program is described in the [scope audit](world-scope-performance-audit-2026-09-07.md). It should centralize derived-state ownership and partition-aware dependencies before adding further standalone caches.

## Test Coverage Assessment

- Independent proposal/cache/grouping/identity scope: **47 passed**, 60.87 s.
- Independent projection delta/tree limits/assembler/ExhaustPromotion scope: **26 passed**, 41.82 s.
- After alias fix, cache/grouping scope: **25 passed**, 54.32 s. Overlap is intentional; these are separate logical verification scopes, not 98 unique tests.
- Alias fix Ruff check/format and mypy: passed.
- Final changed-file Ruff check/format: passed (19 files); mypy: passed (13 source files); Git diff checks: passed.

The installed workflow exercises matching wheels in separate server and client environments. The client environment cannot import Core. Two before and two after copies replay the same prepared seven-member draft, using newly minted private runtime credentials whose actor label matches the accepted principal. Each instance has a real private attached Git workspace, no remote mirror, and no connection to live daemon storage. Existing fixture policy requires no approval attestations; signatures/approval-group behavior are covered by focused tests, not claimed as part of this zero-attestation replay.

## Documentation Assessment

The earlier individual reports retain their original cache conditions and comparisons. This report adds the full installed/attached boundary and does not substitute these numbers into the previous service-only benchmark. Raw evidence strips private token material and records package provenance and exact accepted coordinates.

## Overall Contribution

This closes the existing local performance implementation and establishes a reproducible installed baseline for the derived-state design work.

### Installed benchmark

Four sequential before/after/after/before runs compare baseline `caa174dd` with final implementation `4b72c3ba`. The same prepared draft preserves minted Claim IDs. Each replay measures first and repeated preparation before submission, so submit benefits from same-intent preparation reuse. This is a different boundary from the earlier service-only fresh-submit experiment. Setup, package installation and instance copying are excluded; there is no profiler or concurrent test workload.

| Installed authenticated SDK operation | Before | After |
| --- | --- | --- |
| First connect/orient after daemon start | 14.73–15.09 s | 14.63–14.93 s |
| First prepare after connect | 1.50–1.52 s | 1.49–1.53 s |
| Repeated prepare, same draft | 0.378 s | 0.376–0.381 s |
| Submit, including attached review surfaces | 11.77–12.85 s | 9.01–9.21 s |
| Accept, including attached review surfaces | 13.20–14.40 s | 5.54–5.64 s |
| Explicit refresh after acceptance | 7.29–7.56 s | 7.33–7.36 s |
| Read back five accepted Claims | 0.88–0.96 s | 0.90–0.95 s |
| First floor refresh at new coordinate | 6.94–7.02 s | 7.02–7.16 s |
| Repeated floor refresh | 0.137–0.138 s | 0.137–0.139 s |
| Connect/orient after second daemon restart | 15.23–15.53 s | 15.07–15.16 s |

Mean submission reduction is 26%; mean acceptance reduction is 60%. No improvement is claimed for preparation, cold orientation, refresh or floor export. Four local samples are not percentile/SLO evidence. Every sample produced the same candidate digest, accepted coordinate and five readback values/verdicts; attached workspace advertisement succeeded. A new daemon and SDK process recovered the accepted intent at that same coordinate in every run.

All installed Core Python files were compared byte-for-byte to the wheel and selected source checkout (197 baseline, 199 after). The SDK's 102 Python files match its wheel and source in the client and both server environments; SDK code is unchanged between arms. Client processes assert Core is unavailable.

The measured acceptance is generation 32, so the default every-50-generation checkpoint is excluded. A separate isolated checkpoint construction/write over that already-read accepted tree cost 53–55 ms across two samples. This measures the component, not a 50th-acceptance latency or tail-latency guarantee.

[Raw timings, coordinate parity and wheel provenance](batch0-installed-benchmark-2026-09-07.json).

### Deployment

`playbill` was fast-forwarded and pushed from `caa174dd` to reviewed implementation `4b72c3bafde9b38f845232121c682aabd3a3539b`. The existing daemon was gracefully stopped, its clean editable checkout moved to that exact commit, and the daemon restarted with the existing state root and credentials. All three instances remain present; all three accepted Git heads are unchanged, including program head `4ec38100e4359bbba90fdd3d1a4a62c39bb367bc`.

The newly installed client-only SDK then connected to the live authenticated daemon, resumed the prior accepted project intent, and read back both existing note values as supported. Single-sample timings: connect 6.598 s, accepted-intent resume 9 ms, status 2 ms, readback 0.953 s. No governed project-state writes were performed during deployment or this live smoke check. Full write measurements above used private program copies, not live project mutations.

[Deployment preservation and live SDK evidence](batch0-deployment-2026-09-07.json). Subsequent closeout commits contain reports only; the deployed implementation remains the exact benchmarked code.

## Open Questions

None blocking this integration. The derived-state component's immutability, evidence revision and partition consistency decisions belong to batch 1.

## Suggested Follow-Ups

- Measure attached review-ref reconciliation separately before assigning all installed submission overhead to it. The installed path's larger submission time moves it up the measurement queue; the difference from earlier service benchmarks is not itself a controlled attribution experiment.
- Keep first/repeated preparation, fresh coordinate refresh, floor refresh, restart recovery and periodic checkpoints separately visible.
- Execute the agreed batch-1 design jointly before beginning the new architecture implementation.
