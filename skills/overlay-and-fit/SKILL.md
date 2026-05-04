---
name: overlay-and-fit
description: Create an overlay from a published reference world, use the inherited world plus any applied kit with the lowest-friction path, and only add local config or code when the current overlay is not enough.
---

# Overlay And Fit

Use this skill when the user wants to start from a published reference world and fit it to a local use case with the least friction.

This is the reference-world path, not the greenfield path. Start from the inherited world, use the applied `kit` if one exists, and only add local config or code when the current overlay is not enough.

If you are not sure whether the current workspace is already an overlay, check first:

```bash
cruxible world status
```

If it reports upstream tracking metadata, continue in the current workspace.

If it says the instance is not tracking an upstream published world, create an overlay first:

```bash
cruxible world create-overlay --world-ref <alias> --root-dir <root_dir>
```

Here, `<root_dir>` is the workspace root directory that will contain the local `config.yaml` and the `.cruxible/` instance directory. It is not the `.cruxible/` directory itself.

If that world has a configured default `kit`, its local files will be copied into the workspace automatically. This usually means the overlay-specific `config.yaml` plus any companion workspace files that kit needs, such as local provider code, artifacts or seed data, helper files, or other local starting material. The exact kit shape may vary. Use `--kit <kit>` to override the default `kit`, or `--no-kit` for a bare overlay.

## Cruxible Terms

- `canonical` behavior means deterministic graph-building or refresh behavior whose results can be written directly into world state
- `governed` behavior means judgment-based behavior that should go through proposal, review, and resolution before it becomes durable world state
- `proposal workflows` are the non-canonical `workflows` that produce reviewable candidates or groups instead of writing directly into world state
- `named_queries` are the user-facing query entry points the world exposes
- `providers` are the code or model-backed steps that `workflows` call

## Core Rules

- edit the local `config.yaml`, not `.cruxible/upstream/current/config.yaml`
- treat `.cruxible/composed/config.yaml` as generated output, not as the source of truth
- use the inherited world and the applied `kit` as-is if they already solve the problem
- add local extensions instead of re-declaring inherited config or graph structure from the reference world
- prefer refining the local `kit` pattern over inventing new local machinery
- if a desired change really belongs in the inherited config, call it out as upstream work
- keep the local fit as small as the use case allows

## Phase 1: Establish the overlay baseline

Start by inspecting the overlay workspace:

```bash
cruxible world status
cruxible stats
cruxible sample --type <EntityType> --limit 5
cruxible inspect entity --type <EntityType> --id <entity_id>
```

Inspect the three config layers:

- local config: `config.yaml`
- inherited config snapshot: `.cruxible/upstream/current/config.yaml`
- active composed runtime config: `.cruxible/composed/config.yaml`

If a `kit` was applied, inspect the local files it brought in too.

Then answer:

1. what user problem are we solving in this overlay?
2. what does the inherited world already provide?
3. what does the applied `kit` already provide?
4. what `named_queries` or downstream `workflows` does this overlay need to support for the user?
5. does the current inherited world plus local files already have a clean path for those surfaces?
6. can the current overlay already handle this use case without local changes?
7. if not, what is actually missing?
8. what should stay local instead of being pushed upstream?

Keep this phase grounded in the current overlay. Do not redesign the inherited world from scratch.

## Phase 2: Choose the fit strategy

Decide whether to use the overlay as-is or make the smallest local fit:

1. can the inherited world, applied `kit`, and existing `workflows` handle the user's data or workflow as-is?
2. if yes, which existing `workflows`, proposal flows, or `named_queries` should be used?
3. if no, is the gap missing local graph structure, missing local provider or workflow machinery, missing local `named_queries`, or some combination?
4. does the problem require new local canonical behavior, new governed behavior, new local queries, or none of those?
5. what is the smallest set of local changes needed to support those goals?
6. summarize the chosen path for user confirmation

If the answer is "use the current overlay as-is," continue to Phase 3 and do not edit `config.yaml` yet.

If the answer is "the overlay needs local changes," continue to Phase 4.

## Phase 3: Run the current overlay with no local changes

If the current overlay is already good enough, use it directly.

Do not edit `config.yaml` in this phase.

Instead:

1. identify which existing canonical `workflows` should be run for the user's data
2. identify which existing governed `workflows` or review surfaces should be used
3. identify which existing `named_queries` already answer the user's questions
4. run the current setup and inspect the results

