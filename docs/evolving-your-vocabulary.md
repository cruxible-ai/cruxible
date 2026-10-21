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

A ClaimType change is a **succession**: a new version of the definition that
names the one it replaces, together with what happens to each dependent. In
one change set:

- the successor: the current definition with the new enum, naming its
  predecessor by digest. Read the current one with `cruxible get
  ClaimType:catalog.part.category --detail proof --json`, change
  `literal_schema`, and set `lifecycle.predecessor_digest` to its
  `artifact_digest`;
- `carry_all: true`, which carries every dependent you don't name: the
  `filters` and `wipers` Claims, and the saved query that reads the field;
- the two `brakes` Claims, each marked `re_author` and said again as
  `brake-pads` by a Claim member in the same set.

<!-- update: first-run batch -->
~~~json
{"kind": "change_set", "rationale": "Split the brakes category into brake pads and brake rotors.", "members": [
 {"kind": "claim_type_succession", "successor": {"…": "the current definition, with the new enum and its predecessor's digest"},
  "carry_all": true,
  "dependents": [
   {"identity": {"kind": "Claim", "name": "CLM-636f28bb87de32a5362b11ee30aa4d0c"}, "disposition": "re_author",
    "successor_claim_id": "CLM-636f28bb87de32a5362b11ee30aa4d0c"},
   {"identity": {"kind": "Claim", "name": "CLM-6b043535eef17f24cd5fb0868ef3dc60"}, "disposition": "re_author",
    "successor_claim_id": "CLM-6b043535eef17f24cd5fb0868ef3dc60"}]},
 {"kind": "claim", "subject": "catalog.part/bp-1142", "predicate": "catalog.part.category",
  "object": {"kind": "literal", "value": "brake-pads"}, "role": "observation",
  "revises": "CLM-636f28bb87de32a5362b11ee30aa4d0c",
  "rationale": "Brakes is split into brake pads and brake rotors; this is a pad.",
  "source": {"kind": "self_source", "body": "category: brake-pads\n"}},
 {"kind": "claim", "subject": "catalog.part/bp-2210", "…": "the same, revising CLM-6b043535…"}
]}
~~~

~~~text
$ cruxible authoring submit split-brakes.json --brief
outcome: accepted
...

$ cruxible query catalog.part --select category
subject               category    flags
catalog.part/af-208   filters     -
catalog.part/bp-1142  brake-pads  -
catalog.part/bp-2210  brake-pads  -
catalog.part/wb-330   wipers      -
~~~

A re-authored Claim keeps its identity and its history, so the change reads
as a revision:

~~~text
$ cruxible get catalog.part/bp-1142 --detail history --field category
...
category
  seq 20  4ebd98cf01c7  2026-10-21T19:58:54.430050Z  by YOUR_ID  changed  brakes -> brake-pads  [CLM-636f28bb…]
      reason: Split the brakes category into brake pads and brake rotors.
  seq 18  34d3601cd417  2026-10-21T19:57:53.305113Z  by YOUR_ID  stated  brakes  [CLM-636f28bb…]
      reason: Acme catalog 2026.
~~~

And the old value is gone from the vocabulary:

~~~text
$ cruxible set catalog.part/bp-3300 category brakes --because "Acme catalog 2026."
refused (generation 20, 4ebd98cf01c7)
cruxible.write.value_not_member (change 0): 'brakes' is not a member of category; its members are: brake-pads, brake-rotors, filters, lighting, wipers
  nearest: brake-pads, brake-rotors
~~~

If the change misses a dependent, it's refused before anything is proposed
(`cruxible.authoring.claim_type_succession_closure_incomplete`), and the
repair lists every dependent it needs, saved queries included. Carrying a
Claim whose value the new definition doesn't allow is refused too:

~~~text
cruxible.claim.literal_schema_invalid: The Claim literal 'wipers' fails the exact schema of ClaimType 'catalog.part.category'; its members are: brake-pads, brake-rotors, filters, lighting.
~~~

