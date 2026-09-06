# Code Review

## Verdict

Approved

The request-local reuse preserves current evaluator semantics: exact path/content keys control Claim parsing reuse, temporal filtering still runs per view, and member dispatch receives the same derived identities and referent coordinates. All consumers of the newly shared identity mapping are read-only; the accepted-coordinate collection is immutable. The suggested removal-regression strengthening is resolved: the test now requires exactly `playbill.subject.removal_unsupported`, so an earlier refusal cannot satisfy it accidentally.

## Manual Review Priority

- Priority: P1
- Reason: Shared deterministic proposal evaluation also participates in frozen-history verification, so semantically neutral reuse deserves focused review.
- Suggested Human Review Focus: Exact-byte cache eligibility and lifetime; temporal/lifecycle filtering; lazy derivation after removal/unregistered checks; read-only QueryDefinition/ClaimType mapping consumers.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/proposals.py`, unstaged against HEAD `29989c0cecd6653c0e593430c121eb814bfd4e04`. Reviewed source diff SHA-256: `1e08bf539be71a1a663d3f013bb37af89ab54ce77f69ea79e9f5488a5d81089e`.
- Untracked files: `tests/test_playbill/test_evaluation_request_reuse.py`, including the subsequently added removal-only poison test. The final removal-only assertion was additionally inspected after the original test snapshot.
- Tests examined: New request-reuse tests; Claim policy and corroboration integration tests; QueryDefinition traversal-law tests. Surrounding consumers examined in ClaimType laws, QueryDefinition laws, Claim policy normalization, and accepted-referent coordinate derivation.
- Commands run: `git status --short`; scoped staged/unstaged diffs; targeted `rg` and source reads; `ruff check src/cruxible_core/playbill/proposals.py tests/test_playbill/test_evaluation_request_reuse.py` (passed); `pytest -q tests/test_playbill/test_query_definition_traversal_law.py` (3 passed in 0.93 s).

All commands ran in `/private/tmp/playbill-write-loop-latency` using the canonical virtualenv and worktree `PYTHONPATH=src:packages/cruxible-client/src`. An initially overlapping targeted run of new tests, Claim policies, corroboration, and query tests was interrupted after 20 passes and no failures when the parent reported running the same scope; this is not represented as a completed suite. Parent owns the complete evaluator regression run. No full suite, golden corpus, canonical-checkout testing, or source edits were performed for this review. Other workers' Git/harness changes are excluded.

## Findings

No findings.

## Complexity Assessment

For N accepted Claims and D changed/new candidate Claim bodies, parsing across the two policy views moves from approximately 2N+D parses to N+D. Both sorted tree traversals and both time-dependent projections remain, so traversal remains O(N log N) while expensive validation/canonical rendering is reduced. The memo adds transient O(N+D) parsed-model storage plus keys referencing existing immutable bytes, then is explicitly cleared before policy evaluation; it is not retained across requests or authority epochs.

For M evaluated members and H accepted history/tree content, accepted referent derivation moves from M repetitions to one. The candidate identity mapping similarly moves from one O(N) allocation per evaluated member to one per evaluation. Existing full accepted dependency rebuilding and repeat evaluation at separate protocol stages remain outside this change.

## Architecture Assessment

The implementation stays inside the existing deterministic evaluator and introduces no global cache, new authority source, API, wire version, persistence, or provider behavior. Reuse keys include path as well as content, preserving filename/identity validation. The first valid registered member triggers referent and identity derivation at the same point as before; removal-only and unregistered-member branches avoid that work.

Mutation review found no shared-data writer: `_effective_claim_values` only reads parsed Claim fields; its two projections are constructed before any policy runs; policy context normalization rebuilds canonical nested values. QueryDefinition and ClaimType law consumers use `.get()` on the shared `candidate_identities` mapping. The candidate states/tree are not mutated during this member loop. Accepted referents are a `frozenset` of frozen coordinates.

## Test Coverage Assessment

The exact-path/bytes test compares memoized views with uncached output, checks effective interval boundaries, modifies a candidate value, and requires malformed changed bytes and wrong-path valid bytes to raise. The multi-Claim test verifies one accepted-referent derivation per evaluation and complete candidate/tree parity across repeated evaluations. Existing policy/corroboration and QueryDefinition tests provide additional consumer coverage through the parent and reviewer runs.

The new removal-only poison test addresses the principal lazy-ordering risk. The final assertion now requires exactly `playbill.subject.removal_unsupported`, closing the suggested coverage precision improvement. The parent owns its final targeted rerun.

## Documentation Assessment

The helper docstring accurately describes request locality, exact content/path matching, and repeated temporal filtering. The cache-clear comment usefully records its intended lifetime. No public documentation or schema update is needed because the change adds no served behavior or contract.

## Overall Contribution

This is a cohesive, bounded reduction of repeatedly derived evaluation data. It targets measured costs while keeping authority checks, receipt resolution, error-producing parsing, and effective-time policy evaluation in place. The transient memory tradeoff is explicit and limited to one evaluation.

## Open Questions

None.

## Suggested Follow-Ups

- Retain full candidate/receipt parity checks when extending reuse to accepted dependency state or across protocol stages; this review approves only request-local reuse.
