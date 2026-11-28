# Documents and notes

Most of what a team knows is read in documents: notes, write-ups, policies,
status pages. Cruxible connects documents and typed state in both directions.
A note can back a Claim, so the explanation and the fact land together and
stay together. A passage of a page can reflect state, so it's flagged the
moment it falls behind. And a whole file can be governed, when its exact
wording is the thing that matters.

This guide covers naming files so Cruxible can refer to them, writing notes
with the Claims they back, citing what's already written, governing whole
Documents, linking notes and guidance, and keeping pages in step with
projection blocks. It uses the car
parts example from [Concepts](concepts.md#knowledge-as-claims).

## Naming files: the catalog

Cruxible refers to workspace files by a stable name kept in the file catalog,
`.cruxible/files.yaml`:

~~~text
$ cruxible files add fitment docs/fitment.md
Added file fitment -> docs/fitment.md (no Document: cite it, or project blocks into it) in .cruxible/files.yaml
~~~

The name is a stable handle: the catalog maps it to the file's current path,
so citations and blocks keep working when the file moves. A file must exist
before it can be named, and `files add PATH` without a name derives one from
the path. You don't have to catalogue a file before citing it: a write that
creates a file, or cites one by its path, catalogues it for you under a name
derived from the path, and you go on referring to it by path. For a page
you'll refer to often, or to choose the name, create it and name it first.

## Writing notes with the Claims they back

A `write` can carry document changes beside value changes, and a change can
cite a section the same write produces. The note and the Claim land in one
change, or neither does:

~~~python
from cruxible_client import Cruxible

with Cruxible.connect() as cx:
    w = cx.world()
    check = cx.section(
        "docs/fitment.md",
        heading="bp-1142 on the 2019 Civic",
        under=("Fitment notes",),
        body="Fitted to a 2019 Civic at the shop on 2026-10-21: front axle, no issues.",
    )
    outcome = w.catalog.part["bp-1142"].add(
        fits=w.catalog.vehicle["honda-civic-2019"],
        evidence=check,
        because="Fitment check at the shop.",
    )
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

- **`cx.section(file, heading=..., body=..., under=...)`** (or
  `append_section` / `op: append` in an explicit batch or write file) adds a
  new section under the heading path in `under` (`()` appends at the end of the
  file). With `cx.section`, Cruxible writes the heading line at the depth its
  place needs; with `append_section`, `text` starts with the heading line. Only
  an append at the root (`under` empty) creates a missing file; an append under
  a heading needs the file and that heading to exist, and refuses
  `cruxible.doc.section_not_found` otherwise, writing nothing. To start a new
  file under a heading, append the heading at the root first.
- **`cx.section(..., replace=True, expect=digest)`** (or **`replace_section`**
  / `op: replace` in an explicit batch or prose-only write) replaces the section
  at a heading path, including its heading, in a file that exists. By default it
  overwrites the current text and keeps the old version in history. Pass `expect`
  when others may be editing, using the section's digest when you read it (each
  write reports the digest it wrote): if someone changed the section since then,
  nothing is written.
  Read `get file:<catalog-name> --detail history` to see the old version's
  digest and the author who replaced it; each entry links to its retained bytes.
  History records an edit only after the file write succeeds; a local file
  conflict leaves history unchanged.
  The client confirms each landed replacement after the file write; a token
  stays confirmable while its pending record exists, and other writers cannot
  evict it. Unconfirmed records more than 24 hours old may be removed when the
  same file is written again, and a removed token reads as unknown. A
  confirmation batch records valid tokens even if another token is unknown,
  and reports each result in `doc_confirmations`. Do not resend unknown
  tokens. A client crash after writing the file but before confirmation can
  leave that edit without retained history.
  File overwrite history is live operational state. An `at` pin to an earlier
  accepted generation refuses; a pin to the current head is allowed.
- **Shared section evidence:** pass the same `cx.section(...)` object as
  `evidence=` on every Claim in a World write to author the section once and
  cite it on all of them (in an explicit batch or write file, name it with `id`
  and cite it with `batch.section(ID)` or `{kind: section, ref: ID}`).

Here the note is tidied in a prose-only `cx.changes` batch using the digest the
write above reported, and a second replace that still passes that digest is
refused, because the first one changed the section:

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

### Recording a decision with its evidence

Evidence belongs to each Claim, not to the Subject. A ruling recorded as a
decision Subject is several Claims (its summary, status, date, what it
governs), and each one cites the section that gives the reasoning. Write them
in one `write` with the section, so every Claim cites it, the date included:

~~~python
why = cx.section(
    "docs/decisions.md",
    heading="Ceramic pads for the 2019 Civic",
    under=("Decisions",),
    body=(
        "Decided 2026-11-12. We stock ceramic pads (bp-2210) rather than organic ones\n"
        "for the 2019 Civic: they last about twice as long in city driving."
    ),
)
(
    w.catalog.decision.define("civic-brake-pads")
    .set(
        summary="Stock ceramic pads (bp-2210) for the 2019 Civic",
        status="accepted",
        decided_on="2026-11-12",
        evidence=why,
    )
    .add(governs=w.catalog.vehicle["honda-civic-2019"], evidence=why)
    .write(because="Brake pad choice for the 2019 Civic.")
)
~~~

