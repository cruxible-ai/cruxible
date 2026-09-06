# Prepared lowering: stage improvement, no measured write-loop improvement

The isolated `codex/state-loop-design` change at `86f87611` reuses deterministic
authoring lowering. It does **not** reuse a whole preflight or its acceptance
decision. On a 162-Claim fixture, repeated preflight became faster, but complete
prepare-and-submit time did not measurably improve.

| Median of three samples | Baseline | Lowering reuse |
|---|---:|---:|
| Repeated preflight | 0.818 s | 0.445 s |
| Lowering inside repeated preflight | 0.393 s | No lowering calls |
| Evaluation inside repeated preflight | 0.390 s | 0.386 s |
| First preparation, separate complete-loop experiment | 1.045 s | 1.043 s |
| Submission after preparation | 8.070 s | 8.091 s |
| Complete preparation plus submission | 9.115 s | 9.198 s |

The repeated-preflight reduction is 45.6%. The complete-loop medians differ by
less than the variation between runs and provide no evidence of an end-to-end
speedup. Submission still requires attribution of its other costs. These are
local coordinator measurements, not HTTP latency, activation latency, or a
benchmark of the live program instance.

## What is retained and what is always recomputed

Eligible authoring consists of self-source Claims and pure definition members.
The cache key uses freshly serialized authoring inputs, actor, accepted
coordinate, instance descriptor, and receive limits. Generated CAS bodies and
capture envelopes are verified against their exact content addresses on a hit.
Missing generated bodies cause ordinary lowering to rebuild them; corruption
still refuses. Returned mutable containers are private copies.

Working selections, existing captures, procedures, and other operationally
dependent lowering stay uncached. Every request still performs reference
validation, bounded receive checks, candidate evaluation, and proposal authority
checks. A certificate alone is never sufficient to reuse those decisions.

The process-local cache retains at most four entries per instance and 32 MiB of
accounted serialized/tree bytes; this is not a Python heap-size guarantee. Weak
instance ownership releases entries with their instance. Eviction or restart
only loses saved computation. Authoring intents, accepted ledger state and
referenced authoritative artifacts remain the rebuild inputs.

## Benchmark method and samples

Both experiments use `initialize_local`, `_seed_claim_surface`, and the
change-set test helpers to create 162 self-source Claims with distinct
`item-{index}` qualifiers. The accepted state contains one seeded generation.
The baseline bypasses `reuse_lowering` by invoking its ordinary `compute`
callback; all other code is identical. Baseline and reuse runs alternate.

For the stage experiment, one initial preflight warms the process and prepares
the reusable entry. All six measured calls run against the identical intent and
accepted coordinate. Each complete `ComputedPreflight` is compared for equality
against the initial result, including candidate bytes and certificate.

| Sample | Baseline preflight | Reused preflight | Baseline lowering | Baseline evaluation | Reused evaluation |
|---|---:|---:|---:|---:|---:|
| 1 | 0.786850 s | 0.424851 s | 0.386709 s | 0.367779 s | 0.368644 s |
| 2 | 0.817765 s | 0.445250 s | 0.393157 s | 0.389848 s | 0.386274 s |
| 3 | 0.837957 s | 0.460780 s | 0.410940 s | 0.391670 s | 0.393565 s |

For the complete-loop experiment, each sample creates a fresh isolated instance
with the same fixture, then measures `coordinator.preflight` followed by
`coordinator.submit`. Initialization and intent creation are outside the timer.
Every submission must retain its exact prepared certificate, produce a candidate,
and return all 162 submitted members. No activation is performed.

| Sample | Baseline prepare | Baseline submit | Baseline total | Reuse prepare | Reuse submit | Reuse total |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 1.037640 s | 7.988480 s | 9.026120 s | 1.107046 s | 8.090716 s | 9.197761 s |
| 2 | 1.044880 s | 8.070251 s | 9.115131 s | 1.021201 s | 7.777528 s | 8.798729 s |
| 3 | 1.087487 s | 8.642376 s | 9.729863 s | 1.042872 s | 8.208268 s | 9.251140 s |

## Verification

All tests ran from the isolated worktree with the canonical environment's Python
and worktree `src`/SDK on `PYTHONPATH`:

- `test_prepared_lowering.py` and `test_authoring_preflight.py`: 17 passed.
- `test_authoring_change_set_intents.py`: 27 passed.
- Focused Ruff checks and mypy for the two changed source modules passed.

The new tests exercise exact prepare/submit parity with fresh evaluation,
replaced payloads, accepted-coordinate changes, receive-limit and actor-capability
changes, deleted and corrupt generated bodies, cache clearing and eviction,
uncached working selections, and mutable input/output isolation. No full suite,
golden corpus, or live daemon mutation was used.
