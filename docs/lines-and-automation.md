# Lines and automation

Some upkeep is the same every time: re-reading a supplier's list of
discontinued parts, re-fetching a price that expires, checking a feed for
new notices. Nobody should have to remember to do it, and nobody should have
to trust a script that writes straight into what the team knows. In
Cruxible, a **Line** does that work. It runs a fixed, checked **Procedure**
on a schedule or when something lands. What it finds arrives the way anyone's
change does: as a proposal citing the evidence it read, reviewed under your
approval policy. It keeps doing that until it has earned the right to land
changes itself.

This guide covers the pieces of automation, one worked example from writing
the Line to letting it settle on its own, the inbox for findings a Line
can't decide, and recovering work the daemon missed. It uses the car parts
example from [Concepts](concepts.md#knowledge-as-claims).

## The pieces

| Piece | What it is |
|---|---|
| **Procedure** | A fixed graph of steps: read accepted state, read a source (keeping what it read as a Capture), transform, and end by posting to the inbox, proposing a change or settling one. Every provider it calls is pinned, so a run can be replayed and explained. |
| **Line** | A Procedure with its inputs, its budgets and an authority ceiling (`max_authority`): `observe`, `propose` or `settle`. |
| **Trigger** | When a Line runs: a cron schedule, a cadence, a Capture landing, a window closing, or a generation being accepted. |
| **Mandate** | The grant that lets a Procedure propose, or settle within a scope. |
| **Enablement** | Your consent for a Line to run on its Triggers. |

A Procedure never reads the clock. Time enters only through Triggers, so
"every morning" is a cron Trigger, and the same run on the same inputs gives
the same result. Procedures are written once, as a typed `ProcedureSpec`
(or with the SDK's `Sequence` builder over it), or come ready-made in a kit.

## A worked example: discontinued parts

Acme publishes the part numbers it has discontinued, and the team keeps a copy
in the repository at `supplier/acme-discontinued.txt`, one part number per
line. The Procedure `catalog.acme-discontinued` reads that file and records
`discontinued: true` for each part it lists, citing the file as it read it:

```mermaid
flowchart LR
    file["supplier/acme-discontinued.txt"] -->|read, kept as a Capture| shape["One item per line:<br/>catalog.part/ID discontinued: true"]
    shape --> term["Settle, or propose<br/>while capped"]
    term -->|proposal| review["Your review"]
    term -->|under a settle mandate| state[("Accepted state")]
    review --> state
```

Its last step settles. While its Line is capped at `propose`, that same step
proposes instead, so one Procedure serves the Line before and after it
graduates.

### Write the Procedure

A Procedure is written once, in the one typed authoring form (a
`ProcedureSpec`), and submitted like any definition. The SDK's `Sequence` is a
thin builder over that spec: each step returns a handle, and `handle["field"]`
is a typed reference to one of its fields. This one reads the file, extracts
its records, and ends in a settle step with one item per record
(`item(...)` is the current record):

~~~python
from cruxible_client.authoring.procedures import Sequence, gt, halt, item

procedure = Sequence(
    "catalog.acme-discontinued",
    input={"path": "string"},
    output={"listed": "int"},
    # The slot's signature; a Line binds it to the built-in workspace.file read.
    slots={"read": {"in": "workspace.file.request@1", "out": "workspace.file.result@1",
                    "effect": "external_read"}},
)
listing = procedure.call(
    "listing", slot="read", captures={"capture": "catalog.supplier-file-v1"},
    input={"logical_source": "acme.discontinued", "relative_path": procedure.input["path"]},
)
parts = procedure.extract(
    "parts", capture=listing["capture"], records="/*", row="catalog.part-line@1",
    key={"part": {"at": "part"}}, fields={},
)
listed = procedure.transform("listed", kind="aggregate",
                             spec={"input": parts["records"], "op": "count"})
procedure.guard(gt(listed["result"], 0), else_=halt("empty"))
summary = procedure.project("summary", fields={"listed": listed["result"]})
procedure.settle(
    ({"tag": "playbill-procedure-claim-proposal-item-v3",
      "statement": {"tag": "playbill-procedure-claim-statement-v1",
                    "subject": {"subject_kind": "catalog.part", "key": item("key", "part")},
                    "predicate": "catalog.part.discontinued",
                    "object": {"kind": "literal", "value": True}, "role": "observation"},
      "rationale": "Listed in Acme's discontinued-parts file."},),
    result=summary.whole,
)

cx.changes(rationale="The Acme discontinued-parts check.") \
    .procedure(definition=procedure).submit()
~~~

`procedure.spec()` is the exact spec every surface submits; `get` shows an
accepted Procedure in the same form, each typed reference rendered as its
`$...` text for review.

### Write the Line, its Trigger and its mandate

A Line, its Triggers and its mandate are definitions like any other, so they
go in through authoring as one change set, saved here as `automation.json`:

~~~json
{
  "kind": "change_set",
  "rationale": "Check Acme's discontinued-parts list every morning; propose what it lists for review.",
  "members": [
    {
      "kind": "line",
      "name": "catalog.acme-discontinued",
      "procedure_name": "catalog.acme-discontinued",
      "acquisition_policy_name": "catalog.acquisition",
      "max_authority": "propose",
      "parameters": {"path": "supplier/acme-discontinued.txt"}
    },
    {
      "kind": "trigger",
      "name": "catalog.acme-discontinued-daily",
      "line_name": "catalog.acme-discontinued",
      "schedule": {"kind": "cron", "expression": "0 6 * * *"}
    },
    {
      "kind": "procedure_mandate",
      "name": "catalog.acme-discontinued-propose",
      "procedure_name": "catalog.acme-discontinued",
      "grants": "propose",
      "namespace": ["claims"],
      "valid_from": "2026-10-21T00:00:00Z",
      "expires_at": "2027-01-19T00:00:00Z"
    }
  ]
}
~~~

~~~text
$ cruxible authoring submit automation.json --brief
outcome: accepted
...
~~~

The `parameters` are the Procedure's input, and `acquisition_policy_name`
names the rules for its sources (how old a reading may be, and what to do when
one is missing). Cron schedules are always UTC: 06:00 UTC, here. `cruxible
authoring example line`, `trigger` and `procedure-mandate` print templates.

### Run it once

`line run` runs the Line now, whether or not it's enabled:

~~~text
$ cruxible line run catalog.acme-discontinued
RUN-f4b3d8bd…: succeeded
Result: {"listed": 2}
Read list: supplier/acme-discontinued.txt (14 bytes, sha256:ef7766d0…)
Capture list: sha256:75f0fda7…
Proposal settle: sha256:84437408… candidate sha256:14fb95b1… (settle fell back: cruxible.settle.authority_capped_by_line_max_authority)
~~~

Its Line is capped at `propose`, so the settle step proposed. The proposal
reads like anyone's, with the run that made it and each item's reason:

~~~text
$ cruxible proposal review sha256:84437408…
...
Why:
  Proposed by Procedure catalog.acme-discontinued run RUN-f4b3d8bd… terminal settle
Changes (2):
  set catalog.part/af-208 discontinued: true  [CLM-79c37658…]
  set catalog.part/wb-330 discontinued: true  [CLM-1858ef00…]

Item reasons (2 of 2):
  ...
    Listed in Acme's discontinued-parts file.
~~~

Once approved, each Claim cites what the run read, not the file as it is
now:

~~~text
$ cruxible get CLM-79c37658… --detail evidence
value: true
capture CAP-a175ef6cc924  CaptureContract:catalog.supplier-file-v1 v1  source=acme.discontinued  observed=2026-10-21T19:41:54.870243+00:00  evidence: admitted
rationale: Listed in Acme's discontinued-parts file.
~~~

A later run that finds nothing new proposes nothing, and says so:

~~~text
$ cruxible line run catalog.acme-discontinued
...
Terminal settle: nothing changed (2 items restate accepted state)
~~~

A Line keeps at most one open proposal: a run whose change differs replaces
it.

## Running on a schedule

A Trigger does nothing until the Line is enabled:

~~~text
$ cruxible line enable catalog.acme-discontinued
Line:catalog.acme-discontinued: enabled
Enabled by: YOUR_ID at 2026-10-21T19:46:01.895912+00:00
Matched through: 2026-10-21T19:46:01.895912+00:00
Pending: 0 automatic, 0 awaiting explicit dispatch
~~~

Enabling is consent to run on the Line's current definition and Triggers.
Changing either stops the enablement until someone enables it again, and
`next` says so:

~~~text
$ cruxible next
Status: line enablements stalled=0 stopped=1  next=cruxible orient --section lines
repair  consumer_stalled  Line:catalog.acme-discontinued  next=cruxible.line.enable
  repair: cruxible line enable catalog.acme-discontinued
~~~

Triggers fire only forward: a new Trigger never runs for times before it was
accepted, and enabling a Line never runs ticks it missed. `line disable`
stops it; a run already going finishes. `cruxible get Line:NAME` shows its
Triggers, enablements and recent runs, and `get ProcedureRun:RUN-…` reads one
run step by step.

| Trigger | Fires | Schedule |
|---|---|---|
| Cron | At each instant of a five-field expression, in UTC | `{"kind": "cron", "expression": "0 6 * * *"}` |
| Cadence | Every interval | `{"kind": "cadence", "interval_seconds": 3600}` |
| Capture landing | When a Capture lands under a contract, such as one a fetching Line just retained | `{"kind": "capture_landing", "capture_contract": "NAME"}` |
| Window close | When a window after such a landing closes | `{"kind": "window_close", "window": {"kind": "capture_event", "capture_contract": "NAME", "duration_seconds": 86400}}` |
| Generation accepted | When accepted state changes | `{"kind": "generation_accepted"}` |

A Capture-landing Trigger is how a fetching Line hands off to a parsing Line:
one reads the source on a schedule, and the other runs on what it read. The
daemon's own housekeeping (floor refresh, evidence sweeps) also runs on
Triggers; see [Internal triggers](cli-reference.md#internal-triggers).

## What a Line may do

A Line's `max_authority` caps it, and anything past observing also needs a
mandate for its Procedure:

- **observe**: read and retain what it read, and post to the inbox. No mandate.
- **propose**: propose changes, which wait for review like anyone's. Needs a
  propose mandate.
- **settle**: land changes without approval. Needs a settle mandate, which
  names the fields and kinds of change it covers and a condition each target
  must meet ([Review and governance](review-and-governance.md#delegating-to-automation)
  has the rules).

A Line without the mandate it needs refuses to enable. Mandates expire, so
renew them before they do.

## Earning settle

Settling is earned, not assumed. A Procedure can declare **measurements**,
checks that come due after it's accepted, so its record builds up as it runs.
`catalog.acme-discontinued` declares one: a week after acceptance, the saved
query of discontinued parts that someone has contradicted must be empty.

~~~text
$ cruxible procedure measure catalog.acme-discontinued --run-id RUN-f4b3d8bd…
nothing-contradicted: resolved (satisfied, RSR-2c4907ed…) reading=recorded PRD-4e96a550…

$ cruxible procedure readings catalog.acme-discontinued
nothing-contradicted: resolved readings=1 (satisfied, RSR-2c4907ed…)
PRD-4e96a550… nothing-contradicted procedure_unit satisfied run=RUN-f4b3d8bd…
~~~

When the readings justify it, **graduate** the Line: grant a settle mandate
and raise the Line to `settle`, in one change set reviewed like any other.
The mandate here covers only `discontinued`, and its condition, the saved
query `catalog.part_categorized`, holds only for parts the team has given a
category:

~~~json
{
  "kind": "change_set",
  "rationale": "Graduate the Acme discontinued check to settle: a week of runs, nothing contradicted.",
  "members": [
    {
      "kind": "procedure_mandate",
      "name": "catalog.acme-discontinued-settle",
      "procedure_name": "catalog.acme-discontinued",
      "grants": "settle",
      "namespace": ["claims"],
      "valid_from": "2026-10-21T00:00:00Z",
      "expires_at": "2027-01-19T00:00:00Z",
      "scope": [
        {
          "claim_type": "catalog.part.discontinued",
          "change_kinds": ["create", "revise"]
        }
      ],
      "condition": {
        "query_name": "catalog.part_categorized",
        "binding_parameter": "part_id",
        "required_fields": ["category"],
        "fallback": "propose"
      }
    },
    {
      "kind": "line",
      "name": "catalog.acme-discontinued",
      "procedure_name": "catalog.acme-discontinued",
      "acquisition_policy_name": "catalog.acquisition",
      "max_authority": "settle",
      "parameters": {"path": "supplier/acme-discontinued.txt"}
    }
  ]
}
~~~

The new Line version stops the old enablement, so enable it again. The
Procedure doesn't change, so its record carries over. Now a run lands its
change itself, and history still says where each value came from:

~~~text
$ cruxible line run catalog.acme-discontinued
...
Settled settle: accepted d0f3a27bdecf… (proposal sha256:dcc462a0…)

$ cruxible get catalog.part/bp-2210 --detail history --field discontinued
...
discontinued
  seq 30  d0f3a27bdecf  2026-10-21T19:47:04.647060Z  by YOUR_ID  stated  true  [CLM-75027a8f…]
      reason: Listed in Acme's discontinued-parts file.  (Procedure Procedure:catalog.acme-discontinued, run RUN-2fdbb704…)
~~~

When the list names a part the condition doesn't hold for (here `sp-77`, which
has no category yet), the run follows the mandate's fallback, and the whole
change goes to review: the new values, plus a refresh of the evidence of the
parts already recorded, since the file they rest on changed:

~~~text
$ cruxible proposal review sha256:…
...
Changes (4):
  set catalog.part/bp-2210 discontinued: true  [CLM-74d21b57…]
  refresh evidence catalog.part/wb-330 discontinued: true (value unchanged)  [CLM-ab549499…]
  set catalog.part/sp-77 discontinued: true  [CLM-b8d73b81…]
  refresh evidence catalog.part/af-208 discontinued: true (value unchanged)  [CLM-c31e37b5…]
~~~

Delegate facts copied verbatim from a source, like this list. Keep judgments
for review: whether a part still fits a car stays with a person.

## The inbox: findings a Line can't decide

A rule of thumb: propose when the automation can state the belief, post when
it can't. Acme also sends notices, such as recalls and price changes, and
deciding what a notice means for the catalog is a judgment. An observe-only
Line, `catalog.acme-notices`, posts each notice to the **inbox** instead:

~~~text
$ cruxible next
repair  line_inbox_unread  Line:catalog.acme-notices  next=cruxible.inbox.list
  repair: cruxible inbox list --line catalog.acme-notices --status unread

$ cruxible inbox list --line catalog.acme-notices --status unread
STATUS  ITEM  LINE  SEEN  LAST_POSTED
unread  INB-b73819240332  Line:catalog.acme-notices  -  2026-10-21T19:46:06.044545+00:00
  {"notice": "2026-10-20 Recall: bp-2210 ceramic pads, batch 4471, may delaminate under heavy braking."}
unread  INB-0152266fe7d8  Line:catalog.acme-notices  -  2026-10-21T19:46:06.044545+00:00
  {"notice": "2026-10-21 Price change: all wiper blades up 5% from 2026-11-01."}
~~~

`inbox get ITEM` shows the whole item and the run and Capture behind it. The
inbox is never accepted state. Judge each item and record what you decided:
write what you concluded and `link` the item to it, `dismiss` it with a reason,
or `ack` it as seen:

~~~python
from cruxible_client import Cruxible

cx = Cruxible.connect()
recall = (
    cx.changes(because="Acme's recall notice of 2026-10-20: bp-2210 batch 4471.",
               subject="catalog.note/acme-recall-bp-2210-4471")
    .set("title", "Acme recall: bp-2210 batch 4471 may delaminate")
    .add("about", "catalog.part/bp-2210")
    .write()
)
[(c.field, c.after, c.verdict) for c in recall.changes]
# [('title', 'Acme recall: bp-2210 batch 4471 may delaminate', 'supported'),
#  ('about', 'catalog.part/bp-2210', 'supported')]
~~~

For MCP-only clients, the same write is one `cruxible_write` call:

~~~json
{
  "because": "Acme's recall notice of 2026-10-20: bp-2210 batch 4471.",
  "subject": "catalog.note/acme-recall-bp-2210-4471",
  "changes": [
    {
      "op": "set",
      "field": "title",
      "value": "Acme recall: bp-2210 batch 4471 may delaminate"
    },
    {"op": "add", "field": "about", "value": "catalog.part/bp-2210"}
  ]
}
~~~

Then link the item to the note, and dismiss the other:

~~~text
$ cruxible inbox link INB-b73819240332 --subject catalog.note/acme-recall-bp-2210-4471
linked  INB-b73819240332  Line:catalog.acme-notices  -  2026-10-21T19:46:06.044545+00:00

$ cruxible inbox dismiss INB-0152266fe7d8 --reason "Prices are re-quoted from the November price list."
dismissed  INB-0152266fe7d8  Line:catalog.acme-notices  -  2026-10-21T19:46:06.044545+00:00
~~~

When a Line posts the same item again, it is counted as seen again and keeps
your decision, so a Line that re-reads a whole file doesn't ask you to judge
the same rows twice:

~~~text
$ cruxible inbox list --line catalog.acme-notices
STATUS  ITEM  LINE  SEEN  LAST_POSTED
dismissed (+1 since)  INB-b73819240332  Line:catalog.acme-notices  x2  2026-10-22T02:41:25.215491+00:00
...
~~~

An item you linked to a proposal comes back unread if that proposal is
rejected or withdrawn, since your decision no longer stands.

## Recovering missed work

A daemon that was down didn't run what was due meanwhile, and it never runs
missed work on its own. After a restart, `next` lists each gap with the exact
command that covers it:

~~~text
$ cruxible next
repair  line_coverage_gap  Line:catalog.acme-notices  next=cruxible.line.evaluate
  repair: cruxible line evaluate catalog.acme-notices --since 2026-10-21T19:48:40.974240+00:00 --until 2026-10-21T19:51:32.748324+00:00 --dispatch

$ cruxible line evaluate catalog.acme-notices --since 2026-10-21T19:48:40.974240+00:00 --until 2026-10-21T19:51:32.748324+00:00 --dispatch
Line:catalog.acme-notices: met (2 occurrences)
sha256:6c5718bb…: admitted run=RUN-7a89b406…
sha256:8e6dfbe7…: admitted run=RUN-6a46e70d…
~~~

`--dispatch` runs what it finds; without it, `evaluate` only records the
missed occurrences, and `line dispatch` runs them later.

## Automation that comes with a kit

A kit carries definitions, including Procedures, but installing one never
starts automation. By convention, a kit's automation ships as an inert
example change set that you adopt deliberately: copy it, change the Line
names, sources, schedules and mandate expiry to yours, submit it, review it,
and enable each Line. The security kit's `examples/automation.json` is one,
with a README that explains each choice. Its Lines start at `propose`, and
its README shows how to graduate the ones that copy verbatim facts.
Upgrading a kit that changes a Procedure brings its Lines back disabled
(`line_disabled_by_upgrade` in `next`) and its settle mandates back to
propose, so you consent to the new version and re-grant settling.
See [Kits](kits.md#automation-that-comes-with-a-kit).

## Pitfalls

- **A list naming something you don't have.** An item for a Subject the
  instance doesn't hold refuses the run's whole change
  (`proposal_lowering_refused`, `cruxible.subject.key_missing`). Filter out
  rows you don't carry, or have the Procedure create the Subject with a
  Subject item (a settle mandate then needs `subject_creation` for that
  kind).
- **Expecting it to run after a change.** Changing a Line or any Trigger aimed
  at it stops it until you enable it again.
- **Expecting it to catch up.** Missed ticks wait for `line evaluate`; nothing
  runs retroactively.
- **Local times in cron.** Schedules are UTC.
- **Mandates that lapse.** Without a current mandate, a Line's runs that
  propose or settle are refused. Renew mandates ahead of time.
- **Graduating judgments.** Settle facts copied from a source, not
  conclusions drawn from them.

## Where to go next

- [Review and governance](review-and-governance.md): approval policy and the
  rules for mandates.
- [Keeping knowledge current](keeping-knowledge-current.md): the queue Lines
  report into.
- [Evidence and admission](evidence-and-admission.md): why a Capture a run
  fetched counts where your own words don't.
- The [CLI reference](cli-reference.md#line) for every `line`, `procedure`
  and `inbox` option, and the [Python SDK](https://github.com/cruxible-ai/cruxible/blob/main/packages/cruxible-client/README.md)
  for the `Sequence` builder over the typed `ProcedureSpec`.

## Declare Claims as templates

A `claims` terminal declares the Subject key and the ClaimTypes it may write.
The Line's Mandate decides whether the change is proposed or settled. Source
records carry values; they cannot supply Claim IDs, revisions, retirements or
attestations.

```yaml
- id: write
  for_each: {$ref: step, step: entries, path: [records]}
  subject:
    kind: security.vulnerability
    key:
      cve_id: {$ref: item, path: [key, cve_id]}
    create: never
  claims:
    - type: security.vulnerability.kev_listed
      value: true
      role: observation
    - type: security.vulnerability.kev_due_date
      value: {$ref: item, path: [fields, due_date]}
      role: observation
```

Each ClaimType declares its `value` with the Contract field grammar. Its
`precedence.roles` orders roles from highest to lowest;
`precedence.revisable_by_procedures` lists the roles Procedures may replace.
A mapping's optional `replaces` can only narrow that policy. The same extracted
value and record key produce a re-observation of the new Capture, even when
source bytes, the extract specification or the Procedure version change. The
re-observation records the new production receipt; the Claim keeps its original
receipt, the accepted generation stays unchanged, and freshness advances. A one-valued slot revises
its one eligible holder when the value changes; several eligible holders
report `slot_contested`. A protected holder with the same value is a counted,
quiet no-op; a different value reports `held_disagrees` with both values.
A many-valued slot adds values without retiring the others.

Invalid mapped fields are excluded before any proposal and recorded as typed
`extract_refused` findings. Duplicate keys under `duplicates: refuse_key`
produce `duplicate_key` findings with the key and every record locator.
Findings stay in the run report unless the Line explicitly routes their kinds
to its inbox, for example `findings: {inbox: [extract_refused, duplicate_key]}`.
Routing is bounded and deduplicated across identical reruns; omitting
`findings` routes nothing.

A ClaimType declares the members that need complete input, for example
`requires_complete_input: [fixed, not_affected]` with
`incomplete_fallback: unknown`. Only those members downgrade when their data
or control lineage is incomplete; `affected` still writes. Without a fallback,
the write is skipped and reports `incomplete_source`. An incomplete input
never replaces a held Claim with a downgraded value. Boolean types may list
`false` and cannot declare a fallback: incomplete `false` is skipped and
reported as `incomplete_source`. A fallback must be outside
`requires_complete_input`; acceptance refuses either violation with a repair.
Dates, numbers and other types without members cannot declare this policy. Inbox Contract fields use the same member policy. Declaring
`may_truncate: true` on a Procedure slot makes that call's output incomplete.

A written value retains a `claim-evidence-v2` receipt, including the Capture,
extract specification, extractor version, record key and field location,
transform ids and the claims-node template entry index. `get` derives its
value source (field, literal or parameter) from the pinned Procedure template.
Literals and parameters retain the selected row's extraction lineage.
Reshaping, parsing, casts, canonicalization, filtering and projection preserve
that basis. `derive` and `aggregate` compute a `derived` value and require
`role: derivation`; their receipts retain the computing transform ids and
source extraction receipts. Derivations over Claim reads also bind exact
`inputs` from that row's dependency closure. Derived and provider-measured
values remain propose-only until explicit Mandate basis scopes are available.
Cruxible does not infer attestations.

A Claims step can compare a complete enumeration with an explicit Subject
set. Set `covers: $steps.watched.rows` and `enumeration: $steps.entries`
on the step, then choose `when_absent` on each mapping:

| Policy | A covered, held Claim is absent from the source |
|---|---|
| `keep` | Leave it alone. |
| `surface` (default) | Record `claim_not_in_source` in the run report. A Line may route this finding kind to its inbox. |
| `mirror` | Retire an eligible Claim backed by the same logical source, with reason `superseded`. Other-source and protected Claims are surfaced. |
| `inverse` | Write the ClaimType's declared inverse (`{kind: none}` on its unit), under its evidence rules and precedence policy. |

No `covers` means no absence inference. `covers` and `enumeration` are required
together; if the extractor selects `only_keys`, coverage must be a subset.
A missing record or an explicitly absent mapped field can establish absence.
An invalid field, invalid record, duplicate key or false `when` cannot. For a
many-valued slot, each held unit is compared separately.

Both coverage and enumeration must be complete. Otherwise destructive absence
becomes `surface`, with `source_incomplete: true`. A mirror retirement retains
an absence witness: Capture, logical source, extract specification and extractor,
completeness result, enumerated-key digest, and the accepted coordinate used
for coverage. Inverses cite that proof as evidence. A mirror on a type with a
declared inverse retires the positive and writes the inverse. Re-confirming an
inverse records a re-observation; an appearing value revises it into a positive.
Mirror retirements and inverse writes currently require proposal review even
under a settle grant, and appear in `outside_scope_claim_types`.

An incomplete run includes a bounded typed `completeness` summary in its result,
run card and activity entry: `complete: false`, reasons naming the affected
nodes and inputs, and counts of downgraded and skipped writes. A complete run
omits this field. The summary follows the same data and control lineage that
the write checks use.


A derived Claims mapping currently binds whole cardinality-one Claim reads.
Each input must equal the exact read result in the same row's dependency
closure; that closure must cite exactly one Capture. List-valued reads and
multi-Capture derivations are refused. Read one Claim explicitly for each
input; do not substitute an id or a separately fetched version.

`subject.within` is a run-wide Subject set, resolved once before mapping rows;
it must be independent of `$item`. Project the eligible Subjects before `claims`.

List references select an element by position or project explicitly through a
`[*]` path segment. For example, `path: [rows, '[*]', subject_id]` produces a
list of the row's `subject_id` values. An implicit `rows.subject_id` projection
is refused; a `shape` transform is another explicit way to project the list.
