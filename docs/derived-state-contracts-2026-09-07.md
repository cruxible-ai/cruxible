# Batch 1: derived-state contracts

Status: proposed design, grounded in Core `9c0051a5ca9a5ed60e173fe901e3a94d52fbc37e`.
This batch defines contracts and implementation boundaries; it does not implement
the component, migrate stored formats, or claim new performance results. The
maintainer authorized this design and the direction toward changeset-scoped work,
central lifecycle ownership, and shardability. Recommendations below are not
additional maintainer rulings.

## Outcome and scope

Introduce an internal `PlaybillInstance.derived` owner. It manages immutable
accepted snapshots, isolated candidate overlays, and views over operational
evidence. Index implementations register with this owner instead of acquiring
independent lifecycle, version, and budget rules throughout the service.

Ordinary warm work should scale with changed members, actually affected
dependency/relation groups, and changed storage paths, plus bounded lookup costs.
The size of unrelated accepted state, history, or retained proposals should not
force a complete traversal. Global queries, genuine dependency fan-out, cold
bootstrap, and explicit recovery may legitimately remain broad and must identify
that scope in instrumentation.

This is a common ownership/access/update contract, not one universal index
algorithm or a new distributed database. OSS starts with an in-process local
backend. Managed placement can change independently of logical record identity.
The current single accepted-head compare-and-set remains the ordering boundary.

Use **derived index/state** for machine acceleration and **authored view** for
agent-written Markdown projections in this design. Existing public names remain
unchanged. An authored view can consume this component without being its storage
model or requiring a renderer in Core.

| Concern | Current behavior | Contract after this work is implemented |
|---|---|---|
| Ownership | Instance caches, service memos, process-global caches, and persistent indexes have separate budgets and lifecycle rules | One instance owner and registered adapters; algorithms stay in their domains |
| Accepted reads | Full tree dictionaries and detached whole evaluation states cross boundaries | Leased coordinate-bound handles with scoped lazy reads and private immutable rows |
| Candidate computation | Whole candidate dictionaries/copies, sometimes displacing the accepted cache | Parent snapshot plus isolated staged edits, sealed as an immutable prospective delta |
| Index advancement | Changeset compilation surrounded by broad inventory/copy/rebinding work | Verified transition plus index-specific old/new ownership and dependency plans |
| Dependency granularity | Exact governed artifact pins coexist with implicit cache inputs | Preserve governed pins; explicitly register exact/facet/group/absence/history observations for reuse |
| Serving | One immutable SQLite build and atomic serving pointer | Preserve this trust boundary; permit versioned partition-root manifests without mixed-coordinate reads |
| Operational evidence | Some rooted/partitioned stores, some fresh full file inventories | Source-specific revision handles; keep fresh checks until each writer/reconciliation contract supports scoped reads |
| Deployment | Per-cache limits and process-local assumptions | Shared inventory, aggregate budgets, replay/lag metrics, bounded builds, eviction and rolling rebuild rules |

## 1. Authority and retained source data

The signed generation ledger and accepted Git tree continue to determine governed
state. A derivative never authorizes acceptance, changes a pin, substitutes an
approval, or becomes a second writer of accepted truth.

Rebuildability means **from the declared retained sources**, not that every byte
is present inside the accepted Git tree. Accepted artifacts can reference CAS
bodies and producer receipts. Proposal evidence, authoring-intent streams,
Procedure journals, review observations, and attestation chains also contain
durable observations which accepted Git alone cannot reconstruct. Indexes over
those sources are disposable; their source stores are not disposable caches.
Preserve current retention, verification, backup and recovery contracts. This
batch does not authorize source deletion or change storage durability.

Three namespaces share lifecycle machinery but not source authority:

