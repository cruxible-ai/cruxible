# CLI Reference

This is the full searchable reference for the `cruxible` command line. Walkthroughs and agent recipes live elsewhere; this file is intentionally detailed so an agent can look up command names, flags, side effects, and failure modes without shelling out to `--help` first.

## Runtime Model

- Use `--server-url` or `--server-socket` for daemon transport, and
  `--instance-id` or CLI context for daemon-backed instances.
- The CLI context commands remember transport and the active instance for shell
  users; MCP does not use CLI context.
- Commands that mutate governed state are blocked locally when the command requires a daemon surface.
- `init --kit` accepts standalone kits. Overlay kits are created with `world create-overlay --kit`.
- `run` rejects proposal workflows; use `propose` for workflows that return governed relationship proposals.
- `explain` and `export edges` are direct-local file/rendering utilities. Use receipts and list/query tools for daemon/MCP flows.

## cruxible add-constraint

**Usage:** `cruxible add-constraint [OPTIONS]`

**Purpose:** Add a constraint rule to the config.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--name` | yes | `Sentinel.UNSET` | text | Constraint name. |
| `--rule` | yes | `Sentinel.UNSET` | text | Constraint rule expression. |
| `--severity` | no | `warning` | choice | Severity level (default: warning). |
| `--description` | no | `` | text | Optional description. |

**Output And Side Effects:**
- Read-only. Returns group metadata, members, optional resolution, bucket lifecycle status, and member review state showing proposed tuples, current edge counts, current edge details when unambiguous, and property deltas.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for read operations.
- Group ID not found.

## cruxible add-decision-policy

**Usage:** `cruxible add-decision-policy [OPTIONS]`

**Purpose:** Add a decision policy to the config.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--name` | yes | `Sentinel.UNSET` | text | Decision policy name. |
| `--applies-to` | yes | `Sentinel.UNSET` | choice | Policy application surface. |
| `--relationship` | yes | `Sentinel.UNSET` | text | Relationship type. |
| `--effect` | yes | `Sentinel.UNSET` | choice | Policy effect. |
| `--query-name` | no | `` | text | Named query for query policies. |
| `--workflow-name` | no | `` | text | Workflow name for workflow policies. |
| `--match` | no | `{}` | text | JSON object for exact-match selectors. |
| `--description` | no | `` | text | Optional description. |
| `--rationale` | no | `` | text | Policy rationale. |
| `--expires-at` | no | `` | text | Optional ISO timestamp/date. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible add-entity

**Usage:** `cruxible add-entity [OPTIONS]`

**Purpose:** Add or update an entity in the graph.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--type` | yes | `Sentinel.UNSET` | text | Entity type. |
| `--id` | yes | `Sentinel.UNSET` | text | Entity ID. |
| `--props` | no | `` | text | JSON object of properties. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible add-relationship

**Usage:** `cruxible add-relationship [OPTIONS]`

**Purpose:** Add or update a relationship in the graph.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--from-type` | yes | `Sentinel.UNSET` | text | Source entity type. |
| `--from-id` | yes | `Sentinel.UNSET` | text | Source entity ID. |
| `--relationship` | yes | `Sentinel.UNSET` | text | Relationship type. |
| `--to-type` | yes | `Sentinel.UNSET` | text | Target entity type. |
| `--to-id` | yes | `Sentinel.UNSET` | text | Target entity ID. |
| `--props` | no | `` | text | JSON object of edge properties. |
| `--evidence-ref` | no | `` | text | JSON evidence ref object. Repeat to attach multiple refs. |
| `--source-evidence` | no | `` | text | JSON source-evidence locator. Repeat to attach multiple locators. |
| `--evidence-rationale` | no | `` | text | Optional rationale for the attached relationship evidence. |

**Output And Side Effects:**
- Writes live relationship state and records a mutation receipt. Evidence refs
  and source-evidence locators are persisted as relationship evidence metadata.
  Direct adds are not group-reviewed accepted relationships; use `group propose`
  and `group resolve --action approve` when review/acceptance state matters.

**Example:**

```bash
cruxible add-relationship \
  --from-type RoadmapItem \
  --from-id ri-compact-workflow-trace-payloads \
  --relationship roadmap_item_depends_on_roadmap_item \
  --to-type RoadmapItem \
  --to-id ri-transactional-sqlite-state \
  --source-evidence '{"source_artifact_id":"SRC-...","chunk_id":"CHK-..."}' \
  --evidence-rationale "Extracted from the P0 section."
```

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible batch-direct-write

**Usage:** `cruxible batch-direct-write --payload-file PATH [--dry-run] [--json]`

**Purpose:** Validate or apply one structured direct graph write payload containing
entities, relationships, and optional payload-local shared evidence.

**Payload Shape:**

```yaml
entities:
  - entity_type: RoadmapItem
    entity_id: ri-example
    properties:
      roadmap_item_id: ri-example
      title: Example roadmap item
relationships:
  - from_type: WorkItem
    from_id: wi-example
    relationship: work_item_implements_roadmap_item
    to_type: RoadmapItem
    to_id: ri-example
    shared_evidence_keys: [source_section]
    evidence_rationale: Extracted from the referenced section.
shared_evidence:
  source_section:
    source_evidence:
      - source_artifact_id: SRC-...
        chunk_id: mdchunk_...
```

**Output And Side Effects:**
- `--dry-run` validates entity properties, relationship endpoints/properties,
  evidence locators, duplicate IDs, and missing shared evidence keys without
  mutating graph state.
