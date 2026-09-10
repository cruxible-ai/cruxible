# Code Review

## Verdict

Changes requested.
One reproduced concurrency regression blocks merging the mirror change. The checkpoint,
prepared handoff, and Procedure read changes have no findings from this review. The existing
large-initial-sync refusal and ref-count ceiling are accepted scope limits, not findings.

## Manual Review Priority

- Priority: P1
- Reason: Mirror publication must retain its concurrency contract while reducing arguments.
- Suggested Human Review Focus: Which unchanged refs must participate in the atomic push; checkpoint manifest binding; candidate ancestry preservation.

## Scope Reviewed

- Changed files: the five production modules and four test modules changed from `8eb5e9b15f0a2f439eb9a29d413cbd6e90d94e07` through `7f7448e6`; benchmark and implementer report inspected for claimed scope.
- Untracked files: none at start. This independent report is the only repository artifact created by the reviewer.
- Tests examined: mirror snapshots; prepared evaluation; checkpoint activation and reopen; historical Procedure reads with live authority.
- Commands run: `git diff`, surrounding source reads, and the targeted four-module pytest invocation recorded below. A scratch real-Git reproduction ran against both the new method and the base commit's method. No production files edited, no merge, no full suite or goldens, and no tests in the canonical checkout.

## Findings

### F-001: [High] Unchanged mutable refs lose their atomic lease protection

- Category: Correctness
- Location: `src/cruxible_core/playbill/git.py:735`
- Issue: Skipping every ref whose advertised value equals the desired value removes its refspec and lease from the push. If another writer changes an unchanged approval-note ref after the explicit advertisement but before the push, the push now updates main successfully without checking that note. This is a regression from the base method, which included the unchanged note and refused the entire atomic push when its lease no longer matched.
- Impact: A successful publication can acknowledge the intended snapshot while the remote contains its new accepted main paired with competing approval notes. The established competing-writer rejection and all-ref snapshot contract is weakened, despite the remaining `--atomic` flag. The flag protects only refs actually included in the command.
- Recommendation: Preserve compare-and-swap participation for mutable refs required for coherent publication, including notes, and explicitly establish which archive refs can safely be omitted. Do not describe delta selection as preserving the existing all-ref contract without proving that boundary. If exact full-inventory concurrency fencing is required, a different protocol is necessary; a post-push check alone cannot restore atomic rejection.
- Test Gap: The existing atomic race test changes the note both locally and remotely, so the note remains in the delta and the test passes. Add a race where only main changes locally, the approval-note ref initially matches, and a competitor changes that note between advertisement and push. Assert refusal and unchanged remote main. The scratch reproduction returned success and advanced main on this branch; the identical reproduction with the base method refused atomically and left main unchanged.

## Complexity Assessment

Delta refspec selection removes the growing command for ordinary archive-stable pushes, but
advertisement, retention pins, validation, and sorting remain inventory-scoped. Checkpoint
reuse eliminates repeated body hashing while full manifest/root/principal processing remains.
Prepared handoff preserves existing persistent-map ancestry but still enumerates paths to
find cards. Exact-path Procedure reads avoid full-tree copying while retaining coordinate
verification. These remaining costs are accurately bounded in the stated scope.

## Architecture Assessment

The other three changes reuse established mechanisms rather than introducing parallel
structures. The checkpoint manifest comes from the same reevaluation whose candidate and
stored tree are checked, and the only subsequent added generation record is excluded from
the semantic manifest. Prepared handoff uses the existing overlay normalization and does not
mutate the original outcome. Procedure loading remains pinned to its requested coordinate,
while current authority resolves the current accepted head independently.

## Test Coverage Assessment

The added non-mirror tests exercise useful proof and identity boundaries. Mirror growth and
existing race tests are insufficient to detect removal of leases for unchanged refs; F-001
provides the missing case. Targeted verification: `pytest tests/test_playbill/test_git_mirror_snapshots.py tests/test_playbill/test_prepared_evaluation.py tests/test_playbill/test_replay_checkpoints.py tests/test_playbill/test_procedure_run_surface.py -q` completed with **111 passed in 482.80 seconds**. `git diff --check` also passed. The passing suite does not cover F-001; the independent real-Git baseline/new reproductions establish that defect.

Minimal regression recipe, reusing the real-Git helpers in `test_git_mirror_snapshots.py`:

1. Parameterize the raced ref over the approval note and a settled archive ref.
2. Create `first`, point local main and the raced ref at it, capture the snapshot, and successfully publish it.
3. Create `later` as a child of `first` locally; advance only local main. Create a competing commit in the remote.
4. Wrap `git_module._command`: immediately before invoking the real command whose arguments contain `push`, move the remote raced ref to the competing commit.
5. Call `push_mirror` with the original snapshot as `expected_remote`.
6. Require a non-success result and remote main still pointing to `first`.

The scratch reproduction was executed for both ref classes against the new implementation
and the base method. Both new runs returned success with advanced main and divergent raced
ref; both base runs refused and left main unchanged.

## Documentation Assessment

The implementer report's claim that the change preserves the existing all-ref atomic contract
is currently incorrect because of F-001. After the fix, describe precisely which refs are
atomically fenced. Other comments and scope limitations are appropriately concise.

## Overall Contribution

Three changes are small, cohesive reuse improvements. The mirror argument reduction is
valuable, but needs one further concurrency design correction before this batch is mergeable.

## Open Questions

Which unchanged ref classes must retain atomic compare-and-swap protection? The existing
contract and tests explicitly require rejection of competing note writers; that protection
must at least survive this optimization.

Retaining unchanged active refs while omitting settled refs does not preserve the broader
inventory conflict check: the archive variant of the reproduction fails identically. The
current method does not enforce remote archive immutability. An active-only guarantee would
therefore be a contract narrowing unless separately enforced remote ownership makes archive
mutation impossible. Also, Git's own handling of up-to-date refs should not be mistaken for a
server-side transaction asserting every inventory ref: this review demonstrates a concrete
regression in the existing pre-push race window, not a proof that the old implementation
provided arbitrary-time full-inventory linearizability.

## Suggested Follow-Ups

Keep the already-approved SQLite design work and larger mirror synchronization limits out of
this corrective patch. Re-review the mirror correction against unchanged-note races before
merging the batch.
