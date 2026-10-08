---
name: automate-with-procedures
description: Automate recurring work in Cruxible - author a Procedure that reads state, acquires sources and calls pinned providers, run it, put it on a Line with Triggers and a mandate, enable it, and keep it healthy with next, evaluate, dispatch and measurements.
---

# Automate with Procedures

Use this skill when the same piece of work should run the same reviewed way
every time: a check over accepted state, a feed that should be read and
recorded, a classification that proposes values for review.

This skill is for:

- authoring a Procedure and running it directly
- running a Procedure on a schedule or an event as a Line
- letting a Line propose or settle changes under a mandate
- recovering missed runs and measuring whether the Procedure works

A Procedure is a governed, deterministic graph. It reads accepted state
(named queries and Claims), acquires observations through Source nodes under
an acquisition policy, calls providers pinned to one exact implementation, and
transforms and routes values. Its terminals (retain a Capture, propose a
change set, settle one) act only when it runs as a Line. No model runs inside
it: judgment comes from providers you install and from the people and agents
who review what it proposes.

## Phase 1: Design the work

Agree with the user, before writing anything:

1. what the Procedure reads (which kinds and named queries) and what it
   receives as input;
2. which outside sources it acquires, through which provider interface
   (`cruxible orient --section interfaces`; every instance has the built-in
   `workspace.file`), and under which CaptureContract;
3. which providers it calls, and whether the instance has them
   (`cruxible provider list`, `cruxible orient --section interfaces`);
4. what it ends in: a returned result (observe), a proposed change set
   (propose), or a settled one (settle);
5. when it should run: on demand, on a schedule (cadence or cron, in UTC),
   when a Capture lands, when a window closes, or when a generation is
   accepted;
6. what would show it works: a measurement over a named query, a Claim's
   verdict, or attestations.

If a kit already ships the Procedure or a Blueprint for it, use that
(`adopt-kit`).

## Phase 2: Author the Procedure

Start from the templates, which are accepted together:

```bash
cruxible authoring example procedure
cruxible authoring example acquisition-policy     # when it has Source nodes
```

Or author it in Python: a `Sequence` of steps from
`cruxible_client.authoring.procedures`, or a function decorated with
`@procedure` from `cruxible_client.authoring.source`. Select providers with
`cx.provider_interface(interface, provider=...)`; the daemon pins the exact
implementation. A definition that leaves a provider slot open is a Blueprint,
which never runs; instantiate it to get a Procedure.

Check, then submit:

```bash
cruxible authoring submit PROCEDURE.json --dry-run
cruxible authoring submit PROCEDURE.json --and-activate
cruxible get Procedure:NAME        # runnable: direct, line, or unsupported
```

## Phase 3: Run it directly

When `runnable` is `direct`:

```bash
cruxible procedure run NAME INPUT.json      # or - for stdin
cruxible get ProcedureRun:RUN-…             # state, outcomes, terminal detail
```

A direct run never runs terminals. A Procedure whose terminals propose or
settle reports `runnable: line`; run it as a Line.

## Phase 4: Put it on a Line

A Line runs the Procedure under its own parameters (the Procedure's input),
budgets, authority ceiling (`max_authority`) and acquisition policy:

```bash
cruxible authoring example line          # a Line over the example Procedure
cruxible authoring submit LINE.json --and-activate
cruxible line run LINE                   # one manual occurrence, now
```

`line run` always runs one manual occurrence under the Line's settings; it
never consumes a Trigger. Pass `--event` when the Procedure takes an event as
input.

A Line that proposes or settles needs a mandate covering its Procedure before
it runs or can be enabled:

```bash
cruxible authoring example procedure-mandate
```

A propose mandate bounds what the Line may propose; a settle mandate also
names what it may settle without approvals (its Claim scope and a condition
query) and what happens otherwise (refuse, or fall back to a proposal). Review
mandates with the user like any other governed change.

## Phase 5: Schedule and enable it

```bash
cruxible authoring example trigger       # an hourly cron Trigger for the example Line
cruxible authoring submit TRIGGER.json --and-activate
cruxible get Line:LINE                   # its Triggers; "triggers_inactive: not enabled"
cruxible line enable LINE
```

A Trigger aimed at a Line does nothing until a principal enables the Line.
Enabling needs governed write, even for a Line that only observes, and pins
the Line version and the exact Trigger versions: any change to the Line or its
Triggers stops the enablement until it is enabled again. The daemon then admits
what the Triggers match, under the enabling credential, from now on; it never
catches up. `cruxible line disable LINE` stops it; admitted runs keep going.

## Phase 6: Keep it healthy

```bash
cruxible get Line:LINE           # enablement state, pending work, recent runs
cruxible next                    # stopped enablements, coverage gaps, stalled work
```

- After a daemon restart, time it was down is not matched: `next` shows a
  `line_coverage_gap` row with the exact `cruxible line evaluate LINE --since S
  --until U`, which records the missed range as pending work, and
  `line_work_pending` while work waits for `cruxible line dispatch LINE`.
- `line evaluate LINE --dry-run` shows what a range would make eligible,
  without recording anything.
- An enablement that stopped by itself (a revoked credential, a changed Line
  or Trigger, an inactive principal) shows as `consumer_stalled` with the
  reason; enable it again once the cause is fixed.

When the Procedure declares measurements, evaluate them once due and read
their standing:

```bash
cruxible procedure measure NAME --run-id RUN-…
cruxible procedure readings NAME
```

A completed run does not prove the Procedure worked, and a failed run does not
disprove it; the measurement's verdict comes from its evidence.

## Phase 7: Hand off

Proposals a Line produces are ordinary proposals: review, approve and activate
them per `../_shared/references/governance-flow.md`. Tell the user which Lines
are enabled, under whose credential, on which schedule, with which mandate,
and which `next` rows to watch.
