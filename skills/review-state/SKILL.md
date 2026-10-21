---
name: review-state
description: Review an existing Cruxible instance - its work queues, value verdicts and evidence, open proposals, stale pages and citations, Lines, and kits - and report prioritized findings with the exact repair before changing anything.
---

# Review State

Use this skill on an existing instance when the goal is diagnosis: what is
wrong, what is weak, and what should be fixed first. Review first and report.
Don't write, approve, or activate anything unless the user asks.

The guides explain each signal (https://docs.cruxible.ai, or `docs/` in the
Cruxible repository); this skill is the order to check them in.

## 1. Scope

Ask what the user is worried about (a wrong answer, stale pages, a stuck Line,
general health) and which questions matter most. Then read the map:

```bash
cruxible whoami
cruxible orient
```

## 2. The work queues

```bash
cruxible next                          # what is wrong or waiting, with repairs
cruxible proposal list --status open
cruxible audit                         # Claims most worth verifying
```

Group `next` by family: drift, freshness, disagreement, weak evidence, pages,
proposals, automation, upgrades ([Keeping knowledge
current](https://docs.cruxible.ai/keeping-knowledge-current/)). A status line
above the rows (compiler, floor, mirror, Line enablements) is a finding too.

## 3. Values and evidence

For the kinds that matter, read values with their flags, and for each flagged
value, why:

```bash
cruxible query KIND --select a,b
cruxible get CLM-… --detail evidence
cruxible get CLM-… --detail history
```

Per finding, decide: is the evidence missing, not admitted for the Claim's
role, stale, or contradicted? Is the vocabulary wrong? Is the value just out
of date? ([Evidence and admission](https://docs.cruxible.ai/evidence-and-admission/)
explains verdicts and flags.) Run the saved queries that matter and check they
answer the real question.

## 4. Pages and citations

Stale blocks and drifted citations show in `next`
(`projection_backing_stale`, `projection_dirty`, `citation_drifted`,
`document_modified`). A page that people rely on and that is behind its state
is a high-severity finding; read the Claims behind it with `get`
([Documents and notes](https://docs.cruxible.ai/documents-and-notes/)). Before
judging a file, `cruxible coverage resolve --file PATH` says which Claims rest
on it.

## 5. Lines and kits

```bash
cruxible orient --section lines
cruxible get Line:NAME                 # Triggers, enablements, recent runs
cruxible procedure readings NAME       # when measurements are declared
cruxible kit status                    # edited definitions, kept divergences, newer releases
```

Look for Lines that aren't enabled, enablements that stopped and why, gaps
after a restart, unread inbox items, mandates near expiry, and measurements
that resolved against the Procedure ([Lines and
automation](https://docs.cruxible.ai/lines-and-automation/)).

## 6. Report

Report findings in priority order, one row each:

| Severity | Area | Finding | Evidence | Repair |
|---|---|---|---|---|

- **Evidence** names exact references (`CLM-…`, `KIND/ID`, a proposal ID,
  `Line:NAME`) so anyone can `get` them.
- **Repair** is the exact command (`next` usually supplies it), or the design
  change it needs.

Separate what is wrong now from what is risky, say which empty answers mean
"none" and which mean "unknown", and say what you didn't check.
