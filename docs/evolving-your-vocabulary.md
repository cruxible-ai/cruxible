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

The ordinary way to change kinds and fields is one file,
`.cruxible/vocabulary.yaml`: export it, edit it, see the plan, apply it. This
guide covers what's cheap to change and what isn't, the vocabulary file and
its format, a worked example of splitting a category, the common tasks, what
the file does not do, and upgrading a kit you've edited. It uses the car parts
example from [Concepts](concepts.md#knowledge-as-claims).

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

## The vocabulary file

`.cruxible/vocabulary.yaml` states every kind and its fields in one YAML file,
committed to Git beside `files.yaml`. It is an input to authoring, never the
authority: the signed ledger stays the record, and the file only proposes
changes to it.

~~~text
$ cruxible vocabulary export
Wrote .cruxible/vocabulary.yaml (4 kind(s), 16 field(s)) at abd7c8b678fe
~~~

Edit the file, then see what applying it would change. `plan` writes nothing:

~~~text
$ cruxible vocabulary plan
Vocabulary plan: .cruxible/vocabulary.yaml (based on abd7c8b678fe)
Changes:
  add ClaimType:catalog.part.warranty_months
  add SubjectKind:catalog.supplier
  add ClaimType:catalog.supplier.name
Preflight: passed
~~~

`apply` submits the same changes as one ordinary change set, which lands at
once when your approval policy allows it (`--no-accept` keeps the proposal),
and `export` then moves the file to the new head:

~~~text
$ cruxible vocabulary apply --rationale "Warranty terms, and suppliers as a kind."
...
landed: accepted at d61feee0616d
Next: cruxible vocabulary export
~~~

The same three steps are `cx.vocabulary.export()`, `.plan()` and `.apply()` in
the SDK (printing a result shows the text the CLI prints), and
`cruxible_vocabulary_export`, `_plan` and `_apply` on MCP (the `full`
profile), each reading the file from the workspace and sending its bytes.

What the file leaves out is untouched. A kind or field it does not name is
listed by `plan` as left as it is (`Not in file (left as they are)`), never
retired, and a property an existing field's entry leaves out keeps its
accepted value; write `null` to clear one that may be empty. Only `retired:
true` retires. Definitions a kit installed are edited the same way; `plan`
marks them `kit=<id>`, and export marks them `# kit: <id>`.

A field you add to a kit's kind is yours, not the kit's: it carries no kit
marker, and nothing warns when it lands. An upgrade of the kit leaves it alone
unless the new release defines a field of the same name; then the release's
definition replaces yours and the kit owns it, unless you keep yours
([Upgrading a kit you've edited](#upgrading-a-kit-youve-edited)).

**Keeping it in step.** Only `export` writes the file; the daemon never does.
When accepted vocabulary moves past the file's export (someone proposed a
field from the CLI, say, or a kit upgrade landed), `orient` says so and
`next` lists it, and exporting again repairs it:

~~~text
$ cruxible orient
...
Vocabulary file: .cruxible/vocabulary.yaml at abd7c8b678fe, behind by 3 definition(s); refresh: cruxible vocabulary export
...
$ cruxible next
warning  vocabulary_file_behind  .cruxible/vocabulary.yaml  next=cruxible.vocabulary.export
  repair: cruxible vocabulary export
~~~

`orient`'s `next=` count covers accepted state only; the vocabulary row is in
`cruxible next`, as `orient` notes under its `Attention:` line. A header naming an export this instance does not know is
`vocabulary_file_baseline_invalid`: restore the header, or `vocabulary export
--force`. Export replaces a file only when nothing in it would be lost; a file
holding edits not yet applied refuses, naming them:

~~~text
$ cruxible vocabulary export
Error: cruxible.vocabulary.export_refused: .cruxible/vocabulary.yaml holds changes not in accepted state (ClaimType:catalog.part.title): apply them (cruxible vocabulary apply), or run cruxible vocabulary export --force to replace the file
~~~

**When someone else changed the same field.** `plan` and `apply` read at the
export the file names (`based_on`), so an edit replaces the definition you
saw. If someone changed that definition since your export, preflight refuses
it rather than overwriting their change, and `apply` names each definition
that moved (`moved` in the result) with the repair:

~~~text
$ cruxible vocabulary apply --rationale "My edit."
Vocabulary plan: .cruxible/vocabulary.yaml (based on 305dc6416d93)
Changes:
  replace ClaimType:catalog.part.title (description)
Preflight: refused
  cruxible.authoring.claim_type_succession_invalid at members[0].successor: cruxible.claim_type.migration_invalid: successor does not pin the current ClaimType
Refused: nothing was submitted.
Changed since the file's export: ClaimType:catalog.part.title
Next: Save your edits to .cruxible/vocabulary.yaml elsewhere, run cruxible vocabulary export --force to read the definitions as they are now, redo your edits, check them with cruxible vocabulary plan, then cruxible vocabulary apply
~~~

### The format

The file is a header and a map of kinds. Here is a whole one, exported from
the car parts instance:

~~~yaml
format: cruxible-vocabulary-v1
instance: inst_01286013bfab4b93
based_on:
  git_oid: 5a03be4bbf6ca1da970c77a0f588bb99da59a0cb
  semantic_root: sha256:02dd3eef…
  generation_root: sha256:d3479846…
  compiler_digest: sha256:5b1e93a0…
  vocabulary_digest: sha256:6a30df07…
kinds:
  catalog.decision:
    description: A ruling the shop made; its reasoning lives in a doc section.
    fields:
      slug:
        key: true
        value: 'string{min_len: 1, max_len: 64}'
        description: A short slug for the ruling; the key.
        permitted_roles:
        - normative
      decided_on:
        value: date
        description: The day the shop made the ruling, as the ruling states it.
        permitted_roles:
        - normative
      governs:
        link:
        - catalog.part
        - catalog.vehicle
        many: true
        description: The parts and vehicles the ruling applies to.
        permitted_roles:
        - normative
      status:
        enum:
        - proposed
        - accepted
        - superseded
        description: Where the ruling stands.
        permitted_roles:
        - normative
      summary:
        value: 'string{max_len: 200}'
        description: The ruling in one line; it cites the section that gives the reasoning.
        permitted_roles:
        - normative
  catalog.part:
    description: A part the shop stocks, named by its part number.
    fields:
      name:
        key: true
        value: 'string{min_len: 1, max_len: 64}'
        description: The part number; the key.
        permitted_roles:
        - normative
        canonical_form:
        - nfkc
        - trim
        - casefold
      category:
        enum:
          brake-pads: Brake pads.
          brake-rotors: Brake rotors and discs.
          filters: Oil, air and cabin filters.
          lighting: null
          wipers: Wiper blades.
        description: The part's shelf category.
        permitted_roles:
        - normative
      discontinued:
        value: bool
        description: The supplier has stopped making the part, as its discontinued-parts file says.
        permitted_roles:
        - observation
        evidence_admission_policy:
          rules:
          - rule_id: acme-file
            claim_roles:
            - observation
            admission: direct
            subject_binding: exact_claim_subject
            attestation_requirement: none
            capture_contracts:
            - role: capture-contract
              target:
                kind: CaptureContract
                name: catalog.acme-discontinued
        default_role: observation
        evidence_requirement: captured
      fits:
        link: catalog.vehicle
        many: true
        description: A vehicle the part fits.
        permitted_roles:
        - normative
      replaces:
        link: catalog.part
        many: true
        description: An older part this one supersedes.
        permitted_roles:
        - normative
      title:
        description: The part's name on the supplier's invoice.
        permitted_roles:
        - normative
      warranty_months:
        value: int{0..120}
        description: How many months the supplier's warranty runs.
        permitted_roles:
        - normative
  catalog.supplier:
    description: A parts supplier, named by its name.
    fields:
      name:
        key: true
        value: 'string{min_len: 1, max_len: 256}'
        description: The supplier's name; its key.
        permitted_roles:
        - normative
        canonical_form:
        - nfkc
        - trim
        - collapse_whitespace
        - casefold
  catalog.vehicle:
    description: A car, named by make, model and year.
    fields:
      name:
        key: true
        value: 'string{min_len: 1, max_len: 128}'
        description: Make, model and year; the key.
        permitted_roles:
        - normative
        canonical_form:
        - nfkc
        - trim
        - collapse_whitespace
        - casefold
  semantic:
    fields:
      alias:
        ...
      distinct_from:
        ...
~~~

**The header.** `format` is always `cruxible-vocabulary-v1`; `instance` names
the instance the file belongs to; `based_on` is the export it was written
from: the accepted coordinate (Git OID, semantic root, generation root,
compiler digest) and a digest over every live ClaimType and SubjectKind. Never
edit it; `export` writes it.

**A kind** is a dotted name. Its keys state its SubjectKind, the rule for
naming its Subjects:

| Key | Means |
|---|---|
| `description` | What a Subject of the kind is. |
| `minted: true` | Cruxible generates each Subject's id; the kind has no key fields. |
| `keys` | The key fields in full form (their predicates, in order), for when the keys are not this kind's own fields. Usually unnecessary: mark fields `key: true` instead. |
| `uniqueness` | `unique` (the default: a key names one Subject) or `hint` (a key usually names one). |
| `normalizers` | A core identity normalizer per key part, such as `{name: {normalizer: pep503, version: 1}}` for Python package names. |
| `retired: true` | Retires the kind. |
| `fields` | The kind's fields, by name. |

A kind with only `fields` changes no SubjectKind.

**A field** is one ClaimType, named `<kind>.<field>` (`catalog.part.category`).
`{}` takes every default: one value, a string of at most 256 characters,
normative, backed by its writer's own words or by a workspace file or section
citation. Beside any property of the full ClaimType input form, a field takes
these shorthands:

| Shorthand | Means |
|---|---|
| `enum: [a, b]` | A closed list of members. As a map, `{member: meaning}` gives each member's meaning (`null` for none). |
| `link: KIND` | The value is a Subject of that kind; `link: [KIND, KIND]` for several kinds. |
| `many: true` | Several values at once. Left out: one value (on an edit, what is accepted). |
| `key: true` | A key of the kind, in file order. A key holds one value. |
| `retired: true` | Retire the field, and its live Claims with it; `reason:` (`superseded`, the default, `was-wrong` or `was-rescinded`) is the reason each Claim takes. |

The other properties are the full ClaimType input form, written in the same
entry: `value` (below), `description`, `default_role`, `permitted_roles`,
`canonical_form`, `evidence_requirement`, `evidence`,
`evidence_admission_policy`,
`evidence_freshness` (`{stale_after: P30D}`), `inverse`, `requires_complete_input`
and `incomplete_fallback`, `allowed_subject_kinds` (for a field several kinds
share), and the rest that `cruxible claim-type propose --template --expanded`
prints with their defaults. Anything shorthand cannot say is written this way,
so an export never loses meaning (a ClaimType's `inverse` included; when some
field cannot be written faithfully, export refuses once, naming each one).
Export writes every field's `permitted_roles`, and `evidence_requirement:
captured` where a field has it, so applying an export unchanged plans no
change.
`stale_after` ages only the fields that declare it, and freshness has known
limits in 0.5.0: see [what freshness does and does not do](evidence-and-admission.md#what-freshness-does-and-does-not-do-in-050).

**Evidence.** Own words, and workspace file and section citations, back every
field that is not `evidence_requirement: captured`; none of them needs a rule.
`captured` means a shaped payload: a Capture under a CaptureContract the field
names. Name the contracts in `evidence: [catalog.acme-discontinued]`, the
short form, which export writes back as it is. A rule that needs more than
that is written as `evidence_admission_policy`: `discontinued` above states
its rule in full. Hand-written, the same rule may name the contract briefly
(`capture_contracts: [CaptureContract:catalog.acme-discontinued]`), and export
writes it back in full. A hand `set` on a `captured` field that cites only
your own words or a file section is refused (`cruxible.write.evidence_required`,
naming the contract). A rule can no longer name one workspace file
(`cruxible.claim_type.per_file_evidence_rule`). [Evidence and
admission](evidence-and-admission.md#writing-a-fields-rules) covers evidence
rules.

**`value:`** is the field's type, in the Contract type grammar. Quote a type
that holds `{` and `:` so YAML reads it as one string (`'string{max_len:
200}'`). An enum or a link states the value itself; drop `value` there.

### Value types

| Type | Holds | Example |
|---|---|---|
| `string{...}` | Text. Bounds: `min_len`, `max_len` (a field's string allows at most 256), `pattern` (a regular expression). | `'string{min_len: 1, max_len: 64}'` |
| `int{min..max}` | A whole number; either bound may be left out. | `int{0..120}`, `'int{0..}'` |
| `decimal{min..max}` | An exact decimal number. | `'decimal{0..}'` |
| `number{min..max}` | A floating-point number. | `number` |
| `bool` | `true` or `false`. | `bool` |
| `date` | A calendar date, `2026-11-12`. | `date` |
| `datetime` | An instant with its offset. | `datetime` |
| `duration` | An ISO 8601 duration, `P30D`. | `duration` |
| `enum[...]` | One of a closed list of members (the `enum:` shorthand writes it). | `'enum[open, done]'` |
| `enum_of PREDICATE` | One of another field's enum members. | `enum_of catalog.part.category` |
| `subject<KIND>` | A Subject of that kind, or `subject<A \| B>` (the `link:` shorthand writes it). | `'subject<catalog.vehicle>'` |
| `list<T>{max_items: N}` | A list of up to N values of type T, as one value. | `'list<string{max_len: 20}>{max_items: 5}'` |
| `NAME@N` | A record of a governed Contract's fields. | `catalog.dimensions@1` |

There is no `json`, `object` or `map` type: a field holds a declared shape.
Prose belongs in a document section the field cites, not in a long string. A
number with a unit is clearest as an integer in the smallest unit
(`price_cents`).

### Moving Claims when an enum narrows

A member that leaves an enum must say what happens to the live Claims that hold
it. Write it as the member's entry, beside the members that stay:

- `old: {becomes: new}` says each of its Claims again with `new`, keeping its
  identity, Subject and role and citing its own evidence again;
- `old: {retire: was-wrong}` (or `superseded`, `was-rescinded`) retires them.

`plan` lists the Claims a mapping moves before the definition changes.
[The worked example](#a-worked-example-splitting-a-category) uses one.

### Strict YAML

The file is read strictly, so it can't be read two ways. A duplicate key, an
unknown key, an alias, an anchor or a merge key refuses, and so does a value a
YAML reader could take as something else: `yes`/`no` (write `true` or
`false`), an unquoted date, a decimal number, or an empty value. Each refusal
names its line:

~~~text
$ cruxible vocabulary plan
Error: cruxible.vocabulary.file_invalid: 'yes' at line 98, column 15 reads as a boolean: write true or false, or quote it
$ cruxible vocabulary plan
Error: cruxible.vocabulary.file_invalid: kinds.catalog.part.fields.fits: Value error, unknown field property plural; a field takes key, enum, link, many, retired, reason or a ClaimType input field
~~~

### The `semantic` group

Every export ends with `semantic`, which reads like a kind but is not one: it
is the namespace of Cruxible's built-in fields `semantic.alias` and
`semantic.distinct_from`, which apply to Subjects of every kind (their
`allowed_subject_kinds` is the pseudo-kind `semantic.subject`). There is no
`semantic` SubjectKind and no `semantic/...` Subject. Leave the group as export
writes it ([Duplicates](keeping-knowledge-current.md#duplicates-beta) says what
the two fields are for).

## A worked example: splitting a category

`catalog.part.category` holds one of `brakes`, `filters`, `lighting` and
`wipers`. The shop starts stocking rotors and wants `brakes` split into
`brake-pads` and `brake-rotors`. Both parts recorded as `brakes` today are
pads.

A change to a field is a **succession**: a new version of its definition that
replaces the old one, together with what happens to every Claim written under
it. In the file, the split is one edit: add the two new members, and say that
`brakes` becomes `brake-pads`:

~~~diff
       category:
         enum:
-          brakes: Pads, rotors and calipers.
+          brakes: {becomes: brake-pads}
+          brake-pads: Brake pads.
+          brake-rotors: Brake rotors and discs.
           filters: Oil, air and cabin filters.
           lighting: null
           wipers: Wiper blades.
~~~

`plan` lists the Claims the mapping moves first:

~~~text
$ cruxible vocabulary plan
Vocabulary plan: .cruxible/vocabulary.yaml (based on 732d4d75563b)
Claims:
  CLM-027c62b3b1f74fb09b9f5127c80c930e catalog.part/bp-2210 catalog.part.category: brakes becomes brake-pads
  CLM-5b670cc2987466db3e186ed831ca8d5e catalog.part/bp-1142 catalog.part.category: brakes becomes brake-pads
Changes:
  replace ClaimType:catalog.part.category (member_descriptions, value)
Preflight: passed

$ cruxible vocabulary apply --rationale "Split brakes: the shop now stocks rotors; both brake parts on file are pads."
...
landed: accepted at 10d6f3b38894

$ cruxible query catalog.part --select category
subject               category    flags
catalog.part/af-208   filters     -
catalog.part/bp-1142  brake-pads  -
catalog.part/bp-2210  brake-pads  -
catalog.part/wb-330   wipers      -
~~~

The new definition and the two re-authored Claims land as one generation.
Each Claim keeps its identity, so the field's history reads as revisions
(`cruxible get catalog.part/bp-1142 --detail history --field category`).
`vocabulary export` then writes the enum without `brakes`.

When the old member's Claims go different ways (some pads, some rotors), a
mapping can't say which is which. Split in three steps instead, each a
reviewed change: add the new members in the file and apply; move each Claim
with a write (`batch.set(part, "category", "brake-rotors")`, one `write` for
all of them); then drop the old member in the file and apply. With nothing
left on it, dropping it carries every Claim. A narrowing that would strand a
value is refused before anything lands, and names the value.

## Common tasks

### Add a field

Add an entry under its kind. A field needs only what differs from the
defaults:

~~~yaml
      warranty_months:
        value: int{0..120}
        description: How many months the supplier's warranty runs.
~~~

~~~text
$ cruxible vocabulary apply
...
landed: accepted at d61feee0616d

$ cruxible set catalog.part/bp-1142 warranty_months 24 --because "Acme's warranty card."
...
  set catalog.part/bp-1142 warranty_months: 24  [CLM-ab603653…; verdict supported]
~~~

By default a field is normative and backed by its writer's own words (a
workspace file or section citation is admitted too). Add a
`description`, the roles it takes, and evidence rules when it needs other
evidence ([Evidence and
admission](evidence-and-admission.md#writing-a-fields-rules) shows a field
that requires a supplier's quote). Like any change, the field lands at once
when your approval policy allows it. Numbers with units are clearest as
integers in the smallest unit: store a price in cents, say.

### Add a kind of thing

A new kind needs a **SubjectKind**, which says how its Subjects are named.
Most kinds are keyed: a key field, such as a supplier's name, decides which
Subject a name refers to, so the same supplier is never recorded twice. Give
the key a `canonical_form`, the rule that decides when two spellings are the
same key:

~~~yaml
  catalog.supplier:
    description: A parts supplier, named by its name.
    fields:
      name:
        key: true
        value: 'string{min_len: 1, max_len: 256}'
        description: The supplier's name; its key.
        canonical_form: [nfkc, trim, collapse_whitespace, casefold]
~~~

`apply` adds the SubjectKind and its key field together. Name a new Subject by
its key, and Cruxible creates it and assigns its id:

~~~text
$ cruxible set "catalog.supplier/Bolt Supply" name "Bolt Supply" --because "A second supplier."
accepted (generation 10, 305dc6416d93)
  create catalog.supplier/bolt-supply: created
  + subject catalog.supplier/bolt-supply
~~~

The key is stored in its canonical form (this one folds case), and any
spelling of it finds the same Subject: `cruxible get "catalog.supplier/BOLT
supply"` reads `catalog.supplier/bolt-supply`. A key with a well-known identity
rule, such as a Python package name or a package URL, also names a core
normalizer (`normalizers: {name: {normalizer: pep503, version: 1}}`), so
`Django_REST` and `django-rest` are one package; see [identity
normalizers](cli-reference.md#subject-kinds-and-creation). A kind whose things
have no natural name, such as a remediation task, is `minted: true` instead:
Cruxible gives each a generated id. To bring an existing kind under keys, see
[Subject kinds](cli-reference.md#subject-kinds-and-creation).

### Retire a field

Write `retired: true` on its entry; its live Claims retire with it, and `plan`
says how many:

~~~text
$ cruxible vocabulary plan
...
Changes:
  retire ClaimType:catalog.part.price_cents (0 live Claim(s) retire with it)
Preflight: passed
~~~

`cruxible claim-type retire PREDICATE --because TEXT` does the same from the
command line, and `ChangeSetDraft.retire_claim_type(predicate)` from Python.

### Change what kind of value a field holds

A field whose values change kind, such as text becoming a relation to another
Subject, can't carry its Claims, because their values no longer mean what the
field now says:

~~~text
cruxible.authoring.claim_type_succession_object_kind_change: A successor that changes object_kind cannot carry a live Claim dependent unchanged: its object no longer says what the ClaimType now means.
~~~

Re-author each Claim in the new form, or retire it, in the same change set
([What the file does not do](#what-the-file-does-not-do)). Often it's simpler
to add the new field beside the old one, fill it in, and retire the old one
when nothing reads it.

### Move a paragraph field into prose

A field whose values have grown into paragraphs (a decision's whole ruling, a
long rationale) is prose in the wrong place: reads cut it short, nobody can
filter by it, and it can't be edited like a document. Move the text into a
document, keep a short value that points at it, and retire the long field.

Here, `catalog.decision.ruling` holds each decision's full ruling. Create the
page and name it (`cruxible files add decisions docs/decisions.md`), then make
the move in one write: each ruling becomes a section with an `id`, a one-line
`summary` cites its section, and the long Claim is retired. Preview it with
`dry_run=True`, then write it:

~~~python
from cruxible_client import Cruxible, SlotRef

cx = Cruxible.connect()
batch = cx.changes(
    because="Move decision rulings into the decisions page; keep a one-line summary that cites each.",
    subject="catalog.decision/ceramic-pads-default",
)
batch.append_section("docs/decisions.md", under=("Decisions",), id="ceramic", text=(
    "## Ceramic pads by default\n\n"
    "We stock ceramic front pads (bp-2210) as the default for the 2019 Civic instead of\n"
    "the semi-metallic bp-1142. Customers complained about brake dust and noise ...\n"
))
batch.set("summary", "Ceramic front pads (bp-2210) are the default for the 2019 Civic.",
          evidence=batch.section("ceramic"))
batch.retire(SlotRef(field="ruling"), reason="superseded")
preview = batch.write(dry_run=True)
preview.status                                    # 'would_accept'
[(c.op, c.field, c.verdict) for c in preview.changes]
# [('set', 'summary', 'supported'), ('retire', 'ruling', None)]
outcome = batch.write()                           # refused if either field moved since the preview
outcome.status                                    # 'accepted'
~~~

The write is checked against the coordinate the connection read at, so a
field that moved after the preview refuses instead of being overwritten. For
MCP-only clients, the same write is one `cruxible_write` call (`dry_run: true`
previews it, and `at` names the coordinate you read at):

~~~json
{
  "because": "Move decision rulings into the decisions page; keep a one-line summary that cites each.",
  "subject": "catalog.decision/ceramic-pads-default",
  "docs": [
    {
      "op": "append",
      "id": "ceramic",
      "file": "docs/decisions.md",
      "under": ["Decisions"],
      "text": "## Ceramic pads by default\n\nWe stock ceramic front pads (bp-2210) as the default for the 2019 Civic instead of\nthe semi-metallic bp-1142. Customers complained about brake dust and noise ...\n"
    }
  ],
  "changes": [
    {
      "op": "set",
      "field": "summary",
      "value": "Ceramic front pads (bp-2210) are the default for the 2019 Civic.",
      "evidence": {"kind": "section", "ref": "ceramic"}
    },
    {"op": "retire", "target": {"field": "ruling"}, "reason": "superseded"}
  ]
}
~~~

With `wiper-brand` moved the same way, each decision reads as its one-line
summary:

~~~text
$ cruxible query catalog.decision --select status,summary
subject                                status    summary                                                       flags
catalog.decision/ceramic-pads-default  accepted  Ceramic front pads (bp-2210) are the default for the 2019 C…  -
catalog.decision/wiper-brand           accepted  We carry one wiper blade line, Acme's wb-330.                 -
~~~

The section counts as your own words, so the summary is `supported`, and an
edit to the section later brings the summary back as `citation_drifted`
([Keeping knowledge current](keeping-knowledge-current.md#when-a-field-turns-into-prose)).
The old rulings stay readable in each decision's history.

Once no live Claim uses the long field, retire its definition: write
`retired: true` on its entry in the vocabulary file and apply it, or run
`cruxible claim-type retire catalog.decision.ruling --because "…"`. The
retired Claims stay readable in history. Afterwards the field is gone from
the vocabulary, and a write to it is refused, as here for a retired
`price_cents`:

~~~text
$ cruxible set catalog.part/bp-1142 price_cents 4200 --because "…"
refused (generation 13, c52cbf99eebb)
cruxible.write.unknown_field (change 0): catalog.part has no field 'price_cents'
  repair: Use a field of catalog.part; cruxible orient --kind catalog.part lists them
~~~

To keep the field with a tighter shape instead, lower its `max_len` in the
file after the move; a long value still in it can't be carried, so move every
one first.

### Adopt a built-in field

Cruxible ships `semantic.alias` and `semantic.distinct_from` ([Keeping
knowledge current](keeping-knowledge-current.md#duplicates-beta)). An instance
created before one shipped adopts it with `cruxible claim-type propose
--builtin NAME`.

## What the file does not do

The file covers the common changes: adding and editing kinds and fields,
retiring them, and moving an enum's Claims by a mapping. A few things go
another way:

- **A judgment per Claim.** When a field changes what kind of value it holds,
  or an old member's Claims go different ways, each Claim needs its own new
  value. Widen in the file, move the values with writes, then narrow in the
  file ([the worked example](#a-worked-example-splitting-a-category)). Or,
  from Python, change the field and every dependent in one change set:
  `ChangeSetDraft.succeed_claim_type(successor, dependents=[...])`, with
  `carry`, `re_author`, `retire` and `rescind` for each dependent ([For AI
  agents](for-ai-agents.md#evolving-vocabulary)); `cruxible claim-type
  migrate` takes the same change as a file.
- **Other definitions.** Saved queries, CaptureKinds, Contracts, Procedures
  and Documents go through authoring (`cruxible authoring submit`,
  `capture-contract propose`, `files propose`).
- **One field from the command line.** `cruxible claim-type propose --input
  FILE` still proposes a single ClaimType, and `claim-type retire` retires
  fields; export afterwards so the file catches up.

## Upgrading a kit you've edited

A kit's definitions are yours to change, and a newer release of the kit has to
decide whether to keep your change. `kit add` with a newer release previews
the upgrade, and names every definition where your version differs from the
kit's:

~~~text
$ cruxible kit add ./car-parts-2.1.0
car-parts 2.1.0: would_propose (upgrade from 2.0.0)
Built by owner on inst_0f4ae3ef62c94365 at 04b09ca6417c (claimed)
ClaimType:
  replace: ClaimType:catalog.part.title (overwrites your edit)
  add: ClaimType:catalog.part.warranty_months
unchanged: 24 (--detail lists each)
Preview at b307b5d080ff; nothing was written.
Commit it: rerun the same command with --commit --at b307b5d080ff
~~~

Here the shop reworded the title field's description, and the release
rewords it too. `--keep IDENTITY` keeps your version of one definition, and
`--keep-local-edits` keeps every one you've changed:

~~~text
$ cruxible kit add ./car-parts-2.1.0 --keep ClaimType:catalog.part.title --commit --at b307b5d080ff
car-parts 2.1.0: accepted (upgrade from 2.0.0)
...
ClaimType:
  keep: ClaimType:catalog.part.title (keeps your edit)
  add: ClaimType:catalog.part.warranty_months
...

$ cruxible kit status
...
car-parts 2.1.0 content sha256:df724fce…; update check local source
  source: car-parts-2.1.0
  built by owner on inst_0f4ae3ef62c94365 at 04b09ca6417c (claimed)
  kept: ClaimType:catalog.part.title (your edit)
~~~

Write the identity as the preview prints it: a `--keep` that matches nothing
in the plan is ignored without a message, so check that the line reads `keep:`
before you commit. The kept divergence is recorded, so a later release
that leaves that definition alone doesn't ask again.

The upgrade treats a field of your own on the kit's kind the same way when the
release brings a field of that name: the plan lists it as `replace: ... (takes
over a definition you defined outside the kit)` and names the `--keep` that
keeps yours. A field of yours the release does not define is not in the plan
and stays as it is. A release that narrows a
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
- **Editing an old export.** `plan` and `apply` read at the export the file
  names; a field someone changed since refuses. Re-export, then edit.

## Where to go next

- [Building team knowledge](building-team-knowledge.md): choosing fields and
  kinds well in the first place.
- [Evidence and admission](evidence-and-admission.md): the evidence rules
  every field defines.
- [Kits](kits.md): installing and upgrading shared vocabularies.
- The [CLI reference](cli-reference.md#claim-type) for `claim-type`, and
  [authoring](cli-reference.md#authoring) for change-set members.
