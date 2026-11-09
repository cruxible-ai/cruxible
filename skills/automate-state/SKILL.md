---
name: automate-state
description: Author or adapt requested Cruxible Procedures, Lines, Triggers, Mandates and Runbooks for repeatable work.
---

# Automate state

Procedures describe deterministic work over accepted state and contracted
provider results. A Line binds a Procedure to local parameters, provider
builds and an authority ceiling. Mandates state the grant, scope and
validity interval. A Runbook orders Line runs and actions; it does not widen
their authority.

Read `cruxible orient` and the relevant definitions before creating another
automation; existing kit Procedures and templates may already express it.

What can start work: Triggers react to time (cron, cadence), to a Capture
landing under a CaptureContract (capture_landing, the event path, so imports
cascade), to accepted changes (generation_accepted) or to a window closing. A
Trigger targets a Line, a Runbook or an internal action; a Runbook takes only
time or generation_accepted schedules. `runbook preview` shows the cascades a
Runbook's writes set off, before anything runs.

What a Procedure can read: state taps read accepted state (a Query or a
ClaimType read) at the run's pinned coordinate, in dataflow order, recorded in
the run's receipt. Use them to compute over what is already known instead of
re-fetching or re-deriving it. Computation beyond read, call, extract, claims,
filter, dedupe and inbox posts lives in a provider's derive operation behind a
Contract.

A kit's manifest-named automation template is inert. Tailor sources, local
names, timing and grants, submit it as authoring, and obtain review and
approval from a separate agent session (its own principal). Once accepted,
`cruxible runbook preview <start>` shows reads, writes, ordering problems and
what would settle or go to review; `cruxible runbook run <start>` executes
when authorized. A review stop leaves a proposal to resolve before an
explicit rerun; do not loop or self-approve.

A manual Line run and enabling scheduled execution are distinct decisions.
Enabling consents to the current definition and Triggers. A manual run may
override a Line parameter; that run is then capped at propose. Facts copied
from sources settle under a Line's Mandate; judgments always need review.
Inspect the accepted Mandate rather than assuming what may settle.

Served examples, contracts and refusals define legal syntax; the bundled
[automation reference](../../docs/lines-and-automation.md) explains each
construct. See also the [copy-in rule](../_shared/references/copy-in.md),
[guarded edits](../_shared/references/concurrent-edits.md) and
[governance](../_shared/references/governance-flow.md).
