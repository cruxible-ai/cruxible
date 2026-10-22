# Evolving your vocabulary

A team's vocabulary grows with its questions. A new field arrives when someone
needs to ask something new; a category gets split when it stops being precise
enough; a kind of thing that was text becomes a thing of its own. In Cruxible
the vocabulary is itself governed: fields, kinds and saved questions are
definitions in the ledger, and changing one is a reviewed change like any
other.

What makes a vocabulary change different is that Claims depend on it. Every
Claim was written under a field's definition, so a change to the field has to
say what happens to each Claim already written under it: carry it over
unchanged, say it again in the new terms, or retire it. Cruxible won't accept
a change that leaves any of them unaccounted for, so history never holds a
Claim that its own field doesn't admit.

This guide covers what's cheap to change and what isn't, a worked example of
splitting a category, the common tasks, and upgrading a kit you've edited.
It uses the car parts example from [Concepts](concepts.md#knowledge-as-claims).

## What a change costs

| Change | What happens to existing Claims |
|---|---|
| Add a field, a kind, or a saved query | Nothing; nothing depends on it yet. |
| Reword a description, or allow more values | Every Claim of the field is carried over to the new definition, in the same change. |
| Allow fewer values (drop or rename an enum member) | Claims whose value is still allowed are carried; each of the others must be re-authored with an allowed value, or retired. |
| Change what kind of value a field holds (text to a relation, say) | Nothing can be carried; re-author or retire every Claim. |
| Change a key, or its canonical form | The kind's Subjects are re-keyed: refused while any Subject's key would read differently, or two would share one, unless the change handles them. |

Even the cheap rows touch every Claim of the field: a revision carries all of
them, so a busy field means a large change to review. Get definitions right
early, and prefer adding a field to reshaping one that's full.

## A worked example: splitting a category

`catalog.part.category` holds one of `brakes`, `filters`, `lighting` and
`wipers`. The shop starts stocking rotors and wants `brakes` split into
`brake-pads` and `brake-rotors`. Four Claims are written under the field
today: two parts are `brakes`, one `filters`, one `wipers`.

A change to a field is a **succession**: a new version of its definition that
replaces the old one, together with what happens to every Claim written under
it. An edit names the field and only what changes; everything else carries
over. The split takes three steps, each a reviewed change.

**1. Allow the new values.** Widening the list carries every Claim over as it
is:

~~~yaml
predicate: catalog.part.category
literal_schema:
  type: string
  enum: [brakes, brake-pads, brake-rotors, filters, lighting, wipers]
~~~

~~~text
$ cruxible claim-type propose --input widen.yaml --name widen-category
admitted  sha256:b7b56ddc…  verdict=candidate
landed: accepted at 38b76ed1a46e
~~~

**2. Move the values.** Say again, in the new terms, what each `brakes` part
is:

~~~text
$ cruxible write move-pads.yaml
accepted (generation 42, d336bbfd5eb2)
  set catalog.part/bp-1142 category: brakes -> brake-pads  [CLM-a7bf36ca…; verdict supported]
  set catalog.part/bp-2210 category: brakes -> brake-pads  [CLM-8a23dd7f…; verdict supported]
~~~

**3. Drop the old value.** With nothing left on it, narrowing the list
carries every Claim:

~~~text
$ cruxible claim-type propose --input narrow.yaml --name narrow-category    # the enum without brakes
admitted  sha256:959016af…  verdict=candidate
landed: accepted at 99407d14196a

$ cruxible set catalog.part/af-208 category brakes --because "…"
refused (generation 43, 99407d14196a)
cruxible.write.value_not_member (change 0): 'brakes' is not a member of category; its members are: brake-pads, brake-rotors, filters, lighting, wipers
  nearest: brake-pads, brake-rotors
~~~

A narrowing that would strand a value is refused before anything lands, and
names the value:

~~~text
$ cruxible claim-type propose --input drop-wipers.yaml --name drop-wipers --dry-run
would_block  -  verdict=refused
  cruxible.claim.literal_schema_invalid: The Claim literal 'wipers' fails the exact schema of ClaimType 'catalog.part.category'; its members are: brake-pads, brake-rotors, filters, lighting.
~~~

Each Claim keeps its identity, so the field's history reads as revisions
(`cruxible get catalog.part/bp-1142 --detail history --field category`).

To make a change like this in one atomic step instead, submit a
`claim_type_succession` change-set member, which re-authors the moved Claims
beside the new definition. It takes the full definition today: read it with
`cruxible get ClaimType:catalog.part.category --detail proof --json`, change
it, and set `lifecycle.predecessor_digest` to its `artifact_digest`
(`cruxible authoring example claim-type-succession` prints a template). From
Python, `ChangeSetDraft.succeed_claim_type(successor, dependents=[...])`
builds the same change, with `carry`, `re_author`, `retire` and `rescind` for
each dependent ([For AI agents](for-ai-agents.md#evolving-vocabulary)).

## Common tasks

### Add a field

A field needs its name, the kinds it applies to, what it holds, and how many
values a Subject has; everything else has a default (`cruxible claim-type
propose --template` prints this shape):

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

$ cruxible set catalog.part/bp-1142 warranty_months 24 --because "Acme's warranty card."
accepted (generation 34, d0f4b78923e6)
  set catalog.part/bp-1142 warranty_months: 24  [CLM-db2ff013…; verdict supported]
~~~

By default a field is normative and backed by its writer's own words. Add a
`description`, the roles it takes, and evidence rules when it needs other
evidence ([Evidence and
admission](evidence-and-admission.md#writing-a-fields-rules) shows a field
that requires a supplier's quote). Like any change, the field lands at once
when your approval policy allows it; `--no-accept` only proposes it. Numbers
are integers: store a price in cents, say. To change a field later, propose
an edit naming only what changes, as in the example above.

### Add a kind of thing

A new kind needs a **SubjectKind**, which says how its Subjects are named.
Most kinds are keyed: a key field, such as a supplier's name, decides which
Subject a name refers to, so the same supplier is never recorded twice. Add
the key field first, with a `canonical_form`, the rule that decides when two
spellings are the same key:

~~~yaml
predicate: catalog.supplier.name
description: "The supplier's name; its key."
allowed_subject_kinds: [catalog.supplier]
object_kind: literal
literal_schema: {type: string, minLength: 1}
cardinality: one
canonical_form: [nfkc, trim, collapse_whitespace, casefold]
~~~

Then declare the kind:

~~~text
$ cruxible claim-type propose --input supplier-name.yaml --name add-supplier-name
admitted  sha256:46e5b47f…  verdict=candidate
landed: accepted at 8c5c4c5e53ba

$ echo '{"kind": "subject_kind", "name": "catalog.supplier", "keys": ["catalog.supplier.name"],
    "uniqueness": "unique", "description": "A parts supplier, named by its name."}' \
    | cruxible authoring submit - --brief
outcome: accepted
...
~~~

Name a new Subject by its key, and Cruxible creates it and assigns its id:

~~~text
$ cruxible set "catalog.supplier/Bolt Supply" name "Bolt Supply" --because "A second supplier."
accepted (generation 47, a8242f4b9a28)
  create catalog.supplier/bolt-supply: created
  + subject catalog.supplier/bolt-supply
~~~

The key is stored in its canonical form (this one folds case), and any
spelling of it finds the same Subject: `cruxible get "catalog.supplier/ACME
parts"` reads `catalog.supplier/acme-parts`. A kind whose things have no
natural name, such as a remediation task, is **minted** instead: Cruxible
gives each a generated id. To bring an existing kind under keys, see [Subject
kinds](cli-reference.md#subject-kinds-and-creation).

### Change what kind of value a field holds

A field whose values change kind, such as text becoming a relation to another
Subject, can't carry its Claims, because their values no longer mean what the
field now says:

~~~text
cruxible.authoring.claim_type_succession_object_kind_change: A successor that changes object_kind cannot carry a live Claim dependent unchanged: its object no longer says what the ClaimType now means.
~~~

Re-author each Claim in the new form, or retire it, in the same change set.
Often it's simpler to add the new field beside the old one, fill it in, and
retire the old one when nothing reads it.

### Move a paragraph field into prose

A field whose values have grown into paragraphs (a decision's whole ruling, a
long rationale) is prose in the wrong place: reads cut it short, nobody can
filter by it, and it can't be edited like a document. Move the text into a
document, keep a short value that points at it, and retire the long field.

Here, `catalog.decision.ruling` holds each decision's full ruling. Create the
page and name it, then make the move in one `write`: each ruling becomes a
section with an `id`, a one-line `summary` cites its section, and the long
Claim is retired:

~~~yaml
because: Move decision rulings into the decisions page; keep a one-line summary that cites each.
docs:
  - op: append
    id: ceramic
    file: docs/decisions.md
    under: [Decisions]
    text: |
      ## Ceramic pads by default

      We stock ceramic front pads (bp-2210) as the default for the 2019 Civic instead of
      the semi-metallic bp-1142. Customers complained about brake dust and noise ...
changes:
  - {op: set, subject: catalog.decision/ceramic-pads-default, field: summary,
     value: "Ceramic front pads (bp-2210) are the default for the 2019 Civic.",
     evidence: {kind: section, ref: ceramic}}
  - {op: retire, target: CLM-88af13a35fee7502bbe9d0592a1b4bfb, reason: superseded}
~~~

~~~text
$ cruxible files add decisions docs/decisions.md
$ cruxible write move-rulings.yaml --dry-run
would accept (generation 21, 454fd7f0f0a9)
  doc docs/decisions.md ['Decisions', 'Ceramic pads by default']: not written (sha256:1777714a…)
  set catalog.decision/ceramic-pads-default summary: Ceramic front pads (bp-2210) are the default for the 2019 Civic.  [CLM-…; verdict supported]
  retire catalog.decision/ceramic-pads-default ruling: We stock ceramic front pads (bp-2210) as the default for the 2019 Civic instead of the semi-metallic bp-1142. Customers… (399 chars; --detail evidence for all)  [CLM-88af13a3…]
  ...
next: Run the same command without --dry-run, adding --at 454fd7f0f0a9

$ cruxible write move-rulings.yaml --at 454fd7f0f0a9
accepted (generation 22, a1f869045bd6)
...
$ cruxible query catalog.decision --select status,summary
subject                                status    summary                                                       flags
catalog.decision/ceramic-pads-default  accepted  Ceramic front pads (bp-2210) are the default for the 2019 C…  -
catalog.decision/wiper-brand           accepted  We carry one wiper blade line, Acme's wb-330.                 -
~~~

The section counts as your own words, so the summary is `supported`, and an
edit to the section later brings the summary back as `citation_drifted`
([Keeping knowledge current](keeping-knowledge-current.md#when-a-field-turns-into-prose)).
The old rulings stay readable in each decision's history.

Once no live Claim uses the long field, retire its definition. A retirement
is a succession whose successor is the field's full current definition (read
it with `cruxible get ClaimType:catalog.decision.ruling --detail proof
--json`) with `lifecycle.state` set to `retired` and
`lifecycle.predecessor_digest` set to its `artifact_digest`. `cruxible
claim-type migrate` takes it, first as a preflight that lists the dependents:

~~~text
$ cruxible claim-type migrate retire-ruling.json      # {"mode": "preflight", "successor": {…}, "dependents": []}
ClaimType migration preflight
...
Blast radius: 2 dependent(s)
  Claim:CLM-760d1a32ecbe941c631363c520f0c0f5 (claim)
  Claim:CLM-88af13a35fee7502bbe9d0592a1b4bfb (claim)
Semantic delta:
  ...
  /lifecycle/state: "live" -> "retired"
~~~

The dependents are the Claims you just retired; they move with the retired
definition (`"disposition": "successor"`), which keeps their history readable.
Submit with `"mode": "submit"` and those dispositions, and approve the
proposal it prints straight away: if anything else lands first, run the
preflight and submit again. Afterwards the field is gone from the vocabulary:

~~~text
$ cruxible set catalog.decision/wiper-brand ruling "…" --because "…"
refused (generation 55, fe1ac3c5eb75)
cruxible.write.unknown_field (change 0): catalog.decision has no field 'ruling'
~~~

To keep the field with a tighter shape instead, revise it after the move with
a succession that adds a `maxLength`; a long value still in it can't be
carried, so move every one first ([the worked
example](#a-worked-example-splitting-a-category) shows a succession).

### Adopt a built-in field

Cruxible ships `semantic.alias` and `semantic.distinct_from` ([Keeping
knowledge current](keeping-knowledge-current.md#duplicates-beta)). An instance
created before one shipped adopts it with `cruxible claim-type propose
--builtin NAME`.

## Upgrading a kit you've edited

A kit's definitions are yours to change, and a newer release of the kit has to
decide whether to keep your change. `kit add` with a newer release previews
the upgrade, and names every definition where your version differs from the
kit's:

~~~text
$ cruxible kit add car-parts:1.1.0
car-parts 1.1.0: would_propose (upgrade from 1.0.0)
...
ClaimType:
  replace: ClaimType:catalog.part.title (overwrites your edit; 1 dependent(s))
  add: ClaimType:catalog.part.weight_grams
QueryDefinition:
  replace: QueryDefinition:catalog.parts_for_vehicle
Preview at 246e273be249; nothing was written.
~~~

Here the shop reworded the title field's description, and the release
rewords it too. (The saved query is replaced because the release's version
reads the new title definition; that isn't an edit of yours.) `--keep
IDENTITY` keeps your version of one definition, and `--keep-local-edits`
keeps every one you've changed:

~~~text
$ cruxible kit add car-parts:1.1.0 --keep ClaimType:catalog.part.title --commit --at 246e273be249
car-parts 1.1.0: accepted (upgrade from 1.0.0)
...
ClaimType:
  keep: ClaimType:catalog.part.title (keeps your edit)
  add: ClaimType:catalog.part.weight_grams

$ cruxible kit status
car-parts 1.1.0 sha256:0164a4b4…; …
  ...
  kept: ClaimType:catalog.part.title (your edit)
~~~

The kept divergence is recorded, so a later release
that leaves that definition alone doesn't ask again. A release that narrows a
field your Claims use needs the same decisions as any succession; see
[Kits](kits.md) for upgrades in full.

## Pitfalls

- **Reshaping a full field.** Every revision carries all of its Claims; add a
  field instead when you can.
- **Narrowing without a plan.** Dropping an allowed value strands the Claims
  that use it. Decide, per Claim, between re-authoring and retiring.
- **Changing a value's kind in place.** It can't carry anything; add a new
  field and move over.
- **Changing a key's canonical form casually.** It re-keys every Subject of
  the kind.
- **Upgrading a kit without reading the preview.** Upgrades overwrite your
  edits unless you keep them.
- **Defining and changing a field in one change set.** Both members write the
  same definition, and the set is refused; submit them separately.

## Where to go next

- [Building team knowledge](building-team-knowledge.md): choosing fields and
  kinds well in the first place.
- [Evidence and admission](evidence-and-admission.md): the evidence rules
  every field defines.
- [Kits](kits.md): installing and upgrading shared vocabularies.
- The [CLI reference](cli-reference.md#claim-type) for `claim-type`, and
  [authoring](cli-reference.md#authoring) for change-set members.
