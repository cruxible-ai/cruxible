# Code Review

## Verdict

Approved

No source findings in the scoped cache, extraction and wiring. Only deterministic results of strict Claim parsing and static fact normalization are retained, while exact input bytes bind hits and each result materializes fresh mutable containers. Generation-dependent rows, global validations, body-dependent work and publication proofs remain outside the cache.

## Manual Review Priority

- Priority: P1
- Reason: This reuses validation-derived projection data and narrowly bypasses repeat Pydantic construction through a private immutable representation.
- Suggested Human Review Focus: Exact input binding; normalized-JSON provenance and mutable-value isolation; unchanged refusal order; dynamic/global checks; honest cache memory accounting.

## Scope Reviewed

- Changed files: `src/cruxible_core/playbill/projection_artifacts.py`, `assembler.py`, `activation.py`, and `instance.py`, reviewed against `c85a99bbd00a909c82790faa2ab4e631502c5e0b`, committed as `5a0b81b2` in `/private/tmp/playbill-projection-reuse`.
- Added files: `src/cruxible_core/playbill/projection_claim_cache.py`, `tests/test_playbill/test_projection_claim_cache.py`, and `tests/test_playbill/test_projection_claim_reuse.py`.
- Tests examined: All 15 cache unit cases and all 7 integration cases in `test_projection_claim_reuse.py`, including the corrected complete row/SQLite export oracle. Existing Claim projection and activation-handoff regression results were supplied by the parent.
- Commands run: Scoped `git diff`, `git status --short`, `git diff --check`, `git rev-parse HEAD`, and source/test inspection. No source edits, tests, benchmarks, full suite or golden corpus run by the reviewer, to avoid duplicating parent verification and contention.

## Findings

No findings.

## Complexity Assessment

Warm hits avoid strict Claim model reconstruction, digest derivation, repeated model dumps and static fact normalization. They still perform exact byte-key lookup, strict outer JSON loading, fresh JSON materialization of static values, current history/revision/explanation/verdict work, registry validation and full SQLite/publication work. Thus the complete build remains proportional to world size; the optimization reduces repeated computation rather than making projection incremental as a whole.

The instance cache is bounded to 4,096 entries and 32 MiB of accounted encoded payload by default. The accounting includes retained exact content, key text, scalar metadata, pin metadata and every fact's encoded value/metadata, with framing allowances. It correctly disclaims Python heap/RSS measurement; object and allocator overhead are not the claimed limit. Short locked LRU bookkeeping is separated from compilation and materialization. Over-capacity complete scans can thrash, and disabled/oversized entries still compile without retention; warm gains should be reported for the measured population and capacity.

## Architecture Assessment

Read the cache representation first. CachedClaim and FrozenClaimFact are frozen slotted dataclasses whose retained fields are bytes, strings, booleans, integers and tuples. `from_fact` serializes values only after ordinary ProjectionFact construction has normalized and validated them. `materialize` JSON-decodes fresh containers before `model_construct`; it therefore reuses a local validation result without accepting a persisted/wire cache representation or sharing mutable fact values. Cache keys include compiler digest, explicit codec, canonical path and full bytes, so Python hash collisions still require exact equality.

Next read the extraction. `_claim_static_facts` preserves the old identity, statement, backing, lifecycle/retirement and source-mapping rows. In the original sorted tree traversal, strict outer JSON decoding precedes Claim parse/cache lookup; duplicate identity detection still precedes digest/envelope revision/static facts. Cache publication occurs only after static construction succeeds. A later dynamic or global refusal does not invalidate that independent static proof and is rerun on the next attempt. Envelope revision, exact-law lookup, accepted proof coordinates, verdicts, evidence basis and attestation rows remain freshly derived. Pin conflicts and current registry validation still execute over the complete output. No CAS resolver or body-dependent artifact path is bypassed.

Finally read lifetime wiring. The instance owns the cache and passes it only through its publisher's normal prebuild path. Explicit recovery clears it and rebuilds without cache injection; successful verified handoff retains it for subsequent accepted generations. Eviction or process restart therefore changes cost, not meaning. Direct/provisional parsing remains uncached without the optional cache and coordinate. This keeps the retained state derived and disposable.

## Test Coverage Assessment

Cache unit tests cover source/result nested mutation isolation, frozen metadata, each key component, count/byte LRU ordering, replacement and oversized replacement, zero/negative limits, variable-field accounting and concurrent get/put/clear. They verify meaningful invariants rather than merely mirroring implementation.

The 7 integration cases establish cold/warm parsed-row and full SQLite logical export/digest parity while forbidding repeat parsing/static compilation; actual served prebuild retains the static Claim cache across acceptance while unchanged Claims receive the new coordinate in provenance proofs. Retirement changes input bytes and forces compilation, lifecycle/revision updates and cold/warm parity. Corrupt Claim and historical ChangeSet bytes retain matching refusals, changed registry declarations still refuse, refresh clears the cache, returned-fact mutation remains isolated, and byte-budget bypass preserves output. The initial oracle compared distinct registry objects by identity; the corrected oracle compares parsed rows/request plus complete SQLite logical exports/digests, which is the meaningful invariant.

Final parent-reported validation: 34 distinct tests passed—15 cache unit cases, 12 existing Claim projection/activation-handoff cases, and 7 integration cases (27.22 seconds for the final integration rerun). Scoped mypy passed for all 5 changed source files; Ruff passed for all 7 changed source/test files. No full suite or golden corpus was run, and the reviewer did not duplicate tests or benchmarks.

## Documentation Assessment

Module and materializer comments clearly identify the private already-validated-input boundary and distinguish skipping repeated normalization from skipping input validation. Cache accounting documentation accurately limits its claim to encoded payloads. No public contract, frozen storage format or authority rule changes require user-facing migration documentation.

## Overall Contribution

A cohesive implementation of the smallest byte-dependent Claim reuse seam. It preserves the ledger and full projection verifier as authority while reducing repeated per-Claim work across accepted generations. Report parse-phase and complete acceptance timings separately, including cold population cost and retained-memory accounting limits.

## Open Questions

None.

## Suggested Follow-Ups

None.
