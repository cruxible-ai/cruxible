---
name: survey
description: Assess a project that has no Cruxible instance or vocabulary yet, report what in it is worth holding as shared state, then set up the instance and a first small vocabulary. Use before create-state when starting without a kit.
---

# Survey a project

A survey answers two questions before anything is written:

1. **What should later sessions read as accepted state instead of working out
   again?**
2. **How should that state stay aligned with reality as code, data, or external
   conditions move?**

Its result is a short assessment the user can agree to or change, and only then
an instance and a first small vocabulary (Gear 1). If `cruxible orient` already
answers with kinds and fields, the project has been surveyed: `create-state`
extends what exists, and `automate-state` wires reality checks.

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
copied into Claims. The [copy-in rule](references/copy-in.md)
decides the borderline cases. A small state that is read often is worth more
than a complete mirror nobody queries.

The assessment names, in the project's own words:

- **Kinds and fields:** the few kinds of thing that recur (what each one is and
  what identifies it across sessions) and the handful of fields per kind that
  carry the conclusions and changing facts above.
- **Normative vs. observation:** which fields are normative (decisions,
  hypotheses, baselines, acceptance criteria, or triage verdicts: human or agent
  authority backed by cited prose and hardened through review and signed
  attestation) and which facts come from reality (test or benchmark runs,
  experiments, APIs, scanners, telemetry, analyzers, or custom Python/ML
  models).
- **Keeping state aligned with reality (Gear 2 candidates):** which
  observations should eventually be fed by a Procedure and Line via
  `automate-state` (as simple or as complex as the domain needs), and which
  normative beliefs should pin `based_on` those observations so `cruxible next`
  wakes only what reality contradicts or shifts.
- **Boundaries:** the documents that stay documents, and what was left out on
  purpose.

On pass 1, keep setup fast and safe: a value read off one of the project's own
files starts as an ordinary field that cites the file line. Reserve the
observation-only captured evidence rule for fields a Procedure or Line actually
keeps current, and move a field there when that automation is added; otherwise
manual writes land `uncovered`. Where a published kit already models the domain
(including automated kits such as `security` or `iot-fleet`, or `project-state`
for tracking the project's own roadmap, decisions, and rulings), say so:
`adopt-kit` starts from it, and its vocabulary can be revised like any
definition (`cruxible kit add REFERENCE` previews a kit without writing
anything).

Setting up follows the user's agreement. `cruxible init` attaches the project
and installs the base kit under a single owner (`self_approval_allowed`) by
default. For independent review, `cruxible init --require-independent-approval` also
creates a second principal, `reviewer`, with its key in the per-user config
path (`--reviewer-key-dir DIR` to choose another, outside the repository), or
use `cruxible_init` with `reviewer_id="reviewer"` and
`require_independent_approval=true`: nothing its author proposed activates
until the reviewer, normally a separate agent session, approves it. Which one
is the user's choice; `cruxible next` shows what waits for whom. The first
vocabulary goes in `.cruxible/vocabulary.yaml` (`cruxible vocabulary export`,
`plan`, `apply`);
the bundled [vocabulary reference](references/evolving-your-vocabulary.md)
gives its format and what each later change costs. Start with what the
assessment named and no more: adding a field or a kind later costs nothing,
while narrowing one means re-authoring its Claims. Each field's description
says what its value means; a later session reads it with `cruxible get
ClaimType:NAME`, so a meaning left out of the description is lost.

Then seed the graph with a first 5-10 real, linked starter Subjects from the
project (for example, existing architecture decisions, key invariants, and
active tasks or components), citing the real Markdown sections
(`SectionEvidence`) or code lines (`FileEvidence`) they rest on and pinning
`based_on` (or Subject-valued links) where a normative belief depends on an
upstream Claim. Prefer `cruxible python` with the typed `World` SDK
(`w = cx.world()`, checked against `.cruxible/world.pyi` via
`cruxible python --check` and `dry_run=True`), or `cruxible write FILE` when
working shell-only. Read the seeded state back with `cruxible get` and
`cruxible orient` to confirm it landed cleanly. `create-state` covers writes,
evidence, and vocabulary changes from there;
[governance](references/governance-flow.md) covers proposals that wait for
review. Report the assessment as agreed, the accepted references and
coordinate, any proposal still awaiting approval, and invite the user to open
`cruxible console` (State graph) to inspect their live repo graph and test
editing one cited section or line followed by `cruxible next`.

Once the initial vocabulary is accepted, hand the reality-alignment candidates
to `automate-state` (or an automated kit via `adopt-kit`) to wire Procedures,
Lines, and providers, and add projection pages where summaries need to stay in
step.
