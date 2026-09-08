# State-write dogfooding exercise — 2026-09-07

Recorded the maintainer-authorized performance pause, managed-track follow-ups,
and standing time/friction protocol as two revised reconciliation Claims and one
new feedback Claim with its Subject. All three exact values read back supported
at `78cbe2200f5eee67b2ab29a2fb144f30b6eaf154`. No merge, deployment or production
code change was performed. The roadmap note preserves the pre-v1 integration and
live measurement requirement and does not close the latency/scaling gate.

## Observed live SDK timings

| Stage | Time |
|---|---:|
| connect | 8.429s |
| world | 0.015s |
| prefetch | 1.083s |
| authoring_capture | 0.013s |
| prepare | 1.419s |
| submit | 4.515s |
| review | 0.491s |
| accept_connect | 2.602s |
| preaccept_review_check | 0.814s |
| accept | 6.484s |
| readback | 1.458s |

Total active SDK elapsed time across two client sessions: **27.607s**.
Excluding the two connection calls: **16.577s**. This includes input
reads, authoring, preparation, submission, review checks, acceptance and readback;
it excludes agent composition, tool approval waiting and the offline exact-candidate
self-review pause. It is one actual project update, not a controlled benchmark or
p95 claim. No separate approval attestation was required by the served policy.
There were no SDK errors/retries and no explicit orientation refresh after acceptance.

## Friction observations

- Writing three coherent Claims in one batch was natural. Turning the conversation
  into grounded Claim revisions still required request-specific helper setup and
  a source capsule, even though the existing script and review checks were reused.
- Two sessions incurred 11.030s of connect time.
  Splitting submission and acceptance for manager review caused the second connect;
  it is workflow overhead rather than a necessary acceptance cost.
- The prior narrow prepare/submit/accept timings omitted part of the real experience.
  Counting connection, input reads and review/readback exposes that additional wait.
- Saving the receipt before readback and using its explicit coordinate worked.
  No reconnect for a full orientation refresh or mutation retry was needed after accept.
- The protocol is durable in project state and docs/dogfooding/state-write-friction.md,
  with an AGENTS.md reminder on this branch. Other checkouts gain that reminder on
  integration. Current completion timings live in the adjacent checkpoint JSON;
  carry them into the next meaningful state update, not another measurement-only write.

This exercise confirms that local fixture latency should not be treated as the
current live workflow. Integration/live measurement remains the immediate gate.
