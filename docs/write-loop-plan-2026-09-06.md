# Write-loop performance and follow-up plan

Prepared from the integrated code at `9f5ebb999` (daemon code `2215f370`).
Execution branch: `codex/write-loop-latency`, in an isolated worktree. The first
measured implementation pass is recorded in
[write-loop results](write-loop-results-2026-09-06.md). Four fixes through
`283ca306` are independently reviewed; the performance exit gate below remains
open. Follow-up stages are a sequence, not a claim that every stage has shipped.

## Objective

Make maintaining the project-state instance a routine part of work. The current
18-Claim checkpoint cost 15.736 seconds to prepare, 29.831 to submit and 37.778 to
accept, excluding connection/setup, review and readback. Faster local reads do
not compensate for this write loop. Prioritize this before broader features.

The ledger and signed accepted generations remain authoritative. Rebuildable
indexes and operational publication state must not acquire acceptance authority.
Preserve stored digest rules, exact candidate binding, reference checks, evidence
integrity, current authority checks, and crash/retry behavior.

## Execution order

| Order | Work | Concrete completion evidence |
| --- | --- | --- |
| 1 | Trace complete served prepare, submit and accept, including SDK setup and readback separately. | Matched repeated baseline, phase timings and Git counts; explain the dominant costs of acceptance, not only preflight. |
| 2 | Fix intent-v2 GET dropping `reference_expectations`. | Exact supported-version round-trip through public SDK/HTTP, including nonempty assertions; stale-reference refusal still works. This also removes the known limitation of the copied-payload diagnostic. |
| 3 | Reduce foreground historical review/workspace reconciliation. | A small new write no longer rematerializes every historical review. Publication status and an explicit completion barrier accurately distinguish durable local settlement from derived publication. Exact grouped notes, archives, corruption refusal and recovery remain correct. |
| 4 | Reuse accepted-state evaluation derivations. | Compute accepted referents once per evaluation; reuse bounded parsed policy/Claim and dependency state at verified coordinates. Candidate overlays and evaluation-time filtering remain explicit. Matched candidate/evaluation parity and improved timings. |
| 5 | Address the remaining acceptance bottleneck and repeated evaluation, in the order established by step 1. | No reuse based solely on a preflight certificate. Bind reused computation to its actual inputs and rerun current settlement/authority checks. Measure lock hold/wait time, durable writes, publication and recovery independently. |
| 6 | Validate the complete customer loop and integrate reviewed changes. | Fresh and warm SDK sessions read a task, prepare a real update, submit, review, accept and read it back. Named regression checks and independent review cover each logical fix; a concise full-range guide accompanies integration. |
| 7 | Use the instance for normal project management and remove remaining unnecessary SDK orchestration. | Progress, findings and handoffs are captured with a reusable bounded workflow. Report actual repeated use and friction, rather than declaring success from a microbenchmark. |

The sequence of optimization steps is provisional: an acceptance trace that
identifies a larger independent cost moves that work earlier. Commit source and
meaningful tests together per logical fix. Tests run outside the canonical
checkout with isolated state roots. Do not run the full suite or journal golden
corpus as a substitute for named verification scopes.

## Measurement design

- Separate latency seen by the caller from background completion. Record the
  publication barrier too; deferred work must not disappear from accounting.
- Include cold connection/recovery and warm request measurements separately.
  Record idle repeated samples before adding concurrency; do not label a few
  samples a production p95. Keep baseline and changed-code workloads matched.
- Cover a small single-task update and the representative 18-Claim mixed
  replacement/create checkpoint. Include attached workspace behavior and growing
  historical review counts. Preserve real reference assertions in the workload.
- Use disposable instances with their own signing identities for full settlement
  benchmarks. Do not copy live private signing custody or create meaningless
  performance-only generations in the program instance. Distinguish synthetic
  histories from the read-only copied production world.
