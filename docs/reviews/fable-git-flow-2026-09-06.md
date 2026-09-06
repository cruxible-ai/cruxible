# Code Review

## Verdict

Approved with comments.

The original Fable range contained two reproducible review-state defects and an incomplete advisory-note integrity check; the integrated branch now fixes them in `a03bd632` and `1ea0f40c`. I found no remaining correctness blocker in the reviewed Git review flow, including its asynchronous-publication integration. Approval covers those completed fixes; the separate compact-summary performance follow-up is being reviewed independently and should retain its own validation evidence.

## Manual Review Priority

- Priority: P0
- Reason: Reviewers inspect Git aliases while settlement consumes signed candidate evidence. Ref identity, shared-note grouping, and refusal boundaries must remain consistent across those surfaces.
- Suggested Human Review Focus: Deterministic grouping of original and advisory commit aliases; strict preactivation verification; reconstruction of settled refs from evidence; exact Git identity parity; local lock ordering and publication watermarks.

## Scope Reviewed

- Changed files: Original range `f8ce3336d58b055368c34548ce0bf46609c50428..24273db957f4ef12171c877202ce12bd873b177d`, emphasizing `playbill/git.py`, `instance.py`, `proposal_notes.py`, `proposal_evidence.py`, `proposal_message.py`, `proposals.py`, `settlement.py`, `workspace_advertisement.py`, `service/documents.py`, `service/review.py`, mirror configuration/contracts/transports, and the rationale authoring path. The encompassing range contains 72 files, including unrelated performance work and mechanical snapshots; those unrelated changes are not independently reapproved here.
- Integration examined: Performance branch at `c0b47fa8`, merge `e5604953` of canonical Playbill `5a2e3a7b`, then review corrections `a03bd632` and `1ea0f40c`. The original Fable tip is already an ancestor of canonical Playbill; this is an integration review, not an unlanded original patch.
- Untracked files: New review regression files and `proposal_note_projection.py` were inspected and committed with the fixes. The separate `candidate_review_summary.py` optimization and shared prose extraction belong to another agent and are excluded from this review's final approval.
- Tests examined: Proposal notes, proposal prose, approval-note concurrency, proposal reachability/refusal, workspace advertisement, mirror publication, archive reconstruction, grouped notes, and derived Git identity parity.
- Commands run: `git status --short`; exact-range diffs and surrounding source reads; `git merge-base --is-ancestor 24273db957f4ef12171c877202ce12bd873b177d 5a2e3a7b`; isolated Python reproductions of missing archives and shared-commit note overwrite; scoped pytest commands listed below; Ruff on changed source/tests; mypy on six changed core modules. All execution used the temporary worktree and its `PYTHONPATH`, with the existing canonical environment only as the Python runtime.

No full suite, journal corpus, canonical-checkout tests, deployment, or publication to external remotes was performed by this reviewer.

## Findings

No remaining findings in the completed reviewed slice.

The original defects and their disposition are recorded under Overall Contribution so they cannot be mistaken for unresolved findings.

## Complexity Assessment

The evidence index builds the evaluation lookup once, eliminating reconciliation's repeated evaluation-file scan for every admission. Group construction is linear in completed admissions and evaluations, with deterministic sorting within shared-OID groups. Every original or advisory alias receives one complete group; reconciliation deduplicates aliases instead of repeatedly overwriting one note for each admission.

Reconciliation still considers retained history, not only open proposals. Full historical candidate parsing and note comparison can remain expensive: the parent measured about 6.82 seconds for an earlier index build over 42 admissions and approximately 26 MiB of candidate records. That measurement predates the final hash-only identity path and the separate compact-summary optimization; it is not a final benchmark. Local projection locks now span coherent evidence/group work, so this cost also affects contention. Network I/O remains outside those locks.

Hash-only review identity uses one read-only Git identity/configuration query and creates no Git objects. Materialization continues through Git and verifies the exact derived OID before retaining refs. This preserves the existing no-dangling-proposal-commit invariant rather than relaxing reachability tests.

## Architecture Assessment

The change follows the existing service, evidence, and Git boundaries. A reviewer should read it in this order:

