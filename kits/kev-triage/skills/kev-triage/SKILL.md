---
name: kev-triage
description: Operate a KEV triage instance with judgment-only guidance for daily summaries, evidence boundaries, waivers, controls, and remediation proposals.
---

# KEV Security Triage

Use this skill when operating an already-onboarded KEV triage local. Keep this
as the judgment layer only. Command syntax belongs in command help; query and
relationship catalogs belong in generated config views and generated README
blocks.

## Operating Rule

The agent proposes; a reviewer resolves. Do not resolve groups unless the user
explicitly asks you to act as that reviewer for this run. Use
`cruxible group propose` for reviewable changes and `cruxible group resolve`
only when that reviewer role has been delegated.

Before proposing, confirm the active instance with `cruxible context show`,
check the current counts through the stats surface, and inspect pending review
work with `cruxible group list`. For a specific read surface, inspect required
params with `cruxible query describe` instead of copying query names into this
skill.

If the daemon refuses a write or permission check, surface the exact error and
stop. Do not retry with broader authority unless the user explicitly asks for
operator maintenance.

## Evidence Boundary

Scanner findings, EDR detections, SIEM alerts, reports, postmortems, tickets,
and review packets are evidence references in this kit. They are never graph
entities. Preserve them through artifacts, provider outputs, workflow traces,
tri-state signal evidence, receipts, proposal-member evidence refs, and
evidence rationale.

When evidence says a host was affected, remediated, excepted, or covered by a
control, use it to support or challenge the relevant governed relationship
surface. Do not turn the source record itself into an entity.

`control_mitigates_class` is curated local state. If report evidence shows the
mapping is missing, stale, too broad, or too narrow, report a data/config
authoring issue instead of proposing that relationship as governed review work.

Do not attach numeric confidence to governed proposals or accepted
relationships. This kit uses declared tri-state signals only: support, unsure,
or contradict, plus evidence text and thesis facts. Provider scores may inform
signal mapping; they should not be copied into proposal properties.

## Daily Triage

A daily triage pass should refresh only through the instance's tracked upstream
state path. Use `cruxible state status`, `cruxible state pull-preview`, and
`cruxible state pull-apply` as the command-help surfaces. If the instance is
not tracking the expected upstream reference, or the preview shows conflicts,
breaking compatibility, or an unexpected delta, stop and ask.

Run the proposal-producing workflow surfaces shown by generated config views.
Do not restate the proposal-chain order here; the config already records which
stages read accepted edges from prior stages. If a prerequisite stage is still
pending review, summarize that gap instead of implying later reads are final.

Before treating query output as complete, compare accepted state with pending
review work on the same governed surface. Accepted query results may lag
reviewable proposals.

Summarize daily triage with this taxonomy:

- **Elevated**: exposure on critical, internet-facing, or high-blast-radius
  assets/services, or posture backed by high-priority evidence.
- **Standard**: exposure needing normal remediation with no special urgency or
  prior history.
- **Overdue**: exposure past the KEV due date with no accepted exception.
- **Waived**: exposure covered by an accepted active exception.
- **Remediated-but-conflicted**: remediation has been recorded, but current
  evidence or proposals still need explanation.

Unless the user asked you to resolve groups, hand the summary to the next
reviewer rather than approving your own proposals.

Re-proposing is idempotent at the pending-bucket level: the same signature
rewrites one pending bucket instead of compounding the queue. Approved history
still suppresses unchanged tuples, so only deltas should remain reviewable.

## Exception Or Waiver Intake

For an exception request, confirm the exact asset/vulnerability scope,
approver, rationale, review date, and evidence reference. A broad maintenance
freeze is not enough; the proposed exception must explain why this asset and
this vulnerability are covered.

If the instance has a durable exception record surface, create or update it
using the current schema and command help. The governed judgment is the scoped
relationship that says the exception applies to the asset/vulnerability pair.
Propose that relationship with evidence; do not resolve it unless reviewer
authority was explicitly delegated.

Stop if the asset, vulnerability, exception, owner, or approver cannot be
resolved to the intended graph record.

## Control Effectiveness

For control review, confirm the control exists in local state and that the
evidence is about material mitigation for the relevant vulnerability class, not
merely the presence of a tool. Detection-only evidence should not be summarized
as blocking mitigation.

Treat curated control-class mappings as local data. If the mapping is missing
or wrong, report that as a data/config issue and include the evidence needed to
fix it.

## Remediation Verification

For a remediation claim, confirm:

- the exact asset/vulnerability pair
- the remediation category allowed by the current schema
- the evidence proving closure now
- the ticket, change, scan, or reviewer reference to preserve

Inspect allowed values through the schema or `cruxible query describe`; do not
copy enum lists into the skill.

Remediation state should be explicit. Do not infer closure just because a later
proposal run no longer reproduced an exposure. Propose remediation only when
the user or evidence supports closure for that exact pair.

## Review Feedback Loop

Rejected proposals mean the thesis, scope, or evidence was insufficient. Before
re-proposing, inspect the group with `cruxible group get` and
`cruxible group status`, then adjust the thesis, evidence, or scope. If a
reviewer marks a prior decision as untrusted or needing watch, summarize it as
unconfirmed.

## When To Stop And Ask

- A source record names an asset, owner, vulnerability, product, exception, or
  control that does not resolve to the intended entity.
- A remediation claim exists but the asset/vulnerability scope or verification
  evidence is ambiguous.
- The proposal would require a relationship or signal source not present in the
  current generated config views.
- Review material conflicts with accepted graph state.
- A workflow proposal produces no reviewable group when one was expected.
- Query results look empty, complete, or final while relevant
  `pending_review` groups still exist.
- The daemon returns `DirectWriteRefusedError` or `PermissionDeniedError`.
