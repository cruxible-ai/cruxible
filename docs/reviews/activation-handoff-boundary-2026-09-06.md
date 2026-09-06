# Verified activation handoff: recovery boundary audit

Read-only audit of `f36620bf1a33277d02140fdad96d89f1be89e637`. Scope: recovery invariants compared with generation preparation and successful publication. No implementation inspection in the new worktree, edits, tests or benchmarks.

## Judgment

A routine successful local activation can install a successor `RecoveredInstanceState` without replaying its verified prefix. This is safe only as a private handoff from the actual prepare/prebuild/activate invocation, bound to the exact still-current predecessor and successfully published successor. `VerifiedGenerationBundle` being a frozen dataclass is not by itself that proof. Lost CAS, incomplete publication, inconsistent metadata, or an intervening main update must use recovery rather than install the bundle speculatively.

## Already established on the clean local path

`settlement.py:703 prepare_generation` rereads the current base tree, reproduces candidate laws/closure/manifest/diff and derivative cards, verifies approvals and actor binding, constructs the frozen receipt, writes a signed generation with the expected parent, rereads its exact stored tree, and derives semantic/generation roots. `ActivationPublisher.prebuild` builds and verifies the complete projection. `activation.py:151 _activate_locked` binds the projection to the bundle, CASes main, writes the generation note, publishes serving state, publishes the optional witness, and performs its configured checkpoint work before returning `accepted`.

Therefore the normal handoff does not need to repeat law evaluation, approval verification, full tree derivation, or prefix replay merely to create its in-memory history entry. The ancestor history was already verified when the instance epoch was installed.

## Required additional guards

| Invariant | Evidence / handoff requirement |
|---|---|
| Exact predecessor | Capture the instance's `_recovered` epoch before preparation. Require its head/coordinate to equal the supplied base, including repository, object format, compiler and both roots. Require sequence `parent.sequence + 1` and descriptor parent root equal the captured parent generation root. |
| No regression under another writer | The publisher releases its activation lock before returning. Reacquire the same lock for final main/epoch checks and atomic state installation, or install through a callback still inside that lock. If main has advanced or the instance epoch changed, recover current state; never replace a newer in-memory epoch with this older receipt. The returned receipt must still name this invocation's accepted generation. |
| Parent-key authentication | Recovery `_verify_successor:390` checks the new commit against the active daemon key in the parent principal snapshot. Preparation/prebuild use `GitLedger.verify_commit`, which consults the mutable allowed-signers file. Preserve exact parent-root-key verification or establish an equivalent binding; configured signer-name equality alone is weaker. |
| Append-only frozen history | Recovery compares all predecessor ChangeSet bytes and requires exactly one new canonical receipt path. Preparation directly checks collision only for the new path; projection verifies contiguity but does not compare predecessor bytes. Check the bundle's receipt namespace against the already verified predecessor records rendered with their original frozen format, plus exactly the new canonical receipt. Do not recompute historical digests under a current format. |
| Receipt-to-candidate round-trip | Parse the exact new receipt bytes with `parse_change_set_record`, reconstruct its candidate with the frozen version dispatch in `recovery._candidate_from_record`, and compare that result with the exact candidate verified by preparation. In particular, the v1 ChangeSet after-validator does not itself construct a CandidateRecord. This retains the extra candidate-shape checks that replay performs and produces a detached record for history without another law evaluation. |
| Timestamp semantics | Replay evaluates at `record.candidate.timestamp`; successor replay does not independently compare Git author/committer timestamps. Preserve that exact canonical timestamp in the detached receipt and candidate equality check. Do not replace it with wall time or use Git second-resolution timestamps as a substitute for microsecond canonical authoring time. |
| Successor principals | **`bundle.principals` is the parent registry**, computed from `base_tree` to verify approvals. Recovery `_verify_successor:514` rebuilds the successor registry from the new tree at the new semantic root. Handoff must do the latter. Otherwise registration, revocation and rotation appear stale until reopen. |
| Private immutable ownership | Bundle tree, record and nested evidence are mutable despite frozen wrappers. Use the exact private invocation output; retain a detached/deep copy of the new record and fresh successor principals, and do not retain the whole generation tree in history. Validate the new receipt bytes correspond to the bundle's tree before copying. Existing historical entries may be reused as one captured verified prefix; do not introduce new outward aliases to the bundle. |
| Publication completed | Only `accepted` returned after the entire publisher path qualifies. Check returned coordinate/projection against the bundle and bind current serving state to that exact coordinate before installation. A missing/mismatched note or serving pointer, a post-CAS exception, or incomplete witness publication must not be treated as a completed handoff. Retain repair/recovery behavior for these cases. |
| Removed prerelease content | `recovery.py:173 _refuse_removed_prerelease_content` is the only `knowledge.brief` incompatibility gate found. Preserve it for newly introduced content: with a verified compatible parent, inspect candidate/new content equivalently or conservatively fall back to recovery on a possible match. Do not accidentally permit a state that routine refresh previously refused. |
| Epoch-scoped derivatives | Preserve `refresh()`'s clearing of `_tree_memo`, `claim_read_history_memo`, and `_history_lookup`, plus service-level `reset_claim_resolution_memo()`. Build the entire new history/head/coordinate/projection value before replacing `_recovered`; readers should observe an old or new complete epoch, not partially patched fields. |

