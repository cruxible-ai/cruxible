# Governance Flow

Use this reference after the canonical layer is real and you still need governed relationships, named queries, query proof, feedback/outcome structure, or final handoff.

Once a parent skill tells you to use this reference, read it as the source of truth for the remaining workflow. Individual phases inside it may still be skipped when they are not needed.

This reference is shared by:

- `create-state`, after its initial graph and canonical build phases
- `overlay-and-fit`, after its local canonical fit phase when the inherited state plus selected `kit` are still not enough

When using this reference in an overlay:

- prefer inherited and `kit` surfaces before adding local ones
- add only local entries instead of re-declaring inherited ones
- if a change really belongs upstream, call it out as upstream work

## Phase 1: Design the governed group layer

Only add this phase if the state needs reviewable, judgment-based relationships beyond the canonical layer.

Before choosing providers or proposal workflow steps, understand the proposal process at a high level:

- a non-canonical proposal workflow should produce candidate relationships and a relationship-group proposal
- those candidate relationships do not go directly into state
- instead, Cruxible turns them into a reviewable group for one relationship type
- an `integration` is the named source of judgment used in governed relationship proposals
- a `provider` may produce the raw output, but the `integration` name is what Cruxible uses for signal guardrails, review policy, and trust reuse
- a `signal` is one integration's judgment about one candidate relationship:
  - `support`: this integration supports the relationship
  - `contradict`: this integration argues against the relationship
  - `unsure`: this integration cannot support the relationship strongly enough to approve it
- signals should carry short evidence that explains why the integration produced that judgment
- prior trust rules control when a new group can reuse trust from an earlier approved group with the same signature:
  - `trusted_only`: only prior groups explicitly marked `trusted` can unlock auto-resolve
  - `trusted_or_watch`: either `trusted` or the default `watch` trust can unlock auto-resolve
- `auto-resolve` here means the group's matching policy can mark it eligible for `auto_resolved` status based on its signals and prior trust; it does not mean the agent should bypass proposal/review design
- the group carries:
  - a human-readable `thesis_text`
  - structured `thesis_facts` that define the stable identity/signature of the proposal
  - `analysis_state` for useful context that should help review but should not define identity
  - member-level signals
- the group is then reviewed, approved, rejected, or reused through prior trust
- approved groups create relationships later; rejected groups still matter because they establish precedent and trust context

Before choosing providers or proposal workflow steps, design the governed group structure itself. Answer the macro-level grouping questions first:

1. which relationship types should be proposed and reviewed instead of written canonically?
2. what grouping rule should determine which candidate relationships belong in the same group, and what should be split into separate groups?
3. what is the unit of review for this relationship type, and what single judgment should each resulting group ask the reviewer to make?
4. what general kind of `thesis_text` should explain why a group exists?
5. what kinds of facts should belong in `thesis_facts` so the same proposal can be recognized across repeated runs?
6. what kinds of reviewer context should stay in `analysis_state` instead of the signature?
7. what kinds of evidence or judgment sources should matter for this relationship type?
8. which kinds of evidence should be blocking, required, or advisory in principle?
9. should uncertainty always force review for this relationship type?
10. should this relationship type ever auto-resolve, and if so under what prior trust rule?
11. do the user-facing named queries depend on approved governed relationships, or only on canonical state?
12. summarize the governed group design for user confirmation

Keep this phase at the design-rule level. Define how governed groups should work in general for this relationship type or proposal workflow. Do not answer per-group review questions yet; those belong later when concrete groups exist.

Before writing any governed-group config artifacts, surface the governed design in normal language and get user approval. Do not present the YAML design note itself as the approval surface.

Summarize:

- which relationship types are governed
- what one group means
- what single judgment each group asks the reviewer to make
- what kinds of facts define stable identity in `thesis_facts`
- what kinds of context belong in `analysis_state`
- what kinds of evidence matter
- whether uncertainty forces review
- whether auto-resolve should ever happen, and under what prior trust rule
- whether named queries depend on approved governed relationships

If the user already answered these questions earlier, summarize the resulting design and confirm it rather than re-asking everything.

## Write Step A: Add governed-group structure

After the user approves the governed group design, add the config pieces that define the governed relationship layer.

Add the config pieces that define the governed relationship layer:

- relationship `matching` config where needed
- add proposal-specific `contracts` only if the approved governed design introduces a new proposal artifact shape that was not already defined earlier in the workflow
- otherwise reuse the existing contracts and do not redefine them here
- do not invent new schema fields or top-level group config here; save provider, integration, and workflow implementation details for later phases

