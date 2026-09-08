# Batch 2 derived-state implementation review

Approved after independent correctness review and the fixes recorded below.

Branch: `codex/derived-state-batch2`. Base: `ae5f7a75` (the merged SDK snapshot change and batch 1 design). Implementation head: `dbf613d31bc393f99a2ff33f993786a549b15fd0`.

## Problem and resulting behavior

Every fresh draft rebuilt Claim contender membership. Incremental evaluation still copied member, dependency, reverse and Merkle maps, and detaching a cached result copied the whole evaluation state. Accepted and candidate derivations shared a single cache slot.

The new in-process `PlaybillInstance.derived` owner retains accepted immutable trees bound to the full verified coordinate and genesis incarnation. Candidate builders fork persistent ordered maps and seal complete edits without copying unrelated members. Successive accepted reads derive exact physical changes from verified Git commits and read only changed blobs. Accepted roots and candidate revisions remain separate; old readers retain their data across advancement and cache eviction.

Claim contender membership is reused across fresh singleton, batch and succession authoring. Generated edits update old/new groups before the next member. Operation-local parsed rows reduce repeated work without exposing retained mutable models. The existing live-lifecycle rule, full Subject/predicate key, UTF-8 order, qualifier/disposition decisions, and separate predecessor/successor vocabulary views are preserved.

Members, Claim Subject lookups, dependency/identity/reverse maps and Merkle node storage use structurally shared ordered maps. Canonical-byte or immutable scalar rows are retained; law-facing models and small public wrappers are detached. The prospective edit set drives member commitments and index advancement. External full-tree ingress still verifies a complete comparison; it cannot assert a cheap incomplete delta.

The owner registers initial accepted/evaluation/contender/Claim compilation/proposal-note adapters and owns prepared-lowering retention. It schedules bounded builds outside its lock, exposes version/source declarations and resource status, fences clear against in-flight publication, and supports explicit leases. Overload is an operational HTTP 503 with retryable context, not a persisted law refusal. The SDK's generic error reconstruction does not yet preserve that context for automatic retry.

## Implementation commits

- `ee4021de`: persistent ordered AVL maps and Merkle update storage.
- `237b1632`: scoped dependency and Claim Subject index updates; canonical retained rows.
- `438956cd`: detached Merkle node/root/domain values over shared scalar storage.
- `dbf613d3`: instance owner, snapshot and delta integration, contender consumers, resource bounds and tests.

No governed pin semantics, artifact codec, frozen commitment preimages, admission/approval law, acceptance CAS or publication ordering changed. The ledger and retained source stores remain authority. No derived index becomes durable source data.

## Before and after measurements

Matched source-SDK/Unix HTTP workload: 1,000 seeded Claims, two history steps, four authored Claims per write, eight orphan proposals, two successive writes, attached disposable Git workspace, server profiling disabled. The repeated write follows a real acceptance; it is not a replay of one cached intent. Baseline `17849d77` has the same production bytes as branch base `ae5f7a75`.

| Operation | First before | First after | Repeated before | Repeated after |
|---|---:|---:|---:|---:|
| Prepare | 0.841 s | 0.830 s | 0.528 s | 0.319 s |
| Submit | 3.002 s | 2.181 s | 2.237 s | 1.855 s |
| Accept | 3.168 s | 2.532 s | 3.383 s | 2.630 s |
| Full loop, including approval/readback/refresh | 10.789 s | 8.940 s | 10.084 s | 8.338 s |

Repeated preparation improved about 40%, submission 17%, acceptance 22%, and the full loop 17%. These are small observations, not p95 or a direct comparison with the earlier seven-member private-program workload. Both arms accept and read back exact intended values. Fixture verdicts remain current/uncovered under its existing evidence policy; acceptance is not claimed to prove support.

[Raw served timings and workload](batch2-served-benchmark.json).

Five-sample medians for isolated fresh drafts at fixed changed-member count:

| Unrelated population | Evaluation before → after | Contender lookup before → after |
|---:|---:|---:|
| 100 | 2.884 → 0.742 ms | 15.116 → 0.574 ms |
| 1,000 | 25.769 → 3.546 ms | 155.621 → 0.551 ms |
| 10,000 | 269.377 → 34.649 ms | 1,857.743 → 0.611 ms |