`cruxible authoring compile FILE` runs these checks without submitting
anything. From Python, `ChangeSetDraft.succeed_claim_type(successor,
dependents=[...])` builds the same change, with `carry`, `re_author`, `retire`
and `rescind` for each dependent ([For AI agents](for-ai-agents.md#evolving-vocabulary)).

## Common tasks

### Add a field

<!-- update: first-run batch -->
Start from the template, which prints a complete definition to edit:

~~~bash
cruxible claim-type propose --template > weight.json
~~~

Change its `predicate` (`catalog.part.weight_grams`), the kinds it applies to,
its `literal_schema` (`{"type": "integer", "minimum": 0}`), its roles, a
`description`, and its evidence rules ([Evidence and
admission](evidence-and-admission.md#how-a-field-admits-evidence)). Then
propose it:

~~~text
$ cruxible claim-type propose --input weight.json --name add-weight
admitted  sha256:b6919d39…  verdict=candidate
next: cruxible get sha256:b6919d39…
~~~

A new field stops at a proposal; approve it to accept it (`cruxible proposal
approve PROPOSAL_ID`). Then it's ready to use:

~~~text
$ cruxible set catalog.part/bp-1142 weight_grams 680 --because "Weighed on the shop scale."
accepted (generation 22, 113d18f376d1)
  set catalog.part/bp-1142 weight_grams: 680  [CLM-62297008…; verdict supported]
~~~

To change a field later, submit a succession as in the example above.

### Add a kind of thing

A new kind needs a **SubjectKind**, which says how its Subjects are named.
Most kinds are keyed: a key field, such as a supplier's name, decides which
Subject a name refers to, so the same supplier is never recorded twice. A
keyed kind and its key field go in together:

<!-- update: first-run batch -->
~~~json
{"kind": "subject_kind", "name": "catalog.supplier", "keys": ["catalog.supplier.name"],
 "uniqueness": "unique", "description": "A parts supplier, named by its name."}
~~~

in a change set beside the key ClaimType: literal, one value per Subject, and
with a `canonical_form`, the rule that decides when two spellings are the same
key (`cruxible authoring example subject-kind` prints a complete pair).

Create a Subject by its key, and Cruxible assigns its id:

~~~text
$ echo '{"kind": "subject", "subject_kind": "catalog.supplier", "key": "Acme Parts", "rationale": "Our main supplier."}' \
    | cruxible authoring submit - --brief
outcome: accepted
...
$ cruxible query catalog.supplier --select name
subject                      name        flags
catalog.supplier/acme-parts  acme parts  -
~~~

The key is stored in its canonical form (this one folds case), and any
spelling of it finds the same Subject: `cruxible get "catalog.supplier/ACME
parts"` reads `catalog.supplier/acme-parts`. A kind whose things have no
natural name, such as notes, is **minted** instead: Cruxible gives each a
generated id. To bring an existing kind under keys, see [Subject
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
$ cruxible kit add ./car-parts-1.1.0 --dry-run
car-parts 1.1.0: would_propose (upgrade from 1.0.0)
...
ClaimType:
  replace: ClaimType:catalog.part.title (overwrites your edit)
  add: ClaimType:catalog.part.weight_grams
...
Preview at 2b5ee27583e9; nothing was written.
~~~

Here the shop reworded the title field's description, and the release
rewords it too. `--keep IDENTITY` keeps your version of one definition, and
`--keep-local-edits` keeps every one you've changed:

~~~text
$ cruxible kit add ./car-parts-1.1.0 --keep ClaimType:catalog.part.title --commit --at 2b5ee27583e9
car-parts 1.1.0: accepted (upgrade from 1.0.0)
...
ClaimType:
  keep: ClaimType:catalog.part.title (overwrites your edit; this instance's version is kept)
  add: ClaimType:catalog.part.weight_grams
~~~

The kept divergence is recorded, and `kit status` lists it, so a later release
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
