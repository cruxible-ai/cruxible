# State-write dogfooding

Maintainer instruction, 2026-09-07: for the foreseeable future, measure write time
and friction whenever the agent writes project state. This applies to meaningful
state updates, not synthetic writes made only to collect latency samples.

Scheduling clarification, 2026-09-08: the maintainer's later ruling supersedes
the initial managed-track deferral below. The live write experience needs further
redesign **before OSS release**; pausing that work while integrating and exercising
the procedure loop is temporary. Fleet operation and partition orchestration
remain managed work. Keep the earlier decision and measurements as history, not
as permission to release with the present write friction.

## Practice for each write

- Use the public SDK. Record instance, client/runtime versions when available,
  workload/member count, starting and accepted coordinates, and proposal identity.
  Distinguish live deployment measurements from disposable branch benchmarks.
- Measure connect/orientation separately from the write. Record prefetch/read,
  authoring/capture, prepare, submit, exact review, approval when required,
  acceptance and verified readback. Mark omitted stages rather than assuming zero.
- Record total active SDK elapsed time, individual stage times, errors/retries,
  and qualitative friction: setup/glue, extra connections, manual identifiers,
  reviewing, redundant refreshes and whether batching felt natural. Separate
  tool/approval waiting and human/agent composition from SDK service latency.
- Persist timings and outcome locally alongside the checkpoint/receipt, including
  partial failure. Never log credentials. Save acceptance before readback; after
  an uncertain response inspect status before retrying a mutation.
- Include the previous completed observation in the next meaningful state update.
  A write cannot include its own completion time. Do not create an endless chain
  of governed writes to measure the preceding write. Local timing logs are
  operational observations; they do not replace ledger authority.
- Read back at the receipt's explicit accepted coordinate. Do not add a full
  orientation refresh merely to obtain current state after acceptance.

First exercise: `docs/reviews/write-dogfood-state-checkpoint-2026-09-07.json`.
This note is referenced from AGENTS.md so the practice survives task handoffs.
It becomes effective in other checkouts once this branch is integrated.

## Accepted performance direction

The maintainer accepted pausing speculative local latency optimization and moving
remaining architectural performance work to the managed-instances track. This is
not a claim that the measured six-second loop already describes the live daemon,
that the latency gate is closed, or that the managed work is implemented.

Before v1: review/integrate the accumulated performance branches, measure the real
project instance after rollout, continue the v1 product loops, and use state
routinely. Avoid unnecessary per-field governed writes and reconnect/refresh work.
Escalate measured dogfooding regressions or disruptive workflow friction even while
the broad performance track is paused. No merge or deployment is performed by
this note's checkpoint.

Before managed-instance scale testing, retain these outstanding tasks:

1. Batch 5: finish parent/delta-based generation-tree writing and physical
   inventory validation; scoped reads already exist. Benchmark submit and accept.
2. Batch 6: completed database-verification handoff and normalized explanation
   binding. Preserve frozen exports/digests and independent recovery checks.
3. Batches 7/8: migrate remaining history/query/evidence consumers to the central
   owner with complete dependency observations and sound source revision protocols.
4. Complete managed resource accounting, bounded reclamation, partition replay/
   serving and rolling index-version upgrade orchestration.
5. Keep persistent/batched Git execution and asynchronous workspace advertisement
   as measured options requiring their own contracts. This is distinct from the
   existing asynchronous remote-mirror publication. Broader database snapshot or
   commitment-format changes require size evidence and a separate design decision.

All derivatives remain reconstructible from their declared retained sources;
accepted Git and signed ledger remain governed authority. No pin semantics,
release/adoption fields, authority policy, or stored digest formats change here.

## Prior completed live observation

The publication-audit checkpoint (three Claims and one Subject) measured connect
8.548s, prefetch 1.126s, prepare 1.336s, submit 4.402s, review 0.484s, a second
connect 2.491s, accept 6.150s, and readback 1.739s. These are individual SDK stage
observations, not a complete end-to-end measurement: vocabulary/authoring and a
fresh pre-accept review check were not separately timed. Acceptance and all three
supported values were verified. The extra connection came from a split manager
script and is workflow overhead, not an intrinsic acceptance requirement.

The separate local 1,000-Claim benchmark measured 6.33-6.43s for its full loop with
explicit orientation, or 5.51-5.60s without it. Do not compare those figures as if
they were matched versions of the live workload. Broader batching is acceptable
when it reflects one coherent update; do not hide poor interaction behind giant
batches or claim an unmeasured percentile target.
