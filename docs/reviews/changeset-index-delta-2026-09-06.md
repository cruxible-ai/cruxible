# Code Review

## Verdict

Approved with comments.

This is an implementer self-review, not an independent review. Acceptance now derives a successor index from a verified parent and the verified changeset, compiling only member artifacts. Exact-row comparisons against reconstruction pass on small lifecycle cases and private copies of the program instance. The implementation is commit `c3c42f50` on `codex/write-reconstruction-performance`; it is not deployed or merged into `playbill`.

## Manual Review Priority

- Priority: P1
- Reason: Shared accepted-state indexing changes, with unchanged ledger authority and publication boundaries.
- Suggested Human Review Focus: Verified delta provenance and fallback selection; compiler row ownership; explanation-coordinate rebinding; immutable publication and recovery.

## Scope Reviewed

- Changed files: `playbill/instance.py`, `activation.py`, `assembler.py`, `projection_artifacts.py`, `projection_tree.py`, `storage/playbill_projection.py`, and the projection-tree-limit and exhaust-promotion tests.
- New implementation/test files: `playbill/projection_delta.py`, `tests/test_playbill/test_projection_delta.py`.
- New evidence: this report and `changeset-index-delta-benchmark-2026-09-06.json`.
- Tests examined: Claim create/revise/retire parity, wrong-base refusal, missing/corrupt parent recovery, selective tree reads, mixed changesets and retirement closure, activation handoff and guards, assembler publication, citation freshness and retirement relations, promotion output retention, and original measurement-activation coordinates.
- Commands run: targeted pytest scopes below, Ruff on changed files, mypy on seven changed source files, and `git diff --check`. No full suite or golden corpus; all tests ran in the isolated worktree.

## Findings

No findings.

Review identified and addressed two recovery/ownership cases: an unchanged presentation can refer to a changed artifact, so parents with presentation or fixture extension data use full reconstruction; and missing parent citation indexes now select full relation reconstruction instead of refusing a recoverable derivative loss. Corrupt parents are rejected and the existing recovery path restores accepted state from authority.

## Complexity Assessment

For M changed artifacts, N inventory entries, H historical members, F derived rows and B database bytes, artifact compilation is restricted to M payloads and their dependencies; explanation generation for those members still consults the verified history. Git inventory comparison remains O(N), and the selected reader validates the full inventory's paths, modes and byte bounds before reading selected payloads. Capture contracts are read as a small inventory; unchanged Claim captures are reused through the existing citation-relation algorithm.

This is not an O(M) acceptance implementation. Copying SQLite, rebinding coordinate-bearing explanations, recomputing relation groups, validating the database and calculating its digest still scale with the derivative. Memory includes inventory maps, the already retained verified history, relation facts and the existing logical export. Immutable generations still retain full database pieces.

## Architecture Assessment

Read the implementation in this order:

1. `instance.py`: supplies changeset records from the instance's already verified recovered history. It does not parse a second history or accept a caller-provided serialized delta.
2. `activation.py`: combines that prefix with the verified prepared generation and parent coordinate. Direct publishers without the prefix retain full reconstruction.
3. `projection_delta.py`: verifies successor/base correspondence, modern changeset format and contiguous prefix; selects only understood member kinds; binds the exact parent manifest; checks that the Git diff contains only changeset members, candidate cards and the new changeset record. Missing parents select reconstruction. Promotion changes and fixture/presentation ownership select reconstruction.
4. `projection_tree.py`: optional payload selection occurs after whole-inventory validation. `projection_artifacts.py` consumes the internal verified history while applying the ordinary compiler to member payloads. Compiled member digests are checked against the changeset.
5. `storage/playbill_projection.py`: SQLite backup copies the immutable parent into a private staged database. Indexed ownership lookups replace changed envelopes, liveness, pins and facts. Promotion-owned Procedure/Line track records are retained. Known explanation proof fields are rebound explicitly; arbitrary evidence text and historical activation coordinates are not rewritten. Citation relations use the existing incremental compiler. Generation metadata changes in the same staged transaction.
6. `assembler.py`: owns strategy selection and retains the existing fsync, digest, immutable piece/manifest publication and activation handoff. Reconstruction remains the oracle and recovery path.

The ledger and accepted Git tree remain authoritative. The delta is internal and unsigned; it carries already verified authoritative inputs rather than introducing another truth plane. There are no receipt, signature, compiler, SQLite schema, SDK or public wire changes. Parent facts avoid reopening unchanged CAS bodies during ordinary acceptance; explicit reconstruction still re-reads them.

