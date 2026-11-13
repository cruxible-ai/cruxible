# Keeping knowledge current

Knowledge goes out of date in ordinary ways: a supplier rewrites a catalog
entry, a quote expires, two people record different answers, a summary falls
behind what it summarizes. Cruxible keeps a team's beliefs consistent at the
cost of what changed: when a source changes, only the beliefs resting on it
come back for review, and everything else stays as it is.

What comes back lands in one place, `next`: the maintenance queue for people
and agents alike. Cruxible shows what changed; it never decides what a change
means. Each row names a repair, and a person or an agent decides whether that
repair is the right one. This guide goes through the queue by the kind of
signal, how to work each one, and a routine for keeping up. It uses the car
parts example from [Concepts](concepts.md#knowledge-as-claims).

## The queue

`next` lists everything that is wrong or waiting, most urgent first:

~~~text
$ cruxible next
blocking  claim_conflicted  subjects/catalog.part/bp-2210.json  next=cruxible.claim.retire
  repair: cruxible retire CLM-f3163493…
  or: cruxible retire CLM-0ce675c0…
repair  claim_uncovered  Claim:CLM-af67bbf9…  (catalog.part/bp-1142 catalog.part.supplier)  next=cruxible.get
  repair: cruxible get ClaimType:catalog.part.supplier
  or: cruxible retire CLM-af67bbf9…
repair  claim_contradicting_evidence_available  Claim:CLM-ef7f11e2…  next=cruxible.authoring.example
  repair: cruxible authoring example claim-revision
  or: cruxible retire CLM-ef7f11e2…
repair  proposal_stale  sha256:928b83fd…  next=cruxible.proposal.readmit
  repair: cruxible proposal readmit sha256:928b83fd…
~~~

Each row starts with its severity (`blocking`, `repair`, `warning`, `setup`),
then its reason, then what it concerns (for a Claim, often its Subject and
field), and the lines under it name a repair and, after `or:`, the
alternatives. `setup` marks a choice you own rather than something wrong: a
Line accepted but not yet enabled (`line_not_enabled`) is one. Setup rows sort
after every problem, and `orient` counts them apart (`setup_items`) from the
problems it counts (`next_items`). Rows are read from what changed, so the
queue stays cheap to read however much the instance holds.

In `--json`, a row whose start state records also carries `since`: when the
condition began. A Line not yet enabled has been since its acceptance, one
disabled by an upgrade since the upgrade, a failed run since it finished, a
coverage gap since its start, pending work since its oldest match, new
evidence since its attestation, a proposal awaiting approval since its
admission, a settleable prediction since its window closed, and a conflict
since the newest of its contenders (or their ClaimType) was accepted. Other
rows leave it out.

## Signals by family

| Family | Rows | What happened |
|---|---|---|
| Drift | `citation_drifted`, `document_modified`, `evidence_unavailable` | A cited passage or a governed Document changed, or stored evidence can't be read. |
| Freshness | `evidence_expiring`, `claim_stale_evidence` | Evidence is about to expire, or has, under the field's `stale_after`. |
| Disagreement | `claim_conflicted`, `claim_contradicting_evidence_available` | Two values compete for a field that holds one, or someone attested against a Claim. |
| Weak evidence | `claim_uncovered` | A value was written that nothing admitted backs. |
| Pages | `projection_backing_stale`, `projection_dirty` | A stamped passage reflects state that moved, or someone edited it. |
| Proposals | `proposal_awaiting_approval`, `proposal_stale` | A change waits for an approval, or the state moved since it was checked. |
| Automation | `line_inbox_unread`, `line_coverage_gap`, `line_work_pending`, `line_disabled_by_upgrade` | A Line posted something, missed work, or stopped after its definition changed. |
| Setup | `line_not_enabled` | A Line was accepted but nobody has enabled it yet. |
| Predictions | `prediction_settleable` | A prediction's window closed and it can be settled. |
| Upgrades | `upgrade_available` | A newer compiler revision can be adopted. |

## Working the common rows

### Drift: a source changed under a Claim

A supplier rewrote the catalog line that `bp-2210`'s fitment cites. The Claim
comes back as `citation_drifted`. Read the new text, then decide:

- **It still holds**: **re-bind** it to the new text. The statement stays as
  it is, and the drifted citation is replaced:

  ~~~text
  $ cruxible next
  repair  citation_drifted  Claim:CLM-ef7f11e2…  next=cruxible.rebind
    repair: cruxible rebind CLM-ef7f11e2… --citation 80ead46d3fd6
    or: cruxible retire CLM-ef7f11e2…

  $ cruxible rebind CLM-ef7f11e2… \
      --file 'catalogs/acme-2026.md#bp-2210 Ceramic front brake pads. Fits: 2019 and 2020 Honda Civic' \
      --because "Acme reworded the catalog entry; the fitment still holds."
  accepted (generation 21, 4f0718f3f36e)
    rebind catalog.part/bp-2210 fits: catalog.vehicle/honda-civic-2019 (value unchanged)  [CLM-ef7f11e2…; verdict supported]
  ~~~

  The Claim keeps its ID, and its history shows one `rebound` revision with
  the old and new evidence (`get catalog.part/bp-2210 --detail history
  --field fits`). Re-binding works within the same file or Document; for a
  section you wrote, use `--section FILE#Heading/Path`. If the text moved to
  a different source, retire the Claim and state it again citing the new one.
- **It should now say something different**: write the new value citing the
  new text.
- **It no longer holds**: retire it (`cruxible retire CLM-… --reason was-wrong
  --because "…"`).

A governed Document that changed (`document_modified`) is resolved by
proposing its next revision (`cruxible files propose --file NAME --name
NAME`), which lands under your approval policy like any change.

Before editing a file that Claims cite, `cruxible coverage resolve --file
PATH` lists which Claims rest on which of its lines:

~~~text
$ cruxible coverage resolve --file catalogs/acme-2026.md
exact  catalogs/acme-2026.md:4-4  catalog.part/bp-2210  fits = catalog.vehicle/honda-civic-2019  [CLM-ef7f11e28462]
...
~~~

### Freshness: evidence that expires

A field with `stale_after` warns with `evidence_expiring` before its evidence
expires, and lists `claim_stale_evidence` after. Re-check the source and cite
the fresh observation: the same value again restores the Claim without a new
change. A different value is a new statement, written as usual.
[Evidence and admission](evidence-and-admission.md#freshness) shows both.

### Disagreement: competing values and contradictions

When two values compete for a field that holds one value, here because
someone recorded a part's title with `--contend` instead of replacing it,
`next` lists `claim_conflicted` until someone picks. Retire the value that's
wrong, saying why:

~~~text
$ cruxible retire CLM-dffb2bf7… --reason was-wrong \
    --because "The catalog name is the canonical title; the box label is a marketing name."
accepted (generation 24, bc3542c8b1b5)
  retire catalog.part/bp-2210 title: Ceramic pads, front  [CLM-dffb2bf7…]
~~~

When someone attests against a Claim (a mechanic reporting that a listed part
doesn't fit), `next` lists `claim_contradicting_evidence_available`. The
verdict doesn't change on its own. Examine the attestation (`get CLM-…
--detail evidence` lists it), then revise or retire the Claim, or leave it if
the contradiction doesn't hold up.

### Weak evidence

A `claim_uncovered` row means a value landed without admitted evidence. The
write that made it already said why:

~~~text
warning cruxible.write.verdict_not_supported: catalog.part/bp-1142 supplier was written, but its verdict is uncovered: it is a observation Claim, and the field's evidence rules admit only normative. Write it in an admitted role, or change the ClaimType's evidence rules.
~~~

Cite real evidence for it, write it in a role the field admits, or, if every
write to that field lands uncovered, fix the field's rules ([Evidence and
admission](evidence-and-admission.md#rules-are-per-role)).

### Pages that fell behind

A generated table regenerates itself: `cruxible block sync --all` re-renders
every stale rendered block from its query. A passage someone wrote is only
flagged: re-read it against the current state, rewrite it if needed, and
restamp it with `cruxible block repin FILE BLOCK`, which is the author's
declaration that it's right again. See [Documents and
notes](documents-and-notes.md#pages-that-reflect-state-projection-blocks).

### Proposals waiting or stale

`proposal_awaiting_approval` waits for someone allowed to approve. A
proposal that other changes overtook doesn't need to be redone when they
touched nothing it touches: it stays open, and approving it lands the same
change set on the current state.

~~~text
$ cruxible proposal approve sha256:d23468f3… --yes
...
  set catalog.vehicle/honda-civic-2019 model: Civic -> Civic Sedan  [CLM-…]
accepted (978e04dc4f9c) by YOUR_ID
~~~

If what it changes has itself moved, it is `proposal_stale` (the row names
the changed paths), approval is refused, and its author's `cruxible proposal
readmit` rebases it as a new proposal to review. [Review and
governance](review-and-governance.md) covers reviewing and approving in full.

### Automation and upgrades

Rows from Lines (an unread inbox, missed scheduled work, a Line disabled after
its definition changed) are explained in [Lines and
automation](lines-and-automation.md). A compiler upgrade is covered in
[Upgrading](upgrading.md). Adding a newer release of a kit proposes its changes,
which you review like any change ([Kits](kits.md)).

## Duplicates (beta)

The same thing recorded twice splits what's known about it.
`cruxible curation duplicates --kind KIND` lists Subjects whose ids, aliases
or key values look the same under a loose comparison (case, punctuation) but
differ under the kind's own rules:

~~~text
$ cruxible curation duplicates --kind catalog.part
Possible duplicates of catalog.part: 2 group(s); read 3 Subject(s), 0 alias(es), 7 value(s).
  subjects 'bp1142': catalog.part/bp-1142 (bp-1142), catalog.part/bp1142 (bp1142)
  ...
~~~

Record your judgment as state, so it isn't asked again:

- **The same thing**: merge them. Add the other spelling as an alias of the
  Subject you keep, then retire the duplicate with its Claims:

  ~~~text
  $ cruxible add catalog.part/bp-1142 semantic.alias bp1142 --because "The same part, typed without the hyphen."
  accepted (generation 28, 420fa99e97cc)
  ...
  $ cruxible retire catalog.part/bp1142 --with-claims --because "Duplicate of bp-1142."
  accepted (generation 29, 039815ca8281)
    retire Subject catalog.part/bp1142  [also retires CLM-3b21c9b9…, CLM-9436ad1b…]
  ~~~

  Without `--with-claims` the retirement is refused while the Subject still
  holds Claims, and the refusal names them. From then on, references to
  `bp1142` resolve to the Subject you kept.
- **Different things that look alike**: say so with `semantic.distinct_from`
  (`cruxible add catalog.part/wb-330 semantic.distinct_from catalog.part/wb330
  --because "Different suppliers' part numbers."`).

Both are ordinary fields, so the judgment has its reason and its history like
any other Claim. `cruxible curation list` shows the items detection recorded,
and `overrule`, `suppress`, `unsuppress` and `accept-fixed` close them out.

## When a field turns into prose

Fields drift toward prose over time. A summary that started as one line picks
up a second sentence, then a paragraph, and a field meant to record a ruling
ends up holding the whole argument for it. Watch for two signs, since nothing
in `next` flags them: values that keep getting longer (reads cut them short
with `…`), and a field that holds reasoning rather than a fact anyone would
filter or count by.

The fix keeps both the reasoning and a value worth querying:

1. Write each long value as a section of a document.
2. In the same `write`, keep a one-line value (a summary or a status) that
   cites the section.
3. Retire the long Claims there too.

From then on, an edit to the section brings the one-line value back for
review as `citation_drifted`, which asks whether the summary still says what
the section says. If it does, re-bind it to the edited section; if not,
rewrite it:

~~~text
$ cruxible next
repair  citation_drifted  Claim:CLM-55627160…  next=cruxible.rebind
  repair: cruxible rebind CLM-55627160… --citation b9091755e6c0 --section 'decisions#Decisions/Ceramic pads for the 2019 Civic'
  or: cruxible retire CLM-55627160…
~~~

[Building team knowledge](building-team-knowledge.md#typed-state-or-prose) has
the rule for what belongs in a field, and [Evolving your
vocabulary](evolving-your-vocabulary.md#move-a-paragraph-field-into-prose)
walks through the move, including retiring the long field.

## What to verify next: audit

`next` is about what changed. `cruxible audit` is about what was never
checked well: it ranks Claims worth verifying by their stake, how weak their
evidence is, and how long since anyone looked:

~~~text
$ cruxible audit
40  catalog.part/bp-2210  fits = catalog.vehicle/honda-civic-2019  [CLM-ef7f11e28462]  stake=1 weakness=4 staleness=10
36  catalog.vehicle/honda-civic-2019  year = 2019  [CLM-13f8500437eb]  stake=1 weakness=3 staleness=12
...
~~~

Work from the top, reading each Claim with `get` and its evidence with
`--detail evidence`.

## None, or unknown

Cruxible's model is never complete, so an empty answer means two different
things. Within a scope someone has read in full, nothing found means **none**:
no recall affects this part. Outside it, nothing found means **unknown**:
nobody has looked. Treat unknown as a to-do: pull the source that would answer
it, by hand or with a Line, and record what you find with its evidence.

## A routine

- **An agent, daily**: run `next` and work the rows it can judge (re-check
  freshness, regenerate pages, restate drifted Claims), then take the top of
  `audit`.
- **Lines, continuously**: the checks that are the same every time, such as
  re-fetching expiring quotes or re-reading a supplier's catalog. See
  [Lines and automation](lines-and-automation.md) for setting one up.
- **A person, on review**: approve proposals, decide disagreements and
  contradictions, and rewrite the summaries people rely on.

## Where to go next

- [Evidence and admission](evidence-and-admission.md): what evidence a field
  admits, verdicts and freshness.
- [Building team knowledge](building-team-knowledge.md): modeling that keeps
  maintenance small.
- [Staying in step](concepts.md#staying-in-step): the concepts behind `next`
  and projection blocks.