- Apply mode writes all valid entities and relationships through one mutation
  receipt and returns a compact summary. Direct writes are live/unreviewed
  state, not group-reviewed accepted state.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Payload file is not a JSON/YAML object.
- Unknown shared evidence key or invalid source-evidence locator.

## cruxible analyze-feedback

**Usage:** `cruxible analyze-feedback [OPTIONS]`

**Purpose:** Analyze structured feedback and print remediation suggestions.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--relationship` | yes | `Sentinel.UNSET` | text | Relationship type. |
| `--limit` | no | `200` | integer range | Rows to inspect. |
| `--min-support` | no | `5` | integer range | Minimum support for suggestions. |
| `--decision-surface-type` | no | `` | choice | Optional decision surface type filter. |
| `--decision-surface-name` | no | `` | text | Optional decision surface name filter. |
| `--pair` | no | `Sentinel.UNSET` | text | Explicit mismatch pair as FROM_PROP=TO_PROP. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible analyze-outcomes

**Usage:** `cruxible analyze-outcomes [OPTIONS]`

**Purpose:** Analyze structured outcomes and print trust/debugging suggestions.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--anchor-type` | yes | `Sentinel.UNSET` | choice | Outcome anchor type to analyze. |
| `--relationship` | no | `` | text | Relationship type. |
| `--workflow` | no | `` | text | Workflow name filter. |
| `--query` | no | `` | text | Query name filter. |
| `--surface-type` | no | `` | choice | Explicit surface type filter. |
| `--surface-name` | no | `` | text | Explicit surface name filter. |
| `--limit` | no | `200` | integer range | Rows to inspect. |
| `--min-support` | no | `5` | integer range | Minimum support for suggestions. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible apply

**Usage:** `cruxible apply [OPTIONS]`

**Purpose:** Commit a previously previewed canonical workflow after verifying the preview identity.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--workflow` | no | `` | text | Workflow name from config. |
| `--input` | no | `` | text | Inline JSON or YAML workflow input. |
| `--input-file` | no | `` | path | JSON or YAML file providing workflow input. |
| `--apply-digest` | no | `` | text | Preview apply digest from workflow run. |
| `--head-snapshot` | no | `` | text | Expected head snapshot ID from workflow preview. |
| `--preview-file` | no | `` | file | Read preview state from a file saved by run --save-preview. |
| `--decision-record` | no | `` | text | Decision record ID for audit logging. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible clone

**Usage:** `cruxible clone [OPTIONS]`

**Purpose:** Create a new local instance from a chosen snapshot.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--snapshot` | yes | `Sentinel.UNSET` | text | Snapshot ID to clone from. |
| `--root-dir` | yes | `Sentinel.UNSET` | text | Root directory for the new cloned instance. |
| `--activate / --no-activate` | no | `True` | boolean | Make the cloned server instance the active CLI context instance. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible config-views

**Usage:** `cruxible config-views [OPTIONS]`

**Purpose:** Render canonical Mermaid/Markdown views for a Cruxible config.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--config` | yes | `Sentinel.UNSET` | file | Path to config YAML file. |
| `--view` | no | `all` | choice | View to render. 'all' emits the standard config-drafting diagrams. |
| `--bare` | no | `False` | boolean | Emit the raw selected view without Markdown wrapping. |
| `--update-readme` | no | `Sentinel.UNSET` | file | Replace matching CRUXIBLE marker blocks in a README. |
| `--runtime` | no | `False` | boolean | Compose extends overlays as a runtime composed view. This includes inherited ontology/query surfaces but strips upstream build-only workflows. |

**Output And Side Effects:**
- Produces documentation or file output; graph state is not changed.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible context

**Usage:** `cruxible context [OPTIONS]`

**Purpose:** Manage remembered governed server and instance context.

**Subcommands:**

- `cruxible context clear` - Clear remembered governed CLI context.
- `cruxible context connect` - Persist the current governed transport and optional instance.
- `cruxible context show` - Show the remembered CLI context.
- `cruxible context use` - Set the active governed instance ID.

**Output And Side Effects:**
- Mutates only the remembered CLI context file, not graph state.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible context clear

**Usage:** `cruxible context clear [OPTIONS]`

**Purpose:** Clear remembered governed CLI context.

**Output And Side Effects:**
- Mutates only the remembered CLI context file, not graph state.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible context connect

**Usage:** `cruxible context connect [OPTIONS]`

**Purpose:** Persist the current governed transport and optional instance.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--server-url` | no | `` | text | Remote Cruxible server base URL. |
| `--server-socket` | no | `` | text | Local Cruxible server Unix socket path. |
| `--instance-id` | no | `` | text | Opaque server-mode instance ID. Defaults to remembered CLI context. |

**Output And Side Effects:**
- Mutates only the remembered CLI context file, not graph state.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible context show

**Usage:** `cruxible context show [OPTIONS]`

**Purpose:** Show the remembered CLI context.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Mutates only the remembered CLI context file, not graph state.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible context use

**Usage:** `cruxible context use [OPTIONS]`

**Purpose:** Set the active governed instance ID.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `instance_id` | yes | `Sentinel.UNSET` | text | Positional argument. |

**Output And Side Effects:**
- Mutates only the remembered CLI context file, not graph state.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible decision-record

**Usage:** `cruxible decision-record [OPTIONS]`

**Purpose:** Manage decision records and their logged receipts.

**Subcommands:**

- `cruxible decision-record abandon` - Abandon an open decision record.
- `cruxible decision-record create` - Create an open decision record.
- `cruxible decision-record events` - List decision-record events.
- `cruxible decision-record finalize` - Finalize an open decision record.
- `cruxible decision-record get` - Fetch one decision record.
- `cruxible decision-record list` - List decision records.

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible decision-record abandon

