# Concepts

This page explains the model behind the commands. The
[Quickstart](quickstart.md) shows it in use; the [CLI reference](cli-reference.md)
and [MCP tools](mcp-tools.md) list every operation.

## Accepted state

A daemon owns each instance's accepted state: a Git ledger whose commits are
accepted generations, plus content-addressed storage for large bodies. Nothing
becomes accepted except by activating a proposal, and every accepted
generation has a coordinate:

- the Git object ID of the ledger commit;
- the semantic root, a digest of what the state means;
- the generation root, a digest of the generation;
- the compiler digest, naming the rules that interpreted it.

Generations are also numbered (0 is genesis). Reads answer at a coordinate,
the current head unless you name an older one with `--at`, so an answer can
always say which state it came from.

Everything else is derived and can be rebuilt: the daemon's indexes, the
floor files in your workspace, and the projection blocks in your pages.

## Subjects, ClaimTypes and Claims

A **Subject** is a thing the instance knows about, named `kind/id`
(`project.task/write-docs`). Kinds are dotted names you choose.

A **ClaimType** defines one field of a kind: the predicate
(`project.task.status`, whose field name is `status`), what the value is (a
literal checked against a JSON schema, a reference to another Subject, or exact
content), whether a Subject holds one value or many, which roles a Claim may
take (`normative`, `observation`, `environment_binding`, `derivation`), which
evidence it admits, and how competing values resolve. A ClaimType whose value
is another Subject is a typed relationship.

A **Claim** is one value of one field about one Subject: the statement, its
role, the evidence it cites, and the rationale its author gave. A Claim has an
ID (`CLM-…`) and a history of revisions. Resolution places it in its slot as
accepted, conflicted, overturned or refused, and retiring it ends it with a
reason (`was-rescinded`, `was-wrong` or `superseded`).

Every Claim also has a verdict that says how well its evidence holds up:
`supported`, `uncovered` (no admitted evidence), `stale`, `contradicted`, or
`unresolved`. Reads carry flags beside values (`stale`, `contested`,
`contradicted`, `uncovered`, `unsure_hold`) so an agent sees a weak value as
weak without opening it.

Negative evidence is first-class. When you have evidence against a Claim,
attest to it (`cruxible claim attest CLAIM_ID --contradict`) or record what
you observed instead, rather than minting an adjacent inverse concept.
`--support` and `--unsure` attest the other ways.

## Evidence

A Claim's evidence is one or more of:

- **its author's own words**: the `--because` text every write carries;
- **a citation**: a passage quoted from a catalogued workspace file
  (`--evidence-file PATH#ANCHOR`, `cx.file(path).anchor(...)` in the SDK);
- **a Capture**: retained evidence recorded under a CaptureContract, for
  example by a Procedure that fetched or read something (`--capture CAP-…`, or
  `--evidence-contract` for the newest one about the Subject).

A ClaimType's evidence rules say which of these support a value of that
field. A Claim whose evidence the rules do not admit is still recorded, with
the verdict `uncovered`.

The source catalog, `.cruxible/sources.yaml`, gives each workspace file a
stable name. An entry needs only a `name` and a `locator` (its path) to be
cited. Two different things can then rest on a catalogued file:

| | Citation | Document |
|---|---|---|
| What it is | A Claim quoting a passage of the file as evidence | A governed copy of the whole file: its exact bytes as a versioned artifact with review history |
| When to use it | A Claim relies on what the file says | The file's wording is itself the governed thing: a policy, a spec |
| When the file changes | `next` reports `citation_drifted`; re-bind the Claim to the new text or retire it | `next` reports `document_modified`; `sources check` shows the detail |
| How | `set --evidence-file`, `cx.file(...)` | Add `document_id`, `document_kind`, `title`, `media_type` and `governance_scope` to the entry, then `sources compile` and `sources propose` |

Most usage is citations. A Document whose bytes are not a workspace file (for
example bytes a remote agent supplies) goes through `body store` and
`document propose` instead. `coverage resolve` answers which Claim citations
occur in given file bytes: a whole file, a line range, or `grep -n` output on
standard input.