1. **Authoring contracts and prose.** Optional change-set rationale travels through the request and immutable admission record. Its absence preserves old canonical record bytes. Rationale remains covered by the creation fingerprint; candidate/admission identities are not rewritten during the fixes. Commit messages remain prose and are never parsed as machine state.
2. **Canonical evidence shapes.** `proposal_notes.py` preserves the original admission/evaluation pair and candidate approval-list encodings. `proposal_note_projection.py` combines pairs in proposal-ID order when multiple admissions share a Git OID. A single admission retains its original two-line note bytes.
3. **Alias grouping.** The index includes both original admission commit OIDs and reconstructed advisory OIDs. This handles aliases that collide across those two roles. Candidate signatures remain associated with their signed payload digest, even when distinct candidate timestamps collapse to one Git second and therefore one commit OID.
4. **Write orchestration.** Expensive compilation/evaluation remains outside the review lock. Coherent admission/evaluation persistence, grouped-note changes, and approval projection use local review locking. Lock order is review, then candidate, then Git note; publication and workspace refresh occur after the approval lock is released.
5. **Review inventory and archives.** Reconciliation classifies complete durable admissions using accepted history, withdrawals, and the current parent root. It reconstructs both open and settled refs directly from evidence, including archives never previously observed as open. It also repairs original and advisory note aliases, including complete refused admissions.
6. **Settlement verification.** Activation compares the complete expected groups on the target admission's original and advisory aliases before activation. Present mismatches refuse. Missing notes on existing objects can be reconstructed; a never-materialized advisory alias has no reachable review surface yet and is materialized by normal reconciliation. Neither note content nor commit prose replaces candidate evidence as authority.
7. **Recovery and publication.** A nonempty, canonically ordered subset of exact current evidence is an incomplete projection that reconciliation can extend after a crash. Edited records, unrelated records, duplicates, reordering, and an empty approval note masquerading as an earlier nonempty group refuse. Strict activation does not silently repair a present incomplete group. Remote publication retains the previously reviewed exact snapshots, expected-old leases, bounded retries, and explicit publication watermarks.

The added `ProposalService` lock dependency is explicit; the instance factory and direct service test consumers supply it. The helper creates no new accepted digest format or authority plane.

## Test Coverage Assessment

The final correctness commit passed **66 focused tests in 67.92 seconds**:

- 24 Git identity/refusal cases across SHA-1 and SHA-256, covering fractional seconds, UTC/offset spellings, allowed actor edge characters, prose/newlines, encoding headers, invalid dates, blank messages, and NUL refusal.
- 14 grouped-note cases covering shared identities, distinct signed candidate digests within a Git second, original/advisory alias overlap, concurrent submissions, interrupted evidence and approval writes, incomplete-admission isolation, exact subset repair, corruption refusal, and preactivation advisory-note tamper rejection.
- 28 proposal cases, including the existing no-unreachable-commit regressions that caught and rejected an intermediate implementation.

The archive correction separately passed five regressions for late mirror binding, activation/withdrawal before observation, deleted archive/note reconstruction, and coalesced submit/withdraw. A previous combined integration run passed 58 of 60 cases; its only failures were the two orphan-commit cases subsequently fixed and included in the final passing 66-case run. Ruff and mypy on six changed core modules passed.

The final post-fix notes/archive/review-concurrency/approval-concurrency/prose integration check passed **37 tests in 57.90 seconds**. No known failing test remains in the reviewed implementation.

## Documentation Assessment

The code now explains shared-OID grouping, retained-history reconciliation, local lock order, pure Git identity computation, and the distinction between incomplete and edited projections. Single-admission note encoding remains unchanged, minimizing reader disruption.

The parent has added the corresponding shared-note grammar clarification to `docs/for-ai-agents.md`, `docs/cli-reference.md`, and `CHANGELOG.md`: pairs are ordered by proposal ID, distinct signed payloads remain distinct, and strict original/materialized-alias checks are separate from incomplete-subset repair. This is an additive clarification for note consumers, not a new renderer requirement.

## Overall Contribution

The Git-centered review surface is useful and cohesive: it exposes actual evaluated bytes, canonical evidence, signed approvals, and understandable prose without introducing a competing rendered truth surface. The existing async follow-ups address the original synchronous remote push and stale-snapshot risks.

This review reproduced and resolved three issues:

- **Missing settled archive:** Submit and withdraw before configuring a mirror, then publish. The original code reported `current` while the expected settled ref was absent. `a03bd632` reconstructs archives and their notes from durable evidence, independent of prior open refs.
- **Shared-commit evaluation overwrite:** Submit identical content under different proposal refs at the same timestamp. The commits matched, the proposal IDs differed, and the second evaluation note overwrote the first, causing legitimate activation to refuse. `1ea0f40c` groups all original/advisory aliases deterministically and preserves candidate-scoped signatures.
- **Unchecked advisory corruption:** The original admission note could remain intact while the advisory note actually shown to reviewers was edited. `1ea0f40c` verifies both target aliases before activation; regressions assert accepted state stays unchanged on refusal.

I consider the combined corrected review flow safe to integrate. The original range alone should not be treated as having passed this review without these corrections.

## Open Questions

None.

## Suggested Follow-Ups

- Finish the independently reviewed compact-summary optimization and measure the actual project-state write loop; do not infer end-to-end improvement from index microbenchmarks alone.
- Maintain the previously documented operational limits of atomic mirror argument size and per-command transport deadlines; these are separate from governed acceptance durability.
