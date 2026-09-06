# Code Review

## Verdict

Approved.

The SDK/authoring integration preserves the performance branch's bounded reads and explicit acceptance/refresh behavior alongside graph-v4 Source authoring. Prepared lowering continues to exclude every Procedure payload and every existing-Capture citation; no acquisition or mutable provenance work is reused. This approval covers the assigned integration seam, not the complete Source executor or concurrent Git/publication follow-up changes.

## Manual Review Priority

- Priority: P1
- Reason: Shared SDK and authoring contracts join independently developed behavior, including a new graph generation and operational source observations.
- Suggested Human Review Focus: Procedure policy-name-to-exact-pin lowering; prepared-cache eligibility; SDK snapshot retention across accept/refresh; Source observation transport alongside publication status.

## Scope Reviewed

- Exact integration parents: performance `c0b47fa8` and Playbill `5a2e3a7b`; resulting merge `e5604953b6a8e4624adcd953021595b27cfb5e20`.
- Changed files: `packages/cruxible-client/src/cruxible_client/authoring/sdk.py`; client `contracts/__init__.py`, `contracts/acquisition_policies.py`, `contracts/authoring/inputs.py`, `contracts/authoring/models.py`, `contracts/authoring/wire_catalog.py`, `contracts/procedures/line_specs.py`, `contracts/procedures/results.py`; `src/cruxible_core/playbill/authoring/lowering.py`; `src/cruxible_core/runtime/playbill_api.py`; `src/cruxible_core/cli/commands/playbill.py`. Surrounding performance implementation reviewed in `authoring/preflight.py`, `authoring/prepared_lowering.py`, and the SDK accept/refresh/batch-read paths.
- Untracked files: none within the assigned source scope. This report is outside the repository. The surface-inventory conflict was owned and resolved by the manager. Subsequent uncommitted `git.py` and `instance.py` changes were observed and excluded.
- Tests examined: `tests/test_playbill/test_prepared_lowering.py`, `test_authoring_inputs.py`, `test_authoring_procedures.py`, selected `test_procedure_source_runs.py` authoring/SDK/end-to-end cases; `tests/test_client/test_playbill_accept_refresh.py` and `test_playbill_world_prefetch.py`.
- Commands run: read-only `git status`, diffs against both parents, surrounding source/test reads; scoped pytest command below; Ruff and mypy on eight SDK/authoring/runtime source files; ten executable cache-eligibility combinations described below.

All checks ran from `/private/tmp/playbill-state-loop-design`, using the canonical `.venv/bin/python` with worktree `PYTHONPATH=src:packages/cruxible-client/src`. No full suite, golden corpus, canonical-checkout tests, live instance changes, or network publication ran.

Named pytest scope:

```text
tests/test_playbill/test_prepared_lowering.py
tests/test_client/test_playbill_accept_refresh.py
tests/test_client/test_playbill_world_prefetch.py
tests/test_playbill/test_authoring_inputs.py
tests/test_playbill/test_authoring_procedures.py
tests/test_playbill/test_procedure_source_runs.py::test_the_sdk_authors_a_source_node_on_v4_and_refuses_it_on_v3
tests/test_playbill/test_procedure_source_runs.py::test_the_sdk_carries_the_named_policy_into_the_authoring_payload
tests/test_playbill/test_procedure_source_runs.py::test_the_authoring_path_lowers_the_named_policy_into_the_envelope_pin
tests/test_playbill/test_procedure_source_runs.py::test_the_authoring_path_produces_the_exact_artifact_the_run_lane_executes
```

## Findings

No findings.

## Complexity Assessment

The merge does not widen the cache. Its explicit allowlist admits self-source Claims and selected deterministic definitions only; Procedure V1/V2 are excluded regardless of graph generation or policy field. Any ChangeSet containing an excluded member also misses. Existing-Capture Claims remain excluded because they are not self-source Claims. Exclusion happens before cache-key serialization or cache access, and ordinary lowering reruns.

Eligible cache hits retain the prior bounds of four entries and 32 MiB of accounted serialized/tree bytes per instance; this accounting is not a Python-heap limit. Key construction still serializes current nested input values, accepted coordinates, descriptor, actor, and limits. Body integrity checks and fresh nested-container copies remain on hits; candidate evaluation and reference/authority checks remain outside cached lowering. Procedure authoring still scans/parses its reference tree, which is a deliberate performance limitation rather than a weakened freshness boundary.

SDK Claim batches remain explicitly bounded to 256 inputs and validate coordinate, count, and returned identity order. Source receipt prose is proportional to the observations returned by the run; it introduces no history scan.

