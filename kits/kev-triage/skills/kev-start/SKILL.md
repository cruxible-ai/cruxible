---
name: kev-start
description: Adapt the KEV triage kit to your own asset, inventory, and service-mapping data. Build the reference and local layers, run the proposal chain, and verify that your data participates in the governed loop cleanly.
---

# KEV Start

Use this skill when a developer is adapting the KEV kit to their own data.
The goal is to build a working KEV-shaped state on their inventory: reference
layer loaded, local layer built from their assets/owners/software/services,
proposal chain run, and governed outputs visible in queries.

## Goal

Produce a working KEV local in one pass:

- The public KEV reference layer is loaded
- The local layer is built from the user's asset, owner, software, and service
  inputs
- The proposal chain has run and the review queue is populated
- The final supported KEV surface is tailored to the user's data. Kept
  workflows build or propose successfully, and every kept named query
  executes successfully against the user's data. Queries that should have
  data in this pass are proven with at least one non-empty invocation;
  queries that are intentionally retained but currently empty are called out
  explicitly. Queries or workflows that don't match the user's domain are
  removed or modified, and new query surfaces may be added for common
  traversals the kit doesn't cover
- At least one rendered subject page shows the user's assets or services with
  linked context

## Done When

- Reference and local state both build without drift or missing-input errors
- The proposal queue reflects the user's inventory
- Every kept workflow surface either runs successfully or is intentionally
  removed or modified before onboarding completes
- Every kept named query executes successfully, with at least one
  representative non-empty invocation for the queries tied to loaded data
- A subject-context query returns the user's linked context for at least
  one onboarded subject

## Preconditions

- Run from `kits/kev-triage/` or a local of it
- Cruxible daemon reachable (`cruxible server info --json` succeeds)
- For custom onboarding, replace local seed or source files after
  `state create-overlay --kit` materializes the local overlay, unless you are already
  working in a local where the kit has been applied
- The daemon may run under a restrictive `CRUXIBLE_MODE` (e.g.
  `governed_write`) and/or `refuse_direct_writes` for governed types; that's
  fine — this skill does not perform direct writes to governed types

If the daemon isn't reachable, stop and tell the user to start the project's
configured Cruxible daemon before continuing. Do not try to initialize
without a daemon.

## Generated config views

Use generated config views as the structural review surface during onboarding.
The KEV kit uses `extends`, so render the runtime composed view: inherited
reference ontology and query surfaces are visible, while upstream build-only
workflows are hidden from the onboarding workflow summary.

For fast review in GUI harnesses that can render Mermaid, show the generated
views inline. From the repository root:

```
uv run cruxible config views --config kits/kev-triage/config.yaml --runtime --view all
```

If you are already inside `kits/kev-triage/`, use the same command with the
local config path:

```
uv run cruxible config views --config config.yaml --runtime --view all
```

Also keep the README marker blocks updated so CLI and non-GUI harnesses can
inspect the same generated views. From the repository root:

```
uv run cruxible config views --config kits/kev-triage/config.yaml --runtime --update-readme kits/kev-triage/README.md
```

From inside `kits/kev-triage/`:

```
uv run cruxible config views --config config.yaml --runtime --update-readme README.md
```

Everything between `CRUXIBLE:BEGIN` / `CRUXIBLE:END` markers is code-owned
structural output from `config.yaml`. Authored README prose may explain the KEV
story, operating assumptions, and hand-off notes, but it must not replace or
contradict the generated ontology, workflow, governed relationship, or query
views. Do not hand-author alternate Mermaid diagrams when a generated view
exists.

When iterating the KEV local toward its final supported surface, explicitly
direct the user to the generated README views as the primary way to understand
the current configuration. The YAML remains the execution source of truth, but
the README's generated ontology, workflow, governed relationship, and query
sections are the review surface for human decisions. Before asking the user to
approve kept workflows, governed boundaries, query surfaces, or local
adaptations, update the README marker blocks and point them to the relevant
generated section.

## Flow

### 1. Check current state

```
cruxible server info --json
cruxible context show --json
```

If `context show --json` includes an `instance_id`, then run:

```
cruxible stats --json
cruxible query list --json
```

Use this step to decide which milestone already exists:

- Reference layer built
- Local layer built
- Proposal queue already populated

If the remembered instance's query surface does not look like the KEV kit
(for example it does not include queries such as `vulnerability_asset_context`,
`owner_patch_queue`, and `product_asset_context`), do not try to resume it.
Treat that as "wrong instance selected" and create or select a KEV instance in
Step 4 instead.