| Namespace | Binding | Example | Rebuild source |
|---|---|---|---|
| Accepted | Verified accepted coordinate and derivative definition | Claim slot membership, dependency edges, static compiled facts | Verified ledger/tree plus declared retained CAS/evidence inputs |
| Candidate | Parent accepted binding + immutable candidate revision/delta | Staged Claim memberships, candidate dependency state | Parent plus exact retained intent/candidate inputs |
| Operational | Source-store identity + relevant source revisions | Candidate approvals, producer receipts, attestation lookup | Durable source store, never inferred from accepted state |

Mixed computations record all applicable bindings. There is no universal
operational epoch to stamp on every row whenever any evidence changes.

## 2. Identity and ownership contracts

The following are internal design types, not new SDK payloads. Use discriminated
records and opaque service-created handles; a caller-constructed dataclass or a
serialized `verified: true` is not verification authority.

| Type | Required content and meaning |
|---|---|
| `SourceNamespace` | Instance identity plus verified ledger/trust incarnation; excludes host path and physical placement |
| `AcceptedBinding` | Namespace, full verified accepted coordinate (Git object format/OID, semantic root, generation root, compiler), artifact codec; not only semantic root |
| `IndexDefinition` | Stable name, namespace, schema/build semantics version, decoder/compiler inputs, partition-rule version, source contract, dependencies, supported deltas, validation, retention and accounting rules |
| `LogicalPartition` | Index definition/version + canonical logical key/range; storage location is separate |
| `SnapshotHandle` | Leased immutable root at an `AcceptedBinding`, with lazy access to the required registered indexes |
| `CandidateBuilder` | Mutable private staging state over a parent snapshot; never enters the accepted registry |
| `SealedCandidate` | Immutable parent binding + complete edits + candidate revision/content commitments; no aliases back to the builder |
| `EvidenceView` | Source identity, decoder semantics and captured relevant partition/stream revisions; consistency mode explicitly declared |
| `ReadSet` | Versioned observations of all consequential inputs, including membership/absence and non-state inputs; completeness flag |
| `VerifiedTransition` | Exact verified parent and successor bindings, signed changeset correspondence and exact physical tree changes; minted by existing verification paths |
| `IndexUpdatePlan` | Transition identity, index version, affected owners/groups/partitions, dependencies and fallback reason; deterministic and rebuildable |

Do not put the enclosing generation into every content key. Bind an immutable
payload to a snapshot through the snapshot root. The same payload can then be
referenced by successive snapshots after the transition proves it unchanged.
Candidate keys include the parent and candidate revision; two sibling proposals
cannot alias merely because their edited path names match.

Internal retained state consists of canonical bytes or genuinely immutable
scalar/tuple records in persistent keyed stores. Frozen Pydantic models can
contain mutable dictionaries, and freezing the outer map does not freeze nested
identity objects. Existing laws can receive detached models **for requested
rows**, with operation-local memoization. Returned public values never alias
retained state. A compatibility mapping that iterates all rows records broad
work and is a migration adapter, not evidence of scoped performance.

Persistent maps must update changed keys and reverse buckets without copying
the complete map. Use a structurally shared keyed tree/trie behind the access
contract; select its concrete implementation against batch 2 measurements.
Do not use an unbounded chain of dictionary overlays. Bound mutable candidate
size, retained roots, and any compaction work; an ordinary write must not
occasionally flatten every historical overlay without reporting that cost.

Illustrative access surface:

```python
with instance.derived.accepted(verified_binding) as accepted:
    reads = accepted.read_session(context)
    body = reads.artifact_bytes(path)                 # bytes or explicit absence
    paths = reads.claim_paths(subject, predicate)     # includes empty-group read
    sources = reads.dependency_sources(identity)      # indexed reverse membership
    draft = accepted.fork()
    draft.replace(path, canonical_bytes)              # domain lowering validates
    staged = draft.read_session(context)
    sealed = draft.seal()                            # later writes cannot alter it
```

