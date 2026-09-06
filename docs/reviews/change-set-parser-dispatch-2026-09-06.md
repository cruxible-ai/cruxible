# Code Review

## Verdict

Approved

No findings in the scoped ChangeSet parser change. Valid explicit tags select the same uniquely tagged frozen model as the original union, while failure falls back to the original union before exposing a refusal. Canonical rendering and every version's existing integrity validators remain unchanged; only validation adapters are retained, not parsed models.

## Manual Review Priority

- Priority: P1
- Reason: This shared decoder reads permanently verifiable historical ChangeSets.
- Suggested Human Review Focus: Disjoint literal tags; fallback to the historical union; canonical-byte comparison; fresh mutable outputs.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/settlement.py`, uncommitted against `089100609fddb60755bf3494fb0f268e4661a386` in `/private/tmp/playbill-warm-write-latency`.
- Untracked files: `tests/test_playbill/test_change_set_parser_dispatch.py`.
- Tests examined: Complete new parser-dispatch tests and existing settlement round-trip usage. The unrelated modified served benchmark was excluded.
- Commands run: `git status --short`, scoped `git diff`, `git rev-parse HEAD`, `git diff --check`, `rg`, and source/test inspection. No source edits, tests, or benchmarks were run by the reviewer; the implementer reported 51 named tests and scoped mypy/Ruff passing, and concurrent benchmarking was left undisturbed.

## Findings

No findings.

## Complexity Assessment

Both adapters are compiled once rather than constructing the ordinary union adapter for each record. A valid tagged record validates only its selected version. Invalid or missing-tag input pays for a failed tagged attempt and the original union validation, retaining the same asymptotic bound and historical refusal behavior. Each successful call still fully parses its input and returns a fresh model graph; retained memory is fixed schema/adapter state, not a cache proportional to records or history.

## Architecture Assessment

The change is at the existing single accepted-record decoder. First inspect the three record classes: their distinct Literal tags make valid explicit dispatch unambiguous, and their nested candidate, correspondence, and self-digest checks are unchanged. Next inspect the two module adapters: one preserves the old union, the other adds only a discriminator. Finally inspect the parser: failures of the fast adapter are retried through the historical adapter, the same SettlementIntegrityError wraps its error, and exact rendered bytes must still equal the supplied content. A missing tag can still receive the old model-default treatment before canonical comparison rejects omitted bytes; unknown, wrong-type, and conflicting tags retain old refusal details. No migration, digest rule, public schema, or publisher behavior changes.

## Test Coverage Assessment

The tests use an independent copy of the former parser as the outcome/refusal oracle for all three record versions. They cover valid dispatch with the slow adapter forbidden; missing, unknown, null, wrong-type and other-version tags; self/candidate digest corruption; extra fields; pretty JSON, missing newline and duplicate tags; malformed JSON, non-object input and invalid UTF-8. Refusal comparisons include the outer exception and structured validation cause. A nested law-digest dictionary mutation proves separate calls and subsequent parses do not share mutable output. This is proportionate coverage for the narrow change.

## Documentation Assessment

The existing parser docstring continues to describe the frozen-history contract accurately. The fallback comment explains why apparently redundant retry logic is required and correctly states that parsed models are not retained. No additional public documentation is necessary.

## Overall Contribution

A small, cohesive optimization that avoids speculative cross-version work for normal accepted records while retaining the original refusal and canonical-byte boundaries. It does not rely on prior validation or immutable-looking mutable model objects.

## Open Questions

None.

## Suggested Follow-Ups

None.

## Final verification

Parent/implementer validation in the isolated worktree: `test_change_set_parser_dispatch.py` and `test_settlement.py` passed **51 tests in 7.16 s**. Scoped settlement Mypy and source/test Ruff check and format checks passed. Source and tests committed together as `946d2d96`.
