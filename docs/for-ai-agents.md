# Guide For AI Agents

This guide is the operating playbook for agents that use Cruxible Core. The
agent supplies interpretation and planning. Cruxible supplies deterministic
execution, governed state transitions, receipts, traces, review groups, and
query surfaces.

For `0.2`, prefer a local `cruxible-server` daemon. MCP should be a structured
adapter over the daemon, not the place where workflow policy lives.

## Runtime Boundary

Use this split when permissions matter:

- Daemon environment: `pip install "cruxible-core[server,mcp]"`
- Agent/client environment: `pip install cruxible-client`
- Agent access path: MCP or HTTP client
- State path: daemon-owned `CRUXIBLE_SERVER_STATE_DIR`, outside the agent
  workspace

Permission modes are enforced at the daemon boundary. If the agent can import
`cruxible-core`, read daemon state files, or control the daemon runtime, local
permission modes are advisory.

Recommended agent mode:

```bash
CRUXIBLE_MODE=governed_write
CRUXIBLE_SERVER_URL=http://127.0.0.1:8100
```

Use `admin` only for bootstrap, lock regeneration, canonical apply, and explicit
operator-approved maintenance.

## Core Responsibilities

The agent should:

- read the kit README, generated config views, and source artifacts
- edit config and provider code when authoring or customizing kits
- run validation, lock, workflow preview, proposal, and query tools
- explain receipts, traces, pending groups, and resolution choices to humans
- collect human decisions and apply them through Cruxible surfaces

The agent should not:

- write graph state by editing SQLite, snapshots, or graph files directly
- treat chat notes as accepted operational state
- bypass governed proposal workflows for relationship judgments
- use legacy `ingest` as the default path for new configs

## Standard Lifecycle

Use this lifecycle for existing kits:

```text
read kit docs
  -> validate config
  -> lock workflows after changes
  -> refresh canonical state by preview/apply
  -> run proposal workflows
  -> inspect pending groups
  -> resolve or defer proposals
  -> query accepted state
  -> inspect receipts/traces
```

Use this lifecycle for new or customized kits:

```text
inspect source data
  -> define config schema and contracts
  -> add providers only where source adaptation or domain policy is needed
  -> use common step types for generic row mechanics
  -> validate
  -> lock
  -> run workflow tests or focused previews
  -> regenerate generated docs/readme blocks
```

When authoring graph schemas, keep configs compact: entity and relationship
properties default to `type: string` and optional, `{}` is valid for optional
string fields, and `required: true` is the positive form for required non-ID
fields. Contract fields are different: they still need explicit `type`.

## Recipe: Validate And Lock After Edits

Use this after changing `config.yaml`, provider refs, provider code, artifacts,
contracts, workflows, or decision policies.

CLI:

```bash
cruxible --server-url http://127.0.0.1:8100 validate --config config.yaml
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> lock
```

MCP:

```text
cruxible_validate(config_path="config.yaml")
cruxible_lock_workflow(instance_id)
```

If locking fails, inspect the named provider, artifact, contract, or workflow
step in the error. Do not run workflows from an unlocked or stale config.

## Recipe: Refresh Canonical State

Canonical workflows mutate accepted state only after preview verification.

CLI:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> run \
  --workflow build_local_state \
  --save-preview preview.json

cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> apply \
  --preview-file preview.json
```

MCP:

```text
preview = cruxible_run_workflow(instance_id, "build_local_state")
cruxible_apply_workflow(
  instance_id,
  "build_local_state",
  expected_apply_digest=preview.apply_digest,
  expected_head_snapshot_id=preview.head_snapshot_id,
)
```

Before apply, summarize the changed entities/relationships, receipt ID, trace
IDs, and any warnings. If the source artifact changed unexpectedly, stop and ask
for operator confirmation.

## Recipe: Run A Proposal Workflow

Use proposal workflows for relationship state that needs review, evidence, or
classification. The workflow output is bridged into a candidate group.

CLI:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> propose \
  --workflow propose_asset_exposure
```

MCP:

```text
cruxible_propose_workflow(instance_id, "propose_asset_exposure")
```

