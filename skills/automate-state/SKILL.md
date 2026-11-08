---
name: automate-state
description: Author or adapt requested Cruxible Procedures, Lines, Triggers, Mandates and Runbooks for repeatable work.
---

# Automate state

Procedures describe deterministic work over accepted state and contracted
provider results. A Line binds a Procedure to local parameters, provider
builds and an authority ceiling. Triggers describe events or schedules.
Mandates state the actual grant, scope and validity interval. A Runbook
orders Line runs and actions; it does not widen their authority.

Read `cruxible orient` and the relevant definitions before creating another
automation. ClaimType descriptions and governed Documents explain meanings;
the plugin does not prescribe which sources to inspect or which domain
conclusions to draw. Existing kit Procedures and templates may already express
the required computation.

A kit's manifest-named automation template is inert. Tailor sources, local
names, timing and grants, submit it as authoring, and obtain a second agent's
review and approval. Once accepted, `cruxible runbook preview <start>` exposes
reads, writes, unavailable steps, ordering problems and what would settle or
go to review. `cruxible runbook run <start>` executes when authorized. A step
that stops for review leaves a proposal to resolve before an explicit rerun;
do not loop or self-approve to force completion.

A manual Line run and enabling scheduled execution are distinct decisions.
Enabling consents to its current definition and Triggers; changed definitions
can require renewed enablement. Inspect the accepted Mandate rather than
assuming everything must propose or that every deterministic derivation may
settle. Judgments remain subject to their accepted review rules.

The served authoring examples, contracts and refusals define legal syntax;
consult [automation documentation](https://docs.cruxible.ai/lines-and-automation/)
for a specific construct. Use the [copy-in rule](../_shared/references/copy-in.md)
for mirrored data, [guarded edits](../_shared/references/concurrent-edits.md)
for shared changes, and [governance](../_shared/references/governance-flow.md)
for proposals and acceptance.
