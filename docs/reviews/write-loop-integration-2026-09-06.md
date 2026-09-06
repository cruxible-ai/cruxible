# Code Review

## Verdict

Approved.

The four fixes compose without changing the ledger's authority, signed candidate rules, activation checks, or the lifetime of the data being reused. The public response fix preserves the actual intent version; the three performance fixes remove repeated work while retaining the existing verification and lock boundaries. This approval covers code in exact range `ee895e03aadd0905b3be30d14b001eebcfa1c392..283ca306cfeebabc84becb271079a02266059ba7`; it does not depend on the unfinished full-loop benchmark or authorize deployment.

## Manual Review Priority

- Priority: P1
- Reason: The range touches public authoring serialization, deterministic evaluation reused during settlement and replay, integrity-sensitive review projections, and targeted recovery cleanup.
- Suggested Human Review Focus: Versioned response assertions and catalog binding; request-local evaluator lifetime; review/candidate/note lock ordering; strict activation versus projection repair; unchanged deletion proof after the orphan prefilter.

## Scope Reviewed

- Changed files: `packages/cruxible-client/src/cruxible_client/contracts/authoring/models.py`, `authoring/wire_catalog.py`; `src/cruxible_core/playbill/proposals.py`, `git.py`, `instance.py`, `proposal_note_projection.py`, and `recovery.py`.
- Changed tests: `tests/test_client/test_authoring_intent_response_versions.py`, `test_claim_attestation_contract_catalog.py`; `tests/test_server/test_playbill_authoring_intent_roundtrip.py`; `tests/test_playbill/test_evaluation_request_reuse.py`, `test_review_projection_snapshot.py`, and `test_recovery_orphan_prefilter.py`.
- Untracked files: Only this integration report is authored by this review. Other uncommitted review reports and the full-loop benchmark harness are outside the approved code range.
- Tests examined: The changed regressions and relevant existing authoring reference, grouped-note, archive, publication concurrency, approval concurrency, and recovery cleanup tests; the individual review reports for the four fixes.
- Commands run: `git log`, `git diff --stat`, exact range diffs, `git rev-parse`, and focused `rg`/source reads in `/private/tmp/playbill-write-loop-latency`.

No tests were repeated during this integration review because the implementation scopes already had focused validation and a heavy baseline benchmark was active. Prior results are attributed to the implementer/individual reviewers: V2 response fix 26 named tests; fresh Git snapshot 51 distinct relevant cases across named scopes, scoped Ruff/format and three-source mypy; recovery prefilter 28 tests including its four new cases, scoped Ruff/format and mypy. The evaluator's parent-run scope passed 54 distinct cases across Claim corroboration, policies, authoring change sets, and the strengthened three-case reuse file; its reviewer separately ran three QueryDefinition law tests. Evaluator Ruff and one-source mypy also passed. Counts are per scope and are not summed into an overlapping grand total. No full suite, journal goldens, canonical-checkout testing, or live-instance operations were performed by this review.

| Commit | Reviewed change |
| --- | --- |
| `29989c0cecd6653c0e593430c121eb814bfd4e04` | Preserve V1/V2 intents across all six authoring response wrappers. |
| `89232e926e584ff62ef193a34371aaef6445c15b` | Reuse invariant inputs within one deterministic proposal evaluation. |
| `002428f093cb507d688c2bc39c523ffd1a3e2945` | Batch fresh Git review proofs and reuse exact existing advisory commits. |
| `283ca306cfeebabc84becb271079a02266059ba7` | Reject unsigned orphan commits before deriving full parent state. |

## Findings

No findings.

## Complexity Assessment

The evaluator shares parsing only for the same `(path, exact bytes)` within the parent and candidate policy views, and derives accepted referents and candidate identity mappings once per evaluation. Temporal and lifecycle filtering still occurs per view. The parsing memo is cleared before policy evaluation. Work remains proportional to the relevant tree and candidate members, with transient storage for parsed Claims and the shared mapping; there is no process-wide evaluation cache.

Reconciliation replaces per-commit materialization and individual object/note subprocesses with fresh batches and exact verification. Its 128-object batch size bounds object count per subprocess, not response bytes or total retained snapshot memory. A reconciliation retains object and note bodies proportional to the requested historical inventory. Remaining per-admission Git identity derivation and note-ref listings still scale with retained history.

The orphan prefilter avoids parent-tree read and derivation for commits that fail the accepted parent's daemon-signature check. Signed orphans pay an additional signature check and continue through the complete existing proof. The unreachable-object inventory scan remains unchanged.

These effects compose at separate stages of the same write loop. The measured reconciliation improvement is a subphase result; this review makes no inference about the eventual full-loop percentage or latency.

## Architecture Assessment

Read the range in this order:

