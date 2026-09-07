# Write-loop scope audit

Audited `0e39fea7` on `codex/write-reconstruction-performance`, 2026-09-07. This is an audit and proposed design sequence, not an implementation or a merge of the branch. Coverage is the local SDK authoring coordinator, proposal service, settlement/activation, accepted SQLite construction, and their history/review/query dependencies. It is not a fresh exhaustive audit of every Procedure/provider/read endpoint.

## Conclusion

Incremental compilation is working. The surrounding representations still cause whole-state scans, copies and repeated preparation. Several costs can be narrowed without changing product semantics. In particular, fresh-draft Claim contender lookup and citation relation maintenance deserve attention before a wholesale storage redesign.

Target work is proportional to **changed members + genuinely affected dependency/query/relation groups + changed storage paths**, with cold recovery and explicitly global queries permitted to inspect everything. A change to a widely used type or policy can legitimately affect much of the world; unrelated growth should not increase ordinary small-write work.

## Evidence and measurement limits

A fresh attribution run used a private copy of the program snapshot, the identical seven-member draft from the prior benchmark, followed by the same unrelated Document change. The review index and evaluation-state cache were warmed before profiling. The first lowering for this draft was intentionally not cached: that is the recurring case for a new piece of work. No HTTP transport or attached workspace was present.

The previous unprofiled seven-member measurements remain the latency reference: submit 2.57–2.58 s, accept 4.24–4.27 s. The new cProfile runs took about 4.59 s and 6.49 s. Profiling disproportionately slows Python-heavy code; the percentages below describe this trace, not exact shares of the unprofiled latency. Cumulative entries overlap and cannot be added as a budget or promised saving. Both profile operations accepted the same commits as the earlier exact-input benchmark, and final review memberships matched the full note builder.

Selected attribution is recorded in [the JSON](world-scope-performance-audit-2026-09-07.json). Source line references below name the audited head.

## Scope inventory and proposed fixes

Here N is accepted artifact/path count, B is tree bytes, H is retained history, P is retained proposals, C is citation uses, and D is the number of changed members. Some operations also sort their inputs; O(N) below means a whole-world dependency, not a claim that every implementation is strictly linear.

### 1. Fresh-draft Claim contender lookup — do early

**Where:** `playbill/authoring/lowering.py:440` (`_ClaimPredicateIndex`), `:2015` (`_lower_change_set`). Standalone `_same_slot_claims` also scans Claims.

**Current scope:** A new change-set lowering creates a fresh predicate index. Its first lookup walks the entire staged tree and parses every Claim, then subsequent staged members update it incrementally. The prepared-lowering cache reuses an exact intent's output; it does not share this membership index across new intents. The profile shows 4,318 `_replace` calls and about 0.87 s in `claims_for`, roughly 19% of profiled submission, with 1,734 Claim parses across the submit operation.

**Proposed scope:** Carry an accepted index from exact Claim bytes to `(subject, predicate)` memberships, and layer each candidate's changed memberships over it. Read/parse only matching contenders and changed Claims. Retain qualifiers and all siblings needed for cardinality/fan-in/retirement decisions; narrowing to the exact qualifier prematurely can change law behavior. Reuse existing statement/Subject indexing machinery where appropriate, but its current keys are insufficient for this lookup.

**Contract:** Key reusable parse/membership state to verified bytes and parent state; keep candidate overlays isolated, preserve duplicate/refusal ordering and lifecycle rules, and advance retirement/succession closure paths. Retained models must be deeply immutable or privately owned. Cold parsing remains the parity oracle.

### 2. Evaluation-state copies and incremental-index container copies — do early

**Where:** `playbill/evaluation_state_cache.py:40`; `playbill/closure.py:848`; `playbill/claim_subject_index.py:31`; client `contracts/merkle.py:276`.

**Current scope:** Every cache hit returns `deepcopy(self._state)`. The two submit derivations spent about 0.95 s together; all deep copies were about 0.93 s, roughly 20% of profiled submission. Acceptance has another approximately 0.48 s derivation. Incremental dependency updates parse/re-resolve affected members but copy whole state maps and all pin-source sets. Claim membership maps and the Merkle node map are also copied.

**Proposed scope:** An internal immutable accepted evaluation snapshot with structurally shared maps/sets and changed-key updates. Candidate derivation borrows that snapshot and owns only its overlay. Return detached public models only for requested results, not a copy of the entire internal graph.

