# Code Review

## Verdict

Approved with comments.

The original Fable range required changes: its produced captures could bypass the accepted acquisition policy's replayability requirement, reproduced on both direct and Line runs. The blocker is resolved by `dc029b2c51b9540654aaa4f18806eb83ea0a2af0` in the isolated integration branch, with independent source review and 41 focused passing tests. Approval covers the workspace-file Source observation slice with independent coherence, not the still-unserved proposal and reading loops.

## Manual Review Priority

- Priority: P1
- Reason: This slice brings external observations into procedure execution and binds their authority and provenance across admission, provider invocation, capture retention, and replay.
- Suggested Human Review Focus: Accepted policy eligibility; direct versus Line admission identity; receipt-to-capture correspondence; unsupported terminal boundary; completed replay versus fresh observation.

## Scope Reviewed

- Exact original range: `24273db957f4ef12171c877202ce12bd873b177d..5a2e3a7b0fca631f483de0927396b1b83869f35c`.
- Original checkout: `/Users/robertmalone/Git/p2-worktrees/effectful-s3`, clean and at the reviewed head. Correction checkout: `/private/tmp/playbill-state-loop-design`, integration merge `e5604953` and later concurrent parent-owned changes.
- Changed files: 30 files in the original range. Substantive source review covered procedure SDK authoring, authoring inputs/models/lowering, acquisition policy pin naming, graph-v4 admission models, Source observation result contracts, direct/Line service orchestration and reconstruction, runtime reader injection, repair vocabulary, and CLI output. Documentation, change log, and focused tests were inspected. Generated snapshots were inspected as inventory changes, not independently regenerated.
- Untracked files: None in the original Fable checkout. Correction adds `tests/test_playbill/test_procedure_source_policy.py`; other agents' concurrent integration changes are outside this review.
- Tests examined: Source run oracle, acquisition plan, authoring procedures, Line HTTP refusals, graph-v4 Source runtime, effectful terminal darkness, and new policy regressions.
- Commands run: `git status --short`, exact-range diffs and targeted `rg`/`sed`; canonical virtualenv Python with `PYTHONPATH=src:packages/cruxible-client/src` executing only noncanonical checkout tests. Original scope: `pytest tests/test_playbill/test_procedure_source_runs.py tests/test_playbill/test_procedure_acquisition_plan.py tests/test_playbill/test_authoring_procedures.py tests/test_server/test_playbill_line_run_refusals.py -q --tb=short -x` — **78 passed in 260.46s, no skips**. An initial sandboxed run was interrupted after provider fixture failures caused by denied uv-cache access; the successful rerun used approved escalation. No full suite, journal golden corpus, canonical checkout tests, or live instance operations.
- Correction validation: `pytest tests/test_playbill/test_procedure_source_policy.py tests/test_playbill/test_procedure_acquisition_plan.py tests/test_playbill/p2b4_unit1/test_source_v4_runtime.py -q --tb=short -x` — **37 passed in 23.05s**. Existing Source tests selected by `-k 'a_direct_run_reads or a_line_occurrence_reads or a_changed_file or a_crash_between'` — **4 passed in 23.95s**. Both commands set `CRUXIBLE_PROVIDERS_CHECKOUT=/Users/robertmalone/Git/cruxible-providers`, verified at `8e7436f359dd28c2afdc4b9941fd09e33fa0e470`. Final Ruff check/format on four correction files, mypy on three source files, and scoped `git diff --check` passed. Another agent independently reviewed the correction source and found no blocker.
- Correction files: `src/cruxible_core/playbill/procedures/acquisition.py`, `src/cruxible_core/playbill/procedures/execution.py`, `src/cruxible_core/service/playbill_procedure_runs.py`, and new `tests/test_playbill/test_procedure_source_policy.py`. No wire-schema movement or snapshot regeneration is required for this correction.
- Reproduction: `/private/tmp/probe_fable_policy.py`, run at the exact Fable head with `PYTHONPATH=.:src:packages/cruxible-client/src`. Uses temporary instances and the real pinned provider materialization, with the existing oracle's process-boundary invoker stub.

## Findings

### F-001: [High] Produced Source captures bypass accepted replayability eligibility (resolved)

