# Operating Cruxible as an AI agent

Cruxible is built so an agent's first reads are cheap and exact: a map, then
values with flags, then one thing in depth only when the task needs it. Writes
are governed: an agent proposes, and the instance's approval policy decides
what is accepted.

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
the rest: authoring, ClaimTypes, Procedures, Lines, predictions, sources,
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

## Operating rules

1. Treat accepted coordinates as state and proposals as provisional. A
   proposal is not accepted state, and an approval is not activation.
2. Read before you write: grep the floor or `query` for an existing Subject or
   Claim before minting an adjacent one.
3. Record contradiction as evidence against a Claim (`claim attest
   --contradict`, or the value you observed) rather than an inverse concept.
4. Cite where a value came from. Your own words are the default evidence; a
   catalogued file passage or a Capture is checkable.
5. Review the exact candidate before you ask anyone to sign it.
6. Never request, transmit, or place a private key in a repository or a tool
   argument.
7. Treat a repair a diagnostic names as an invitation to a governed
   operation, never as authority.
8. Handle each typed result: a write can be refused, a proposal can go stale,
   an activation can lose a race. Stop and surface the refusal rather than
   retrying blindly.

## Read

Read with three verbs, cheapest first:

1. **orient** maps the instance: each Subject kind with its fields, artifact
   and Claim counts, who you are, what needs attention, and the next calls.
   `orient(kind=K)` widens one kind; `orient(section=S)` pages one family
   (documents, procedures, claim_types, queries, interfaces, runs, lines,
   captures, predictions, mandates, principals, policies, ...).
2. **Find by name**: grep the floor. `.cruxible/floor/current/<kind>/<id>.yaml`
   holds one Subject per file, its first line names its reference, and `get`
   reads that reference live. Without a shell, `query(contains=...)` searches
   every Claim value.
3. **query** answers a question as rows of values with flags: a kind with
   `where`, `select`, `follow` and `order_by`; a named query with `name` and
   `params`; or, for what compact filters cannot say, a full query spec
   (`cruxible_query_spec`, `cruxible query --spec`). Rows carry `flags`
   (`stale`, `contested`, `contradicted`, `uncovered`, `unsure_hold`); a
   truncated page carries `next_cursor`. A wrong kind, field or enum member
   refuses with the nearest valid names instead of answering empty.
4. **get** reads one thing by any reference you have seen: a Claim ID or
   prefix, `kind/id`, a predicate, `ClaimType:`, `Document:`, `Procedure:`,
   `Blueprint:`, `Line:`, `Trigger:`, `Principal:`, `ProviderInterface:`,
   `query:`, a proposal ID. Values come first; `detail` goes deeper:
   `evidence`, `why`, `history` (newest first, paged), `proof`, or a Document
   `body` by byte range. A summary cuts a long string to 500 characters and
   marks it truncated; `evidence` and `proof` read it whole.

The floor is for orientation and grep and is eventually current. Right after
a write, read exact values with `get` or `query`. `since(GENERATION)` lists
exactly what changed after a generation.

### The typed world (SDK)

`cx.world()` reads the accepted vocabulary and hands it back as objects, so
names and constrained values come from the daemon's accepted ontology:

~~~python
w = cx.world()

w.sec.package.cryptography              # SubjectRef, by attribute
w.sec.vulnerability["cve-2026-69247"]   # SubjectRef, by index for any ID
w.sec.vuln.affects_package              # ClaimTypeRef for that predicate
w.sec.vuln.severity.high                # a value only this predicate admits
w.sec.vuln.severity.cardinality         # object_kind, cardinality, permitted_roles, ...
~~~

Dotted kinds nest, so `w.sec.package` and `w.project.task` are namespaces on the
same tree as the predicates. A Subject that does not exist refuses
`AbsentSubject`; an enum member that does not exist refuses naming every
member; a value checks its schema before it reaches the wire. Where an accepted
name is not a Python identifier (a keyword, a hyphen, a collision with a
member), reach it by index: `w.kind("project.class")`,
`w.claim_type("sec.vuln.import")`, `w.sec.vulnerability["cve-2026-69247"]`.

Reading back goes through the same objects:

~~~python
vulnerability = w.sec.vulnerability["cve-2026-69247"]
vulnerability.affects_package   # tuple[ClaimView, ...]: live Claims under that predicate
vulnerability.claims            # every live Claim about this Subject
vulnerability.explain()         # governance and provenance context
~~~

