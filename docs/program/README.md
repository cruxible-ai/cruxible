# The program instance

Playbill's own development is governed by a Playbill instance. This directory
is that instance's human surface: the pages an agent or a reader consults to
find out what is in flight, what landed, what was ruled, what hurt, and what
the rules of the work are.

## What this is

Every table row on the pages in this directory is backed by an accepted Claim
in the program instance. The pages are not a summary of the ledger written
alongside it — they are the ledger's declared reflection. When a claim moves,
the block that reflects it is flagged stale and an agent supersedes the prose
in a reviewable diff.

Nothing here is generated. There is no renderer, deliberately: the same facts
have many valid presentations and the model writes the one that fits the page.
What the system guarantees is not the wording but the binding — which claims a
passage reflects, at which generation, and whether they have moved since.

## The pages

| page | what it holds | subject kind |
|---|---|---|
| `in-flight.md` | every batch not yet landed, what it is waiting on, and its open verdicts | `dev.batch` |
| `landings.md` | every landed batch, its landed commit and the state it reached | `dev.batch` |
| `decisions.md` | every ruling that constrains the work, with its status | `dev.decision` |
| `cards.md` | the friction cards, their status and the batch that closed each | `dev.card` |
| `laws.md` | the standing method laws, with the incident each came from | `dev.law` |

## How to read a page

Blocks are marked in the file. A marker pair names the block and the claim it
reflects; the markers are written by the publication step, never by hand, and
a publication whose region is not marker-wrapped is refused. Prose outside the
markers is ordinary prose: legal, unpoliced, and not backed by anything.

Three states are worth distinguishing:

- **governed and current** — the block's backing claims are live at the head.
- **governed and stale** — a backing claim has been revised, superseded or
  retired since the block was written. The workspace check reports the block
  and names the repair; the page still says what it said.
- **prose** — outside any marker. Believe it at the ordinary rate.

A reader with a workspace runs the workspace check to learn which of the three
applies. A reader without one is reading a snapshot, and should treat the
generation stamp in each block as the age of the claim, not of the file.

## How to change a page

Do not edit a governed block to change what it asserts. Change the claim, then
sync the block. Editing the prose alone leaves the page and the ledger
disagreeing, which the amend-in-place check will report against you.

- A fact changed (a batch landed, a card closed): revise the claim in place.
  Identity is stable, the block's binding survives, and the sync updates the
  page.
- A shape changed, or something is no longer true rather than out of date
  (a ruling is retracted): succeed or retire, which is a closure ceremony and
  needs its dispositions declared.
- A wording improvement with no change of assertion: amend the block body and
  sync. The check will still notice, because a body-only amend is a revision.

## What is deliberately not modelled

Counts and measurements are not entities. "Eight review rounds", "3345 tests
passed", "twenty-one consecutive first-pass rejections" are readings. They live
in the artifacts the claims cite, not as subjects of their own. Modelling a
reading as an entity makes it look governed when nothing governs it.

Reviews and landings are not entities either. A review pass is a qualifier on a
verdict claim; a landing is a state plus a commit. Both were tried as subject
kinds and neither earned it.
