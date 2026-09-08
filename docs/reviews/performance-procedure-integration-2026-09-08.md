# Code Review

## Verdict

Approved with comments.

The reviewed procedure branch and the local performance work compose after the
two conflict resolutions below. This is a manager integration self-review, not a
new independent review of every inherited implementation. All 173 distinct named
cases passed after resolving provider test-environment setup; no unresolved code
failure or design question remains. No live daemon rollout or latency improvement
is claimed by this merge.

## Manual Review Priority

- Priority: P1
- Reason: Shared authoring, proposal authorization and accepted-state ownership.
- Suggested Human Review Focus: Fresh mandate check versus evaluation reuse;
  publication-lock ordering; Line candidate isolation; new artifact payloads.

## Scope Reviewed

- Procedure input: `094bff300d4af5f968ca6150df5e78cee637f52a` (`playbill`).
- Performance input: `12a8985330e425f01819b22d33aac216b5f0eff2`.
- Common base: `ae5f7a75e8d958500cef33d54976e1bd184952da`.
- Isolated branch/worktree: `codex/performance-procedure-integration`,
  `/private/tmp/playbill-performance-procedure-integration`.
- Conflicted files: `src/cruxible_core/playbill/authoring/lowering.py` and
  `src/cruxible_core/playbill/proposals.py`.
- Integration regression: `tests/test_playbill/test_prepared_evaluation.py`.
- Documentation: this guide and the dated scheduling correction in
  `docs/dogfooding/state-write-friction.md`. The inherited AGENTS.md reminder
  makes that protocol discoverable in the merged checkout.
- Other inherited changes rely on their recorded branch reviews, supplemented
  by the focused combined checks here. Canonical untracked documents were neither
  included nor edited. No tests ran in the canonical checkout.

## Findings

No unresolved findings.

The merge required explicit composition, rather than choosing either side:

1. In `ProposalService.submit`, retain both `authorize` and `prepared`. The fresh
   actor/mandate callback runs against the current accepted snapshot before the
   prepared result can be consumed. Reuse still revalidates its exact bindings
   and mutable observations. Publication retains review-lock then activation-lock
   order, a fresh head fence before writes, candidate/evaluation/admission order,
   and the integrity check before releasing the lock. External advertisement stays
   outside. Prepared evaluation does not authorize a proposal.
2. In lowering, retain CaptureContract and SourceAcquisitionPolicy authoring,
   Line staging/order and all existing validation. Use `Mapping` inputs and
   persistent candidate forks for both staged and standalone Lines, matching the
   performance branch's mandate path. The automatically merged standalone Line
   branch still had a full `dict(base_tree)` copy; this was changed to `fork_tree`
   as part of the same integration. Lowered results continue to seal snapshots.

## Complexity Assessment

The merge preserves scoped contenders, persistent member/dependency maps,
citation owner/group deltas, same-call evaluation reuse and batched discovery.
New Line paths no longer reintroduce an explicit whole-tree dictionary copy.
Authorization and publication fencing remain fresh; they are not traded for
latency. Existing Git inventory/serialization, SQLite snapshot/export and broad
operational-evidence costs remain. Tests ran concurrently in separate disposable
instances; their durations are validation evidence, not benchmarks.

## Architecture Assessment

The ledger remains governed authority and all existing publication/recovery
boundaries are preserved. Accepted and candidate state remain isolated under the
instance's derived-state owner. No new cache, public wire field, frozen law,
digest rule, authority policy or renderer was added by the resolution. Existing
combined procedure surface and SDK pins pass without further regeneration.

## Test Coverage Assessment

All commands used the repository virtualenv with
`PYTHONPATH=.:src:packages/cruxible-client/src` from the isolated worktree.
The selected test groups were:

| Selection | Passing cases |
|---|---:|
| Prepared evaluation, publication lock, measurement SDK snapshots, lowering snapshots | 42 |
| New prepared-plus-authorization/publication-fence regression | 3 |
| Projection delta, discovery batch, Git delta, derived snapshots and contract guards | 61 |
| Procedure proposal delivery and public rung-2 HTTP loop | 29 |
| Measurement/readings Core and HTTP, pending Source admission consumers | 38 |
| **Distinct cases** | **173** |

The new regression exercises successful reuse with authorization, authorization
refusal without consuming the handoff, and head movement after reuse with no
proposal ref publication. Existing tests cover unchanged candidate output, stale
mandates, interrupted proposal/evidence publication, retries, recovery, observation
coordinates, evidence changes, readings, immutable snapshots and cold/delta parity.

Reproduction scopes:

```text
tests/test_playbill/test_prepared_evaluation.py
tests/test_playbill/test_proposal_publication_lock.py
tests/test_client/test_procedure_measurement_snapshots.py
tests/test_playbill/test_lowering_snapshot.py
tests/test_playbill/test_projection_delta.py
tests/test_playbill/test_discovery_batch_reads.py
tests/test_playbill/test_git_tree_delta_reads.py
tests/test_playbill/test_derived_snapshots.py
tests/test_guardrails/test_playbill_v1_served_surface.py
tests/test_guardrails/test_contract_freeze.py
tests/test_client/test_authoring_wire_catalog.py
tests/test_client/test_contract_snapshot.py::test_client_contract_snapshot_is_current
tests/test_client/test_contract_snapshot.py::test_authoring_program_stamp_commits_the_exact_public_contract_snapshot
tests/test_playbill/test_procedure_proposal_delivery.py
tests/test_server/test_playbill_procedure_rung2_public.py
tests/test_playbill/test_procedure_measurement_readings.py
tests/test_server/test_playbill_procedure_measurements.py
tests/test_playbill/test_pending_source_admissions.py
```

The initial run skipped 29 provider cases because the temporary checkout lacked
an adjacent provider repository. Setting `CRUXIBLE_PROVIDERS_CHECKOUT` selected
the clean real checkout at pinned `8e7436f359dd28c2afdc4b9941fd09e33fa0e470`.
Sandbox restrictions then prevented the offline provider build from accessing
the existing uv cache: 28 setup failures and one fixture error. All 29 cases
passed when rerun with build-cache access; the other 38 cases passed in the
original selection. No product code was changed to bypass seed verification.
The public HTTP test uses a test provider lane; it is not a hosted-provider pilot.

Mypy passed for all 313 Core/client source files. Ruff passed for Core/client
source and the changed test; formatting and staged whitespace checks passed.
No full suite, journal golden corpus or live-instance mutation was part of these
tests. Installed-package/deployed combined-loop validation remains separate.

## Documentation Assessment

The proposal docstring explicitly states that prepared evaluation cannot replace
fresh authorization or the head check. This guide identifies all conflict
choices and validation limits. The dogfooding protocol now prominently records
the maintainer's latest ruling: further write-path redesign is required before
OSS release; pausing it to integrate/exercise procedures is temporary. Historical
measurements and the superseded managed-track decision remain as history.

## Overall Contribution

This combines the completed performance and procedure work into one source build
without changing their product scope or governance semantics. It removes the
branch split as a prerequisite for the next customer-loop and latency work.

## Open Questions

None.

## Suggested Follow-Ups

- Roll out aligned SDK/Core builds separately, then measure the actual project
  workflow and verify ordinary live as well as pinned reads.
- Continue write-path redesign before OSS release; retain managed partition and
  fleet orchestration as managed work. Do not infer acceptable live latency from
  the earlier disposable 1,000-Claim benchmark.
- Continue recording SDK write time and friction on every meaningful state update.
