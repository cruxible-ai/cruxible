# Code Review

## Verdict

Approved. The manager independently inspected `5203a525` after requesting a
regression for canonical candidate bytes stored under the wrong digest filename.
The correction binds both full and summary reads to the requested identity.
The optimization preserves full validation on changed bytes and memoizes only
immutable prose and parent metadata.

## Manual Review Priority

- Priority: P1
- Reason: Cached metadata feeds review commit identities and settled-ref classification.
- Suggested Human Review Focus: Fresh-byte proof, filename binding, unchanged prose,
  cache ownership, and cold versus warm performance claims.

## Scope Reviewed

- Changed files: candidate_review_summary.py, proposal_evidence.py,
  proposal_message.py, proposal_note_projection.py, instance.py, the focused
  candidate-summary tests, and the portable benchmark under docs/benchmarks.
- Untracked files: New helper and test inspected before their final commit.
- Tests examined: All twelve helper cases and existing grouped-note/archive/prose
  assertions named by the implementer.
- Commands run: Exact source diffs and surrounding strict candidate read/formatting
  code; the independent copied-instance index comparison is recorded in the
  integration report. Implementer verification: 48 scoped tests, Ruff/format,
  and five-source Mypy passed.

## Findings

No remaining findings. The wrong-filename candidate case now refuses through
both readers and has a focused regression.

## Complexity Assessment

Every lookup opens and reads the current regular file and hashes all its bytes.
The cache skips repeated parsing, canonical rendering and prose extraction;
unchanged bytes are never inferred from mtime or file size. Retention is bounded
at 512 entries and 8 MiB of accounted immutable fields, not interpreter heap.
The final-code index benchmark improves 4.427 seconds without reuse to a 0.689
second warm median. Cold construction remains 4.345 seconds. These measurements
exclude instance recovery and note publication; all OIDs and grouped note bytes
are compared outside the timer.

## Architecture Assessment

The full reader and summary reader use one strict parser. Only three immutable
strings survive validation: parent semantic root, neutral summary, and ordered
member roll. Both the original formatter and cached summary use the same prose
assembly. Index and archive consumers substitute these fields directly. The
cache contains no candidate model, approval, admission, or authority decision.

## Test Coverage Assessment

Tests cover current and legacy records, exact prose, same-size/restored-mtime
corruption, changed canonical metadata, wrong-file identity, malformed/truncated
bytes, symlink/directory refusal, disappearance, immutable returns, path isolation,
and count/byte eviction. Grouped-note/archive/prose regressions passed. No full
suite or journal golden corpus was run.

## Documentation Assessment

Helper comments and benchmark metadata distinguish byte verification, parse
reuse, accounted memory, and complete-index versus end-to-end latency.

## Overall Contribution

A bounded optimization of a concrete regression exposed by the review fix.
Ledger and evidence remain authoritative; removing the cache changes cost only.

## Open Questions

None.

## Suggested Follow-Ups

Profile remaining historical Git/note work before adding further caches. Cold
recovery and whole-world orientation remain separate costs.
