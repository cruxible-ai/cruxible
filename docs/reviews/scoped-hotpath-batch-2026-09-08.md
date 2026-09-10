# Code Review

## Integration status — September 10, 2026

The maintainer authorized integrating all four reviewed fixes, then authorized reverting
only the mirror optimization. Commit `918c1d7e` reverts `7e0cc9d7`; the mirror production
module and its test module exactly match the pre-batch baseline. This removes the F-001
regression by restoring the previous refspec and lease behavior. The original argument-size
and inventory limits remain open; no replacement mirror protocol was implemented.
Checkpoint manifest reuse, prepared-handoff ancestry preservation and both exact-path
Procedure readers remain integrated. No push or deployment was performed.

The report below describes the original implementation and measurements, not the current
mirror behavior. See [the independent review](scoped-hotpath-independent-review-2026-09-09.md)
for the finding that led to the revert. Post-revert verification: both mirror test modules passed (40 tests, 60.67 seconds);
`git diff --check` passed. Mirror source and tests match the pre-batch baseline exactly.

## Verdict

Approved with comments (implementer self-review; independent review still required before merge).
The four scoped fixes are committed on `codex/scoped-hotpath-batch`, based on
`8eb5e9b15f0a2f439eb9a29d413cbd6e90d94e07`. No changes are merged or deployed.
The mirror fix preserves strict atomicity, so oversized initial synchronization remains a limit.

## Manual Review Priority

- Priority: P1
- Reason: Reuse must preserve exact snapshot bindings and mirror concurrency guarantees.
- Suggested Human Review Focus: Read the four changes below, starting with mirror delta selection, then the checkpoint manifest binding.

| Read order | Before → after | Code and commit | Remaining broad work |
| --- | --- | --- | --- |
| Mirror | Validate full inventory → push every owned ref. Now validate the same inventory → push only differences; no push for an already-matching snapshot. | `GitLedger.push_mirror`, `src/cruxible_core/playbill/git.py:732`; `7e0cc9d7` | Full ref advertisement, validation, sorting, and retention pins remain. The existing 4,096-ref ceiling remains. A delta over the 64 KiB argument budget still refuses. |
| Checkpoint | Settlement verifies semantic members → checkpoint hashes them again. Now settlement carries that manifest in `VerifiedGenerationBundle` → checkpoint reuses it. | `src/cruxible_core/playbill/settlement.py:849` → `src/cruxible_core/playbill/activation.py:292`; `4535eb77` | Root construction, principal collection, member-map copy, and writing remain whole-snapshot work. Incremental checkpoint format is post-v1. |
| Prepared handoff | Strip cards into a dictionary → build an unrelated snapshot. Now fork the immutable tree → remove cards → seal the existing overlay. | `PreparedEvaluationScope.retain`, `src/cruxible_core/playbill/prepared_evaluation.py:175`; `32a2f880` | Finding cards still scans all paths, and removing inherited cards takes work proportional to those cards. No new card index is introduced. |
| Procedure reads | Obtain and copy the entire accepted tree → look up one path. Now use existing `blob_at` for that path. | `_accepted_procedure`, `src/cruxible_core/service/playbill_procedure_runs.py:650`, and `_CurrentProcedureAuthority.current_procedure_digest:1640`; `cb916464` | The instance acceptance proof and Procedure parsing remain. Other whole-tree callers are untouched. |

The checkpoint manifest excludes generated change-set records and review cards, exactly as
`members_for_tree` does. Principal reconstruction still uses the resulting generation tree,
not the parent's principal registry. The Procedure loader retains the chosen historical
coordinate; the authority adapter independently resolves current accepted head.

## Scope Reviewed

- Changed files: five production modules and four test files in the table/test commands below.
- Untracked files: the benchmark probe, captured results, and this review guide, subsequently committed together.
- Tests examined: checkpoint activation/reopen; prepared handoff, invalidation, and immutability; served Procedure snapshot binding; mirror atomicity, leases, divergence, retries, errors, and archive growth.
- Commands run (all in the isolated worktree, using the existing venv with `PYTHONPATH=.:src:packages/cruxible-client/src`):
  - `python -m pytest tests/test_playbill/test_replay_checkpoints.py::test_activation_writes_a_checkpoint_on_its_configured_stride -q` — 1 passed.
  - `python -m pytest tests/test_playbill/test_prepared_evaluation.py -q` — 26 passed.
  - `python -m pytest tests/test_playbill/test_procedure_run_surface.py -k 'explicit_historical_coordinate or readiness_and_idempotent_run or explicit_at_equal' -q` — 3 passed.
  - `python -m pytest tests/test_playbill/test_git_mirror_snapshots.py tests/test_playbill/test_ledger_mirror.py -q` — 42 passed, including SHA-1 and SHA-256 fixtures.
  - Ruff on all changed Python files; `git diff --check`.
  - `python docs/benchmarks/scoped_hotpath_probe.py` — results below.