The raw edit builder remains private to validated authoring/ingress adapters.
It is not a public write API, and staging does not assert that a candidate is
lawful. Invalid input must still reach the same deterministic refusal boundary.
Each builder read session captures one immutable staging revision. Subsequent
member writes create a new revision/session; operation-local parsed/group memos
cannot leak across revisions. Preserve `MEMBER_STAGING_ORDER` and member order
within stages. Claim contender lookup uses Subject + predicate, returns paths in
UTF-8 order, applies the existing live-lifecycle rule and no new effective-time
filter, and leaves qualifier/disposition decisions to the existing lowering law.
All generated edits update both old/new groups before the next stage. Singleton
and succession helpers use the same index. ClaimType succession retains separate
predecessor-vocabulary and successor-vocabulary views.
Separate semantic members from history, candidate cards, and physical tree
inventory; equal semantic roots do not imply equal complete Git trees.
Preserve UTF-8 canonical iteration at commitment/export boundaries; changing
map implementation cannot change the order hashed by a frozen manifest format.
Do not sort the whole world during every update merely to repair arbitrary map
iteration. Where a legacy flat export requires a full traversal, retain and
report that explicit export cost.

## 3. One changeset-based transition, several derived plans

Existing changesets already record before/after artifact digests, dispositions,
closure roles, candidate correspondence, law evidence and approvals. They do not
need to contain SQLite row mutations, shard locations, or cached query results.

Before acceptance there is no signed accepted changeset. The daemon constructs
a **prospective delta** from verified parent state and the complete candidate
edits. It includes all generated succession/retirement/disposition edits, not
only the user's initial authoring members. Separate these representations:

1. Raw member edits, keyed by canonical path, with exact old/new bytes or blob
   references and file/content commitments. Explicit absence represents creates
   or removals. Parsed identity, artifact digest and law disposition are validated
   enrichment when available, not prerequisites for constructing a draft delta.
   Malformed input and semantic paths without artifact envelopes must retain
   their normal parsing/refusal phase and ordering.
2. Physical tree edits, including daemon-owned candidate cards and the eventual
   changeset file, modes, path additions/removals and resource accounting.
3. Derived index plans, computed from those edits and indexed old/new groups.

Only a verified daemon path may attest delta completeness. Public full-tree
submissions still need ingress verification against their parent. A full scan
here is not a loophole for an SDK caller to assert an incomplete cheap delta.
Normalize paths, check ancestor/file collisions, object formats/modes and receive
limits, including collisions introduced by the combined edit set.

After settlement, reconcile prospective member commitments and physical edits
with the actual Git successor and verified changeset. The changeset member scope
is the governed closure, while the index update scope can include unchanged
artifacts whose assessments or relation rows are affected. Such derived changes
do not manufacture governed changeset members.

Each index supplies `plan(transition, parent)`, `apply(plan, staging)`,
`validate(staging)`, and `rebuild(source, scope)` behavior. Plans specify row
ownership and both **old and new** grouping keys. Unsupported ownership selects
an explicit full/partition rebuild; an optimization must not silently omit it.

For citations, a Claim moving capture/source/span groups removes its old use,
adds its new use, and recomputes conflicts in the union of affected groups.
Unchanged Claims in those groups can gain or lose derived conflict rows. A
CaptureContract change has its own owner/group effects. Retired witnesses,
ordering and removals must match the cold builder. The existing local-kind
fallback for promotions/fixtures/presentation stays until an adapter proves
their row ownership.

Prefer deriving these plans from the existing record plus indexed parent state
over expanding the signed wire format. If a later worker handoff needs a
serialized transition hint, it must be rebound/reverified against those sources;
it is not a replacement ledger.

## 4. Dependency granularity: two separate contracts

**Governed pins** specify the revision a stored artifact commits to, and the
closure/law obligations when it changes. **Dependency observations** specify
what a particular computation consumed and when its output can be reused.
The latter cannot silently relax the former.
Delta completeness, read-set completeness and successful law evaluation are
independent properties; none proves the other two.