Do not treat "nonzero entities" as the only branch. A partially built state is
common when adapting inputs. Resume from the first missing milestone instead of
starting over blindly.

### 2. Prepare and verify the input files

Before building the local layer, make sure the local source files are usable as
KEV inputs, not just present on disk. At this stage, verify only the minimum
base surfaces needed for a KEV-shaped local:

- assets
- owners
- software inventory
- service mapping

If one of these base surfaces is missing, stop and name exactly which input is
not present. Do not compensate by inventing graph state manually.

Sanity-check the prepared inputs before continuing:

- asset IDs are stable and unique
- owner IDs are stable and unique
- service IDs are stable and unique
- service mappings reference known assets and services
- software inventory rows reference known assets
- each software row has product name, version, and vendor — enough to
  fingerprint against reference CPEs. Without all three, `propose_asset_products`
  will emit nothing.

If the source files are too messy to normalize cleanly, stop and switch to
`prepare-data` or the general `create-state` skill rather than forcing bad
state into the KEV local.

### 3. Tailor the final KEV surface to the user's data

The exit condition for onboarding is that the final supported KEV surface is
validated against the user's local. Walk through the relevant parts of
`config.yaml` with the user and decide what survives this onboarding pass.
Before making those keep/modify/remove decisions, render the runtime config
views inline and update the README marker blocks. Use the generated ontology,
workflow summary, governed relationship table, query map, and query catalog as
the surface-selection aid instead of asking the user to reason over YAML alone.

Treat the surfaces in three buckets:

1. **Core required workflows**
   These are part of the standard KEV loop unless the user is intentionally
   narrowing the kit:
   - `build_local_state`
   - `propose_asset_products`
   - `propose_asset_exposure`
   - `propose_exposure_reconciliation`

   For each one, decide:
   - **Keep** if the user's data supports the workflow's assumptions and
     downstream purpose
   - **Modify** if the workflow shape is right but input file names, provider
     params, property names, or traversal endpoints need adjusting
   - **Remove only with explicit callout** if the user is intentionally not
     onboarding the standard KEV loop on this pass

2. **Optional workflows / enrichment surfaces**
   These usually depend on secondary inputs such as controls, exceptions,
   patch windows, or review-material evidence.

   For each optional workflow or governed surface, decide:
   - **Keep** if the user's data covers it
   - **Modify** if the same concept exists under different source shapes
   - **Remove** if the user doesn't have that data and doesn't plan to add it
     in this pass

3. **Named queries**
   Walk through `named_queries` and decide per query:
   - **Keep** if the user's data covers its entry point and traversal
   - **Modify** if the user's data has the same shape under different
     property names or traversal endpoints
   - **Remove** if it depends on data the user doesn't have and doesn't plan
     to add in this pass
   - **Add** if the user's domain has a common traversal the kit doesn't
     cover

Once the final supported surface is chosen, verify that any additional
required inputs for the kept queries and workflows are actually present. This
is where optional inputs such as exceptions, controls, patch windows, and
review-material evidence become required if the user chooses to keep the
queries or workflows that depend on them.

`control_mitigates_class` is deterministic curated local state, not a governed
proposal workflow. During onboarding, load or edit the local control-class
mapping seed/config and treat missing or wrong mappings as data authoring
issues rather than review queue items.

After modifying the final supported surface, rerender the runtime config views
inline and update the README marker blocks again. Do this before initializing
or running the local so the durable README matches the config the agent is about
to execute.

Confirm the final supported surface with the user before initializing. The
remaining steps verify that:

- kept workflows can build or propose successfully
- every surviving query executes
- the queries backed by loaded data have at least one known-good non-empty
  invocation

A query may be intentionally kept even if this dataset is expected to leave it
empty, but a kept workflow should not remain in the final surface if its
required inputs are absent for this pass.

### 4. Create the KEV local from the published state

Server mode is the primary path for 0.2. The daemon owns the instance
directory (under `CRUXIBLE_SERVER_STATE_DIR`, default `~/.cruxible/server/
instances/inst_<id>/`). In server mode, `state create-overlay` defaults its workspace
root to the current directory, so you do **not** need to pass `--root-dir` for
normal onboarding. Do not create a local `.cruxible/` directory.

If no instance exists:

```
cruxible state create-overlay --state-ref kev-reference --kit kev-triage
```

This materializes the local local overlay into the workspace root, including
`config.yaml`, `providers.py`, and `data/seed/`. That is the local-side kit
overlay, not the published reference config. Do not replace `data/seed/` before
this command unless you are prepared for the kit materialization to overwrite
those local files.