If no group is created, check the workflow output status first. Some proposal
workflows intentionally complete without creating a group when there are no
candidates; those return `status: no_candidates` and `group_created: false`.
Treat that as a terminal "nothing to review" outcome, not as a failed proposal.
For other no-group outcomes, inspect suppressed members and prerequisite state.
In KEV triage, for example, asset exposure proposals depend on accepted
asset-product mappings and public vulnerability-product reference state.

## Recipe: Inspect A Pending Group

Always inspect the group before resolving it.

CLI:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> group list \
  --status pending_review
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> group get \
  --group <group-id>
```

MCP:

```text
cruxible_list_groups(instance_id, status="pending_review")
cruxible_get_group(instance_id, group_id)
```

Present:

- thesis and thesis facts
- relationship type and member count
- member-level signals: support, unsure, contradict
- review priority
- pending version
- source workflow receipt and trace IDs
- suppressed members or prior resolution history when present

## Recipe: Resolve Or Defer A Proposal

Resolve only from the pending version the reviewer saw.

CLI:

```bash
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> group resolve \
  --group <group-id> \
  --action approve \
  --expected-pending-version <pending-version> \
  --rationale "Reviewed evidence and accepted the proposal"
```

MCP:

```text
cruxible_resolve_group(
  instance_id,
  group_id,
  action="approve",
  expected_pending_version=pending_version,
  rationale="Reviewed evidence and accepted the proposal",
)
```

Use rejection when the proposal is wrong. Use no action when evidence is not
ready. Do not create accepted edges manually just to skip group review.

## Recipe: Debug Provider Failure

When a workflow fails:

1. Capture the workflow name, step ID, provider name, receipt ID if present,
   and trace IDs if present.
2. Inspect the provider declaration and contracts in the generated config view.
3. Check artifact names and hashes against the lock.
4. Re-run with the smallest input payload that reproduces the failure.
5. Fix the provider or config, then validate and lock again.

Useful commands:

```bash
cruxible config-views --config config.yaml --runtime --view workflow-steps
cruxible config-views --config config.yaml --runtime --view signal-policy-catalog
cruxible --server-url http://127.0.0.1:8100 --instance-id <instance-id> decision-record events \
  --trace <trace-id>
```

Receipts prove how a query or state transition was decided. Execution traces
prove what provider ran, with which provider version, artifact hash, inputs,
outputs, status, error, and timing.

## Recipe: Update Source Data Safely

When a source artifact changes:

1. Confirm the file path belongs to the kit or local workspace.
2. Validate the config.
3. Regenerate the workflow lock. Use `--force` only when intentionally accepting
   new live canonical artifact hashes.
4. Run the canonical workflow in preview mode.
5. Summarize the changed examples and receipt/trace evidence.
6. Apply only after the operator accepts the preview.
7. Run dependent proposal workflows and inspect new or refreshed groups.

Do not edit SQLite or graph snapshots to "fix" source state.

## Recipe: Regenerate Kit Docs

Generated kit README blocks are code-owned. After changing a kit config, refresh
the marked blocks:

```bash
cruxible config-views --config kits/kev-triage/config.yaml --runtime \
  --update-readme kits/kev-triage/README.md
```

For a full local wiki:

```bash
cruxible render-wiki --output wiki --scope local
```

The generated docs are grounding material for the agent and reviewer. They are
not a substitute for MCP/CLI review actions.

## Modeling Guidance

Use Cruxible for shared operational truth:

- accepted facts and relationships
- governed judgments and review history
- deterministic workflow outputs
- receipts, traces, decision records, feedback, and outcomes

Keep temporary reasoning in the agent. Commit only state that future agents,
humans, or software should rely on.

Use providers for source adapters, external services, model calls, and
domain-specific policy. Use built-in step types for generic deterministic
mechanics such as shaping rows, joining item sets, filtering, deduping, building
graph objects, and applying canonical state.

## Handoff Checklist

Before handing work back to a human or another agent, report:

- active instance ID and kit
- current config/lock status
- workflows run and whether they previewed, applied, or proposed
- receipt IDs and trace IDs for meaningful operations
- pending groups requiring review
- accepted state changed
- rejected/deferred proposals and rationale
- next safe command to run