**Contract:** Frozen dataclasses/Pydantic models are not enough when nested containers are mutable. Audit law/helper writes and enforce ownership; merely deleting `deepcopy` would expose cache poisoning. Bound overlay depth/retention and compact outside individual writes when necessary. Existing Merkle and closure commitments must remain byte-identical.

### 3. Repeated full-tree discovery and validation — bundle with the internal delta

**Where:** `proposals.py:400`, `:1422`, `:3463`; `candidate_cards.py:97`; client `canonical.py:374`; `authoring/lowering.py` staged `dict(base_tree)` copies; `authoring/prepared_lowering.py:100` retention.

**Current scope:** Full dictionaries cross boundaries, so each stage rediscovers changed paths, filters semantic members, normalizes/checks the entire path set and copies mappings. Submit calls `semantic_projection` eight times in the trace (about 0.15 s), and whole-tree receive validation twice (about 0.20 s). Lowering can copy an N-entry dictionary for multiple staged members. Prepared lowering retains a whole proposed tree per eligible cached intent, with full-tree byte accounting.

**Proposed scope:** Pass an internal verified parent snapshot plus an explicit changed-member overlay. Carry canonical path membership, total bytes, file count, depth/size constraints and directory collision indexes forward. Update candidate cards from the same member list; preserve historical cards by parent reference. Cache compact lowered deltas rather than complete candidate trees.

**Contract:** This must be a daemon-derived capability, not a caller assertion that its list of changes is complete. External full-tree submissions still require ingress verification. Before acceptance there is no signed accepted ChangeSetRecord yet: use a prospective delta, bind it to the evaluated candidate, and reconcile it exactly with the final verified changeset. Do not let callers hide out-of-scope mutations or daemon-controlled files.

### 4. Git reads and writes materialize the whole world — next major scope change

**Where:** `git.py:335` (`_write_tree`), `:1252` (`read_tree`); `proposals.py:3703`; `settlement.py:704`; `projection_delta.py:132`.

**Current scope:** Ordinary submit still transfers the current tree once and writes two complete index descriptions. `_write_tree` hashes every blob in Python, checks all object addresses, starts an empty temporary Git index and feeds every path. It already writes only absent blob bodies, so it is not rewriting every stored blob. Profile: one full read ~0.44 s and two tree writes ~0.76 s in submit. Acceptance reads the candidate, accepted base and stored generation (~1.30 s combined), plus a whole tree write (~0.35 s). Delta index construction inventories both parent and successor.

**Proposed scope:** Construct child Git trees from a verified parent tree plus member/card/changeset updates. Carry unchanged blob OIDs; write changed blobs only. A parent-seeded Git index is a practical first step, though Git may still serialize the whole index. A persistent Git-tree/path representation can rebuild only changed directories for a stronger bound. Verify the resulting tree OID and actual Git diff, reading changed objects instead of downloading unchanged blobs for equality again.

**Contract:** Keep real signature/ref/CAS checks. Parent/child coverage, modes, path collisions, object format and changed-body integrity must be proven. Transfer an exact operation-owned read/verification result across layers where available; do not silently replace fresh reads with arbitrary coordinate-only memo hits. Explicit corruption checks and cold verification remain part of the existing trust contract. Benchmark the first step rather than calling it O(D) while Git still walks an entire index.

### 5. Citation relationship reconstruction and replacement — do early

**Where:** `projection_delta.py:196`; `citation_relations.py:139`; `storage/playbill_projection.py:312` relation replacement.

**Current scope:** Unrelated changes now skip this entirely. A Claim change, however, still loads all previous use/conflict facts, copies all carried uses, regroups them, emits/validates the full relation slice, and deletes/reinserts that slice. Changed capture bodies and conflict generation are already scoped in parts of the algorithm; global fact materialization remains. In the Claim acceptance profile the relation build is ~0.82 s, and its timed build-plus-input/validation wrapper ~1.09 s (about 17%). SQL relation replacement is additional and overlaps the database-update total.

**Proposed scope:** Index relation ownership by Claim path and relation membership by capture digest, exact external source and version/span group. Derive affected keys from both old and new uses; load only those groups; update changed use/contract rows and conflict rows belonging to affected keys. Carry all other rows directly in SQLite. Range overlap queries or per-version indexes can further narrow same-version span work.

**Contract:** A changed use can affect other Claims in its group, so the update scope is not just the authored Claims. Preserve retired conflict witnesses/counts, deterministic ordering, removals of obsolete conflicts, and CaptureContract changes. A genuinely dense shared group may remain large. Compare every row with the full builder across new citations, retirement, supersession and span overlaps.

### 6. Every explanation is rebound to the new generation — representation fix