- Category: Correctness
- Location: `src/cruxible_core/service/playbill_procedure_runs.py:1233`; `src/cruxible_core/playbill/procedures/acquisition.py:178`
- Issue: The new shared planner binds the accepted policy but checks only the existence of classification selectors, the capture contract, and an output-byte cap. The runtime acquisition helper then selects every `acquired` capture without checking `InputAcquisitionRuleV1.permitted_replayability`. A pinned policy permitting only `exact` therefore accepts an `attested_only` workspace Source observation. This disagrees with the established accepted-capture selector in `contracts/acquisition_policies.py:512`.
- Impact: Both served lanes return `succeeded`, expose the derived value, and retain a selected capture despite the accepted policy excluding that replayability. Reproduction printed `policy permits ('exact',)`, `requested replayability attested_only`, then `result succeeded {'severity': 'high'} spawns 1`, followed by the same successful Line result. The provenance is recorded, but the claimed policy constraint is not honored.
- Resolution: Corrected in `dc029b2c51b9540654aaa4f18806eb83ea0a2af0`: shared post-acquisition replayability and age checks, selected-only output association, reservation release for denied captures, and typed refusal of unsupported Source coherence policies. Refused observations preserve attempted read receipts without a selected capture digest; conservative defaults still require independent authorization.
- Recommendation: Grade the actual acquired envelope before it enters the run context, applying the policy's declared failure behavior and conservative-default authorization. Do not advertise a denied/defaulted/omitted capture as selected run output. Apply freshness at the admitted evaluation instant. Explicitly refuse Source policy coherence modes the runtime cannot enforce instead of treating them as independent.
- Test Gap: Original tests use a policy permitting both replayability modes, so they cannot detect the bypass. Add direct and Line exact-only versus attested-only regressions, retained-status checks, freshness and default-authority cases, and non-independent coherence admission refusals before read/provider invocation.

## Complexity Assessment

The original implementation scans the accepted tree to build provider, interface, capture-contract, and policy catalogs. It then plans provider occurrences and policy decisions; this is reasonable for the slice but remains whole-world admission work. `_direct_acquisition_policy` constructs the complete policy catalog even for an exact pin, despite its stronger docstring wording. This is a performance follow-up, not an authority bypass. Journal-to-run reconstruction remains proportional to retained records and receipt bodies. The correction adds constant work per acquired result and one constant coherence-kind check per plan, with no new cache or authority store.

## Architecture Assessment

The integration mostly follows the intended service boundary: both lanes share one external-occurrence planner, runtime injects the daemon's workspace reader, and accepted artifacts determine provider implementations and capture contracts. Graph-v4 authoring uses the already-supported accepted envelope and generation-specific definition digest. The acquisition policy is an exact envelope pin, preserving definition digest semantics while changing the full artifact identity appropriately.

Read the implementation in this order: SDK/input payload policy name; lowering to an envelope pin and graph-v4 definition; served-node readiness; accepted policy/contracts/provider closure resolution; direct V5 or Line admission and plan digest; executor workspace read and provider receipt; capture construction; run-state reconstruction and CLI presentation. Direct admission binds acquisition and deployment state without inventing Line, mandate, or calibration coordinates. The emitted direct receipt remains the existing direct receipt generation; Source observations are additive run-state data.

This is **Source observation**, not the completed effectful loop. Graph-v4 workspace-file Source can read, produce its observational capture, transform/project, and expose receipts. `emit_capture`, `post_inbox`, `propose_change_set`, and `mandate_settlement` remain unserved; direct Provider nodes and provider repeat bodies remain unsupported as well. There is no new reading emitter or end-to-end procedure-to-proposal-to-reading loop here.

## Test Coverage Assessment

The existing oracle exercises actual accepted artifacts, policy pinning, daemon file containment and byte receipts, runtime execution, and retained status. Its provider subprocess boundary is stubbed, so it is not a fresh independent real-subprocess or all-transport equivalence test. The pinned provider checkout is still built and verified. Path traversal, symlink and case-folding control paths, absent/mismatched policy, unsupported terminals, crash between read and capture, and unrelated-policy acceptance are covered.

The new policy tests address the concrete omitted eligibility dimension. Their early implementation exposed two test/setup details and was corrected: a Line occurrence lookup may create an empty journal before refusal, and freshness must use the admitted evaluation instant rather than an unrelated execution wall clock. Neither successful original baseline tests nor fixture skips were treated as validation of the fix.

## Documentation Assessment

Docs correctly distinguish Source reads from effectful terminals and explain same-instant replay. They should additionally state: “Served Source runs currently support independent acquisition coherence. Bounded-window or declared-snapshot-group policies refuse before source reads/provider invocation. Each actual capture is checked against permitted replayability and max_age; denied captures follow the rule's failure behavior and cannot become selected run outputs.” Parent owns integration documentation.

The docstring claiming pinned direct-policy resolution “never consults the rest of the tree” overstates the current catalog scan; selection remains exact, but implementation does inspect other policy artifacts.

## Overall Contribution

This is a cohesive, useful Source slice with substantial behavioral coverage and correctly preserved terminal boundaries. The confirmed policy enforcement gap is corrected and the supported coherence mode is now explicit. The broader DS-3/DS-4 proposal and reading loops remain future work.

## Open Questions

None requiring a maintainer decision for the bounded correction. Non-independent coherence is explicitly unsupported rather than silently weakened.

## Suggested Follow-Ups

- Add a visible completed-replay marker and clearer fresh-observation ergonomics; unchanged evaluation instant deliberately reuses the prior observation, even if the file changed.
- Finish SDK authoring for SourceAcquisitionPolicy and CaptureContract, which currently still require other accepted-state authoring/seeding paths.
- Implement a real cross-source coherence reducer/proof path before serving non-independent Source policies.
- Complete the separately scoped proposal, terminal capture, and reading loops.
- Avoid whole-policy-catalog scans when the Procedure already carries an exact policy pin.