A value written later, in a write that does not produce the section, cites
it by file and heading path instead, as in [Citing what's already
written](#citing-whats-already-written). Writing a value and its date
together with the reasoning they rest on keeps them one change: all of it
lands, or none of it.

To see what backs a decision, read its Claims: `cruxible query
catalog.decision --select status,decided_on --claims` lists each cell's Claim
ID, and `get CLAIM_ID --detail evidence` shows what that Claim rests on.
`get` on the Subject itself takes no `evidence` detail (it refuses
`cruxible.get.detail_unsupported` and lists the details it takes):

~~~text
$ cruxible get CLM-65f3a838… --detail evidence
value: 2026-11-12
capture CAP-a344c0846930  CaptureContract:cruxible.coordinator-self-source-v1 v1  source=docs/decisions.md  ...  own words in docs/decisions.md (181 bytes): admitted
rationale: Brake pad choice for the 2019 Civic.
~~~

### Adding to a section without disturbing what cites it

A Claim that cites a section commits to that section's bytes: its heading and
its own body, up to its first subsection. Replacing the whole section with
`replace_section`, even to reword one sentence, changes those bytes, so every
Claim citing it comes back as `citation_drifted` for someone to re-bind or
retire; and the replacement drops the section's subsections too. To add to a
decision or a note, append a subsection under it instead:

~~~python
later = cx.changes(because="Follow-up on the Civic pad ruling.")
later.append_section("docs/decisions.md",
                     under=("Decisions", "Ceramic pads for the 2019 Civic"), text=(
    "### Follow-up, 2026-11-20\n\nFleet customers report no squeal after a week.\n"
))
later.write()
~~~

The four Claims citing the ruling stay current. Replace a section only when
what it says has changed, and then re-check each Claim `next` brings back.

A field that requires captured evidence refuses a section, whether the same
write produces it or it already exists (`cruxible.write.evidence_required`):
"captured" means a shaped payload under one of the field's CaptureContracts,
and neither your own words nor a quote from a workspace file is one.

~~~text
$ cruxible write price-from-note.yaml
refused (generation 9, 7ad067b7d160)
cruxible.write.evidence_required (change 0): price declares evidence_requirement 'captured' (web.search-results): a shaped payload under a declared contract; a file quote does not satisfy it
  nearest: web.search-results
  repair: Cite a Capture under web.search-results: contract evidence naming it (--evidence-contract) or a Capture handle (--capture); or add a shaped CaptureContract to price's evidence (evidence: [<contract name>, ...] in the vocabulary file)
~~~

Write the note for the reasoning, and back such a field with a Capture
([Evidence and admission](evidence-and-admission.md#requiring-captured-evidence)).

A write can also change documents alone. When it does, Cruxible reminds you
that nothing cites the text:

~~~python
prose = cx.changes(because="Shop notes on wiper blades.")
prose.append_section("docs/fitment.md", under=("Fitment notes",), text=(
    "## Wiper blades\n\nNotes from the shop on fitting wiper blades.\n"
))
written = prose.write()
written.hint
# 'No Claim cites this text. If it records a belief or decision, add a Claim citing the section; to keep it as a note about Subjects, link it with an about change (cruxible note); a draft stays prose.'
~~~

Document changes are applied only once the write is accepted (or is waiting
for approval); a refused write or a dry run (`--dry-run`, `dry_run=True`)
touches no file. Files under `.cruxible` and `.git` can't be written this way,
and neither can a projection block (see below).

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
Either is a citation of a workspace file. Every field admits file and section
citations wherever it admits your own words, with no rule to write; only a
field that requires captured evidence refuses them ([Evidence and
admission](evidence-and-admission.md)).
A file the catalog does not name yet is catalogued when the write is admitted,
and the outcome lists it under `catalogued` (a dry run says it would be), so
there is no need to catalogue it first. The quoted anchor is text on one line,
found exactly once in the file, and is exactly what the Claim cites: for a
multi-line passage, quote the line that decides it, or cite the section. A line
that appears more than once in the file cannot be quoted this way; quote a
nearby line that is unique.

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

The Document fields on `files add` go together: `--document-id`,
`--document-kind`, `--title` and `--scope` (the media type comes from the file
suffix unless `--media-type` gives it). Give all of them or none; an entry
with only some refuses (`cruxible.files.catalog_edit_refused`) and the catalog
is left as it was. An entry with none is a file to cite and to project blocks
into, not a Document. `--replace` updates an entry that is already catalogued.

## Linking notes and guidance

A link points prose at state without changing either. `cruxible link` makes
the two kinds, and a mismatched argument refuses with which is which:

- **A note** links a *section* `about` Subjects: it says what that text is
  about. It is a value-lane write, under the same governance as `set`, and
  `backlinks` on the Subject lists it. The note has its own handle, which
  `get` reads:

  ~~~text
  $ cruxible link docs/fitment.md#'Fitment notes/Wiper blades' \
      --about catalog.part/wb-500 --because "Shop notes on this blade"
  accepted (generation 10, 2d6011f047c5)
    note docs/fitment.md#Fitment notes/Wiper blades about catalog.part/wb-500  [NOTE-7c16a8c253ba05b9d4f65e26f12287a4]
  $ cruxible get NOTE-7c16a8c253ba05b9d4f65e26f12287a4
  note: Note:NOTE-7c16a8c253ba05b9d4f65e26f12287a4
  file: fitment
  section: Fitment notes, Wiper blades
  about: catalog.part/wb-500
  capture: CAP-4a1fa58a26de
  lifecycle: live
  next: cruxible backlinks catalog.part/wb-500
  next: cruxible get CAP-4a1fa58a26de --detail summary
  ~~~

  `cruxible note` writes the section and links it in one step: an append of
  the text plus the `about` link, a note and never a Claim.

  ~~~text
  $ cruxible note docs/fitment.md --under 'Fitment notes' --heading "wb-500 squeak" \
      --text "Squeaks on dry glass." --about catalog.part/wb-500 --because "Customer report."
  accepted (generation 13, 7787f0c92c5e)
    doc docs/fitment.md ['Fitment notes', 'wb-500 squeak']: written (sha256:cd024f87…)
    note docs/fitment.md#Fitment notes/wb-500 squeak about catalog.part/wb-500  [NOTE-7fe0ae4f8df1e64027a67ea6017bcbe8]
  ...
  $ cruxible backlinks catalog.part/wb-500
  backlinks of catalog.part/wb-500 (generation 13, 7787f0c92c5e): 2
    about Note NOTE-7c16a8c253ba05b9d4f65e26f12287a4 docs/fitment.md#Fitment notes/Wiper blades  [unchanged]
    about Note NOTE-7fe0ae4f8df1e64027a67ea6017bcbe8 docs/fitment.md#Fitment notes/wb-500 squeak  [unchanged]
  ~~~

  The Subject's own `get` card points to its notes, never their prose (up to
  five; `get --detail notes` pages them all, each with its read), and
  `cruxible section` reads one section on demand. Floor Subject files list the
  same locators under `notes:`.

  ~~~text
  $ cruxible get catalog.part/wb-500
  catalog.part/wb-500  (live, 0 incoming)
    ...
  note docs/fitment.md#Fitment notes/Wiper blades  [unchanged]
  note docs/fitment.md#Fitment notes/wb-500 squeak  [unchanged]
  next: cruxible get catalog.part/wb-500 --detail notes
  $ cruxible section 'docs/fitment.md#Fitment notes/wb-500 squeak'
  ## wb-500 squeak

  Squeaks on dry glass.
  ~~~

- **Guidance** links a *whole registered page* to a ClaimType or Procedure as
  its review guidance. The link is recorded on the page's catalog entry and the
  page's Document revision is proposed with it; `proposal review` of a change
  touching that field shows the page. The page must be in the file catalog
  first, so register it, then link it:

  ~~~text
  $ cruxible files add docs/fitment-review.md
  Added file doc-docs-fitment-review.md-33671a52e1f3 -> docs/fitment-review.md (no Document: cite it, or project blocks into it) in .cruxible/files.yaml
  $ cruxible link docs/fitment-review.md --guides ClaimType:catalog.part.fits
  doc-docs-fitment-review.md-33671a52e1f3: guides ClaimType:catalog.part.fits (admitted)
  Declared Document doc-docs-fitment-review.md-33671a52e1f3 on its catalog entry
  Proposal: sha256:e973524e…
  landed: accepted at 263de78e00c3
  Recorded in files.yaml; files propose keeps these links
  ~~~

  Without the `files add`, the link refuses and names it:

  ~~~text
  $ cruxible link docs/fitment-review.md --guides ClaimType:catalog.part.fits
  refused (not submitted)
  cruxible.link.file_unregistered: docs/fitment-review.md is not a registered workspace file; guidance links a page in the file catalog, the one its Document compiles from
    repair: Register it first: cruxible files add docs/fitment-review.md
  ~~~

The file is named by its path or its catalog name. Linking an existing section
`--about` Subjects also needs the file in the catalog
(`cruxible.doc.file_uncatalogued`, with the same `files add` repair); a section
that `cruxible note` or a `write` appends catalogues its file itself. `files
add` is a CLI step: MCP has no tool that edits the file catalog. Later `files
propose` runs keep the guidance links, and `--unlink` removes either kind. A
page catalogued without a Document becomes one under its catalog name when it
is first linked as guidance.

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
     repair: cruxible block repin civic summary
     also: blocking  projection_dirty  civic#summary
   warning  projection_backing_stale  civic#parts  next=cruxible.block.repin
     repair: cruxible block repin civic parts
   ~~~

4. **Bring it back in step.** `cruxible block sync --all` re-renders every
   stale rendered block, and `cruxible block repin FILE BLOCK` (the repair
   `next` names) renders one again while its body is still its query's
   rendering; FILE is the page's path or its catalog name. An authored block is only flagged: re-read it, rewrite
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
  `files`, `write`, `link`, `note` and `block` option.
