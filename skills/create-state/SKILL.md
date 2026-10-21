---
name: create-state
description: Model a new domain in Cruxible - decide what is typed state and what stays prose, define Subject kinds and ClaimTypes, seed values with evidence, and add named queries and pages - in reviewed stages.
---

# Create State

Use this skill when a user wants a domain's knowledge in Cruxible: typed,
governed values that people and agents share, each with evidence and history.
Each stage ends in something the user can review; don't define vocabulary,
load values and build pages in one pass.

If the inputs are messy files, run `prepare-data` first. If a kit already
models the domain, use `adopt-kit` and come back here only for what it lacks.

The guides are the source for everything below (https://docs.cruxible.ai, or
`docs/` in the Cruxible repository); this skill is the order to do it in.

## 1. Connect and orient

```bash
cruxible whoami        # who you are, and whether you can author
cruxible orient        # kinds, fields, counts, attention, next commands
```

Creating an instance is operator work (`cruxible server start`, then `cruxible
init` in the repository); if there is none, ask the user. If vocabulary
exists, read it first (`cruxible orient --kind KIND`) and reuse it.

## 2. Decide what is state

Use the rubric in [Building team
knowledge](https://docs.cruxible.ai/building-team-knowledge/): typed state when
the system must do something with it (ask across items, enforce a vocabulary,
check links, coordinate writers, keep per-field history, drive `next` and
Lines); prose in a file when it's read and revised whole. The usual answer is
both: the prose in a document, and the few values extracted from it as Claims
citing it.

Sketch the model as a table (kind, how it's named, field, value type, one or
many, role, evidence) and get the user's confirmation before writing
anything. Apply the guide's habits: stable keys, value types chosen early,
edges for anything you'll ask about, the normative role for what the writer
states, no numbers in titles, no reserved field names.

## 3. Define the vocabulary

Follow [Evolving your
vocabulary](https://docs.cruxible.ai/evolving-your-vocabulary/): add a field
from `cruxible claim-type propose --template`, and a keyed kind with a
`subject_kind` member beside its key ClaimType. Give each field a
`description`, a `default_role`, and evidence rules that admit what will back
its values for that role ([Evidence and
admission](https://docs.cruxible.ai/evidence-and-admission/)).

<!-- update: first-run batch -->
Today a field definition must be complete (start from the template), a new
field stops at a proposal (`cruxible proposal approve PROPOSAL_ID`), and a key
ClaimType needs a `canonical_form`. Land one definition before proposing the
next, or put them in one change set, so none goes stale.

## 4. Seed the values

Write with the value verbs, one `write` per checkpoint, with a `because`
that says where the values came from:

```bash
cruxible write seed.yaml --dry-run      # every check, nothing written
cruxible write seed.yaml
```

Cite files where values came from them (`--evidence-file 'PATH#passage'`),
and write reasoning as a document section in the same `write` as the Claims
that cite it ([Documents and notes](https://docs.cruxible.ai/documents-and-notes/)).
Read each change's verdict: anything other than `supported` means the
evidence or the field's rules need fixing, not ignoring.

## 5. Add the questions and the pages

Save the questions people will ask as named queries ([Asking
questions](https://docs.cruxible.ai/asking-questions/)), and check each one
answers the real question:

```bash
cruxible authoring submit QUERY.yaml --dry-run
cruxible authoring submit QUERY.yaml
cruxible query --name NAME --param KEY=VALUE
```

For a table people read in a page, write block markers in a catalogued page
and render it from the query: `cruxible block repin FILE ID --query
QueryDefinition:NAME --render`. For prose only a person can write, write it
between the markers and `cruxible block repin FILE ID --claim CLM-…` for each
Claim it summarizes. `next` reports both when their state moves.

## 6. Verify and hand off

```bash
cruxible orient --kind KIND                 # what others will see
cruxible query KIND --select a,b            # values with flags
cruxible next                               # nothing unexpected
```

Then follow `../_shared/references/governance-flow.md` for the approval
policy, agent principals and the hand-off.
