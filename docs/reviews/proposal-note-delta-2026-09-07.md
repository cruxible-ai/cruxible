# Code Review

## Verdict

Approved with comments.

Implementer self-review; no independent review is claimed. Commit `e5550a16` on `codex/write-reconstruction-performance` incrementally maintains review relationships over freshly checked evidence and preserves the full builder as its oracle. This batch follows `604f95bf`, which already includes changeset-driven accepted-index construction. Neither batch is merged into `playbill` or deployed by this work.

## Manual Review Priority

- Priority: P1
- Reason: Shared proposal/approval review machinery, with cache correctness and interruption semantics.
- Suggested Human Review Focus: Evidence freshness and cache ownership; original/advisory alias collisions; completion of interrupted admissions; fresh approval reads under the existing locks.

## Scope Reviewed

- Changed files: `playbill/instance.py`, `proposal_evidence.py`, `proposal_note_projection.py`, `proposals.py`, `service/documents.py`, and `test_grouped_proposal_notes.py`.
- New implementation/test files: `proposal_note_cache.py`, `test_proposal_note_cache.py`.
- New documentation: this guide and `proposal-note-delta-benchmark-2026-09-07.json`.
- Tests examined: grouping, shared candidates/commits, concurrent approval/publication, corrupted notes, interrupted evidence, archival rebuild, stale instance reconciliation, index budget, source replacements/deletions, same-size/restored-mtime corruption, symlinks and returned-model mutation.
- Commands run: named pytest scopes below, Ruff on changed files, mypy on six changed source files, and `git diff --check`. Tests ran in `/private/tmp/playbill-grep-surfaces-v1` with the repository virtualenv and `PYTHONPATH=src:packages/cruxible-client/src`; no canonical-checkout tests, full suite or golden corpus.

## Findings

No findings.

Review caught an evidence-object injection issue before commit: direct review publication must use its supplied evidence reader so approval reads remain observable and under the candidate lock. The final instance index accessor accepts that reader; the existing lock regression passes.

## Complexity Assessment

For N evidence records, B evidence bytes and M changed complete proposals, each load still inventories and reads the admission/evaluation files and referenced candidate bytes. It does not trust timestamps or an unverified filesystem revision. Unchanged record validation is reused by exact byte length and SHA-256; existing candidate-summary validation independently checks fresh candidate bytes.

Membership comparison, map copies and the protected return snapshot remain O(N). Only affected proposals need Git alias derivation; an unchanged load launches no alias subprocesses and decodes no admission/evaluation records. Candidate-to-proposal lookup replaces a scan through all commit groups for candidate-specific operations. Rendered notes and approvals are not cached.

Retention is instance-owned and capped at 20,000 admission/evaluation files and 16 MiB of accounted source/summary bytes, excluding Python heap overhead. Oversized snapshots are returned without retention. Failure clears the cache; restart reconstructs it. This is incremental relationship maintenance, not a fully delta-bounded filesystem read path or a persistent index.

## Architecture Assessment

Read the implementation in this order:

1. `proposal_evidence.py`: factors ordinary canonical model validation into `parse_model_bytes`; `_read_model` still applies it to freshly read bytes. The new cache calls the same validator rather than defining weaker parsing rules.
2. `proposal_note_cache.py`: observes fresh inventories under the caller's review lock. It reuses validated records only for identical bytes, reads each referenced candidate through the existing strict summary reader, refuses duplicate evaluations, and preserves the cold builder's behavior for incomplete persistence. It compares complete proposal inputs, removes obsolete memberships, and derives aliases only for affected proposals. Missing candidates becoming present naturally complete every waiting admission. Caller-visible results are deep copies so mutable nested models and sets cannot poison retained validation.
3. `proposal_note_projection.py`: retains `build()` as an independent full reconstruction. Adds a candidate-to-proposal reverse map for scoped alias lookup; canonical note rendering, corruption/subset rules and publication remain unchanged.
4. `instance.py`: owns one cache and supplies it to the common proposal service, approval/activation checks and review reconciliation. It persists across accepted-head movement because its own evidence bytes determine freshness. Reconciliation honors explicitly supplied evidence readers.
5. `proposals.py`: obtains before/after snapshots through the provider, while standalone services default to full reconstruction. Durable admission/evaluation/candidate writes still precede note publication. The publication delta now includes all aliases associated with the candidate, including older admissions newly completed by its persistence.
6. `service/documents.py`: uses the same index for approval and acceptance reconciliation. Git notes and approval records are still read at the operation boundary under the existing review/candidate locks.