World fields return every live contender for a Subject and predicate;
cardinality-one metadata never silently picks a winner. `world.values(subjects=...,
predicates=...)` returns values and verdicts without full Claim views, and
`w.<ns>.<kind>.where(field=value).select("field")` is the typed form of
`cx.query(kind, where=[...], select=[...])`. A World is pinned to the
coordinate it was read at and stays readable when the live client advances.

`cruxible stub --out world.pyi` writes the vocabulary as closed Python types,
stamped with its coordinate, so an editor or a model completes real names
instead of `Any`. Regenerate it after any activation that changes vocabulary.

## Write values

Values change in the value lane: one change with its verb, several changes
that must land together with `write`.

| | One change | Several, atomically |
|---|---|---|
| MCP | `cruxible_set`, `cruxible_retire` | `cruxible_write` (`set`, `add`, `retire` changes) |
| CLI | `cruxible set`, `add`, `retire` | `cruxible write FILE` or `write -` |
| SDK | `cx.set(...)`, `cx.retire(...)`, `w.<kind>[id].set(because=..., field=value)` | `cx.changes(because=...)` then `.set`, `.add`, `.retire`, `.write()` |

~~~python
from cruxible_client.contracts.write import FileEvidence

outcome = cx.set("project.task/ship-v1", "status", "doing",
                 because="Standup says Grace started.",
                 evidence=FileEvidence(file="notes/standup.md#Grace is starting"))

batch = cx.changes(because="Planning for the week.", subject="project.task/ship-v1")
batch.set("owner", "grace").set("status", "open")
outcome = batch.write()
~~~

Each write proposes one change set and accepts it in the same call when the
approval policy and your tier allow it; otherwise the outcome says
`awaiting approval` and names who may approve it and the exact call. A
refusal names the change, the code, the nearest valid names and a repair.
Check each change's verdict and the warnings: a value written with evidence
the ClaimType does not admit is accepted as `uncovered`, and says so.

- `because` is required; it is the reason and the default evidence.
- `dry_run` runs every check and writes nothing.
- `accept="never"` (`--no-accept`) stops at a proposal.
- A write refuses if the field changed since the coordinate you read at;
  `expect` compares by value instead, and `at` names the coordinate.
- Several sibling changes to one single-value slot cannot land in one set:
  merge them, or split the set.

## Write definitions

Definitions go through the authoring lane: Subjects, named queries,
Procedures, Blueprints, Lines, Triggers, mandates, acquisition policies, the
approval policy, and Claims that need a role, evidence or an effective period
the value verbs do not express.

- **MCP** (full profile): `cruxible_authoring_submit` with a `payload` checks
  and submits in one call; `cruxible_authoring_compile` stages a durable
  intent you can revise (`intent_id`), `preflight`, `rebase` and submit;
  `cruxible_authoring_status` says what still separates it from acceptance;
  `cruxible_authoring_list` and `cruxible_authoring_get` find unfinished work.
  `cruxible_authoring_example` prints a template for every input kind.
- **CLI**: `cruxible authoring submit -` (or `PAYLOAD`), `--dry-run` for every
  refusal without saving anything, `--and-activate` to settle at once when no
  approval is needed. `cruxible authoring example NAME` lists the templates.
- **SDK**: `cx.changes(rationale=...)` opens a change set that `.claim(...)`,
  `.subject(...)`, `.query_definition(...)`, `.procedure(...)`, `.line(...)`,
  `.trigger(...)`, `.claim_type(...)`, `.retire(...)` and the other members
  write into; `.submit()` compiles the whole set as one intent and submits it,
  returning the diagnostics on a refusal, and `.prepare()` checks without
  submitting.

~~~python
draft = cx.changes(rationale="Name the package this advisory affects.")
package = draft.subject(w.sec.package.define("click"))       # a ref, usable in this set
draft.claim(
    subject=w.sec.vulnerability["cve-2026-69247"],
    predicate=w.sec.vuln.affects_package,
    value=package,
    role="observation",
    rationale="The advisory names this package.",
    self_source="affects: click\n",
)
intent = draft.submit()
~~~

A change set lowers once, proposes once and generates once, and is admitted
or refused whole; one malformed member refuses the intent, typed to that
member's index. A member may read a Subject or ClaimType the same set
defines. The `rationale` becomes the candidate commit's message, so say why the
set exists.

### Evolving vocabulary

