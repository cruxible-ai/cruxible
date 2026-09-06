# Code Review

## Verdict

Approved.

The archive fix rebuilds settled review projections from proposal evidence without changing accepted history. The Source-policy fix grades acquired material before binding it and refuses unsupported coherence before Source execution. No blocking correctness or integration issue was found in these exact changes; collision-group note work remains explicitly outside this review.

## Manual Review Priority

- Priority: P1
- Reason: Changes affect rebuildability of review history and enforcement of accepted acquisition policies.
- Suggested Human Review Focus: Evidence-derived archive targets; atomic ref updates; admission-bound freshness evaluation; rejected material and reservation handling; pre-execution coherence refusal.

## Scope Reviewed

- Exact archive commit: `a03bd632c3ed022d2bd712678a46b95462433509`.
- Exact Source-policy commit: `dc029b2c51b9540654aaa4f18806eb83ea0a2af0`.
- Archive files: `src/cruxible_core/playbill/git.py`, `src/cruxible_core/playbill/instance.py`, `tests/test_playbill/test_review_archive_rebuild.py` as committed in the archive fix, excluding later edits to these files.
- Source files: `src/cruxible_core/playbill/procedures/acquisition.py`, `src/cruxible_core/playbill/procedures/execution.py`, `src/cruxible_core/service/playbill_procedure_runs.py`, `tests/test_playbill/test_procedure_source_policy.py`.
- Untracked files: the Source test was initially untracked during inspection, then reviewed in its exact committed form. No other untracked implementation was included.
- Tests examined: all new archive and Source-policy tests; surrounding source-run fixtures, acquisition-policy selection rules, reservation handling, and proposal evidence note builders.
- Commands run: `git status`, exact `git show` for both fixes, scoped diffs, surrounding source/test reads, call-site searches, and `git diff --check`. No product files were edited. No broad tests were repeated because implementers had just completed the named scopes below.

## Findings

No findings.

## Complexity Assessment

Archive reconciliation scans admissions and reconstructs both open and settled review commits. It is linear in total proposal history before ref sorting and includes per-proposal Git/note work; approval rendering also scales with stored approvals. This intentionally restores missing archives but does not make historical rebuild work bounded or incremental. The publication worker keeps this work separate from ordinary durable submission; a large backlog can still delay mirror acknowledgment.

Source eligibility adds constant work per acquired input: replayability membership and one duration comparison. Coherence screening uses the already-built planned Source map. Rejected Captures release reservations; selected captures retain reservations until the durable produced-capture event. The second release on non-selected non-refusal paths is harmless because release empties its pending list.

## Architecture Assessment

Read the changes in this order:

1. In archive reconciliation, each admission's evaluated tree/base, author, timestamp, and persisted rationale recreate the same advisory commit. Accepted, withdrawn, and stale candidates select the settled namespace even when no open projection ever existed.
2. `replace_proposal_review_refs()` validates identifiers and OIDs, refuses a proposal supplied as both open and settled, gives explicit evidence-derived settled targets precedence over disposable departing refs, and performs archive updates plus open-ref removals in one Git transaction. Evaluation/approval notes follow reachable refs under the existing reconciliation and approval locks.
3. The internal mirror-retention identifier now uses the repository's `new_id` primitive. It retains a 32-hex random suffix and changes only private temporary refs, not object-format rules or accepted digests.
4. `apply_acquisition_result()` checks acquired Capture replayability and maximum age before returning selected. Failure uses the accepted rule's omission/default/refusal behavior; defaults still require explicit authorization. Considered digests remain in the decision for auditability.
5. All executor call sites supply the immutable admission evaluation instant: V3 and descendants use `occurrence_evaluation_time`; older admissions use `admitted_at`. Wall-clock scheduling cannot change eligibility. Non-selected results never reach produced-capture binding/associations; the attempted read and decision remain journaled.
6. Source-plan selection refuses non-independent coherence until the runtime has the required reducer/proof capability. Both direct and Line lanes return typed admission refusal before reads or provider invocation. Source-free plans retain prior behavior. The Line lane may create an empty journal directory during occurrence lookup, but records no admission/run event on this refusal; the direct lane does not create its run journal.

Neither fix changes frozen contract fields, canonical serializers, digest domains, accepted generation records, or accepted tree bytes. Historical receipts remain stored evidence; the Source fix changes eligibility of newly executed acquisitions rather than rewriting prior results. Review refs and notes remain derived inspection surfaces.

## Test Coverage Assessment

Implementer-reported validation, inspected rather than redundantly rerun:

- Archive: **5 passed** in `test_review_archive_rebuild.py`, covering late mirror binding after activation/withdrawal, deletion and exact-OID rebuild of archives/notes, and coalesced submit/withdraw before the first open projection.
- Supporting Git/guardrails: **28 mirror snapshot tests passed**, including SHA-1 and SHA-256, plus **2 primitives checks passed**. The five archive fixtures use the default instance format; they do not separately parameterize both formats. The new archive logic delegates OID validation and transactions to the same format-independent Git path.
- Source: **37 passed**, comprising **14 new policy tests** plus existing acquisition-plan and v4 runtime scopes. Coverage includes direct/Line replayability refusal, no selected Capture output after denial, unsupported coherence before acquisition, optional/default/refusal behavior, default authorization, source-free compatibility, and exact maximum-age boundary.
- Source regression: **4 existing success/replay/crash cases passed** after the fix. The separately reported original Fable baseline of 78 passing tests is not counted as post-fix validation here.
- Both implementers report clean scoped Ruff, formatting, mypy, and diff checks.

No full suite, golden corpus, canonical-checkout test run, or live publication was performed by this review.

## Documentation Assessment

Comments explain evidence-derived archive precedence, eligibility after observation, admission-time semantics, and why unsupported coherence must refuse. Repair text tells callers that independent coherence is the served capability without silently changing an accepted policy. The existing reconciliation docstring's reference to per-open-proposal steady-state work now understates that settled proposals are included; a future documentation touch should say “per retained proposal” when describing reconciliation cost. This does not obscure the correctness contract.

## Overall Contribution

Both fixes address concrete gaps exposed during integration: coalescing could skip the only chance to archive a proposal, and successful acquisition could bypass accepted eligibility/coherence requirements. Their scopes are cohesive, preserve the ledger/derived-surface boundary, and include focused regressions for the observed failures.

## Open Questions

None.

## Suggested Follow-Ups

- Profile long-lived mirror instances and consider an incremental, rebuildable review-evidence index if full historical reconciliation dominates acknowledgment time.
- Parameterize the archive reconstruction regression by Git object format when the archive path next changes.
- Update reconciliation cost wording to include settled proposals during the next documentation pass.
