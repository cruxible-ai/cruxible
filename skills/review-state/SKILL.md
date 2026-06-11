---
name: review-state
description: Review an existing state, inspect evaluation, queries, governed groups, feedback, and outcomes, and surface prioritized issues with the likely fix surface before making changes.
---

# Review State

Use this skill on an existing state when the goal is diagnosis, quality review, or prioritizing follow-up work.

This skill is for:

- finding structural or behavioral problems
- inspecting representative queries and receipts
- reviewing governed groups and their outputs
- spotting repeated feedback or outcome patterns
- telling the user what is wrong and what should be fixed next

This skill is not the main build flow. By default, review first and surface findings before changing config, workflows, or graph state.

## Phase 1: Establish review scope

Start by identifying:

1. what the user is worried about
2. whether the review is about:
   - graph quality
   - governed relationships
   - query quality
   - feedback or outcome patterns
   - overall state health
3. whether the state is a root state or an overlay
4. which user-facing queries or downstream decisions matter most
5. whether source files, prepared files, or `prepare-data` outputs are available for a source-to-state audit

Then inspect the current state:

```bash
cruxible state status
cruxible stats
cruxible evaluate
```

If the state is an overlay and the review is about overall health, local fit quality, or handoff readiness, include upstream pull compatibility in scope by default.

## Phase 2: Inspect the most important surfaces

Work from the surfaces that actually affect users.

### Workflows, providers, and implementation

If findings may come from how the state is built rather than only from final query outputs, inspect the implementation surfaces too:

- canonical `workflows`
- proposal `workflows`
- `providers`
- `integrations`
- relevant config sections and provider code

Use this to answer:

- is the bad behavior caused by graph shape, or by how workflows and providers are wired?
- are canonical `workflows` writing only deterministic, trusted results?
- are proposal `workflows` producing the right candidates, signals, and groups?
- is the likely fix in config design, provider code, or workflow implementation?

### Queries and receipts

If the state has `named_queries`, run the ones that matter to the user's concern and inspect receipts:

```bash
cruxible query --query <query_name> --param key=value
cruxible explain --receipt <receipt_id>
```

If the review is about overall state health or handoff readiness, do not stop at representative coverage. Exercise all `named_queries`.

Use this to answer:

- does the query answer the real user question?
- does the receipt show a believable evidence path?
- are results missing because of graph gaps, governed gaps, or query design problems?

### Governed groups

If the state uses governed relationships, inspect the review surfaces:

```bash
cruxible group list
cruxible group get --group <group_id>
cruxible group resolutions
```

Also inspect the governed design artifacts and config that define what those groups are supposed to mean:

- relationship `matching` config
- `design/governed/<relationship_type>.yaml`
- relevant proposal `workflows`, `integrations`, and provider code

Use this to answer:

- are the right relationship types being governed?
- do groups ask the right review question?
- do `thesis_facts` and `analysis_state` look well-modeled?
- are groups getting stuck, suppressed, or producing poor signals?
- is the problem in the governed design itself, or in the provider/workflow implementation that is supposed to realize it?

### Feedback and outcomes

If the state has enough history, inspect recurring review and outcome patterns:

```bash
cruxible analyze-feedback --relationship <relationship_type>
cruxible analyze-outcomes --anchor-type <receipt|resolution> --surface-type <query|workflow|operation> --surface-name <name>
```

Use whichever filters match the review surface you are investigating. Treat these as evidence about recurring process failures, not as automatic instructions to mutate the state.

If history is sparse, inspect the config surfaces directly instead:

- `feedback_profiles`
- `outcome_profiles`
- `decision_policies`
- `quality_checks`
- `constraints`

Use this to answer:

- do the configured review and outcome surfaces match the real recurring loops the state actually has?
- are governance rules present where they are justified, and absent where they would just add noise?

### Source-to-state audit

If source files, cleaned files, or `prepare-data` outputs are available, compare them against the current state design and behavior.

Use:

- source files and cleaned files
- `source_inventory`
- `transform_lineage`
- `loading_readiness_by_surface`
- `likely_modeling_implications`
- `open_questions`
- current `config.yaml`
- canonical `workflows`
- governed relationship `matching` and design notes where relevant

Use this to answer:

- does each `entity_type` match a real source grain?
- are current deterministic relationships actually supported by explicit keys and joins?
- are any current canonical relationships ambiguous enough that they should be governed instead?
- are any governed relationships actually deterministic and overcomplicated?
- is the graph shape still consistent with what the prepared data supports?

If those source or preparation artifacts are not available, say explicitly that the source-to-state classification audit was not performed.

### Overlay pull compatibility

If the state is an overlay and pull compatibility is in scope, inspect:

```bash
cruxible state pull-preview
```

Use this to answer:

- does the local fit still compose cleanly with the upstream state?
- are any local additions likely to conflict with future upstream pulls?
- should a change stay local, move upstream, or be simplified?

## Phase 3: Classify findings by fix surface

Do not just list problems. For each real issue, classify where the fix belongs:

- `prepare-data`: source file quality, key issues, join issues, grain issues
- `create-state`: base graph shape, wrong canonical-versus-governed boundary, canonical workflow design, governed design, named queries, or feedback/outcome structure in a root state
- `overlay-and-fit`: local fit boundary, wrong local canonical-versus-governed boundary, local canonical fit, local governed additions, local queries, or overlay pull-compatibility issues
- provider or workflow implementation
- query design
- review policy, trust, feedback, or outcome configuration
- upstream work rather than local work

This classification is the main value of the review.

## Phase 4: Prioritize what matters

Present findings in this order:

1. broken or invalid states
2. query failures or misleading query results
3. governed-group design or workflow problems affecting important decisions
4. repeated review or outcome patterns that indicate a real systemic issue
5. missing relationships or coverage gaps
6. cleanup or simplification opportunities

Do not bury the important issues under low-value polish.

## Phase 5: Surface the review to the user

Default output is a prioritized findings report.

For each finding, include:

- what is wrong
- why it matters
- representative evidence
- the likely fix surface
- whether it should be fixed now, later, locally, or upstream

If there are no meaningful issues, say that explicitly and mention any residual blind spots, such as:

- no governed history yet
- no representative query receipts yet
- too little feedback or outcome data to infer patterns
- source files or `prepare-data` outputs were not available for a source-to-state audit

## Optional Follow-Up

Only move from review into changes if the user asks for fixes or clearly wants you to continue.

If the next step is clear:

- use `prepare-data` for source-data issues
- use `create-state` for root-state build or redesign work
- use `overlay-and-fit` for local adaptation work
