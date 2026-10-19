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
(`project.task/write-docs`). Kinds are dotted names you choose; core chooses
the id. A **SubjectKind** declares how a kind's Subjects are named -- by the
values of one or more key fields (a key that names one Subject, `unique`, or
usually one, `hint`), or by a minted id -- and a new Subject is created by
naming its kind and key, so the same key always names the same Subject.

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

Markdown prose and the Claims it supports can land in one `write`. The agent
chooses the file and ATX heading path, supplies `docs` append/replace operations,
and cites `{"kind":"section","file":"notes.md","section":["Decision"]}`.
The selected bytes include the heading and its own body, ending before the first
child. Prose authored by that write counts as the author's own words: a retained
self-source Capture keeps the basis and an ordinary file-copy citation records
its location for drift. Existing sections are file evidence. ClaimType rules
still admit their named contracts; captured-only requirements reject own words.

The client applies only agent-authored text after admission, including when
approval remains pending. The daemon receives bytes and logical source IDs,
never opens workspace paths, and never writes prose. Dry runs write nothing.
Section digests provide compare-and-set; concurrent changes to other sections
can rebase, while a changed target yields a repair. Check each document outcome's
`written` flag. Editing a Document also names the `files propose` follow-up.

A Claim's evidence is one or more of:

- **its author's own words**: the `--because` text every write carries;
- **a citation**: a passage quoted from a catalogued workspace file
  (`--evidence-file PATH#ANCHOR`, `cx.file(path).anchor(...)` in the SDK);
- **a Capture**: retained evidence recorded under a CaptureContract, for
  example by a Procedure that fetched or read something (`--capture CAP-…`, or
  `--evidence-contract` for the newest one about the Subject).

