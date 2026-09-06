# Code Review

## Verdict

Approved

The final per-event reuse preserves the frozen payload, fingerprint, member-identity, and event commitments while avoiding repeated normalization and member hashing. An existing-model/context reuse defect found during review has been corrected by consuming proof slots before event rendering or verification; the regression now exercises ordinary nested dictionary mutation. No remaining unsafe reuse was found in the production fresh-byte decode path.

## Manual Review Priority

- Priority: P1
- Reason: This optimization changes how integrity-validation intermediates are reused across nested validators.
- Suggested Human Review Focus: Context ownership and consumption; payload rationale versus fingerprint preimages; exact model-identity guards; canonical-byte comparison and historical chain checks.

## Scope Reviewed

- Changed files: `packages/cruxible-client/src/cruxible_client/contracts/authoring/models.py`, `src/cruxible_core/playbill/authoring/store.py`, and `tests/test_playbill/test_authoring_event_decode_reuse.py`, uncommitted against `c1a521bd83c8222c7c4e8130a9f2fb6751ee9561` on `codex/intent-cold-validation` in `/private/tmp/playbill-intent-cold-validation`.
- Untracked files: `docs/benchmarks/intent-cold-lookup.py` belongs to the parent's separate benchmark scope and was excluded.
- Tests examined: Decode reuse, binding reuse, and source-presence tests; relevant store validation and memo consumers. With explicit permission, the reviewer replaced the existing-model mutation fixture with ordinary nested dictionary mutation and added absent/present Unicode rationale parity cases in the decode test file only.
- Commands run: `git status --short`, scoped `git diff`, `git diff --check`, `rg`/`sed` source and test inspection, and a bounded Python correctness probe reproducing the pre-fix context-reuse defect. No benchmark, broad suite, golden corpus, or canonical-checkout test was run by the reviewer; parent validation was coordinated to avoid timing contention.

## Findings

No findings.

## Complexity Assessment

The work remains linear in each event's payload and members. Member identities are computed once during member validation, then reused during intent binding; the already normalized payload is reused by the event envelope check. The event is still serialized and the full canonical output rendered and compared with freshly read bytes. Context space is temporary, proportional to the current event, and is neither a cross-event model cache nor an additional retained history cache. This reduces repeated work without making cold history lookup sublinear.

## Architecture Assessment

Read the private client context first: it carries intermediate results but introduces no Pydantic fields or wire/schema changes. The member validator publishes identities only after uniqueness and ordering checks, and binding reuses them only for the exact tuple; normal contexts retain the ordinary computation. A required-info `_validated_binding` after-validator delegates to `_binding(info)` so Pydantic supplies context; the undecorated helper still accepts direct context-free calls. Binding publishes the exact payload's fully normalized tagged representation only after its commitment and semantic checks complete. The existing rationale omission from payload identity and inclusion in fingerprint/event identity are preserved.

Next read the store boundary: fresh JSON input receives a new private context; entry reset prevents proof carryover on repeated dictionary validation. Event verification moves the bound payload and normalized snapshot into locals and clears all slots before serialization or digest checks, including failing checks. This additional consumption matters because Pydantic can skip before validators for an existing model. Only exact payload identity permits reuse; otherwise full normalization applies. Ordinary callers still use the independent public digest helper.

Finally read `_validated_events`: exact canonical-byte comparison, freshly read historical bytes, sequence/previous-event linkage, operation-key uniqueness, and stream identity checks remain intact. The dependency direction remains core-to-client, and no public validation or frozen digest helper is weakened.

## Test Coverage Assessment

Coverage includes all three event versions; missing, null, and populated source bodies; independent public helper and canonical-byte parity; malformed digests and fields; noncanonical input encodings; frozen nested preflight serialization; per-member computation count; success/failure/success context reuse; and revalidation after a normal nested dictionary mutation. Added prose coverage checks omitted and Unicode rationale against ordinary decoding and public digest helpers. Existing direct `_binding()` compatibility is preserved through an undecorated optional-info helper behind a required-info validator wrapper. The wrapper avoids Pydantic treating an optional validator argument as context-free; per-member and envelope-normalization assertions verify the optimization is actually engaged. Parent reported 109 scoped history/source/wire/catalog/decoder cases and 52 binding/change-set cases passing before the final reviewer test adjustments; the final decoder rerun is recorded below when available.

## Documentation Assessment

Parent final verification addendum: after the reviewer test edits and required-info
validator wrapper, the seven-file named scope passed **136 tests in 3.24 s**.
Ruff check/format passed for the two source files, decoder tests and benchmark
harness; Mypy passed for both source files. The matched unprofiled cold median
was **11.532 s before / 9.152 s after** with exact intent and history inventory
parity. The reviewer did not independently rerun that benchmark.

Private context documentation explains ownership and lifetime. Inline comments correctly explain identity guards, why event verification consumes slots before possible failure, and why rationale appears in one preimage but not another. No public API, storage format, or user-facing documentation update is required for this internal optimization.

## Overall Contribution

This is a cohesive reduction in repeated cold validation work with a conservative proof boundary. The implementation retains fresh-byte and historical integrity checks and does not add persistent mutable-model sharing. The review-discovered context lifecycle issue was resolved in the final source rather than accepted as a limitation.

## Open Questions

None.

## Suggested Follow-Ups

None.
