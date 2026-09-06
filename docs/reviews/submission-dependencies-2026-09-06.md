# Code Review

## Verdict

Approved

The reviewed changes reuse only an exact-byte semantic manifest and dependency derivation, while candidate laws, body verification, current-coordinate query facts, approvals, and settlement checks continue to execute. The failure-only cold fallback resolves the refusal-order issue identified during review. No remaining correctness blockers were found.

## Manual Review Priority

- Priority: P1
- Reason: This adds cross-operation retained state to proposal evaluation and settlement, where provenance and validation parity matter.
- Suggested Human Review Focus: Exact semantic-byte binding; incremental failure fallback; ownership of returned dependency state; fresh freeze/corroboration evaluation; served settlement wiring.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/proposals.py`, `authoring/preflight.py`, `instance.py`, and `settlement.py`, relative to `/private/tmp/playbill-submission-dependencies/src/cruxible_core/playbill/` where appropriate.
- Untracked files: `src/cruxible_core/playbill/evaluation_state_cache.py`; the three focused test files listed below.
- Tests examined: `tests/test_playbill/test_evaluation_state_cache.py`, `test_served_evaluation_state.py`, `test_claim_policy_value_demand.py`; surrounding incremental dependency and policy helpers.
- Commands run: Read-only `git status`, `git diff`, `git rev-parse`, `rg`, `cat`, and bounded `sed` reads. Exact reviewed base: `dd5ae1215ea8701b960c713de437dbf64c8069b0`.

No tests or benchmarks were run by this reviewer, as requested to avoid contention with the implementers' validation. This report covers the final inspected failure fallback and test, committed as `3dfed69d`, not subsequent changes.

## Findings

No findings.

## Complexity Assessment

One semantic-tree snapshot is retained, bounded by 16,384 paths and 32 MiB of input path/body bytes by default. This is an input-accounting limit, not a total Python heap limit: dependency maps, pin edges, Merkle nodes, temporary updated maps, and detached outputs add memory. Every lookup still projects and compares the semantic tree and deep-copies the complete derived state; incremental misses parse changed members and update affected dependencies. Thus the change removes repeated parsing/hashing work without claiming constant-time lookups. Oversized inputs discard retained state and use the cold builder. All cache derivation and copying is serialized by one cache-local lock.

## Architecture Assessment

Read the implementation in this order: `EvaluationStateCache.derive`, the existing `build_tree_state`/`advance_tree_members`/`advance_tree_state` helpers, evaluator and service wiring, instance lifetime, then lazy Claim-policy values. The cache projects with the same semantic projection as the cold builder, retaining immutable exact bytes rather than trusting a coordinate, hash alone, or caller-provided derived state. The dependency parser currently has no compiler, clock, registry, or external-input dependency, so those fields are correctly absent from this cache's identity.

The incremental updater copies its mutable maps; cache outputs are deep-copied, including nested Pydantic records, and the caller's mapping is not retained. A failed advance falls back to the cold builder before publishing replacement state. This preserves cold refusal precedence when incremental duplicate detection would otherwise beat a later malformed member. The cache lock has no callback into instance locks, avoiding a reverse lock ordering with explicit refresh. Concurrent requests can replace the one retained snapshot but cannot obtain a state for different bytes.

The optional provider executes at the prior parent cold-build position. Candidate rebasing/cards, scoped member checks, closure, laws, bodies, query facts, and settlement's fresh base checks are unchanged. Replay's explicit parent state still takes precedence. Refresh clears the cache; a verified in-process activation handoff retains it for the next exact-byte advance. No proof representation or frozen wire format changed.

Freeze values remain evaluation-local and time-filtered; they are constructed once when any applicable freeze requirement needs them, including cross-ClaimType and unchanged Subject values. Empty and corroboration-only policies do not read these maps. Corroboration continues to obtain and verify the exact current facts coordinate and execute the accepted query.

## Test Coverage Assessment

The cache tests cover additions, revisions, repinning, retirement, deletion, empty trees, rewind, exact hits, changed-member parse demand, caller/nested-result mutation, excluded history bytes, capacity bypass, explicit clearing, concurrency, and cold refusal fallback with retention of the previous successful state. The served tests exercise two consecutive authoring submissions and activations in SHA-1 and SHA-256 repositories, forbid cold rebuilds during warm operations, compare complete candidate and evaluated tree against the cold evaluator, and verify reopened coordinate/history parity. Policy tests cover empty policy, real corroboration-only query execution, and complete once-per-evaluation cross-type freeze views for eligible/refused outcomes.

Final implementer-reported validation: 63 unique named cases passed. The final cache rerun passed all 12 cases in 1.01 seconds; the other named scopes cover served evaluation, authoring submission, proposals, incremental closure/manifest, policy/request reuse, and cross-type freeze. Ruff check and format check passed for all eight changed source/test files, scoped Mypy passed for five source files, and `git diff --check` passed. No full suite or golden corpus was run for this review.

## Documentation Assessment

The cache docstring accurately distinguishes disposable derivation state from authority and input-byte limits from heap usage. The evaluator documentation now explains the optional derivation provider; inline comments explain freeze demand and cold error precedence. No public contract inventory or historical digest update is required for these internal changes.

## Overall Contribution

A cohesive optimization of repeated proposal operations, using established incremental dependency machinery and explicit ownership rather than a second proof path. The retained full-state copying and input scan remain visible costs, but the scope is appropriately bounded and preserves the ledger as authority.

## Open Questions

None.

## Suggested Follow-Ups

None.
