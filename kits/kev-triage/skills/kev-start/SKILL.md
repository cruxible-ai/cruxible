---
name: kev-start
description: Adapt the KEV triage kit to local asset, owner, software, and service data using generated config views, data sanity checks, and governed proposal review.
---

# KEV Start

Use this skill when adapting the KEV triage kit to a user's local inventory.
Keep this as the judgment layer only. Command syntax belongs in command help;
kit structure belongs in generated config views and generated README blocks.

## Authoritative Surfaces

- For kit working mechanics and config-view handling, use `kits/README.md`
  "Working With A Kit".
- For ontology, workflow, governed relationship, and query structure, use the
  generated README views or `cruxible config views`.
- For query parameters and examples, inspect the specific query with
  `cruxible query describe`.
- For command options, use command help. Do not preserve local command recipes
  in this skill.
- For refusals, surface the daemon error and stop instead of restating the
  daemon's policy in prose.

## Goal

Produce a working KEV local over the user's data:

- the published KEV reference layer and local overlay are connected cleanly
- local assets, owners, software inventory, and services participate in the
  governed loop
- the final supported surface is intentionally kept, modified, removed, or
  extended for this onboarding pass
- kept workflows build or propose successfully
- kept named queries execute successfully, with representative non-empty
  results for surfaces backed by loaded data
- expected empty results are called out instead of treated as success by
  silence

## Data Sanity Bars

Before building or proposing, verify the local input surfaces are usable KEV
data, not merely files on disk:

- asset, owner, and service IDs are stable and unique
- service mappings reference known assets and known services
- software inventory rows reference known assets
- every software row has product name, version, and vendor, because those are
  the minimum useful fingerprint for reference product matching
- optional inputs become required only when the user keeps workflows or queries
  that depend on them

If a base surface is missing or too messy to normalize cleanly, stop. Do not
invent graph state manually to make onboarding appear complete.

## Tailor The Surface

Use generated config views as the human review surface before asking the user
to approve the final scope. Walk workflows, governed surfaces, and named
queries as keep / modify / remove decisions:

- **Keep** when the user's data supports the surface's assumptions and
  downstream purpose.
- **Modify** when the same concept exists but source shapes, property names,
  provider parameters, or traversal endpoints differ.
- **Remove** when the user does not have the data and is not onboarding that
  surface in this pass.
- **Add** a named query only when the user's domain has a common traversal the
  kit does not cover.

Do not hand-author alternate catalogs or diagrams in the skill. The generated
views are the structural source to review and refresh.

`control_mitigates_class` is curated local state, not a governed proposal
surface. During onboarding, missing or wrong control-class mappings are data or
config authoring issues to report and correct, not review-queue items to
propose.

## Onboarding Mode

Default to propose-only discipline. A reviewer resolves; the agent does not
resolve groups unless the user explicitly delegates that governance decision
for the onboarding run.

Choose the approval mode with the user:

- **Fast onboarding**: summarize each clean, expected proposal stage and
  continue only when the user has delegated approval authority for that pass.
  Stop before approval when a stage looks surprising.
- **Guided onboarding**: pause for explicit user approval at each unresolved
  stage.

Use the config/workflow surfaces for stage names and order; this skill does
not restate the proposal chain. Each proposal stage should be reviewed for
relationship type, member count, thesis or purpose, representative members,
signal quality, evidence, and scope. Do not force per-member narration unless
the user asks.

Stop before approval when:

- the group is much larger or smaller than expected
- the group is empty even though upstream data should support it
- representative members look wrong for the user's domain
- signals or evidence are weak, contradictory, or out of scope
- downstream blast radius would surprise the user

Do not attach numeric confidence to governed proposals or accepted
relationships. This kit uses declared tri-state signals only: support, unsure,
or contradict, plus evidence text and thesis facts. Provider match scores are
workflow inputs for signal mapping, not graph properties to preserve as
confidence.

Re-proposing is idempotent at the pending-bucket level: the same signature
rewrites one pending bucket instead of compounding the queue. Approved history
still suppresses unchanged tuples, so only deltas should remain reviewable.

## Verification

After tailoring and proposing:

- every kept workflow surface either builds/proposes successfully or has been
  intentionally modified or removed
- every kept named query executes; inspect required params with
  `cruxible query describe`
- queries tied to loaded data have at least one representative non-empty result
- intentionally empty queries are named in the hand-off
- at least one subject-context read returns linked user asset or service
  context
- the hand-off says which surfaces were kept, modified, removed, or added

## When To Stop And Ask

- The daemon is not reachable or the active context is not the KEV local the
  user intends to onboard. Confirm with `cruxible server info` and
  `cruxible context show`.
- Input files cannot be normalized into stable KEV surfaces.
- Required inputs for kept workflows or queries are missing.
- A kept workflow cannot build or propose cleanly against the user's data.
- A kept query that should have data returns empty after the relevant accepted
  edges exist.
- Proposal output is surprising, weakly evidenced, contradictory, or would
  create unexpected downstream blast radius.
- A read result looks final but relevant `pending_review` groups still exist;
  inspect the queue with `cruxible group list` and the relevant bucket with
  `cruxible group get` or `cruxible group status`.
- The daemon returns a direct-write, permission, artifact, schema, or missing
  input error. Surface the exact error and stop.

## Hand-Off

Tell the user what is live, what was intentionally excluded, which proposal
stages remain pending for review, and which query surfaces were verified with
real local data. If the user wants ongoing operation after onboarding, hand off
to `kev-triage`.
