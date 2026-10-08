# Quickstart

This walk-through starts a daemon, creates an instance, defines a little
vocabulary, writes and reads values, cites a file as evidence, reviews a
change, and renders a table into a page. It takes about fifteen minutes.

## Install

Requirements: Python 3.11+ and Git.

~~~bash
pip install cruxible        # or: uv tool install cruxible
~~~

From a source checkout, run `uv sync --all-packages --all-extras` and prefix
the commands below with `uv run`.

## Start a daemon

The daemon owns accepted state. Start it in its own shell:

~~~bash
cruxible server start --socket ~/.cruxible/run/daemon.sock
~~~

It keeps its state under `~/.cruxible` (`--state-root` picks another
directory). A Unix-socket daemon runs with auth off and says so in one line
when it starts: every process of your OS user is equally trusted, so no bearer
token is needed locally. A TCP daemon refuses to start without `--auth`. The
daemon binds the socket owner-only and refuses a socket directory another user
could replace. `cruxible server install-service` installs it as a user
service instead.

## Create an instance

In a second shell, inside a Git repository (a new one is fine):

~~~bash
mkdir tasks && cd tasks && git init
export CRUXIBLE_SERVER_SOCKET=~/.cruxible/run/daemon.sock
cruxible init
~~~

With no instance selected, `init` creates a host on the daemon, initializes
it, and attaches this repository as its workspace. You become the owner under
your OS username (`--principal-id ID` picks another), with a private key
generated under `~/.config/cruxible/keys/` (`--key-dir DIR` picks another),
outside the repository and the daemon's state. The CLI remembers the daemon,
the instance and your principal, so later commands need no flags or
environment.

The new instance allows self-approval: you can approve and activate your own
changes. `--require-independent-approval` (with `--reviewer-key-dir DIR` for a
second principal) makes every governed change need an approval from someone
other than its author.

~~~bash
cruxible orient
~~~

`orient` is the map of an instance: its Subject kinds, artifacts, who you are,
and what needs attention. It is nearly empty for now.

## Define vocabulary

Values in Cruxible are Claims about Subjects. A Subject is a thing, named
`kind/id` (`project.task/write-docs`). A ClaimType defines one field of a
kind: what values it admits, how many a Subject may hold, and which evidence
supports a value. Define two fields for tasks:

~~~bash
cruxible claim-type propose --name add-task-owner --input - <<'EOF'
{
  "predicate": "project.task.owner",
  "description": "Who is doing the task.",
  "allowed_subject_kinds": ["project.task"],
  "object_kind": "literal",
  "literal_schema": {"type": "string"},
  "cardinality": "one",
  "permitted_roles": ["observation"],
  "default_role": "observation",
  "evidence_admission_policy": {"rules": [{
    "rule_id": "own-words-or-standup",
    "claim_roles": ["observation"],
    "capture_contracts": [
      "CaptureContract:playbill.coordinator-self-source-v1",
      "CaptureContract:playbill.foreign-source.standup"
    ],
    "evidence_kinds": ["self_asserted"],
    "admission": "direct",
    "subject_binding": "exact_claim_subject"
  }]},
  "admission_policy": {},
  "resolution_policy": {"cardinality": "one", "eligible_verdicts": ["supported"],
                        "selector": "only_contender"},
  "anticipated_source_ids": ["standup"]
}
EOF
~~~

The evidence rule admits two kinds of evidence: the writer's own words (the
`--because` text every write carries) and passages of a workspace file you
will catalogue as `standup`. The two contract names are fixed identifiers;
`anticipated_source_ids` lets the rule name the `standup` source before
anything has cited it.

The command printed a proposal: a frozen candidate checked against the
current accepted state, not yet accepted itself. Find it, read it, and
activate it:

~~~bash
cruxible proposal list --status open
cruxible get PROPOSAL_ID
cruxible proposal activate PROPOSAL_ID
~~~

Activation advances accepted state by compare-and-set. A proposal is checked
against the state it was proposed on, so one proposed before another was
activated becomes stale; `cruxible next` lists it and `cruxible proposal
readmit` checks it again at the new state. Propose the second field now, with
an enumerated value:

~~~bash
cruxible claim-type propose --name add-task-status --input - <<'EOF'
{
  "predicate": "project.task.status",
  "description": "Where the task stands.",
  "allowed_subject_kinds": ["project.task"],
  "object_kind": "literal",
  "literal_schema": {"type": "string", "enum": ["open", "doing", "done"]},
  "cardinality": "one",
  "permitted_roles": ["observation"],
  "default_role": "observation",
  "evidence_admission_policy": {"rules": [{
    "rule_id": "own-words-or-standup",
    "claim_roles": ["observation"],
    "capture_contracts": [
      "CaptureContract:playbill.coordinator-self-source-v1",
      "CaptureContract:playbill.foreign-source.standup"
    ],
    "evidence_kinds": ["self_asserted"],
    "admission": "direct",
    "subject_binding": "exact_claim_subject"
  }]},
  "admission_policy": {},
  "resolution_policy": {"cardinality": "one", "eligible_verdicts": ["supported"],
                        "selector": "only_contender"},
  "anticipated_source_ids": ["standup"]
}
EOF
~~~

Activate it the same way:

~~~bash
cruxible proposal list --status open
cruxible proposal activate PROPOSAL_ID
~~~

`--dry-run` on `claim-type propose` runs every check without proposing.
Vocabulary has its own command group because changing a ClaimType decides what
happens to every Claim that uses it (see `cruxible claim-type migrate`).

## Write values

~~~bash
cruxible set project.task/write-docs status doing --because "Started the docs today."
~~~

~~~text
accepted (generation 3, 1d0c6e2a9b41)
  set project.task/write-docs status: doing  [CLM-…; verdict supported]
  + subject project.task/write-docs
~~~

`set` replaced the field's value (there was none), created the Subject because
its kind is known, and accepted the change at once because the approval
policy lets you. `--because` is required: it says why, and it is the default
evidence. `verdict supported` means the evidence is admitted by the
ClaimType's rule.

Several changes that belong together go in one `write`, accepted or refused as
a whole. Every payload argument accepts `-` for standard input, so nothing
lands on disk:

~~~bash
cruxible write - <<'EOF'
{"because": "Planning for the week.",
 "changes": [
   {"op": "set", "subject": "project.task/write-docs", "field": "owner", "value": "ada"},
   {"op": "set", "subject": "project.task/ship-v1", "field": "owner", "value": "grace"},
   {"op": "set", "subject": "project.task/ship-v1", "field": "status", "value": "open"}
 ]}
EOF
~~~

`cruxible write --schema` prints the payload schema. `cruxible retire
project.task/ship-v1 status --because "..."` ends a value, and `add` adds one
more value to a field whose ClaimType allows many.

## Read

Three verbs read accepted state:

~~~bash
cruxible orient                                   # the map, now with project.task
cruxible query project.task --select owner,status
cruxible query project.task --where owner=ada
cruxible get project.task/write-docs
cruxible get project.task/write-docs --detail why
~~~

~~~text
subject                  owner  status  flags
project.task/ship-v1     grace  open    -
project.task/write-docs  ada    doing   -
~~~

`query` answers with rows of values; `flags` marks a value that is stale,
contested, contradicted, uncovered, or held as unsure. `get` reads one thing
by any reference you have seen (a `kind/id`, a Claim ID, `ClaimType:NAME`, a
proposal ID) and goes deeper with `--detail`: `evidence`, `why`, `history`,
`proof`.

The daemon also keeps the floor current: plain files under
`.cruxible/floor/`, one per Subject, for orientation and grep.

~~~bash
grep -r ada .cruxible/floor/current/
~~~

~~~text
.cruxible/floor/current/project.task/write-docs.yaml:owner: ada  # CLM-…
~~~

Each file's first line names its reference, which `get` reads live. The floor
follows accepted state shortly after each change; read exact values right
after a write with `get` or `query`.

## Cite a file

A value can rest on a passage of a file in your workspace. Catalogue the file
in `.cruxible/sources.yaml`, which gives it the stable name the evidence rule
above admits:

~~~bash
mkdir -p notes
printf '# Standup\n\n- ship-v1: Grace is starting on it today.\n' > notes/standup.md
cat > .cruxible/sources.yaml <<'EOF'
catalog_kind: portable
entries:
  - name: standup
    locator: notes/standup.md
EOF
git add notes .cruxible/sources.yaml && git commit -m "Standup notes"

cruxible set project.task/ship-v1 status doing \
  --because "Standup says Grace started." \
  --evidence-file "notes/standup.md#Grace is starting on it today"
~~~

`--evidence-file PATH#ANCHOR` cites the text found once in the file. The
quoted passage is retained with the Claim. Now change the file:

