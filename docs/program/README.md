# The program instance

Playbill's own development is governed by a Playbill instance. This directory
is that instance's human surface: the pages an agent or a reader consults to
find out what is in flight, what landed, what was ruled, what hurt, and what
the rules of the work are.

## Two kinds of governed block

A governed block is either a **source block** or a **projection block**, and
never both. A source block is authored text captured as evidence: it flows
*into* state, and claims cite it. A projection block is text rendered from
accepted claims: it flows *out of* state, and is never evidence for anything.
The direction is the whole distinction — source, then state, then projection —
and a block that tried to be both would let a page attest itself into concrete.

The projection markers in these files mark the second kind only. A projection
block is a marker pair naming the claims the passage reflects; the markers are
written by `playbill block repin`, never by hand. A source block carries no
marker: it is ordinary prose that a `dev.program.prose` claim cites by span, so
what makes it governed is recorded in the ledger rather than in the file. Prose
that no claim cites is ordinary prose — legal, unpoliced, and backed by nothing.

Nothing here is generated. There is no renderer, deliberately: the same facts
have many valid presentations and the model writes the one that fits the page.
What the system guarantees is not the wording but the binding — which claims a
passage reflects, at which generation, and whether they have moved since.

## The pages

| page | what it holds | subject kind |
|---|---|---|
| `in-flight.md` | every batch not yet landed, what it is waiting on, and its open verdicts | `dev.batch` |
| `landings.md` | every landed batch, its landed commit and the state it reached | `dev.batch` |
| `decisions.md` | every ruling that constrains the work, with its standing | `dev.decision` |
| `cards.md` | the friction cards, their disposition and the batch that closed each | `dev.card` |
| `laws.md` | the standing method laws, with the incident each came from | `dev.law` |

## How to read a page

Three states are worth distinguishing:

- **projection, current** — the block's backing claims are live at the head and
  say what the block says they say.
- **projection, backing-stale** — a backing claim has been revised, superseded
  or retired since the block was declared. The workspace check reports the
  block and names the repair; the page still says what it said.
- **prose** — outside any marker. Governed as a source block if a claim cites
  it, and ordinary prose otherwise; the file cannot tell you which, and the
  claim listing can.

A reader with a workspace runs the workspace check to learn which applies. A
reader without one is reading a snapshot, and should treat the generation stamp
in each block as the age of the claim, not of the file.

## How to change a page

Do not edit a projection block to change what it asserts. Change the claim,
then re-declare the block. Editing the prose alone leaves the page and the
ledger disagreeing, which the workspace check reports against you as a dirty
block.

- A fact changed (a batch landed, a card closed): revise the claim in place.
  Identity is stable, the block's binding survives, and a repin re-stamps it.
- A shape changed, or something is no longer true rather than out of date
  (a ruling is retracted): succeed or retire, which is a closure ceremony and
  needs its dispositions declared.
- A wording improvement with no change of assertion: edit the body and repin.

A source block is changed the other way round, because the text is the
evidence: edit the prose, then supersede the `dev.program.prose` claim that
cites it. A claim whose cited span no longer exists is reported unobserved.

## What is deliberately not modelled

Counts and measurements are not entities. "Eight review rounds", "3345 tests
passed", "twenty-one consecutive first-pass rejections" are readings. They live
in the artifacts the claims cite, not as subjects of their own. Modelling a
reading as an entity makes it look governed when nothing governs it.

Reviews and landings are not entities either. A review pass is a qualifier on a
verdict claim; a landing is a state plus a commit. Both were tried as subject
kinds and neither earned it.

The prose of a card, a decision's short name and a law's short name are not
claims either. They are the labels a reader navigates by; the governed content
is the disposition, the standing, the ruling text and the law text.
