# Code Review

## Verdict

Approved with comments.

Reviewed final correctness commit `1ea0f40c49e617e59c38b28de286b97618b585f7` against base `e576c276`. The original-alias recovery bug, inherited advisory-note tamper gap, and unreachable advisory-object regression are resolved; no blocking findings remain. Independent source review and a before/after advisory-tamper probe support approval, alongside the implementer's 66 passing focused tests.

## Manual Review Priority

- Priority: P1
- Reason: The change coordinates evidence persistence, grouped review projections, approval concurrency, and strict settlement checks across multiple aliases of a commit.
- Suggested Human Review Focus: Original/advisory collision groups; distinct signed payloads; review→candidate→Git-note lock ordering; recovery subset recognition versus strict activation; skipped incomplete evidence.

## Scope Reviewed

- Exact reviewed range: `e576c276..1ea0f40c49e617e59c38b28de286b97618b585f7` in `/private/tmp/playbill-state-loop-design`. Subsequent candidate-summary optimization is separate and not covered by this correctness approval.
- Changed files: new `src/cruxible_core/playbill/proposal_note_projection.py`; `git.py`; `proposal_evidence.py`; `proposals.py`; `instance.py`; `playbill/service/documents.py`; `packages/cruxible-client/src/cruxible_client/contracts/proposal_models.py`; related grouped-note, proposal, archive-rebuild, and review-publication-concurrency tests.
- Untracked files: new projection module and `tests/test_playbill/test_grouped_proposal_notes.py` reviewed. Other concurrent agent files are excluded.
- Verification evidence: implementer reports **66 passed in 67.92s**: 24 Git identity parity/refusal cases, 14 grouped-note cases, and 28 proposal cases including no-orphan regressions. Ruff on owned files and mypy on six core files passed. Existing archive/note/concurrency integration tests are being rerun separately after the hash-only change; they are not included in the 66 count.
- Tests examined: colliding same-second admissions; subsecond timestamps sharing Git OIDs with distinct candidate digests; original/advisory aliases; crash after evidence before note; incomplete admission/evaluation/candidate persistence; corrupt notes; canonical subset checks; archive rebuild and approval lock tests.
- Commands run: `git status`, exact-base diffs, targeted source reads/searches. No duplicate broad test run: the implementer owns the scoped test run. Reviewer ran one isolated behavioral probe, `/private/tmp/probe_grouped_advisory_tamper.py`, with canonical virtualenv Python and worktree-local `PYTHONPATH=.:src:packages/cruxible-client/src`. No canonical checkout tests, live instance operations, full suite, or golden corpus.

## Findings

### F-001: [High] Recovery rebuilt advisory notes but left original collision groups stale (resolved in 1ea0f40c)

- Category: Correctness
- Location: `src/cruxible_core/playbill/instance.py:_reconcile_proposal_review_refs_locked`
- Issue: Initial implementation collected only advisory review OIDs for note publication. If a second admission persisted its evidence but crashed before grouped note publication, the original candidate OID retained an incomplete note. When original and advisory OIDs differ, the recovery pass never repairs the original.
- Impact: Strict activation continues refusing the proposal after the advertised repair operation; grouped evidence is durable but the original note never catches up.
- Recommendation: Rebuild all complete indexed original and advisory OID groups, including refused evaluations where no candidate exists, while preserving the separate candidate-only open/settled branch rules.
- Test Gap: The implementer's existing crash regression reproduced this failure. The working correction now derives publication from every `proposal_ids_by_oid` entry; the corrected original/advisory recovery test passes in the final 66-case scope.

### F-002: [High] Activation ignores a tampered advisory alias (inherited limitation, resolved in 1ea0f40c)

- Category: Correctness
- Location: `src/cruxible_core/playbill/service/documents.py:_reconcile_proposal_notes`
- Issue: Settlement verifies only `proposal.admission.candidate_commit_oid`. The advisory commit a Git reviewer receives can have a different OID and a disagreeing evaluation note while the original note remains intact. The new alias index makes all relevant notes discoverable, but settlement still checks only one.
- Impact: Reviewer probe created a valid approved proposal with distinct original/advisory OIDs, replaced the advisory evaluation note with `edited reviewer facts\n`, and activated it. Result: `activation accepted main moved True`. This behavior predates this diff; it is not a new grouped-note regression.
- Recommendation: If this batch's strict review-integrity boundary covers the published advisory surface, strictly compare every alias returned by `oids_for_candidate(candidate_digest)` before activation under the review-projection lock. Only absent notes should be repaired on the activation door; present incomplete or edited notes must refuse. Otherwise record the limitation explicitly as deferred.
- Resolution: Settlement now strictly checks the target proposal’s original and advisory aliases, comparing each full collision group. Independent reviewer rerun raises `ProposalIntegrityError` before activation. Added evaluation/approval advisory tamper regressions assert accepted state remains unchanged.
- Test Gap: Covered by the new alias tamper regressions and existing collision-group tests.