| Observation selector | Token covers | Required invalidation behavior |
|---|---|---|
| Exact artifact | Identity/path resolution and exact artifact revision or explicit absence | Revision, rebinding, insertion or removal |
| Identity presence | Canonical identity and presence; lifecycle is a separately named facet if consumed | Insertion/removal/rebinding; never implicitly ignore lifecycle |
| Registered facet | Named/versioned extractor over canonical inputs | Any input changing that extractor's output or its semantics |
| Indexed membership/range | Canonical key/range, membership/order/truncation semantics, including empty result | Insertion, removal, old/new group move, visibility or sort changes as consumed |
| Historical object | Immutable referenced revision/content and verifier semantics | Does not follow current head; current availability/integrity is a separate check |
| Operational/external input | Exact source revision or explicitly fresh observation | Source-specific evidence/authorization/config/availability changes |
| Time interval | Evaluation instant and proven validity interval/boundaries | Expiry/effective-time boundary; no arbitrary TTL as correctness proof |

`ReadSet` records the source view, selector, observed token, extractor/computation
version, completeness, and time validity where applicable. Query joins inherit
both membership and destination dependencies; cached subcomputations contribute
their complete observations or a correctly maintained output dependency token.
Observation discovery and reverse-subscription maintenance are part of the index
contract: inserts, rebindings and old/new group moves must discover newly relevant
dependencies as well as invalidate existing ones.

Untracked reads make a computation non-reusable or explicitly broad. Reading
only returned rows is insufficient: empty groups, hidden candidates, cardinality
conflicts, ordering, top-k boundaries and visibility changes can alter results.
The five query backend primitives are a useful observation seam, but they do not
capture external CAS, authority, provider, receipt or time inputs automatically.
Reuse rules apply to refusals as well as successful results. Transient source
failures or incomplete observations must not become cached durable absence.

The first implementation keeps **same accepted coordinate** as the eligibility
rule for prepared evaluation reuse. It still benefits from incremental index
advancement across generations. Broader cross-coordinate computation reuse is
allowed only after complete observations and their invalidation rules exist.

### Pin audit and proposed scope decision

| Existing commitment | Finding | Batch 1 disposition |
|---|---|---|
| `ArtifactPin(role, target, artifact_digest)` | Always exact revision; closure resolves equality and live reverse dependents require dispositions | Preserve existing formats and law meaning |
| Claim Subject/object Subject pins | Exact backing digest and pin checks apply even when `referent_sensitivity='identity'` | Record as potential versioned semantics change, not cache work |
| Claim statement vs artifact digest | Statement identity is narrower; artifact digest also commits backing, pins and lifecycle | Use statement/membership facets only for computations that actually consume them |
| ClaimType/provider/policy dependencies | Type constraints and executable meaning influence validation/results | Keep exact semantics unless an audited consuming law defines a narrower contract |
| Procedure terminal dependencies | Already follow item/dataflow/branch dependencies; accepted-state token commits the admitted query input | Preserve frozen provenance; reuse computation separately from generating a new bound receipt |
| Query result digest | Includes read coordinate/time and result semantics | Reuse eligible material, then construct fresh required result/receipt binding |

Subject shells have **no arbitrary properties or metadata**: kind/id, pins and
lifecycle are the material fields. Ordinary property Claims do not rewrite the
Subject shell. Therefore Subject pin narrowing is not the explanation for every
small-write cascade. Pin/lifecycle changes can still cause real fan-out today.

Recommendation: implement internal precision first with unchanged governed pins.
Do not make a broad pin-format migration a prerequisite for eliminating map
copies, whole-tree reads or relation rebuilds. Carry a separate, explicit decision
for a narrowly versioned Subject referent contract: stable identity/kind plus
required lifecycle/eligibility semantics, while retaining observed historical
shell revision as provenance and exact mode where required. Before adopting it,
specify whether changes to the Subject's own pins alter referent eligibility,
how mixed old/new Claim populations behave, and how closure dispatches by edge
semantics. Old exact dependents must retain their old obligations. Mere existence
is not enough. No stored pin/digest/receipt is reinterpreted or mass-rewritten.

