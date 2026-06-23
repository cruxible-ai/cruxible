---
name: create-state
description: Create a new Cruxible state from raw domain data through staged graph, workflow, query, and review-loop design.
---

# Create State

Use this skill when a user wants to turn new data into a usable Cruxible state.

If the source files are messy, run `prepare-data` first.

Work in stages. Do not try to design the graph, workflows, queries, and review loop all at once.

## Generated config views

Do not rely on YAML alone when drafting or reviewing a state. At each major
config checkpoint, generate canonical views from the config and use those
views as the review surface.

Use inline output for fast review when the agent harness can render Mermaid:

```bash
uv run cruxible config views --config config.yaml --view all
```

Also update durable README marker blocks so CLI and non-GUI harnesses can
inspect the same generated views:

```bash
uv run cruxible config views --config config.yaml --update-readme README.md
```

If the config uses `extends`, render the runtime composed view for
onboarding/review surfaces:

```bash
uv run cruxible config views --config config.yaml --runtime --view all
uv run cruxible config views --config config.yaml --runtime --update-readme README.md
```

Everything between `CRUXIBLE:BEGIN` / `CRUXIBLE:END` markers is code-owned
structural output from the config. Authored prose may explain intent,
tradeoffs, open questions, and operating guidance, but it must not replace or
contradict the generated structural view. Do not hand-author alternate Mermaid
diagrams when a generated view exists.

When iterating the config toward its final shape, explicitly direct the user to
the generated README views as the primary way to understand the current system.
The YAML remains the execution source of truth, but the README's generated
ontology, workflow, governed relationship, and query sections are the review
surface for human decisions. Before asking the user to approve graph shape,
workflow sequencing, governed boundaries, or query surfaces, update the README
marker blocks and point them to the relevant generated section.

## Phase 1: Understand the domain shape

Before writing config:

1. inspect the source files
2. identify entity types and likely primary keys
   - a primary key is the stable property that uniquely identifies one real entity across reloads and updates
   - prefer durable source IDs or external identifiers
   - avoid names, titles, or other mutable text unless there is no better identifier
   - if no good primary key exists, stop and design one before continuing
   - if a concept may need to be a future query or traversal surface, model it as its own entity instead of leaving it as a property
   - if users may need to start from it, fan out from it, relate other things to it, or review it independently over time, it is usually better modeled as an entity
3. identify deterministic relationships between entities that can be loaded directly from the source data
4. identify any obvious bad states that should remain invalid across future ingests, refreshes, and graph updates, and should later become constraints or quality checks
   - do not invent constraints just to fill the slot
   - if there are no clear durable invalid states yet, leave this empty for now
5. identify the major user-facing query and use-case categories the state should eventually support
6. summarize the structural model for user confirmation

Use a concrete summary:

- entity type
- likely primary key column
- key properties
- source file
- relationship name
- from -> to
- how it gets populated
- notes or ambiguities

Keep this phase focused on domain shape. Do not jump ahead to workflow design unless the user raises it.

## Write Step A: Write the base graph config

Write only the minimum needed for the base graph:

- `entity_types`
- `relationships`
- obvious `constraints`
- minimal `contracts` only if clearly required

Do not spend time on named queries, review loops, or advanced workflows yet.

After writing the base graph config, render at least the ontology view inline.
If the state has a README, add or update the ontology marker block there too.
Use this as the graph-shape checkpoint before moving on.

## Phase 2: Validate the base graph config

Use the real CLI to validate:

```bash
cruxible validate --config config.yaml
```

Then initialize the instance. If you are connected to a governed daemon (server mode), use:

```bash
cruxible init --config config.yaml
```

The daemon manages the instance directory. Do **not** pass `--root-dir .` or create a local `.cruxible/` directory when running in server mode — the daemon owns instance state.

If you are working locally without a daemon (developer mode only):

```bash
cruxible init --config config.yaml --root-dir .
```

At this stage, the goal is not to design or run the full operational loading path yet. The goal is to confirm that the base graph definition is coherent enough to proceed.

Stop on validation or init errors. Do not continue with a broken base config.

## Phase 3: Understand the operational workflows

Once the base graph shape is defined and validated, figure out how this state is built and maintained over time.

Start with the operating loop, not the config nouns. Ask:

1. where does new data come from?
2. what repeatable steps turn raw inputs into graph state?
3. which steps are deterministic and repeatable?
4. which steps require judgment, matching, or review?
5. what should be automatically committed versus proposed for review?
6. summarize the workflow plan for user confirmation

Keep this phase focused on operations. Ask only the workflow questions needed to understand refresh cadence, automatic rebuilds, proposal-vs-direct-apply boundaries, and where review is required.

Then translate those answers into Cruxible terms:

- `artifacts`: input files, bundles, or external data sources the workflow depends on
- `providers`: reusable logic, model calls, or external processing steps
- `contracts`: structured workflow input and output shapes
- `workflows`: repeatable procedures that build, refresh, or propose graph state
- `canonical` workflows: workflows whose results are written directly into state instead of first becoming reviewable proposals. Use this only for deterministic or otherwise highly trusted operations that are safe to commit without a proposal/review step.

At the end of this phase, separate the workflow plan into two buckets:

- canonical workflows to design now
- judgment-based workflows that should become reviewable proposals later

For the later judgment-based workflows, define the task and the expected input/output shape now, but defer detailed provider and proposal-workflow design until after the graph and query surfaces are clearer.

## Write Step B: Add workflow machinery

Fully design the canonical workflow path now. Extend the config with:

- `artifacts`
- `contracts`
- `providers` needed for deterministic or otherwise trusted steps
- deterministic `workflows`

For judgment-based tasks that will need model judgment, matching, ranking, or review:

- add only the base `contracts` needed to describe their task input and output shapes
- do not build the provider-backed proposal workflows yet
- do not mark these tasks `canonical`

Do not introduce Cruxible workflow machinery just because the schema allows it.

After adding workflow machinery, render the workflow pipeline and workflow
summary inline. If the state has a README, update the generated workflow marker
blocks there too. The user should be able to review the sequence, each stage's
input context, the produced state or proposal, and provider provenance without
mentally parsing the YAML.

## Phase 4: Build and inspect the state for the first time

After workflow config changes:

```bash
cruxible validate --config config.yaml
cruxible config reload --config config.yaml
cruxible lock
```

Use `cruxible plan --workflow <workflow_name>` when you need to inspect the compiled workflow before running it.

Then run every canonical build or refresh workflow you defined in Step B, in dependency order:

```bash
cruxible run --workflow <workflow_name> --apply
```

This is the first real population step. Use it to build or refresh the state through the full canonical workflow path you designed in Step B.

Do not run judgment-based or proposal workflows in this phase. This phase is only for the workflows that are safe to apply directly to the state.

Re-check the state with:

```bash
cruxible stats
cruxible sample --type <EntityType> --limit 5
cruxible inspect entity --type <EntityType> --id <entity_id>
```

## Shared Governance Flow

After Phase 4, read and follow:

- `../_shared/references/governance-flow.md`

That shared reference is the source of truth for the remaining flow after the canonical layer is in place. Some phases inside it are conditional, but the reference itself is not optional once you move past Phase 4.

When following it from `create-state`, use Phases 1-4 of this skill as the earlier loopback points for graph shape, canonical workflow design, and canonical state build questions.

After adding governed relationships, named queries, feedback profiles, outcome
profiles, or decision policies, rerender the full default config view bundle
inline and update README marker blocks if present. Use the ontology, workflow
summary, governed relationship table, query map, and query catalog together as
the final structural review surface before hand-off.
