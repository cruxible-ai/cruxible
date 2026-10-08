---
name: create-state
description: Model a new domain in Cruxible - decide what is typed state and what stays prose, define Subject kinds and ClaimTypes, catalogue sources, seed values with evidence, and add named queries and projection blocks - in reviewed stages.
---

# Create State

Use this skill when a user wants a domain's knowledge in Cruxible: typed,
governed values that people and agents share, each with evidence and history.
You are building the vocabulary and the first accepted state, so every stage
below ends in something the user can review.

This skill is for:

- turning a domain (a team's work, a security inventory, a set of decisions) into Subjects and ClaimTypes
- choosing what to keep as prose in files and cite, rather than store as values
- seeding the first values with evidence that `next` can keep honest
- adding the named queries and rendered tables people will read

If the inputs are messy files, run `prepare-data` first. If a kit already
models the domain, use `adopt-kit` instead and come back here only for what the
kit lacks.

Work in stages. Do not define vocabulary, load values and build views in one
pass.

## Phase 0: Connect and orient

```bash
cruxible whoami        # who you are on this instance, and whether you can author
cruxible orient        # kinds, artifacts, attention, next commands
```

On MCP, `cruxible_whoami` and `cruxible_orient`. Creating an instance is
operator work (`cruxible server start`, then `cruxible init` in the
repository); if there is none, ask the user to run it. If `whoami` says you
cannot author, stop and surface its repair.

If the instance already holds vocabulary, read it before adding any:
`cruxible orient --kind KIND` for each kind, and `cruxible orient --section
claim_types`. Reuse what exists.

## Phase 1: Decide what is state

For each kind of information the user has, decide where it lives.

Make it **typed state** when the system must do something with it: ask across
items (filter, count, sort), enforce a vocabulary (an enum, a link that must
resolve), check consistency across links, coordinate concurrent writers, keep
per-field history, or drive behavior (`next` rows, Lines, stale blocks).

Keep it **prose in a file** when it is read and revised whole: rationale,
trade-offs, narrative, guides, meeting notes, anything that would need an
invented schema only to store text.

The usual answer is a **hybrid**: the prose stays in a Markdown file, and a few
typed values are extracted from it with a citation of the passage. When the
prose changes, `next` reports the citation as drifted.

Then sketch the model:

- **Subject kinds**: dotted names (`project.task`, `sec.package`). A kind is
  worth having when people start from it, link to it, or review it on its own.
- **IDs**: stable for the real thing (a source system's identifier, a slug),
  never a mutable title.
- **Fields**, one ClaimType each: the predicate (`project.task.status`), the
  value (a literal with a JSON schema, an `enum` for a closed set, a reference
  to another Subject, or exact content), one value or many, the roles writers
  use (`observation` for what was seen, `normative` for what should be), and
  the evidence that should support a value.
- **Sources**: which files will be cited, and under what catalog name.

Present the model as a table (kind, ID rule, field, value type, cardinality,
evidence) and get the user's confirmation before writing anything.

## Phase 2: Define the vocabulary

Start from the template and preview before proposing:

```bash
cruxible claim-type propose --template            # a complete example input
cruxible claim-type propose --name add-task-status --input - --dry-run <<'EOF'
{ ...one ClaimType... }
EOF
```

Each ClaimType input names `predicate`, `allowed_subject_kinds`,
`object_kind`, `literal_schema` (for literals), `cardinality`,
`permitted_roles`, `evidence_admission_policy`, `admission_policy` and
`resolution_policy`. Also give:

- `description` (and `member_descriptions` for an enum), so `orient` and other
  agents know what the field means;
- `default_role`, so writers need not pass `--role`;
- evidence rules naming what supports a value: the writer's own words are
  `CaptureContract:playbill.coordinator-self-source-v1`; a catalogued file
  named `NAME` is `CaptureContract:playbill.foreign-source.NAME`, listed in
  `anticipated_source_ids` until something cites it.

Propose one ClaimType, then activate it before proposing the next: a proposal
is checked against the state it was proposed on, so one made before another
was activated goes stale and needs `cruxible proposal readmit`. Under the
default self-approval policy:

```bash
cruxible proposal list --status open
cruxible get PROPOSAL_ID                  # status, changes, refusal diagnostics
cruxible proposal activate PROPOSAL_ID
```

When the instance requires independent approval, follow
`../_shared/references/governance-flow.md`.

Changing a ClaimType later disposes the Claims that use it, so it goes through
`cruxible claim-type migrate`, never a quiet redefinition. Get the vocabulary
reviewed now.

## Phase 3: Catalogue the sources

List each file you will cite in `.cruxible/sources.yaml`:

```yaml
catalog_kind: portable
entries:
  - name: standup
    locator: notes/standup.md
```

A `name` and a `locator` are enough to cite a file. Make an entry a Document
(add `document_id`, `document_kind`, `title`, `media_type`,
`governance_scope`) only when the file's exact wording is itself governed, such
as a policy; then `cruxible sources propose --source NAME --name PROPOSAL`
proposes each revision.

## Phase 4: Seed the values

Write values with the value verbs. Preview a batch first, then write it:

```bash
cruxible write - --dry-run <<'EOF'
{"because": "Initial load from the planning spreadsheet.",
 "changes": [
   {"op": "set", "subject": "project.task/ship-v1", "field": "owner", "value": "grace"},
   {"op": "set", "subject": "project.task/ship-v1", "field": "status", "value": "open"}
 ]}
EOF
```

- `set` replaces a single-value field and adds a missing Subject of a known
  kind; `add` appends to a many-valued field; `retire` ends a value.
- Cite files where values came from them:
  `cruxible set KIND/ID FIELD VALUE --because "..." --evidence-file "PATH#ANCHOR"`
  (or a `file` evidence entry in a `write` change).
- Read each change's verdict. A value written with evidence the ClaimType does
  not admit is accepted as `uncovered` with a warning; fix the evidence rule or
  the citation rather than ignoring it.
- `cruxible write --schema` prints the payload schema. On MCP, `cruxible_set`
  and `cruxible_write`.

## Phase 5: Queries and views

Add the named queries the user will ask repeatedly. Start from a template and
check before submitting:

```bash
cruxible authoring example query-claims-by-type
cruxible authoring submit QUERY.json --dry-run
cruxible authoring submit QUERY.json --and-activate
cruxible query --name NAME
```

For a table or list people read in a page, use a rendered projection block:
write `<!-- cruxible:block:ID -->` and `<!-- /cruxible:block:ID -->` in a
catalogued page, then `cruxible block repin SOURCE ID --query
QueryDefinition:NAME --render`. For a summary only prose can give, write the
prose between the markers and repin with `--claim CLM-…` for each Claim it
summarizes. Both are reported by `next` when their backing state moves.

## Phase 6: Verify and hand off

```bash
cruxible orient --kind KIND                 # fields and counts as others will see them
cruxible query KIND --select a,b --claims   # values with flags
cruxible get KIND/ID --detail why           # evidence and provenance of one value
cruxible next                               # should hold nothing unexpected
cruxible stub --out world.pyi               # if the user works in Python
```

Then follow `../_shared/references/governance-flow.md` for the approval
policy, agent principals and the final handoff.
