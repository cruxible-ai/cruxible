# Code Review

## Verdict

Changes requested.

The ordinary submission, approval and review-reconciliation paths preserve fresh-byte validation, mutation isolation, lock order and standalone reconstruction. One bounded compatibility edge remains: candidate-specific OID lookup no longer preserves all groups when the explicitly supported duplicate-admission fallback is used. This is an advisory review-projection issue, not an accepted-ledger or signature bypass.

## Manual Review Priority

- Priority: P1
- Reason: Shared review evidence and publication paths rely on exact grouping and fresh inputs.
- Suggested Human Review Focus: Cache freshness and returned-model isolation; alias derivation configuration; interruption completion; candidate-specific group parity; review-before-approval lock order.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/proposal_note_cache.py`, `proposal_note_projection.py`, `proposal_evidence.py`, `proposals.py`, `git.py`, `instance.py`, and `service/documents.py`. Instance projection-assembler arguments belong to the separate projection review.
- Range: `caa174dde80677a9bffab245a23b2bd33eb15016..612be6c2ca4953cbceb545fdda206c03b81078c2` in `/private/tmp/playbill-grep-surfaces-v1`.
- Untracked files: none at inspection; no production edits made.
- Tests examined: `test_proposal_note_cache.py`, `test_grouped_proposal_notes.py`, `test_review_commit_identity.py`, added base-read test in `test_proposals.py`; surrounding candidate-summary and publication logic.
- Commands run: git status/diff and source searches; focused pytest below; minimal direct cold-builder reproduction of duplicate-admission alias grouping.
- `PYTHONPATH=src:packages/cruxible-client/src /Users/robertmalone/Git/cruxible-core-0.2-stretch-goals/.venv/bin/python -m pytest tests/test_playbill/test_proposal_note_cache.py tests/test_playbill/test_grouped_proposal_notes.py tests/test_playbill/test_review_commit_identity.py -q`: **47 passed in 60.87s**.
- Tests ran only in the isolated worktree, coordinated with the integration manager. No full suite or golden corpus. No fresh lint/type run by this reviewer; integration manager owns those checks.

## Findings

### F-001: [Medium] Candidate lookup omits aliases retained by duplicate-admission fallback

- Category: Correctness
- Location: `src/cruxible_core/playbill/proposal_note_projection.py:98`
- Issue: `ProposalNoteCache.load` explicitly delegates duplicate admission IDs to the cold builder to retain historical behavior. That builder accumulates original and advisory groups for every encountered record, although its admissions/review-OID dictionaries keep the last record for an ID. The new `oids_for_candidate` reconstructs OIDs from those final dictionaries, rather than looking up actual group membership. A duplicate ID with a distinct rationale therefore leaves the earlier advisory alias in `proposal_ids_by_oid` but omits it from candidate lookup. A direct cold-builder reproduction returned groups `original`, `alias:before`, `alias:after`; the new lookup returned only `original`, `alias:after`, while the previous implementation returned all three.
- Impact: Candidate-specific approval validation/publication can omit earlier retained groups for this tolerated foreign-file evidence shape. Existing notes on the omitted alias are neither checked by that approval operation nor updated until full reconciliation. Normal immutable writer output does not generate this shape, so this does not block ordinary proposals or affect accepted-ledger authority.
- Recommendation: Derive candidate-to-OID access from the actual group membership, or retain the previous full-scan lookup specifically for the duplicate-admission fallback. Do not silently tighten evidence admission semantics as part of this performance patch.
- Test Gap: Add two canonical admissions with the same proposal ID under distinct filenames and differing review rationale (and optionally original commit), build through the cache fallback, and compare candidate-specific OID lookup to the previous group-based expression. Existing tests compare the cache to the modified cold builder and cover duplicate evaluations, but not this duplicate-admission inverse-lookup behavior.

## Complexity Assessment

Loads still enumerate and freshly read all admissions/evaluations and referenced candidate bodies. Record decode and Git alias derivation are reused only when relevant inputs match. Group comparison, copied maps, returned deep copies and reverse-map construction remain linear in evidence count; this is a useful intermediate performance step, not changeset-scoped operational evidence. Retention is explicitly bounded by record count and accounted source/summary bytes, not interpreter heap. The base-tree reuse removes a redundant operation-local blob transfer without relaxing object verification.

## Architecture Assessment

The instance owns the cache and passes it into the shared proposal service. Standalone services retain their transport-compatible cold builder. Admission/evaluation validation is factored into the same canonical validator, while candidate summaries use their existing fresh-byte verifier. Operational evidence remains distinct from accepted authority. Review and per-candidate approval locks retain the existing order; cached indexes never retain approval reads or Git note contents. Longer-term derived-state centralization remains appropriate but is not a prerequisite for this intermediate integration.

## Test Coverage Assessment

Focused tests pass and exercise model/set poisoning, same-size restored-mtime corruption, symlinks, external replacement/deletion, interrupted candidate completion, budgets/restart, Git encoding changes, shared commits, overlapping original/advisory aliases, approval groups, crash repair and strict corruption refusal. The duplicate-admission fallback needs the specific parity test described above. This reviewer did not rerun live daemon/SDK flows or archive/publication concurrency suites; those remain integration-manager coverage.

## Documentation Assessment

Comments clearly explain fresh-byte proof, cache ownership, bounds and caller lock obligations. The fallback comment claims historical duplicate-admission behavior, which motivates F-001. No public wire changes or new user flow require documentation. Existing review reports distinguish warm/cold workloads and self-review from independent review appropriately.

## Overall Contribution

The change is cohesive and removes substantial repeated operational-index work while preserving the ledger boundary. Once the bounded inverse-lookup parity issue is corrected, this reviewed slice is suitable for integration subject to the separate projection and deployment checks.

## Open Questions

None beyond resolving F-001.

## Suggested Follow-Ups

- Move cache lifecycle and instrumentation under the proposed instance-derived-state owner when that design is implemented.
- Replace global operational evidence inventory scans only after a reliable revision/replay contract exists; do not weaken fresh-byte corruption detection based on timestamps.

## Resolution Addendum

The integration manager requested and authorized a bounded correction after the initial independent review. I implemented it in `proposal_note_projection.py` and `test_proposal_note_cache.py` only; the manager is reviewing that patch before committing.

The candidate-to-OID inverse is now constructed from actual `proposal_ids_by_oid` groups, preserving earlier original and advisory aliases even when tolerated duplicate IDs overwrite the final admission dictionary. Lookup returns a fresh set. The existing candidate-to-proposal map remains available. Added tests build duplicate canonical admission records with distinct original/advisory aliases, compare against the explicit historical group-scan expression, verify returned-set isolation, and verify that mutation of a detached inverse cannot poison the retained cache.

Post-fix verification:

- `pytest tests/test_playbill/test_proposal_note_cache.py tests/test_playbill/test_grouped_proposal_notes.py -q`: **25 passed in 54.32s**.
- Ruff check and format check on the two modified files: passed.
- Mypy on `proposal_note_projection.py`: passed.
- `git diff --check`: passed.

F-001 is resolved by the working-tree patch, pending the integration manager's review/commit. There are no remaining findings in this review scope. The initial review was independent; this addendum's fix verification was performed by the fix author and must not be described as an additional independent review.


## Integration Manager Closure

The manager inspected the correction and regression tests, committed them as `4b72c3ba`, and confirmed final changed-file static checks and authenticated installed SDK replay. F-001 is closed. The historical initial verdict above records the independent pre-fix review; the final integration verdict and deployment evidence are in `batch0-performance-integration-2026-09-07.md`.