Capture the returned `instance_id` and persist it with `cruxible context use
<instance_id>` so subsequent commands target it.

Narrate briefly: "Created a KEV local from the published upstream state with the
`kev-triage` overlay kit. Daemon owns state at
`~/.cruxible/server/instances/<instance_id>/`."

### 5. Check for newer upstream reference releases

The onboarding path should consume the published upstream KEV state, not
rebuild it locally. A fresh `state create-overlay` already materializes the current
published KEV release into the local. This step checks whether a newer release
needs to be pulled into the instance:

```
cruxible state status
cruxible state pull-preview
```

If `cruxible state status` shows that the instance is not tracking a published
upstream KEV state, stop and fix that first. Do not continue onboarding on a
local-only reference path.

Inspect the `state pull-preview` output before applying. At minimum, check and
narrate:

- current release
- target release
- compatibility
- upstream entity and edge deltas
- any warnings
- any conflicts

If the preview shows conflicts, stop. If compatibility or delta size is
unexpected for this onboarding pass, stop and ask before applying.

If the preview reports "Already at latest pulled release", narrate that and
continue without applying. Otherwise, if the preview looks clean, use the
`apply_digest` returned by `state pull-preview` in:

```
cruxible state pull-apply --apply-digest <digest>
```

If apply fails because the head moved, rerun `state pull-preview` and apply the
new digest. Do not switch to a local reference rebuild path during onboarding.

Narrate with real counts pulled from `cruxible stats --json`:
"Published KEV reference is ready in the local — N vulnerabilities, M products,
V vendors."

### 6. Build local state

Skip this step if `cruxible stats` from Step 1 shows local entities
(`Asset`, `Owner`, `BusinessService`) already loaded.

Run the canonical local build directly:

```
cruxible lock --force
cruxible run --workflow build_local_state --apply
```

`lock --force` is required if you replaced local seed files after the local was
created. It accepts the current on-disk artifact hash into the workflow lock.
For the checked-in demo seed data it is safe but not normally necessary.

Narrate: "Local state loaded — X assets, Y owners, Z services, plus whichever
optional surfaces (controls, exceptions, patch windows, review evidence) were
included to support the kept surface."

### 7. Walk the proposal chain with review gates

The four proposal workflows form a dependency chain: each one reads approved
edges from the previous one. They cannot run back-to-back without review in
between.

Use `cruxible propose` (not `cruxible run`) — `propose` bridges the workflow
output into a candidate group; `run` only executes and returns the payload.

At the start of this step, choose the onboarding mode with the user:

- **Fast onboarding** — the default recommendation. The agent may approve
  clean, expected stages after summarizing them, and should stop only when a
  stage looks surprising or ambiguous.
- **Guided onboarding** — pause for explicit user approval at each unresolved
  stage before continuing.

In both modes, walk the chain one stage at a time. After each `propose`,
inspect the resulting group and summarize:

- relationship type
- member count
- thesis / purpose of the stage
- a few representative members
- anything notable about tri-state signals, evidence, or scope

Do **not** force per-member narration unless the user asks for it.
Do **not** add or ask users to approve numeric confidence properties on
governed relationships. This kit represents confidence through declared
tri-state signals (`support`, `unsure`, `contradict`) plus evidence text and
thesis facts. Internal provider match scores are only workflow inputs for
signal mapping; they should not appear in proposal properties or accepted graph
relationships.

In **fast onboarding**, stop and ask before approving if any of these happen:

- the group is much larger or smaller than expected
- the group is empty when the upstream data should support it
- representative members look wrong for the user's domain
- signals/evidence look weak or contradictory
- a stage would create surprising blast radius downstream

If none of those conditions hold, the agent may approve the stage and continue.

**Stage 1 — asset_runs_product:**

```
cruxible propose --workflow propose_asset_products --json
```

Capture the returned `group_id`, then inspect it:

```
cruxible group get --group <group_id>
cruxible group status --group <group_id> --json
```

This is usually the stage that deserves the closest look, because it is the
fuzzy bridge from internal software inventory into the reference product
state.

In guided onboarding, ask the user to approve. In fast onboarding, approve if
the summary looks clean and expected.

When approving:

```
cruxible group resolve --group <group_id> --action approve \
  --source agent --expected-pending-version <n> --rationale "<reason>"
```

Use the `pending_version` surfaced by `group get` / `group status` as `<n>`.