**Usage:** `cruxible decision-record abandon [OPTIONS]`

**Purpose:** Abandon an open decision record.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--id` | yes | `Sentinel.UNSET` | text | Decision record ID. |
| `--reason` | no | `` | text | Reason for abandoning the record. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible decision-record create

**Usage:** `cruxible decision-record create [OPTIONS]`

**Purpose:** Create an open decision record.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--question` | yes | `Sentinel.UNSET` | text | Question or decision being evaluated. |
| `--subject-type` | no | `` | text | Optional subject type. |
| `--subject-id` | no | `` | text | Optional subject identifier. |
| `--opened-by` | no | `human` | choice |  |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible decision-record events

**Usage:** `cruxible decision-record events [OPTIONS]`

**Purpose:** List decision-record events.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--id` | no | `` | text | Decision record ID. |
| `--receipt` | no | `` | text | Receipt ID. |
| `--trace` | no | `` | text | Trace ID. |
| `--status` | no | `` | choice |  |
| `--limit` | no | `100` | integer range |  |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible decision-record finalize

**Usage:** `cruxible decision-record finalize [OPTIONS]`

**Purpose:** Finalize an open decision record.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--id` | yes | `Sentinel.UNSET` | text | Decision record ID. |
| `--final-decision` | yes | `Sentinel.UNSET` | text | Final decision text. |
| `--decision-class` | yes | `Sentinel.UNSET` | choice |  |
| `--rationale` | no | `` | text | Decision rationale. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible decision-record get

**Usage:** `cruxible decision-record get [OPTIONS]`

**Purpose:** Fetch one decision record.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--id` | yes | `Sentinel.UNSET` | text | Decision record ID. |
| `--events, --no-events` | no | `True` | boolean |  |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible decision-record list

**Usage:** `cruxible decision-record list [OPTIONS]`

**Purpose:** List decision records.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--status` | no | `` | choice |  |
| `--subject-type` | no | `` | text |  |
| `--subject-id` | no | `` | text |  |
| `--decision-class` | no | `` | choice |  |
| `--limit` | no | `100` | integer range |  |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible evaluate

**Usage:** `cruxible evaluate [OPTIONS]`

**Purpose:** Assess graph quality: orphans, gaps, violations, unreviewed co-members.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--limit` | no | `100` | integer | Max findings to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible explain

**Usage:** `cruxible explain [OPTIONS]`

**Purpose:** Explain a query result using its receipt.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--receipt` | yes | `Sentinel.UNSET` | text | Receipt ID to explain. |
| `--format` | no | `markdown` | choice | Output format. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible export

**Usage:** `cruxible export [OPTIONS]`

**Purpose:** Export graph data to files.

**Subcommands:**

- `cruxible export edges` - Export all edges to CSV.

**Output And Side Effects:**
- Produces documentation or file output; graph state is not changed.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible export edges

**Usage:** `cruxible export edges [OPTIONS]`

**Purpose:** Export all edges to CSV.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--output, -o` | yes | `Sentinel.UNSET` | file | Output file path. |
| `--relationship` | no | `` | text | Filter by relationship type. |
| `--exclude-rejected` | no | `False` | boolean | Exclude edges with rejected review_status. |

**Output And Side Effects:**
- Produces documentation or file output; graph state is not changed.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible feedback

**Usage:** `cruxible feedback [OPTIONS]`

**Purpose:** Submit feedback on a specific edge by explicit relationship coordinates.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--receipt` | yes | `Sentinel.UNSET` | text | Receipt ID. |
| `--action` | yes | `Sentinel.UNSET` | choice | Feedback action. |
| `--from-type` | yes | `Sentinel.UNSET` | text | Source entity type. |
| `--from-id` | yes | `Sentinel.UNSET` | text | Source entity ID. |
| `--relationship` | yes | `Sentinel.UNSET` | text | Relationship type. |
| `--to-type` | yes | `Sentinel.UNSET` | text | Target entity type. |
| `--to-id` | yes | `Sentinel.UNSET` | text | Target entity ID. |
| `--edge-key` | no | `` | integer | Edge key (multi-edge disambiguation). |
| `--reason` | no | `` | text | Reason for feedback. |
| `--reason-code` | no | `` | text | Structured feedback reason code. |
| `--scope-hints` | no | `` | text | JSON object of structured scope hints. |
| `--corrections` | no | `` | text | JSON object of edge property corrections (for action=correct). |
| `--source` | no | `human` | choice | Who produced this feedback (default: human). |
| `--group-override` | no | `False` | boolean | Mark relationship assertion metadata as a group override (edge must exist). |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible feedback-from-query

**Usage:** `cruxible feedback-from-query [OPTIONS]`

