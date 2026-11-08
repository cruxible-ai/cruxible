---
name: create-state
description: Define or extend Cruxible vocabulary and evidence-backed values for a user's domain, including preparing data that needs to become shared state.
---

# Create state

Cruxible holds typed Claims about Subjects, with evidence and accepted history.
Begin with `cruxible orient` for the small index and relevant ClaimType
descriptions. Existing named queries and governed Documents may already
describe the state the user needs. An available kit can supply vocabulary;
`adopt-kit` handles its installation.

A ClaimType says what a Claim means: its applicable Subject kinds, value
type, cardinality, role and evidence rules. Its description explains that
meaning, not a recipe for an agent's work. Keys identify the same thing across
observations. A relationship with its own facts can be a pair Subject keyed
by Subject-valued ends; those ends are immutable. Prefer existing kinds and
fields when they express the intended meaning.

Apply the [copy-in rule](../_shared/references/copy-in.md) before turning source
material into Claims. A governed Document can point to a source instead.
Documents have `about` and `guides` links and backlinks; prose and its citations
can remain evidence without becoming a mirrored database. When copying is
justified, the [data-readiness reference](../_shared/references/data-readiness.md)
helps resolve grain, identifiers and joins without requiring a profiling
project for every input.

Definitions use authoring; values use `set`, `retire` or `write`. File passages,
sections, Captures, contracts and the writer's own words provide evidence
through the supported write surface. Preserve verdict flags instead of
treating every stored value as supported.

Use [guarded edits](../_shared/references/concurrent-edits.md) for shared state
and [governance](../_shared/references/governance-flow.md) when a change awaits
review. Report the accepted references and coordinate, or the specific proposal
still awaiting acceptance. Pages and automation are additions only when the
user's intended outcome needs them.