Do not proceed to Stage 2 until this group is resolved. If the user rejects,
explain that Stage 2 depends on approved `asset_runs_product` edges and
will fail with `Members list must not be empty` if run now.

**Stages 2-3** follow the same pattern:

- Stage 2: `cruxible propose --workflow propose_asset_exposure`
  (depends on approved Stage 1 edges and reference vulnerability-product edges)
- Stage 3: `cruxible propose --workflow propose_exposure_reconciliation`
  (depends on approved Stage 1 edges, reference vulnerability-product edges,
  and existing accepted exposure state)

For these stages:

- in guided onboarding, pause for explicit user approval at each unresolved
  stage
- in fast onboarding, summarize and continue unless something looks off

This is still the observable governance loop, but onboarding should optimize
for proving the chain works cleanly on the user's data rather than forcing
maximum ceremony when the stages are behaving as expected.

### 8. Verify the governed outputs and kept query surface

Named queries all require parameters. Before running them, list the final
query surface and its entry points:

```
cruxible query list --json
```

For each named query, inspect its required params and example IDs with:

```
cruxible query describe --query <query_name> --json
```

Then run each query using `--count --json` (returns only the total so the
output stays inspectable):

```
cruxible query --query <query_name> --param <key>=<value> --count --json
```

Confirm each kept query executes successfully. For queries tied to loaded
data, confirm at least one representative non-empty result.

If a kept query that should have data returns empty, stop and diagnose before
moving on:

- If the user's data doesn't support that query, go back to Step 3 and remove
  or modify it, then re-run the affected steps
- If an upstream proposal workflow hasn't produced the needed edges, approve
  a related proposal (or adjust the proposal workflow) and retry
- Do not ship an onboarded local with a query that should have data but still
  returns empty — either the query or the data needs to change

If a kept query is expected to be empty for this dataset, note that explicitly
in the hand-off instead of treating it as a failure.

Run a subject-context query against at least one onboarded subject (a user
asset or service) and confirm the linked context comes back correctly, e.g.:

```
cruxible query run asset_context --param asset_id <one of the user's assets>
```

### 9. Hand off

Tell the user concisely:

- The local is live; the proposal chain walked through with reviewer approval
  at each stage
- To exercise the ongoing daily loop on this local (waiver intake, control
  coverage inspection, remediation verification, daily triage summary), run
  `kev-triage`
  next
- If they want to keep adapting the kit, the next likely changes are provider
  templates, input-file shapes, workflow params, kept workflows, and named
  queries

Approvals issued during Step 7 were user-directed governance decisions. Do
not issue further approvals outside that flow.

## When to stop and ask

- Daemon not reachable. Stop; tell the user how to start one.
- Input files cannot be normalized into stable KEV surfaces. Stop and switch
  to `prepare-data` or `create-state`.
- Required input surfaces for the kept queries or workflows are missing.
  Stop and name the missing source (or go back to Step 3 and remove or modify
  the surfaces that depend on it).
- The state is partially built. Resume from the first missing milestone rather
  than restarting blindly.
- Any `run` / `apply` command fails. Surface the full error and stop — do
  not retry blindly. Common cause: artifact hash drift (see step 4) or
  missing seed data.
- Review queue is empty after the proposal chain. Something upstream didn't
  produce candidates; surface it. Likely causes: empty software inventory,
  schema drift, reference layer not built.
- Any kept query that should have data returns empty after proposals were
  created. Go back to Step 3 to remove or modify the query, or fix the data
  or proposal workflow that should have supplied its results. Do not proceed
  to hand-off with unexplained empty results for supported queries.
- Any kept workflow cannot build or propose cleanly against the user's data.
  Go back to Step 3 and either modify that workflow or remove it from the
  final supported surface for this pass.

## What this skill does NOT do

- Does not redesign the KEV ontology. This skill assumes the KEV kit shape is
  broadly correct and focuses on adapting data to it.
- Does not create a new state from scratch. Use the general `create-state`
  skill for that.
- Does not force every optional KEV data surface on the local. Surfaces are
  required only for queries and workflows the user chooses to keep in Step 3.
- Does not approve or reject proposals outside the Step 7 walk-through.
  Approvals in Step 7 are the user's governance decisions, issued one stage
  at a time with explicit consent.
- Does not optimize the full daily triage loop. That is the role of
  `kev-triage` once the local is producing sane results.

## Next

Once the local data is producing sane proposals and query results, run
`kev-triage` on the same instance to exercise the ongoing daily loop and
the richer evidence/outcome story.