**Where:** `storage/playbill_projection.py:369–413`.

**Current scope:** The updater rewrites coordinate fields inside governance/provenance/coverage/history JSON for every carried artifact. The trace rebound **8,448 rows** for the seven-member change and **8,476 rows** for the one-Document change (~0.17 s Python callback time, plus SQL work). This creates global writes despite local semantic changes.

**Proposed scope:** Store coordinate-independent explanation payloads and an explicit typed generation-binding reference. Resolve the current enclosing coordinate at the read/export boundary. Do not rewrite arbitrary embedded evidence: an artifact's historical acceptance or source coordinate remains that historical coordinate.

**Contract:** Preserve externally returned explanation bytes/semantics where promised. Internal schema/compiler versioning may be needed, especially because SQL consumers and the frozen logical-export digest currently see the embedded JSON. If the old canonical export still synthesizes all bound rows, digesting that export remains global; this fix alone cannot remove that separate cost.

### 7. Full database verification and flat digests — remove duplicate work before changing format

**Where:** `assembler.py:294`; `storage/playbill_projection.py:493`, `:668`, `:1067`.

**Current scope:** Build runs SQLite integrity verification, counts rows, exports/hashes every logical row and hashes the entire file. First activation binding checks the new piece again, including its logical export and integrity. The profile contains **two logical digests (~0.76 s total)**. Later binds reuse the verified-piece memo for physical/logical/integrity checks, but still execute all table counts.

**Proposed first step:** Transfer an internal completed-build verification result for the exact immutable published file into activation. Reuse completed integrity/count/digest results only after the builder has performed all equivalent checks. On an unchanged already-verified piece, reuse verified counts rather than recounting tables.

**Contract:** Bind to exact file identity, manifest and coordinate; any changed/replaced piece or failure must use verification. Preserve same-size/restored-mtime corruption detection (the current memo includes ctime), publication races, and recovery tests. Do not pre-register an incompletely verified file. This reduces duplicates; one complete flat export/hash remains.

**Longer term:** A partitioned/Merkle commitment over logical rows permits delta digest updates. A flat SHA digest of the canonical whole export cannot generally be updated from arbitrary changed rows alone. Introduce an explicitly versioned derivative format and retain old verifiers; never recompute existing stored digests under a new rule. A new commitment without partitioned physical storage would still leave the whole-file hash.

### 8. Full SQLite snapshot copying — later, based on size

**Where:** `storage/playbill_projection.py:329`.

**Current scope:** `backup()` copies the complete parent into a new database for each generation. In this trace the copy itself is only ~0.09 s. This is a real O(database size) cost, but not the primary present latency problem. Physical hashing also remains proportional to file bytes.

**Options:** Filesystem reflinks with a correct portable fallback can reduce physical copying while preserving immutable generations; SQLite/WAL/checkpoint assumptions need explicit handling. For stronger portable scaling, choose versioned rows/snapshots or bounded base-plus-delta segments with background compaction and indexed reads. Partitioning stable pieces would also let unchanged file digests carry forward.

**Tradeoff:** These options affect historical readers, query planning, storage growth, compaction, manifest identity and crash recovery. Do not introduce an ever-growing overlay chain, mutate historical snapshots in place, or make the cache a second authority. This is a larger derived-storage design and ranks below the measured Python/global relationship work.

### 9. Law support registries and historical evidence — maintain focused indexes

**Where:** `proposals.py:2720`, `:922`, `:3115`; client `claim_attestations.py:192`; `projection_artifacts.py:542–632`; `instance.py:607`, `:1260`, `:1465`.

**Current scope:** Each evaluation walks all dependency states and reparses Subjects, ClaimTypes, CaptureContracts, Providers, Interfaces and Procedures into `_ResolvedArtifacts` (~0.16 s across submit evaluations). It also collects candidate identities, scans query definitions, and scans all changeset JSON to reconstruct accepted referent coordinates (~0.13 s in submit). Projection helpers search history per changed artifact for revision count, timestamp, accepted coordinate and law result. Accepted-coordinate maps and verified prefixes are rebuilt/copied; installation appends to the history tuple and invalidates the OID lookup, causing an O(H) rebuild on its next use.

**Proposed scope:** Carry immutable typed registries keyed by kind/identity/digest, a path-to-history index (revision count, latest acceptance and per-revision evidence), accepted referent-coordinate membership, and persistent OID/sequence lookups. Update from verified changeset members and law evidence. Resolve laws' requested dependencies lazily against these indexes. Preserve all ClaimTypes applicable to a Subject where freeze policy intentionally crosses predicates; do not limit that rule to the authored type.

