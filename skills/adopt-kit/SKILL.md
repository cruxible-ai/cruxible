---
name: adopt-kit
description: Start from a Cruxible kit, install or upgrade it, and tailor its automation template when the user wants a starting domain model.
---

# Start from a kit

A kit is a starting point, not a schema to adopt: a seed of vocabulary,
queries, Procedures with their provider packages, reference pages and an
automation template. Claims, principals, approval policy and local automation
belong to the instance, which is the user's to grow. Read `cruxible orient`
and `cruxible kit status` to see what exists. ClaimType descriptions explain
each field's meaning; governed Documents linked by `guides` carry domain
guidance. Build on those meanings rather than adding a parallel model.

A registry version names a release; its digest identifies the bytes. `cruxible
kit pull NAME:VERSION --out DIRECTORY` fetches and verifies without installing.
`cruxible kit add REFERENCE` previews the change. Its guarded commit uses
`--commit --at OID` with the preview's coordinate. Provider installation
needs admin authority. A blocked or awaiting-providers result is unfinished,
not an installed kit; report its served repair.

The manifest's `automation_template` names inert local automation, not an
assumed examples path. Tailoring it is the normal path: adjust its sources,
names, timing and grants, then submit and obtain review and approval from a
separate agent session (its own principal); after acceptance, inspect
`cruxible runbook preview <start>` and run `cruxible runbook run <start>` when
authorized. A review stop leaves a proposal, not permission to approve it
yourself. Enabling scheduled Lines is separate consent. Facts copied from
sources settle under a Line's Mandate; judgments always need review.

Installed kit definitions, Procedures included, can be revised locally like
any definition, and guidance Documents are the instance's to edit; new
vocabulary can also go in a local namespace. An upgrade preview names your
local edits and dependent definitions; the release's version replaces an edit
unless you keep it. Keep or retire each one deliberately.

The bundled [kits reference](../../docs/kits.md) explains manifests, releases,
upgrades and kit automation. For proposal handling read
[governance](../_shared/references/governance-flow.md); for guarded edits read
[concurrent edits](../_shared/references/concurrent-edits.md). Kit adoption
does not itself justify copying external data: apply the
[copy-in rule](../_shared/references/copy-in.md).
