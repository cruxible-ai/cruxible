---
name: review-state
description: Diagnose requested questions about Cruxible state, evidence, proposals or automation, and review exact candidates without implicitly changing state.
---

# Review state

An instance describes its own vocabulary through `cruxible orient`,
ClaimType descriptions, named queries and governed Documents linked by
`guides`. Read the part relevant to the user's question. A requested diagnosis
does not imply an audit of every kind or permission to repair the instance.

`next` reports attention and served repairs; `query` answers questions as
rows with verdict flags; `get` exposes a reference's summary, evidence, why
and history. Evidence belongs to each Claim: `query KIND --claims` names the
Claims behind each cell, and `get CLAIM --detail evidence` shows what one
rests on. Distinguish an explicit none from an unknown or unexamined value.
A stored Claim is not automatically supported, and a proposal is not accepted
state. Name the evidence and coordinate behind a conclusion. Before
re-deriving one, check whether an accepted Claim already answers the question;
cite it instead of recomputing, and use `next` to see what changed since.
`since` lists what was accepted after a generation you name (a coordinate
carries one): each changed member and how it changed, so the state itself
answers what happened since last time and no separate end-of-session report
is needed.

Pair Subjects have immutable ends and facts of their own. Follow served
pair/backlink cursors; hidden counts do not disclose hidden members. Governed
Documents retain their own links and citations. A Runbook's retained runs
describe the definition each run used; its current preview describes the
head, not a historical execution.

An attestation records examination of an exact Claim version. It accepts the
same evidence choices as a write: because/own words, a section, a file
passage, a Capture or a contract. Supporting, contradicting and unsure have
different consequences. An attestation is a signed append, never proposal
approval; append one only when requested, with what was actually examined.

Never approve your own proposal, even with auth off. A second agent (normally
a subagent) acting as its own principal (`reviewer`, from `init --reviewer-key-dir
DIR --require-independent-approval` or `principal add`) reviews the exact
candidate digest and approves; with no second principal, set one up rather than
self-approve, and the person approves only if they asked to. A reviewer
subagent uses the CLI with the reviewer's settings (`cruxible context use
--principal reviewer` or `CRUXIBLE_PRINCIPAL_ID=reviewer`): subagents share the
parent's MCP servers. `proposal review` prints each proposed value change with the Captures it
cites and their text (an erased or unavailable body says so), then one block
per definition (a Line's bindings and parameters, a mandate's ClaimType table,
a Runbook's steps, a Trigger's schedule); `--json` adds the stored fields
grouped by kind, `--detail` every member. The [governance reference](../_shared/references/governance-flow.md)
covers acceptance and changed candidates. Authorized repairs use
[guarded edits](../_shared/references/concurrent-edits.md). Report findings with
references, evidence, repair and limits of what was checked; do not turn
diagnosis into an unrequested mutation.