**Contract:** A history-membership index must represent the exact coordinates the current helper derives from law evidence; replacing it with every Git OID would broaden attestation admissibility. Keep legacy candidate/law-version replay and exact refusal precedence. Full-history parsing remains the cold oracle. The `_ResolvedArtifacts` comment calling itself the one remaining instance-wide cost is outdated; this audit identifies several others.

### 10. Review evidence still scales with retained proposals — lower current urgency

**Where:** `proposal_note_cache.py:77`; `proposal_note_projection.py`; `instance.py:1068`.

**Current scope:** Warm alias derivation is incremental, but each load inventories/reads/hashes all admission/evaluation and referenced candidate evidence, compares all memberships, copies the maps and deep-copies the returned index. This is O(P + evidence bytes), not O(changed proposals). Warm unprofiled index load is presently 36–40 ms. Attached-workspace reconciliation additionally visits all history/proposals/aliases, reads withdrawals and notes, then replaces the derived ref inventory. Attachment was absent from these benchmarks, so no latency claim is made for it.

**Proposed scope:** Candidate-specific accessors and immutable index snapshots first. Later, a sequenced daemon-owned evidence transition feed with replayable updates can maintain membership and mark only affected aliases/refs dirty. An accepted-parent advance can legitimately stale all open proposals at that base, but need not revisit every already-settled proposal. Keep full reconciliation for startup, archive repair and explicit audit.

**Contract:** Proposal evidence has interrupted/multi-file completion and tamper-detection semantics. A best-effort callback or filesystem watcher does not prove completeness. Define producer ownership, gaps, multi-process publication and missing-file completion before relying on a cursor; detect unknown writes and fall back. Removing global fresh-byte checks changes when unrelated corruption is detected and therefore needs an explicit ruling, not an assumed optimization.

### 11. Corroboration query inputs can materialize the whole world — conditional

**Where:** `proposals.py:1056`; `service/playbill_query.py:194–305`.

**Current scope:** When a Claim policy requires corroboration, the facts provider builds all live Claim fact rows plus Subjects/Providers before running the query. It can also consult current source/body availability. This path was not exercised as a corroboration workload in the timing above. Ordinary freeze-policy value lookup already uses the Subject membership index; that is not a mandatory global Claim scan in the served path.

**Proposed scope:** Compile query selection into an explicit read plan over indexed accepted facts, and perform fresh evidence/availability checks only for the selected candidate rows and true dependencies. Record both positive and negative/range dependencies for later invalidation. Cache immutable parsed evidence separately from time- or availability-dependent verdicts.

**Contract:** Some queries truly request the whole world. Absence tests and aggregates can change when a previously unseen row is inserted, so a dependency list containing only returned Claims is incomplete. Keep receipt hashes, ordering, time semantics and replay output identical; broad queries should expose their real scope rather than promise constant time.

### 12. Periodic checkpoints and deliberate fallback reconstruction — retain, separate latency

**Where:** `activation.py:261`; `checkpoints.py:275`; `projection_delta.py` unsupported-kind/ownership fallback; instance recovery.

**Current scope:** Every configured checkpoint interval, activation builds a whole member manifest for the checkpoint. Cold recovery and unsupported projection ownership shapes still reconstruct broadly. ExhaustPromotion changes are outside the current local-kind allowlist, and a parent containing fixture envelopes or presentation rows deliberately falls back because row ownership is not artifact-local. Losing CAS or publication failure can trigger recovery. These are not ordinary repeated cache misses but can produce latency spikes; the audit profile is not a tail-latency study.

**Proposed scope:** Reuse already-verified member commitments for checkpoint creation and serialize the captured immutable checkpoint asynchronously if failure/ordering guarantees remain intact. Add explicit delta row-ownership adapters for concrete common artifact kinds that currently fall back, with cold parity tests. Retain deliberate full reconstruction as the correctness/recovery oracle. Do not remove fallback merely to improve a benchmark.

## Cross-cutting duplication: evaluation handoff

`authoring/coordinator.py:724` preflights, then `ProposalService.submit` evaluates again; settlement evaluates a third time. The two submit evaluations total ~1.66 s in the profiled trace, but include the snapshot copies and other items above. Avoid summing this with their costs.