This addresses coarseness in batch 1: we identify which narrowing is safe now
and the exact semantic boundary that needs a ruling. It is not deferred as an
unspecified indexing problem to batch 7.

## 5. Self-sufficient artifacts and stable bindings

Separate four concerns instead of refreshing unrelated artifact fields:

| Concern | Changes when | Storage/read rule |
|---|---|---|
| Intrinsic content commitment | The artifact's committed content changes | Preserve existing versioned digest rules |
| Historical provenance | New evidence/revision is explicitly authored | Existing used/accepted coordinates stay historical |
| Enclosing snapshot binding | The caller selects a different accepted snapshot | Resolve via handle/root or read-time binding; do not copy into every static row |
| Current assessment | Actual policy/evidence/lifecycle/time dependencies change | Recompute affected assessment, without rewriting artifact history |

Self-sufficiency means local identity/content and explicit dependencies. It does
not mean a Claim independently proves its own current truth or eligibility.
Authorization, existence, cardinality and evidence can require related state.
Do not embed every neighbor's changing assessment into an artifact digest.

Explanation normalization is a derived-schema change: keep historical coordinates
inside provenance intact and synthesize current enclosing coordinates at read or
export time. The frozen existing logical export/digest must still reproduce.
If producing that old export hashes every row, that cost remains global until a
separate versioned export/commitment decision. Partitioning alone cannot remove
that lower bound, SQLite backup cost, or Git's whole-index serialization.

## 6. Partition consistency and publication

Partition by each index's lookup/ownership needs: artifact identity, Claim
Subject/predicate group, citation capture/source/span group, or operational
stream/candidate. One universal shard key would hide cross-group work. Logical
keys are independent of host paths and placement. Dense groups remain dense;
partitioning does not make genuine fan-out disappear.

A read captures one accepted coordinate. All dynamically discovered partitions
must serve that coordinate using the same snapshot root. An unchanged physical
partition can be shared from an older build if verified transition coverage
proves its contents valid at the selected coordinate. Do not test only that a
partition cursor is greater than the requested sequence: it may contain newer
values and no longer serve the requested snapshot.

Maintain a persistent partition directory/root with changed entries, not a flat
manifest rewritten for every partition on every generation. A coordinator replay
frontier proves it processed the exact transition sequence and routed all
affected keys; untouched partitions do not need no-op cursor writes each time.
Root publication binds their carried references to the new coordinate. Scoped
rebuild requires trustworthy routing/membership metadata; if that is also lost,
initial source discovery can be global. Do not claim O(partition) recovery from
an empty machine with no way to locate the partition's source objects.

Progress belongs to each index definition/version and its declared coverage. Its
published immutable root commits the exact applied frontier/source revision;
alternatively index data and progress commit in the same atomic transaction.
Advance it only after all required routed updates are durable and validated.
Recover progress from that validated root, never from a separately advanced
worker cursor. New/lazy adapters and new versions cannot inherit another index's
frontier as proof that their own data was materialized. Shared routing progress
and an adapter's applied progress are distinct.

For multi-partition updates, stage immutable new pieces and publish one root
only after all pieces required by that root are durable and validated. Locally,
reuse the existing assembler/serving protocol. Future remote workers can stage
parts, but the coordinator validates their correspondence; neither worker output
nor a cache proof authorizes an accepted generation.