- Trace compile/lower, intent-store transitions, reference validation, evaluation,
  review-note integrity, locks, signing, Git/CAS/ledger durability, publication and
  response serialization. Report nested timings as nested costs, not additive
  independent rows. Account for profiler overhead with unprofiled wall timings.
- Check exact deterministic output parity at controlled inputs/clock, plus
  stale-base, changed-authority, tampered-note, collision-group and crash/retry
  behavior where the changed code can affect them. Retain rebuild-from-ledger
  verification for altered derivatives.

## Performance exit criteria

Provisional engineering targets, not promises: a warm one-task update should
take about one second per foreground stage, with a representative 18-Claim
prepare/submit/accept sequence near five seconds total, excluding deliberate
review time and optional remote publication barriers. Measure setup separately
and keep an SDK connection/World reusable across ordinary work.

Regardless of the precise targets, do not move on while a routine small write
still incurs unexplained tens of seconds or work proportional to all historical
reviews. After the dominant costs are removed, report any remaining budget gap
and its measured tradeoff. Avoid an endless micro-optimization campaign once
ordinary project management is comfortably interactive.

## Product work after the performance gate

Current next performance slice: cold intent-index initialization after restart,
then projection prebuild and verified checkpoint recovery, followed by repeated
evaluation and broad readback. The exact program workload's cold intent lookup
takes 11.741 seconds versus 0.109 warm; the checkpoint report records its limits.
The [subsequent per-event validation pass](intent-cold-validation-2026-09-06.md)
reduces a matched cold median from 11.532 to 9.152 seconds; warm remains about
0.08 seconds. Cold readiness remains open. Before a durable index or new journal
format, resolve payload retention, verifier-bound index trust, and the policy
that historical intent corruption currently gates unrelated writes.
Batched fresh Git
reads removed most reconciliation subprocess cost without changing publication
semantics. Moving publication off the foreground path is still a separate
decision; no speedup in this pass is attributed to deferred acknowledgment.

| Priority | Follow-up | Release proof |
| --- | --- | --- |
| First | Complete procedure output to proposal and the required capture/reading provenance path. | An accepted Procedure acquires file evidence, computes an interpretation, returns a rung-2 proposal, and produces inspectable provenance through review and acceptance. Deterministic computation does not automatically make its interpretation accepted truth. |
| Alongside that loop | Delegated proposal bundles with captures. | A subagent returns a self-contained proposal/evidence bundle tied to a base coordinate; the manager can inspect, submit and settle it without manually reconstructing its captures. Stale bases and conflicting parallel proposals are explicit. |
| Next | Freshness and repair through Lines. | A changed source identifies affected interpretations, reruns the relevant work and offers a reviewed repair. Failed acquisition, unchanged evidence, retry and conflicting results have truthful observable outcomes. |
| Then | One complete KEV migration/demo. | Read real source data, form useful customer-world interpretations, update evidence, review a repair, and preserve unrelated local judgments. Use this to validate the product loop before broad provider/catalog expansion. |
| After demonstrated need | Managed warmth, scheduling and paid curation. | Keep the same OSS authority semantics while serving warm indexes, recurring execution and measured useful state maintenance. Expand orchestration and reference-world offerings from observed workloads. |

The human inspection surface remains Git review plus structured explanations and
agent-produced projections. Avoid a renderer project in this pass. Compact
projection identifiers remain a separate retention/export-aware design task.

## Handoff and decision boundaries

At each completed stage, record the exact branch/commit, change, named checks,
matched timings, remaining uncertainty and next action. Update project state at
meaningful milestones rather than paying the write overhead for each trace.
Keep the current slow timings visible until replaced by measured results.

Routine implementation choices within this plan do not need another permission
round. Surface a decision if a proposed speedup changes authority semantics,
requires weakening durable guarantees, alters public settlement meaning, or
requires a product scope tradeoff. Preserve independent review before integration;
do not mistake benchmark success for approval of a correctness-sensitive change.
