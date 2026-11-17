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
exactly these settings to `DIR/cruxible.env`. Setup (starting a daemon,
`cruxible init`, adding principals) is operator work on the CLI; on MCP,
`cruxible_init` initializes the workspace folder as `cruxible init` does, and
nothing else sets up. It creates the owner only, under `self_approval_allowed`,
and takes one argument (`principal_id`): cosign setup on an existing instance (a reviewer
principal and no self-approval) is two CLI steps, in this order: `cruxible
principal add reviewer --key-dir DIR --signer-key OWNER_KEY --mode graph_write`
(the tier that may submit approvals when the daemon runs with auth), then submit the
approval policy (`cruxible authoring example approval-policy > policy.json`,
`cruxible authoring submit policy.json`; its mode is
`independent_approval_required`). Add the reviewer first: once the policy
lands, the owner can no longer land a change alone. A new instance gets both
at creation with `cruxible init --reviewer-key-dir DIR
--require-independent-approval`; on an initialized instance that command
answers "already initialized" and adds nothing. See [MCP tools](mcp-tools.md) for every variable.

Some operator acts have no MCP tool, so an agent asks for them or runs them
as CLI steps: making a principal's key (`cruxible principal add NAME --key-dir
DIR`, and `cruxible init` for the owner's and the reviewer's), registering a
file in the file catalog (`cruxible files add PATH`), and stopping the daemon
(`cruxible server stop`).

First setup on one machine, in order:

1. In the project folder, run `cruxible init --principal-id owner
   --reviewer-key-dir DIR --require-independent-approval`. This is the
   recommended setup, cosign: an owner who writes and a reviewer who approves.
   With no daemon running, the CLI starts one on
   `~/.cruxible/run/daemon.sock` and says so (`cruxible server stop` stops
   it). The folder may be a Git repository or not; init writes
   `.cruxible/coverage.json` there, which gives CLI and MCP the daemon and
   instance. A plain `cruxible init --principal-id owner` is the one-owner
   option: no reviewer, and every write lands at once.
2. Approve the base kit as the reviewer; under cosign it waits like any other
   change. Init prints the command (`cruxible proposal approve ID --signer-id
   reviewer`).
3. A refusal that says a command "needs server mode" means no daemon was found
   and none could be started (`--no-autostart`, for example): start one with
   `cruxible server start --socket ~/.cruxible/run/daemon.sock`, or pass
   `--server-socket PATH`.
4. In a Git repository the daemon also mirrors the ledger into it (the
   `cruxible-ledger` remote that `proposal review --detail` diffs against). A
   plain folder has no mirror; after `git init` there, `cruxible workspace
   attach` turns it on.
5. An agent started in the project acts as its owner with no configuration:
   init's `.cruxible/identity.json` names the owner (see
   [MCP tools](mcp-tools.md#instances-and-the-adapter-environment)). A
   reviewing session is its own MCP server or shell with
   `CRUXIBLE_PRINCIPAL_ID=reviewer` in its environment (`cruxible context use
   --principal reviewer` on the CLI). The key that signs as a principal is
   found in one order on CLI, SDK and MCP: an explicit key
   (`CRUXIBLE_PRINCIPAL_KEY`, the CLI's `--key`), then the settings remembered
   for that principal on this instance, then the folder identity if it names
   that principal, then the default key directory `cruxible init` uses;
   otherwise the call refuses and names the repair, and never signs with
   another principal's key. Add more principals with `cruxible
   principal add NAME --key-dir DIR`.

Cosign between agents on one machine is a convention, not a security boundary:
any process of the OS user can read both keys ([Review and
governance](review-and-governance.md#what-approval-protects-and-what-it-doesnt)).

The agent plugin's README (`plugins/README.md`) walks the same path through to
installing a kit.

The `default` profile advertises 13 tools, the everyday loop: `orient`,
`query`, `get`, `next`, `set`, `retire`, `write`, `proposal_list`,
`proposal_review`, `proposal_approve`, `proposal_activate`, `whoami` and
`server_info` (each prefixed `cruxible_`). Set `CRUXIBLE_MCP_PROFILE=full` for
the rest: authoring, ClaimTypes, Procedures, Lines, predictions, files,
coverage, blocks, kits and providers. Profiles change what is
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

`orient` maps the instance in one call, as a small index: each kind with its
Subject count and field names, who you are, what needs attention, and the next
calls to make.

~~~text
$ cruxible orient
Cruxible inst_12acc3474754447c generation=23 at b05914f1229d proposed 2026-11-17T23:05:05.876506Z
Floor: .cruxible/floor at b05914f1229d, current
Vocabulary file: .cruxible/vocabulary.yaml at 7ad067b7d160, current
You: owner, can author
Kinds (3):
  catalog.decision  subjects=1  fields=decided_on,governs,slug,status,summary
  catalog.part  subjects=3  fields=category,fits,name,price,replaces (+1)
  catalog.vehicle  subjects=1  fields=make,slug
Artifacts: claim_types=15 documents=4 operations=2 captures=16 capture_contracts=5 capabilities=8 claims.accepted=16
Attention: next=0 open_proposals=0 (workspace rows not counted: next)
Next:
  cruxible orient --kind catalog.part
  cruxible query catalog.part --select category,fits,name --limit 10
  cruxible orient --section operations
~~~

The `next=` count covers accepted state only, and the line says so: rows that
come from the workspace (a drifted citation, a modified Document, a stale
block, the vocabulary file) are not counted there. Run `next` for the whole
queue.

`orient --kind K` reads one kind in full, each field with its value type,
cardinality, evidence and counts:

~~~text
$ cruxible orient --kind catalog.part
...
Kind catalog.part  subjects=3  evidence=cruxible.coordinator-self-source-v1
  category  one  enum[brakes|filters|wipers|lighting]  claims=1  subjects=1
  fits  many  subject<catalog.vehicle>  claims=2  subjects=2
  ...
  price  one  int{0..}  claims=0  subjects=0  evidence=web.search-results
  ...
~~~

`orient --section S` pages one family (claim_types, queries, lines, documents,
generations, graph, pins, …).

### 2. See what needs attention

`next` lists what is wrong or waiting, most urgent first, each row with the
exact repair: drifted or stale evidence, disagreements, uncovered Claims,
proposals waiting for approval or activation, stale proposals, stale pages,
Line gaps, Trigger
instants missed while no daemon ran (`trigger_instants_missed`) and inbox
items. Work it top down,
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
- **Definitions**: kinds and fields go through the vocabulary file
  (`.cruxible/vocabulary.yaml`: `vocabulary export`, edit, `plan`, `apply`);
  saved queries, CaptureKinds and automation go through authoring ([Evolving
  your vocabulary](evolving-your-vocabulary.md), [Lines and
  automation](lines-and-automation.md)).
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

For MCP-only clients, the same write is one `cruxible_write` call:

~~~json
{
  "because": "Fitment check at the shop.",
  "subject": "catalog.part/af-208",
  "docs": [
    {
      "op": "append",
      "id": "check",
      "file": "docs/fitment.md",
      "under": ["Fitment notes"],
      "text": "## af-208 on the 2019 Civic\n\nFitted at the shop on 2026-10-21: no issues.\n"
    }
  ],
  "changes": [
    {
      "op": "add",
      "field": "fits",
      "value": "catalog.vehicle/honda-civic-2019",
      "evidence": {"kind": "section", "ref": "check"}
    }
  ]
}
~~~

A Subject the write creates is part of the same batch: `batch.create(kind,
key=...)` returns a handle that names it as `subject=` or as a value, and a
`cx.new_subject(...)` draft works in the same places (`create` changes over
MCP and in a CLI write file). A key is the value of a kind's one key, or
`{field: value}` for each key field.

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
w.catalog.part["zz-999"]           # AbsentSubject: raised, with the repair to create it

part = w.catalog.part["bp-1142"]
part.category                      # tuple of ClaimViews: every live Claim of that field
part.explain()                     # governance and provenance context
w.catalog.part.where(category="brakes").select("title").run().rows
~~~

Where a name isn't a Python identifier, use `w.kind("catalog.part")` and
`w.claim_type("catalog.part.fits")`. A World's writes are checked from the
coordinate it was read at, advanced past its own accepted writes, so its
references stay valid after them; only a field someone else moved since
refuses.

A World writes in two ways. On an accepted Subject, `set`, `add` and `retire`
write at once, and need `because=`:

~~~python
part.set(title="Front brake pads, semi-metallic", because="Acme's corrected box label.")
# accepted (generation 16, 46713fb47b64)
#   set catalog.part/bp-1142 title: ... -> Front brake pads, semi-metallic  [CLM-9b63920c…; verdict supported]
~~~

To send several Subjects' changes as one change set, build them instead:
`w.<kind>.define(...)` drafts a new Subject, and `subject.edit().set(...)`
collects changes to an accepted one; nothing is sent until `w.write(...)`:

~~~python
rotor = w.catalog.part.define("rt-500", title="Front brake rotor", category="brakes")
w.write(rotor, part.edit().add(replaces=rotor), because="New rotor line from Acme's 2026 list.")
~~~

Passing `part.set(...)` itself to `w.write` fails (it has already tried to
write, and lacks `because`); pass `part.edit().set(...)`.
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

The ordinary way to change kinds and fields is the vocabulary file:
`cruxible vocabulary export` writes `.cruxible/vocabulary.yaml` from accepted
state, you edit it, `plan` shows what would change (and the Claims an enum
mapping moves), and `apply` submits it as one change set. The SDK spelling is
`cx.vocabulary.export()`, `.plan()` and `.apply()`; MCP has
`cruxible_vocabulary_export`, `_plan` and `_apply` in the `full` profile.
`orient` and `next` say when the file is behind accepted state
(`vocabulary_file_behind`); `vocabulary export` repairs it. [Evolving your
vocabulary](evolving-your-vocabulary.md#the-vocabulary-file) has the format.

Citations need no rule: a field admits your own words and passages of
catalogued workspace files and sections unless it requires captured evidence.
A field backed by a particular kind of source lists its shaped
CaptureContracts in `evidence: [CONTRACT, ...]` (vocabulary file, `claim-type
propose`, or the SDK's `field(..., evidence=[...])`); `captured` then means a
Capture under one of them
([Evidence and admission](evidence-and-admission.md#writing-a-fields-rules)).

For a change that needs a judgment per Claim, the SDK states it directly.
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
approve` and `cruxible_proposal_approve` do it in one call. The MCP tool signs
as the principal its server acts as: the owner by default, the reviewer when
the server's environment sets `CRUXIBLE_PRINCIPAL_ID=reviewer`. The key is
found by the lookup in [Connect](#connect) (`CRUXIBLE_PRINCIPAL_KEY` names one
explicitly, and `CRUXIBLE_MCP_KEY_DIR` a directory of keys); no tool argument
takes a key.

