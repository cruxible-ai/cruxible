# Asking questions

Once a team's knowledge is in Cruxible, people and agents ask it questions:
which parts fit this car, what replaces a discontinued part, what did we
believe last month and why. Every answer comes from accepted state at a named
point in history, the same way every time, with each value's flags beside it
so a weak answer looks weak.

This guide covers the ways to ask: `query` across many things, `get` for one
thing in depth, saved questions (named queries), reading the past, `orient`
for a map, and the floor for grep. It uses the car parts example from
[Concepts](concepts.md#knowledge-as-claims).

## Ask across many things: query

`query` answers with rows: one per Subject of a kind, with the fields you
select, filtered and ordered:

~~~text
$ cruxible query catalog.part --where category=brakes --select title,category,price --order-by title
subject               title                     category  price  flags
catalog.part/bp-2210  Ceramic front brake pads  brakes    -      -
catalog.part/bp-1142  Front brake pads          brakes    42     stale
~~~

`--where` takes `=`, `!=`, `<`, `<=`, `>`, `>=`, `in a,b`, `exists`, and `~`
for text containing a value, and several filters all have to hold. The
`flags` column marks values that are stale, contested, contradicted, uncovered
or held as unsure; here the price's evidence has expired ([Evidence and
admission](evidence-and-admission.md#freshness)).

On a field that holds several values, `=` matches a Subject that has the value
among its values, and `!=` matches one that doesn't have it, including a
Subject with no value at all:

~~~text
$ cruxible query catalog.part --where 'fits=catalog.vehicle/honda-civic-2019' --select title,fits
subject               title                     fits                                                          flags
catalog.part/bp-1142  Front brake pads          catalog.vehicle/honda-civic-2019                              -
catalog.part/bp-2210  Ceramic front brake pads  catalog.vehicle/honda-civic-2019, catalog.vehicle/honda-civ…  -
~~~

Long answers come in pages. A truncated page ends with the exact command for
the next one:

~~~text
$ cruxible query catalog.part --where 'category in brakes,filters' --select title --limit 2
subject               title             flags
catalog.part/af-208   Cabin air filter  -
catalog.part/bp-1142  Front brake pads  -
truncated: 1 more row; next: cruxible query catalog.part --where 'category in brakes,filters' --select title --limit 2 --cursor q2.14ab…
~~~

To search every value of every kind for some text, use `--contains`:

~~~text
$ cruxible query --contains ceramic
subject               kind          predicate           value                     claim      flags
catalog.part/bp-2210  catalog.part  catalog.part.title  Ceramic front brake pads  CLM-a696…  -
~~~

## Follow relations

A field whose value is another Subject is an edge, and queries follow edges.
`--follow field:alias` walks forward, and the alias names the other end's
fields. Every part still gets a row, with `-` where it has no edge, so add a
filter to keep only the parts that have one:

~~~text
$ cruxible query catalog.part --follow fits:vehicle --where 'fits exists' --select title,vehicle.model
subject               title                     vehicle.model  flags
catalog.part/bp-1142  Front brake pads          Civic Sedan    -
catalog.part/bp-2210  Ceramic front brake pads  Civic Sedan    -
~~~

`--follow-in kind.field:alias` walks backwards, along another kind's edge
that points here, so you never keep a reverse list by hand. Which notes are
about each part:

~~~text
$ cruxible query catalog.part --follow-in catalog.note.about:note --where 'note.title exists' --select title,note.title
subject               title             note.title                                flags
catalog.part/bp-1142  Front brake pads  Fitment check of bp-1142 on a 2019 Civic  -
~~~

## Read one thing in depth: get

`get` reads one thing by any reference you have: a `kind/id`, a Claim ID, a
proposal ID, or a definition such as `ClaimType:catalog.part.fits`. Values come
first, and `--detail` goes deeper:

| `--detail` | Shows |
|---|---|
| `summary` (default) | The current values |
| `evidence` | What each value rests on, and whether it counts |
| `history` | Every change, with who made it, when and why (`--field` narrows to one field) |
| `incoming` | The Claims elsewhere that point at this Subject |
| `why` | The full structured account of a Claim's standing |
| `runs` | A Line's or Procedure's runs |
| `proof` | A definition's accepted inventory entry |

## Saved questions: named queries

A question the team asks often can be saved as a **named query**: a definition
anyone can run by name, with parameters. A vehicle's parts:

~~~text
$ cruxible query --name catalog.parts_for_vehicle --param vehicle=honda-civic-2019
subject               category  title                     flags
catalog.part/bp-1142  brakes    Front brake pads          -
catalog.part/bp-2210  brakes    Ceramic front brake pads  -
~~~

Named queries are what generated tables in documents are rendered from (see
[Documents and notes](documents-and-notes.md#pages-that-reflect-state-projection-blocks)), so the same saved question
feeds both a terminal and a page. `cruxible query QueryDefinition` lists them,
with their parameters.

To save a new one, submit its definition through authoring. A "parts in a
category" query:

~~~yaml
kind: query_definition
query_definition:
  identity: {kind: QueryDefinition, name: catalog.parts_in_category}
  description: The parts in one category.
  parameters: [{name: category, value_type: string}]
  entry: {binding: part, subject_kinds: [catalog.part]}
  where:
    kind: comparison
    left: {kind: claim_value, binding: part, predicate: catalog.part.category}
    operator: eq
    right: {kind: parameter, parameter: category}
    value_type: string
  projection:
    fields:
      - {name: title, value: {kind: claim_value, binding: part, predicate: catalog.part.title}}
  result_binding: part
~~~

~~~text
$ cruxible authoring submit parts-in-category.yaml --brief
outcome: accepted
...
$ cruxible query --name catalog.parts_in_category --param category=brakes
subject               title                     flags
catalog.part/bp-1142  Front brake pads          -
catalog.part/bp-2210  Ceramic front brake pads  -
~~~

The `entry` names the kind the rows are about, `where` filters them (here
against the `category` parameter), `projection` picks the columns, and
`result_binding` says which binding a row is. Everything else takes the usual
defaults: one row per Subject, supported and current values only, and a
bounded number of rows. A query that follows edges adds a `traversal`;
`cruxible authoring example query-claims-by-type` prints a fuller template.

## Definitions are queryable too

Fields, kinds, saved queries and automation are state like anything else, so
`query` lists them: `ClaimType`, `SubjectKind`, `CaptureContract`,
`QueryDefinition`, `Procedure`, `ProcedureMandate`, `Line` and `Trigger`.

~~~text
$ cruxible query SubjectKind
kind              rule  keys                   uniqueness  description                                       subjects  kit  lifecycle
...
catalog.part      keys  catalog.part.name      unique      A part in the catalog, named by its part number.  6         -    live
catalog.vehicle   keys  catalog.vehicle.name   unique      A vehicle, named make-model-year.                 1         -    live
~~~

## Reading the past

Every accepted change is a generation, and every generation stays readable.
`--at` answers any `query`, `get` or `orient` as of an earlier one, by
generation number or by the first twelve or more characters of its git
object ID:

~~~text
$ cruxible query catalog.part --select title,category --at 20
subject               title                     category  flags
catalog.part/bp-1142  Front brake pads          brakes    -
catalog.part/bp-2210  Ceramic front brake pads  -         -
~~~

`get KIND/ID --detail history` tells the story of a Subject field by field,
each change with its generation, author and reason:

~~~text
$ cruxible get catalog.part/bp-2210 --detail history --field fits
...
fits
  seq 21  4f0718f3f36e  2026-10-22T02:23:49.600012Z  by YOUR_ID  rebound  catalog.vehicle/honda-civic-2019  (CAP-cc2fb88675a6 -> CAP-30e0aeeecaf5)  [CLM-ef7f11e2…]
      reason: Acme reworded the catalog entry; the fitment still holds.
  seq 20  f79763ef6c2a  2026-10-22T02:23:29.942379Z  by YOUR_ID  stated  catalog.vehicle/honda-civic-2019  [CLM-ef7f11e2…]
      reason: Acme's 2026 catalog lists bp-2210 for the 2019 Civic.
~~~

Here the supplier reworded its catalog line, and the fitment was re-bound to
the new text: one Claim, two revisions.

`cruxible since GENERATION` lists what each later generation created or
retired, one line per value:

~~~text
$ cruxible since 27
28  create  catalog.part/bp-1142  semantic.alias = bp1142  [CLM-fa381cefb106]
29  retire  catalog.part/bp1142  title = Front brake pads  [CLM-3b21c9b9f3ee]
29  retire  catalog.part/bp1142  name = bp1142  [CLM-9436ad1bc0a5]
29  retire  subject catalog.part/bp1142
~~~

## A map: orient

`orient` is the place to start in an unfamiliar instance: every kind with its
fields and counts, who you are, what needs attention, and the next commands
to run. `--kind K` reads one kind in full, and `--section` pages one family
at a time: `claim_types`, `subject_kinds`, `queries`, `lines`, `documents`,
`generations`, `graph` (the Subjects and the Claims between them), `pins`
(what each definition depends on) and more.

## The floor: knowledge you can grep

The daemon keeps a plain-file copy of the current state in your workspace,
`.cruxible/floor/`, refreshed after every accepted change:

- `current/<kind>/<id>.yaml`: one file per Subject, each line a value and its
  Claim ID, with an `INDEX` per kind;
- `ontology/`: a card per kind (each field's type, cardinality, evidence and
  owner) and indexes of fields, contracts, queries, Lines and kits;
- `changes/`, `sources/INDEX` and `projections/INDEX`: recent changes, the
  catalogued files evidence comes from, and the stamped passages in your
  pages.

~~~text
$ cat .cruxible/floor/current/catalog.part/bp-2210.yaml
# catalog.part/bp-2210  kind=catalog.part  changed gen 38
category: brakes  # CLM-393e0d3d…
fits:
  - catalog.vehicle/honda-civic-2019  # CLM-66aeb4ad…
name: bp-2210  # CLM-12d49bda…
title: Ceramic front brake pads  # CLM-a69683d2…

$ grep -rl honda-civic-2019 .cruxible/floor/current
.cruxible/floor/current/catalog.part/bp-2210.yaml
.cruxible/floor/current/catalog.part/bp-1142.yaml
.cruxible/floor/current/catalog.vehicle/honda-civic-2019.yaml
...
~~~

The floor is for finding your way: grep it to locate the Subjects and Claims
you care about, then confirm with `get` or `query` before acting, because it
follows accepted state shortly after each change rather than at the same
moment. An agent without a shell can do the same search with `query
--contains`.

## Found, none, or unknown

Cruxible's model is never complete, so treat an empty answer carefully. It
means **none** only within a scope someone has read in full: if every recall
notice for a part has been recorded, no recall Claim means no recall. Outside
such a scope, it means **unknown**: nobody has looked. Say which one you mean
when you answer, and treat unknown as something to pull ([Keeping knowledge
current](keeping-knowledge-current.md#none-or-unknown)).

## Pitfalls

- **Acting on a page without checking it.** A table or summary in a document
  may be behind the state. `next` lists stale passages
  (`projection_backing_stale`); when one is stale, read the Claims behind it
  with `get` instead.
- **Acting on the floor without confirming.** Grep to find, then confirm with
  the verbs.
- **Reading an empty answer as "none".** It's "unknown" unless the scope was
  read in full.
- **Stopping at the first page.** A truncated answer says so, with the command
  for the next page.

## Where to go next

- [Building team knowledge](building-team-knowledge.md): modeling edges so the
  questions you'll ask have answers.
- [Keeping knowledge current](keeping-knowledge-current.md): what to do when
  answers come back flagged.
- The [CLI reference](cli-reference.md) for every `query`, `get` and `orient`
  option.
