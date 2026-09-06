# Code Review

## Verdict

Approved.

The signature prefilter rejects only unreachable commits that already fail a necessary condition of the existing successor proof. It preserves the complete verification and targeted collection path for passing commits and does not change accepted-state replay, deletion authority, or serving admission. Approval covers exact commit `283ca306` and its four new regression cases. The parent completed the combined recovery scope: 28 tests passed, including all four new cases; Ruff, formatting, and mypy checks passed.

## Manual Review Priority

- Priority: P1
- Reason: Recovery cleanup may delete proven unaccepted generation objects, so a performance change must preserve the precise authorization for collection.
- Suggested Human Review Focus: Parent-root daemon key selection; unchanged full successor verification; retained unsigned and signed-invalid debris; unchanged reachability checks before collection.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/recovery.py`, adding the signature precheck in `_clean_unaccepted_generations` at line 654.
- Untracked files: `tests/test_playbill/test_recovery_orphan_prefilter.py`.
- Tests examined: Both-format unsigned-orphan and signed-invalid-orphan cases in the new file; existing positive crash-residue collection and losing-CAS cleanup coverage in `tests/test_playbill/test_recovery.py` at lines 156 and 216.
- Commands run: `git diff` and focused source/test inspection, including `_verify_successor`, historical public-key signature verification, and `collect_unreachable_generation`.

No tests were rerun by this reviewer because the parent ran the exact new cases and existing recovery test file (28 passed). No source edits or live-instance operations were performed.

## Findings

No findings.

## Complexity Assessment

For each unsigned or incorrectly signed orphan with an accepted parent, cleanup now performs parent membership and exact daemon-signature verification, then stops. It avoids reading, parsing, and deriving the full parent tree before reaching the same rejection. It does not eliminate the existing unreachable-object inventory scan or signature-check subprocess.

A correctly signed orphan pays one additional signature verification and then the entire existing successor proof. This is an appropriate tradeoff for the measured population dominated by unsigned proposal/review residue. Parent tree memory is no longer allocated for those rejected orphans; no new cache or retained state is introduced.

## Architecture Assessment

The new check uses `parent.principals.require_active("daemon")` and `verify_commit_with_public_key` with exactly the parent-root daemon public key, matching `_verify_successor` at lines 391–398. It does not rely on current credentials, a global signer allowlist, a note, or a projection as authority.

Passing the precheck does not permit deletion. The original parent-tree derivation, `_verify_successor`, second signature verification, change-set/law/provenance checks, and `collect_unreachable_generation` call remain in place. The collector still independently rejects current main and any commit reachable from refs, and protects objects reachable from other retained refs.

Both the new precheck and the existing proof remain within the same `try`/`except PlaybillError` boundary. A failed verification returns early; typed failures retain unaccepted debris. The patch adds no locks, changes no lock order, and removes no crash or collection checks. It does not modify the normal accepted-history successor verification path.

## Test Coverage Assessment

The unsigned-orphan regression is meaningful: it replaces the parent-tree reader with a failure, runs cleanup on an actual unreachable unsigned Git commit, and verifies the object and accepted coordinate remain intact. The signed-invalid regression proves that a valid daemon signature cannot authorize collection of a child missing its required change-set record. Both run for SHA-1 and SHA-256.

Existing recovery tests cover the complementary behavior: a fully valid failed generation must still be collected, including restart after interrupted generation construction and losing-CAS cleanup. The parent's combined scope therefore checks both the optimized rejection path and the unchanged valid collection path.

## Documentation Assessment

The added comment accurately explains why the prefilter comes before full parent-tree derivation and explicitly states that the full successor proof remains the authority for targeted collection. No public API or wire behavior changes require additional documentation.

## Overall Contribution

This is a narrow, well-supported optimization of repeated recovery work. It reduces the cost of irrelevant unsigned orphan commits without treating them as deletable, trusting a cache, or bypassing accepted-state verification.

## Open Questions

None.

## Suggested Follow-Ups

None required for this change. Report the full refresh and full write-loop measurements separately from the cleanup subphase improvement.