| Interruption or conflict | Required behavior |
|---|---|
| Build fails before acceptance | No serving/root change; keep parent readable |
| Same transition retried | Exact identity/content match is idempotent; conflicting content is an integrity refusal |
| Wrong parent, missing delta, wrong version | Catch up/rebuild or refuse; never apply onto a convenient latest root |
| One partition staged, another missing | New root is unavailable; no mixed snapshot |
| Accepted-head CAS loses | Candidate remains non-serving; reclaim only its unleased derivative artifacts |
| Ledger advances, publication crashes | Recover authority using existing protocol, finish/rebuild serving; do not roll back accepted history to fit cache |
| Required index missing/lagging | Rebuild/catch up under a bounded request budget or return an explicit unavailable result; never silently return stale values as current |
| Source corrupt/missing | Preserve source-specific refusal/availability behavior; do not rebuild an empty success |
| Reader holds previous snapshot during eviction/upgrade | Lease preserves that root and required pieces until release |

Authority CAS, generation-note, serving and witness ordering remain as currently
implemented. This batch does not move required prebuild checks after CAS or
promise asynchronous acceptance. Background warmup applies to optional caches
and explicitly deferred work; changing the acceptance/availability contract
would be a separate product decision.

## 7. Operational evidence and prepared computation

Use existing source revisions where they are trustworthy: authoring stream
revisions, review partition heads, and attestation published roots/partition
heads. A mixed accepted/evidence result names its accepted coordinate and the
relevant evidence view. If an operation requires a coherent multi-store cut,
capture under applicable locks or capture/validate revisions and retry; otherwise
label it explicitly as observed, not an atomic global snapshot.
Revision tokens include source incarnation and non-reused revision identity, so
store restoration/recreation cannot make changed data match an old token (ABA).

Proposal and authoring file caches currently detect arbitrary changed/deleted
files through fresh byte reads. A new cursor alone cannot replace those checks.
First migrate their current behavior under the owner. A later scoped protocol
must atomically publish source mutations and source revisions, account for
alternate writers/imports, and retain reconciliation/corruption checks. Preserve
current semantics until that contract is implemented; never pretend mtime or a
bare cursor proves unchanged bytes.

CAS content identity, local availability, receipt validity, active actor authority,
and configuration such as Git commit encoding are distinct inputs. An immutable
content digest does not prove a body is still present, or that an approval is
currently acceptable. Keep fresh checks where a sound revision mechanism is
absent. Build on the current exact-byte and file-identity validation behavior.

Prepared evaluation is an opaque same-call handoff bound to parent, sealed delta,
intent revision/minted identities, authenticated actor/operation, timestamp,
receive limits, compiler/laws, and complete relevant observations. Submission
can reuse its pure result while checking current writable state, authority,
accepted base, ref/admission bindings and mutable provenance as required. If
anything invalidates it, recompute or follow the existing conflict/refusal path.
SDK preflight output is not such a capability. Approval-delayed acceptance reuse
is a later, stronger contract. Matching inputs never authorize replaying an
effectful Procedure without its effect/idempotency rules.

## 8. Central registry and operational limits

`DerivedState` owns registration, leases, scheduling, accounting, status and
shutdown. Source adapters keep source-specific verification and domain algorithms.
No new standalone cache should bypass the owner while migration proceeds.

| Adapter family | Existing seam | Migration scope |
|---|---|---|
| Accepted artifacts/evaluation | `EvaluationStateCache`, `ClaimCompilationCache`, tree memo, Claim/dependency/Merkle maps | First owner/immutable snapshot and Claim contender work |
| Accepted SQLite | Assembler, activation, serving, `_VERIFIED_PIECES` | Register lifecycle first, then adapt scoped row updates/verification reuse |
| History and floors | Instance history/floor memos | Register source/version/budget dependencies; narrow algorithms in later batches |
| Prepared authoring | `prepared_lowering` weak-key cache | Move under candidate ownership, compact delta retention |
| Proposal review | `ProposalNoteCache` and source store | Preserve fresh byte/alias/Git config checks; add source revision protocol later |
| Authoring history | Global history/fingerprint memos | Intent/source-scoped registry adapters; no broad source-check removal by fiat |
| Assessments/receipts | Verdict memo, attestation caches, producer receipt resolver | Explicit evidence/time dependencies and source-specific recovery |
| Recovery checkpoints | Existing verified-prefix checkpoint protocol | Register accounting/status; retain trust semantics |

