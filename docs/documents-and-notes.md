# Documents and notes

Most of what a team knows is read in documents: notes, write-ups, policies,
status pages. Cruxible connects documents and typed state in both directions.
A note can back a Claim, so the explanation and the fact land together and
stay together. A passage of a page can reflect state, so it's flagged the
moment it falls behind. And a whole file can be governed, when its exact
wording is the thing that matters.

This guide covers naming files so Cruxible can refer to them, writing notes
with the Claims they back, citing what's already written, governing whole
Documents, and keeping pages in step with projection blocks. It uses the car
parts example from [Concepts](concepts.md#knowledge-as-claims).

## Naming files: the catalog

Cruxible refers to workspace files by a stable name kept in the file catalog,
`.cruxible/files.yaml`:

~~~text
$ cruxible files add fitment docs/fitment.md
Added file fitment -> docs/fitment.md (no Document: cite it, or project blocks into it) in .cruxible/files.yaml
~~~

The name is what citations and blocks use, so pick one that will last. A file
must exist before it can be named. A write that creates a new file catalogs it
for you under a generated name, so for a page you'll refer to often, create
it and name it first.

## Writing notes with the Claims they back

A `write` can carry document changes beside value changes, and a change can
cite a section the same write produces. The note and the Claim land in one
change, or neither does:

~~~python
from cruxible_client import Cruxible

cx = Cruxible.connect()
batch = cx.changes(because="Fitment check at the shop.")
batch.append_section("docs/fitment.md", under=("Fitment notes",), id="check", text=(
    "## bp-1142 on the 2019 Civic\n\n"
    "Fitted to a 2019 Civic at the shop on 2026-10-21: front axle, no issues.\n"
))
batch.add("catalog.part/bp-1142", "fits", "catalog.vehicle/honda-civic-2019",
          evidence=batch.section("check"))
outcome = batch.write()
outcome.status                                    # 'accepted'
[(c.field, c.after, c.verdict) for c in outcome.changes]
# [('fits', 'catalog.vehicle/honda-civic-2019', 'supported')]
note = outcome.docs[0]
note.section, note.written                        # (('Fitment notes', 'bp-1142 on the 2019 Civic'), True)
~~~

On the CLI, `cruxible write FILE` takes the same write as a YAML file. For
MCP-only clients, it is one `cruxible_write` call:

~~~json
{
  "because": "Fitment check at the shop.",
  "docs": [
    {
      "op": "append",
      "id": "check",
      "file": "docs/fitment.md",
      "under": ["Fitment notes"],
      "text": "## bp-1142 on the 2019 Civic\n\nFitted to a 2019 Civic at the shop on 2026-10-21: front axle, no issues.\n"
    }
  ],
  "changes": [
    {
      "op": "add",
      "subject": "catalog.part/bp-1142",
      "field": "fits",
      "value": "catalog.vehicle/honda-civic-2019",
      "evidence": {"kind": "section", "ref": "check"}
    }
  ]
}
~~~

You choose where documents live and how they're organized; Cruxible only needs
heading paths to find sections:

- **`append_section`** (`op: append`) adds a new section under the heading
  path in `under` (`()` appends at the end of the file). Its text must start
  with a heading.
- **`replace_section`** (`op: replace`) replaces the section at a heading
  path, including its heading. Pass `expect` with the section's current
  digest (each write reports the digest it wrote): if someone changed the
  section since you read it, nothing is written.
- **`id`** names the section so changes can cite it: `batch.section(ID)`, or
  `{kind: section, ref: ID}` in a write file.

Here the note is tidied with the digest the write above reported, and a
second replace that still passes that digest is refused, because the first one
changed the section:

~~~python
read = note.after_digest
path = ("Fitment notes", "bp-1142 on the 2019 Civic")
tidy = cx.changes(because="Tidy the fitment note.")
tidy.replace_section("docs/fitment.md", path, expect=read, text=(
    "## bp-1142 on the 2019 Civic\n\n"
    "Fitted at the shop on 2026-10-21: front axle, no issues.\n"
))
tidy.write().docs[0].written                      # True
again = cx.changes(because="Tidy it again.")
again.replace_section("docs/fitment.md", path, expect=read, text=(
    "## bp-1142 on the 2019 Civic\n\nFitted at the shop: front axle.\n"
))
again.write()                                     # WriteRefusalError: cruxible.doc.section_changed
~~~

The refusal names the section's digest now and its repair: re-read the
section, then retry with that `expect`.

A section written this way counts as its author's own words, with a location
people can read, and a later edit to it brings every Claim citing it back for
review ([Keeping knowledge current](keeping-knowledge-current.md#drift-a-source-changed-under-a-claim)).

A write can also change documents alone. When it does, Cruxible reminds you
that nothing cites the text:

~~~python
prose = cx.changes(because="Shop notes on wiper blades.")
prose.append_section("docs/fitment.md", under=("Fitment notes",), text=(
    "## Wiper blades\n\nNotes from the shop on fitting wiper blades.\n"
))
written = prose.write()
written.hint
# 'No Claim cites this text. If it records a belief or decision, add a Claim citing the section; if it is a draft or a note, prose is right.'
~~~

Document changes are applied only once the write is accepted (or is waiting
for approval); a refused write or a dry run (`--dry-run`, `dry_run=True`)
touches no file. Files under
`.cruxible` and `.git` can't be written this way, and neither can a projection
block (see below).

## Citing what's already written

To cite a passage of a file that already exists, quote it:

~~~bash
cruxible add catalog.part/bp-2210 fits catalog.vehicle/honda-civic-2019 \
  --because "Acme's 2026 catalog lists bp-2210 for the 2019 Civic." \
  --evidence-file 'catalogs/acme-2026.md#bp-2210 Ceramic front brake pads. Fits: 2019 Honda Civic'
~~~

or cite an existing section by its heading path: in the SDK,
`SectionEvidence(file="docs/fitment.md", section=("Fitment notes", "Wiper blades"))`
from `cruxible_client.contracts.write`, or
`{kind: section, file: docs/fitment.md, section: [Fitment notes, Wiper blades]}`
in a write file.
Either is evidence from a file, not your own words, so the field's rules must
admit that file's source ([Evidence and admission](evidence-and-admission.md)).

## Governed Documents

A citation commits to the bytes it quotes, and stays current as long as they
still occur in the file, however the rest of the file changes. When a Claim
depends on the whole file saying exactly what it says (a returns policy, a
specification), govern the file as a **Document**: its exact bytes become a
versioned artifact with review history, and any change to the file, even an
added line, is reported.

~~~bash
cruxible files add returns docs/returns-policy.md --document-id returns-policy \
  --document-kind policy --title "Returns policy" --scope policies
cruxible files propose --file returns --name returns-policy-v1
~~~

~~~text
$ cruxible next
warning  document_modified  document:returns-policy  next=cruxible.files.propose
  repair: cruxible files propose --file returns --name revise-returns
~~~

Each new revision is proposed the same way, and lands under your approval
policy like any change (`--no-accept` only proposes it). Most usage is
citations; use a Document only when the wording itself is governed.

## Pages that reflect state: projection blocks

A **projection block** is a marked passage of a page that reflects accepted
state, with a stamp recording exactly what it reflects, so Cruxible can tell
when it no longer does. Governed doesn't mean generated: a block is one of two
kinds, and both are watched the same way.

- **Rendered**: a table or list generated from a named query. Use it for
  lists, tables and indexes.
- **Authored**: prose you or an agent wrote, stamped against the query or
  Claims it summarizes. Use it for anything a query can't write.

### The workflow

1. **Write the markers**, with prose between them for an authored block, and
   catalog the page:

   ~~~markdown
   ## Parts that fit

   <!-- cruxible:block:parts -->
   <!-- /cruxible:block:parts -->

   ## Summary

   <!-- cruxible:block:summary -->
   Two brake pad options fit the 2019 Civic.
   <!-- /cruxible:block:summary -->
   ~~~

2. **Stamp them with what they reflect.** `--render` fills a rendered block
   from its query:

   ~~~bash
   cruxible block repin civic parts --query QueryDefinition:catalog.parts_for_vehicle \
     --params '{"vehicle":"honda-civic-2019"}' --render
   cruxible block repin civic summary --query QueryDefinition:catalog.parts_for_vehicle \
     --params '{"vehicle":"honda-civic-2019"}'
   ~~~

   An authored block can be stamped against specific Claims instead, with
   `--claim CLM-…`.

3. **Let `next` tell you.** When the state behind a block moves, `next` lists
   `projection_backing_stale`; when someone edits a block's text by hand, it
   lists `projection_dirty`:

   ~~~text
   $ cruxible next
   blocking  projection_backing_stale  civic#summary  next=cruxible.block.repin
     also: blocking  projection_dirty  civic#summary
   warning  projection_backing_stale  civic#parts  next=cruxible.block.repin
   ~~~

4. **Bring it back in step.** `cruxible block sync --all` re-renders every
   stale rendered block. An authored block is only flagged: re-read it, rewrite
   it if it no longer holds, and `cruxible block repin FILE BLOCK`. A repin is
   your declaration that the passage reflects its backings again.

Before acting on what a page says, check `next` for a stale block on it, and
if there is one, read the Claims behind it with `get`.

### Currency policy

By default (`--currency-policy warn`) a stale block is a warning, and `block
sync` exits zero. Stamped with `--currency-policy require_current`, a block
whose backing moved is a blocking row in `next` and makes `block sync` fail,
which suits pages that gate a release. A hand edit to the block's text is only
reported either way, since it's the author's prose that moved, not the state.
Neither ever blocks accepting the state underneath.

### Blocks are not evidence

A page can hold both passages that Claims cite and projection blocks, but
never in the same place. Citing text inside a block is refused, because a
passage that both reflected state and backed it would let a page vouch for
itself:

~~~text
$ cruxible set catalog.part/bp-1142 title "Front brake pads" --because "…" \
    --evidence-file 'docs/civic.md#Front brake pads'
refused (generation 55, fe1ac3c5eb75)
...
cruxible.projection.evidence_from_projection (change 0): Bytes [163, 179) of source 'civic' lie inside projection block 'parts'. A projection block is never evidence, whatever the citation role: cite the Claims it reflects, or prose outside the block.
  repair: Cite the accepted Claims the block reflects, or select prose outside it.
~~~

For the same reason, a `write` can't change a section that holds a block;
blocks change only through `block repin` and `block sync`:

~~~text
$ cruxible write into-block.yaml
refused (not submitted)
cruxible.doc.projection_edit: A doc write edits prose only; a projection block changes through its backing
  repair: cruxible block repin docs/civic.md parts
~~~

### Taking a block down

`cruxible block depublish FILE BLOCK` releases the block's registration, so
`next` stops asking for it back; it doesn't edit the page. Then `cruxible
block detach PAGE` (the page's path) strips the markers and keeps the text
between them:

~~~text
$ cruxible block depublish civic parts
civic#parts: depublished
$ cruxible block detach docs/civic.md
docs/civic.md#parts: detached
~~~

Detaching a block that is still published is refused, with the depublish to
run first (`refused:block_still_published`).

## Pitfalls

- **Notes beside the knowledge instead of behind it.** A write-up that no
  Claim cites is only prose; if it records a belief or a decision, write the
  Claim with it.
- **Citing a file when the whole file is the point.** Govern it as a Document.
- **Trusting a page without checking `next`.** A block can be behind the
  state it reflects.
- **Editing a block's text by hand and moving on.** Repin it, so the stamp
  says the text was reviewed.

## Where to go next

- [Building team knowledge](building-team-knowledge.md): deciding what
  becomes a Claim and what stays prose.
- [Evidence and admission](evidence-and-admission.md): what evidence a field
  admits.
- The [CLI reference](cli-reference.md#block) for the marker grammar and every
  `files`, `write` and `block` option.
