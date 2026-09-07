# Code Review

## Verdict

Approved.

No actionable correctness or authority finding was identified in the accepted-index delta changes at `612be6c2ca4953cbceb545fdda206c03b81078c2`, compared with `caa174dde80677a9bffab245a23b2bd33eb15016`. This is an independent source review of the assigned files, with focused validation recorded below. Approval is limited to this range and scope; it does not certify the separate review-note cache or installed daemon integration.

## Manual Review Priority

- Priority: P1
- Reason: This changes how accepted derived state is constructed and carried between immutable generations, so row ownership and dependency completeness need focused review.
- Suggested Human Review Focus: Verified delta provenance; conservative ownership fallback; exact explanation rebinding; missing/corrupt parent distinction; immutable publication.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/activation.py`, `assembler.py`, `projection_artifacts.py`, `projection_delta.py`, `projection_tree.py`, and `src/cruxible_core/storage/playbill_projection.py`.
- Supporting context: instance publisher construction, explanation compiler, citation relation compiler, ExhaustPromotion and Line track-record compilers, projection schema and publication/activation boundaries.
- Untracked files: none in implementation worktree at review start/end.
- Tests examined: `test_projection_delta.py`, `test_projection_tree_limits.py`, `test_assembler.py`, `test_exhaust_promotions.py`, and relevant publication/activation guards.
- Commands run: Git range diff/status/log, `rg`/`sed`/file reads; focused pytest scope listed below. No full suite, golden corpus, canonical-checkout tests, production edits, merge, or deployment performed by this reviewer.

## Findings

No findings.

## Complexity Assessment

Compilation now scales with changed payloads for supported artifact kinds, and unrelated Document changes avoid citation reconstruction entirely. The implementation still inventories parent and successor trees, copies the parent SQLite file, visits coordinate-bearing explanation rows, counts tables, and exports the logical digest. Claim/CaptureContract changes also materialize and replace the citation relation slice, even where only a subset of conflicts is recomputed. These remaining whole-world costs are accurately documented in the follow-up audit; this batch is not a fully changeset-scoped storage engine.

Fixture or presentation ownership fallback is conservative. It incurs selective inventory work before choosing full reconstruction, but that is a deliberate correctness fallback, not a hidden ordinary-path regression. Future adapters should establish explicit ownership rather than merely expand the local-kind allowlist.

## Architecture Assessment

The delta stays internal and carries the instance's already verified history plus the verified successor bundle. It introduces no second authority, public mutation API, altered historical digest rule, or altered receipt contract. The successor checks its requested coordinate against the bundle, compiler compatibility, history sequence continuity, supported member kinds, actual Git diff, and compiled member digests.

Implementation walkthrough, in dependency order:

1. The instance supplies verified recovered changesets to `ActivationPublisher`; the publisher binds them to the current base and prepared generation.
2. The assembler's existing request/commit checks and immutable publication pipeline remain in force. The new population hook selects delta construction or the original full compiler.
3. `populate_successor` permits only known locally owned artifact families. It binds the exact immutable parent and falls back for missing derivatives, legacy records, compiler changes, unsupported member kinds, fixtures, or presentation facts. Corrupt parents refuse rather than silently laundering their contents.
4. The tree reader validates modes, registration, canonical paths, collisions, file counts and declared resource bounds over the complete inventory before selecting changed payloads. Reusing the just-read inventory avoids a redundant fetch without weakening those gates.
5. The ordinary artifact compiler receives only member payloads plus internal verified history; it still performs canonical parsing and normal fact validation. Cross-member identity collisions remain refused by the SQLite uniqueness constraints.
6. The storage updater backs up the parent into a separate staged file, removes only changed artifact-owned rows, and preserves Procedure/Line track records owned by accepted ExhaustPromotions. Explicit typed explanation proof locations get the new serving coordinate; embedded historical evidence and accepting coordinates are not globally replaced.
7. Claim/CaptureContract changes run citation maintenance; unrelated supported members retain those immutable relation rows. Generation metadata, rows, schema checks, physical/logical commitments, and publication are completed before authority switches.

The implementation intentionally does not reopen unchanged CAS bodies during ordinary delta activation. It trusts their previously compiled facts from the verified immutable parent. Changed content is checked by the ordinary compiler, body reads still verify their objects, and cold reconstruction rereads its sources. This is consistent with the documented content-addressed model, but should not be described as a complete CAS corruption audit on every acceptance.

## Test Coverage Assessment

The added tests compare every SQLite table and logical digest across Claim create, revise and retire, preserve historical database bytes, reject a wrong successor, distinguish missing-parent reconstruction from corrupt-parent refusal, validate unselected inventory metadata, and prove an unrelated Document carries nonempty citation rows without invoking their compiler. The promotion case verifies retained output and original accepting coordinate against a ledger-only rebuild.

Focused test result: **26 passed in 41.82 seconds** on the reviewed head. Command: `PYTHONPATH=src:packages/cruxible-client/src /Users/robertmalone/Git/cruxible-core-0.2-stretch-goals/.venv/bin/python -m pytest tests/test_playbill/test_projection_delta.py tests/test_playbill/test_projection_tree_limits.py tests/test_playbill/test_assembler.py tests/test_playbill/test_exhaust_promotions.py -q`. Output: `/private/tmp/batch0-index-review-tests.txt`.

Limits: this review does not exhaustively exercise every supported artifact-family combination, all compiler versions, filesystem corruption races, managed HTTP latency, or distributed operation. Parent integration validation covers additional activation/handoff scopes separately.

## Documentation Assessment

Inline comments explain the non-obvious ownership and exact-coordinate decisions without duplicating simple code. The batch and overnight reports distinguish the changed-payload optimization from retained global work and explicitly document CAS reuse, fallback behavior, and benchmark conditions. No documentation correction is required for approval.

## Overall Contribution

The change is cohesive and materially reduces redundant compilation while retaining a cold reconstruction oracle. It preserves authority and immutable historical outputs, and leaves unsupported ownership cases on the established path. The central derived-state and scope-reduction work proposed next remains necessary for larger-world scalability.

## Open Questions

None.

## Suggested Follow-Ups

- When introducing a central derived-state component, represent ownership and dependency inputs explicitly instead of extending schema-name exceptions in the updater.
- Add focused parity cases for each new ownership adapter or artifact compiler as it joins the delta path, including nonempty fixture/presentation fallback worlds.
- Keep fixed-delta/unrelated-world-growth counters for inventory reads, copied bytes, explanation rows, citation rows and logical export so remaining global work stays visible.