Account aggregate per-instance retained payload bytes, estimated heap where
available, entry counts, open handles, candidate size, build concurrency and
queue length. Distinguish byte-accounting estimates from measured RSS. Share
backing ownership accounting across roots so old/new snapshots do not each
charge or free the same allocation incorrectly. Eviction removes acceleration,
not source data or live readers; backpressure bounds new work when leases pin
too much memory. Oversize fallback must not truncate results.
Keep foreground lease/reference release bounded. Potentially broad reclamation
of unreachable shared nodes runs in bounded background work, rather than hiding
a whole-map traversal in ordinary acceptance or handle closure.

Warm the known common accepted indexes once during admission/startup or a bounded
background warmup; lazy misses use the same owner and single-flight build key.
Prioritize required foreground work; avoid holding a global owner lock during
law execution, file I/O or expensive builds. Schedule bounded work without
unbounded per-instance threads. Future fleet budgeting can sit above instance
budgets, without becoming an OSS service dependency.

Status exposes source/target coordinates, definition versions, replay frontier,
lag, retained bytes/leases, queued work, rebuild/fallback reason and phase times.
Do not attach unbounded Claim IDs to metrics labels. Count touched artifacts,
groups/partitions, parsed/copied rows, Git/CAS bytes, history/evidence records,
full exports, and whole-scope fallbacks in request diagnostics/benchmarks.

Upgrade derivative versions alongside old versions; rebuild and verify the new
root, atomically switch routing, retain leased old builds, then reclaim. Do not
upgrade a stored digest by recomputing it under new semantics. Process-local
verification capabilities cannot be persisted or sent to another worker as
portable trust. Remote/restarted components must rebind through their source
verification contracts.
Current-serving routing switches only after the new version catches up and binds
to the intended accepted/evidence coordinate under publication checks. A build
at an older coordinate can serve that history, not silently replace current.

## 9. Implementation batches and evidence gates

| Batch | Concrete work | Scope/performance evidence required |
|---|---|---|
| 1 (this document) | Ownership, bindings, delta and dependency contracts; pin semantics boundary; migration and failure rules | Source audit and independent adversarial design review; no speedup claim |
| 2a | Owner/registry/leases/budget instrumentation; immutable scoped rows and persistent map seam | Mutation poisoning and lease/concurrency checks; no naked removal of `deepcopy` |
| 2b | Accepted Claim contender index across singleton, changeset and succession paths; isolated candidate overlays | Warm fresh drafts parse only matched/changed Claims; exact candidate/diagnostic ordering parity |
| 2c | Persistent member/dependency/reverse/Merkle updates; complete prospective delta handoff | Fixed-D edits stop copying unrelated maps/buckets; byte-identical roots and closure proofs |
| 3 | Citation owner/group delta adapters | Cold/incremental row equality; changed/retired/moved/shared/span groups, contract changes |
| 4 | Same-call prepared evaluation handoff | Equal candidate/diagnostics, changed authority/evidence invalidation, separate approval-delay exclusion |
| 5 | Parent-based Git/path/inventory operations | Exact tree/commit/receipt parity and collisions/resource checks; report any residual Git global serialization |
| 6 | Completed index-verification handoff; normalized explanation binding | Corruption/replacement checks; frozen exports still reproduce; label format-dependent residual work |
| 7 | Registry/history/query consumers adopt scoped APIs and dependency observations | Unrelated history/population scaling and negative/range/join/time invalidation |
| 8 | Proposal/review/evidence scope via source revisions | Alternate writer/corruption/interrupt parity before inventory checks can be removed |

