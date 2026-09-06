# Ledger publication latency: next design slice

Review-flow integration at `24273db9` introduces synchronous remote publication
on submission, approval, activation and withdrawal. This document is a design
follow-up, not an implemented or benchmarked network improvement.

| Boundary | Current | Proposed |
|---|---|---|
| Governed write | Durable local operation followed by remote reconciliation/push | Durable local operation, pending publication notification, response |
| Scheduling | Callers may push concurrently | One cross-process serialized publisher per instance; coalesce newer work |
| Contents | Mutable ref names and wildcard pruning | Exact captured OIDs for main, notes, proposals and settled refs |
| Status | Last-attempt status plus accepted main OID | Exact published ref manifest and request watermark; newer writes stay pending |
| Remote review | Caller waits for publication attempt | Separate publication ticket/status; explicit wait for required review snapshot |
| Operator barrier | Set-mirror tests publication immediately; no separate publish command | Preserve set-mirror check and add explicit `ledger publish` wait barrier |
| Restart | Last attempt file | Reconcile ledger/evidence against remote publication; queue is disposable |

Three correctness issues should be resolved before moving publication out of the
request. In `PlaybillInstance.publish_ledger_mirror`, successful publication
records `read_main()` after the push, which can name a concurrent generation that
was never pushed. Tracking main alone also cannot reveal an unpublished proposal
or approval note when accepted main is unchanged. Finally, concurrent non-atomic
pushes can reject a stale main while still force-updating derived refs to older
contents. The fixed temporary status filename also needs serialized writes or
unique temporary files.

The publisher should snapshot exact refs, retain their objects until completion,
and push atomically. Main remains non-forced; derived ref updates/deletions use
expected-old-OID leases. Unexpected remote edits produce a divergence report.
An old completion may never clear a newer pending watermark. Bound retries and
report failures through the existing mirror-behind work item.

Local evidence, notes, approvals, and acceptance remain synchronous. A crash
between durable publication-worthy work and enqueue is repaired by startup
reconciliation. Enqueue/push failure cannot refuse an already durable write.
The local signed ledger remains authority. Remote acknowledgement means only
that the remote accepted the exact snapshot at that time.

Validation must cover concurrent accepted generations, approval-only changes,
older push completion after newer work, atomic rejection, unexpected remote
edits, crash before enqueue, restart, offline timeout and explicit wait behavior.
No remote latency savings are claimed until that loop is implemented and timed.