**Purpose:** Submit edge-level feedback by selecting one relationship row or path segment from a query receipt.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--receipt` | yes | `Sentinel.UNSET` | text | Query receipt ID. |
| `--result-index` | yes | `Sentinel.UNSET` | integer | Zero-based index of the query result row to adjudicate. |
| `--action` | yes | `Sentinel.UNSET` | choice | Feedback action. |
| `--source` | no | `human` | choice | Who produced this feedback (default: human). |
| `--reason` | no | `` | text | Reason for feedback. |
| `--reason-code` | no | `` | text | Structured feedback reason code. |
| `--scope-hints` | no | `` | text | JSON object of structured scope hints. |
| `--corrections` | no | `` | text | JSON object of edge property corrections (for action=correct). |
| `--group-override` | no | `False` | boolean | Mark selected edge assertion metadata as a group override (edge must exist). |
| `--path-index` | no | `` | integer | Zero-based path segment index for path query rows. |
| `--path-alias` | no | `` | text | Traversal alias for the selected path segment. |

**Output And Side Effects:**
- Creates normal feedback records and feedback receipts through the existing edge-feedback path.
- Adjudicates one existing relationship assertion from query evidence. It does not resolve candidate groups.
- Use `cruxible group get --group <group_id>` and `cruxible group resolve --group <group_id> --action approve|reject --expected-pending-version <n>` when the decision is about a group thesis or member set.

**Common Errors:**
- The receipt is missing, is not a query receipt, or the result index is out of range.
- Entity-shaped query rows do not contain relationship evidence.
- Multi-hop path rows require exactly one of `--path-index` or `--path-alias`.
- The selected path alias is missing or duplicated, or the selected edge is no longer in the graph.

## cruxible feedback-batch

**Usage:** `cruxible feedback-batch [OPTIONS]`

**Purpose:** Submit a batch of edge feedback with one top-level receipt.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--items-file` | no | `` | path | JSON or YAML file with batch feedback items. |
| `--items` | no | `` | text | Inline JSON array of feedback items. |
| `--source` | no | `human` | choice | Who produced this feedback batch (default: human). |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible feedback-profile

**Usage:** `cruxible feedback-profile [OPTIONS]`

**Purpose:** Display the configured feedback profile for one relationship type.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--relationship` | yes | `Sentinel.UNSET` | text | Relationship type. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible get-entity

**Usage:** `cruxible get-entity [OPTIONS]`

**Purpose:** Look up a specific entity by type and ID.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--type` | yes | `Sentinel.UNSET` | text | Entity type. |
| `--id` | yes | `Sentinel.UNSET` | text | Entity ID. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible get-relationship

**Usage:** `cruxible get-relationship [OPTIONS]`

**Purpose:** Look up a specific relationship by its endpoints and type.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--from-type` | yes | `Sentinel.UNSET` | text | Source entity type. |
| `--from-id` | yes | `Sentinel.UNSET` | text | Source entity ID. |
| `--relationship` | yes | `Sentinel.UNSET` | text | Relationship type. |
| `--to-type` | yes | `Sentinel.UNSET` | text | Target entity type. |
| `--to-id` | yes | `Sentinel.UNSET` | text | Target entity ID. |
| `--edge-key` | no | `` | integer | Edge key (multi-edge disambiguation). |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible group

**Usage:** `cruxible group [OPTIONS]`

**Purpose:** Manage candidate groups for batch edge review.

**Subcommands:**

- `cruxible group get` - Get details of a candidate group.
- `cruxible group list` - List candidate groups.
- `cruxible group propose` - Propose a candidate group of edges for batch review.
- `cruxible group resolutions` - List group resolutions.
- `cruxible group resolve` - Resolve a candidate group (approve or reject).
- `cruxible group status` - Show lifecycle status for a signature bucket.
- `cruxible group trust` - Update trust status on a resolution.

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible group get

**Usage:** `cruxible group get [OPTIONS]`

**Purpose:** Get details of a candidate group.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--group` | yes | `Sentinel.UNSET` | text | Group ID. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible group list

**Usage:** `cruxible group list [OPTIONS]`

**Purpose:** List candidate groups.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--relationship` | no | `` | text | Filter by relationship type. |
| `--status` | no | `` | choice | Filter by status. |
| `--limit` | no | `50` | integer | Max groups to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible group propose

**Usage:** `cruxible group propose [OPTIONS]`

**Purpose:** Propose a candidate group of edges for batch review.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--relationship` | yes | `Sentinel.UNSET` | text | Relationship type for the group. |
| `--members-file` | no | `` | path | JSON file with member list. |
| `--members` | no | `` | text | Inline JSON array of members. |
| `--thesis` | no | `` | text | Human-readable thesis text. |
| `--thesis-facts` | no | `` | text | Optional JSON object used as agent-supplied direct proposal scope. |
| `--analysis-state` | no | `` | text | JSON object of opaque analysis state. |
| `--signal-source` | no | `Sentinel.UNSET` | text | Signal source name used in this proposal. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible group resolutions

**Usage:** `cruxible group resolutions [OPTIONS]`

**Purpose:** List group resolutions.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--relationship` | no | `` | text | Filter by relationship type. |
| `--action` | no | `` | choice | Filter by action. |
| `--limit` | no | `50` | integer | Max resolutions to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible group resolve

**Usage:** `cruxible group resolve [OPTIONS]`

**Purpose:** Resolve a candidate group (approve or reject).

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--group` | yes | `Sentinel.UNSET` | text | Group ID to resolve. |
| `--action` | yes | `Sentinel.UNSET` | choice | Resolution action. |
| `--rationale` | no | `` | text | Rationale for this resolution. |
| `--source` | no | `human` | choice | Who resolved (default: human). |
| `--expected-pending-version` | yes | `Sentinel.UNSET` | integer | Pending version the reviewer saw when deciding. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible group status

**Usage:** `cruxible group status [OPTIONS]`

**Purpose:** Show lifecycle status for a signature bucket.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--group` | no | `` | text | Concrete group ID. |
| `--signature` | no | `` | text | Signature bucket ID. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible group trust

**Usage:** `cruxible group trust [OPTIONS]`

**Purpose:** Update trust status on a resolution.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--resolution` | yes | `Sentinel.UNSET` | text | Resolution ID. |
| `--status` | yes | `Sentinel.UNSET` | choice | Trust status to set. |
| `--reason` | no | `` | text | Reason for trust status change. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible init