Do not dispatch 2a–2c as three independent foundations. Root owns the shared
contracts and persistent-state boundary, then gives subagents separate consumer
modules with exact interfaces. Citation work can proceed independently once
transition/ownership contracts settle. Use independent review of state isolation
and failure behavior before integration. Separate worktrees/commits, focused named
checks, no tests in the canonical checkout and no unrequested full suite/goldens.

The cold builder is the reference oracle, not dead code to delete. Test both
success bytes and refusal ordering. Essential scenarios:

- Sibling overlays modify the same group; neither sees the other's staged rows.
- Old snapshot survives accepted advancement, eviction pressure and an index upgrade.
- Empty group gains a Claim; reverse dependency insertion and group moves are observed.
- Within one draft, an empty group is read, a Claim is staged, and the next member
  sees the new contender and requires the same disposition as cold lowering.
- A non-returned Claim becomes visible or changes order/conflict/truncation.
- ClaimType succession preserves predecessor and successor vocabulary views and staging order.
- Scope omits a physical change, contains conflicting paths, or uses the wrong parent.
- Multi-partition apply retries/crashes and post-CAS publication recovery.
- Evidence mutates at unchanged accepted head; body/receipt is absent or corrupt.
- Unrelated state advances while historical provenance bytes remain unchanged.
- Old and new derivative versions yield the same frozen exported semantics.

Benchmark fixed changed-member counts while independently growing unrelated
Claims, history and proposals, then grow a truly related hot group. Compare
same-coordinate repeats and warm **fresh** drafts after accepted advancement;
measure cold bootstrap separately. Include installed SDK/HTTP and Git attachment
timings, not just in-process services. No claim of a flat end-to-end scaling curve
until remaining physical full-copy/export operations are removed or isolated.

Batch 0's installed attached baseline remains submit 9.011–9.213 s and accept
5.539–5.644 s; first connect 14.633–14.933 s in its controlled private copies.
These are small paired samples with the documented workload, not p95 promises.
See [the integration report](reviews/batch0-performance-integration-2026-09-07.md)
and [scope audit](reviews/world-scope-performance-audit-2026-09-07.md).

## 10. Source map and design limits

The source audit was split across dependency semantics, immutable ownership,
and lifecycle/evidence, then integrated against this exact code baseline:

- `playbill/instance.py`: current owner/memos, proposal/evaluation injection, accepted binding and activation.
- `playbill/evaluation_state_cache.py`: exact-byte cache with whole semantic projection/detachment.
- `playbill/proposals.py`: `EvaluatedTreeState`, member/state advancement and evaluation consumers.
- `playbill/closure.py`: exact pin resolution, reverse closure and broad map copies.
- `playbill/authoring/lowering.py`: contender scan, staging and succession views.
- `playbill/authoring/prepared_lowering.py`: input eligibility, source checks and detached cache results.
- `playbill/projection_delta.py`: current verified transition and explicit unsupported ownership fallback.
- `playbill/settlement.py`, `activation.py`, `serving.py`: changeset correspondence and publication boundary.
- `playbill/citation_relations.py`, `storage/playbill_projection.py`: group scope, explanation binding and frozen export costs.
- Client `contracts/artifacts.py`, `claims.py`, `subjects.py`, `candidates.py`, `merkle.py`: stored pin/digest/law boundaries.
- `playbill/query/backends.py`, `query/engine.py`, `query/impact.py`: observation seam, result binding and historical/current distinction.
- `playbill/procedures/input_planes.py`, `execution.py`, `terminal_dependencies.py`: admitted input and terminal provenance.
- `playbill/proposal_note_cache.py`, `authoring/store.py`, `claim_attestation_store.py`, `producer_receipts.py`, `service/playbill_verdict_memo.py`: heterogeneous source/lifecycle contracts.

Paths above are relative to `src/cruxible_core/` unless labelled Client (under
`packages/cruxible-client/src/cruxible_client/`). No new storage engine, distributed
acceptance protocol, universal effect cache, arbitrary facet selector language,
or changed governed pin format is specified as an implemented feature here.
