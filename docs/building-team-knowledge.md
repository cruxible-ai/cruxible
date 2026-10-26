# Building team knowledge

A team's knowledge comes in two shapes. Some of it is facts the system has to
work with: which parts fit which vehicles, who owns a decision, whether it
still stands. Some of it is reasoning people read whole: why a choice was made,
what was tried, what a report found. Cruxible holds the first kind as typed
state and leaves the second as prose in your documents, and the most useful
knowledge is the two connected: a typed fact that cites the passage explaining
it.

This guide is about deciding which is which, connecting them, and the modeling
habits that keep typed state useful as it grows. It uses the same car parts
example as [Concepts](concepts.md#knowledge-as-claims): everyone already knows
how parts relate to cars, so the modeling choices are easy to see. The same
choices apply to whatever your team works on.

## Typed state or prose

Make something **typed state** when the system has to do something with it:

- **ask across items**: filter, count, sort and follow relations ("which
  brake parts fit the 2019 Civic?");
- **enforce vocabulary**: a category from a closed list, a "fits" link that
  must point at a real vehicle;
- **check consistency mechanically** across links;
- **coordinate writers**: changes that land together or not at all, and
  writes that refuse when the value moved since you read it;
- **keep per-field history and attribution**: who changed a part's category,
  when, and why;
- **drive behavior**: `next` rows, Lines, Triggers, and staleness of the
  documents that summarize it.

Keep it **prose** when it is read whole and revised whole: reasoning and
design rationale, trade-offs and rejected options, narrative, reports,
checkpoints. Storing those in fields only produces values too long to read and
too vague to query.

Cruxible doesn't need to hold everything your team knows, and it isn't the
source of truth: your catalogs, databases and documents are. Model what your
questions need, as they come up, drawn from those sources and citing them. A
model is never complete, and doesn't need to be.

## The bridge: a fact and the passage behind it

The usual right shape is both at once. "Decision: stock ceramic pads for the
2019 Civic" is a field; "because they last longer in city driving, and Acme
lists them as the replacement" is a section of a document the field cites. A
single `write` puts the section in the document and records the Claims that
rest on it, so neither lands without the other:

~~~python
from cruxible_client import Cruxible

cx = Cruxible.connect()
batch = cx.changes(because="Brake pad choice for the 2019 Civic.",
                   subject="catalog.decision/civic-brake-pads")
batch.append_section("docs/decisions.md", under=("Decisions",), id="why", text=(
    "## Ceramic pads for the 2019 Civic\n\n"
    "We stock ceramic pads (bp-2210) rather than organic ones for the 2019\n"
    "Civic: they last about twice as long in city driving, and Acme lists them\n"
    "as the replacement for bp-1142.\n"
))
why = batch.section("why")
batch.set("summary", "Stock ceramic pads (bp-2210) for the 2019 Civic", evidence=why)
batch.set("status", "accepted", evidence=why)
batch.add("governs", "catalog.vehicle/honda-civic-2019", evidence=why)
outcome = batch.write()
outcome.status                                    # 'accepted'
outcome.subjects_added                            # ('catalog.decision/civic-brake-pads',)
[(c.field, c.after, c.verdict) for c in outcome.changes]
# [('summary', 'Stock ceramic pads (bp-2210) for the 2019 Civic', 'supported'),
#  ('status', 'accepted', 'supported'),
#  ('governs', 'catalog.vehicle/honda-civic-2019', 'supported')]
~~~

`batch.section("why")` cites the section the same write appends, and
`subject=` names the decision once for every change. On the CLI, `cruxible
write FILE` takes the same write as a YAML file. For MCP-only clients, it is
one `cruxible_write` call:

~~~json
{
  "because": "Brake pad choice for the 2019 Civic.",
  "subject": "catalog.decision/civic-brake-pads",
  "docs": [
    {
      "op": "append",
      "id": "why",
      "file": "docs/decisions.md",
      "under": ["Decisions"],
      "text": "## Ceramic pads for the 2019 Civic\n\nWe stock ceramic pads (bp-2210) rather than organic ones for the 2019\nCivic: they last about twice as long in city driving, and Acme lists them\nas the replacement for bp-1142.\n"
    }
  ],
  "changes": [
    {
      "op": "set",
      "field": "summary",
      "value": "Stock ceramic pads (bp-2210) for the 2019 Civic",
      "evidence": {"kind": "section", "ref": "why"}
    },
    {
      "op": "set",
      "field": "status",
      "value": "accepted",
      "evidence": {"kind": "section", "ref": "why"}
    },
    {
      "op": "add",
      "field": "governs",
      "value": "catalog.vehicle/honda-civic-2019",
      "evidence": {"kind": "section", "ref": "why"}
    }
  ]
}
~~~

The reasoning stays where people read it, and the decision becomes something
you can query, link and review. The connection also keeps them honest: when
someone later edits the reasoning, every Claim citing it comes back for
review.

~~~text
$ cruxible next
repair  citation_drifted  Claim:CLM-55627160…  next=cruxible.rebind
  repair: cruxible rebind CLM-55627160… --citation b9091755e6c0 --section 'decisions#Decisions/Ceramic pads for the 2019 Civic'
  or: cruxible retire CLM-55627160…
...
~~~

Whether the decision still holds after the edit is a judgment for a person or
an agent, not for Cruxible. If it does, re-bind each Claim to the edited
section (the repair, plus a `--because`); if it doesn't, retire or revise
them. [Keeping knowledge current](keeping-knowledge-current.md)
covers working through rows like these. More on writing documents and citing
them is in [Documents and notes](documents-and-notes.md).

## Modeling habits

The examples below use this vocabulary:

| Kind | Field | Value | How many |
|---|---|---|---|
| `catalog.part` | `title` | text | one |
| | `category` | one of `brakes`, `filters`, `wipers`, `lighting` | one |
| | `fits` | a `catalog.vehicle` | many |
| | `replaces` | a `catalog.part` | many |
| `catalog.vehicle` | `make`, `model` | text | one |
| | `year` | integer | one |
| `catalog.decision` | `summary` | text, one line | one |
| | `status` | one of `proposed`, `accepted`, `superseded` | one |
| | `governs` | a `catalog.part` or `catalog.vehicle` | many |
| | `amends` | a `catalog.decision` | many |
| `catalog.note` | `title` | text, one line | one |
| | `about` | a `catalog.part` or `catalog.vehicle` | many |

### Name things by stable IDs, and look before you mint

A Subject's id should stay right for as long as the thing exists: a part
number, a durable identifier from the source, or a slug you won't need to
change, never a title that might be reworded. Before creating a new Subject,
look for the one that may already exist: `get` the exact `kind/id`, grep the
floor for likely names, and `query KIND --contains TEXT` for near matches. Two
Subjects for one thing split everything known about it in half.

### Pick value types early

Changing a field's value type later can't carry the Claims already written
under it, so decide up front. Use an **enum** for a closed list whose members
have no facts of their own: a part's `category`. Use an **edge** (a field
whose value is another Subject) when the value has facts of its own: a part
`fits` a vehicle, and the vehicle has a make, a model and a year.

### Choose how many by whether several values are true at once

A field holds **one** value when only one is true at a time (a part's title, a
decision's status); `set` replaces it. It holds **many** when several are true
together (the vehicles a part fits); `add` adds to them. The wrong verb is
refused before anything lands:

~~~text
$ cruxible set catalog.part/bp-1142 fits catalog.vehicle/honda-civic-2019 --because "…"
refused (generation 18, f31713020dd9)
cruxible.write.field_is_many (change 0): fits holds many values, so set has nothing to replace
  repair: Use add to add a value, or retire one first

$ cruxible add catalog.part/bp-1142 title "Pads" --because "…"
refused (generation 18, f31713020dd9)
cruxible.write.field_is_single (change 0): title holds one value, so add would contend with it
  repair: Use set to replace the value (contend: true to contest it)
~~~

### Anything you will ask about is an edge, never prose

If a question will ever be "which parts fit this car?" or "which notes are
about this part?", the answer has to be an edge, not a sentence inside a
description. Edges can be followed in either direction. `--follow-in` walks an
edge backwards, so you never maintain a reverse list by hand:

~~~text
$ cruxible query catalog.part --follow-in catalog.note.about:note --select title,note.title
subject               title             note.title                                flags
catalog.part/bp-1142  Front brake pads  Fitment check of bp-1142 on a 2019 Civic  -
~~~

See [Asking questions](asking-questions.md) for following edges and
everything else `query` does.

### Keep one current field; put history in notes

A field says what is true now. When you need a trail of findings,
checkpoints or reports about something, make each one a note Subject with an
`about` edge to what it concerns, rather than appending to a field or a
description. Each note has its own title, its own evidence and its own
history, and the edge makes "every note about this part" one query.

### Make decisions Subjects, and amend rather than edit

A decision is a Subject with a short summary, a status and edges to what it
governs, citing the reasoning behind it. When the team changes its mind, record
a new decision that `amends` the old one and mark the old one superseded, so
both the current answer and the path to it stay readable:

~~~text
$ cruxible query catalog.decision --select summary,status,amends
subject                                    summary                                          status      amends                             flags
catalog.decision/civic-brake-pads-2026-11  Ceramic pads for fleet Civics only               accepted    catalog.decision/civic-brake-pads  -
catalog.decision/civic-brake-pads          Stock ceramic pads (bp-2210) for the 2019 Civic  superseded  -                                  -
~~~

### Keep measured values out of titles

A price, a stock level or a measured wear rate changes. Written into a title
or description, it stays there long after it stops being true, and people
keep quoting it. Give a measured value a field of its own, resting on the
evidence it was measured from, which carries the date: the latest value is
then the one everybody reads, and its history keeps the earlier ones. When a
Procedure measures something about its own runs, that measurement belongs in
the Procedure's readings (see [Lines and
automation](lines-and-automation.md#earning-settle)).

### Use the normative role for what you state yourself

A field's evidence rules are set per role. Things a person or agent states
on their own authority, such as decisions, owners and categories assigned by
the team, are **normative**, and their own words are the evidence. Facts
about the outside world are **observations**, and should rest on what was
observed. A field whose rules don't admit the role you write with records
every value as `uncovered`. So check the verdict on every write: anything other
than `supported` is a signal. Here a `supplier` field was defined to allow only
observations, while its rules admit the writer's own words only for normative
statements:

~~~text
$ cruxible set catalog.part/bp-1142 supplier Acme --because "We buy it from Acme."
accepted (generation 24, d6b405f15016)
  set catalog.part/bp-1142 supplier: Acme  [CLM-af67bbf9…; verdict uncovered]
warning cruxible.write.verdict_not_supported: catalog.part/bp-1142 supplier was written, but its verdict is uncovered: it is a observation Claim, and the field's evidence rules admit only normative. Write it in an admitted role, or change the ClaimType's evidence rules.
~~~

The mismatch shows earlier too: proposing such a field reports the lint
`cruxible.claim_type.permitted_role_unadmitted`.

[Evidence and admission](evidence-and-admission.md) explains roles, rules and
verdicts in full.

### Avoid reserved field names

Six short names are reserved for the columns every read carries: `subject`,
`subject_id`, `kind`, `predicate`, `claim` and `flags`, and anything starting
with `value.`. A field you name `kind` works, but always reads under its full
predicate (`catalog.part.kind`), never as `kind`. Proposing one reports the
lint `cruxible.claim_type.field_name_reserved`; pick another name.

### Write a checkpoint as one change

When an agent finishes a piece of work, it should record what it found as one
`write`: one reason, every change together, every outcome reported at once. A
half-recorded checkpoint is worse than none.

### Preview, then pin

To change something you just read, preview the change with `--dry-run`, then
commit it at the coordinate the preview names. If someone else changed the
same field in between, the write refuses instead of silently overwriting
them:

~~~text
$ cruxible set catalog.part/bp-1142 title "Front brake pads, organic" --because "…" --dry-run
would accept (generation 25, a4cd9ab8aff0)
  set catalog.part/bp-1142 title: Front brake pads -> Front brake pads, organic  [CLM-…; verdict supported]
next: Run the same command without --dry-run, adding --at a4cd9ab8aff0

$ cruxible set catalog.part/bp-1142 title "Front brake pads, organic" --because "…" --at a4cd9ab8aff0
refused (generation 26, bcb9f74ad949)
cruxible.write.slot_changed (change 0): catalog.part/bp-1142 title changed after your read: it now holds 'Front pads' as CLM-…, accepted in generation 26 by YOUR_ID
  repair: Re-set it to replace it, or set it with contend: true to contest it
~~~

## Where a vocabulary comes from

You can install a vocabulary someone else built as a [kit](kits.md), extend or
revise it like anything you authored yourself, or define your own fields. A
field needs only its name, the kinds it applies to, what it holds, and how
many values a Subject has:

~~~yaml
predicate: catalog.part.warranty_months
allowed_subject_kinds: [catalog.part]
object_kind: literal
literal_schema: {type: integer, minimum: 0}
cardinality: one
~~~

~~~text
$ cruxible claim-type propose --input warranty.yaml --name add-warranty
admitted  sha256:a1a00ee1…  verdict=candidate
landed: accepted at dfe3a8309a22
~~~

Defined this way, a field is normative and backed by its writer's own words;
[Evidence and admission](evidence-and-admission.md#writing-a-fields-rules)
shows how to require other evidence. [Evolving your
vocabulary](evolving-your-vocabulary.md) covers defining and changing fields
safely. Either way, start from the
questions your team needs answered, and add fields as those questions arrive.

## Mistakes we made ourselves

Cruxible's own project knowledge is kept in Cruxible, and we made each of these
mistakes there first. Here is what happened, and the same mistake avoided in
the car parts example.

| What happened to us | The fix, in car parts |
|---|---|
| **Paragraphs in fields.** Roadmap titles and reconciliation notes ran to hundreds of characters: unreadable in every view, and nothing in them could be cited. | A part's `title` is a short name. Why it was chosen, or what a fitment check found, is a section of a document the Claim cites. |
| **A number in a title.** A timing ("62 s to 92 s") sat in a title. A later measurement of 4.7 s never replaced it, and the old number kept being quoted as current. | A supplier's price is a field of its own, resting on the supplier's quote, never part of the part's title. |
| **Relations in prose.** Which items a ruling covered was written only inside the ruling's text, so nothing listed them, and five items sat unbatched for a day. | Which vehicles a part fits is a `fits` edge per vehicle, so "what fits the 2019 Civic?" is a query. |
| **Uncited reports.** Reports and audits were kept as loose Markdown, so decisions couldn't point at what they were based on. | A fitment check is a section of a document, written in the same change as the note or decision that cites it. |
| **Trusting stale summaries.** Agents acted on rendered summaries that had fallen behind the state they summarized. | Before relying on a summary of a vehicle's parts, check `next` (or the floor's `projections/INDEX`); if it's stale, read the Claims with `get`. |
| **The wrong role for self-stated facts.** A field allowed only the observation role, while its rules admitted own words only for normative statements, so every write landed `uncovered`. | A decision's `status` permits the normative role and admits the writer's own words; the verdict on every write is checked. |

## Where to go next

- [Evidence and admission](evidence-and-admission.md): what counts as evidence
  for a field, and how verdicts come out.
- [Documents and notes](documents-and-notes.md): writing documents and citing
  them.
- [Asking questions](asking-questions.md): queries, edges and history.
- [Kits](kits.md): installing and extending a vocabulary someone else built.