Also write a governed-group design note for each governed relationship type at:

```text
design/governed/<relationship_type>.yaml
```

Use this shape:

```yaml
relationship_type: asset_runs_product
grouping_rule: one group per asset under one product-matching thesis
unit_of_review: one reviewable judgment about which product relationships should exist for one asset
judgment_question: Which product relationships for this asset should be approved?
thesis_text_guidance: short human-readable statement of why this relationship group exists
thesis_facts_fields:
  - asset_id
  - matching_scope
analysis_state_fields:
  - candidate_rankings
  - provider_notes
  - raw_match_context
evidence_source_types:
  - exact identifier match
  - catalog similarity
  - human supplied context
signal_policy:
  blocking_kinds:
    - explicit contradiction from trusted source
  required_kinds:
    - core matching judgment
  advisory_kinds:
    - weaker contextual hints
  unsure_forces_review: true
auto_resolve_policy:
  enabled: false
  prior_trust_rule: trusted_only
query_depends_on_governed_relationships: true
open_questions: []
```

This design note is an internal record of the approved governed semantics. It should capture:

- grouping and review semantics
- thesis/signature design
- evidence and signal policy in principle
- auto-resolve policy
- whether named queries depend on approved governed relationships

Do not put later implementation details in this note:

- no provider names
- no workflow step design
- no concrete group instances
- no resolution outcomes

Do not choose providers or build proposal workflows yet. First make the group semantics explicit.

## Phase 2: Design provider-backed proposal workflows

Only after the governed group structure is clear.

Use the approved `design/governed/<relationship_type>.yaml` note and `matching` config for each governed relationship type as the source of truth in this phase. Do not redesign grouping, thesis, `thesis_facts`, `analysis_state`, or auto-resolve policy here unless implementation exposes a real mismatch that needs to go back to the user.

In this phase:

- a `provider` is the executable logic behind a workflow step
- it takes structured inputs and returns structured outputs defined by contracts
- it may be code, external logic, or model-backed judgment
- it implements the approved governed design; it does not define that design by itself
- an `integration` is the named judgment source Cruxible uses for signals, review policy, and trust reuse

1. for each governed relationship type approved in Phase 1, identify the provider-backed task or tasks needed to produce candidates and signals
2. identify what raw inputs each task needs from the graph or artifacts
3. identify what outputs the provider should produce before signal mapping
4. choose which concrete integrations should implement the approved evidence policy for each task
5. map each integration to the already-approved `blocking`, `required`, or `advisory` role
6. decide how provider output becomes:
   - candidate relationships
   - `support`, `contradict`, or `unsure` signals
   - the fields that will populate `thesis_facts`
   - the fields that will populate `analysis_state`
   - a relationship group proposal
7. decide which non-canonical workflows should end in `propose_relationship_group`
8. summarize the proposal-workflow implementation choices
9. if implementation exposes a semantic mismatch or open question, stop and return to the user before changing the approved governed design

Keep this phase implementation-focused. Choose providers, integrations, inputs, outputs, and workflow wiring that implement the approved governed design. Do not reopen group semantics here unless implementation exposes a real mismatch.

## Write Step B: Add proposal workflows and providers

Extend the config with the proposal machinery that is actually justified:

- `artifacts`
- `contracts`
- `integrations`
- `providers`
- non-canonical proposal `workflows`

Only add the pieces this governed relationship type actually needs.

Those non-canonical proposal `workflows` should gather the needed graph or artifact inputs, call providers, build candidates with `make_candidates`, convert evidence into signals with `map_signals`, and emit reviewable groups with `propose_relationship_group`.

If a provider in this phase is implemented as code, write that provider code now. Make sure it accepts the structured inputs the contracts define, returns the structured outputs the contracts define, and is wired into the workflow config correctly.

Keep these workflows non-canonical and route them through the proposal/review step. Do not bypass review for judgment-based matching or other non-deterministic relationship decisions.

## Phase 3: Run proposal workflows and establish the governed layer

Only do this phase if named queries or downstream review depend on approved governed relationships.

Run the proposal workflows, inspect the resulting groups, and review enough representative groups to make the intended governed layer real.

This phase is where the governed design and the proposal-workflow implementation get validated against real emitted groups. Treat the approved governed design as the current best design, not as untouchable ground truth. If the real groups show that the governed layer should work differently, iterate.

Use the real CLI surfaces for this work:

```bash
cruxible propose --workflow <workflow_name>
cruxible group list
cruxible group get --group <group_id>
cruxible group resolve --group <group_id> --action approve
cruxible group resolve --group <group_id> --action reject
cruxible group resolutions
cruxible group trust --resolution <resolution_id> --status <watch|trusted|invalidated>
```

