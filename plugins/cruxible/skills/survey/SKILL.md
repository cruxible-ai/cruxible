---
name: survey
description: Assess a project that has no Cruxible instance or vocabulary yet, report what in it is worth holding as shared state, then set up the instance and a first small vocabulary. Use before create-state when starting without a kit.
---

# Survey a project

A survey answers one question before anything is written: what in this
project should later sessions be able to read as accepted state instead of
working out again? Its result is a short assessment the user can agree to or
change, and only then an instance and a first vocabulary. If `cruxible orient`
already answers with kinds and fields, the project has been surveyed:
`create-state` extends what exists.

Read the project the way a returning agent would: its notes, decision
records, issue lists, configuration and the documents people consult to
remember what was settled. Three things mark material worth holding as state:

- a conclusion that took work to reach and that later sessions would
  otherwise derive again (a verdict, a ruling, a triage result, a mapping);
- a fact that changes, where the current value and its earlier values both
  matter (a status, an owner, a due date);
- a fact other work depends on, so that a change to it should be noticed.

Everything else stays where it is. Prose, source files and reference
material are cited as evidence or linked as governed Documents; they are not
copied into Claims. The [copy-in rule](../_shared/references/copy-in.md)
decides the borderline cases. A small state that is read often is worth more
than a complete mirror nobody queries.

The assessment names, in the project's own words: the few kinds of thing that
recur (what each one is and what identifies it across sessions), the handful
of fields per kind that carry the conclusions and changing facts above, which
fields are judgments (reviewed, cited) and which are observations of a
source, the documents that stay documents, and what was left out on purpose.
Where a published kit already models the domain, say so: `adopt-kit` starts
from it, and its vocabulary can be revised like any definition. State whose
job is tracking the project's own work (roadmap items, decisions, rulings)
is what the project-state kit models.

Setting up follows the user's agreement. `cruxible init` attaches the project
and proposes the base kit. The default sets up a second, reviewing principal
(`cruxible init --principal-id owner --reviewer-key-dir DIR
--require-independent-approval`, the key directory outside the repository):
nothing its author proposed activates until the reviewer, normally a separate
agent session, approves it. `cruxible init --principal-id owner` alone lets
one owner approve everything. Which one is the user's choice; `cruxible next`
shows what waits for whom. The first vocabulary goes in
`.cruxible/vocabulary.yaml` (`cruxible vocabulary export`, `plan`, `apply`);
the bundled [vocabulary reference](../../docs/evolving-your-vocabulary.md)
gives its format and what each later change costs. Start with what the
assessment named and no more: adding a field or a kind later costs nothing,
while narrowing one means re-authoring its Claims.

Then record a first few real values, each citing the passage or file it came
from, and read them back with `cruxible get` and `cruxible orient`.
`create-state` covers writes, evidence and vocabulary changes from there;
[governance](../_shared/references/governance-flow.md) covers proposals that
wait for review. Report the assessment as agreed, the accepted references and
coordinate, and any proposal still awaiting approval. Automation and pages
are later additions, when the user's intended outcome needs them.