**Usage:** `cruxible init [OPTIONS]`

**Purpose:** Initialize a new instance or governed server-backed workspace.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--config` | no | `` | text | Path to config YAML file. |
| `--kit` | no | `` | text | Standalone kit alias or ref to materialize. |
| `--root-dir` | no | `` | text | Workspace root for config/artifact provenance (defaults to current directory). |
| `--data-dir` | no | `` | text | Directory for data files. |
| `--activate / --no-activate` | no | `True` | boolean | Make a new server instance the active CLI context instance. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible inspect

**Usage:** `cruxible inspect [OPTIONS]`

**Purpose:** Inspect entities plus canonical read-only system views.

**Subcommands:**

- `cruxible inspect entity` - Inspect an entity and its immediate neighbors.
- `cruxible inspect governance` - Show the canonical governance view for the current instance.
- `cruxible inspect ontology` - Show the canonical ontology view for the current instance config.
- `cruxible inspect overview` - Show the generated config overview built from canonical views.
- `cruxible inspect queries` - Show the canonical query view for the current instance config.
- `cruxible inspect relationship-lineage` - Inspect a relationship's stored provenance lineage.
- `cruxible inspect trace` - Inspect a provider execution trace by ID.
- `cruxible inspect workflows` - Show the canonical workflow view for the current instance config.

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible inspect entity

**Usage:** `cruxible inspect entity [OPTIONS]`

**Purpose:** Inspect an entity and its immediate neighbors.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--type` | yes | `Sentinel.UNSET` | text | Entity type. |
| `--id` | yes | `Sentinel.UNSET` | text | Entity ID. |
| `--direction` | no | `both` | choice | Neighbor traversal direction. |
| `--relationship` | no | `` | text | Optional relationship filter. |
| `--limit` | no | `` | integer range | Max neighbors to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible inspect governance

**Usage:** `cruxible inspect governance [OPTIONS]`

**Purpose:** Show the canonical governance view for the current instance.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--format` | no | `markdown` | choice | Output format. |
| `--limit` | no | `200` | integer range | Max pending groups and resolutions to inspect. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible inspect trace

**Usage:** `cruxible inspect trace [OPTIONS] TRACE_ID`

**Purpose:** Inspect a provider execution trace by ID.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `trace_id` | yes | `Sentinel.UNSET` | text | Positional argument. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only. Returns the persisted provider execution trace, including provider metadata, retained input/output payload fields, payload digest/size metadata, status, timings, and error details when present. Payload fields follow the instance config's `runtime.trace_payloads` retention policy.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Trace ID not found.
- Permission mode too low for read operations.

## cruxible inspect relationship-lineage

**Usage:** `cruxible inspect relationship-lineage [OPTIONS]`

**Purpose:** Inspect a relationship's stored provenance lineage.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--from-type` | yes | `Sentinel.UNSET` | text | Source entity type. |
| `--from-id` | yes | `Sentinel.UNSET` | text | Source entity ID. |
| `--relationship` | yes | `Sentinel.UNSET` | text | Relationship type. |
| `--to-type` | yes | `Sentinel.UNSET` | text | Target entity type. |
| `--to-id` | yes | `Sentinel.UNSET` | text | Target entity ID. |
| `--edge-key` | no | `` | integer | Edge key (multi-edge disambiguation). |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only. Returns the matching relationship, `_provenance`, linked proposal group/resolution when provenance points to a group, source workflow receipt ID, source trace IDs, and warnings for missing or non-group provenance.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for read operations.
- Ambiguous relationship tuple without `--edge-key`.

## cruxible inspect ontology

**Usage:** `cruxible inspect ontology [OPTIONS]`

**Purpose:** Show the canonical ontology view for the current instance config.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--format` | no | `markdown` | choice | Output format. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible inspect overview

**Usage:** `cruxible inspect overview [OPTIONS]`

**Purpose:** Show the generated config overview built from canonical views.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--format` | no | `markdown` | choice | Output format. |
| `--limit` | no | `200` | integer range | Max pending groups and resolutions to inspect. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible inspect queries

**Usage:** `cruxible inspect queries [OPTIONS]`

**Purpose:** Show the canonical query view for the current instance config.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--format` | no | `markdown` | choice | Output format. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible inspect workflows

**Usage:** `cruxible inspect workflows [OPTIONS]`

**Purpose:** Show the canonical workflow view for the current instance config.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--format` | no | `markdown` | choice | Output format. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible lint

**Usage:** `cruxible lint [OPTIONS]`

**Purpose:** Run the aggregate read-only corpus lint pass.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--max-findings` | no | `100` | integer | Max graph findings to include. |
| `--analysis-limit` | no | `200` | integer | Rows to inspect for feedback and outcome analysis. |
| `--min-support` | no | `5` | integer | Minimum support for lint suggestions. |
| `--exclude-orphan-type` | no | `Sentinel.UNSET` | text | Entity type to exclude from orphan checks. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible list

**Usage:** `cruxible list [OPTIONS]`

**Purpose:** List entities, receipts, or feedback.

**Subcommands:**

- `cruxible list edges` - List edges in the graph.
- `cruxible list entities` - List entities of a given type.
- `cruxible list feedback` - List feedback records.
- `cruxible list outcomes` - List outcome records.
- `cruxible list receipts` - List receipt summaries.
- `cruxible list traces` - List provider execution trace summaries.

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible list edges

**Usage:** `cruxible list edges [OPTIONS]`

**Purpose:** List edges in the graph.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--relationship` | no | `` | text | Filter by relationship type. |
| `--limit` | no | `50` | integer | Max edges to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible list entities