Pass the candidate digest you reviewed (`candidate_digest`, the CLI's
`--candidate-digest`, the SDK's `reviewed=`). If accepted state moved since,
the daemon has proposed the change again at the head as a new proposal, and
the old one reads `superseded` with `superseded_by`. With the digest, approve
then refuses `cruxible.approval.reviewed_candidate_changed`, names the new
proposal, says whether its members are identical, and signs nothing: review
the new proposal and approve it. Without a digest, approve signs the new
proposal in the same call when its members are identical.
A signer outside the MCP process uses the full-profile pair
`cruxible_proposal_approve_prepare` (the exact statement to sign) and
`cruxible_proposal_approve_submit` (the public signature).

Review the candidate first: `proposal review` (MCP `cruxible_proposal_review`,
SDK `str()` of the summary) prints each value change with what it cites and
the cited text, then one block per definition; page long proposals with
`changes_from` and `definitions_from`. `--detail` gives the `git diff` command
for the ledger. An agent with no attached workspace reads the same branches
from the ledger mirror (`mirror_url` in `orient --json`).

The reviewer is a different agent from the author, in its own session and as
its own principal, even with auth off and a policy that allows self-approval.
An author who wants that review writes with `--no-accept` (`accept="never"`):
the proposal waits as `ready_to_activate`, and `next` lists it. The reviewer
runs `proposal review`, then `proposal approve` (signs, and activates when
nothing else is required) or `proposal activate`. [Review and
governance](review-and-governance.md#a-second-agent-reviews) shows both
sides.

## Automation

Lines run Procedures on schedules and events; the [Lines and
automation](lines-and-automation.md) guide walks through one. From the SDK:

- A Procedure is authored in one typed form, the `ProcedureSpec` (JSON over
  MCP/HTTP, YAML on the CLI). In Python, `Sequence`
  (`cruxible_client.authoring.procedures`) is a thin builder over that spec:
  `read`, `call` (fetch, then derive), `extract`, `transform` (filter and
  dedupe), `claims` and `inbox` steps, typed references (`handle["field"]`)
  and typed predicates (`eq`, `gt`, `present`, `all_of`, ...), with
  `.preview()` before submitting. Every other step kind refuses
  `procedure_step_closed`: computation lives in a provider derive. Each slot
  names a capability (`slots={"read": {"capability": "workspace.file@1"}}`),
  never a provider; `orient --section capabilities` lists them. An extract
  names a record set of its CaptureKind's layout (`records="parts"`).
- `cx.line(name).run()` runs a Line once; `.enable()` lets its Triggers fire.
- `cx.accepted_procedure(name)` reads one: `.run(input=...)` for a direct run,
  `.measure(run=...)` and `.readings(...)` for its measurements.
- Read what a Line posted with `inbox list` and `inbox get`, and record your
  judgment with `inbox link`, `dismiss` or `ack`: the inbox never changes
  accepted state. An inbox item is a call for attention, not storage. When the
  list already exists as state (a Query) or as evidence (a Capture), a Line
  posts one summary item pointing at it; it posts one item per element only
  when each needs its own decision that is recorded nowhere else.

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
