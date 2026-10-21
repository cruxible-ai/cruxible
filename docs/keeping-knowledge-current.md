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
blocking  claim_conflicted  subjects/catalog.part/bp-2210.json  ...
repair  citation_drifted  Claim:CLM-14179bd0…  ...
repair  claim_contradicting_evidence_available  Claim:CLM-14179bd0…  ...
repair  proposal_stale  sha256:d23468f3…  ...
warning  evidence_expiring  Claim:CLM-00e10f50…  ...
warning  projection_backing_stale  civic#parts  ...
~~~

Each row starts with its severity (`blocking`, `repair`, `warning`), then its
reason, then what it concerns, and the line under it names a repair command.
Rows are read from what changed, so the queue stays cheap to read however much
the instance holds.

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
| Predictions | `prediction_settleable` | A prediction's window closed and it can be settled. |
| Upgrades | `upgrade_available` | A newer compiler revision can be adopted. |

## Working the common rows

### Drift: a source changed under a Claim

A supplier rewrote the catalog line that `bp-2210`'s fitment cites. The Claim
comes back as `citation_drifted`. Read the new text, then decide:

- **It still holds**: restate it against the new text. Retire the drifted
  Claim and state it again citing the new passage, in one write:

  ~~~yaml
  because: Acme reworded the catalog entry; the fitment still holds.
  changes:
    - {op: retire, target: CLM-fba5b025…, reason: superseded}
    - op: add
      subject: catalog.part/bp-2210
      field: fits
      value: catalog.vehicle/honda-civic-2019
      evidence: {kind: file, file: "catalogs/acme-2026.md#bp-2210 Ceramic front brake pads. Fits: 2019 and 2020 Honda Civic"}
  ~~~

  This makes a new Claim: you judged the fitment again against the new text,
  and the field's history (`get catalog.part/bp-2210 --detail history --field
  fits`) shows both. A dedicated way to re-confirm a Claim in place is planned.
- **It should now say something different**: write the new value citing the
  new text.
- **It no longer holds**: retire it (`cruxible retire CLM-… --reason was-wrong
  --because "…"`).

A governed Document that changed (`document_modified`) is resolved by
proposing its next revision (`cruxible files propose --file NAME --name
NAME`), which goes through review like any change.

Before editing a file that Claims cite, `cruxible coverage resolve --file
PATH` lists which Claims rest on which of its lines.

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

A `claim_uncovered` row means a value landed without admitted evidence. Cite
real evidence for it, or, if every write to that field lands uncovered, fix the
field's rules: [Evidence and
admission](evidence-and-admission.md#rules-are-per-role) explains the usual
cause.

### Pages that fell behind

A generated table regenerates itself: `cruxible block sync --all` re-renders
every stale rendered block from its query. A passage someone wrote is only
flagged: re-read it against the current state, rewrite it if needed, and
restamp it with `cruxible block repin FILE BLOCK`, which is the author's
declaration that it's right again. See [Documents and
notes](documents-and-notes.md#pages-that-reflect-state-projection-blocks).

### Proposals waiting or stale

`proposal_awaiting_approval` waits for someone allowed to approve. A
proposal that went stale because other changes landed first doesn't need to be
redone: approving it recompiles it against the current state and lands it if
it still applies.

~~~text
$ cruxible proposal approve sha256:d23468f3… --yes
...
  set catalog.vehicle/honda-civic-2019 model: Civic -> Civic Sedan  [CLM-…]
accepted (978e04dc4f9c) by YOUR_ID
~~~

If what it changes has itself moved, approval is refused, and `cruxible
proposal readmit` rebases it as a new proposal to review.

### Automation and upgrades

Rows from Lines (an unread inbox, missed scheduled work, a Line disabled after
its definition changed) are explained in [Procedures and
Lines](concepts.md#procedures-and-lines). A compiler upgrade is covered in
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

- **The same thing**: add the other spelling as an alias of the Subject you
  keep (`cruxible add catalog.part/bp-1142 semantic.alias bp1142 --because
  "…"`). References to the alias then resolve to the Subject you kept.
  Retiring the duplicate Subject itself isn't possible yet; it's coming.
- **Different things that look alike**: say so with `semantic.distinct_from`
  (`cruxible add catalog.part/wb-330 semantic.distinct_from catalog.part/wb330
  --because "Different suppliers' part numbers."`).

Both are ordinary fields, so the judgment has its reason and its history like
any other Claim. `cruxible curation list` shows the items detection recorded,
and `overrule`, `suppress`, `unsuppress` and `accept-fixed` close them out.

## What to verify next: audit

`next` is about what changed. `cruxible audit` is about what was never
checked well: it ranks Claims worth verifying by their stake, how weak their
evidence is, and how long since anyone looked. Work from the top, reading each
Claim with `get` and its evidence with `--detail evidence`.

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
  [Procedures and Lines](concepts.md#procedures-and-lines) for what to hand to
  Lines first.
- **A person, on review**: approve proposals, decide disagreements and
  contradictions, and rewrite the summaries people rely on.

## Where to go next

- [Evidence and admission](evidence-and-admission.md): what evidence a field
  admits, verdicts and freshness.
- [Building team knowledge](building-team-knowledge.md): modeling that keeps
  maintenance small.
- [Staying in step](concepts.md#staying-in-step): the concepts behind `next`
  and projection blocks.