## Cleanup, archival and publication ordering

Recovery additionally runs `_clean_unaccepted_generations:631`, `_clean_unaccepted_publications:587`, and `_clean_torn_projection_files:705`, repairs missing generation notes/serving state, and optionally checks/repairs witness history. Successful local activation proves this invocation's own publication finished; it does **not** prove unrelated old crash residue has been collected. Keep that cleanup on open/explicit recovery and all uncertainty/lost-CAS paths. If omitting its routine sweep on clean acceptance is intentional, document that maintenance timing change; do not claim full recovery equivalence for unrelated residue.

Review-branch archival is **not done by `refresh()` or `recover_instance()`**. `instance.py:1019 advertise_workspace` invokes `_reconcile_proposal_review_refs`, and the mirror worker also reconciles before publishing. The reconciler captures the newly installed history, uses accepted candidate digests plus withdrawals/staleness to select archives, and `GitLedger.replace_proposal_review_refs:511` atomically archives departing branches. Keep advertisement and mirror enqueue after the new epoch is installed. With neither attachment nor mirror, review projection already remains lazy; the handoff should not invent an expensive additional reconciliation.

A private instance method owning prepare → detached successor derivation → prebuild → activate-with-callback is a stronger boundary than accepting an externally supplied bundle. A per-instance RLock shared with refresh prevents an older recovery result overwriting an installed successor. Keep the ordering instance RLock → activation lock; a callback may reenter its already-held RLock. Release both before advertisement/review work.

Lock ordering matters: current review reconciliation holds `review_projection_lock` and may call refresh if its handle is stale. That becomes review lock → instance RLock after adding the refresh lock, so never hold the instance RLock while acquiring the review lock either. Do not hold activation lock while acquiring review lock or performing advertisement callbacks; do not recursively acquire the file-based activation lock (separate opens can self-deadlock). Keep network mirror work asynchronous and outside both locks. An advertisement may itself observe/trigger a later acceptance: receipt coordinate remains the original publisher result, as the existing regression requires.

## Named validation for the implementation

Add focused handoff tests proving: clean local success never invokes recovery and immediately serves the new coordinate; next write succeeds on that same instance; successor registration/revocation/rotation matches a reopened instance; mutating the original bundle afterward cannot mutate accepted history; stale base/changed epoch/intervening main takes recovery and never regresses head; malformed append-only receipt history and wrong sequence are rejected/fallback; exact parent-key verification cannot be bypassed with a substituted allowed-signers entry; a possible removed-prerelease artifact retains refusal; post-CAS note/serving/witness failure is repaired only through recovery; lost-CAS still collects its exact loser; warm read memos cannot return old state.

Reuse these named existing checks as relevant, without a full suite or golden corpus:

- `test_activation.py::test_prebuild_is_unserved_until_winning_cas_then_switches_atomically`
- `test_activation.py::test_two_candidates_from_one_base_leave_one_winner_and_no_loser_projection`
- `test_activation.py::test_qualified_git_formats_preserve_candidate_changeset_and_semantic_root`
- `test_recovery.py::test_restart_recovers_every_activation_boundary`
- `test_recovery.py::test_restart_finishes_losing_cas_orphan_cleanup`
- `test_recovery.py::test_replay_retains_no_generation_trees_and_serves_history_from_the_ledger`
- `test_principal_history.py::test_owner_rotation_and_recovery_replacement_replay_exact_key_roots`
- `test_principal_history.py::test_owner_registration_and_revocation_make_old_reviewer_key_inactive`
- `test_activation_receipt_coordinate.py::test_receipt_keeps_own_generation_when_advertisement_observes_later_acceptance`
- `test_review_publication_concurrency.py::test_stale_handle_reconciliation_cannot_resurrect_a_settled_proposal`
- `test_review_archive_rebuild.py::test_late_mirror_binding_rebuilds_never_open_settlement_and_notes`
- `test_ledger_publication_worker.py::test_older_main_snapshot_is_not_reported_as_the_newly_accepted_main`

Also include targeted mixed-version successor coverage from `test_wire_succession_boundary.py`; the new in-memory history must keep the same v1/v2/v3 record classes and roots that a reopen derives.
