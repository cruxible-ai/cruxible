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
next: cruxible proposal approve sha256:9afd6283… --signer-id reviewer --key <reviewer.ed25519>
~~~

`--no-accept` stops any write at a proposal even when it could land, and
`--dry-run` runs every check without proposing anything.

## Reviewing and approving

`cruxible proposal list --status open` shows what's waiting, and `proposal
review` shows what one would change and why:

~~~text
$ cruxible proposal review sha256:9afd6283…
...
Why:
  Acme catalog 2026.
Changes (3):
  set catalog.part/bp-1142 name: bp-1142  [CLM-…]
  set catalog.part/bp-1142 title: Front brake pads  [CLM-…]
  create subject subjects/catalog.part/bp-1142.json
~~~

An **approval** is a signature over that exact candidate, made with the
approver's private key on their own machine; only the signature reaches the
daemon. `proposal approve` shows the same summary and asks before signing.
When an approval is the one the proposal still needed, and the approver may
activate, the same command accepts it:

~~~text
$ cruxible context use --principal reviewer
Active principal: reviewer (settings ~/keys/grace/cruxible.env)
$ cruxible proposal approve sha256:9afd6283…
Signing key for reviewer: ~/keys/grace/reviewer.ed25519 (from remembered settings ~/keys/grace/cruxible.env)
...
Approved sha256:fc586140… as reviewer
Activated at 9b659ad32d3f…
~~~

A reviewer created at `cruxible init --reviewer-key-dir DIR` has its settings
in `DIR`, so `context use` switches to it; on another machine, a reviewer
can instead pass `--signer-id reviewer --key PATH`.

`--hold` signs without accepting, for when someone else should decide when
the change lands; they run `proposal activate` later:

~~~text
$ cruxible proposal approve sha256:93339c6f… --hold
Approved sha256:64effc63… as reviewer
Held: not activated. Next: cruxible proposal activate sha256:93339c6f…
~~~

If other changes were accepted after a proposal was checked, approving or
activating recompiles it against the current state, as a fresh proposal that
replaces the old one; approvals given to the old one don't carry over. If what
it changes has itself moved in the meantime, it's refused, and `proposal
readmit` rebases it as a new proposal to review. A stale field proposal from
`claim-type propose` is readmitted rather than recompiled, and a stale
`claim-type migrate` is run again from its preflight.

A reviewer who decides a change shouldn't land rejects it, saying why
(`proposal reject ID --reason "…"`); an author drops their own proposal with
`proposal withdraw ID --reason "…"`.

## The approval policy

Each instance has one approval policy, chosen at `cruxible init`:

- **`self_approval_allowed`** (the default): an author may approve their own
  changes, so writes land at once for anyone whose tier may activate.
- **`independent_approval_required`** (`init --require-independent-approval`):
  every governed change needs an approval from someone other than its author.
  An author approving their own change is refused:

  ~~~text
  $ cruxible proposal approve sha256:9afd6283…
  Error: cruxible.approval.creator_forbidden: independent_approval_required mode needs an active ordinary approver other than the candidate creator, whose approval activates it
  ~~~

Independence is what lets a team rely on each other's findings: under it, an
agent that found something can't also be the one who confirms it.

Approval is attribution and role separation, not a security boundary. A
signature records which principal approved a change, and independence keeps
one principal from both finding and confirming it; neither stops an agent that
can read every principal's private key on the machine from signing as each of
them. Where a fact must not rest on anyone's say-so, the control is the field's
evidence rule: admit only Captures a run fetched from the outside world, which
the daemon's journal proves were fresh reads, not bytes someone wrote (see
[Evidence and admission](evidence-and-admission.md#requiring-captured-evidence)).

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

$ cruxible proposal approve sha256:791e4f1f…
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
`proposal review` prints the exact commands to diff a proposal against
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
