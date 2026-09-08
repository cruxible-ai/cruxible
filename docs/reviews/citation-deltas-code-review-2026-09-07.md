# Code Review

## Verdict

Approved.

Implementation self-review of `5a8ab4fce..75d64f50e7e2eabbb4446febfa5109825ee72cbd`, performed before committing the reviewed production/test changes. No independent reviewer was dispatched in this slice. The change preserves the cold compiler's semantic rows while replacing whole citation-slice maintenance with immutable owner/group state and exact SQLite row deltas. It remains local, not merged or deployed.

## Manual Review Priority

- Priority: P1
- Reason: Cross-artifact conflict maintenance, immutable cache ownership, and the accepted-generation publication path are shared infrastructure.
- Suggested Human Review Focus: Old/new group membership and removals; Claim-wide capture precedence; verified coordinate and cache-clear boundaries; cold fallback for older lossy conflict slices.

## Scope Reviewed

- Changed files: `playbill/citation_relations.py`, `projection_delta.py`, `assembler.py`, `activation.py`, `instance.py`, and `storage/playbill_projection.py`; existing citation retirement and projection delta tests.
- Untracked files at review: `playbill/citation_index.py` and its focused tests. The separate benchmark harness is validation tooling, not daemon code.
- Tests examined: Citation retirement, owner/group parity and retention, projection delta, citation freshness, assembler, activation, activation guards, projection extensions, Claim compilation cache, derived runtime.
- Commands run: Named pytest scope below; Ruff check/format for changed Python files; mypy on seven changed production modules; `git diff --check`.

The tests run from the isolated worktree with its source/client directories on `PYTHONPATH`, using the canonical environment's interpreter and tools. No canonical-checkout tests, full suite, or golden journal corpus were run.

## Findings

No findings.

The review identified and resolved one compatibility edge before the commit: old partial rebuilds retained only exposed conflicts and could lose suppressed source/span rows. Bootstrap now compares its reconstructed visible conflicts with the exact parent's stored rows. A mismatch selects full successor reconstruction before SQLite mutation. Historical parent files are not rewritten.

## Complexity Assessment

Let D be changed owner paths/uses, G the uses in the union of old/new affected capture, exact-external and same-version groups, and C the raw conflict rows belonging to affected live Claims. Persistent membership updates cost logarithmic map operations per changed use; conflict work follows affected groups and Claim-wide precedence. Unrelated groups are neither enumerated nor reparsed on a warm update. Group algorithms preserve existing sweep and witness semantics, including their potentially large costs for a heavily shared group; the bound is not universally O(D).

Only changed owner facts and affected visible conflict rows are compared for an exact row delta. SQLite uses existing primary keys for deletes and inserts; no relation-wide delete remains. Full database backup, coordinate-bearing explanation rebinding, integrity verification and logical export remain broad operations outside this slice.

Roots retain canonical bytes/scalars in persistent maps. There are no parent snapshot chains. The instance registry owns the cache and build scheduling. Default retention is at most four roots and 128 MiB of conservative encoded-payload estimates, configurable on the private adapter constructor. Estimates double-count some shared payloads and are not measured RSS or a hard budget on caller-held roots. Eviction, explicit clear, restart, and oversize roots select cold bootstrap; cold group construction remains proportional to the full citation inventory.

## Architecture Assessment

Read in this order:

1. `citation_relations.py`: The cold compiler's contract, Claim-use, use-row and conflict builders are factored without changing their schemas or conflict rules. Raw conflicts can now be retained before Claim-wide precedence filters them. The legacy partial helper remains for existing internal callers; production fallback uses the full cold compiler.
2. `citation_index.py`: Owners retain their exact emitted use/source/external/contract facts. Canonical use rows are indexed by Claim path and citation ID, preserving cold ordering. Persistent capture/external/version memberships remove old uses and add new ones. Touched groups rebuild from final membership, including unchanged Claims within them. Raw conflicts are indexed by both group and live Claim; updating one group can therefore reveal or suppress conflicts in another group for the same Claim. Exact old/new visible rows produce the SQL delta. Returned models materialize from immutable bytes.
3. `rebuild_citation_index` and `CitationIndexCache`: The first use rebuilds from a verified parent's citation-use and contract rows and checks exposed conflict equality. Keys include instance, object format, Git OID, semantic root, generation root, compiler digest and schema. They omit physical repository location. Cache hits do not establish authority; the caller has already bound and verified the parent projection. Clear epochs fence in-flight cache publication, and successful SQL apply precedes successor retention.
4. `projection_delta.py`: The existing verified generation bundle and complete member set supply changed paths. Only changed member blobs are opened; unchanged CaptureContract inventory is no longer opened for a Claim change. Cached parent loading and delta computation use separate bounded build scopes to avoid nested build admission. Fixture/presentation/unsupported ownership, missing parent, or lossy parent state select cold reconstruction. Unrelated member changes carry an already-warm citation root without forcing cold bootstrap.
5. `storage/playbill_projection.py`: Copy the verified parent into an independent staged database, apply exact citation primary-key deletes/inserts in the existing transaction, then verify the resulting database. Coordinate metadata and all existing publication order remain unchanged. No table, index, fact schema, ledger wire field, or commitment format changes.
6. `instance.py` and `activation.py`: One instance-owned adapter is passed through the shared activation publisher to the assembler. Standalone assemblers without an owner rebuild the index from their verified parent; there is no global process cache or transport-specific orchestration.

Candidate cache entries may exist after staging but before acceptance, under their exact future coordinate. They cannot make a candidate accepted: another consumer must first bind a verified accepted parent with that complete coordinate. SQL failure does not retain a successor, and later publication/CAS failure cannot advance authority through the cache.

## Test Coverage Assessment

The named regression run passed 100 cases in 117.94 seconds:

```
test_citation_index.py
test_citation_retirement_relations.py
test_projection_delta.py
test_projection_citation_freshness.py
test_assembler.py
test_activation.py
test_activation_handoff_guards.py
test_projection_extensions.py
test_projection_claim_cache.py
test_derived_runtime.py
```

The additional witness-cap/half-open-span/complete-removal case passed separately. The existing real Capture test was extended after collection and passed separately: scoped maintenance opens exactly one changed Claim's Capture and reproduces every cold relation row. There are 101 distinct passing cases across these runs, not a single 101-case run.

Focused coverage includes 100 deterministic randomized transitions checked against the full cold compiler; multi-citation moves/removals/retirement; restoring untouched weaker conflicts; exact delta application; retained-reader/model isolation; shared untouched map nodes; incremental weight parity with reconstruction; contract replacement/removal without Claim group reads; coordinate cache isolation, eviction/clear fencing and lossy-parent fallback; witness limits and span boundaries. Real activation tests assert no global citation slice is read on a warm successor and compare every SQLite row/logical digest to full reconstruction across create/revise/retire. Existing activation tests cover publication failure boundaries and historical parent preservation.

Ruff and formatting passed for all changed Python files and the benchmark harness. Mypy passed on seven production modules. No new transport or SDK contract was introduced; the actual SDK/Unix HTTP benchmark separately validates writes, exact readback, and fresh-process recovery.

## Documentation Assessment

The private module documents the authority boundary, retained raw conflicts, explicit full export, encoded-byte accounting, and cold fallback. Comments explain the non-obvious old/new group and Claim-wide precedence cases. The performance report and portable benchmark describe cold versus warm work and keep phase timing separate from end-to-end latency. No public SDK example or wire catalog change is necessary.

## Overall Contribution

This is the citation owner/group slice of the derived-state redesign. It removes unrelated-world work from repeated citation maintenance while leaving exact ledger authority and cold reconstruction available. It also restores cold parity where an old lossy incremental path could suppress a conflict indefinitely. It does not complete the full performance or managed-state program.

## Open Questions

None.

## Suggested Follow-Ups

- Carry exact prepared evaluation results into submission when their complete inputs still match.
- Address explanation binding, physical Git/database copies and logical export in their planned slices.
- Extend managed configuration, shared-allocation accounting and reclamation across all derived adapters; large hot groups still need measured optimization or partitioning.