## Writing: two lanes

Every change is a change set proposed against an accepted base. Two lanes lead
to it, over the same engine.

**Values** change with `set` (replace one field's value), `add` (one more
value in a many-valued field), `retire` (end one Claim) and `write` (several of
these as one atomic change set with one reason). In the SDK, `cx.set(...)`,
`cx.retire(...)` and `cx.changes(because=...)` with `.set`, `.add`,
`.retire` and `.write()`. A value change is accepted in the same call when the
approval policy and the caller's tier allow it, and otherwise stops at a
proposal that names who may approve it.
`--no-accept` stops it at a proposal anyway; `--dry-run` runs every check and
writes nothing; `--expect` and `--expect-absent` make it compare-and-set on the
current value.

**Definitions** go through `authoring`: Subjects, named queries, Procedures,
Blueprints, Lines, Triggers, mandates, acquisition policies and the approval
policy, alone or as members of one change set. `cruxible authoring submit
PAYLOAD` checks and submits a payload in one call, and `--dry-run` returns
every refusal without saving anything. For staged work on a large definition,
`authoring compile` keeps a durable intent that `compile --intent-id`,
`preflight`, `rebase` and `submit --intent-id` revise and advance. In the SDK,
`cx.changes(rationale=...)` collects definitions and Claims into one change
set that `.submit()` sends. `cruxible authoring example NAME` prints a
template for each input.

**Vocabulary** keeps its own group, `cruxible claim-type`: `propose` defines
or revises a ClaimType, `migrate` succeeds one and decides, in the same change
set, what happens to every Claim that depends on it (carry it to the new
version, retire it, or say it again under the new vocabulary), and `upgrade`
moves older ClaimTypes to the current format. Changing vocabulary disposes
dependent Claims, so it is never a side effect of another write. After an
activation that changes vocabulary, rerun `cruxible stub` if you use a typed
stub.

Every payload argument accepts `-` for standard input, so a heredoc or a pipe
works and no throwaway file lands on disk. Real artifacts stay files:
Procedure source, signed source bundles, kit directories, key directories,
cited workspace files and pages.

## Proposals, approval and activation

A **proposal** is a frozen candidate: the exact change, evaluated against the
accepted base it names. It is not accepted state. A refused proposal is kept
with its diagnostics, and `get PROPOSAL_ID` reads any proposal, its status, and
for a refusal the code, message and repair. `proposal review` shows the
candidate and how to diff it with plain Git.

The instance's **approval policy** (`ApprovalPolicy:instance`) is either
`self_approval_allowed`, where an author may approve and activate their own
changes, or `independent_approval_required`, where a governed change needs an
approval from someone other than its author. Writes, provider installs, and
kit adds and removes all land at once when the policy requires no approval
and the caller's tier may activate, and otherwise stop at a proposal.

An **approval** is a signature over the exact candidate, made with the
approver's private key on the client; only the signature reaches the daemon.
An approval does not activate anything. **Activation** checks the approvals
and the policy and advances accepted state by compare-and-set: if another
generation was accepted first, the proposal is stale and must be re-admitted
(`proposal readmit`) at the new head before it can activate. `proposal
withdraw` retires a proposal that will never activate.

## Principals and credentials

Two different things answer "who is this", and they are easy to confuse:

- A **principal** is a governed identity in the instance's ledger: a public
  key with a kind (owner, ordinary, or recovery). Principals attribute every
  governed act; approvals and attestations are signatures made with the
  principal's private key, which stays in a key directory on the client
  (`~/.config/cruxible/keys/` by default). Principals are added, rotated,
  recovered and revoked with `cruxible principal`, each a governed change.
- A **credential** is a daemon bearer token. It decides whether a caller may
  reach the daemon's endpoints and caps what it may do with a tier:
  `read_only`, `governed_write` (propose and author), `graph_write` (also
  approve and activate) or `admin` (also operator acts such as credentials,
  hosts, principal changes, compiler upgrades and provider installs).
  Credentials are minted, rotated and revoked with `cruxible credential`.