No new authoritative store, durable change journal or public wire format is introduced. Proposal evidence remains out-of-band evidence, not accepted state. Governed authority remains the signed ledger and accepted Git tree. There is no atomic dual write to ledger and cache.

## Test Coverage Assessment

- `pytest tests/test_playbill/test_grouped_proposal_notes.py tests/test_playbill/test_proposal_notes.py -q`: **24 passed**, 40.35 s.
- `pytest tests/test_playbill/test_proposal_note_cache.py tests/test_playbill/test_review_publication_concurrency.py tests/test_playbill/test_review_projection_snapshot.py tests/test_playbill/test_review_archive_rebuild.py -q`: **42 passed**, 37.00 s.
- Ruff on all changed Python files: passed.
- Mypy on all six changed source files: passed.
- `git diff --check`: passed.

The new interrupted-candidate case uses different rationales to create distinct original and advisory aliases for admissions sharing a candidate. Completing that candidate must publish both original groups; checking only the newest proposal's commit would miss this case. Other tests prove exact parity of memberships, aliases and note bytes with the cold builder, plus no repeated alias derivation or record decoding on unchanged loads.

## Documentation Assessment

Module and method documentation explain freshness, ownership, caller lock requirements, retention bounds and the distinction between reusable validation and cached notes. There is no new SDK-facing flow or format to document. This guide records the workload and warm/cold boundary explicitly.

## Overall Contribution

This removes repeated global reconstruction of review relationships from the common write loop. It improves repeated submission and acceptance while retaining fresh-byte corruption detection and recoverability.

### Paired benchmark

Private program copies, identical seven-member authoring input and accepted parent; each replay independently mints Claim IDs, so these are equivalent workloads rather than identical accepted coordinates. Four runs in full/incremental/incremental/full order. Both arms include the earlier changeset-driven accepted-index optimization. Startup indexing is measured separately, then both arms run in a warm-index condition. Open/create/setup are excluded; there is no profiler, HTTP transport or attached workspace. No tests ran concurrently with timing.

| Operation | Full review-index reconstruction | Incremental review index | Mean reduction |
| --- | --- | --- | --- |
| Warm note-index load | 0.690–0.792 s | 0.0195–0.0196 s | 97% |
| Warm submission | 5.78–5.88 s | 4.26–4.27 s | 27% |
| Warm acceptance | 5.31–5.37 s | 4.51–4.83 s | 13% |
| Cold note-index startup | 4.39–4.48 s | 4.41–4.61 s | No improvement claimed |

Every replay compared its derived membership, advisory aliases and all rendered note groups with a fresh full builder over that replay's evidence. Each completed with 94 commit groups. These timings must not be compared directly with the prior 11–12 second acceptance result, whose process/cache condition differed.

A separate attribution-only profile found a cold full index dominated by candidate validation (8.75 of 9.57 profiled seconds), while its second call spent 0.72 of 0.77 seconds deriving 47 review commit aliases. That explains both the warm improvement and unchanged restart cost. The profile is not a latency benchmark.

## Open Questions

None blocking this batch.

## Suggested Follow-Ups

- Independently review and integrate both performance batches; then measure the installed SDK/daemon workflow.
- Keep cold startup distinct from repeated operations. A future durable, trustworthy evidence change stream could avoid complete inventory reads; this implementation does not infer such a stream from file timestamps.
- Review branch reconciliation still enumerates the full retained branch inventory, although it reuses this index. Consider targeted ref updates if measured publication cost warrants them, while preserving the real fan-out when accepting a new base makes many proposals stale.
- Attribute the remaining warm submit/accept cost before selecting the next optimization. Current results do not justify claiming that note maintenance explains the rest.
