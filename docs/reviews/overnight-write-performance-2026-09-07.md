# Code Review

## Verdict

Approved with comments.

Implementer self-review; no independent review is claimed. This round follows `e14325e0` on `codex/write-reconstruction-performance` and adds three bounded work-reuse improvements plus one cache invalidation correction. It does not merge or deploy the performance branch.

## Manual Review Priority

- Priority: P1
- Reason: Shared submission and accepted-index construction, with evidence-derived cache correctness.
- Suggested Human Review Focus: Same-operation tree reuse; complete inventory validation; the citation dependency boundary; review alias invalidation on Git encoding changes.

## Scope Reviewed

- Changed files: `playbill/git.py`, `proposal_note_cache.py`, `proposals.py`, `projection_tree.py`, and `projection_delta.py`.
- Tests examined: `test_proposal_note_cache.py`, `test_review_commit_identity.py`, `test_proposals.py`, `test_projection_delta.py`, `test_projection_tree_limits.py`, `test_assembler.py`, and `test_citation_retirement_relations.py`.
- Untracked files: this report and its benchmark JSON before the evidence commit.
- Commands run: named pytest scopes below, Ruff, mypy on five changed source files, `git diff --check`, sequential paired benchmarks, and a program-instance cold-rebuild comparison. Tests ran in the isolated worktree, never the canonical checkout; no full suite or golden corpus.

## Findings

No findings.

Self-review caught a missing dependency in the preceding note-cache batch: unchanged proposal evidence is insufficient to reuse Git review aliases if local commit encoding changes. This round fixes it and adds a regression test before the performance measurements.

## Complexity Assessment

Submission removes one full Git-tree transfer when the proposed and current bases are identical: approximately 26 MB across 4,320 files in the benchmark parent. Rebases still read their distinct proposed base. Successor construction removes a duplicate full Git inventory listing without weakening inventory or selected-blob validation.

For changesets containing neither Claims nor CaptureContracts, citation relations are carried in the copied database. This avoids reading and validating unaffected contracts, rebuilding the global relation slice, and replacing unchanged relation rows. Claim/contract changes still use the existing incremental relation builder. These improvements reduce repeated work; remaining ledger verification, database copying/digesting, and evaluation-state detachment still scale with retained state.

Each warm note-index load now reads Git's effective commit encoding with one additional subprocess. This is a small measured regression for correctness; it does not repeat alias derivation unless the context changes. No new retained cache or durable store is introduced.

## Architecture Assessment

Read the changes in this order:

1. `git.py` and `proposal_note_cache.py` (`e6e3e9d8`): freshly read the mutable commit-encoding context at each load. A change invalidates the previous alias/membership snapshot; validated evidence records remain reusable by exact bytes. Cache reset also resets context. Existing explicit identity/date overrides and isolated Git environment remain authoritative for alias derivation.
2. `proposals.py` (`8e9a28e8`): after reading the current accepted tree and checking the actor, reuse that tree only when its exact Git OID equals the requested proposed base. This is reuse within one operation, not a new cross-operation trust assumption.
3. `projection_tree.py` and `projection_delta.py` (`13f3a4f6`): factor the gated inventory consumer into a private helper. Delta validation retains the successor inventory it just fetched and passes that same tuple to the helper. The public tree reader still fetches its own inventory. Whole-inventory path, collision, mode, count and size gates, selected-blob content checks, and resource limits remain intact.
4. `projection_delta.py` (`19cb1bf0`): determine citation work from the verified changeset member paths. Fetch the contract inventory and rebuild citation relations only for Claim or CaptureContract changes. Other artifact compilers own their own rows; existing fixture/presentation fallbacks protect arbitrary row ownership. Passing no replacement relation slice preserves the verified parent's rows.

The signed ledger and accepted Git tree remain authoritative. Accepted indexes remain disposable and reconstructible. No wire/schema/version changes, SDK changes, dual-write protocol, or Rust dependency are required.

## Test Coverage Assessment

