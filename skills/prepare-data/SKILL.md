---
name: prepare-data
description: Profile and prepare raw source files before modeling or loading them into Cruxible; validate keys, grain, joins, and transformation needs, then produce a concrete readiness report.
---

# Prepare Data

Use this skill before `create-state`, and before `overlay-and-fit` whenever local source files need to be loaded into an overlay.

Cruxible validates, loads, and evaluates. File cleaning and transforms are external.

This skill is about source-data preparation only:

- profiling files
- validating keys and joins
- checking grain and cardinality
- identifying cleaning and transform work
- producing cleaned files or a preparation plan

It is not for cross-dataset matching or governed relationship design. Those happen later in the state-building flow.

Use your own tools freely here: Python, Polars, SQL, spreadsheets, or shell tooling. The goal is to hand the later skills source files that are understood, defensible, and ready.

## Workflow

## Phase 1: Inventory the files

For each file, identify:

- what the file appears to represent
- whether it looks like an entity source, deterministic relationship source, reference file, or unknown source
- what its likely row grain is
- what other files it seems to join to

Do not assume the target state shape is already known. This skill is part of discovering it.

## Phase 2: Profile each file

For every source file, inspect:

- row count
- columns
- dtypes
- null counts
- sample rows
- obvious schema inconsistencies across files of the same kind

Do not stop at one-line summaries. The point is to understand what later modeling and loading work would actually consume.

## Phase 3: Validate entity keys and relationship joins

For likely entity files, check:

- duplicate primary keys
- null or empty IDs
- whitespace, prefix garbage, or sentinel values in IDs
- whether the file grain is really one row per entity

For likely deterministic relationship files, check:

- source and target key columns exist
- foreign keys actually resolve against the referenced entity files
- duplicate `(from, to, relationship_type)` rows
- whether the file grain is really one row per relationship

Across files, check:

- shared join columns exist where expected
- values actually overlap
- types are compatible
- normalization work is needed before joins can succeed

## Phase 4: Identify cleaning and transform needs

Look for:

- junk rows
- placeholders and test records
- all-null or effectively empty rows
- repeated rows caused by a secondary dimension
- text normalization issues
- encoding issues
- embedded dates, IDs, or structured values worth extracting

Keep preparation scoped to what reliable downstream modeling and loading will actually need. Do not over-clean for its own sake.

## Phase 5: Summarize modeling implications

After the files are profiled and cleaned enough to reason about them, summarize what they imply:

- likely entity-like record types
- likely deterministic relationship sources
- likely join keys across files
- obvious ambiguous areas that will need user clarification later
- obvious column rename, normalization, or reshape requirements
- whether cross-dataset matching will likely be needed later

If a draft config or draft state model already exists, you may compare against it here as a consistency check. Do not treat that as the starting assumption for this skill.

## Phase 6: Produce the preparation result

End with a concrete result, not just observations.

## Output

Report:

- `readiness`: `ready` | `ready_with_warnings` | `blocked`
- `source_inventory`
- `blocking_issues`
- `warnings`
- `cleaned_files`
- `transform_lineage`
- `required_transforms`
- `join_or_key_risks`
- `loading_readiness_by_surface`
- `likely_modeling_implications`
- `open_questions`
- `recommended_next_step`

`source_inventory` should capture, for each file:

- file path
- guessed role
- row grain
- likely join keys
- whether it looks loader-ready, transform-needed, or unclear

`transform_lineage` should capture, for each cleaned file:

- source file
- transform summary
- columns renamed, dropped, or derived
- rows removed and why

`loading_readiness_by_surface` should separate:

- likely entity loading readiness
- likely deterministic relationship loading readiness
- likely later matching or governed need

`open_questions` should capture true source ambiguity that the agent should not guess past.

`recommended_next_step` should usually be one of:

- start `create-state`
- continue `overlay-and-fit`
- clean specific files first
- clarify ambiguous source semantics with the user before modeling further
