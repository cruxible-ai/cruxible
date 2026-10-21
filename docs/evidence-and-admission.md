# Evidence and admission

Cruxible holds what your team believes, not what is true, and every belief is
only as good as what it rests on. Each Claim records its evidence, and each
field (each ClaimType) says which evidence it accepts. The result is a
**verdict** on every Claim that says how well its evidence holds up, so a
person or an agent can tell a checked fact from a guess before acting on it.

This guide covers what counts as evidence, how a field decides what to admit,
what verdicts and flags mean, how evidence goes stale, when to cite a passage
and when to govern a whole Document, and what attestations do. It uses the
car parts example from [Concepts](concepts.md#knowledge-as-claims).

## What counts as evidence

A Claim rests on one or more of these:

- **Your own words**: the `because` text every write carries. Right for what
  you state on your own authority: a decision, an owner, a category the team
  assigned.
- **A passage you wrote**: a section of a Markdown document written in the
  same `write` as the Claim that cites it. It is still your own words, with a
  location people can read.
- **A citation**: a passage quoted from a catalogued workspace file, such as
  a supplier's catalog saved in the repository (`--evidence-file
  PATH#ANCHOR`).
- **A Capture**: retained bytes recorded under a CaptureContract, typically a
  response that a Procedure fetched from the outside world.

`get --detail evidence` shows what a Claim rests on and whether each piece
counts. Here, a fitment the shop checked and wrote up, and a fitment cited from
the supplier's catalog:

~~~text
$ cruxible get CLM-a0d72af7… --detail evidence
value: catalog.vehicle/honda-civic-2019
...
capture CAP-5f31df955084  CaptureContract:cruxible.foreign-source.fitment v1  source=fitment  ...  passage of fitment: admitted
capture CAP-c8442c467896  CaptureContract:cruxible.coordinator-self-source-v1 v1  ...  own words: admitted
rationale: Fitment check at the shop.

$ cruxible get CLM-9482bb3b… --detail evidence
value: catalog.vehicle/honda-civic-2019
capture CAP-18a94f6104cb  CaptureContract:cruxible.foreign-source.catalog v1  source=catalog  ...  evidence: admitted
rationale: Acme's 2026 catalog lists bp-2210 for the 2019 Civic.
~~~

Each line ends with how the verdict reads it. A copy is labeled by where it
came from (`own words`, or `passage of` a catalogued file), and anything else
as `evidence`. Each is then `admitted` (the field's rules accept it), `not
admitted` (offered as evidence, refused by the field's rules) or `not
evidence` (a copy the rules don't count).

## How a field admits evidence

### Rules are per role

A Claim has a role: **normative** for what someone states should be (a
decision, an assignment), **observation** for what was seen in the world. A
field's evidence rules are set per role, and the two want different evidence.
Your own words are good evidence for a normative statement you make; they
aren't evidence that a brake pad fits a car. A field whose rules don't match
the role its Claims are written with records every value as `uncovered`
([Building team knowledge](building-team-knowledge.md#use-the-normative-role-for-what-you-state-yourself)
shows that mistake).

### Requiring captured evidence

A field can require captured evidence, so that your own words are never
enough. A price the supplier quoted shouldn't rest on someone's memory of a
phone call:

~~~text
$ cruxible set catalog.part/bp-2210 price 39 --because "Acme quoted 39 on the phone."
refused (generation 15, 65a4644294d3)
cruxible.write.evidence_required (change 0): price declares evidence_requirement 'captured' (…); your own words cannot back it

$ cruxible set catalog.part/bp-1142 price 42 --because "Acme quote of 2026-10-21." \
    --evidence-file 'quotes/acme-2026-10-21.md#bp-1142 Front brake pads: 42 USD'
accepted (generation 11, 3a2b5a725645)
  set catalog.part/bp-1142 price: 42  [CLM-…; verdict supported]
~~~

"Captured" rules out your own words, but a passage of a catalogued workspace
file still counts, as the second command shows. Workspace files are files an
agent can write, so they are the right evidence for documented reasoning, not
for facts about the outside world. For a fact that must come from outside, a
supplier's price or a recall notice, admit only contracts whose Captures are
fetched by a run. A fetched Capture counts only when the daemon's journal
records the run that produced it, so its bytes can't be passed off as
something a run observed.

### Seeing a field's settings

Fields are queryable like everything else:

~~~text
$ cruxible query ClaimType --where 'name~catalog.part' --select predicate,role,stale_after,captured_required
predicate              role         stale_after  captured_required
catalog.part.category  observation  -            false
catalog.part.fits      observation  -            false
catalog.part.name      normative    -            false
catalog.part.price     observation  2m           true
catalog.part.replaces  observation  -            false
catalog.part.title     observation  -            false
~~~

Defining a field's evidence rules is part of defining the field; see
[Changing knowledge](concepts.md#changing-knowledge).

## Verdicts and flags

Every Claim has a verdict:

| Verdict | Means |
|---|---|
| `supported` | Admitted evidence backs it. |
| `uncovered` | Nothing it cites is admitted by the field's rules. |
| `stale_evidence` | Its evidence is older than the field allows (see below). |
| `stale` | Its evidence no longer describes the period the Claim is about. |
| `contradicted`, `unresolved` | Admitted evidence both supports and contradicts it; the field's rules decide which of the two it reads as. |

Reads carry **flags** beside each value (`stale`, `contested`,
`contradicted`, `uncovered`, `unsure_hold`), so weak knowledge looks weak
wherever it's read. Check the verdict on every write: most writes land even
when their evidence isn't admitted (a field that requires captured evidence is
the exception, and refuses), and anything other than `supported` comes with a
warning.

## Freshness

Some facts go out of date by themselves: a price, a stock level, a supplier's
lead time. A field can set how long its evidence stays fresh (`stale_after`).
The age counts from the most recent observation, so re-checking a source and
finding the same value keeps the Claim fresh without changing it.

Shortly before a Claim's evidence expires, `next` warns with
`evidence_expiring`. Once it has expired, the verdict becomes
`stale_evidence`, reads flag the value `stale`, and `next` lists the Claim as
`claim_stale_evidence`:

~~~text
$ cruxible query catalog.part --select title,price
subject               title                     price  flags
catalog.part/bp-1142  Front brake pads          42     stale
catalog.part/bp-2210  Ceramic front brake pads  -      -
~~~

Citing a fresh observation of the same value restores it, and records the
re-check rather than a new change:

~~~text
$ cruxible set catalog.part/bp-1142 price 42 --because "Acme quote of 2026-10-21, re-checked." \
    --evidence-file 'quotes/acme-2026-10-21.md#bp-1142 Front brake pads: 42 USD'
accepted (generation 15, 65a4644294d3)
  set catalog.part/bp-1142 price: 42 -> 42  [CLM-…; verdict supported]
~~~

A Line can do the re-checking on a schedule; see [Procedures and
Lines](concepts.md#procedures-and-lines).

## Citation or Document

A **citation** commits to the exact bytes it quotes. It stays current as long
as those bytes still occur in the file unchanged, even if the file grows
around them: adding a filters section to the supplier's catalog leaves the
brake pad citation alone. When the quoted passage itself changes, `next`
reports `citation_drifted` for every Claim that cites it.

Sometimes a Claim depends on a whole file saying exactly what it says and
nothing else: a returns policy, a specification. Make that file a governed
**Document**. Its exact bytes become a versioned artifact with review history,
and any change to the file is reported, even an added line:

~~~bash
cruxible files add returns docs/returns-policy.md --document-id returns-policy \
  --document-kind policy --title "Returns policy" --scope policies
cruxible files propose --file returns --name returns-policy-v1 --commit
~~~

~~~text
$ cruxible next
warning  document_modified  document:returns-policy  next=cruxible.files.propose
  repair: cruxible files propose --file returns --name revise-returns
~~~

Most usage is citations. [Evidence](concepts.md#evidence) has the comparison
in full.

## Attestations

Anyone can **attest** to a Claim with a signature: support it, contradict it,
or say they're unsure. When a mechanic finds that a part the catalog lists
doesn't actually fit:

~~~text
$ cruxible claim attest CLM-9482bb3b… --contradict \
    --note "Tried bp-2210 on a 2019 Civic at the shop: the caliper bracket doesn't match."
Recorded contradict on CLM-9482bb3b… at 2026-10-21T18:38:28Z (event sha256:…)
counts toward verdict: no (recorded, not accepted: the verdict reads accepted attestations)
~~~

The attestation is recorded and shown on the Claim (`get --detail evidence`
lists it, with whether it counts and why), and `next` brings the
contradiction back for review as `claim_contradicting_evidence_available`.
The verdict doesn't move on its own: Cruxible shows the disagreement, and a
person or an agent decides whether to revise or retire the Claim. An
attestation counts toward the verdict only when it is accepted citing a
Capture the Claim admits, or when the field's attestation consequence policy
says so.

Record disagreement this way, or record what you observed instead, rather
than creating an inverse field ("does not fit").

## Predictions

A prediction turns "we expect this" into a test: which later observation
settles a Claim, by what rule, within what window. A supplier's promised
delivery date can be predicted and later settled from when the part is
recorded as received. Settled predictions are how a team learns which
sources and which agents to trust. See
[Predictions](concepts.md#predictions) and the [CLI
reference](cli-reference.md) for `prediction propose` and `prediction
settle`.

## Pitfalls

- **Own words for outside facts.** A fact about the world should rest on what
  was observed. Require captured evidence, and for facts that must come from
  outside, admit only fetched contracts.
- **Treating a workspace file as a fetched source.** A file in the repository
  is something an agent can edit. Cite it for documented reasoning, not as
  proof about the world.
- **Ignoring the verdict.** A write that lands `uncovered` or `stale_evidence`
  is a signal, not a success.
- **Inverse fields for disagreement.** Attest, or record the observation; don't
  add "does not fit" beside "fits".
- **A citation where a Document is needed.** If the Claim depends on the file
  saying nothing else, govern the file as a Document.

## Where to go next

- [Building team knowledge](building-team-knowledge.md): what to model, and how.
- [Keeping knowledge current](keeping-knowledge-current.md): working
  through drift, expiry and contradictions as they come up.
- [Evidence](concepts.md#evidence): the concepts behind this guide.
- The [CLI reference](cli-reference.md) for `claim attest`, `files` and
  `get --detail evidence`.