A Capture under a contract the daemon fetches for (not one recording an
author's own words or a quoted file) is evidence only when the journal records
a run producing it: a Source read the run selected, or an `emit_capture`
terminal. Anything else holding Capture bytes, such as bytes put with `body
store` or a Source read its acquisition rule did not select, is refused when
first cited (`capture_production_unproven`). A version-1 Capture under such a
contract carries no producer receipt and is refused when first cited
(`capture_v1_citation_refused`); a Claim already citing one keeps it. A
proposal is held to this again when it is activated, so one admitted before
the rule existed that cites such a Capture is refused
(`cruxible.proposal.capture_production_unproven`): readmit it to see which
citation fails, then resubmit it citing a Capture a run produced.

A ClaimType's evidence rules say which of these support a value of that
field. A Claim whose evidence the rules do not admit is still recorded, with
the verdict `uncovered`.

The file catalog, `.cruxible/files.yaml`, gives each workspace file a
stable name. An entry needs only a `name` and a `locator` (its path) to be
cited; `cruxible files add NAME PATH` writes one. Two different things can then rest on a catalogued file:

| | Citation | Document |
|---|---|---|
| What it is | A Claim quoting a passage of the file as evidence | A governed copy of the whole file: its exact bytes as a versioned artifact with review history |
| When to use it | A Claim relies on what the file says | The file's wording is itself the governed thing: a policy, a spec |
| When the file changes | `next` reports `citation_drifted`; re-bind the Claim to the new text or retire it | `next` reports `document_modified`; `files check` shows the detail |
| How | `set --evidence-file`, `cx.file(...)` | Add `document_id`, `document_kind`, `title`, `media_type` and `governance_scope` to the entry, then `files compile` and `files propose` |

Most usage is citations. A Document whose bytes are not a workspace file (for
example bytes a remote agent supplies) goes through `body store` and
`document propose` instead. `coverage resolve` answers which Claim citations
occur in given file bytes: a whole file, a line range, or `grep -n` output on
standard input.

## Observations over time

Repeated observation never overwrites evidence. Each fetch or read a run makes
is its own Capture with its own time and provenance; Captures of identical
bytes share storage and match each other (their observed content is the same)
without collapsing into one observation.

What a new observation does to accepted state depends on what it shows:

- **The same value again** changes no Claim. Restating a statement on content a
  Claim already cites is a no-op; the re-observation is recorded beside the
  Claim instead, and keeps it fresh: a ClaimType's evidence age counts from the
  latest re-observation, not the first Capture.
- **A different value** is a new statement, proposed as a revision like any
  other change. Claims that depend on the old value are not revised for you:
  `next` resurfaces them, and an agent decides what the change means for each.
- **A failed observation** is a failure to observe, recorded in the run's
  journal. It is not evidence that the value changed, vanished or stayed the
  same.

Keep the boundary by what information supports, not by how often it changes.
Run bookkeeping (started, timed out, retried) stays in the journal. Observations
that conclusions rest on are retained as Captures, including negative results
when a conclusion relies on absence. Accepted Claims hold the assertions and
interpretations you govern. A rapidly changing measurement can be captured on
every poll without a Claim for every fluctuation; propose a Claim when a value
crosses something that matters.

What counts as a meaningful change is the consumer's rule, not core's: a
Procedure compares what it observed with accepted state (its state tap) and
proposes only the differences it is written to care about. When a Line notices
something worth attention but not a proposal, it posts to its inbox.

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
PAYLOAD` checks and lands a payload in one call when no approval is required
and the caller may activate. Otherwise the typed outcome names the proposal,
stop reason and next step. `--no-accept` keeps the proposal; `--dry-run` returns
every refusal without saving anything. For staged work on a large definition,
`authoring compile` keeps a durable intent that `compile --intent-id`,
`preflight` and `submit --intent-id` revise and advance. In the SDK,
`cx.changes(rationale=...)` collects definitions and Claims into one change
set that `.submit()` sends. `cruxible authoring example NAME` prints a
template for each input.

**Vocabulary** keeps its own group, `cruxible claim-type`: `propose` defines
or revises a ClaimType, `migrate` succeeds one and decides, in the same change
set, what happens to every Claim that depends on it (carry it to the new
version, retire it, or say it again under the new vocabulary), and `upgrade`
moves older ClaimTypes to the current format. Changing vocabulary disposes
dependent Claims, so it is never a side effect of another write. With floor
delivery on, the daemon rewrites the workspace's typed stub
(`.cruxible/world.pyi`) when an activation changes vocabulary; with it off,
rerun `cruxible stub`.

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

In a summary read, a text value over 500 characters is shown as a truncated
preview that says how long the value is and names the exact read of the whole
value (`get REF --detail evidence` at the generation it came from), so a long
value never passes for a short one. `cx.read_whole(preview)` makes that read in
the SDK; `cx.read_whole_many(previews)` reads many of them in a few batched
reads, each pinned where its preview was read.

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

`.cruxible/world.pyi` types the accepted vocabulary as Python types for an
editor or a model. It depends on the vocabulary alone (Subjects are typed by
kind), so floor delivery keeps it current by rewriting it only when the
vocabulary changes; `cruxible stub --out PATH` writes one explicitly.

A proposal becomes stale when accepted head moves past its base. On activate,
approve, or readmit, retained authoring/write and Procedure inputs are compiled
again at the head as a fresh proposal that supersedes the old one. Normal
approval policy applies, with no carried approvals. Providers and state-tap
reads are not repeated. Pins and delegated settle authority are checked again;
a refusal leaves the source stale with its repair. Other origins keep byte
readmission. This work happens only on those explicit operations.

## Work queues

Three queues tell you what to do, each a read:

- **next** is what is wrong or waiting on you, each row with the exact
  operation that repairs it: stale or drifted evidence, stale proposals, stale
  blocks, Lines with gaps, available compiler upgrades, and more.
- **audit** ranks Claims worth verifying, by stake, weakness and recency.
- **curation** (beta) lists ontology-maintenance patterns that detection found.
  Detection runs only when a `curation-detect` Trigger aimed at the
  `curation.detect` action is authored (none is seeded), never on `next`'s path;
  `curation list` only reads. Each item takes one ruling: `overrule` closes it as not applying,
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
edited the block's text) and `block sync` shows it. A stale rendered block
regenerates itself: `block sync` re-renders it from its query and repins it.
An authored one needs only the staleness signal: you re-check it, and rewrite
or repin it. See [Projection
blocks](declared-blocks.md).

## Predictions

A prediction is a governed test of an accepted Claim: which later observation
settles it, by what mechanical rule, within which window. `prediction propose`
proposes one; `prediction settle` settles it from the accepted observation
Claim; `prediction list CLAIM` lists the accepted predictions that test that
exact Claim. A prediction is stored as a resolution contract, so `get
ResolutionContract:NAME` reads one.

Settling is an explicit act; nothing settles a prediction on its own. Any
accepted observation inside the window can settle it, and once the window has
closed `next` lists the prediction as `prediction_settleable` with the settle
command. A window that closes with no accepted observation inside it cannot
settle (`prediction_deadline_passed`); Cruxible gives an unobserved window no
meaning of its own, so retire the contract, or record the observation you did
make as a revised Claim and predict again.

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
**Provider** is an installed package that implements interfaces. An interface
is registered once per instance by its definition, so every package
implementing the same definition binds the same registration, and a slot typed
by it takes any of them; a provider runs only the kinds of input it claims.
Every instance starts with the built-in `workspace.file` provider. Others
install with `cruxible provider install`, or arrive with a kit (bundled, or a
default installed by name); installing grants no permission to run anything.

In Python, `@procedure` from `cruxible_client.authoring.source` turns a
decorated function into a `ProcedureSource`, compiled into the same governed
graph; its `slots=` declare open slots by interface, and a source with
unbound slots builds a Blueprint. See the [Procedure source reference](procedure-source.md).

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
  names what the Line may settle on its own and what happens otherwise. A
  Procedure that settles still only proposes when its Line is capped at
  `propose` (`max_authority`) or its only mandate grants propose, so one
  Procedure serves a Line before and after it graduates to settling.
- `line run` always runs one manual occurrence now, under the Line's own
  settings; it never consumes or waits on a Trigger.
- Timed Triggers tick on their schedule's own grid. Enabling or resuming a
  Line never runs a missed tick; `line evaluate` over the missed range records
  every tick it skipped, and `line dispatch` runs them (`line evaluate --dispatch`
  does both). `next` names the exact command for each gap, for example after
  the daemon was down.

Internal actions (floor refresh, evidence sweeps, prediction retries, and
curation detection once its Trigger is authored) also run on Triggers, and need
no enablement.

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
