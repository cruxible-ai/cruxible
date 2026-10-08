---
name: prepare-data
description: Profile and prepare raw source files before modeling them in Cruxible; check identifiers, grain, joins and cleaning needs, decide what becomes typed values and what stays cited prose, and produce a concrete readiness report.
---

# Prepare Data

Use this skill before `create-state`, or before seeding values into an
instance a kit set up (`adopt-kit`), whenever local source files need to be
understood first.

Cruxible validates and governs what is written to it; cleaning and transforms
happen outside it.

This skill is for:

- profiling files
- validating identifiers and joins
- checking grain and cardinality
- identifying cleaning and transform work
- producing cleaned files, or a preparation plan, and a mapping onto Subjects and fields

It is not for designing the vocabulary itself; that happens in `create-state`.

Use your own tools freely here: Python, Polars, SQL, spreadsheets, or shell
tools. The goal is to hand the next skill files that are understood,
defensible, and ready to load.

## Phase 1: Inventory the files

For each file, identify:

- what it appears to represent;
- whether it looks like a list of things (a likely Subject kind), a list of
  links between things (a likely Subject-valued field), reference data, prose
  that should stay a file and be cited, or something unclear;
- its likely row grain;
- what other files it seems to join to.

Do not assume the target model is already known. This skill is part of
discovering it.

## Phase 2: Profile each file

For every file, inspect row count, columns, types, null counts, sample rows, and
schema inconsistencies across files of the same kind. Do not stop at one-line
summaries: the point is to understand what loading would actually consume.

## Phase 3: Validate identifiers and joins

For files that list things:

- duplicate identifiers;
- null or empty identifiers;
- whitespace, prefix garbage or sentinel values in identifiers;
- whether the grain really is one row per thing.

Cruxible addresses a Subject as `kind/id`, so the identifier must be stable for
the real thing across reloads. Prefer a source system's durable identifier; do
not use a mutable name or title. If there is no good identifier, stop and design
one.

For files that link things:

- both ends' identifier columns exist;
- each end actually resolves against the file that lists it;
- duplicate links;
- whether the grain is one row per link.

Across files: shared join columns exist, values overlap, types are compatible,
and normalization needed before joins succeed.

## Phase 4: Identify cleaning and transform needs

Look for junk rows, placeholders and test records, empty rows, repeated rows
caused by a secondary dimension, text normalization and encoding problems, and
embedded dates, identifiers or structured values worth extracting.

Check values against what a field will accept: a column that should become an
enum needs its distinct values listed and normalized; a numeric or date column
needs a consistent format.

Keep preparation scoped to what reliable loading needs. Do not over-clean.

## Phase 5: Map onto Subjects and fields

Summarize what the files imply, as a draft for `create-state` to confirm:

- likely Subject kinds and their identifier columns;
- likely fields per kind, with value type (text, enum, number, date, a link to
  another kind), and whether a Subject holds one value or many;
- which files or columns are prose to keep in files and cite (`--evidence-file`)
  rather than store as values;
- where each value's evidence will come from: the loader's own statement, a
  catalogued file, or a retained capture;
- ambiguities that need the user.

If vocabulary already exists in the instance (`cruxible orient`, `cruxible
orient --kind KIND`), compare against it and note mismatches.

## Output

Report:

- `readiness`: `ready` | `ready_with_warnings` | `blocked`
- `source_inventory`: per file, its path, role, row grain, identifier and join columns, and whether it is load-ready, needs transforms, or is unclear
- `blocking_issues` and `warnings`
- `cleaned_files` and `transform_lineage`: per cleaned file, its source, the transform, columns renamed, dropped or derived, and rows removed and why
- `subject_and_field_mapping`: the draft from Phase 5
- `files_to_cite`: files that stay prose, with a proposed catalog name each
- `open_questions`: real source ambiguity not to guess past
- `recommended_next_step`: usually `create-state`, `adopt-kit`, cleaning specific files first, or clarifying source semantics with the user
