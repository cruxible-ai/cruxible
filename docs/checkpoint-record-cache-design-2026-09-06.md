# Checkpoint record reuse: bounded design assessment

**Recommendation: defer full-record caching in this pass.** It can be made safe, but its net benefit and retained-memory cost have not been measured. The reported 4.63 s is parsing 26 historical records; a cache cannot remove its own deep-copy cost, and cold daemon startup would still parse the records. No profiling, parsing-size walk, implementation, or live mutation was performed for this assessment, preserving the other worker's benchmark window.

## What the code requires

`checkpoints._prefix_records` reads each exact historical path from the newly read signed checkpoint tree, fully parses it, checks its sequence, then checks that no extra changeset paths exist. `parse_change_set_record` dispatches V1/V2/V3, verifies their existing internal candidate/changeset/closure rules, and checks exact canonical rendering. `_rederive_prefix` needs approvals, candidate scope, version-specific root inputs and principal changes; importantly, it retains **the full record** in each `CheckpointGeneration`. Recovery carries that record into `RecoveredGeneration`, and history consumers inspect it later.

Thus the compact review-summary pattern does not fit this seam without changing the recovered-history interface. Existing accepted history already owns parsed full records. Their Pydantic models are frozen only at attribute assignment: nested `law_digests` dictionaries and law-result payload containers remain mutable. Sharing these caller-visible models directly through a cache would let a consumer poison later recovery.

The existing prepared-lowering cache uses deep copies but bounds accounted serialized/tree bytes, not Python heap. The compact candidate-review cache avoids this issue by retaining only immutable strings and exact-byte proofs; its 8 MiB accounting similarly is not a claim about whole interpreter memory. Neither pattern alone supplies a safe low-memory full-record cache.

## Smallest robust implementation if measurements justify it

1. Keep the optimization local to checkpoint parsing, leaving the frozen `parse_change_set_record` validator as the miss oracle. Use a process-local bounded map keyed by **exact `(path, content_bytes)`**, or a digest-indexed map that also checks exact raw byte equality. Input bytes must still come from the current fresh Git tree read. Do not use mtime, OID existence, sequence alone, or mutable recovered-record identity as proof.
2. On miss, call the original full parser. Retain a private parsed model only after successful validation. Never expose this retained model: return a deep copy on both the admitted miss and every hit. For a record too large to admit, return the ordinary uncached parsed result. Thread synchronization protects cache bookkeeping and entry lifetime; computation/copying can happen outside the lock after obtaining a private immutable-by-convention entry reference.
3. Leave sequence/path correspondence, complete prefix inventory, manifest/Merkle checks, principal reconstruction, signature verification, and version-specific semantic/generation root derivation outside the cache and active on every load. Changes to any raw record bytes take the full parser/refusal path. Cache eviction or deletion merely restores ordinary parsing.
4. Use both an entry cap and a measured retained-object graph cap, not a multiplier of serialized length. A candidate configuration is **256 entries and 64 MiB of recursively accounted reachable Python object storage**, including retained raw keys, model dictionaries, nested containers, tuples, Pydantic field/private metadata and cache bookkeeping. Count shared objects once within an entry, but conservatively count shared objects again across entries. Skip objects/types the accounting cannot safely cover. This is an explicit retained-object accounting budget, **not an RSS guarantee**: allocator overhead, the active recovered history and transient returned deep copies remain additional memory. If a true hard process-memory ceiling is required, this full-model design does not provide it.
5. Test capacity thrashing explicitly. A sequential full-history scan with ordinary LRU can miss every record indefinitely when the working set barely exceeds capacity. Measure whether the entire 26-record prefix fits before choosing this approach; do not quietly increase a global cache until it holds another copy of every instance's history.

## Required evidence before implementation is worthwhile

- Cold parse plus cache admission cost, warm lookup plus deep-copy cost, and uncached parse baseline on the same copied prefix.
- Retained object-graph bytes for the complete prefix, peak temporary allocation during refresh, and behavior just below/above capacity.
- Exact recovered prefix records, coordinates, root chains and final accepted output equality across mixed historical record versions.
- Mutation of a returned nested law-result/digest container cannot affect a subsequent hit. Changed bytes, wrong-path sequence, missing/extra paths and malformed canonical bodies preserve their existing refusals and ordering. Eviction and oversize entries recover through the original validator.

## Better longer-term direction

Reuse a verified prefix with a fresh byte proof while separating private immutable verification state from caller-visible mutable history models. This avoids permanently retaining two complete model graphs, but requires an intentional ownership/interface design rather than casually caching the current `RecoveredGeneration.record` objects. A compact prefix summary alone is insufficient while downstream consumers still require full records.

There is also a smaller independent candidate: `parse_change_set_record` currently constructs `TypeAdapter(ChangeSetRecordAnyVersion)` on every invocation. Reusing only that compiled adapter avoids full-record retention and does not bypass validation. Its contribution to the 4.63 s has not been isolated; it should be measured before being prioritized, and cannot be presented as eliminating the full parsing cost.