If a proposal workflow produces no reviewable group or a suppressed result, stop and inspect prerequisites, provider output, and workflow wiring before assuming the governed layer exists.

For each concrete group you inspect, answer the per-group questions:

1. what is this group's actual `thesis_text`?
2. what are this group's actual `thesis_facts`?
3. what useful context belongs in this group's `analysis_state`?
4. do the candidate relationships in this group actually belong together under one review decision?
5. do the signals and evidence make the group reviewable, or does the grouping rule need to change?
6. should this group be approved, rejected, or escalated for deeper review?

Use the agent's judgment to evaluate proposal quality, but make improvements
by changing durable Cruxible surfaces rather than treating the agent's private
reasoning as the system of record. After each proposal run, inspect the actual
group output and summarize:

- group count and member count by relationship type
- review priority distribution
- signal distribution by integration
- three representative `support` members, when available
- three representative `unsure` members, when available
- three representative `contradict` members, when available
- whether the emitted groups are coherent review units

Classify any problem before editing:

- **Too broad**: unrelated contexts are bundled into one group, so one
  approve/reject decision would be awkward or unsafe.
- **Too narrow**: every candidate becomes its own group, so trust and feedback
  cannot compound across a reusable decision pattern.
- **Weak evidence**: required signals are missing, mostly `unsure`, or based on
  evidence a reviewer would not trust.
- **Conflicted evidence**: required or high-value integrations disagree in a
  way the current grouping/policy does not explain.
- **Bad review unit**: the group is internally related, but the question it asks
  is not the question the reviewer actually needs to answer.
- **Unstable identity**: `thesis_facts` include run-specific or one-off details
  that prevent repeated runs from matching the same proposal signature.
- **Missing review context**: `analysis_state` omits information a reviewer or
  downstream agent needs, even though that information should not define group
  identity.

When improving proposal quality, make one durable change at a time, rerun the
workflow, and compare the before/after group output. Typical changes are:

- narrow or broaden candidate generation
- change `thesis_facts` fields
- move context between `thesis_facts` and `analysis_state`
- split one workflow into multiple proposal workflows
- merge overly fragmented workflows
- change integration roles between required and advisory
- change `always_review_on_unsure` or auto-resolve policy
- add or revise feedback reason codes
- fix provider output or `map_signals` mapping

Do not keep iterating just because a group is imperfect. Stop when groups are
good enough review units:

- each group asks one clear judgment question
- the group can be approved or rejected as a unit without surprising side
  effects
- required signals are present for the members that should be reviewable
- contradictory or low-confidence members are split out, escalated, or made
  explicitly review-only
- `thesis_facts` are stable enough for repeated runs and prior trust reuse
- `analysis_state` carries useful context without changing group identity
- group size is practical for the intended reviewer

Use those answers to test and iterate on the current governed design:

- does the emitted group follow the current `grouping_rule`, or is that rule too broad or too narrow?
- does it ask the intended `unit_of_review` question, or is the review unit wrong?
- do the actual `thesis_facts` match the intended identity fields, or is identity being modeled incorrectly?
- does the actual `analysis_state` look like useful review context rather than identity-bearing data?

When real groups expose ambiguity or a design mismatch, ask targeted iteration questions before changing the design. For example:

- should this group be split or merged differently?
- is the review question the user actually wants to answer here?
- are these `thesis_facts` stable enough to define identity across repeated runs?
- is important reviewer context missing, or is too much identity leaking into `analysis_state`?
- are the current signals strong enough to support review and later trust reuse?

If the governed design enabled auto-resolve or this relationship type is supposed to reuse prior resolutions across repeated runs, inspect prior resolutions and trust status before moving on. Use `group trust` only when a representative resolution should become reusable precedent or should invalidate earlier precedent.

After approving representative groups, verify that the intended governed relationships now exist in the state before moving on to named queries. Re-check the state with:

```bash
cruxible stats
```

If the grouping rule, review question, thesis/signature design, or signal policy is wrong, go back to Phase 1 and revise the governed design with the user. If the provider output, signal mapping, or workflow wiring is wrong, go back to Phase 2 and revise the implementation.

Do not design the final query surface against a state that is still missing the governed relationships it is supposed to rely on.

## Phase 4: Understand the user-facing query surface

1. identify the repeated user questions that matter most
2. choose the real entry-point entity type for each question
3. decide the traversal direction and fan-out needed
4. identify what evidence path a human should be able to inspect
5. summarize the planned query surface for user confirmation