ClaimTypes keep their own group because changing vocabulary disposes the
Claims that depend on it: `cruxible claim-type propose` (MCP
`cruxible_claim_type_propose`) defines or revises one, and `cruxible claim-type
migrate` (MCP `cruxible_claim_type_migrate`) succeeds one and decides each
dependent Claim's fate in the same change set.

Inside an SDK change set, `ChangeSetDraft.succeed_claim_type(successor,
dependents=[...])` does the same alongside the Claims that speak the new
vocabulary, so "I need this distinction, and here is everything it changes"
lands as one generation. The successor names its predecessor and pins its
current digest; `dependents` must be exactly the predecessor's dependent
closure, and an inexact one refuses
`cruxible.authoring.claim_type_succession_closure_incomplete` naming every
required dependent. Each dependent takes one disposition:

| Helper | What the dependent becomes |
|---|---|
| `carry(claim)` | Re-pinned to the successor, otherwise unchanged. |
| `rescind(claim)` | Retired as `was-rescinded`, keeping the statement it was accepted with. |
| `retire(claim, reason=..., effective_until=...)` | An attributed retirement (`was-wrong`, `was-rescinded`, or `superseded`). |
| `re_author(claim)` | Said again by a sibling Claim member of the same set (`revises=` that Claim), under the successor. |

A successor that changes `object_kind` refuses `carry` for any live dependent;
rescind, retire or re-author those. A re-authored Claim keeps its identity and
slot. The deprecated `invalidation` disposition is refused inside a change set
(`cruxible.authoring.claim_type_succession_disposition_deprecated`); say
`retire` with a reason.

## Evidence

- **Own words**: every write's `because` (or a Claim's `self_source`).
- **A file passage**: catalogue the file in `.cruxible/sources.yaml` (a
  `name` and a `locator` suffice) and cite `PATH#ANCHOR`: `--evidence-file`
  on the CLI, `{"kind": "file", "file": "PATH#ANCHOR"}` as `evidence` on MCP
  `cruxible_set`, `FileEvidence` on SDK writes, or `cx.file(path).anchor(...)`
  as `supported_by` or `copied_from` on an authored Claim. The client reads the
  file and sends what it observed; the daemon never reads workspace files.
  When the passage changes, `next` reports `citation_drifted`.
- **A Capture**: retained evidence, cited by handle (`CAP-…`) or as the newest
  verified Capture of a CaptureContract about the Subject. `cx.capture(ref)`
  and `cruxible capture read` read a retained body; they never refetch the
  source.
- **Attestation**: `cruxible claim attest CLAIM_ID --support|--contradict|--unsure`
  (MCP `cruxible_claim_attest`) signs that you examined the exact Claim.

A passage inside a projection block is never evidence; cite the Claim it
reflects. `coverage resolve` (MCP `cruxible_coverage_resolve`) answers what
files you read or changed have to do with accepted state, including `grep -n`
output.

## Review a proposal

The ledger is Git, so review is Git. The daemon fetches its own refs into the
attached workspace on every proposal, so a reviewer diffs the candidate
against accepted state with ordinary tooling:

~~~text
git diff cruxible-ledger/accepted...cruxible-ledger/proposals/<proposal-id>
~~~

The branch name is the proposal ID without its `sha256:` prefix; `cruxible
proposal review ID` prints the exact command. The candidate commit's message
is the change set's own summary. The daemon's records are Git notes on the
same commit: `refs/notes/playbill-eval` carries the admission and the
evaluation verdict with every diagnostic behind a refusal, and
`refs/notes/playbill-approval` the approvals with each signer's attestation
(`git notes --ref=refs/notes/playbill-eval show
cruxible-ledger/proposals/<proposal-id>`). Nothing parses those messages;
every fact an agent should act on is in `proposal review --json`, in `get
PROPOSAL_ID`, or in the notes.

An agent with no attached workspace reads the same refs from the ledger
mirror: `orient --json` carries `mirror_url` when the instance publishes to
one. Clone it; `origin/main` is accepted state and
`origin/proposals/<proposal-id>` the candidate, and `git fetch origin
'+refs/notes/*:refs/notes/*'` fetches the notes. Local acceptance does not
imply the mirror has it yet: before a remote review, run `cruxible ledger
publish --json` and require `published_sequence >= wait_sequence`.