An internal prepared-evaluation result can remove redundant work within submission if it binds the exact parent, candidate delta, actor, timestamp, compiler/law versions, policy/query dependencies, relevant CAS bytes and operational receipt checks. Submission must still perform publication/ref/authorization checks and handle a changed base. Reuse across a human approval delay needs stronger freshness rules than reuse within one operation; fresh approvals and policy/time/source checks remain mandatory as applicable. This is complementary to scope reduction, not a substitute: running an O(N) preparation once still makes every new proposal O(N).

## Recommended implementation sequence

1. **Shared Claim contender index plus isolated candidate overlay.** New concrete high-value target; do not postpone it behind a storage redesign.
2. **Immutable internal evaluation snapshot.** Remove whole-world detachment and gradually replace container copies; build the common snapshot/delta interface here.
3. **Citation group deltas in SQLite.** Scope reads, grouping, validation and replacement together; skipping only one stage leaves the other global costs.
4. **Prepared evaluation handoff and parent-based Git I/O.** Separate reviewable changes sharing an explicit exact-parent/delta contract. Keep public full-tree ingestion as a verified adapter.
5. **Completed projection verification handoff, then normalized explanation binding.** The first can remove duplicate checks with the existing format; the second needs a deliberate derived-schema/export decision.
6. **History/registry indexes and conditional query planning**, based on workloads and scaling counters. Retained-proposal feed/ref deltas follow attachment measurements.
7. **Physical snapshot/commitment redesign only when justified.** The measured backup cost alone does not warrant it yet.

Add operation counters to the focused replay harness: unrelated Claims parsed, whole-tree blobs/bytes read and hashed, maps detached, relation rows read/replaced, explanations rebound, full logical exports, history records traversed, and proposal evidence files opened. Benchmark fixed-D writes while independently growing unrelated artifacts, unrelated history, unrelated proposals, and a genuinely related citation/dependency group. This distinguishes accidental global work from legitimate dependency fan-out. Include per-checkpoint and attached-workspace runs separately. Exact accepted commits, receipts and cold SQLite reconstruction remain parity oracles.

No production code or live project state was changed by this audit.

## Maintainer direction: managed deployment and shardability

Recorded from the maintainer's follow-up on 2026-09-07: centralized management of indexes is a deployment requirement for managed instances. Design for shardability; whole-instance work is a scalability problem as well as a latency problem.

This updates the sequence above: establish the shared derived-state ownership and partition-aware access/update contract before adding the contender index. Implement the first indexes through that contract, initially using the local backend. Do not first build several standalone caches and defer their consolidation to cloud work.

Centralization means one registry/lifecycle owner for index definitions, dependency rules, update/rebuild protocols, versions, budgets and observability. It does not require all index contents to be resident in one process. The following are proposed engineering consequences of the maintainer's requirement, not an already implemented distributed architecture:

- Represent an accepted snapshot as a coordinate-bound read handle with lazy, scoped access. Callers should not require a materialized whole-instance dictionary. Candidate state is a bounded overlay over that handle.
- Give derived records explicit ownership and stable logical keys. Keep logical partition identity separate from physical placement so partitions can move. Different access paths may require different secondary partitions: artifact identity, Claim slot and citation group are not interchangeable shard keys. Do not prematurely select one universal physical shard key.
- Supply affected members, old/new relationship keys and dependency invalidations to each index updater from a verified transition. Support cursor-based replay, idempotent application and per-partition rebuild. Track cross-partition and negative/range dependencies explicitly; successful reads alone do not describe complete query dependencies.
- Bind each read to an accepted coordinate and a known compiler/index version. Track partition progress and refuse, wait or reconstruct when required partitions cannot serve that coordinate. Never combine arbitrary latest partition contents and present them as one accepted snapshot.
- Preserve the signed ledger's authority and current acceptance CAS. Partitioning derived storage and evaluation does not itself decentralize accepted-head ordering. Cross-shard acceptance, independent ledger heads and distributed transactions require a separate decision. First separate parallelizable preparation from the existing final acceptance boundary.
- Treat warmup, memory limits/eviction, replay lag, bounded work queues, rebuild after loss, rolling index-version upgrades and recovery as shared lifecycle concerns. The local backend should exercise the same contracts without requiring a distributed service for OSS.
- Require explicit execution plans for legitimate broad operations. Measure touched partitions, transferred bytes and affected dependency groups; route global audit/rebuild work separately from small-write paths where semantics permit it. Include skewed/hot relationship groups in scaling tests; hashing rows does not eliminate real fan-out.

These requirements strengthen the rationale for delta scope and immutable ownership. They do not authorize changing stored digest rules, weakening verification or treating derived partition state as independent authority. This direction is recorded in repository documentation; it has not been activated into the live project-state instance.