No full suite, goldens, canonical-checkout tests, daemon restart, remote deployment, or project-state write.

## Findings

No findings requiring a code change within the agreed narrow scope.

The mirror fix is deliberately incomplete for a fresh mirror or backlog whose actual
changed-ref arguments exceed the limit. Supporting those with bounded archive batches
requires relaxing the current all-ref atomic publication contract. That decision was raised
with the maintainer; this batch retains the existing refusal pending a ruling. It does not
claim to remove every ref-count ceiling or all history-proportional mirror work.

## Complexity Assessment

The table above distinguishes removing repeated hashing/copying from eliminating all broad
work. These fixes do not implement the shared SQLite redesign. Snapshot sharing reuses the
existing persistent map; no additional whole-world cache was introduced. Checkpoint reuse
carries an existing manifest reference rather than copying it into a second structure.

Isolated measurements (median of seven invocations; 101 for tiny warm dictionary operations):

| Substep | 1,000 members before → after | 10,000 members before → after |
| --- | --- | --- |
| Prepared handoff, including existing candidate/result copies | 2.58 → 0.77 ms | 27.74 → 6.01 ms |
| Checkpoint body construction, excluding file write | 21.86 → 16.58 ms | 545.42 → 490.54 ms |
| Git read without application tree memo | 37.25 → 32.89 ms | 58.84 → 33.17 ms |
| Warm dictionary copy-and-lookup → lookup only | 0.0044 → 0.0001 ms | 0.0482 → 0.0001 ms |

A one-main-ref mirror update with 20 archive refs measured 160.78 → 157.26 ms; treat that
small difference as noise, not a substantial latency gain. With 300 archive refs the old
method refused at its argument precheck; the new method completed in 199.14 ms.

Method: the probe loads the actual old handoff and push methods from the base commit and
compares them to the new methods in the same process. The checkpoint comparison supplies
or omits the existing optional manifest argument and asserts identical bodies. Scratch
fixtures add 1,000 or 10,000 synthetic 4 KiB documents, sharing identical payload bytes;
they measure structural work, not a representative domain world. Git reads use real scratch
Git objects, but omit the unchanged instance acceptance proof. "Uncached" means no
application tree memo: OS/Git caches may be warm. Warm mapping figures isolate the removed
copy, not the full service call. Mirror fixture reset is outside the timed region. Setup
is untimed. These are substep measurements, not new full SDK-loop times or slope guarantees.

Reproduction: `docs/benchmarks/scoped_hotpath_probe.py`.
Raw results: `docs/benchmarks/scoped-hotpath-results-2026-09-08.json`.

## Architecture Assessment

Existing service, immutable-tree, Git, and checkpoint APIs are reused. No transport-specific
or parallel orchestration path was introduced. Durable ledger/proof formats are unchanged.
The required `members` field is internal to the generation bundle; its single construction
site supplies the exact re-evaluated semantic manifest.

## Test Coverage Assessment

72 targeted tests passed. The added checks prove identical checkpoint members and successful
reopen with rehash disabled, preserve card stripping for standalone and parented snapshots,
retain historical Procedure loading alongside current-head authority, and demonstrate that
300 unchanged archive refs do not enter push arguments. Existing real-Git tests retain
competing-writer atomicity and uncertain-outcome coverage.

## Documentation Assessment

The mirror method now distinguishes full inventory verification from delta publication.
This guide records the retained limits rather than implying the entire operation is scoped.
The benchmark is intentionally separate from product instrumentation and CI slope tests.

## Overall Contribution

Production diff: **20 added, 15 deleted, net +5 lines**. Tests: **97 added, 3 deleted**.
Supporting benchmark, raw results, and review guide: **356 added, 0 deleted**.
The small net production addition is justified by carrying verified members across the
existing handoff and preserving an immutable overlay while removing cards. It adds neither
an index nor a framework. Tests cover the proof and identity boundaries being changed;
the benchmark and this guide support the requested maintainer walkthrough.

## Open Questions

Before expanding mirror transport: may historical archive refs be transferred in bounded
batches before atomically publishing active refs? This is not needed to review the routine
delta fix, which preserves the existing all-ref atomic contract.

## Suggested Follow-Ups

Pause for the maintainer code walkthrough and independent review before merge. Then design
the shared SQLite projection, candidate overlays, revision/publication protocol, and an
explicit replacement/deletion map. Receipt storage and proposal evaluation locators belong
there. Verdict invalidation remains a separate correctness triage. Full incremental
checkpoints remain post-v1; mirror oversized-sync and ref-count limits remain explicit.
