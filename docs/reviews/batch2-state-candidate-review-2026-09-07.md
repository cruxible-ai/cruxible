# Batch 2 program-state candidate review

**Approved: no findings.** This approval covers routine manager reconciliation of the exact candidate below. It does not adopt roadmap priorities, authorize release or deployment, or approve a different candidate. The reviewer made no live-instance writes and did not activate the proposal.

## Exact reviewed binding

- Proposal: `sha256:e9b8ac6dcaf90115af3de736396246876d020386c42948b6ede0d7df158d837d`
- Candidate: `sha256:772af61e979485bb4b8e507616d5e8e87d984577d415a2974905cfd60d1fa7a8`
- Base accepted Git OID: `7fcc40c771411f210d3514b66c3cbee452233789`
- Parent semantic root: `sha256:1c4f2251a1f1b08db3c889075840e00f1b08e2db31c9ce239eaef6f39e90046c`
- Base generation root: `sha256:a9ef22a31a5585a9b576b1d093dd1f91dd8f5ee1a17d77e52591c0d94f36dfff`
- Compiler digest: `sha256:97dc147603444a6f910e9edde93ed56f20e196cceefa02280f26572553e53cab`
- Candidate evaluation time: `2026-09-07T18:55:48.891594Z`
- Full served review input: `/private/tmp/playbill-batch2-candidate-review.json`; SHA-256 `775b03e5353b8f5943d42de945eab45790fce904491cf1206ce504f01ce741e3`.
- Expected payloads and authorization: [checkpoint](batch2-state-checkpoint-2026-09-07.json).

## Scope and integrity checks

The served candidate, complete member inventory, and semantic scope contain exactly four authored members:

| Member | Operation | Meaning |
|---|---|---|
| `CLM-03e378a41032c2774d501ec86ab21187` | Replace | Existing performance roadmap reconciliation note; normative role preserved |
| `CLM-7be19d54b36f43ed4d229fe0e4ade39d` | Replace | Existing derived-state roadmap reconciliation note; normative role preserved |
| `CLM-59a7392fcf7abb0433f64861ba6a4273` | Create | Agent-customer implementation finding, observation role |
| `dev.product_feedback/derived-state-batch2-2026-09-07` | Create | Live Subject for that finding, with no pins |

Programmatic checks recomputed the semantic candidate digest and all four artifact digests from the served payloads. All three Claim values, roles, Subjects and predicates exactly match the checkpoint's expected records. Both prior note values match the recorded predecessors. Each revised Claim's lifecycle predecessor equals its recomputed base artifact digest; Claim identity, statement fields other than the literal value, and all governed pins remain unchanged. The new Claim and Subject have no predecessor.

No ClaimType, principal, policy, release, adoption or authority artifact is in scope. The served governance retains `governed_write`, snapshot activation, and no required approvals. The served law evidence reports all three Claims supported; this review does not treat that grade as independent proof of measured performance or reinterpret acceptance as a truth gate.

## Source and measurement checks

The manager-authored source capsule was reconstructed from the checkpoint metadata and exact source file contents. Its 43,553 bytes hash to `sha256:70d38539e96c27941d7bf1415e1eaf81b0dd1d8b63071f1e9f0b7996a6e8c8d5`. Every candidate Claim carries a whole-capsule source mapping with that digest and byte range. Existing revision history remains represented in the backing; the new source is an explicitly attributed manager assessment.

Verified source files:

| File | SHA-256 |
|---|---|
| `derived-state-batch2-2026-09-07.md` | `23011f0ac63aaf0b85348afdfeb4c2a6d9b54a2d188d48349427ffe6cc19b4ab` |
| `batch2-scope-benchmark.json` | `cd2ea3b2d763366f889252069ce3cb80be340491d8778b19ca2cdea660c24526` |
| `batch2-wheel-validation.json` | `e211994752258a022ff97a1561e441a3f9766aa52a924b4e69be01a65141c80f` |

The rounded prepare, submit, accept and full-loop numbers reproduce from the repeated rows in `batch2-served-benchmark.json`: 0.528→0.319 s, 2.237→1.855 s, 3.383→2.630 s and 10.084→8.338 s. The fixed-group contender and 10,000-member evaluation numbers reproduce from the scope benchmark comparisons. The checkpoint accurately retains the small-sample qualification, different workload from the prior program benchmark, modest cold-path regression, remaining Merkle fan-out, local-only implementation status, and incomplete managed-memory/lifecycle work. Installed-wheel and recovery statements match the validation record.

The claims accurately summarize [the final implementation report](derived-state-batch2-2026-09-07.md) at implementation commit `dbf613d31bc393f99a2ff33f993786a549b15fd0`. They do not claim that the branch has been merged, pushed or deployed, or that the latency/scalability gate is closed.

Activation may proceed only for this exact reviewed candidate through the normal manager operation and its fresh accepted-base/authority checks.
