# Code Review

## Verdict

Approved

No findings in the scoped SDK, tests, or benchmark changes. `world()` now uses one current vocabulary response to select both its contents and coordinate, while preserving connection advancement and stale-World refusal. The removed orientation call has no additional required SDK or persistence side effects; the benchmark distinguishes initial acquisition from repeated, coordinate-advancing writes.

## Manual Review Priority

- Priority: P1
- Reason: The change removes a read from a shared typed authoring entry point and changes the benchmark's workload shape.
- Suggested Human Review Focus: Server-side listing coordinate selection; connection advancement and old-reference refusal; lazy Subject costs; profiled versus unprofiled timing boundaries.

## Scope Reviewed

- Changed files: `packages/cruxible-client/src/cruxible_client/authoring/sdk.py`, `tests/test_client/test_playbill_sdk_world.py`, and `docs/benchmarks/write-loop-served.py`, uncommitted against `089100609fddb60755bf3494fb0f268e4661a386` in `/private/tmp/playbill-warm-write-latency`.
- Untracked files: None in this review scope. The new ChangeSet parser test and settlement changes belong to the separate parser review.
- Tests examined: Updated current-listing and moved-coordinate cases, surrounding lazy-World and read-only connection tests, and fake client behavior.
- Commands run: Scoped `git diff`, `git status --short`, `git diff --check`, `rg`, and source inspection across SDK, HTTP transport, route, runtime, ClaimType service, search service, manager and World consumers. No tests or benchmarks run by the reviewer because the parent owns concurrent verification and timings; no files in the checkout were edited.

## Findings

No findings.

## Complexity Assessment

World construction still costs the vocabulary listing and local vocabulary parsing, but avoids a discarded orientation page that derives Claim/Procedure discovery state. The first Subject lookup still loads the complete Subject list for that World, and new World snapshots do not reuse the previous snapshot's Subject cache. The harness includes that lazy work in each draft phase rather than hiding it as setup. No new retained state or cache growth is introduced.

## Architecture Assessment

First inspect `Playbill.refresh`: it performs a current orient search and only installs the returned coordinate. `_search` creates a result object without retaining orientation state, changing workspace sources, or installing another SDK cache. The runtime records consumed paths only for search mode, not orient, and the service describes and implements a read without durable daemon writes. Removing this discarded orient result loses no required refresh side effect; it does change incidental cache warming, which end-to-end timings should capture.

Next inspect `service_list_playbill_claim_types`: `at=None` resolves the current accepted coordinate once, reads the exact accepted tree at that OID, and emits both list and individual views with the captured coordinate. The SDK now installs that response coordinate and builds the World from the same response's envelopes. This removes the old two-request coordinate-selection window while keeping one coherent snapshot. The World continues to pin subsequent Subject reads, and `_assert_current` refuses an older World when a newly constructed World advances the connection. Explicit `refresh()` remains available and unchanged.

Finally inspect the harness: `--world` selects typed Subject/ClaimType references, retains the SDK and daemon processes across loops, and acquires a fresh World after each accepted generation. The assertion binds the new World to the activation receipt. String-reference mode retains its original explicit refresh. This is consistent with World's snapshot ownership rather than mutating an old snapshot into a new generation.

## Test Coverage Assessment

The updated tests require no orient search during World construction, exactly one unpinned vocabulary listing, and no eager Subject listing. The moved-coordinate test proves a connection uninformed of another acceptance picks the new current listing, advances its coordinate, and refuses its previous World. Existing lazy Subject and typed reference tests exercise consumers. This is sufficient targeted coverage for the small SDK change; parent execution results should accompany integration.

The harness's initial World acquisition is separately reported, outside per-write totals. Each loop includes draft, governed write, approval, acceptance, pinned readback and refresh/new World. The first loop follows connect/orient and optional initial World acquisition; it is not a fully cold operation or an OS-cache-controlled measurement. Warm loops advance accepted state rather than repeat identical reads. Readback values, counts, grades and coordinates are checked; the fixture explicitly reports uncovered claims rather than presenting it as a supported-evidence proof. Readback profiling is enabled only with the broader write-phase profiling option, and comparison runs must use matching instrumentation and `world` settings.

## Documentation Assessment

The SDK docstring accurately describes selecting the coordinate through the vocabulary listing and retains the lazy Subject cost warning. The harness documents typed snapshots, persistent processes, profiling overhead, advancing coordinates, and the fixture's evidence limitation. The report stores `world` and separate initial acquisition timing, so phase comparisons are interpretable.

## Overall Contribution

A focused removal of expensive unused discovery work while preserving typed snapshot semantics. The updated workload measures actual typed SDK usage across multiple accepted generations and exposes both acquisition and repeated-operation costs.

## Open Questions

None.

## Suggested Follow-Ups

None.

## Final verification

Final source was tested in the isolated worktree with `tests/test_client/test_playbill_sdk_world.py`, `tests/test_client/test_playbill_world_prefetch.py`, `tests/test_server/test_playbill_sdk_world_generation.py`, and `tests/test_server/test_playbill_sdk_demo_world.py`: **46 passed in 90.88 s**. Scoped SDK Mypy, Ruff, and format checks passed. Source and tests committed together as `c42feb24`. No full suite or golden corpus was run.
