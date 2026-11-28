---
name: automate-state
description: Author or adapt requested Cruxible Procedures, Lines, Triggers, Mandates and Runbooks for repeatable work.
---

# Automate state

Procedures and Lines keep accepted state aligned with reality. Normative Claims
hold hypotheses, baselines, criteria, and verdicts; Procedures gather
observations (fetching APIs, running scanners, executing experiments or
benchmarks, or scoring with custom Python or ML models) at zero LLM tokens so
new Captures and observation Claims either confirm standing beliefs or wake
`cruxible next` for review and signed attestation.

Procedures describe deterministic work over accepted state and contracted
provider results, and can be as simple or as sophisticated as the domain needs.
A Procedure names no provider: each slot names a capability, a versioned kind
of call with a request Contract, a result Contract and an effect (`cruxible
orient --section capabilities`; the base kit holds the shared `workspace.` and
`web.` ones). A Line binds each slot to a provider operation that serves its
capability and fits it, and binds its `parameters` to the Procedure's params,
under an authority ceiling. Mandates state the grant, scope and validity
interval. A Runbook orders Line runs and actions; it does not widen their
authority.

Read `cruxible orient` and the relevant definitions before adding automation.
A kit's Procedures and its automation template's Lines are a
starting point to adapt, replace or extend.

What can start work: Triggers react to time (cron, cadence), to a Capture
landing under a CaptureContract (capture_landing, the event path, so imports
cascade), to accepted changes (generation_accepted) or to a window closing. A
Trigger targets a Line, a Runbook or an internal action; a Runbook takes only
time or generation_accepted schedules. `runbook preview` shows the cascades a
Runbook's writes set off, before anything runs.

What a Procedure can read: state taps read accepted state (a Query or a
ClaimType read) at the run's pinned coordinate, in dataflow order, recorded in
the run's receipt. Use them to compute over what is known instead of
re-fetching or re-deriving it. What a Capture holds is declared once, as its
CaptureKind's layout (header fields and named record sets); an extract names
a record set. Computation beyond read, call, extract, claims, filter, dedupe
and inbox posts lives in a provider's derive operation behind a capability.

Keep heavy computation, parsing, filtering, experiments, and ML scoring in
Python providers rather than cramming logic into a long Procedure graph. A
pure provider is one file of `@derive` functions (`cruxible-provider-runtime`
0.6.0): a `CaptureView` parameter reads a Capture input, and `derived_capture`
(or `view.derive(...)`) returns a computed document, such as a
`procedure_output` Capture, so a rich report stays one Capture and Claims hold
its coarse facts. `cruxible provider publish FILE` builds and installs it;
after an edit, `provider publish FILE --move` (or `provider install PACKAGE
--move`) registers the build and moves its Lines in one proposal. When no
operation widens (capability, effect, endpoints, `may_truncate`), an
observation settle grant keeps settle and an enabled Line stays enabled.

A kit's manifest-named automation template is inert. Tailor sources, local
names, timing and grants and submit it as authoring; it lands like any
proposal. Never approve it yourself: a second agent (normally a subagent)
acting as its own principal (`reviewer`, from `init
--require-independent-approval` or `principal add`) reviews the exact
candidate and approves, using the CLI with the reviewer's settings (`cruxible
context use --principal reviewer` or `CRUXIBLE_PRINCIPAL_ID=reviewer`) since
subagents share the parent's MCP servers. With no second principal, set one up
rather than self-approve; the person approves only if they asked to. Once accepted,
`cruxible runbook preview <start>` shows reads, writes, ordering problems and
what would settle or go to review; `cruxible runbook run <start>` executes
when authorized. A review stop leaves a proposal to resolve before an
explicit rerun; do not loop or self-approve.

A manual Line run and enabling scheduled execution are distinct decisions.
Enabling consents to the current definition and Triggers; `cruxible line
enable LINE... --runbook RUNBOOK` enables several, one receipt line each. A manual run may
override a Line parameter; that run is then capped at propose. Facts copied
from sources settle under a Line's Mandate; judgments always need review.
For a Line that only observes, one grant covers every ClaimType it writes:
`cruxible line grant LINE --settle-observations`, or `scope: "observations"`
on a `procedure_mandate`. A mandate takes a span `expires` (`"90d"`) instead of
`expires_at`, and `valid_from` defaults to submission. Inspect the accepted
Mandate, not assumptions.

Served examples, contracts and refusals define legal syntax; the bundled
[automation reference](references/lines-and-automation.md) explains each
construct. See also the [copy-in rule](references/copy-in.md),
[guarded edits](references/concurrent-edits.md) and
[governance](references/governance-flow.md).