**Usage:** `cruxible list entities [OPTIONS]`

**Purpose:** List entities of a given type.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--type` | yes | `Sentinel.UNSET` | text | Entity type to list. |
| `--limit` | no | `50` | integer | Max entities to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible list feedback

**Usage:** `cruxible list feedback [OPTIONS]`

**Purpose:** List feedback records.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--receipt` | no | `` | text | Filter by receipt ID. |
| `--limit` | no | `50` | integer | Max records to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible list outcomes

**Usage:** `cruxible list outcomes [OPTIONS]`

**Purpose:** List outcome records.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--receipt` | no | `` | text | Filter by receipt ID. |
| `--limit` | no | `50` | integer | Max records to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible list receipts

**Usage:** `cruxible list receipts [OPTIONS]`

**Purpose:** List receipt summaries.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--query-name` | no | `` | text | Filter by query name. |
| `--operation-type` | no | `` | text | Filter by operation type. |
| `--limit` | no | `50` | integer | Max receipts to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible list traces

**Usage:** `cruxible list traces [OPTIONS]`

**Purpose:** List provider execution trace summaries.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--workflow` | no | `` | text | Filter by workflow name. |
| `--provider` | no | `` | text | Filter by provider name. |
| `--limit` | no | `100` | integer range | Max traces to show. |
| `--offset` | no | `0` | integer range | Rows to skip. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only. Returns trace summary rows with trace ID, workflow, step, provider, runtime, and creation time.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for read operations.
- Invalid limit or offset.

## cruxible lock

**Usage:** `cruxible lock [OPTIONS]`

**Purpose:** Generate a workflow lock file for the current instance config.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--force` | no | `False` | boolean | Accept live canonical artifact hashes when regenerating the lock. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible outcome

**Usage:** `cruxible outcome [OPTIONS]`

**Purpose:** Record the outcome of a decision.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--receipt` | yes | `Sentinel.UNSET` | text | Receipt ID. |
| `--outcome` | yes | `Sentinel.UNSET` | choice | Outcome of the decision. |
| `--detail` | no | `` | text | JSON string with outcome details. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible outcome-profile

**Usage:** `cruxible outcome-profile [OPTIONS]`

**Purpose:** Display the configured outcome profile for one anchor context.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--anchor-type` | yes | `Sentinel.UNSET` | choice | Anchor type to resolve. |
| `--relationship` | no | `` | text | Relationship type. |
| `--workflow` | no | `` | text | Workflow name. |
| `--surface-type` | no | `` | choice | Receipt surface type. |
| `--surface-name` | no | `` | text | Receipt surface name. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible plan

**Usage:** `cruxible plan [OPTIONS]`

**Purpose:** Compile a workflow plan for the current instance.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--workflow` | yes | `Sentinel.UNSET` | text | Workflow name from config. |
| `--input` | no | `` | text | Inline JSON or YAML workflow input. |
| `--input-file` | no | `` | path | JSON or YAML file providing workflow input. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible propose

**Usage:** `cruxible propose [OPTIONS]`

**Purpose:** Execute a `type: proposal` workflow and bridge its output into a candidate group.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--workflow` | yes | `Sentinel.UNSET` | text | Workflow name from config. |
| `--input` | no | `` | text | Inline JSON or YAML workflow input. |
| `--input-file` | no | `` | path | JSON or YAML file providing workflow input. |
| `--decision-record` | no | `` | text | Decision record ID for audit logging. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible query

**Usage:** `cruxible query [OPTIONS]`

**Purpose:** Execute a named query, or discover the query surfaces on this instance.

**Subcommands:**

- `cruxible query describe` - Describe one named query with required params and example IDs.
- `cruxible query inline` - Execute a bounded inline query definition for exploration.
- `cruxible query list` - List named queries with entry points and required params.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--query` | no | `Sentinel.UNSET` | text | Named query from config. |
| `--param` | no | `Sentinel.UNSET` | text | Query parameter as KEY=VALUE. |
| `--limit` | no | `` | integer range | Max results to display. |
| `--relationship-state` | no | `` | choice | Override query relationship visibility state: `live`, `accepted`, `pending`, or `reviewable`. The named query must set `allow_relationship_state_override: true`. |
| `--count` | no | `False` | boolean | Show only summary metadata. |
| `--decision-record` | no | `` | text | Decision record ID for audit logging. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible query inline

**Usage:** `cruxible query inline [OPTIONS]`

**Purpose:** Execute a bounded inline query definition without persisting it to config.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--definition-json` | no | `` | text | Inline query definition as a JSON object. |
| `--definition-file` | no | `` | path | Path to a JSON or YAML inline query definition. |
| `--param` | no | `Sentinel.UNSET` | text | Query parameter as KEY=VALUE. |
| `--limit` | no | `` | integer range | Max results to display. |
| `--relationship-state` | no | `` | choice | Override query relationship visibility state: `live`, `accepted`, `pending`, or `reviewable`. The inline definition must set `allow_relationship_state_override: true`. |
| `--count` | no | `False` | boolean | Show only summary metadata. |
| `--decision-record` | no | `` | text | Decision record ID for audit logging. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Example:**

```bash
cruxible query inline \
  --definition-json '{"name":"brake_parts","mode":"collection","returns":"Part","result_shape":"entity","where":{"result.properties.category":{"eq":"brakes"}}}' \
  --json
```

**Output And Side Effects:**
- Read-only graph access. Inline queries persist query receipts and optional
  decision events, but they do not modify or persist config.

