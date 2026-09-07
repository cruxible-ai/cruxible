# Code Review

## Verdict

Approved with comments. This is an implementation self-review, not an independent review. The internal submit handoff removes one repeated evaluation while preserving fresh ingress, principal, writable, accepted-coordinate, CAS and publication checks. No SDK certificate authorizes reuse.

## Manual Review Priority

- Priority: P1
- Reason: Changes the evaluation boundary before proposal publication.
- Suggested Human Review Focus: observation completeness and fallback; immutable candidate/tree ownership; preflight-to-submit binding; unchanged publication checks.

## Scope Reviewed

- Changed files: `playbill/authoring/preflight.py`, `authoring/coordinator.py`, `instance.py`, `proposals.py`.
- New files: `playbill/prepared_evaluation.py`, `tests/test_playbill/test_prepared_evaluation.py`.
- Implementation: `7165b40a379345f284fdd9055594ce334ac9c902`, based on `d6f24a7b6`.
- Tests examined: same-call handoff, authoring change sets and preflight, prepared lowering, evaluation request reuse, proposal publication/refusal/rebase, capture v2, and claim policy value demand.
- Commands run: focused pytest groups (58 + 67 + 2 passing cases; one golden-named case excluded); Ruff check and format checks on all six changed files; mypy on all five production modules; `git diff --check`.
- No full suite, golden corpus, canonical-checkout test, rollout, or independent review was run.

## Findings

No findings.

## Complexity Assessment

Fresh observation replay is proportional to distinct CAS observations and the body bytes actually read. Observation storage is capped at 32 MiB and 16,384 records; exceeding either selects full evaluation. Those bounds do not cover the entire candidate: retained immutable tree handles and detached candidate/diagnostic/account models remain bounded by the existing receive contract. The adapter keeps counters and an invalidation epoch, not persistent evaluation results. Scope exit releases the handoff even on exceptions.

Card stripping still scans and constructs a submission snapshot proportional to the physical candidate tree. Receive validation and Git tree publication retain existing broader work. This is duplicate-work removal, not a claim that submission has become entirely changeset scoped.

## Architecture Assessment

Read in this order:

1. `prepared_evaluation.py`: the instance-owned adapter registers with DerivedState. Its per-call scope observes CAS verify/read operations, retains detached passing results, and closes on every exit. Unknown body operations, writes, inconsistent observations and unsupported operational observations disable reuse. Epoch invalidation and one-time consumption prevent later reuse.
2. `authoring/preflight.py`: only a submit-supplied scope wraps evaluation dependencies. The retained operation binds the complete authored intent except computed status, descriptor, exact accepted coordinate/compiler, actor, request, limits and canonical timestamp. Failed and rebased evaluations cannot provide a handoff.
3. `authoring/coordinator.py`: one scope encloses submission. After persisting preflight status, the coordinator rechecks authored-operation equality and passes the sealed non-card snapshot to ProposalService. Ordinary SDK preflight remains unchanged.
4. `proposals.py`: fresh writable, capability, namespace, main, principal and receive checks precede consumption. Snapshot identity and complete canonical metadata must match. CAS observations replay against the current store with their original access context. A miss uses the original evaluator; commit, evidence, notes and ref publication remain unchanged.
5. `instance.py`: owns and supplies the adapter through the existing service factory. There is no transport field, new authority store, wire migration or frozen-digest change.

Operational query facts, producer receipts and promotion verification currently select fallback when consulted. This deliberately avoids treating mutable exhaust as revision-bound evidence before such a binding exists. Synchronous same-call reuse does not span a daemon restart, SDK request boundary or approval delay.

## Test Coverage Assessment

Integration tests count one evaluation on eligible submit and compare the stored candidate with full preflight. Clear/budget/operational fallback tests count two evaluations and compare candidates. Unit cases vary current coordinate, actor, timestamp, request ref, receive limits, owner and sealed tree identity; check operation mismatch, output-model mutation, expiry, single use, original CAS access context, changed/missing/error bodies and unsupported body operations. Fresh CAS failure reaches the old evaluator and preserves the coordinator's unchanged-coordinate integrity error. A fresh write guard still rejects before consuming the handoff. Existing proposal and authoring tests exercise publication, rebase and refusal behavior.

## Documentation Assessment

Module and scope comments describe the lifetime, mutable-input exclusions and fallback contract. The accompanying performance report distinguishes measured latency from eliminated evaluation count and lists remaining world-sized work. No public SDK/API behavior or schema changed.

## Overall Contribution

A cohesive internal optimization under centralized derived-state ownership. It spends previous same-call computation once while retaining ledger authority and fresh mutable observations. It intentionally does not extend reuse to acceptance or operational evidence without a trustworthy revision binding.

## Open Questions

None.

## Suggested Follow-Ups

- Bind operational evidence revisions before admitting those evaluations to reuse.
- Replace broader receive/card/Git inventory work with verified scoped interfaces where parity can be established.
- Account for active handoff memory and concurrent admission under managed-instance resource budgets.
