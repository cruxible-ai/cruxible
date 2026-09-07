# Code Review

## Verdict

Approved with comments.

Both independently reviewed procedure branches compose with the current Playbill
performance and SDK changes after the integration fixes below. Verification here
is limited to static checks and isolated unit tests: the maintainer explicitly
deferred product loops and benchmarks while contracts and projections are being
redesigned. This is an integration review, not a fresh claim of end-to-end parity.

## Manual Review Priority

- Priority: P1
- Reason: Shared proposal publication ordering and SDK snapshot semantics.
- Suggested Human Review Focus: Publication lock scope; admission-backed evidence
  consumers; measurement observation coordinates; combined public surface pins.

## Scope Reviewed

- Integration base: `ae5f7a75e8d958500cef33d54976e1bd184952da` on `playbill`.
- Rung-2 reviewed tip: `4a0b374031d3719e44ae304c79e95533d23146c8`.
- Readings reviewed tip: `5e17831615368055d9a2f5c3c26229bdc1c40d12`.
- Both source branches were based on `caa174dde80677a9bffab245a23b2bd33eb15016`.
- Integration branch: `codex/procedure-integration`; source worktrees untouched.
- Changed logic examined: proposal publication and note-index composition;
  `Procedure.measure`, `Procedure.readings`, `ProcedureRun.measure`; pending Source
  and curation retirement evidence consumers. Other branch behavior relies on the
  completed independent reviews of the exact tips above.
- New tests: `test_procedure_measurement_snapshots.py`,
  `test_pending_source_admissions.py`, `test_proposal_publication_lock.py`.
- Existing untracked canonical documents are outside this integration.

Verification commands run from the isolated integration checkout, using the
canonical virtualenv with `PYTHONPATH=.:src:packages/cruxible-client/src`:

```text
python -m pytest -q tests/test_guardrails/test_playbill_v1_served_surface.py tests/test_guardrails/test_contract_freeze.py tests/test_architecture/test_playbill_clock_taxonomy.py tests/test_client/test_authoring_wire_catalog.py tests/test_client/test_claim_attestation_contract_catalog.py tests/test_client/test_contract_snapshot.py::test_client_contract_snapshot_is_current tests/test_client/test_contract_snapshot.py::test_authoring_program_stamp_commits_the_exact_public_contract_snapshot tests/test_playbill/test_pending_source_admissions.py
python -m mypy src packages/cruxible-client/src
ruff check src packages/cruxible-client/src tests/test_client/test_procedure_measurement_snapshots.py tests/test_playbill/test_pending_source_admissions.py
ruff format --check <Python files changed since integration base>
git diff --check
```

The first selection passed 43 tests. Full source type checking passed for 307
files; Ruff and formatting passed. SDK isolated verification passed 28 tests,
including 16 new measurement cases and existing snapshot/compatibility cases.
Publication ordering verification passed 2 isolated tests in `2f581eb7`.
Together these selections passed 73 tests. The SDK fix is `4f0c3f42`; the
admission-consumer fix is `5871f90b`.

The authoring-wire, client-contract, HTTP-surface, and served-surface generators
were run against the combined tree. Only the SDK contract digest and the served
surface needed further adjustment after the merges; other generated outputs
already matched. The combined served-surface succession is
`2026-09-07:procedure-integration`, authorized by the maintainer's integration
instruction. Surface generation used an isolated temporary state root.

## Findings

No unresolved findings in the integration scope. Three issues were fixed:

1. New measurement methods bypassed the current SDK's live/pinned read helpers.
   They now resolve explicit coordinates, reject conflicting pins before I/O,
   validate returned observation coordinates, and remember successful reads.
   Run measurement delegates to the same path; observation stays distinct from
   the run's admission coordinate. Cursor continuation preserves server selection.
2. Admission-last publication can leave an evaluation before admission commits.
   Pending Source status and curation retirement attribution now require an
   admitted proposal ID, excluding incomplete publication evidence.
3. The proposal door checked accepted main after releasing the activation lock.
   A legitimate intervening activation could make a durable submission report an
   integrity failure. The check now occurs under the lock; mirror publication and
   workspace advertisement remain outside it.

## Complexity Assessment

The merge retains current-tree reuse, incremental `_note_index()` calls, and
candidate-alias completion updates. SDK adaptation adds no head lookup. The two
operational evidence consumers add a linear admission scan and ID set alongside
their existing evaluation scans; they are not submit/accept hot-path changes.
The publication check moves without adding I/O. No performance improvement or
regression estimate is claimed without the deferred benchmarks.

## Architecture Assessment

Publication continues to acquire the review lock before the activation lock;
evaluation stays outside. Evidence ordering remains candidate, evaluation,
admission, followed by notes, with admission as the group commit point. The note
index includes complete admission-backed groups and preserves aliases. SDK
methods reuse the existing snapshot helpers and shared transport. No derived
index becomes authoritative, and no live instance or deployment was changed.

## Test Coverage Assessment

Mocked tests cover live head changes without extra lookups, all explicit pin
forms, conflicting pins, response-coordinate mismatches, cursor continuation,
run-admission versus observation coordinates, orphan evaluations, and lock exit
ordering. Existing branch end-to-end evidence remains attached to the reviewed
tips; the combined product loops, crash/restart loops, full suite, journal corpus,
and performance harnesses are deliberately not rerun in this integration.

## Documentation Assessment

Both branches' changelog entries are retained. Public method descriptions and
inline comments explain observation semantics and the publication invariants.
Generated public inventories describe the combined surface. This guide records
the conflict decisions and limits of validation.

## Overall Contribution

The integration lands the reviewed Line-to-proposal and measurement/readings
capabilities while preserving the intervening SDK and performance work. Follow-up
changes are limited to compatibility and publication correctness; they do not
expand the ongoing contracts/projections redesign.

## Open Questions

None blocking the authorized merge.

## Suggested Follow-Ups

After the redesign stabilizes, run the combined source/capture/proposal and
measurement/reading loops, crash recovery checks, and cold/warm benchmarks.
Retain the existing follow-ups about repeated Line tree reads, run-index rebuilds,
and proposal/acceptance cost; this integration does not resolve those costs.