### F-003: [Medium] Index construction created unreachable advisory commits (resolved in 1ea0f40c)

- Category: Correctness
- Location: `src/cruxible_core/playbill/proposal_note_projection.py:build`; `src/cruxible_core/playbill/git.py:proposal_review_commit_oid`
- Issue: Initial index construction called the materializing Git commit helper before any advisory ref retained the new object. Existing no-unreachable-commit submission tests failed.
- Impact: Ordinary submission introduced dangling derived commits and violated the repository's existing reachability behavior.
- Recommendation: Derive the advisory OID without writing an object; materialize and retain it in normal reconciliation before notes are published.
- Resolution: Commit `1ea0f40c` adds a hash-only helper using Git-normalized identities/dates/config, frames the unsigned commit body, and hashes it under the repository object format. Materialization compares real Git's result with the derived identity. Publication skips an entirely unmaterialized advisory alias but refuses missing original commits. No new retention-ref namespace is introduced.
- Test Gap: New real-Git identity tests cover SHA-1/SHA-256, subsecond and offset timestamps, final/multiple newlines, multiline and Unicode messages, ISO-8859-1 encoding headers, utf8 alias, and invalid dates/messages. They assert the identity-only path creates no unreachable objects.

## Complexity Assessment

The current index builds whole-instance admission/evaluation catalogs, deserializes all candidate records, derives advisory Git identities without materializing objects, and materializes grouping dictionaries. Submission builds it both before and after evidence persistence; approval and activation build it again. This is substantial work and memory on the local path, especially with large candidate bodies. Parent independently measured 6.8 seconds for an index build over 42 admissions and 26 MiB; optimization is separately assigned and not assumed correct by this review. Group rendering sorts proposal IDs and candidate digests and reads approvals per candidate; its complexity is proportionate to the group.

## Architecture Assessment

The central index is a coherent way to reconcile Git's one-note-per-object storage with Playbill's distinct proposal and candidate identities. Evaluation notes concatenate canonical admission/evaluation pairs ordered by proposal ID. Approval notes concatenate candidate-ordered signer lists, retaining each signature's original payload digest. No governed identity, candidate digest, signature, accepted generation, or original Git commit needs to change.

Read the implementation in this order: evidence-store canonical reads and missing-candidate distinction; index construction and both alias mappings; grouped canonical note rendering; subset recognition; submission validation-before-write and publication-after-evidence; approval publication; background reconciliation; strict activation. Production writers consistently acquire the review-projection lock before candidate locks, then the Git note lock. Reconciliation takes multiple candidate locks in sorted digest order. Network publication remains outside these locks. A private helper invoked directly by tests relies on its caller holding the review lock; production calls honor that contract.

Skipping missing evaluation/candidate records allows an interrupted unrelated evidence write to stop poisoning all future authoring. Present malformed candidate bytes still use strict evidence validation. Settlement independently reads its target proposal/candidate strictly, so skipping incomplete index rows does not itself make an incomplete proposal activatable.

## Test Coverage Assessment

The grouped collision tests exercise real Git timestamp truncation and distinct signed candidate payloads. Recovery and tamper tests meaningfully distinguish incomplete exact evidence subsets from edited, reordered, duplicated, or empty-note corruption. The original-alias crash case caught a real bug before approval. Concurrent colliding submissions and approval-before-note crash recovery are covered in the committed grouped-note tests. Reviewer intentionally did not duplicate that scope while it runs.

Subset recovery recognizes only nonempty canonical ordered subsets of current exact evidence. It cannot distinguish deliberate deletion of a genuine row from an interrupted publication; that is an inherent projection-repair tradeoff. Strict activation should continue refusing such present disagreement until an explicit/background repair runs.

## Documentation Assessment

The new module explains grouping and byte compatibility clearly. The service approval comment now correctly names the outer review lock. One inherited comment remains outdated: `git.py:approval_note_lock` says distinct candidates contend only for the note ref. Grouped publication now deliberately uses the instance-wide review-projection lock. Suggested wording: “The review-projection lock serializes grouped evidence/note updates across candidates; candidate locks preserve the narrower approval invariant inside that boundary.”

## Overall Contribution

The change solves a real identity mismatch without weakening signatures or inventing unique Git metadata. It should eliminate same-second collision overwrites and allow evidence-based reconstruction once every alias is included. The exact committed correctness change is approved after the recovery, strict advisory integrity, and hash-only identity corrections. Separate performance optimization must preserve this behavior and receive its own verification.

## Open Questions

None. Maintainer included strict validation of the target proposal’s original and advisory aliases in this batch.

## Suggested Follow-Ups

- Optimize full-candidate index construction while preserving fresh exact-byte evidence validation.
- Keep operational projection repair distinct from strict activation disagreement checks.
- Update lock-scope comments to describe the new instance-wide serialization accurately.
