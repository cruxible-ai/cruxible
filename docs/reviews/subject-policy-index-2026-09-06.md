# Code Review

## Verdict

Approved

No source correctness findings were identified in the reviewed Subject-index change or portable freeze-policy benchmark adapter. The index retains statement membership only; lifecycle, effective time, freeze results, and corroboration remain fresh evaluation inputs. The 39 unique named cases and scoped static checks passed; the reviewed source and benchmark harness are committed as `0b2f1d76`.

## Manual Review Priority

- Priority: P1
- Reason: This changes the derived state used by admission policy evaluation and verified checkpoint continuation.
- Suggested Human Review Focus: Statement-versus-pin identity; retarget/deletion updates; per-Subject completeness; malformed-input ordering; historical-checkout benchmark imports.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/proposals.py`, `src/cruxible_core/playbill/checkpoints.py`, `docs/benchmarks/write-loop-served.py`, `tests/test_playbill/test_replay_checkpoints.py`.
- Untracked files: `src/cruxible_core/playbill/claim_subject_index.py`, `tests/test_playbill/test_claim_subject_index.py`.
- Tests examined: The new Subject index tests, modified checkpoint reconstruction assertion with its Claim-bearing fixture, prior policy-value demand tests, and adoption-fixture construction used by the benchmark.
- Commands run: Read-only `git status`, `git diff`, `git rev-parse`, `rg`, `cat`, and bounded `sed` reads in `/private/tmp/playbill-subject-policy-index`. Exact base: `52a4558289dadbe3da99df2409c520a2d5cd9296`.

No tests or benchmarks were run by the reviewer, per the explicit request to avoid contention. No source files were edited.

## Findings

No findings.

## Complexity Assessment

Cold index construction parses every Claim and retains a Claim-to-Subject mapping plus reverse Subject buckets. Incremental construction parses only changed Claim paths, copies both top-level maps, and copies only touched reverse buckets. Retained metadata is O(Claims + Subjects); it contains strings and frozen path sets, with no Claim models, values, or law results. The containing evaluation cache still accounts for input bytes rather than total Python heap, and its detached result copy now includes these maps.

Freeze evaluation reads all Claims belonging to each affected Subject, including unchanged Claims of other ClaimTypes. It does not narrow to changed Claims or frozen predicates. Time and lifecycle filtering run again at each requested evaluation time. Cost therefore depends on affected Subject population, while index-map copying remains proportional to total indexed population. This is appropriately described as reduced parsing scope, not constant-time state advancement.

## Architecture Assessment

Read the change in this order: `ClaimSubjectIndex` and its cold/incremental builders; the three `EvaluatedTreeState` constructors; `_claim_admission_evaluations`; then the portable benchmark adapter.

Membership derives from `parse_claim(...).statement.subject.artifact_path`, using the same exact Claim-path regex and strict V2/V3 parser as the previous full policy scan. Subject pins intentionally do not substitute for statement identity, and admission law remains responsible for their correspondence. Retired and future-effective Claims remain indexed so changing evaluation time cannot make the index stale. Retargeting removes the old bucket entry and adds the new one; deletion removes the forward entry and prunes an empty bucket. Updates copy maps and touched sets before mutation, so failure cannot alter the preceding index.

All three state constructors derive the index from the same exact tree as manifest/dependency state. Cold and checkpoint construction perform dependency validation first; candidate advancement remains after the existing scoped malformed-member checks and dependency update. Checkpoint membership is rebuilt from verified coordinate bytes, never accepted from persisted unverified index fields. Existing cache ownership and exact-byte eligibility apply to the extended state.

The indexed policy path is used only when both parent and candidate indexes exist; otherwise the previous complete-tree path is retained. Each affected Subject receives complete parent and candidate value maps, shared across its applicable freeze policies. Current-coordinate corroboration facts, query execution, policy digests, candidate law, and authority checks are unchanged. No stored wire format, historical digest, or proof representation changes.

The benchmark imports fixture and contracts only after selecting `--repo`. Its adapter replaces fixture inputs before vocabulary, ClaimType digests and dependent pins are built, then restores both hooks in `finally`. It does not monkeypatch production evaluation. Constant ready values avoid ambiguous parent values; inactive freeze requirements still exercise full freeze-value computation. The flag, workload description, and setup timing distinguish this workload from the original empty-policy fixture.

## Test Coverage Assessment

The focused tests inspected cover cold/incremental parity, actual statement-versus-pin mismatch, retargeting, V2-to-V3 retirement, deletion, empty state, rewind, unchanged predecessor ownership, and changed-member parse demand. They also cover effective-time boundaries, retired membership, full-versus-indexed policy output for eligible/refused cross-type freeze, exclusion of unrelated Subject Claim parsing, and multiple changed Subjects with retargeting.

The additional reviewed cases verify that an actually malformed changed Claim reproduces the cold refusal without mutating the predecessor index, caller mutation cannot poison cached Subject maps, and a verified checkpoint rebuilds the complete index from its Claim-bearing tree. Final implementer-reported validation: 39 unique cases passed. The first scope passed 25 cases in 18.64 seconds (index 7, policy 4, cache 12, served 2); the second passed 21 in 42.29 seconds (index 9, corroboration 10, checkpoint 2), with seven index cases overlapping. Mypy passed for three source files; Ruff check and format check passed for six source/test/harness files; `git diff --check` passed. Reviewed commit: `0b2f1d76`. This reviewer ran no tests or benchmarks; no full suite or golden corpus was run for this review.

## Documentation Assessment

The index docstring explains why statement membership differs from pins and why time/lifecycle are deferred. The benchmark flag and output explain inactive freeze and constant-value setup without claiming active-freeze enforcement as the benchmark's purpose. Comments are proportionate to the changed logic. No public schema or contract inventory update is needed.

## Overall Contribution

The change is a cohesive next step after lazy policy-value construction: it reduces repeated reads to complete affected Subject populations while retaining the cold path and existing admission semantics. The benchmark extension targets that specific workload and can measure older repository versions through the same input adapter.

## Open Questions

None.

## Suggested Follow-Ups

None.