Use the existing CLI surfaces that match the current overlay:

```bash
cruxible run --workflow <workflow_name> --apply
cruxible propose --workflow <workflow_name>
cruxible query --query <query_name> --param key=value
cruxible explain --receipt <receipt_id>
```

If the current overlay and `kit` solve the problem, stop here and hand off the working flow.

If the current setup is not enough, continue to Phase 4 and make the smallest necessary local change.

## Phase 4: Define the local fit boundary

Start with the local operating loop, not just the config nouns. Decide what the local config actually needs to add:

1. which local entities or relationships are truly missing?
2. what repeatable local steps should turn local inputs into local graph state?
3. which of those steps are deterministic and repeatable enough to become local `canonical` `workflows`?
4. which of those steps require judgment, matching, or review and should become later `proposal workflows` instead?
5. which local review or governed surfaces are actually needed?
6. which local `named_queries` are actually needed?
7. what can be solved by refining the applied `kit` or local config instead of inventing new machinery?
8. summarize the local fit boundary for user confirmation

Keep this phase ownership-focused. If a change would require inherited entries or semantics to change, call that out as upstream work instead of forcing it into the local config.

At the end of this phase, separate the local workflow plan into two buckets:

- local `canonical` `workflows` to implement and run in Phase 5
- judgment-based local `proposal workflows` to design later in Phases 6-8

## Write Step A: Update the local config

Edit only the local `config.yaml`.

Use the applied `kit` as a starting point where possible. Add only the local pieces that are actually needed:

- `entity_types`
- `relationships`
- `artifacts`
- `contracts`
- `integrations`
- `providers`
- `workflows`
- `named_queries`
- `feedback_profiles`
- `outcome_profiles`
- appended `constraints`, `quality_checks`, `decision_policies`, or `tests`

Use local names for new entries so they do not collide with inherited names.

In this step, fully add the local graph structure and local `canonical` workflow machinery needed for Phase 5.

For later judgment-based tasks that should become `proposal workflows`, only add the base local structure you already know you need, such as supporting graph elements or base `contracts`. Do not fully design the provider-backed proposal flow here; that belongs in Phases 6-7.

Do not copy large chunks of inherited config into the local file. Reuse inherited structure and `kit` patterns instead.

## Phase 5: Validate and run the local canonical fit

After local config changes:

```bash
cruxible validate --config config.yaml
cruxible reload-config --config config.yaml
cruxible lock
```

Run only the local canonical `workflows` you added through the local config:

```bash
cruxible run --workflow <workflow_name> --apply
```

Do not try to rebuild inherited state here. The overlay should add local state and local behavior on top of what is already inherited.

Re-check the world:

```bash
cruxible stats
cruxible sample --type <EntityType> --limit 5
cruxible inspect entity --type <EntityType> --id <entity_id>
```

If the inherited world plus the applied `kit` now solve the problem, stop adding machinery.

## Shared Governance Flow

After Phase 5, read and follow:

- `../_shared/references/governance-flow.md`

That shared reference is the source of truth for the remaining flow after the local canonical fit. Some phases inside it are conditional, so if the inherited world plus the selected `kit` already solve the problem you may skip the unnecessary add-more-machinery phases there. But the shared reference itself is not optional once you move past Phase 5.

When following it from `overlay-and-fit`:

- use Phases 1-5 of this skill as the earlier loopback points for local fit boundary, local canonical config, and local canonical build questions
- prefer inherited and `kit` surfaces before adding local ones
- add only the smallest local fit needed to solve the current problem
- if a change really belongs upstream, call it out instead of forcing it into the overlay

Before doing the final handoff phase in `governance-flow.md`, return here for the overlay-specific final phase below.

## Phase 6: Check future upstream pull compatibility

Before the final handoff, confirm that the local config still stays compatible with future upstream pulls:

```bash
cruxible world status
cruxible world pull-preview
```

Inspect warnings, compatibility, and conflicts.

If the local config does not compose cleanly with the upstream preview:

- simplify the local fit
- move the conflicting change into upstream work
- or clearly document the pull risk before handoff

Only apply the pull if the user wants to test it directly:

```bash
cruxible world pull-apply --apply-digest <digest>
```

When you return to the final handoff phase in `governance-flow.md`, also clearly distinguish:

- what remains inherited
- what came from the selected `kit`
- what was added locally in this overlay
- the current `world status` / `pull-preview` result
- what should stay local versus what should be proposed upstream later
