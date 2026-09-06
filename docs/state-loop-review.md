# Code Review

## Verdict

Approved with comments.

The reviewed lineage index, block-sync integration and SDK backing batching
preserve the authority boundary and the existing terminal-lineage behavior.
Two issues identified during review were corrected before this verdict:
per-backing history-prefix retention/comparison, and historical batch-read errors
preempting an earlier marker refusal. The remaining comments concern scaling
limits; they do not block this improvement.

## Manual Review Priority

- Priority: P1
- Reason: Shared derived state changes how historical Claim currency is read.
- Suggested Human Review Focus: Verified prefix-tip binding; fault-only per-path
  fallback and deferred errors; snapshot coordinates and private returned models;
  exact backing order and completeness checks.

## Scope Reviewed

- Changed files: `service/playbill_projection_sync.py` under Core and
  `authoring/blocks.py` under the client SDK.
- Untracked files: `service/playbill_projection_lineage.py`, its lineage tests,
  and `tests/test_client/test_projection_backing_batch.py`.
- Supporting code examined: accepted-coordinate/history and blob-read methods,
  Claim parsing and marker contracts, the new backing-read service and contracts.
- Tests examined: the new lineage/backing tests and existing
  `test_block_sync_service.py` and `test_playbill_block_sync.py`.
- Commands run: scoped pytest from `/private/tmp/playbill-state-loop-design`,
  using the canonical environment's Python and worktree source/SDK paths;
  a fault-injection probe replacing a malformed later historical blob with an
  unreadable later blob. The original four-file run passed 36 tests; the final
  lineage/backing subset passed 9 tests after the fault-handling correction.

This is an independent review of the above slice. It does not independently
review this agent's prepared-lowering implementation, World batching, activation
changes, or compact-manifest work. No production state or source implementation
was changed by the reviewer.

## Findings

No findings.

## Complexity Assessment

Cold lineage construction batches requested paths per generation and parses only
distinct consecutive artifact bytes. The cache has explicit path, node and
retained-source-byte bounds; oversize results remain usable without retention.
A cached path stores a prefix length and verified tip, whose generation root
commits the preceding chain, rather than a complete retained history tuple.
Fully warm reads skip historical blob traversal; extensions inspect only new
generations. Returned node/model copies cost space proportional to the selected
lineages and cannot mutate retained entries.

The improvement does not make the entire sync request independent of historical
generation count. The instance's coordinate lookup still scans accepted history.
Warm origin reads call that helper per backing, retaining an O(backings times
generations) in-memory membership-check term. Cold construction additionally
invokes it per generation. These are existing helper costs, not repeated
historical Claim parsing. Origin artifact parsing and terminal traversal also
remain proportional to the selected backings and their actual revisions.

SDK metadata requests are bounded to 256 identities per request. No Claim
admission evaluation is requested for stamping, and responses must preserve the
exact coordinate and requested identity order. The fault-only read fallback can
perform one read per requested path; this extra work preserves refusal behavior
when a shared read fails and is absent from the successful hot path.

## Architecture Assessment

The index is process-local, weakly owned by its instance, and derives solely from
replay-verified accepted history and its exact Claim artifacts. It adds no
authoritative facts, changes no stored digests, and can be discarded and rebuilt.
The fixed history prefix prevents a concurrent acceptance from injecting a later
successor into the current answer. Operational source observations and evidence
verdicts are not cached as lineage facts.

The sync service retains the existing origin validation and terminal selection.
It consumes the batch through a scoped context that is reset in `finally`.
Historical path failures are raised when the original backing order reaches that
path, preserving earlier refusals. SDK fallback is restricted to structural
clients that lack the batch method; server failures are not silently retried
through a different semantic read.

## Test Coverage Assessment

Coverage exercises cold batching, warm reuse, incremental acceptance,
historical rollback, eviction/rebuild, snapshot pinning during concurrent
acceptance, and avoiding partial index publication on parse failure. The added
parse-versus-read fault regression verifies both the earlier-marker refusal and
the later failure once the earlier backing becomes valid.

Existing service/client tests cover moved statements, body-only successors,
multiple held Claims, retirement, ambiguity, dirty local prose, and retaining
authored bytes. New SDK tests check request chunking, omission/order refusal and
mixed-coordinate refusal. This is sufficient for the reviewed behavior.

## Documentation Assessment

Module comments explain authority, rebuildability, retention limits and snapshot
pinning. The verified-tip comment explains why the prefix optimization is sound,
and the fallback comment explains the otherwise unusual error attribution.
Performance reports should distinguish avoiding historical reads/parses from
eliminating every O(generations) metadata check; this report records that limit.

## Overall Contribution

This is a coherent improvement to projection maintenance: it batches metadata
acquisition, reuses verified historical structure and retains agent-authored
prose. The scope remains appropriately conservative about evidence admission and
operational freshness. The reviewed corrections address both measurable scaling
overhead and a concrete fault-path semantic regression.

## Open Questions

None.

## Suggested Follow-Ups

- Give the instance a rebuildable accepted-OID lookup, or a verified read context,
  to remove repeated linear accepted-history membership checks.
- Consider batching declared-origin artifact reads once per block, preserving
  per-path fault attribution, if those reads become the next measured cost.
