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

An acquisition policy must have a rule for every Source alias the Procedure
reads (extra rules are allowed, so one policy can serve several Procedures);
a missing rule is refused at admission, naming the alias.

Or author it in Python: a `Sequence` of steps from
`cruxible_client.authoring.procedures`, or a function decorated with
`@procedure` from `cruxible_client.authoring.source`. Select providers with
`cx.provider_interface(interface, provider=...)`; the daemon pins the exact
implementation. A definition that leaves a provider slot open is a Blueprint,
which never runs; instantiate it to get a Procedure.

Sequence supports exactly these 11 graph kinds:

| Graph kind | Sequence step | Served lane / requirements |
|---|---|---|
| `state_tap` | `StateTap` | Direct or Line; named query binds at admission, before provider outputs exist |
| `source` | `Source` | Direct or Line; bound provider/interface, CaptureContract and acquisition policy |
| `call` | `Call` | Direct or Line; bound provider/interface and applicable effect policy/authority |
| `transform` | `Transform` | Direct or Line; closed transform spec and matching Contracts |
| `project` | `Project` | Direct or Line; explicit fields satisfy output Contract |
| `guard` | `Guard` | Direct or Line; forward branches, false defaults to `$abort` |
| `halt` | `Halt` | Direct or Line; ends without a successful value |
| `emit_capture` | `EmitCapture` | Line only; registers evidence and ends the path |
| `post_inbox` | `PostInbox` | Line only; observe authority, bounded `max_items`, ends the path |
| `propose_change_set` | `ProposeChangeSet` | Line only; covering mandate/authority, submits without accepting |
| `settle_change_set` | `SettleChangeSet` | Line only; effective authority and one covering settle mandate with its declared fallback |

The remaining graph kinds have no Sequence step:

| Graph kind absent from Sequence | Raw graph model | Served lane / requirements |
|---|---|---|
| `state_claim` | `ClaimTapNode` | Direct or Line; exact ClaimType and named Subject, bounded selection |
| `repeat` | `RepeatNode` | Transform-only body: direct or Line; provider-bearing body: Line only; bounded attempts |
| `select` | `SelectNode` | Direct or Line; join mutually exclusive producers |
| `constant` | `ConstantNode` | Direct or Line; literal record, dollar-prefixed strings remain data |
| `return` | `ReturnNode` | Direct or Line; explicit typed successful leaf |
| `invoke` | `InvokeNode` | Direct or Line subject to the child's closure and terminal authority |
| `exhaust_tap` | `ExhaustTapNode` | No lane yet; fresh authoring refuses `cruxible.procedure.node_not_served` |

For the six served kinds absent from Sequence, use the raw typed
`ProcedureInput` from `cruxible_client.authoring.inputs`: supply graph-format-6
node records in `definition.nodes`, declare `contracts`, then call
`cx.procedure(definition=input).prepare()` or `.submit()`. The corresponding
node models in `cruxible_client.contracts.procedures.models` specify each
record's fields; the daemon resolves authoring references and validates the
resulting `ProcedureDefinition` through the same compiler. Use symbolic
`accepted` / `candidate` references and `carried_contract` references in fresh
inputs; exact artifact pins and digests belong to accepted graphs. The tagless
wire fallback is that same `ProcedureInput` JSON via `authoring compile` or
`authoring submit --dry-run`. This adds no execution implementation.

Sequence's global `returns` alias and a returning value-producing leaf remain
available, but there is no explicit `Return` step. These six missing step
classes are planned for the kit-authoring phase; they are not Sequence support
today. `exhaust_tap` has no runnable fallback, including on a Line; retained
artifacts still read and verify. Open provider slots belong to a Blueprint,
which must be instantiated before running.

In a v3 Claim proposal item, name a one-key Subject with
`{"subject_kind": "sec.package", "key": "$item.package"}`. The same reference
works for a Subject-valued object (`kind: "subject_key"`, `subject: {...}`).
For a composite key, map each part by name. A matching Subject item in the
terminal creates or reuses the Subject; the Claim item states the real fact.