Evaluation semantic-projection visits fall from 4N to zero; contender parsing falls from N to two in this bounded-group workload. Cold 10,000-member evaluation increases from 1.154 to 1.233 seconds and contender initialization from 1.922 to 1.973 seconds. Persistent maps have higher small-map constants. Wide flat Merkle directory ancestors still cause real hashing fan-out; evaluation is not universally O(changed members).

[Scope benchmark evidence](batch2-scope-benchmark.json) and [reproduction harness](../benchmarks/derived-state-scope.py).

## Correctness review and validation

Independent review closed these issues before integration:

- Resource accounting walked whole roots after delta advancement: byte/member counts now advance from old/new changed rows, separating semantic retention eligibility from history/cards.
- Incremental collision errors differed from cold diagnostics: failed fast-path validation now defers to the original cold validator. Noncanonical external input retains its original normalization boundary.
- Frozen nested models, Merkle values and small index wrappers could poison later reads: retained rows are bytes/scalars, public models are detached, and immutable wrappers use slots or detached shells.
- Cold candidate lookup tried to parse malformed replaced/deleted parent data: unverified cold candidate roots build from final bytes; verified warm roots use scoped updates.
- Different-key builds were unbounded and snapshot cache limits were bypassed: registry admission and explicit retention checks now cover those paths.
- Capacity exhaustion initially received an artifact-edit repair: it now carries HTTP 503/retryable context without the misleading repair.

Validation ran only in isolated worktrees/private environments:

- Final named Core/Client scope: 222 passed and one obsolete Git-read count assertion. That assertion was updated to test one accepted-root binding, and its rerun passed: all 223 named cases have passed.
- Separate final resource/server-error scope: 31 passed, one skipped. Additional authoring/capture/succession scopes passed during consumer development; counts overlap and are not summed.
- Independent review ran 32 targeted cases and 100 randomized staged/rollback/accepted-delta accounting checks.
- Ruff check/format passed for all changed production/test Python files; Mypy passed for 20 changed source modules; Git whitespace checks passed.
- Offline Core and client wheels passed byte comparisons for all 305 packaged Python files. An installed daemon completed two typed-World write/approval/accept cycles; a separate client-only environment, with Core absent, read the accepted Claims. Fresh-process recovery reproduced the final coordinate. [Installed validation](batch2-wheel-validation.json).
- No full suite or golden journal corpus was dispatched. Tests cover both Merkle commitment families and exact cold/incremental roots, proofs and refusal ordering.

## Remaining scope and limits

This implements the batch 2 local owner/snapshot/contender/persistent-update foundation, not the complete multi-batch design or a managed distributed runtime.

- Memory accounting is conservative input-byte estimates, not measured RSS or exact shared-allocation accounting. Cache/build limits are enforced; caller-held leases have accounting but no hard memory cap. Aggregate shared-node budgeting, bounded reclamation and deployment-grade version-upgrade orchestration remain owner follow-ups before managed rollout.
- Legacy history/floor/query/evidence adapters are not all migrated to the registry. Existing fresh source checks remain; source-revision protocols and complete dependency observations belong to later batches.
- Full external ingress, some migration/retirement closure builders, Procedure reference parsing, flat exports, Git serialization and some SQLite work remain broad. Cold bootstrap and explicit recovery are still broad by design.
- The current Merkle format hashes complete direct-child lists. Removing that fan-out would require a separately reviewed commitment/layout decision; this change preserves it.
- Citation owner/group updates, same-call prepared evaluation reuse, broader Git path/inventory work, normalized explanation binding and evidence/history scope are still subsequent batches. Prepared lowering ownership has moved; this is not an approval-delayed evaluation cache.
- Existing exact governed dependency pins are preserved. Any narrower Subject referent law remains a separate versioned decision.

Code is committed locally on its isolated branch; it has not been merged into playbill, pushed or deployed. Installed validation used private fixtures. Routine program-state reconciliation is recorded separately, without changing adoption or release authority.