Keep this phase user-facing. Design queries around the real questions users ask and the evidence they should be able to inspect. Do not reopen earlier graph or workflow design unless an important question has no clean path through the current state.

If an important user question has no clean path through the current state, do not force a bad query. Go back to the earlier step that owns the problem:

- the earlier graph-shape or local-fit phase in the parent skill if the state is missing the needed entities or deterministic relationships
- Phase 1 if the governed relationship design is wrong
- Phase 2 if the proposal workflow implementation is wrong

Also use this phase to simplify the state when needed. If an entity, relationship, or governed path was added in anticipation of queries that the real user-facing query surface does not actually need, do not keep that complexity by default. Go back to the earlier step that introduced it and narrow the design.

## Write Step C: Add named queries

Write the actual `named_queries` now. For each user-facing query you are keeping:

- add a stable query name
- set the `entry_point`
- define the `traversal` steps
- set `returns`
- keep the query as narrow and inspectable as the use case allows

Do not stop at describing the query surface in prose. Add the `named_queries` to the config before validating.

When the config changes:

```bash
cruxible validate --config config.yaml
cruxible config reload --config config.yaml
```

If providers, artifacts, or workflows changed too, lock again:

```bash
cruxible lock
```

## Phase 5: Prove the queries work

Run every `named_query` you added and inspect its receipt:

```bash
cruxible query run <query_name> --param key=value
cruxible explain --receipt <receipt_id>
```

Do not hand off a state whose `named_queries` have not all been exercised against representative cases.

## Phase 6: Understand the feedback and outcome flywheel

In this phase:

- `feedback` is structured review about whether a relationship is right, wrong, or needs correction
- a `feedback_profile` defines the structured vocabulary for relationship-scoped review:
  - `reason_codes` say what went wrong or what kind of correction is being made
  - `remediation_hint` says what kind of fix the feedback points toward
  - `scope_keys` are the named fields used to group and analyze similar feedback consistently
- proposal groups may still be reviewed by humans or agents, but that review should usually resolve into relationship-scoped feedback
- an `outcome` is later evidence about whether a prior resolution, query result, workflow result, or other system decision was actually correct or useful
- an `outcome_profile` defines the structured vocabulary for those later results:
  - it anchors to either a `resolution` or a `receipt`
  - `outcome_codes` say what later happened
  - `remediation_hint` says what kind of fix the outcome points toward
  - `scope_keys` are the named fields used to group and analyze similar outcomes consistently
  - receipt-anchored profiles also record the relevant surface metadata
- a `resolution` is the recorded approve/reject decision on a candidate group
- a `receipt` is the recorded output of a query, workflow, or operation
- `quality_checks` are recurring health checks over the state
- `decision_policies` are exact-match rules that suppress or require review for governed decisions
- `constraints` define invalid states the state should reject or warn on

1. identify where humans or agent reviewers will review relationships or proposal groups
2. identify which relationship-scoped review surfaces need structured `feedback_profiles`
3. identify the `feedback_profiles` those relationship review surfaces need
4. identify which downstream outcomes should be recorded for resolutions, queries, workflows, or operations, and whether they should anchor to a `resolution` or a `receipt`
5. identify the `outcome_profiles` those outcome surfaces need
6. identify what repeated failure modes should become `constraints`, `quality_checks`, `decision_policies`, provider fixes, workflow fixes, or graph fixes
7. identify what feedback and outcome flywheels should improve the state over time
8. summarize the feedback and outcome flywheel for user confirmation

Keep this phase focused on real recurring review and outcome surfaces for both human and agent reviewers. Do not invent `feedback_profiles`, `outcome_profiles`, or governance rules unless there is a real review or outcome loop to support.

## Write Step D: Add feedback and outcome structure

Add the later-stage governance pieces that are actually justified:

- `quality_checks`
- `constraints`
- `decision_policies`
- `feedback_profiles`
- `outcome_profiles`

Define `feedback_profiles` and `outcome_profiles` to match the real recurring review and outcome surfaces you identified earlier, not speculative completeness.

## Phase 7: Evaluate and hand off

Run:

```bash
cruxible evaluate
```

Then summarize:

- entity counts
- relationship counts
- canonical `workflows` used
- governed relationship types and proposal `workflows` used
- all `named_queries` exercised, with example invocations
- one representative receipt or query that was checked in detail
- review surfaces and feedback/outcome plans
- open questions or deferred cleanup
- next actions the user can take

Simple domains may stop earlier. Do not force every domain to use every later-stage feature.
