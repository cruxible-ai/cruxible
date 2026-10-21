---
name: automate-with-procedures
description: Automate recurring work in Cruxible - design a Procedure, put it on a Line with Triggers and a mandate, run and enable it, judge what it posts to the inbox, graduate it to settle on its record, and keep it healthy with next.
---

# Automate with Procedures

Use this skill when the same piece of work should run the same reviewed way
every time: re-reading a source, re-checking values that expire, posting
notices someone has to judge.

The source for everything below is [Lines and
automation](https://docs.cruxible.ai/lines-and-automation/), which walks
through one Line from its first run to settling on its own. If a kit already
ships the Procedure or a Blueprint for it, start from that (`adopt-kit`).

## 1. Design the work

Agree with the user, before writing anything:

1. what the Procedure reads (state, named queries) and which sources it reads,
   through which provider (`cruxible orient --section interfaces`; every
   instance has the built-in `workspace.file`);
2. what it ends in: posting to the inbox when it can't state the belief,
   proposing or settling when it can;
3. when it runs: a cron schedule (UTC), a cadence, a Capture landing, a window
   closing, or a generation being accepted;
4. what would show it works: a measurement over a saved query.

A Procedure never reads the clock; time enters only through Triggers.

## 2. Author the Procedure

Write it with the SDK, as a `Sequence` of typed steps or Python source with
`@procedure` ([Procedure source
reference](https://docs.cruxible.ai/procedure-source/)), and submit it like
any definition. Give a Procedure that may one day settle a settle terminal
from the start: while its Line is capped at `propose`, it proposes.

```bash
cruxible get Procedure:NAME        # runnable: direct, line, or unsupported
```

## 3. Put it on a Line

Submit the Line, its Trigger and its mandate as one change set (`cruxible
authoring example line`, `trigger` and `procedure-mandate` print templates),
then run it once by hand and review what it proposes:

```bash
cruxible authoring submit automation.json --brief
cruxible line run LINE
cruxible proposal review PROPOSAL_ID
```

A Line that proposes or settles needs a mandate before it runs; a propose
mandate takes no scope. Start at `max_authority: propose`.

## 4. Enable it

```bash
cruxible line enable LINE
cruxible get Line:LINE             # Triggers, enablements, recent runs
```

Enabling is consent to the Line's current definition and Triggers: changing
either stops it until it's enabled again. It never runs what it missed.

## 5. Judge what it posts

`next` lists `line_inbox_unread`. Read each item (`cruxible inbox get ITEM`),
record what you concluded as Claims or a note, and `inbox link` the item to
it, or `dismiss --reason`, or `ack`. A Line that re-reads the same rows posts
them again, and judged items come back unread.

## 6. Graduate on its record

Let the Procedure's measurements build a record (`cruxible procedure measure
NAME --run-id RUN-…`, `cruxible procedure readings NAME`). When they justify
it, submit a settle mandate (scope plus a condition query) and a Line
successor at `max_authority: settle` in one change set, and enable the Line
again. Settle only facts copied verbatim from a source; keep judgments for
review ([Review and
governance](https://docs.cruxible.ai/review-and-governance/#delegating-to-automation)).

## 7. Keep it healthy

`next` names every problem with its repair: `consumer_stalled` when an
enablement stopped, `line_coverage_gap` after a restart (run the `line
evaluate … --dispatch` it names), `line_inbox_unread`, and proposals to
review. Tell the user which Lines are enabled, on which schedule, under which
mandate (and when it expires), and which rows to watch.
