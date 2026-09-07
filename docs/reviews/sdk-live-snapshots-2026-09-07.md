# SDK live accepted reads and explicit snapshots

Branch: `codex/sdk-live-snapshots`. Base: `9c0051a5ca9a5ed60e173fe901e3a94d52fbc37e`.

Implementation commits:

- `5c02383b`: Resolve unpinned Claim batches at one accepted head.
- `c502af37`: Make SDK accepted reads live with explicit snapshot contexts.

## Problem and resulting behavior

Previously, accepting a proposal could leave the calling SDK connection reading its old coordinate. Reading newly accepted Claims then failed until refresh. Moving the parent connection also invalidated existing Worlds.

Default accepted reads now resolve the current head on the server. An explicit `pb.at(coordinate)` context stays pinned, and Worlds own independent pinned contexts. Successful acceptance updates a live connection's last observation from the receipt; later live reads may observe a newer head.

```python
receipt = pb.accept(proposal_id)
pb.claim_views(ids)  # current accepted head
if receipt.accepted_coordinate is not None:
    pb.at(receipt.accepted_coordinate).claim_views(ids)  # exact receipt coordinate
```

`pb.coordinate` is the last observed coordinate, with no implicit polling. Drafts capture that last observation and retain it throughout authoring. Typed references select their explicit coordinates; mixed-coordinate inputs and inconsistent batch responses refuse. Pagination retains the initial response coordinate. Existing operational writes and current-state admission remain governed by their existing rules.

Pinned contexts borrow their parent's transport; the parent must remain open. Closing a borrowed context does not close the parent. `connect(at=...)` provides an explicit snapshot without initial orientation; reads verify its authority.

## Scope and performance

The initial Claim batch request permits omitted `at`; the service resolves it once and returns the full coordinate. Continuations require the explicit returned coordinate. Explicit-coordinate request/result behavior is preserved.

Ordinary reads do not gain a separate network head lookup. The multi-stage, procedure-projection-only `next` path obtains a coordinate through the existing small metadata endpoint instead of full orientation, then reuses it for observation and `next`.

Procedure binding retains its coordinate consistency checks before transport. The existing binding endpoint still resolves targets at current state and has no expected-coordinate wire field; this work does not eliminate that existing remote race.

## Review and validation

Independent review approved the implementation after a mixed-coordinate Procedure-binding regression was corrected and covered. A subsequent focused review approved the projection-only metadata correction.

- Named SDK, Claim batch, and real HTTP scopes: **169 passed in 42.70 seconds**.
- Follow-up snapshot, projection-observation, and SDK scopes after the metadata correction: **64 passed in 1.59 seconds**. These overlap the earlier scope.
- Ruff and diff whitespace checks passed. Mypy passed for all five changed source modules.
- The real HTTP test exercises another client accepting, the original client reading new Claims without refresh, an old World retaining its generation, and exact receipt-coordinate readback.
- Tests ran in the isolated worktree. No full suite, golden journal corpus, installed-wheel benchmark, or live daemon rollout was performed.

The served-surface catalog carries succession `2026-09-07:sdk-live-accepted-reads` for the additive initial Claim batch coordinate behavior. Its exact catalog guardrail still encounters four **pre-existing baseline** mismatches: HTTP `coverage/resolve` request, HTTP `floor/export` request and response, and MCP `export_floor` output. These were reproduced on the unchanged base in an isolated archive. Their ratified pins were preserved; this change updates only the Claim batch request digest and associated catalog metadata.

## Delivery status

Implementation and this review guide are local branch commits. They have not been merged, pushed, installed, or deployed. No live project-state update was made for this SDK change.