A Unix-socket daemon runs with auth off by default: every process of your OS
user is equally trusted, there are no bearer tokens, and the principal ID a
process sends is a claim of identity, not authentication. A TCP daemon must run
with `--auth`, and then each credential is bound to one principal. Either way,
an approval still needs the principal's private key. Local key directories give
attribution and hygiene, not a security boundary between processes of one OS
user.

`cruxible init` makes you the owner and remembers your settings; `cruxible
principal add` registers another principal and writes its settings to its key
directory; `cruxible context use --principal ID` switches which principal the
CLI acts as; `cruxible whoami` reports who you are and whether you can author.

## Reading

Three verbs read accepted state on every surface:

- **orient** maps the instance: each Subject kind with its fields and counts,
  artifact counts, who you are, what needs attention, and the next commands.
  `--kind K` reads one kind in full; `--section S` pages one family
  (documents, procedures, claim types, queries, interfaces, runs, lines,
  captures, predictions, mandates, principals, policies, ...).
- **query** answers a question as rows of values with flags: a kind with
  `--where`, `--select`, `--follow` and `--order-by`, `--contains` for text in
  any value, a named query with `--name`, or a full query spec with `--spec`.
  Kinds `ClaimType`, `Procedure`, `Trigger` and `Line` list definitions.
- **get** reads one thing by any reference: `kind/id`, a Claim ID, a proposal
  ID, `ClaimType:`, `Document:`, `Procedure:`, `Blueprint:`, `Line:`,
  `Trigger:`, `Principal:`, `ProviderInterface:`, `query:` and others. Values
  come first; `--detail` goes to `evidence`, `why`, `history`, `proof`, or a
  Document `body`.

`since GENERATION` lists exactly what changed after a generation.

### The floor

The floor is a directory of plain files, `.cruxible/floor/`, with one file per
Subject (`current/<kind>/<id>.yaml`, each line a value and its Claim ID), for
orientation and grep. It is eventually current: it follows accepted state
shortly after each change, so read exact values after a write with `get` or
`query`, not the floor.

A local daemon delivers the floor itself to a registered workspace, after each
accepted generation, while delivery is on (`cruxible floor delivery on|off`;
`init` and `workspace attach` turn it on, and `workspace attach
--no-floor-delivery` leaves it off). Anywhere else, such as a remote daemon or
with delivery off, pull it with `cruxible floor export` or
`cx.refresh_workspace()`.

`cruxible stub --out world.pyi` writes the accepted vocabulary as Python types
for an editor or a model. A stub describes one coordinate: regenerate it after
activations that change vocabulary.

## Work queues

Three queues tell you what to do, each a read:

- **next** is what is wrong or waiting on you, each row with the exact
  operation that repairs it: stale or drifted evidence, stale proposals, stale
  blocks, Lines with gaps, available compiler upgrades, and more.
- **audit** ranks Claims worth verifying, by stake, weakness and recency.
- **curation** lists ontology-maintenance patterns that detection found.
  Detection runs on its own after accepted generations; `curation list` only
  reads. Each item takes one ruling: `overrule` closes it as not applying,
  permanently; `suppress` hides the item, or with `--scope lineage` also every
  successor its pattern opens; `unsuppress` lifts a suppression; and
  `accept-fixed` links it to the accepted proposal or generation that fixed
  it.

## Projection blocks

A projection block is a delimited passage of a workspace page that reflects
accepted state, with a stamp recording exactly what it reflects. Blocks are
either:

- **rendered**: the output of a named query, written by `block repin
  --render` (a table or a list); or
- **authored**: prose you or an agent wrote, stamped against the Claims or
  queries it summarizes.