## Test Coverage Assessment

Every-table parity includes generation metadata and presentation tables, not just the logical digest. Tests also verify historical database bytes remain unchanged. A real accepted exhaust promotion retains its output and original accepting coordinate across an unrelated delta; its complete successor matches a ledger-only rebuild. Procedure updates themselves require dependent promotion closure under current admission rules, so a Procedure-only mutation is correctly refused before indexing.

Verification results are recorded below. Intermediate failures were test setup errors (an invalid Claim enum, injection intercepting recovery as well as activation, and omitting required promotion closure); the final cases exercise valid product operations and typed refusals.

### Verification scopes

- `pytest tests/test_playbill/test_projection_delta.py tests/test_playbill/test_activation_handoff.py tests/test_playbill/test_activation_handoff_guards.py` plus the mixed-change-set and retirement-closure cases: **26 passed** in 82.96 s before the added recovery tests.
- New parent recovery/refusal and selective-reader cases passed; the corrupt-parent case was rerun after correcting the injection to allow recovery assembly (**1 passed** in that recheck; the simultaneous promotion setup failure was corrected below).
- `pytest tests/test_playbill/test_exhaust_promotions.py::test_promotion_passes_proposal_replay_and_projects_canonical_output tests/test_playbill/test_assembler.py tests/test_playbill/test_citation_retirement_relations.py tests/test_playbill/test_projection_citation_freshness.py`: **33 passed** in 78.37 s on the final implementation.
- `test_derived_activation_remains_bound_to_its_accepting_generation` passed in the boundary scope.
- Managed HTTP authoring approval/recovery/repair workflow: **1 passed** in 7.66 s in the initial integration scope.
- Ruff on all changed Python files: passed. Mypy on all seven changed source files: passed. `git diff --check`: passed.
- Tests ran with `PYTHONPATH=src:packages/cruxible-client/src` and the repository virtualenv Python from `/private/tmp/playbill-grep-surfaces-v1`. Benchmarks ran without simultaneous tests or profiling. Both benchmark cases compared every SQLite table successfully.

## Documentation Assessment

Internal docstrings explain trusted input provenance, fallback ownership, staged copy semantics and exact coordinate rebinding. The benchmark evidence records operation boundaries and individual samples. There is no new consumer-facing API to document.

## Overall Contribution

The change removes repeated compilation of unrelated accepted artifacts from the ordinary acceptance path without changing governance or digest meanings. It materially improves acceptance, but neither proposal submission nor total acceptance latency is solved.

### Before and after

Private copies of the program snapshot; identical seven-member proposal (five Claims and two Subjects); order full/delta/delta/full; independent instance opens and identical parent. Setup/open and proposal submission are outside the measured acceptance. No profiler or HTTP transport. Each arm produces the same accepted coordinate and every SQLite table matches.

| Operation | Full reconstruction | Changeset delta | Mean reduction |
| --- | --- | --- | --- |
| Full service acceptance | 18.01–18.92 s | 11.35–11.64 s | 38% |
| Derived-index prebuild within acceptance | 7.65–7.98 s | 1.77–1.94 s | 76% |

A separate identical prepared one-Claim candidate, over 2,144 artifact envelopes and 25,262 semantic facts, took 7.22–8.12 s with full reconstruction and 2.27–2.69 s with delta construction. All tables matched there too. Differences between the one-Claim and seven-member comparisons reflect independent run/cache conditions, not an assertion that bigger changes are cheaper. These are local paired samples, not managed HTTP latency guarantees.

Small final ownership/recovery guards were added after the timings; they do not change the measured snapshots' selected path. The final tests cover those guards and reconstruction parity.

## Open Questions

None blocking this implementation.

## Suggested Follow-Ups

- Address proposal-note/index reconstruction shared by write operations; submission is unchanged by this patch. Roughly ten seconds of the paired acceptance remains outside prebuild, and should be attributed separately before promising further savings.
- Normalize explanation coordinates out of per-artifact payloads in a future compiler format, if remaining rebinding cost justifies migration.
- Add explicit row-ownership adapters before enabling delta updates for promotions or extensible presentation data.
- Extend the verified changeset envelope to other derived-index consumers when their dependency/ownership contracts are explicit.
- Obtain an independent review, integrate, and measure the installed SDK/daemon path before replacing the currently deployed timing claims.
