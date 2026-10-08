---
name: review-state
description: Review an existing Cruxible instance - its work queues, value verdicts and evidence, open proposals, stale blocks and citations, Procedures and Lines, and kits - and report prioritized findings with the exact repair before changing anything.
---

# Review State

Use this skill on an existing instance when the goal is diagnosis: what is
wrong, what is weak, and what should be fixed first.

This skill is for:

- a health check before a hand-off or a release
- finding weak, stale or contested values and their causes
- checking that pages, citations and automation still match accepted state
- telling the user what to fix next and with which command

Review first and report. Do not write, approve, or activate anything during the
review unless the user asks.

## Phase 1: Scope

Ask what the user is worried about (a wrong answer, stale pages, a stuck Line,
general health) and which queries or decisions matter most. Then read the map:

```bash
cruxible whoami
cruxible orient
```

`orient` shows every kind, artifact counts, Claims by status, attention from
the `next` queue, and Line enablements that stopped.

## Phase 2: The work queues

```bash
cruxible next                    # what is wrong or waiting, each row with its repair
cruxible audit                   # Claims ranked as worth verifying
cruxible curation list           # ontology-maintenance patterns detection found
cruxible proposal list --status open
```

- `next` status facets come first (instance, floor, ledger mirror, provider
  lane, compiler, Line dispatch, consumers, triggers): any facet that needs
  attention is a finding.
- Group `next` rows by reason: `citation_drifted`, `claim_uncovered`,
  `claim_stale_evidence`, `claim_conflicted`, `proposal_stale`,
  `projection_backing_stale`, `line_coverage_gap`, `consumer_stalled` and the
  rest. A row whose repair you cannot run still shows, with `repair_requires`
  naming the tool and tier.
- A stale proposal blocks nothing but will never activate; its author readmits
  or withdraws it.

## Phase 3: Values and evidence

For the kinds that matter, read values with their flags:

```bash
cruxible query KIND --select a,b --claims
cruxible query KIND --where 'status=open' --select owner
```

Flags (`stale`, `contested`, `contradicted`, `uncovered`, `unsure_hold`) are
findings. For each flagged value, read why:

```bash
cruxible get KIND/ID
cruxible get CLM-… --detail evidence
cruxible get CLM-… --detail why
cruxible get CLM-… --detail history
```

Ask, per finding: is the evidence missing, not admitted by the ClaimType's
rules, stale, or contradicted? Is the vocabulary wrong (a missing enum member,
a field that should be many-valued)? Is the value simply out of date?

Run the named queries that matter (`cruxible query --name NAME`, with
`--receipt full` for the Claims each row read) and check they answer the real
question.

## Phase 4: Files, pages and the floor

```bash
cruxible sources check           # catalogued files against accepted Documents
cruxible block sync --all        # projection blocks against their backings
cruxible coverage resolve --all  # which spans of catalogued files are governed
```

A drifted citation, a modified Document, or a stale block is a finding; so is a
catalogued file that should be cited and is not.

## Phase 5: Procedures, Lines and kits

When the instance automates work:

```bash
cruxible orient --section lines
cruxible get Line:NAME           # Triggers, enablements, pending work, recent runs
cruxible orient --section runs
cruxible procedure readings NAME # measurement standing, when measurements are declared
cruxible kit status              # edited definitions, kept divergences, newer releases
```

Look for Lines whose Triggers are inactive because the Line is not enabled,
enablements that stopped (and why), coverage gaps after a restart, failing
runs, and measurements that resolved contradicted.

## Phase 6: Report

Report findings in priority order, each as one row:

| Severity | Area | Finding | Evidence | Repair |
|---|---|---|---|---|

- **Evidence** names the exact references (`CLM-…`, `KIND/ID`, a proposal ID,
  `Line:NAME`) so anyone can `get` them.
- **Repair** is the exact command (`next` usually supplies it), or the design
  change it needs (a vocabulary migration, a new evidence rule, a source to
  catalogue).

Separate what is wrong now from what is risky, and say what you did not check.