## Architecture Assessment

Suggested implementation walkthrough:

1. Read `ProcedureAuthoringPayloadV2` and `ProcedureInput`: the additive acquisition-policy field carries a semantic name, with canonical-name validation in the lowered payload. Input conversion selects V2 when either carried contracts or the named policy require it. Existing V1 input behavior remains available.
2. Read `Playbill.procedure()`: graph-v4 permits the Source node, graph-v3 retains its prior node allowlist, and unsupported terminals remain refused. The policy name is carried into payload, program identity, and source-location metadata. This hunk does not overlap `claim_views()`, `accept()`, or `refresh_workspace()`; their coordinate checks and explicit snapshot behavior remain intact.
3. Read `_lower_procedure()` and `_acquisition_policy_pin()`: model parsing dispatches by graph generation, definition digest dispatches through the corresponding frozen digest implementation, and the policy resolves through existing accepted/candidate reference machinery. Its exact pin is on the Procedure envelope, leaving definition bytes independent of policy adoption. Closure evaluation remains responsible for the declared pin.
4. Read `_eligible()` then the `reuse_lowering()` call in preflight: Procedure payloads cannot enter the optimization. New graph-v4 authoring therefore reaches the new lowering logic on every request; prepared self-source Claim lowering retains its independent fresh evaluation path.
5. Read the public run-state contract, `_echo_source_observations()`, and `playbill_line_run()`: Source observations survive contract serialization, CLI procedure/line output uses that shared field, and Line execution receives the same daemon-owned workspace reader capability as direct execution. Permission and authenticated-actor checks remain before service execution. Ledger publication receipt additions coexist in separate contract/runtime/CLI sections.
6. Read the input/wire contract pins: the merged authoring catalog changes from the Playbill parent remain present, while performance additions to floor-coordinate and mirror-status receipts remain present. The manager owns final served-surface inventory verification.

The integration retains the existing contract → SDK/runtime → core-service layering and does not duplicate execution orchestration in surface handlers.

## Test Coverage Assessment

The first named run completed with **60 passed, 4 skipped in 114.87 seconds**. All four skipped tests needed the separately pinned provider checkout, which is not discoverable relative to this temporary worktree. Supplying `CRUXIBLE_PROVIDERS_CHECKOUT=/Users/robertmalone/Git/cruxible-providers` located commit `8e7436f359dd28c2afdc4b9941fd09e33fa0e470`; the first explicit rerun failed during fixture initialization because sandbox access to uv's existing offline cache was denied. Retrying those same four tests with approved cache access completed with **4 passed in 19.54 seconds**. Thus all **64 selected tests passed** across the completed scopes; the initial skips and environmental failures do not represent exercised product failures.

Ruff passed and mypy reported no issues in eight sources: SDK, authoring input/model contracts, procedure result contracts, lowering, preflight, prepared lowering, and runtime API.

Additional executable checks constructed parsed Procedure V1/V2 payloads for graph-v3/v4, with V2 acquisition-policy names, both standalone and inside single-member ChangeSets. All eight combinations called the supplied compute function independently on both invocations without touching an instance or cache input. Two further parsed combinations—existing-Capture Claim V3 alone and in a ChangeSet—were ineligible. These checks supplement existing tests for cache invalidation, missing/corrupt bodies, changed coordinates, fresh authority/evaluation, and prose-only changes.

The complete Source executor suite, CLI/guardrail inventory tests, and mirror regressions were deliberately left to the other assigned integration reviewers/manager.

## Documentation Assessment

The new public Procedure API explains semantic policy naming and exact accepted-envelope binding. Lowering comments explain graph-generation dispatch and why the policy changes the envelope rather than the definition digest. Source observations explicitly describe their receipt/capture boundary and do not claim completed Claim lineage. The cache module states its limited scope and byte-accounting distinction. Existing accept/refresh documentation continues to explain retained read snapshots and explicit maintenance. No documentation blocker was found in the reviewed integration seam.

## Overall Contribution

The merge preserves both branches' behavior with a narrow, understandable interaction: new effectful authoring stays outside the deterministic lowering cache, while pure Claim workflows retain their performance improvements. Contract and SDK additions are additive within this development line, and the selected authoring-to-run case proves the merged graph-v4 output is accepted and executable.

## Open Questions

None.

## Suggested Follow-Ups

Preserve the explicit Procedure/observed-Capture cache-exclusion cases as permanent focused regressions when this optimization is next extended. Any proposal to cache Procedure lowering should first enumerate its mutable reference/provenance dependencies rather than infer safety from the graph's deterministic execution model.