- Review cache and commit identity: **33 passed**, 20.52 s. Includes changing and restoring commit encoding while evidence stays identical, comparing against the full alias builder.
- Proposal service: **29 passed**, 16.36 s. Includes one current/base read for a non-rebase submission and existing rebase coverage.
- Projection delta, reader limits, assembler: **22 passed**, 22.13 s. Lifecycle parity checks require exactly one successor inventory listing and compare every resulting SQLite row with cold reconstruction.
- Projection delta and citation retirement relations: **15 passed**, 82.93 s. An unrelated Document change starts with nonempty citation use rows, forbids relation reconstruction, verifies carried rows, and compares all database rows with a cold rebuild.
- Ruff on changed source/tests: passed.
- Mypy on all five changed source files: passed.
- `git diff --check`: passed.

Some scopes intentionally overlap across logical commits. Benchmarks ran after these tests, without concurrent testing or profiling.

## Documentation Assessment

Inline comments explain the reuse and dependency boundaries. The factored inventory helper is private and preserves its caller's existing contract; no new public usage guide is needed. The benchmark evidence below distinguishes steady-state warm operations from earlier runs that warmed only review indexing.

## Overall Contribution

This completes the straightforward improvements found in this attribution pass. The changes remove redundant reads and unnecessary dependency reconstruction without weakening the ledger boundary.

### Benchmark

Two samples per arm, run sequentially before/after/after/before. Baseline is `e14325e0`; after is `19cb1bf0`. Each private program copy starts with the same prepared seven-member authoring draft (five Claims and two Subjects), preserving minted IDs. The following Document change has identical bytes in every arm. Both the review index and evaluation-state cache are warmed before timing. Startup, instance copying/opening, intent creation, body storage, and oracle validation are excluded. The Document service call includes its own candidate-tree preparation. No HTTP transport or attached workspace is included.

| Operation | Before | After | Mean reduction |
| --- | --- | --- | --- |
| Seven-member submit | 2.95–3.01 s | 2.57–2.58 s | 14% |
| Seven-member accept | 4.32–4.48 s | 4.24–4.27 s | 3% |
| Unrelated Document submit | 2.61–2.64 s | 2.23–2.24 s | 15% |
| Unrelated Document accept | 4.28–4.45 s | 3.66–3.71 s | 16% |
| Warm note-index load | 20–21 ms | 36–40 ms | Additional encoding check |

The small acceptance difference for a Claim-changing proposal should be treated as a modest result, not a precise general prediction. The larger unrelated-Document gain reflects the skipped relation work. These are **fully warm** operations and must not be compared directly with the preceding report's submit numbers, which warmed the review index but paid the first evaluation-state cache build.

All four arms produced identical accepted Git OIDs, semantic roots, generation roots and compiler identities at both steps; only the local repository path differs. Each arm matched review memberships and candidate reverse memberships against the cold note builder and ended with 96 commit groups. The final optimized program index also matched every row in all 10 SQLite tables against a fresh ledger-only rebuild (34,926 rows).

The [raw results](overnight-write-performance-benchmark-2026-09-07.json) record exact coordinates and timings. Four local runs are useful for this regression comparison, not a capacity or latency-SLA study.

An attribution-only profile before these changes also showed costs that remain: repeated preflight/submission evaluation, whole-state defensive copying, generation preparation and database copying/digesting. The profiled submit paid the first evaluation-state build and incurred profiler overhead; its 7.53 s submit / 6.80 s accept totals are not user-facing latency estimates.

## Open Questions

None blocking this batch.

## Suggested Follow-Ups

- Review and integrate the accumulated performance branch, then measure the installed SDK/daemon workflow. Local service measurements omit HTTP and attached workspace costs.
- The next substantial submit improvement is reuse of prepared evaluation results across preflight and submission. That needs an explicit validity contract covering exact base/candidate content, bodies, queries, actor, policies and time; skipping a second evaluation on assumption is insufficient.
- Evaluation-state deep copies remain measurable. Removing them requires an ownership or immutability design because nested models are mutable; do not expose a shared writable snapshot to callers.
- Retain fresh authority checks and cold reconstruction. Consider further verified-prefix/index-proof reuse only with an explicit dependency and corruption-detection contract.
- Git buffering and temporary-file stdout experiments did not show a reliable material improvement. Neither change was adopted. No justification for a Rust rewrite emerged from this pass.