Both are governed the same way. When the state behind a block moves, `next`
reports it (`projection_backing_stale`, or `projection_dirty` when someone
edited the block's text) and `block sync` shows it; you re-check it, and
rewrite or repin it. Only a rendered block can regenerate itself; an authored
one needs only the staleness signal. See [Projection
blocks](declared-blocks.md).

## Predictions

A prediction is a governed test of an accepted Claim: which later observation
settles it, by what mechanical rule, within which window. `prediction propose`
proposes one; `prediction settle` settles it from the accepted observation
Claim; `prediction list CLAIM` lists the accepted predictions that test that
exact Claim. A prediction is stored as a resolution contract, so `get
ResolutionContract:NAME` reads one.

## Procedures, Blueprints and providers

A **Procedure** is a governed, deterministic graph: it reads accepted state
(named queries and Claims), acquires observations through Sources, calls
providers, transforms and routes values, and ends in a result or a terminal
that retains a Capture, proposes a change set, or settles one. Every provider
it calls is pinned to one exact implementation, so a run is reproducible from
its record. `cruxible procedure run NAME INPUT` runs one directly; `get
Procedure:NAME` says whether it runs directly, only as a Line (its terminals
act only there), or not at all. A Procedure may declare measurements, which
`procedure measure` evaluates once due and `procedure readings` reads.

A **Blueprint** is the same definition with one or more provider slots left
open, each naming the ProviderInterface it needs. It never runs.
Instantiating it (a `blueprint_instance` authoring input) binds one installed
provider per slot and produces an ordinary Procedure that records its
Blueprint and bindings. `get Blueprint:NAME` lists each slot and the installed
providers that fit it.

A **ProviderInterface** is a typed contract (input, output, effect); a
**Provider** is an installed package that implements interfaces. Every
instance starts with the built-in `workspace.file` provider. Others install
with `cruxible provider install`, or arrive bundled in a kit; installing grants
no permission to run anything.

In Python, `@procedure` from `cruxible_client.authoring.source` turns a
decorated function into a `ProcedureSource`, compiled into the same governed
graph; its `slots=` declare open slots by interface, and a source with
unbound slots builds a Blueprint. See the [SDK v2 reference](sdk-v2-reference.md).

## Lines, Triggers and mandates

A **Line** runs a Procedure under its own inputs, budgets, authority ceiling
and acquisition policy, and records each run on the Line's history. A
**Trigger** says when: on a cadence, a cron schedule (UTC), when a Capture
lands under a CaptureContract, when a window closes, or when a generation is
accepted.

Lines are enabled, not armed:

- A Trigger aimed at a Line does nothing until a principal enables the Line
  (`line enable`). Enabling needs governed write, even for a Line that only
  observes.
- Enabling pins the Line version and the exact Trigger versions aimed at it.
  Changing the Line or any of its Triggers stops the enablement until someone
  enables it again, which is consent to the new schedule.
- A Line that proposes or settles changes needs a current **mandate**
  covering its Procedure, and refuses to enable without one. A settle mandate
  names what the Line may settle on its own and what happens otherwise.
- `line run` always runs one manual occurrence now, under the Line's own
  settings; it never consumes or waits on a Trigger.
- `line evaluate` and `line dispatch` recover what automation missed, for
  example the time a daemon was down. `next` names the exact command for each
  gap.

Internal actions (floor refresh, evidence sweeps, prediction retries,
curation detection) also run on Triggers, and need no enablement.

"Arm" has one meaning: a labeled branch of a Procedure graph, such as a
guard's true or false edge, which measurements can read at `arm` grain to
compare experiment arms.

## Kits

A kit is a release of definitions (ClaimTypes, CaptureContracts, named
queries, Procedures, Blueprints, ProviderInterfaces and acquisition policies)
that another instance installs and upgrades as one reviewed change set. See
[Kits](kits.md).

## The compiler

The compiler is the deterministic interpretation of the ledger, named by its
digest in every coordinate. Installing a new release never changes accepted
state; adopting a new compiler revision is a governed change. See
[Upgrading](upgrading.md).

## Hot and cold paths

High-rate activity (runs, attempts, logs, intermediate results) is retained
as operational exhaust, not as accepted state. Procedures and Lines select
from it and propose what should be governed. The exhaust records what
happened; the ledger records what has been accepted.
