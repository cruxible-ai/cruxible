# Batch 1 derived-state design review

Design: [derived-state contracts](../derived-state-contracts-2026-09-07.md).
Source baseline: `9c0051a5ca9a5ed60e173fe901e3a94d52fbc37e`.
Scope: design and implementation boundaries, not production implementation.

Three subagents independently audited bounded portions of the current source,
then reviewed the root agent's integrated design. The root agent wrote all
document revisions; reviewers made no production or document edits.

| Review scope | Initial findings | Resolution and final verdict |
|---|---|---|
| Governed pins, consuming laws, query and Procedure provenance | No must-fix finding; requested explicit dependency discovery, refusal reuse and delta/read-set/law distinction | Clarifications incorporated. Exact-pin preservation and separately versioned referent semantics approved for batch 1 design scope |
| Immutable snapshot, candidate overlay, delta and scaling contracts | Raw deltas incorrectly required parsed artifact metadata; candidate read-session revision semantics unspecified | Raw byte/file commitments separated from validated enrichment; immutable staging revisions and memo isolation explicit. Reviewer rechecked: approved for implementation planning |
| Lifecycle, evidence, partition publication and deployment | Replay frontier not explicitly atomic with index data/root or scoped by index version/coverage | Frontier committed in validated root or same data transaction; recovery/new-version rules explicit. Reviewer rechecked: approved for reviewed design scope |

Additional incorporated refinements: canonical iteration at commitment/export
boundaries; intra-draft empty-group insertion regression; source incarnation/ABA
protection; current-coordinate catch-up before upgrade switch; bounded background
reclamation of shared nodes.

No outstanding must-fix findings in these scopes. Approval is of the proposed
engineering contract, not evidence that the component is implemented, fast, or
ready to deploy. No tests or benchmarks were run for this documentation-only
batch. Runtime parity, failure injection and scaling checks are specified as
gates on the implementation batches.

The product choice to relax governed Subject-reference pins remains separate.
The recommended implementation preserves all existing pin, digest and receipt
meanings. This report is not a maintainer ruling adopting a new law or roadmap.
