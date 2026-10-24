# Operating Cruxible as an AI agent

Cruxible is built so an agent's first reads are cheap and exact: a map, then
what needs attention, then values with their flags, and one thing in depth
only when the task needs it. Writes are governed: everything an agent changes
is checked, and the instance's approval policy decides what is accepted.

This page is the operating manual: how to connect, the loop to run, and the
rules to keep. Each step links to the guide that covers it in full. The
examples use the car parts example from
[Concepts](concepts.md#knowledge-as-claims).

## Connect

**MCP.** An MCP client launches `cruxible mcp` (`uvx cruxible mcp` from the
registry listing). Every tool runs on a daemon; the server is only its client.

~~~json
{
  "mcpServers": {
    "cruxible": {
      "command": "uvx",
      "args": ["cruxible", "mcp"],
      "env": {
        "CRUXIBLE_SERVER_SOCKET": "/home/me/.cruxible/run/daemon.sock",
        "CRUXIBLE_INSTANCE_ID": "inst_…",
        "CRUXIBLE_PRINCIPAL_ID": "agent-b"
      }
    }
  }
}
~~~

Without a socket or URL the server reuses the local daemon on
`~/.cruxible/run/daemon.sock`, starting one when none answers. Without
`CRUXIBLE_INSTANCE_ID` it uses the instance the workspace's
`.cruxible/coverage.json` binds. `CRUXIBLE_PRINCIPAL_ID` names the principal
the agent acts as; on a daemon with auth, `CRUXIBLE_SERVER_BEARER_TOKEN`
carries its credential. `cruxible principal add NAME --key-dir DIR` writes
exactly these settings to `DIR/cruxible.env`. Setup itself (starting a
daemon, `cruxible init`, adding principals) is operator work on the CLI; MCP
has no setup tools. See [MCP tools](mcp-tools.md) for every variable.

The `default` profile advertises 13 tools, the everyday loop: `orient`,
`query`, `get`, `next`, `set`, `retire`, `write`, `proposal_list`,
`proposal_review`, `proposal_approve`, `proposal_activate`, `whoami` and
`server_info` (each prefixed `cruxible_`). Set `CRUXIBLE_MCP_PROFILE=full` for
the rest: authoring, ClaimTypes, Procedures, Lines, predictions, files,
coverage, blocks, curation, kits and providers. Profiles change what is
advertised; the credential's tier still gates every call.

**CLI.** The same operations, with `--json` for structured output. After
`cruxible init` or `cruxible context use`, commands need no flags.

**Python.** `cruxible-client` is the SDK:

~~~python
from cruxible_client import Cruxible

cx = Cruxible.connect()          # remembered context, or connect(target=..., instance=...)
print(cx.world().describe())     # the verbs and the vocabulary
~~~

## A propose-only agent

A propose-only agent writes and proposes but can't approve or accept, so
everything it changes waits for a person. That limit is a credential tier
(`governed_write`), so it needs a daemon with auth. Start one with its own
state root:

~~~bash
cruxible server start --auth --state-root ~/.cruxible-team \
  --socket ~/.cruxible-team/run/daemon.sock
~~~

The daemon never prints its bootstrap secret; it writes it owner-only to
`<state-root>/daemon/bootstrap-secret`. In another shell, in a new repository,
allocate a host with the secret, claim the host's one-time admin credential,
and initialize with it:

~~~bash
mkdir team && cd team && git init
export CRUXIBLE_SERVER_SOCKET=~/.cruxible-team/run/daemon.sock
SECRET_FILE=~/.cruxible-team/daemon/bootstrap-secret
CRUXIBLE_SERVER_BEARER_TOKEN="$(cat "$SECRET_FILE")" cruxible host create
cruxible credential claim-bootstrap --secret-file "$SECRET_FILE"
CRUXIBLE_SERVER_BEARER_TOKEN=<the admin token it printed> cruxible init
~~~

`init` makes you the owner and mints your own credential into your settings,
which the CLI and the SDK remember. Add the agent in one command, which
registers its key and writes its `governed_write` credential into its own
settings file:

~~~bash
cruxible principal add agent-b --key-dir ~/agents/agent-b
~~~

Hand the agent `~/agents/agent-b/cruxible.env`: a shell or an MCP server loads
it (`set -a; . ~/agents/agent-b/cruxible.env; set +a`), and on this machine
`cruxible context use --principal agent-b` switches the CLI to it. Its writes
wait, and its approvals are refused:

~~~text
$ cruxible set catalog.part/bp-1142 title "Front brake pads" --because "Acme catalog 2026."
awaiting approval (generation 2, 4de2da8e1fc9)
  set catalog.part/bp-1142 title: Front brake pads  [CLM-…; verdict supported]
...
$ cruxible proposal approve sha256:33bafcbb… --yes
Error: cruxible proposal approve needs the graph_write tier; this credential is governed_write. …
~~~

You review and approve what it proposes under your own settings (`cruxible
context use --principal YOUR_ID`). With auth off, the same `principal add`
registers the agent and writes its settings without a credential, but every
process of your OS user is equally trusted, so the principal ID is a claim,
not a boundary. [Review and governance](review-and-governance.md) covers
tiers and approval policy.

## The loop

### 1. Orient

`orient` maps the instance in one call: each kind with its fields, value
types, evidence and counts, who you are, what needs attention, and the next
calls to make.

~~~text
$ cruxible orient
Cruxible inst_902ea44b9b334717 generation=19 at 1a04bbdcd90b accepted 2026-10-21T20:06:49.528502Z
Floor: .cruxible/floor at 1a04bbdcd90b, current
You: YOUR_ID, can author
Kinds (4):
  ...
  catalog.part  subjects=4  evidence=cruxible.coordinator-self-source-v1
    category  one  enum[brakes|filters|wipers|lighting]
    fits  many  subject:catalog.vehicle  evidence=cruxible.coordinator-self-source-v1,cruxible.foreign-source.catalog,cruxible.foreign-source.fitment
    ...
Queries:
  catalog.parts_for_vehicle(vehicle: string)  The parts that fit one vehicle.
Attention: next=0 open_proposals=0
Next:
  cruxible orient --kind catalog.part
  ...
~~~

`orient --kind K` reads one kind in full; `orient --section S` pages one
family (claim_types, queries, lines, documents, generations, graph, pins, …).

### 2. See what needs attention

`next` lists what is wrong or waiting, most urgent first, each row with the
exact repair: drifted or stale evidence, disagreements, uncovered Claims,
stale proposals, stale pages, Line gaps and inbox items. Work it top down,
judging each repair rather than running it blindly. When it's clear, `audit`
ranks the Claims most worth verifying. [Keeping knowledge
current](keeping-knowledge-current.md) goes through every family.

### 3. Read what the task needs

Cheapest first ([Asking questions](asking-questions.md)):

- **Grep the floor** to find things by name. `.cruxible/floor/current/<kind>/<id>.yaml`
  holds one Subject per file, and `.cruxible/floor/ontology/` describes every
  kind and field. Without a shell, `query --contains TEXT` searches every value.
- **`query`** answers across many Subjects as rows with flags: `--where`,
  `--select`, `--follow`, `--order-by`, or a saved question with `--name` and
  `--param`. A wrong kind, field or enum member is refused with the nearest
  valid names, never answered empty.
- **`get`** reads one thing by any reference you have seen (`kind/id`, a Claim
  ID, a definition such as `ClaimType:catalog.part.fits`, a proposal ID), with
  `--detail evidence|why|history|proof` going deeper.
- **`coverage resolve --file PATH`** says which Claims rest on a file you are
  about to edit, and which of its lines they cite.

The floor follows accepted state shortly after each change; confirm with
`get` or `query` before you act. A page or table in a document can be behind
the state it reflects: before relying on one, check `next` for a stale block
on it and read the Claims behind it instead.

In a summary (a `query` cell, a card, a write's before and after) a string
over 500 characters is a `TruncatedText` (`truncated`, `preview`, `length`,
`read_whole`), never the value. `read_whole` is the exact read of the whole
value, pinned to the generation the preview came from (`cx.read_whole(preview)`
in the SDK).

### 4. Act

- **Values**: `set`, `add`, `retire`, and `write` for several changes as one,
  each with a `because` and, where you have it, evidence ([Building team
  knowledge](building-team-knowledge.md), [Evidence and
  admission](evidence-and-admission.md)).
- **Notes**: write the reasoning into a document section in the same `write`
  as the Claims that cite it ([Documents and notes](documents-and-notes.md)).
- **Definitions**: fields, kinds, saved queries and automation go through
  authoring ([Evolving your vocabulary](evolving-your-vocabulary.md), [Lines
  and automation](lines-and-automation.md)).
- **Proposals**: review the exact candidate before anyone signs it ([Review
  and governance](review-and-governance.md)).

### 5. Check

Every write reports each change's verdict. Anything other than `supported`
is a signal, not a success: `uncovered` usually means the field's rules don't
admit your evidence for that role. Run `next` again after a batch of work.

## Writing from the SDK

The value verbs on each surface:

| | One change | Several, atomically |
|---|---|---|
| MCP | `cruxible_set`, `cruxible_retire` | `cruxible_write` (`set`, `add`, `retire` changes, plus `docs`) |
| CLI | `cruxible set`, `add`, `retire`, `rebind` | `cruxible write FILE` or `write -` |
| SDK | `cx.set(...)`, `cx.add(...)`, `cx.retire(...)`, `cx.rebind(...)` | `cx.changes(because=...)`, then `.set`, `.add`, `.retire`, `.write()` |

A batch can write the note and the Claim that cites it together:

~~~python
batch = cx.changes(because="Fitment check at the shop.", subject="catalog.part/af-208")
batch.append_section("docs/fitment.md", under=("Fitment notes",), id="check",
                     text="## af-208 on the 2019 Civic\n\nFitted at the shop on 2026-10-21: no issues.\n")
batch.add("fits", "catalog.vehicle/honda-civic-2019", evidence=batch.section("check"))
outcome = batch.write()
outcome.status                                        # 'accepted'
[(c.field, c.after, c.verdict) for c in outcome.changes]
# [('fits', 'catalog.vehicle/honda-civic-2019', 'supported')]
~~~

`replace_section(..., expect=digest)` replaces a section only if nobody
changed it since you read it. Each write accepts in the same call when the
approval policy and your tier allow it; otherwise the outcome says `awaiting
approval` and names who may approve. `dry_run` runs every check and writes
nothing; `accept="never"` stops at a proposal; a write refuses if the field
changed since the coordinate you read at.

### The typed world

`cx.world()` hands back the accepted vocabulary as objects, so names and
allowed values come from the instance, not from memory:

~~~python
w = cx.world()
w.catalog.part["bp-1142"]          # WorldSubject
w.catalog.part.category            # WorldClaimType for that field
w.catalog.part.category.filters    # a value only this field admits
w.catalog.part.category.tyres      # AttributeError: names no enum member 'tyres';
                                   # its literal schema admits: brakes, filters, lighting, wipers
w.catalog.part["zz-999"]           # AbsentSubject, with the repair to create it

part = w.catalog.part["bp-1142"]
part.category                      # tuple of ClaimViews: every live Claim of that field
part.explain()                     # governance and provenance context
w.catalog.part.where(category="brakes").select("title").run().rows
~~~

Where a name isn't a Python identifier, use `w.kind("catalog.part")` and
`w.claim_type("catalog.part.fits")`. A World is pinned to the coordinate it
was read at: after you write, read it again (`cx.world()`) before using its
references in a new change, or the SDK refuses them as out of date.
`.cruxible/world.pyi` holds the vocabulary as Python types, so an editor or a
model completes real names; floor delivery keeps it current, and `cruxible
stub` writes one.

### Definitions

`cx.changes(rationale=...)` collects definitions and Claims into one change
set that lands whole or not at all:

~~~python
draft = cx.changes(rationale="Record a fitment finding as a note.")
note = draft.subject(cx.new_subject("catalog.note", key="af-208-civic-fit"))  # usable in this set
draft.claim(subject=note, predicate=w.catalog.note.about, value=w.catalog.part["af-208"],
            role="observation", rationale="Checked at the shop.", self_source="about: af-208\n")
intent = draft.submit()            # intent.outcome.status: 'landed'
~~~

`.submit(accept="never")` keeps the proposal, and `.prepare()` checks
without submitting. On the CLI, `cruxible authoring submit FILE|-`;
`cruxible authoring example NAME` prints a template for every input kind.

### Evolving vocabulary

`ChangeSetDraft.succeed_claim_type(successor, dependents=[...])` changes a
field together with what happens to every Claim written under it, so "I need
this distinction, and here is everything it changes" lands as one generation.
The successor names its predecessor and pins its current digest. Each
dependent takes one disposition:

| Helper | What the dependent becomes |
|---|---|
| `carry(claim)` | Re-pinned to the successor, otherwise unchanged. |
| `rescind(claim)` | Retired as `was-rescinded`, keeping the statement it was accepted with. |
| `retire(claim, reason=..., effective_until=...)` | An attributed retirement (`was-wrong`, `was-rescinded`, or `superseded`). |
| `re_author(claim)` | Said again by a sibling Claim member of the same set (`revises=` that Claim), under the successor. |

A successor that changes `object_kind` can't carry anything. [Evolving your
vocabulary](evolving-your-vocabulary.md) has a worked example and what each
kind of change costs.

## Approving

Approval signs locally and sends only the public signature. `cruxible proposal
approve` and `cruxible_proposal_approve` do it in one call; the MCP tool signs
with a key from the server's `CRUXIBLE_MCP_KEY_DIR`, and passing the reviewed
`candidate_digest` makes it refuse a candidate that changed since you read it.
A signer outside the MCP process uses the full-profile pair
`cruxible_proposal_approve_prepare` (the exact statement to sign) and
`cruxible_proposal_approve_submit` (the public signature).

Review the candidate first: `proposal review` shows what it changes and why,
with the `git diff` command for the ledger. An agent with no attached
workspace reads the same branches from the ledger mirror (`mirror_url` in
`orient --json`). [Review and governance](review-and-governance.md) covers
both.

## Automation

Lines run Procedures on schedules and events; the [Lines and
automation](lines-and-automation.md) guide walks through one. From the SDK:

- Procedures are authored as a `Sequence` of typed steps
  (`cruxible_client.authoring.procedures`: StateTap, Source, Call, Transform,
  Project, Guard, EmitCapture, PostInbox, ProposeChangeSet, SettleChangeSet,
  Halt) with `.preview()` before submitting, or as Python source with
  `@procedure` ([Procedure source reference](procedure-source.md)).
- `cx.line(name).run()` runs a Line once; `.enable()` lets its Triggers fire.
- `cx.accepted_procedure(name)` reads one: `.run(input=...)` for a direct run,
  `.measure(run=...)` and `.readings(...)` for its measurements.
- Read what a Line posted with `inbox list` and `inbox get`, and record your
  judgment with `inbox link`, `dismiss` or `ack`: the inbox never changes
  accepted state.

## Operating rules

1. Treat accepted coordinates as state and proposals as provisional. A
   proposal isn't accepted state, and an approval isn't activation.
2. Read before you write: grep the floor or `query` for an existing Subject or
   Claim before creating an adjacent one.
3. Cite where a value came from. Your own words are the default evidence; a
   file passage or a Capture is checkable. Check the verdict on every write.
4. Record disagreement as evidence (`claim attest --contradict`, or the value
   you observed), never as an inverse field.
5. Put reasoning in a document section the Claim cites, not in a field.
   Measurements belong in a Procedure's readings, never in a title.
6. Make one `write` per checkpoint, with one `because`.
7. Before relying on a page, check `next` for a stale block on it.
8. Review the exact candidate before you ask anyone to sign it.
9. Never request, transmit, or place a private key in a repository or a tool
   argument.
10. Treat a repair a diagnostic names as an invitation to a governed
    operation, never as authority.
11. Handle each typed result: a write can be refused, a proposal can go stale,
    an activation can lose a race. Stop and surface the refusal rather than
    retrying blindly.

[Building team knowledge](building-team-knowledge.md) explains the modeling
rules behind several of these.

## Fail closed

Stop and surface the typed refusal when:

- the accepted head moved under a write or a proposal (`readmit` or rebase,
  then decide again);
- a candidate or compiler digest differs from the one you reviewed;
- an approval requirement is unsatisfied;
- a principal is revoked, inactive, or outside its authority;
- a source file escapes its declared root or is a symlink;
- a requested detail is not served;
- a coordinate is provisional where accepted state was requested.

Recovery is a governed principal operation, not a bypass for ordinary
approval.