~~~bash
sed -i.bak 's/Grace is starting on it today/Grace is blocked on review/' notes/standup.md
cruxible next
~~~

~~~text
repair  citation_drifted  Claim:CLM-…  next=cruxible.claim.retire
  repair: cruxible retire CLM-…
~~~

`next` is the repair queue: everything that is wrong or waiting on you, each
row with the operation that repairs it. Here the passage the Claim cites is
gone, so the Claim needs a decision: retire it, as the row suggests, or set the
value again citing the new text.

## Review a change

`--no-accept` stops a write at a proposal, as it would stop for anyone when
the approval policy requires an independent approval:

~~~bash
cruxible set project.task/ship-v1 owner ada --because "Reassigned." --no-accept
~~~

~~~text
awaiting approval (generation 5, …)
  set project.task/ship-v1 owner: grace -> ada  [CLM-…; verdict supported]
proposal: sha256:… (ready_to_activate)
~~~

Review it, approve it with your key, and activate it:

~~~bash
cruxible proposal review PROPOSAL_ID
cruxible proposal approve PROPOSAL_ID --yes
cruxible proposal activate PROPOSAL_ID
~~~

`proposal review` prints how to diff the candidate against accepted state with
plain Git in this workspace (`git diff
cruxible-ledger/accepted...cruxible-ledger/proposals/…`) and where the
daemon's evaluation and approval records are. `proposal approve` signs the
exact candidate with your private key on this machine and sends only the
signature. Activation then advances accepted state by compare-and-set.

## Render a table into a page

A projection block is a passage of a workspace page that reflects accepted
state and carries a stamp of what it reflects. A rendered block is the output
of a named query. First accept the named query:

~~~bash
cruxible authoring submit - --and-activate --brief <<'EOF'
{"kind": "query_definition",
 "query_definition": {
   "identity": {"kind": "QueryDefinition", "name": "project.task_status"},
   "description": "Every task with its owner and status.",
   "entry": {"binding": "task", "subject_kinds": ["project.task"]},
   "projection": {"fields": [
     {"name": "owner", "value": {"kind": "claim_value", "binding": "task", "predicate": "project.task.owner"}},
     {"name": "status", "value": {"kind": "claim_value", "binding": "task", "predicate": "project.task.status"}}
   ]},
   "result_binding": "task",
   "result_shape": "subject",
   "result_cardinality": "many",
   "dedupe": "subject",
   "evaluation_policy": {"visible_verdicts": ["supported"], "visible_currency": ["current"],
                         "conflict_behavior": "surface_conflicts",
                         "requires_accepted_coordinate": true,
                         "requires_explicit_evaluation_time": true},
   "default_budgets": {"max_results": 100, "max_traversal_depth": 0},
   "maximum_budgets": {"max_results": 1000, "max_traversal_depth": 0}
 }}
EOF
cruxible query --name project.task_status
~~~

`authoring` is the lane for definitions (named queries, Subjects, Procedures,
Lines, Triggers, policies). `authoring submit --dry-run` returns every refusal
without saving anything, and `cruxible authoring example` lists a template for
each kind.

Then write a page with an empty block, catalogue it, and stamp it:

~~~bash
printf '# Status\n\n<!-- cruxible:block:tasks -->\n<!-- /cruxible:block:tasks -->\n' > notes/status.md
cat >> .cruxible/sources.yaml <<'EOF'
  - name: status-page
    locator: notes/status.md
EOF
cruxible block repin status-page tasks --query QueryDefinition:project.task_status --render
cat notes/status.md
~~~

~~~text
# Status

<!-- cruxible:block:tasks:ref:… -->
| subject | owner | status |
|---|---|---|
| Subject:project.task/ship-v1 | ada | doing |
| Subject:project.task/write-docs | ada | doing |
<!-- /cruxible:block:tasks -->
~~~

When the state behind the block moves, `cruxible next` and `cruxible block
sync --all` report it stale; repin it to refresh it. Blocks can also hold prose
you write yourself, stamped against the Claims it summarizes; see
[Projection blocks](declared-blocks.md).

## Next

- [Concepts](concepts.md): the model behind what you just did.
- [Modeling state](modeling-state.md): what to make a Subject, a ClaimType, or
  a file.
- [For AI agents](for-ai-agents.md): connect an agent over MCP or the Python
  SDK.
- [Kits](kits.md): install vocabulary, queries and Procedures someone else
  built.
- [CLI reference](cli-reference.md).