**Common Errors:**
- Provide exactly one of `--definition-json` or `--definition-file`.
- Inline query definitions use the same shape as configured named queries plus
  required `name`; repeated or workflow-critical inline queries should be
  promoted into config as named queries.

## cruxible query describe

**Usage:** `cruxible query describe [OPTIONS]`

**Purpose:** Describe one named query with required params and example IDs.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--query` | yes | `Sentinel.UNSET` | text | Named query from config. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible query list

**Usage:** `cruxible query list [OPTIONS]`

**Purpose:** List named queries with entry points and required params.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible reload-config

**Usage:** `cruxible reload-config [OPTIONS]`

**Purpose:** Validate the active config or repoint the instance to a new config file.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--config` | no | `` | text | Optional new config path. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible render-wiki

**Usage:** `cruxible render-wiki [OPTIONS]`

**Purpose:** Render a deterministic Markdown wiki from the current world state.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--output` | yes | `Sentinel.UNSET` | directory | Directory to write the rendered wiki into. |
| `--focus` | no | `Sentinel.UNSET` | text | Render a focused wiki around EntityType:EntityId. |
| `--include-type` | no | `Sentinel.UNSET` | text | Limit rendered subject pages to specific entity types. |
| `--scope` | no | `` | choice | Wiki projection scope. Defaults to local for CLI renders. |
| `--max-per-type` | no | `50` | integer range | Maximum subject links or high-fanout neighbors to render per type. |
| `--all-subjects` | no | `False` | boolean | Deprecated alias for --scope all. |

**Output And Side Effects:**
- Produces documentation or file output; graph state is not changed.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible run

**Usage:** `cruxible run [OPTIONS]`

**Purpose:** Execute a workflow for the current instance. Canonical workflows run as previews and return an `apply_digest` plus `head_snapshot_id`; use `cruxible apply` to commit them. For `type: proposal` workflows, use `cruxible propose` instead.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--workflow` | yes | `Sentinel.UNSET` | text | Workflow name from config. |
| `--input` | no | `` | text | Inline JSON or YAML workflow input. |
| `--input-file` | no | `` | path | JSON or YAML file providing workflow input. |
| `--save-preview` | no | `` | file | Save preview state to a JSON file for use with apply --preview-file. |
| `--decision-record` | no | `` | text | Decision record ID for audit logging. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible sample

**Usage:** `cruxible sample [OPTIONS]`

**Purpose:** Show a sample of entities of a given type.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--type` | yes | `Sentinel.UNSET` | text | Entity type to sample. |
| `--limit` | no | `5` | integer | Number of entities to show. |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible schema

**Usage:** `cruxible schema [OPTIONS]`

**Purpose:** Display the config schema for this instance.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible server

**Usage:** `cruxible server [OPTIONS]`

**Purpose:** Inspect live daemon state.

**Subcommands:**

- `cruxible server info` - Show live daemon metadata such as agent mode and state dir.

**Output And Side Effects:**
- Command-specific output only.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible server info

**Usage:** `cruxible server info [OPTIONS]`

**Purpose:** Show live daemon metadata such as agent mode and state dir.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Command-specific output only.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible snapshot

**Usage:** `cruxible snapshot [OPTIONS]`

**Purpose:** Manage immutable world-model snapshots.

**Subcommands:**

- `cruxible snapshot create` - Create an immutable full snapshot for the current instance.
- `cruxible snapshot list` - List snapshots for the current instance.

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible snapshot create

**Usage:** `cruxible snapshot create [OPTIONS]`

**Purpose:** Create an immutable full snapshot for the current instance.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--label` | no | `` | text | Optional human label for the snapshot. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible snapshot list

**Usage:** `cruxible snapshot list [OPTIONS]`

**Purpose:** List snapshots for the current instance.

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible source

**Usage:** `cruxible source [OPTIONS] COMMAND [ARGS]...`

**Purpose:** Register local source documents and dereference source-backed
evidence locators.

**Subcommands:**

- `cruxible source register` - Parse and register a local Markdown source artifact.
- `cruxible source dereference` - Resolve a registered source-evidence locator back to source text.

**Output And Side Effects:**
- `source register` writes a source artifact manifest, parsed chunk metadata, and
  optional archived source bytes into the current instance.
- `source dereference` is read-only.

**Common Errors:**
- Missing local instance or stale daemon `--instance-id`.
- Permission mode too low for governed write/read operations.
- Unsupported source kind, missing local source path, incomplete locator, or
  drifted source content hash.

## cruxible source register

**Usage:** `cruxible source register [OPTIONS]`

**Purpose:** Register a Markdown document as source-backed proposal evidence.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--path` | yes | `Sentinel.UNSET` | text | Local Markdown source path. Relative paths resolve from the current workspace. |
| `--kind` | no | `markdown` | choice | Source parser kind. |
| `--retention` | no | `manifest_only` | choice | Source retention mode: `manifest_only` or `archive`. |
| `--original-uri` | no | `` | text | Optional display/provenance URI to preserve in the manifest. |
| `--label` | no | `` | text | Optional display label. |
| `--json` | no | `False` | boolean | Output the registered artifact and chunk manifest as JSON. |

**Examples:**

```bash
cruxible source register \
  --path docs/vendor-evidence.md \
  --original-uri https://vendor.example/evidence.md \
  --label "Vendor evidence" \
  --json
```

```bash
cruxible source register \
  --path docs/vendor-evidence.md \
  --retention archive