Check, then submit:

Submit lands immediately when no approval is required and the caller may
activate. Use `--no-accept` to keep a proposal; a stop names its reason and next step.

```bash
cruxible authoring submit PROCEDURE.json --dry-run
cruxible authoring submit PROCEDURE.json
cruxible get Procedure:NAME        # runnable: direct, line, or unsupported
```

## Phase 3: Run it directly

When `runnable` is `direct`:

```bash
cruxible procedure run NAME INPUT.json      # or - for stdin
cruxible get ProcedureRun:RUN-…             # state, outcomes, terminal detail
```

A direct run supports typed returns and halt. Outward terminals
(`emit_capture`, `post_inbox`, `propose_change_set`, `settle_change_set`) and
provider-bearing Repeat bodies require a Line; inspect `runnable` first.

## Phase 4: Put it on a Line

A Line runs the Procedure under its own parameters (the Procedure's input),
budgets, authority ceiling (`max_authority`) and acquisition policy:

```bash
cruxible authoring example line          # a Line over the example Procedure
cruxible authoring submit LINE.json
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

To let a settling Procedure earn trust first, start its Line at
`max_authority: propose` with a propose mandate: its settle terminal then
proposes instead (`fallback_reason`
`cruxible.settle.authority_capped_by_line_max_authority`), and people review
each result. To graduate, accept a Line successor with `max_authority: settle`
and a covering settle mandate; the Procedure is unchanged, so its track record
carries over. Trust can also graduate per ClaimType: a settle mandate whose
scope names only the ClaimTypes you trust settles those, and a change that also
carries another ClaimType proposes instead (`fallback_reason`
`cruxible.settle.outside_mandate_scope`, naming it in
`outside_scope_claim_types`).

## Phase 5: Schedule and enable it

```bash
cruxible authoring example trigger       # an hourly cron Trigger for the example Line
cruxible authoring submit TRIGGER.json
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

- After a daemon restart, time it was down is not matched: when something was
  due in it, `next` shows a `line_coverage_gap` row with the exact `cruxible
  line evaluate LINE --since S --until U`, which records every tick the range
  skipped as pending work (a range with nothing due is never a row, and an
  evaluation that finds nothing reports `not_met` and succeeds), and
  `line_work_pending` while work waits for `cruxible line dispatch LINE`.
- A settling Line whose settled items match earlier refusals (a retired Claim,
  or a rejected proposal, of the same statement on the same evidence) shows as
  `line_settled_prior_refusals`. Read the runs it names, then fix the
  Procedure or demote the Line to `max_authority: propose` with a Line
  successor (`cruxible authoring example line`).
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
cruxible procedure dispose NAME RESOLUTION_ID --verdict upheld --reason "Evidence checked."
```

Find resolution IDs in measure/readings or `get Procedure:NAME`. A reviewer can
record `upheld` or `overturned` with a reason under `GOVERNED_WRITE`; Cruxible
records the authenticated principal and the judgment without interpreting it.
The full-profile MCP tool is `cruxible_procedure_dispose`; the SDK method is
`cx.accepted_procedure(NAME).dispose_resolution(ID, verdict=..., reason=...)`.
An exact retry reuses the judgment. Get/history retain reviewer, reason and
verdict. Overturn reopens the latest answer's contract; measure can resolve it
again inside the original window, while expired windows stay expired. `next`
does not invent judgments or automatically remeasure.

A completed run does not prove the Procedure worked, and a failed run does not
disprove it; the measurement's verdict comes from its evidence.

## Phase 7: Hand off

Proposals a Line produces are ordinary proposals: review, approve and activate
them per `../_shared/references/governance-flow.md`. A Line keeps one open
proposal per terminal: a rerun with the same change reports it still current
and proposes nothing; a rerun with a different change withdraws it as
superseded by the new one, so review the newest. Tell the user which Lines
are enabled, under whose credential, on which schedule, with which mandate,
and which `next` rows to watch.