1. **Public response fidelity.** `models.py` defines a private discriminated V1/V2 intent union and applies it to view, list, submit, and all three insertion response wrappers. The actual nested tag selects the schema; required V2 reference expectations survive serialization and are still required during parsing. V1 remains accepted. Stored intent definitions, identity computation, reference-check semantics, candidate formation, and receipt rules are unchanged. The independent authoring wire catalog and its cross-check pin move together; this range does not re-pin the separate SDK handshake snapshot.
2. **Deterministic evaluator locality.** `_claim_admission_evaluations` creates and clears its own parsed-Claim map. `_evaluate_scoped_members` lazily creates its accepted-referent set and candidate-identity map after the preexisting removal/unregistered checks. The shared consumers are read-only and the accepted coordinates are immutable. Preparation, submission, activation reevaluation, and accepted-history replay continue to invoke the evaluator independently; one stage cannot reuse another stage's stale authority, evidence, or policy result.
3. **Derived review projection.** The instance captures evidence-derived original/advisory groups under its existing review lock, reads fresh exact Git objects and note bodies, and reuses only advisory commits whose actual bytes hash to the derived OID. Required trees/parents retain their type/presence proof. Missing advisory commits still use the original checked materialization path. Open/settled refs are retained before notes are published; group bytes and valid-subset recovery rules remain unchanged. Partial snapshot entries fall back to ordinary reads.
4. **Recovery rejection order.** The orphan prefilter repeats the same parent-root daemon-key check already required by `_verify_successor`, but performs it before reading and deriving the full parent tree. A passing signature is not collection authority: the complete existing successor proof, including the second signature check, must still succeed. The collector still protects current main, commits reachable from refs, and objects retained by other refs.

Interaction checks found no conflicting ownership or authority source. The response union transports reference assertions to clients; the evaluator does not cache or discard them. The evaluator's reuse is local even when called by recovery. Git snapshot reuse concerns derived review presentation, not the signed generation or accepted state. Skipping unnecessary review-commit creation reduces residue but does not give the orphan prefilter permission to delete unsigned proposals.

The existing outer review lock encloses evidence/index construction, snapshot capture, and note comparison/publication. Approval and strict settlement-note writers take that same lock before candidate locks. Snapshot reuse therefore adds no interval in which an internal approval writer can make a group stale. Note writes retain the existing inner Git-note lock. Activation continues direct strict comparison of original/materialized advisory notes, fresh settlement verification, prebuild, main CAS, durable serving publication, and ordinary recovery; none of these checks is replaced by the snapshot or prefilter. The asynchronous mirror's separate publication/state locks and acknowledgement semantics are unchanged.

## Test Coverage Assessment

The response tests exercise both versions in all six wrappers, exact nested JSON, restored model type, and refusal of missing V2 assertions. HTTP coverage reaches the actual create/get/resume/list/submit response serialization. The authoring catalog check still recomputes the schema digest; it does not replace verification with a constant-only assertion.

Evaluator tests cover exact path/byte matching, changed and malformed bytes, effective-time boundaries, candidate parity, one-time referent derivation, and the precise removal-only refusal. Existing Claim policy, corroboration, and QueryDefinition consumers were included in its focused review.

Git snapshot coverage includes both Git object formats, changed notes on fresh calls, absent notes, missing/wrong-type dependencies, corrupt objects, malformed batch framing, replacement refs, large object inventories, commit reuse, partial-snapshot fallback, grouped collisions, archive rebuilding, and concurrent approval/publication. It explicitly checks tamper refusal rather than treating a projection snapshot as authority.

Recovery coverage checks unsigned debris avoids parent materialization and remains present; a signed but structurally invalid child also remains present. Existing positive tests ensure valid failed generations and losing-CAS residue are still collected. The four fixes do not share mutable cross-request cache state, so no additional concurrency test is essential solely because they are combined in this range.

## Documentation Assessment

The new comments and docstrings explain the relevant lifetimes and proofs: nested tag selection, request-local parsing with per-view temporal filtering, fresh snapshot capture under the review lock, and the distinction between a signature prefilter and collection authority. The individual review reports preserve testing attribution and the snapshot's object-count versus byte-memory limitation.

The final user-facing performance report should identify the benchmark fixture, baseline and final commits, what was measured, and whether the result is an entire operation or one subphase. Completion of that report and the still-running benchmark is separate from this code approval.

## Overall Contribution

This is a cohesive set of fixes for the measured write loop: it repairs a diagnostic response defect, removes repeated deterministic derivation, cuts local Git subprocess work, and rejects irrelevant recovery debris earlier. The range preserves accepted-state authority and keeps the reused data scoped to a single evaluation or locked reconciliation. No integration blocker remains in the reviewed code.

## Open Questions

None for code approval.

## Suggested Follow-Ups

- Complete the separate full-loop benchmark and report exact result parity before making end-to-end performance claims.
- If retained review notes become large, consider streaming or byte-budgeted snapshot storage while preserving fresh-byte proof and the same review-lock lifetime.
