---
name: adopt-kit
description: Install or upgrade a Cruxible kit and adopt its local automation when the user wants an existing domain model.
---

# Adopt a kit

A kit carries definitions and provider packages. Claims, principals, approval
policy and local automation belong to the instance. Read `cruxible orient`
and `cruxible kit status` to identify what exists. ClaimType descriptions
explain each field's meaning; governed Documents linked by `guides` carry
domain guidance. Reuse those meanings rather than adding a parallel model.

A registry version names a release; its digest identifies the bytes. `cruxible
kit pull NAME:VERSION --out DIRECTORY` fetches and verifies without installing.
`cruxible kit add REFERENCE` previews the change. Its guarded commit uses
`--commit --at OID` with the preview's coordinate. Provider installation
needs admin authority. A blocked or awaiting-providers result is unfinished,
not an installed kit; report its served repair.

The manifest's `automation_template` names inert local automation, not an
assumed examples path. Starting it has two parts: tailor the template's
sources, names, timing and grants, then submit and obtain a second agent's
review and approval; after acceptance, inspect `cruxible runbook preview
<start>` and run `cruxible runbook run <start>` when authorized. A review stop
leaves a proposal, not permission to approve it yourself. Enabling scheduled
Lines is separate consent.

Upgrades expose local divergences and dependent definitions. Preserve or retire
them deliberately using the preview, rather than overwriting local choices.
Additional vocabulary uses a local namespace; explain what it adds.

For proposal handling read [governance](../_shared/references/governance-flow.md);
for guarded edits read [concurrent edits](../_shared/references/concurrent-edits.md).
Kit adoption does not itself justify copying external data: apply the
[copy-in rule](../_shared/references/copy-in.md).