```

**Output And Side Effects:**
- Persists a source artifact ID, document hash, parser version, byte count, and
  deterministic chunk IDs in `state.db`.
- With `manifest_only`, Cruxible stores the manifest and local path but not a
  deep copy of the source bytes.
- With `archive`, Cruxible also stores the source bytes so later dereference can
  use the archived body if the local file is missing or changed.

**Common Errors:**
- Missing source path, unsupported source kind, path outside the registered
  workspace in daemon mode, or unreadable source file.

## cruxible source dereference

**Usage:** `cruxible source dereference [OPTIONS]`

**Purpose:** Resolve a registered source-evidence locator back to source text.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--artifact` | yes | `Sentinel.UNSET` | text | Source artifact ID returned by `source register`. |
| `--chunk` | no | `` | text | Deterministic chunk ID from the registered manifest. |
| `--heading` | no | `` | text | Heading path segment. Repeat for nested headings. |
| `--block-selector` | no | `` | text | Block selector under the heading path, such as `paragraph:1`. |
| `--expected-content-hash` | no | `` | text | Optional expected chunk content hash for drift checks. |
| `--json` | no | `False` | boolean | Output dereference status, chunk metadata, and body as JSON. |

Source-evidence locators must use one of two forms:

- `--chunk <chunk-id>`
- `--heading <heading> [--heading <nested-heading> ...] --block-selector <selector>`

**Examples:**

```bash
cruxible source dereference \
  --artifact SRC-... \
  --chunk CHK-... \
  --json
```

```bash
cruxible source dereference \
  --artifact SRC-... \
  --heading "Compatibility Evidence" \
  --block-selector paragraph:1
```

**Output And Side Effects:**
- Read-only. Returns `available`, `drifted`, or `unavailable` plus source body
  when Cruxible can safely dereference the locator.
- `body_origin` is `archive` when archived bytes are used, or `local_path` when
  Cruxible rereads the registered local file.

**Common Errors:**
- Missing artifact, incomplete locator, unknown chunk, unavailable local source
  file for `manifest_only`, or content drift against the stored manifest/hash.

## cruxible stats

**Usage:** `cruxible stats [OPTIONS]`

**Purpose:** Display entity and relationship counts for this instance.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--json` | no | `False` | boolean | Output as JSON. |

**Output And Side Effects:**
- Read-only output unless the command records an explicit receipt, feedback, outcome, or decision event.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible test

**Usage:** `cruxible test [OPTIONS]`

**Purpose:** Execute config-defined workflow tests for the current instance.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--name` | no | `` | text | Run only a named workflow test. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible validate

**Usage:** `cruxible validate [OPTIONS]`

**Purpose:** Validate a config YAML file without creating an instance.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--config` | yes | `Sentinel.UNSET` | text | Path to config YAML file. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible world

**Usage:** `cruxible world [OPTIONS]`

**Purpose:** Publish immutable worlds and manage pullable overlays.

**Subcommands:**

- `cruxible world create-overlay` - Create a new local overlay instance from a published world release.
- `cruxible world publish` - Publish the current root world-model instance as an immutable release bundle.
- `cruxible world pull-apply` - Apply a previewed upstream release into the current overlay.
- `cruxible world pull-preview` - Preview pulling a newer upstream release into the current overlay.
- `cruxible world status` - Show upstream tracking metadata for the current instance.

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible world create-overlay

**Usage:** `cruxible world create-overlay [OPTIONS]`

**Purpose:** Create a new local overlay instance from a published world release.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--transport-ref` | no | `Sentinel.UNSET` | text | Transport ref, e.g. file://... or oci://... |
| `--world-ref` | no | `Sentinel.UNSET` | text | World alias, e.g. kev-reference or kev-reference@2026-03-27. |
| `--kit` | no | `Sentinel.UNSET` | text | Apply a checked-in local overlay kit, e.g. kev-triage. |
| `--no-kit` | no | `False` | boolean | Skip automatic kit application and create a bare overlay. |
| `--root-dir` | no | `` | text | Workspace root for the new overlay (defaults to current directory in server mode). |
| `--activate / --no-activate` | no | `True` | boolean | Make the new server overlay the active CLI context instance. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible world publish

**Usage:** `cruxible world publish [OPTIONS]`

**Purpose:** Publish the current root world-model instance as an immutable release bundle.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--transport-ref` | yes | `Sentinel.UNSET` | text | Transport ref, e.g. file://... or oci://... |
| `--world-id` | yes | `Sentinel.UNSET` | text | Stable published world identifier. |
| `--release-id` | yes | `Sentinel.UNSET` | text | User-supplied release identifier. |
| `--compatibility` | no | `data_only` | choice | Compatibility classification for the published release. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible world pull-apply

**Usage:** `cruxible world pull-apply [OPTIONS]`

**Purpose:** Apply a previewed upstream release into the current overlay.

**Options And Arguments:**

| Name | Required | Default | Type | Description |
| --- | --- | --- | --- | --- |
| `--apply-digest` | yes | `Sentinel.UNSET` | text | Apply digest returned by pull-preview. |

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible world pull-preview

**Usage:** `cruxible world pull-preview [OPTIONS]`

**Purpose:** Preview pulling a newer upstream release into the current overlay.

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.

## cruxible world status

**Usage:** `cruxible world status [OPTIONS]`

**Purpose:** Show upstream tracking metadata for the current instance.

**Output And Side Effects:**
- Calls the service layer and may create receipts, traces, snapshots, config changes, groups, or graph mutations depending on the command.

**Common Errors:**
- Missing or stale `--instance-id` for daemon-backed commands.
- Permission mode too low for mutations or admin operations.
- Unknown config/workflow/query/entity names, or stale workflow locks where applicable.
