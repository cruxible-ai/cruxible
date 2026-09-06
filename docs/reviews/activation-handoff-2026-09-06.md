# Code Review

## Verdict

Approved

The final source preserves the verified activation boundary while avoiding routine replay on clean local success. The handoff owns preparation, derives detached successor metadata, and installs it only after exact publication checks under the writer lock; uncertain outcomes retain recovery. The publication guard gap noted during review has been resolved.

## Manual Review Priority

- Priority: P1
- Reason: This changes the installation of accepted in-memory state and the synchronization of recovery with publication.
- Suggested Human Review Focus: Parent-root proofs and detached record ownership; successor principal registry; state/activation/review lock order; post-CAS failure repair; maintenance scheduling changes.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/activation.py`, `src/cruxible_core/playbill/instance.py`, `src/cruxible_core/playbill/recovery.py`, and `src/cruxible_core/playbill/service/documents.py` in `/private/tmp/playbill-activation-handoff`, based on `f36620bf1a33277d02140fdad96d89f1be89e637`.
- Untracked files: `tests/test_playbill/test_activation_handoff.py` and `tests/test_playbill/test_activation_handoff_guards.py`. The follow-up review also covered the `--reopen-after` addition in `docs/benchmarks/write-loop-served.py`.
- Tests examined: Both complete new handoff test files and the relevant existing activation/recovery, principal-history, receipt-coordinate, archive/reconciliation and mirror tests. Final implementation validation: 72 distinct cases passed, detailed below.
- Commands run: Scoped `git diff`, `git status --short`, `git diff --check`, `rg`, and source inspection. No source edits, tests, benchmarks, full suite or golden corpus run by this reviewer; independent validation belongs to the parent and test author.

## Findings

No findings.

## Complexity Assessment

Clean success no longer reevaluates the accepted prefix or the just-prepared candidate through `recover_instance`. It still performs preparation, full projection prebuild/publication, exact signature and receipt checks, successor principal parsing, final serving verification, and an O(history) tuple extension. No complete generation tree is retained in accepted history. New record parsing provides isolation without copying all historical records. Recovery remains the more expensive uncertainty path.

The change intentionally stops recovery's unrelated orphan sweep and current-head checkpoint rewrite on every clean acceptance. The publisher's existing checkpoint interval is 50, so a later open or stale-handle recovery may replay up to 49 generations after the latest stride checkpoint. This is a maintenance/cold-latency tradeoff, not an authority change, and must accompany performance reporting.

## Architecture Assessment

Read `prepared_generation_for_handoff` first. It is explicitly an internal continuation of an owned preparation, not a general verifier for externally supplied bundles. Preparation has already reproduced laws, approvals, roots and exact stored tree bytes. The helper adds the replay-only checks: exact predecessor, active parent-root daemon key, Git-proven append-only receipt namespace, canonical new record and sequence, and frozen record-to-candidate correspondence. It conservatively returns to recovery for possible removed prerelease content. It constructs successor principals from the new tree and semantic root, correctly avoiding `bundle.principals`, which is the parent approval registry. Parsed record and principals are fresh, descriptor is copied, and no tree is retained.

Next read the instance operation. It holds the per-instance RLock across private preparation and publication, capturing one predecessor epoch. The publisher invokes its completion callback before releasing its cross-process activation lock. The callback requires an accepted outcome, unchanged epoch, available detached successor and matching current main; otherwise it invokes locked recovery. Final complete coordinate equality, exact generation-note bytes and `bind_current_projection` tie the installed epoch to what actually became durable and served. It constructs the complete replacement state before assigning `_recovered` and clears all instance memo surfaces formerly cleared by refresh.

Finally read failure/lock handling. Public refresh acquires instance RLock then activation lock; its private helper assumes both are already held. Callback fallback uses that helper rather than recursively opening the same flock. Exceptions escaping publisher/prebuild reach refresh only after the publisher context has released activation lock; the RLock safely reenters. Review/workspace/mirror actions stay outside the instance and activation locks, avoiding inversion with review reconciliation, which can invoke refresh while holding its review lock. Service-level search memo invalidation, advertisement, archival reconciliation and asynchronous mirror enqueue remain. The receipt continues to use this invocation's publisher coordinate even if advertisement later observes another accepted generation.

## Test Coverage Assessment

Required focused coverage is defined in `/private/tmp/activation-handoff-boundary-audit.md`: clean success without recovery followed by another write and immediate reads; reopened-state parity and successor principals; caller mutation isolation; stale epoch/intervening main/lost CAS; post-CAS note/serving failures; preserved archive/mirror behavior; stride checkpoint and deferred maintenance behavior. Both new test files have now been reviewed. They exercise actual flock exclusion and concurrent refresh ordering, ordinary mutable bundle isolation, sequential served writes in SHA-1/SHA-256, registration/revocation, lost-CAS and stale-epoch recovery, missing note/serving repair, successful and failed post-CAS repair, three frozen receipt versions with reopened-state parity, parent-key rejection, changed receipt history, invalid sequence, removed-content fallback, and checkpoint stride/reopen behavior. Final reported validation: root's six regression files plus guard tests passed 61 cases in 116.56 seconds; the independent test author's handoff file passed 11 cases in 32.11 seconds, for 72 distinct cases. Scoped mypy passed for four source files, and Ruff check/format checks passed. The reviewer did not rerun them.

### Additional retained-prefix ownership audit

Every production `accepted_history()` call site was inventoried. Consumers read generation coordinates, timestamps, candidate/receipt digests, member paths or principal scalar values; evidence readers parse raw law evidence into derived models. Curation's internal helpers do carry receipt references between functions, but only read them while constructing separate operational payloads. No mutation of borrowed receipt dictionaries or nested models was found. Principal listing passes frozen scalar-only PrincipalRecord objects into a response that the runtime/SDK serializes. Public SDK callers do not receive the daemon's receipt objects.

This is a trusted in-process read-only borrow, not a hardened arbitrary-caller mutation boundary. Mutating `accepted_history()[-1].record.law_digests` from embedded Python would already poison the current epoch; retaining the prefix extends that object's lifetime but introduces no observed production mutation path. It is not a blocker for this change. The smallest useful clarification is to document the borrowed read-only ownership contract; deep-copying the entire history on every read would add substantial cost without closing the daemon's other private-object access paths. Any future supported mutable/third-party history API should detach only the requested records at that boundary. The new successor is independently parsed and does not alias the prepared bundle.

### Reopen benchmark review

`--reopen-after` stops and waits for the benchmark daemon after all timed write loops, then launches a fresh interpreter using the selected checkout's inherited PYTHONPATH. It measures only `PlaybillInstance.open`, after imports and trust-file parsing, reports that duration separately, and checks the reopened coordinate equals the final acceptance receipt. It neither warms the active write process nor folds shutdown/import/reopen time into write totals. The documentation correctly labels it fresh-process recovery rather than daemon startup or SDK connect latency; filesystem caches remain uncontrolled. No timing result was asserted by this source review.

## Documentation Assessment

The new docstrings explain that handoff trusts an owned preparation and does not verify arbitrary bundles. The callback documents that failures cannot roll back committed acceptance. Comments identify held-lock assumptions and the rule against installing uncertain state. Integration documentation should explicitly state that orphan sweeps move to recovery and checkpoints return to the existing 50-generation stride; the source alone should not be presented as identical maintenance scheduling.

## Overall Contribution

A cohesive change that removes repeated proof work from the normal SDK write loop while retaining ledger authority, atomic publication and frozen restart verification. The accepted in-memory history is derived from the same owned verified generation rather than a second replay of it. Cold-start and maintenance costs are shifted rather than eliminated and should be measured separately.

## Open Questions

None.

## Suggested Follow-Ups

None.

Implementation and both test files committed together as `24f84f749320df15fccba373725c9276d34636a5`. The accepted-history docstring now states the internal read-only borrowing contract; the publisher callback docstring explicitly prohibits reacquiring the ledger activation lock.
