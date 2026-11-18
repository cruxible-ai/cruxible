# Review and governance

When several people and agents keep knowledge together, two questions matter
as much as the knowledge itself: how much review does a change need before it
lands, and who is allowed to do what? Cruxible answers both with policy rather
than habit. Every change is checked, lands at once only when policy allows it,
and otherwise waits for a signed approval; every act is attributed to the
principal who made it.

This guide covers how a change lands, reviewing and approving proposals, the
approval policy, principals and credentials, setting up a propose-only agent,
delegating to automation, and reviewing with plain Git. It uses the car parts
example from [Concepts](concepts.md#knowledge-as-claims).

## How a change lands

Every change, a value write, a definition, a kit, a provider install, is
checked against the vocabulary and the current state first. Then:

- if the approval policy requires no one else's approval and the caller's tier
  may activate, it **lands at once**, in the same call;
- otherwise it **stops at a proposal** and says why and what happens next.

On an instance that requires independent approval, a write waits for someone
other than its author, and names who can approve it:

~~~text
$ cruxible set catalog.part/bp-1142 title "Front brake pads" --because "Acme catalog 2026."
awaiting approval (generation 2, cd46246265f9)
  set catalog.part/bp-1142 title: Front brake pads  [CLM-ec704119…; verdict supported]
  + subject catalog.part/bp-1142
proposal: sha256:9afd6283… (awaiting_external_approval)
awaiting: independent approval; approvers: reviewer
next: cruxible proposal approve sha256:9afd6283… --signer-id reviewer
~~~

`--no-accept` stops any write at a proposal even when it could land, and
`--dry-run` runs every check without proposing anything.

## Reviewing and approving

`cruxible proposal list --status open` shows what's waiting, and `proposal
review` reads one the way a reviewer needs it: each value change as the write
that made it printed it, what each change cites (with the cited text), and why:

~~~text
$ cruxible proposal review sha256:b4f34127…
Proposal: sha256:b4f34127d76cba1e517db95009f2710aad29e0aa330e5a5b75823b374e0486c0
Candidate: sha256:96ff95d1d40f6ba316cbc1db44deb65e969636831680d70d0aa9ae991c287473
Why: Acme's 2026 catalog lists the ceramic pads as the successor.
Required tier: governed_write; activation: snapshot
Changes: 2 created / 0 revised / 0 retired
  catalog.part: 2 created / 0 revised / 0 retired
Values (2):
  set catalog.part/bp-2210 category: brake-pads  [CLM-f836c274c35fd8336c23418c7c1a972c; verdict supported]
    cites CAP-4a44b941b725 Principal:owner (own words): the Why text above
  add catalog.part/bp-2210 replaces: catalog.part/bp-1142  [CLM-6952e393f1cbf0b55c19a8f31b0ba7b0; verdict supported; role observation]
    cites CAP-a7886657755c catalogs/acme.md#Replaces bp-1142.
      | Replaces bp-1142.
Stored fields grouped by kind: --json. Every member's artifact and delta: --detail
~~~

When the candidate needs approvals, a line after the tier names them
(`Approval: independent-principal, 1 distinct signer(s)`), and a Subject the
proposal creates has its own row (`create catalog.part/bp-2210: created`). A
change shows its Claim's role when it isn't normative, up to three
citations, and a Procedure item's own reason (`because:`). Cited text is cut
at 600 characters. A citation whose Capture body was erased says `erased by
WHO at WHEN: WHY`; one whose body is missing or damaged says `unavailable`.

Definitions follow the values, one block each. A Line shows its Procedure,
authority, bindings, parameters, acquisition, budgets and inbox; a mandate its
Line, grant, validity, condition and a table of the ClaimTypes it covers with
the basis for each kind of change; a Runbook its steps; a Trigger its schedule
and target; a new ClaimType its value type, cardinality, kinds, roles and the
evidence it admits; a Document its title and links (`guides ClaimType:...`); a
note its section and the Subjects it is about. Other kinds print one line, and
a revision lists the fields it changes:

~~~text
Definitions (1):
  Runbook catalog.weekly: created
    Sweep the evidence once a week.
    1  action:evidence.sweep
~~~

A page shows up to 10 value changes and 20 definitions. A longer proposal
ends with `More values: --changes-from N` or `More definitions:
--definitions-from N`, plus counts by kind and field for the values not shown.
`--json` adds the stored fields grouped by kind, and `--detail` is the full
review of every member's artifact and delta. When a proposed Claim cites a
Capture the reader may not read, the summary is withheld: it keeps each
change's op, Subject and field and each definition's kind, name and action,
and leaves out values, verdicts, cited text, reasons and definition fields.

An **approval** is a signature over that exact candidate, made with the
approver's private key on their own machine; only the signature reaches the
daemon. `proposal approve` prints the same review, then the approvals the
candidate requires (a count per role), the key it will sign with, and asks
before signing. `--yes` signs without asking and prints one counts line
instead of the review; with no one at the terminal the command refuses and
says to review, then rerun with `--yes`. When an approval is the one the
proposal still needed, and the approver may activate, the same command
accepts it:

~~~text
$ cruxible context use --principal reviewer
Active principal: reviewer (settings ~/keys/grace/cruxible.env)
$ cruxible proposal approve sha256:9afd6283…
...
Required approvals: 1 independent-principal
Signing key for reviewer: ~/keys/grace/reviewer.ed25519 (from remembered settings ~/keys/grace/cruxible.env)
Sign this exact candidate? [y/N]: y
Approved sha256:fc586140… (sha256:9afd6283…) as reviewer
Activated at 9b659ad32d3f…
~~~

A reviewer created at `cruxible init --reviewer-key-dir DIR` has its settings
in `DIR`, so `context use` switches to it. One command can also name the
reviewer without switching: `CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal
approve ID` acts as the reviewer, and `--signer-id reviewer` signs as it; both
find the reviewer's key the same way. The key that signs as a principal is
looked up in one order on the CLI, the SDK and MCP: a key given explicitly
(`--key PATH`, or `CRUXIBLE_PRINCIPAL_KEY`), then the settings remembered for
that principal on this instance, then the folder's identity record if it names
that principal, then the default key directory `cruxible init` uses; otherwise
the command refuses and names the repair. It never
falls back to another principal's key. On a machine that remembers no settings
for it, a reviewer passes `--signer-id reviewer --key PATH`.

`--hold` signs without accepting, for when someone else should decide when
the change lands; they run `proposal activate` later:

~~~text
$ cruxible proposal approve sha256:93339c6f… --hold
Approved sha256:64effc63… (sha256:93339c6f…) as reviewer
Held: not activated. Next: cruxible proposal activate sha256:93339c6f…
~~~

If other changes were accepted after a proposal was checked, approving or
activating proposes it again against the current state, as a fresh proposal
that replaces the old one; approvals given to the old one don't carry over.
The old one closes as `superseded`: `get` and `proposal list` show
`superseded_by` (the new proposal) and who closed it (`closed_by`, the
principal who approved, activated or readmitted, often not its author). The
receipts name the old one (`readmits`), and the CLI says `Landed at the head
as NEW: the same change set (proposed as OLD)`.

A new proposal is a new candidate with a new digest, even when every member is
byte-identical. What `proposal approve` does with it depends on whether you
named the candidate you reviewed (the `Candidate:` line of `proposal review`),
and is the same on the CLI (`--candidate-digest`), MCP (`candidate_digest`)
and the SDK (`reviewed=`, always bound):

| You passed | The proposal was proposed again at the head | Result |
|---|---|---|
| the reviewed candidate digest | yes | refuses `cruxible.approval.reviewed_candidate_changed`, naming the new proposal and `members identical: true` or `false`; nothing is signed; review the new proposal and approve it |
| no digest | yes, members identical | signs the new candidate in the same call |
| no digest | yes, members differ | signs nothing; the result names the new proposal to review |

Here the owner has two proposals open and the reviewer approves a third, so
the head moves under both. With the reviewed digest, the approval refuses and
names the new proposal; without one, it signs the new proposal, whose members
are identical:

~~~text
$ cruxible proposal approve sha256:a93b9805… --candidate-digest sha256:a8617204… --yes
...
Error: cruxible.approval.reviewed_candidate_changed: proposal sha256:a93b9805… was proposed again at the head as sha256:ec9b7ac6… (candidate sha256:abc57541…), not the reviewed sha256:a8617204…; members identical: true; nothing was signed. Review sha256:ec9b7ac6… and approve it
Repair: cruxible proposal review sha256:ec9b7ac6…

$ cruxible proposal approve sha256:7c8dca19… --yes
...
Landed at the head as sha256:4e8b6756…: the same change set (proposed as sha256:7c8dca19…); signing it
Approved sha256:1f147c90… (sha256:4e8b6756…) as reviewer
Activated at 67576f1c7d362b2aa8a2cc5041b6b17803ca04d8

$ cruxible proposal list
STATUS  TERMINAL_REASON  PROPOSAL_ID  TARGET_REF  COORDINATE_TIME
...
open  -  sha256:ec9b7ac6…  refs/proposals/owner/readmit-f470fb67…  2026-11-17T22:57:42.978053Z
settled  superseded  sha256:a93b9805…  refs/proposals/owner/intent-13cd8525…  2026-11-17T22:57:44.604446Z
  superseded_by: sha256:ec9b7ac6…
settled  superseded  sha256:7c8dca19…  refs/proposals/owner/intent-ee6eee5c…  2026-11-17T22:57:46.054829Z
  superseded_by: sha256:4e8b6756…
~~~

Every acceptance moves the head. With several proposals open and approvals
bound to a digest, review each one after the one before it has landed.

Activating a proposal whose approvals
aren't in refuses first (`cruxible.approval.requirement_unsatisfied`, naming
who may sign) and changes nothing. If what it changes has itself moved in the
meantime, it's refused, and `proposal readmit` (by its author only) rebases it
as a new proposal to review. Acting again on a proposal that was already
accepted refuses `cruxible.proposal.already_accepted`. A stale field proposal from
`claim-type propose` is readmitted rather than recompiled, and a stale
`claim-type migrate` is run again from its preflight.

A reviewer who decides a change shouldn't land rejects it, saying why
(`proposal reject ID --reason "…"`); an author drops their own proposal with
`proposal withdraw ID --reason "…"`.

### A second agent reviews

Under the default policy an author's write lands at once. For a second agent
to look first, the author writes with `--no-accept`. The proposal needs no
approval, so it waits as `ready_to_activate`, and `next` lists it:

~~~text
$ cruxible set catalog.part/bp-2210 title "Ceramic front brake pads" --because "Acme catalog 2026." --no-accept
awaiting approval (generation 3, bea76b47acab)
  set catalog.part/bp-2210 title: Ceramic front brake pads  [CLM-54cdb905…; verdict supported]
  + subject catalog.part/bp-2210
proposal: sha256:86bd8301… (ready_to_activate)
awaiting: kept (accept=never)
next: cruxible proposal approve sha256:86bd8301… --signer-id '<SIGNER_ID>'
$ cruxible next
...
repair  proposal_awaiting_activation  sha256:86bd8301…  (catalog.part/bp-2210 catalog.part.name; catalog.part/bp-2210 catalog.part.title)  next=cruxible.proposal.activate
  repair: cruxible proposal activate sha256:86bd8301…
~~~

The reviewing agent works in its own session, as its own principal (a
reviewer from `init --reviewer-key-dir`, or one `principal add` registers). It
runs `proposal review ID`, then either `proposal approve ID`, which signs the
exact candidate and, since nothing else is required, activates it, or
`proposal activate ID`, which activates without a signature. It turns the
change down with `proposal reject`. A reviewer registered after the author
wrote is absent at the proposal's base; its `proposal approve` lands the
proposal at the head first (`Landed at the head as NEW ... before reviewer was
registered`) and signs that, so nobody needs to readmit it. A kit change or a
generated ClaimType migration is the exception: planned against the instance
at its base, it refuses `cruxible.proposal.readmit_requires_resubmission` for
such a reviewer, naming its author and the command that remakes it at the
head (the kit committed again, or the migration run again). Every one of these
acts needs a named principal: a session that names none refuses
`cruxible.identity.principal_unconfigured`.

~~~text
$ cruxible context use --principal reviewer
$ cruxible proposal approve sha256:86bd8301… --yes
Signing sha256:8d23151f… (sha256:86bd8301…): 3 changes (3 create)
...
Approved sha256:8d23151f… (sha256:86bd8301…) as reviewer
Activated at e6c4614c7c6e28366962c8f125501260b13592fd
~~~

Under `self_approval_allowed` this split is an instruction to agents, which
the MCP server's instructions and the bundled skills give: never approve your
own proposal, even with auth off. Under `independent_approval_required` core
enforces it.

## The approval policy

Each instance has one approval policy in force. `cruxible init` sets the first
one, and it can be changed later (below):

- **`self_approval_allowed`** (a plain `cruxible init`): an author may approve
  their own changes, so writes land at once for anyone whose tier may activate.
- **`independent_approval_required`** (`init --require-independent-approval`):
  every governed change needs an approval from someone other than its author.
  An author approving their own change is refused:

  ~~~text
  $ cruxible proposal approve sha256:9afd6283…
  Error: cruxible.approval.creator_forbidden: independent_approval_required mode needs an active ordinary approver other than the candidate creator, whose approval activates it
  ~~~

Independence is what lets a team rely on each other's findings: under it, an
agent that found something can't also be the one who confirms it.

### Setting up cosign

The recommended setup is cosign: an owner who writes and a reviewer who
approves. A new instance gets both at creation:

~~~text
$ cruxible init --principal-id owner --reviewer-key-dir ~/keys/reviewer --require-independent-approval
...
Approval policy: independent_approval_required
Base kit: base 1.0.0 awaits approval, proposal sha256:4acb1302… (cruxible proposal approve sha256:4acb1302… --signer-id reviewer, as a second principal)
Reviewer: reviewer, private key ~/keys/reviewer/reviewer.ed25519 (cruxible context use --principal reviewer)
~~~

The reviewer acts through `CRUXIBLE_PRINCIPAL_ID=reviewer` for one command, or
`cruxible context use --principal reviewer` for the session. Its first act is
approving the base kit, which waits like any other change.

On an instance that already exists, cosign is two steps, in this order:

~~~bash
cruxible principal add reviewer --key-dir ~/keys/reviewer --signer-key OWNER_KEY --mode graph_write
cruxible authoring example approval-policy > policy.json
cruxible authoring submit policy.json
~~~

`principal add` registers the reviewer (`graph_write` is the tier that may
submit approvals when the daemon runs with auth), and the second step submits
the approval policy as an ordinary authoring input; the example's mode is
`independent_approval_required`. Add the reviewer first: once the policy
lands, the owner can no longer land a change alone. Running `cruxible init`
again on an initialized instance answers that it is already initialized and
changes nothing.

A change of policy is itself governed by the policy in force. Under
`self_approval_allowed` the owner's submit lands at once; under
`independent_approval_required` a submit that returns to self-approval stops
at a proposal and waits for the reviewer.

One owner approving their own changes stays an option: a plain `cruxible init
--principal-id owner` sets `self_approval_allowed`, and every write lands at
once.

Cosign between agents on one machine is a convention, not a security boundary:
any process of the OS user can read both keys. The next section says what that
means.

### What approval protects, and what it doesn't

Approval is a record of who agreed to a change, and independence keeps whoever
found something from also confirming it. That's attribution and a separation
of roles, not a security boundary: an agent that can read every principal's
private key on the machine can sign as any of them, and on a daemon without
`--auth` any process can claim to be any principal. Treat approvals as an
honest team's record, not a lock.

Where a fact must not rest on anyone's say-so, the control is evidence. A field
can require captured evidence and admit only contracts whose Captures a run
fetched from the outside world, which the daemon's journal shows were fresh
reads rather than bytes someone wrote ([Evidence and
admission](evidence-and-admission.md#requiring-captured-evidence)). To keep
agents from acting as each other, keep their keys where other agents can't
read them, such as under another OS user or on another machine, and give each
a credential scoped to its tier.

## Principals and credentials

Two different things answer "who is this":

- A **principal** is a governed identity: a public key recorded in the
  ledger. Principals sign approvals and attestations, and every change is
  attributed to one. Their private keys stay on their own machines.
- A **credential** is a bearer token for the daemon. It decides whether a
  caller may connect and caps what it may do with a **tier**:

| Tier | Can |
|---|---|
| `read_only` | Read, query, and see the work queues |
| `governed_write` | Also write values, author definitions and propose |
| `graph_write` | Also approve and activate |
| `admin` | Also operator acts: credentials, hosts, principals, the ledger mirror, compiler upgrades, provider installs |

A daemon on a Unix socket runs without authentication by default: every
process of your user account is trusted equally, there are no tokens, and the
principal a process names is a claim, not proof. Start the daemon with
`--auth` (required on TCP) when you need credentials to mean something, such as
an agent that may propose but not approve.

## A propose-only agent

Give an agent a `governed_write` credential, and everything it changes waits
for a person. That needs a daemon started with `--auth`; [For AI
agents](for-ai-agents.md#a-propose-only-agent) walks through starting one,
claiming the first admin credential and initializing. Then one command
registers the agent's key and writes its `governed_write` credential into its
own settings file, which you hand to the agent's process or MCP server:

~~~bash
cruxible principal add agent-b --key-dir ~/agents/agent-b
~~~

The agent's writes wait, and its approvals are refused:

~~~text
$ cruxible whoami
Actor: agent-b
...
Credential permission mode: governed_write

$ cruxible set catalog.part/bp-1142 title "Front brake pads" --because "Acme catalog 2026."
awaiting approval (generation 3, 5bbdedbab3a1)
  set catalog.part/bp-1142 title: Front brake pads  [CLM-b6a3c08f…; verdict supported]
  + subject catalog.part/bp-1142
proposal: sha256:791e4f1f… (ready_to_activate)
awaiting: activation; your tier cannot activate
next: someone who can runs cruxible proposal activate sha256:791e4f1f…

$ cruxible proposal approve sha256:791e4f1f… --yes
Error: cruxible proposal approve needs the graph_write tier; this credential is governed_write. …
~~~

You review and approve what it proposes under your own settings (`cruxible
context use --principal YOUR_ID` switches back), and approving activates it.

## Delegating to automation

Lines can act on their own only as far as you allow. A Line's ceiling is
`observe`, `propose` or `settle`, and anything beyond observing needs a
**mandate** for its Procedure:

- a **propose** mandate lets it propose changes, which then wait for review
  like anyone's;
- a **settle** mandate lets it land changes without approval, within what
  the mandate names: which fields, which kinds of change (create, revise,
  retire), optionally which Subjects and which kinds of new Subject, and a
  condition, a saved query that must say each target qualifies.
  A change to a field the mandate doesn't name goes to review as a proposal; a
  condition that doesn't hold proposes or refuses, as the mandate declares;
  anything else outside it is refused.

The mandate, not the tier of whoever starts the run, is what authorizes
settling. A mandate is itself a definition, so granting one is a governed
change, reviewed like any other; mandates expire, and can be suspended or
retired.

A good rule, though core doesn't enforce it, is to delegate facts copied
verbatim from a source and keep judgments for review. A Line that re-reads a
supplier's price list can earn a settle mandate for prices; whether a part
fits a car stays with a person. The security kit's example automation follows
the same rule: Lines that copy a vulnerability's published listings can
graduate to settling, while whether a vulnerability affects you never enters a
mandate, so every such finding is reviewed. [Lines and
automation](lines-and-automation.md#earning-settle) walks through a
graduation.

## Reviewing with Git

The ledger is a Git repository, so review can use the tools you already have.
Beside the reviewer's view above, `proposal review --json` carries the stored
fields grouped by Subject kind and exact template: shared values appear once,
and rows show the before and after fields that vary. Page groups with
`--summary-after`, or a group's rows with `--summary-group` and `--rows-after`.
The originating Line and its exact digest appear when a Line produced the proposal.

`proposal review --detail` prints the exact commands to diff a proposal against
accepted state in your workspace, and where the daemon's evaluation and
approval records are kept:

~~~bash
git diff cruxible-ledger/accepted...cruxible-ledger/proposals/<proposal-digest>
git notes --ref=refs/notes/cruxible-eval show cruxible-ledger/proposals/<proposal-digest>
git notes --ref=refs/notes/cruxible-approval show cruxible-ledger/proposals/<proposal-digest>
~~~

The evaluation note carries the checks and every diagnostic behind a refusal;
the approval note carries each signer's approval.

To let reviewers and auditors elsewhere fetch the ledger, mirror it to a Git
remote (an empty bare repository). From then on the daemon publishes in the
background whenever a proposal is submitted, approved, activated or
withdrawn: the accepted history as `main`, and a branch per open proposal.
Before you point a reviewer at it, `ledger publish` waits until the remote
has caught up:

~~~bash
cruxible ledger set-mirror git@example.com:team/garage-ledger.git --yes
cruxible ledger publish
~~~

## Compiler upgrades

The rules that interpret the ledger are pinned too, so adopting a new compiler
revision is itself a governed change: `cruxible compiler upgrade` previews it
and asks before committing. It's one-way, so back up first. See
[Upgrading](upgrading.md).

## Pitfalls

- **Treating an auth-off principal as proof.** Without `--auth`, any process
  of your user account can act as any principal; use auth for agents you need
  to bound.
- **Self-approval on a team.** If agents' findings matter, require independent
  approval.
- **Delegating judgments.** Grant settle mandates for facts copied verbatim
  from a source, not for judgments about them.

## Where to go next

- [For AI agents](for-ai-agents.md): connecting agents and their operating
  loop.
- [Keeping knowledge current](keeping-knowledge-current.md): proposals in the
  maintenance queue.
- The [CLI reference](cli-reference.md) for `proposal`, `principal`,
  `credential` and `ledger`.
