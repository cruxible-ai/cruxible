---
name: create-state
description: Define or extend Cruxible vocabulary and evidence-backed values for a user's domain, including preparing data that needs to become shared state.
---

# Create state

Cruxible holds typed Claims about Subjects, with evidence and accepted history.
Begin with `cruxible orient` for the small index (`cruxible get
ClaimType:NAME` gives a field's description); existing named queries and governed Documents may already
describe the needed state. An available kit can supply vocabulary;
`adopt-kit` handles its installation. Before re-deriving a conclusion, check
whether an accepted Claim already answers it; cite that Claim instead of
recomputing, and check `next` for what changed since it was accepted.

A ClaimType says what a Claim means: its applicable Subject kinds, value
type, cardinality, role and evidence rules. Its description explains that
meaning, not a recipe for an agent's work. Keys identify the same thing across
observations. A relationship with its own facts can be a pair Subject keyed
by Subject-valued ends; those ends are immutable. Prefer existing kinds and
fields when they express the intended meaning.

The instance's kinds and fields are stated in one file,
`.cruxible/vocabulary.yaml`: `cruxible vocabulary export` writes it from
accepted state, `plan` shows what an edit would change (including the Claims
an enum mapping moves) and `apply` submits it as one change set. A kind or
field the file leaves out is untouched. `next` says when the file is behind
accepted state. Kit vocabulary is a starting point, not a fixed schema: revise
it like any definition (allow more enum values, add a field, split a
category); new vocabulary goes where it fits, in a local namespace or beside
the kit's own types. A kit upgrade names your local edits and replaces them
unless you keep them. The bundled
[vocabulary reference](../../docs/evolving-your-vocabulary.md) covers the
file's format, its value types and what each kind of change costs.

Apply the [copy-in rule](../_shared/references/copy-in.md) before turning source
material into Claims. A governed Document can point to a source instead.
Documents have `about` and `guides` links and backlinks; prose and its citations
can remain evidence without becoming a mirrored database; see the bundled
[documents reference](../../docs/documents-and-notes.md). When copying is
justified, the [data-readiness reference](../_shared/references/data-readiness.md)
helps resolve grain, identifiers and joins without requiring a profiling
project for every input.

Kinds and fields use the vocabulary file; other definitions use authoring;
values use `set`, `retire` or `write`. A `write` (SDK `cx.changes(because=...)`)
can create the Subjects it is about in the same change set: a `create` change
whose `key` is the kind's one key value or `{field: value}` (SDK
`batch.create(kind, key=...)` or a `cx.new_subject(...)` draft, usable as
`subject=` and as a value). File passages, sections, Captures, contracts and
the writer's own words provide evidence through the supported write surface.
A workspace file is cited by its path; a file the catalog does not name yet is
catalogued by the write that cites it. A quoted code line (`--evidence-file
PATH#TEXT`) is a pointer for reviewers, not a witness that resurfaces the
judgment; a judgment that depends on code cites a check result where one exists
(a scanner's finding), and prose keeps quote and section citations. Evidence belongs to each Claim: a decision and the values that
rest on its reasoning (its date included) cite the section written in the
same change. Preserve verdict flags instead of treating every stored value as
supported.

Use [guarded edits](../_shared/references/concurrent-edits.md) and
[governance](../_shared/references/governance-flow.md) for shared or reviewed
changes. Report the accepted references and coordinate, or the specific proposal
still awaiting acceptance. Pages and automation are additions only when the
user's intended outcome needs them.
