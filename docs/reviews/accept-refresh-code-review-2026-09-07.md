# Code Review

## Verdict

Approved with comments. Implementation self-review, not independent review. Discovery now reads the accepted inputs it needs and shares immutable batch inputs; acceptance reconstructs exact physical trees from a proven parent and Git's complete delta. Neither change caches live evidence or removes settlement's final stored-payload comparison.

## Manual Review Priority

- Priority: P1
- Reason: Shared read orchestration and the physical verification path before publication.
- Suggested Human Review Focus: batch coordinate ownership; fresh evidence/time behavior; complete physical delta versus semantic scope; mode and injected-file refusals.

## Scope Reviewed

- Base: `8cfe40b6b` (`codex/prepared-evaluation-reuse`).
- Refresh fix: `73d4013c`, services `playbill_search.py`, `playbill_claims.py`, `playbill_evidence.py`, and `test_discovery_batch_reads.py`.
- Batch lookup follow-up: `53684b5d` keeps group ClaimType lookup in the same verified context and adds a no-extra-Git-read assertion.
- Acceptance fix: `d422c08c`, `playbill/git.py`, `instance.py`, `settlement.py`, `service/documents.py`, and `test_git_tree_delta_reads.py`.
- Diagnostic harness: search worker profiling added to the existing opt-in write-phase profiler.
- Verification: 55 distinct focused cases passed across runs (19 existing discovery/evidence, 2 new batch-read, 25 settlement/activation/handoff, 9 new Git-delta/settlement cases). Ruff, format checks, mypy on seven changed production modules, and diff whitespace checks passed. No full suite, golden corpus or canonical-checkout tests.

## Findings

No findings.

## Complexity Assessment

Previously discovery materialized full fact payloads for every Claim, reconstructed Claim inputs again, and scanned/sorted the full tree's provider catalog for every verdict. The request-local context parses each Claim once and scans the provider catalog once; discovery does not load full fact views. Whole-instance orientation still enumerates all requested Claims, resolves every live Claim on a memo miss and constructs its summary. This is reduced work and removal of a repeated global scan, not fully incremental orientation.

Delta tree reads transfer only added/modified blobs. Complete path mappings are still copied/sorted and compared, so CPU/allocation remains world-sized. Git publication, signatures, workspace advertisement and database export retain existing work. The new method creates no cache; it consumes the existing centralized immutable accepted root. Batch context retention lasts one request and scales with the selected tree and parsed Claim inputs.

## Architecture Assessment

Read in this order:

1. `ClaimVerdictReadContext` in `playbill_evidence.py`: a frozen request binding to an instance and accepted coordinate, backed by the instance-owned immutable tree. Parsed Claims and the provider catalog are shared only inside that request. The verdict service rejects mismatched instance/coordinate contexts and still evaluates current replay, attestations and time boundaries normally.
2. `playbill_claims.py`: group resolution accepts the optional context and passes it through the ordinary verdict service. Other callers retain their current behavior.
3. `playbill_search.py`: discovery consumes accepted Claim envelopes directly, bypasses unused full fact views, and avoids Claim work when the requested kinds exclude Claims. Resolution, filtering, pagination and orientation output contracts remain intact. Existing resolution memo behavior remains unchanged.
4. `GitLedger.read_tree_delta`: the explicit precondition is an exact, previously proven parent tree. Git's complete raw mode/object diff identifies all additions, modifications, type changes and removals. Unchanged entries retain their proven bytes; changed destination modes must be ordinary files, and changed blobs are read through the existing verified batch reader. Parent/status inconsistencies refuse. The result preserves full tree mapping and byte order.
5. `instance.proposal_tree`: an optional accepted base enables delta reconstruction for acceptance; existing callers without a base keep the full read. Accepted-base lookup still proves the coordinate.
6. `service/documents.py` and `settlement.py`: acceptance supplies the evaluated base, then verifies the stored signed generation using another complete physical delta from the accepted parent. Full equality against the expected generation remains mandatory. Semantic candidate members do not substitute for the physical delta, so derivative cards, daemon records, deletions and unrelated injected files are included.

No transport/schema, pinned law, signed receipt, approval, digest or publication-order changes. Cold recovery remains available and existing restart-parity tests pass. The Git delta helper is parallel to the older recovery helper; migrating that historical verification implementation was deliberately left outside this change.

## Test Coverage Assessment

New discovery tests compare raw accepted Claim inputs with full projected views and compare batched verdicts with independent service verdicts. They assert one provider scan, prohibit full fact-view materialization, exercise non-Claim discovery, preserve freshness boundaries and fresh replay checks, and reject a wrong-coordinate context. The same discovery test forbids per-group `blob_at` calls, covering the dependency on the removed full-tree memo warmup. Existing search tests cover deterministic outputs and pagination; evidence tests cover attestation succession and historical reads.

New Git tests cover SHA-1/SHA-256, insert/edit/delete/move, unusual paths/binary bytes, no-op and reverse deltas, exact mapping/order parity, changed-blob counts, and executable/symlink/gitlink refusals. The settlement regression disallows full-tree reads after the parent is warm and injects an unrelated signed-tree file; final payload comparison rejects it before main moves. Existing settlement and handoff tests verify restart parity, frozen candidate versions and failure boundaries.

## Documentation Assessment

Docstrings state the proven-parent precondition and request-local evidence boundary. The performance report records measured end-to-end effects, profiler overhead, remaining broad work and deployment status. Public SDK refresh behavior is unchanged: it still returns full orientation rather than merely moving a pointer.

## Overall Contribution

Two bounded fixes selected from measured execution costs. They reduce unnecessary materialization and reuse exact immutable inputs without adding a new authority store or persistent cache. They improve repeated interaction latency while retaining the broader incremental-orientation and publication tasks as explicit remaining work.

## Open Questions

None.

## Suggested Follow-Ups

- Incremental orientation/resolution requires dependency-scoped accepted evidence and reliable mutable-input revisions, not coordinate-free verdict reuse.
- Consolidate the older recovery delta reader with the Git helper only in a separate parity-focused change.
- Profile remaining Git publication/advertisement and database serialization after these changes; do not assume they all disappear through delta reads.