Approval signs locally and sends only the public signature. `cruxible proposal
approve` and `cruxible_proposal_approve` do it in one call; the MCP tool signs
with a key from the server's `CRUXIBLE_MCP_KEY_DIR`, and passing the reviewed
`candidate_digest` makes it refuse a candidate that changed since you read it.
A signer outside the MCP process uses the full-profile pair
`cruxible_proposal_approve_prepare` (the exact statement to sign) and
`cruxible_proposal_approve_submit` (the public attestation).

## Work queues

- `next` lists what is wrong or waiting on you, each row with its exact
  repair: drifted or stale evidence, uncovered Claims, stale proposals to
  readmit, stale projection blocks, Line gaps, compiler upgrades, consumers
  that stalled. Work it top down.
- `audit` ranks Claims worth verifying (full profile).
- `curation list` lists ontology-maintenance patterns; each takes one ruling:
  `overrule`, `suppress`, `unsuppress`, or `accept-fixed` (full profile).

## Procedures and Lines

A Procedure is authored like any definition. The SDK offers three forms, all
producing graph-format-6 definitions:

- `ProcedureInput`, the raw definition;
- `Sequence` from `cruxible_client.authoring.procedures`, a typed step list
  (StateTap, Source, Call, Transform, Project, Guard, EmitCapture,
  ProposeChangeSet, SettleChangeSet, Halt) with `.preview()` before
  submitting;
- `ProcedureSource`: a function decorated with `@procedure` from
  `cruxible_client.authoring.source`, compiled into the same graph; see the
  [source authoring reference](sdk-v2-reference.md).

Every provider a Procedure calls is pinned to one exact implementation;
`cx.provider_interface(interface, provider=...)` selects one. A definition
that leaves provider slots open is a Blueprint, which never runs:
`get Blueprint:NAME` lists each slot with the installed providers that fit,
and a `blueprint_instance` input binds them into an ordinary Procedure
(`cruxible authoring example blueprint-instance`).

`get Procedure:NAME` says how a Procedure runs: directly, only as a Line, or
not at all. A direct run is `cruxible procedure run NAME INPUT`,
`cruxible_procedure_run`, or `cx.accepted_procedure(name).run(input=...)`.
Terminals that retain a Capture, propose a change set or settle one act only
on a Line.

A Line runs a Procedure under its own parameters, budgets, authority ceiling
and acquisition policy:

- `line run` (`cx.line(name).run()`) runs one manual occurrence now; it never
  consumes a Trigger. Pass `event` when the Procedure takes one.
- Triggers do nothing until the Line is enabled (`line enable`,
  `cx.line(name).enable()`), which needs governed write even for a Line that
  only observes. Enabling pins the Line and Trigger versions; any change to
  either stops it until it is enabled again.
- A Line that proposes or settles needs a covering mandate.
- `line evaluate` and `line dispatch` recover what automation missed; `next`
  names the exact command for each gap.

A Source node reads through an accepted Provider under accepted authority, not
through ambient filesystem or network access. Before it can run, accepted state
must hold the Provider and its interface, the CaptureContract the node pins,
and the SourceAcquisitionPolicy that governs the read. Name that policy when
you author the Procedure (or on the Line); the Procedure then reads only that
policy. A missing or ambiguous policy refuses
`source_acquisition_policy_required`; a rule that denies an input refuses
`source_acquisition_refused`; a path outside an authorized workspace root or
over the CaptureContract's selection budget refuses
`workspace_file_read_refused`. None of these leave partial run history.

### Measurements and readings

A Procedure may declare measurements: an accepted query with an expectation, a
Claim statement's acceptable verdicts, or the attestations on a statement. The
generation that accepts the Procedure activates them, and the window
(`check_after`, `expires_after`) runs from that acceptance. Evaluating is a
separate, explicit step:

~~~python
proc = cx.accepted_procedure("release-guard")
run = proc.run(input=proc.input(release="2.4.0"))

batch = proc.measure(run=run)             # evaluated at this connection's clock
batch["rollout-healthy"].status           # "pending" | "open" | "expired" | "resolved"
batch["rollout-healthy"].reading_status   # "recorded", "replayed", "no_resolution", ...

page = proc.readings(measurements=("rollout-healthy",), limit=50)   # read-only
~~~

A pending or expired measurement reports and writes nothing; a due one gathers
evidence and resolves, and the standing resolution answers every later call
until it is overturned. A reading is minted only for the grain the run really
reached. A completed run does not satisfy a measurement, and a failed one does
not contradict it; the verdict comes from the evidence. Resolutions and
readings are operational records, not accepted state. The CLI forms are
`cruxible procedure measure` and `cruxible procedure readings`.

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
