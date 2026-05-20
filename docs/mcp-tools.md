# MCP Tools Reference

This is the full searchable reference for Cruxible MCP tools. MCP is a curated agent connector, not full CLI parity. The HTTP API/client remain the broader remote product surface; CLI keeps shell-only utilities such as `context`, `config-views --update-readme`, `export edges`, and local receipt `explain`.

## Permission Modes

| Mode | Env value | Meaning |
| --- | --- | --- |
| READ_ONLY | `read_only` | Query, inspect, receipts, samples, evaluation, lint, wiki rendering, snapshots listing. |
| GOVERNED_WRITE | `governed_write` | READ_ONLY plus workflow runs/tests, proposal workflows, feedback, outcomes, decision records, and proposal groups. |
| GRAPH_WRITE | `graph_write` | GOVERNED_WRITE plus raw graph mutation and group resolution/trust updates. |
| ADMIN | `admin` | Full lifecycle, config reload, locks, canonical apply, snapshots, clone, world publication/pull, ingest, constraints, policies. |

## cruxible_version

**Permission:** `READ_ONLY`

**Purpose:** Return the cruxible-core version. Use this to confirm which build is running.

**Arguments:** none.

**Returns:** Returns a JSON object with dynamic keys.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_server_info

**Permission:** `READ_ONLY`

**Purpose:** Return live daemon metadata such as server-required status, state dir, and instance count.

**Arguments:** none.

**Returns:** Top-level fields: `server_required`, `state_dir`, `version`, `instance_count`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_init

**Permission:** `READ_ONLY`

**Purpose:** Create or reload a governed daemon-backed instance. Provide `config_path` or `config_yaml` when creating a new instance. In server mode, `config_path` is read locally and uploaded as config content; the daemon stores its own active copy. To reload after a restart, omit both.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `root_dir` | yes | string |  |
| `config_path` | no | string | null |  |
| `config_yaml` | no | string | null |  |
| `data_dir` | no | string | null |  |
| `kit` | no | string | null |  |

**Returns:** Top-level fields: `instance_id`, `status`, `warnings`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_validate

**Permission:** `READ_ONLY`

**Purpose:** Validate a config file or inline YAML without creating an instance. Provide exactly one of `config_path` (path to a YAML file) or `config_yaml` (raw YAML string).

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `config_path` | no | string | null |  |
| `config_yaml` | no | string | null |  |

**Returns:** Top-level fields: `valid`, `name`, `entity_types`, `relationships`, `named_queries`, `warnings`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_world_create_overlay

**Permission:** `ADMIN`

**Purpose:** Create a new governed overlay from a published world release.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `root_dir` | yes | string |  |
| `transport_ref` | no | string | null |  |
| `world_ref` | no | string | null |  |
| `kit` | no | string | null |  |
| `no_kit` | no | boolean |  |

**Returns:** Top-level fields: `instance_id`, `manifest`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_lock_workflow

**Permission:** `ADMIN`

**Purpose:** Generate the workflow lock file for the current instance config. Run this after changing providers, artifacts, or workflow config and before planning or executing workflows.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `force` | no | boolean |  |

**Returns:** Top-level fields: `lock_path`, `config_digest`, `providers_locked`, `artifacts_locked`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_plan_workflow

**Permission:** `READ_ONLY`

**Purpose:** Compile a configured workflow into a concrete execution plan.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `workflow_name` | yes | string |  |
| `input_payload` | no | object | null |  |

**Returns:** Top-level fields: `plan`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_run_workflow

**Permission:** `GOVERNED_WRITE`

**Purpose:** Execute a configured workflow and return receipts, traces, and output. Canonical workflows run in preview mode and return an `apply_digest` plus the current `head_snapshot_id`. To commit a canonical workflow, call `cruxible_apply_workflow` with those values.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `workflow_name` | yes | string |  |
| `input_payload` | no | object | null |  |
| `decision_record_id` | no | string | null |  |

**Returns:** Top-level fields: `workflow`, `output`, `receipt_id`, `mode`, `workflow_type`, `canonical`, `apply_digest`, `head_snapshot_id`, `committed_snapshot_id`, `apply_previews`, `query_receipt_ids`, `trace_ids`, `receipt`, `traces`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_apply_workflow

**Permission:** `ADMIN`

**Purpose:** Apply a canonical workflow after verifying the preview identity.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `workflow_name` | yes | string |  |
| `expected_apply_digest` | yes | string |  |
| `expected_head_snapshot_id` | no | string | null |  |
| `input_payload` | no | object | null |  |
| `decision_record_id` | no | string | null |  |

**Returns:** Top-level fields: `workflow`, `output`, `receipt_id`, `mode`, `workflow_type`, `canonical`, `apply_digest`, `head_snapshot_id`, `committed_snapshot_id`, `apply_previews`, `query_receipt_ids`, `trace_ids`, `receipt`, `traces`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_test_workflow

**Permission:** `GOVERNED_WRITE`

**Purpose:** Run configured workflow tests for an instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `name` | no | string | null |  |

**Returns:** Top-level fields: `total`, `passed`, `failed`, `cases`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_query

**Permission:** `READ_ONLY`

**Purpose:** Run a named query and return results plus a receipt. `params` must include the primary-key field of the query's entry_point entity type (e.g. if entry_point is Vehicle and its primary key is vehicle_id, pass {"vehicle_id": "V-123"}). Use `cruxible_schema` to find primary key fields. `receipt_id` is also promoted to top-level for follow-up tools. After querying, use `cruxible_receipt` to inspect the traversal proof showing exactly how results were derived. Use `limit` to cap the number of returned results and omit the inline receipt (fetch it later via `cruxible_receipt`).

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `query_name` | yes | string |  |
| `params` | no | object | null |  |
| `limit` | no | integer | null |  |
| `relationship_state` | no | string | null | One of `live`, `accepted`, `pending`, or `reviewable`. |
| `decision_record_id` | no | string | null |  |

**Returns:** Top-level fields: `results`, `receipt_id`, `receipt`, `total_results`, `truncated`, `steps_executed`, `result_shape`, `dedupe`, `relationship_state`, `param_hints`, `policy_summary`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list_queries

**Permission:** `READ_ONLY`

**Purpose:** List named queries with their entry points, required params, and example IDs.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |

**Returns:** Top-level fields: `queries`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_describe_query

**Permission:** `READ_ONLY`

**Purpose:** Describe one named query with the details needed to invoke it correctly.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `query_name` | yes | string |  |

**Returns:** Top-level fields: `name`, `entry_point`, `required_params`, `returns`, `description`, `example_ids`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_receipt

**Permission:** `READ_ONLY`

**Purpose:** Fetch a stored receipt by `receipt_id` from a previous query.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `receipt_id` | yes | string |  |

**Returns:** Returns a JSON object with dynamic keys.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_get_trace

**Permission:** `READ_ONLY`

**Purpose:** Fetch a full provider execution trace by `trace_id`.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID or local instance root. |
| `trace_id` | yes | string | Provider execution trace ID, usually returned by workflow run/apply/propose results. |

**Returns:** Returns the full persisted trace payload with provider metadata, input/output payloads, status, timings, and error details when present.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Trace ID not found.
- Permission mode too low for this tool.

## cruxible_list_traces

**Permission:** `READ_ONLY`

**Purpose:** List provider execution trace summaries with optional workflow/provider filters.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID or local instance root. |
| `workflow_name` | no | string | null | Filter by workflow name. |
| `provider_name` | no | string | null | Filter by provider name. |
| `limit` | no | integer | Maximum trace summaries to return. |
| `offset` | no | integer | Number of summaries to skip. |

**Returns:** Top-level fields: `traces`, `count`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Invalid limit or offset.

## cruxible_feedback

**Permission:** `GOVERNED_WRITE`

**Purpose:** Record edge-level feedback tied to a receipt. ``source`` identifies who produced this feedback: ``"human"`` for human review, ``"agent"`` for AI agent review. Rejected edges are excluded from future query results. Approved edges are trusted in traversals. Use `corrections` with `action="correct"` and set `edge_key` only when disambiguation is needed. `applied=False` means the record was saved but the graph edge was not updated. Set `group_override=True` to stamp the edge with a group_override property, marking it as pre-approved for group resolve. The edge must already exist in the graph.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `receipt_id` | yes | string |  |
| `action` | yes | enum: approve, reject, correct, flag |  |
| `source` | yes | enum: human, agent |  |
| `from_type` | yes | string |  |
| `from_id` | yes | string |  |
| `relationship` | yes | string |  |
| `to_type` | yes | string |  |
| `to_id` | yes | string |  |
| `edge_key` | no | integer | null |  |
| `reason` | no | string |  |
| `reason_code` | no | string | null |  |
| `scope_hints` | no | object | null |  |
| `corrections` | no | object | null |  |
| `group_override` | no | boolean |  |

**Returns:** Top-level fields: `feedback_id`, `applied`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_feedback_from_query

**Permission:** `GOVERNED_WRITE`

**Purpose:** Record edge-level feedback by selecting one relationship row or path segment from a query receipt. This adjudicates one existing relationship assertion and does not resolve candidate groups; use group get/resolve for group thesis and member-set decisions.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID or local instance root. |
| `receipt_id` | yes | string | Query receipt ID. |
| `result_index` | yes | integer | Zero-based query result row index. |
| `action` | yes | enum: approve, reject, correct, flag | Feedback action. |
| `source` | no | enum: human, agent | Who produced this feedback. |
| `reason` | no | string | Reason for feedback. |
| `reason_code` | no | string | Structured feedback reason code. |
| `scope_hints` | no | object | Structured feedback scope hints. |
| `corrections` | no | object | Edge property corrections for `action="correct"`. |
| `group_override` | no | boolean | Stamp the selected edge for group override. |
| `path_index` | no | integer | Zero-based path segment index for path rows. |
| `path_alias` | no | string | Traversal alias for the selected path segment. |

**Returns:** Top-level fields: `feedback_id`, `applied`, `receipt_id`

**Side Effects:** Creates normal feedback records and feedback receipts through the existing edge-feedback path.

**Common Errors:**
- Receipt is missing, not a query receipt, or result index is out of range.
- Entity-shaped query rows do not contain relationship evidence.
- Multi-hop path rows require exactly one of `path_index` or `path_alias`.
- Selected path alias is missing or duplicated, or selected edge is no longer in the graph.

## cruxible_feedback_batch

**Permission:** `GOVERNED_WRITE`

**Purpose:** Record batch edge feedback under one top-level mutation receipt.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `items` | yes | array |  |
| `source` | no | enum: human, agent |  |

**Returns:** Top-level fields: `feedback_ids`, `applied_count`, `total`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_outcome

**Permission:** `GOVERNED_WRITE`

**Purpose:** Record a structured outcome for a receipt or proposal resolution.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `outcome` | yes | enum: correct, incorrect, partial, unknown |  |
| `receipt_id` | no | string | null |  |
| `anchor_type` | no | enum: resolution, receipt |  |
| `anchor_id` | no | string | null |  |
| `source` | no | enum: human, agent |  |
| `outcome_code` | no | string | null |  |
| `scope_hints` | no | object | null |  |
| `outcome_profile_key` | no | string | null |  |
| `detail` | no | object | null |  |

**Returns:** Top-level fields: `outcome_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list

**Permission:** `READ_ONLY`

**Purpose:** List `entities|edges|receipts|feedback|outcomes` with optional filters. `entity_type` is required for `resource_type="entities"`. `relationship_type` filters edges by type for `resource_type="edges"`. `property_filter` filters by exact property matches (AND semantics). Applies to `resource_type="entities"` and `resource_type="edges"`. `operation_type` filters receipts (e.g. "query", "add_entity", "ingest"). Edge items include `edge_key` for use with `cruxible_feedback` when multiple edges exist between the same endpoints.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `resource_type` | yes | enum: entities, edges, receipts, feedback, outcomes |  |
| `entity_type` | no | string | null |  |
| `relationship_type` | no | string | null |  |
| `query_name` | no | string | null |  |
| `receipt_id` | no | string | null |  |
| `limit` | no | integer |  |
| `property_filter` | no | object | null |  |
| `operation_type` | no | string | null |  |

**Returns:** Top-level fields: `items`, `total`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_evaluate

**Permission:** `READ_ONLY`

**Purpose:** Run graph quality checks (orphans, gaps, violations, co-members). Checks: orphan entities, coverage gaps, constraint violations, candidate opportunities, governed support state, and unreviewed co-members (entities sharing an intermediary with a cross-referenced entity but lacking a cross-reference edge themselves). Use `exclude_orphan_types` to skip reference/taxonomy entity types (e.g. ``["PCDBPartType"]``) that are expected to be unconnected.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `max_findings` | no | integer |  |
| `exclude_orphan_types` | no | array | null |  |

**Returns:** Top-level fields: `entity_count`, `edge_count`, `findings`, `summary`, `constraint_summary`, `quality_summary`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_stats

**Permission:** `READ_ONLY`

**Purpose:** Return graph counts, relationship counts, and head snapshot metadata.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |

**Returns:** Top-level fields: `entity_count`, `edge_count`, `entity_counts`, `relationship_counts`, `head_snapshot_id`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_lint

**Permission:** `READ_ONLY`

**Purpose:** Run aggregate read-only config, graph, feedback, and outcome checks.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `max_findings` | no | integer |  |
| `analysis_limit` | no | integer |  |
| `min_support` | no | integer |  |
| `exclude_orphan_types` | no | array | null |  |

**Returns:** Top-level fields: `config_name`, `config_warnings`, `compatibility_warnings`, `evaluation`, `feedback_reports`, `outcome_reports`, `summary`, `has_issues`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_get_feedback_profile

**Permission:** `READ_ONLY`

**Purpose:** Return the configured feedback profile for one relationship type.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `relationship_type` | yes | string |  |

**Returns:** Top-level fields: `found`, `relationship_type`, `profile`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_analyze_feedback

**Permission:** `READ_ONLY`

**Purpose:** Analyze structured feedback into deterministic remediation suggestions.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `relationship_type` | yes | string |  |
| `limit` | no | integer |  |
| `min_support` | no | integer |  |
| `decision_surface_type` | no | string | null |  |
| `decision_surface_name` | no | string | null |  |
| `property_pairs` | no | array | null |  |

**Returns:** Top-level fields: `relationship_type`, `feedback_count`, `action_counts`, `source_counts`, `reason_code_counts`, `coded_groups`, `uncoded_feedback_count`, `uncoded_examples`, `constraint_suggestions`, `decision_policy_suggestions`, `quality_check_candidates`, `provider_fix_candidates`, `warnings`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_get_outcome_profile

**Permission:** `READ_ONLY`

**Purpose:** Return the configured outcome profile for one anchor context.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `anchor_type` | yes | enum: resolution, receipt |  |
| `relationship_type` | no | string | null |  |
| `workflow_name` | no | string | null |  |
| `surface_type` | no | string | null |  |
| `surface_name` | no | string | null |  |

**Returns:** Top-level fields: `found`, `profile_key`, `anchor_type`, `profile`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_analyze_outcomes

**Permission:** `READ_ONLY`

**Purpose:** Analyze structured outcomes into trust and debugging suggestions.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `anchor_type` | yes | enum: resolution, receipt |  |
| `relationship_type` | no | string | null |  |
| `workflow_name` | no | string | null |  |
| `query_name` | no | string | null |  |
| `surface_type` | no | string | null |  |
| `surface_name` | no | string | null |  |
| `limit` | no | integer |  |
| `min_support` | no | integer |  |

**Returns:** Top-level fields: `anchor_type`, `outcome_count`, `outcome_counts`, `outcome_code_counts`, `coded_groups`, `uncoded_outcome_count`, `uncoded_examples`, `trust_adjustment_suggestions`, `workflow_review_policy_suggestions`, `query_policy_suggestions`, `provider_fix_candidates`, `debug_packages`, `workflow_debug_packages`, `warnings`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_schema

**Permission:** `READ_ONLY`

**Purpose:** Return the active config schema for an instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |

**Returns:** Returns a JSON object with dynamic keys.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_sample

**Permission:** `READ_ONLY`

**Purpose:** Return up to `limit` entities for quick data inspection.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `entity_type` | yes | string |  |
| `limit` | no | integer |  |

**Returns:** Top-level fields: `entities`, `entity_type`, `count`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_inspect_entity

**Permission:** `READ_ONLY`

**Purpose:** Inspect one entity and its immediate incoming/outgoing neighbors.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `entity_type` | yes | string |  |
| `entity_id` | yes | string |  |
| `direction` | no | string |  |
| `relationship_type` | no | string | null |  |
| `limit` | no | integer | null |  |

**Returns:** Top-level fields: `found`, `entity_type`, `entity_id`, `properties`, `neighbors`, `total_neighbors`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_inspect_ontology

**Permission:** `READ_ONLY`

**Purpose:** Return the structured canonical ontology view for an instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |

**Returns:** Top-level fields: `view`, `payload`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_inspect_workflows

**Permission:** `READ_ONLY`

**Purpose:** Return the structured canonical workflow view for an instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |

**Returns:** Top-level fields: `view`, `payload`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_inspect_queries

**Permission:** `READ_ONLY`

**Purpose:** Return the structured canonical query view for an instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |

**Returns:** Top-level fields: `view`, `payload`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_inspect_governance

**Permission:** `READ_ONLY`

**Purpose:** Return the structured canonical governance view for an instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `limit` | no | integer |  |

**Returns:** Top-level fields: `view`, `payload`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_inspect_overview

**Permission:** `READ_ONLY`

**Purpose:** Return the structured canonical overview view for an instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `limit` | no | integer |  |

**Returns:** Top-level fields: `view`, `payload`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_render_wiki

**Permission:** `READ_ONLY`

**Purpose:** Render local wiki pages and return path/content payloads.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `focus` | no | array | null |  |
| `include_types` | no | array | null |  |
| `scope` | no | string | null |  |
| `max_per_type` | no | integer |  |
| `all_subjects` | no | boolean |  |

**Returns:** Top-level fields: `pages`, `page_count`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_add_relationship

**Permission:** `GRAPH_WRITE`

**Purpose:** Add or update relationships in the graph (upsert). Each relationship needs: from_type, from_id, relationship, to_type, to_id. Optional properties must be declared by the relationship schema. Entities must already exist. Re-submitting an existing edge merges declared domain properties while preserving system review metadata. For governed judgment relationships, prefer proposal workflows or candidate group proposal flows so Cruxible can preserve tri-state signal-source evidence (support, unsure, contradict) and review history. For bulk state loading, use workflows with tabular providers, dataflow steps, and apply_relationships.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `relationships` | yes | array |  |

**Returns:** Top-level fields: `added`, `updated`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_add_entity

**Permission:** `GRAPH_WRITE`

**Purpose:** Add or update entities in the graph (upsert). Each entity needs: entity_type, entity_id. Optional properties dict. Re-submitting an existing entity replaces all its properties (full overwrite, not merge). Use for small explicit writes; use workflows for repeatable source-artifact state loading.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `entities` | yes | array |  |

**Returns:** Top-level fields: `entities_added`, `entities_updated`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_add_constraint

**Permission:** `ADMIN`

**Purpose:** Add a constraint rule to the config. Writes the updated config to YAML. Constraints are evaluated by cruxible_evaluate to flag edges that violate them. Rule format: RELATIONSHIP.FROM.property <op> RELATIONSHIP.TO.property Supported operators: ==, !=, >, >=, <, <= Identifiers may contain letters, digits, underscores, and hyphens. Example: classified_as.FROM.Category == classified_as.TO.CategoryName

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `name` | yes | string |  |
| `rule` | yes | string |  |
| `severity` | no | enum: warning, error |  |
| `description` | no | string | null |  |

**Returns:** Top-level fields: `name`, `added`, `config_updated`, `warnings`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_add_decision_policy

**Permission:** `ADMIN`

**Purpose:** Add a decision policy to the config for query/workflow execution.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `name` | yes | string |  |
| `applies_to` | yes | enum: query, workflow |  |
| `relationship_type` | yes | string |  |
| `effect` | yes | enum: suppress, require_review |  |
| `match` | no | DecisionPolicyMatchInput | null |  |
| `description` | no | string | null |  |
| `rationale` | no | string |  |
| `query_name` | no | string | null |  |
| `workflow_name` | no | string | null |  |
| `expires_at` | no | string | null |  |

**Returns:** Top-level fields: `name`, `added`, `config_updated`, `warnings`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_reload_config

**Permission:** `ADMIN`

**Purpose:** Reload or replace an instance config after validation.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `config_path` | no | string | null |  |
| `config_yaml` | no | string | null |  |

**Returns:** Top-level fields: `config_path`, `updated`, `warnings`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_propose_workflow

**Permission:** `GOVERNED_WRITE`

**Purpose:** Execute a configured workflow and bridge its output into a governed relationship group. Use this when a repeated decision procedure should propose relationship state through Cruxible's proposal/review/trust boundary instead of writing edges directly. The workflow must be `type: proposal` and return a relationship proposal artifact from a `propose_relationship_group` step.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `workflow_name` | yes | string |  |
| `input_payload` | no | object | null |  |
| `decision_record_id` | no | string | null |  |

**Returns:** Top-level fields: `workflow`, `output`, `receipt_id`, `mode`, `workflow_type`, `canonical`, `group_id`, `group_status`, `review_priority`, `suppressed`, `suppressed_members`, `query_receipt_ids`, `trace_ids`, `prior_resolution`, `policy_summary`, `receipt`, `traces`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_create_decision_record

**Permission:** `GOVERNED_WRITE`

**Purpose:** Open a decision record that can collect query and workflow receipts.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `question` | yes | string |  |
| `subject_type` | no | string | null |  |
| `subject_id` | no | string | null |  |
| `opened_by` | no | string |  |

**Returns:** Top-level fields: `record`, `events`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_get_decision_record

**Permission:** `READ_ONLY`

**Purpose:** Fetch one decision record, optionally including its logged events.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `decision_record_id` | yes | string |  |
| `include_events` | no | boolean |  |

**Returns:** Top-level fields: `record`, `events`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list_decision_records

**Permission:** `READ_ONLY`

**Purpose:** List decision records with lifecycle and subject filters.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `status` | no | string | null |  |
| `subject_type` | no | string | null |  |
| `subject_id` | no | string | null |  |
| `decision_class` | no | string | null |  |
| `limit` | no | integer |  |

**Returns:** Top-level fields: `records`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list_decision_events

**Permission:** `READ_ONLY`

**Purpose:** List decision-record events by record, receipt, trace, or status.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `decision_record_id` | no | string | null |  |
| `receipt_id` | no | string | null |  |
| `trace_id` | no | string | null |  |
| `status` | no | string | null |  |
| `limit` | no | integer |  |

**Returns:** Top-level fields: `events`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_finalize_decision_record

**Permission:** `GOVERNED_WRITE`

**Purpose:** Finalize a decision record with an indexed decision class and rationale.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `decision_record_id` | yes | string |  |
| `final_decision` | yes | string |  |
| `decision_class` | yes | enum: recommended, rejected, deferred, escalated |  |
| `rationale` | no | string |  |

**Returns:** Top-level fields: `record`, `events`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_abandon_decision_record

**Permission:** `GOVERNED_WRITE`

**Purpose:** Abandon an open decision record without finalizing a recommendation.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `decision_record_id` | yes | string |  |
| `reason` | no | string |  |

**Returns:** Top-level fields: `record`, `events`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_propose_group

**Permission:** `GOVERNED_WRITE`

**Purpose:** Propose a candidate group of edges for batch review. Each member carries tri-state signals (support/contradict/unsure) from declared relationship signal sources. The group carries a thesis (structured facts that get hashed into a deterministic signature) and optional analysis_state (opaque agent data, NOT hashed). If a prior trusted resolution exists for the same thesis signature and all signals meet the auto-resolve policy, the group is auto-resolved. Otherwise it enters pending_review with a Cruxible-derived review_priority.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `relationship_type` | yes | string |  |
| `members` | yes | array |  |
| `thesis_text` | no | string |  |
| `thesis_facts` | no | object | null |  |
| `analysis_state` | no | object | null |  |
| `signal_sources_used` | no | array | null |  |
| `proposed_by` | no | enum: human, agent |  |
| `suggested_priority` | no | string | null |  |

**Returns:** Top-level fields: `group_id`, `signature`, `status`, `review_priority`, `member_count`, `prior_resolution`, `suppressed`, `suppressed_members`, `policy_summary`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_resolve_group

**Permission:** `GRAPH_WRITE`

**Purpose:** Resolve a candidate group by approving or rejecting it. Approve creates edges in the graph for valid members (skipping members whose edges already exist). Reject records the resolution without graph mutation. Both persist the resolution for audit and future auto-resolve precedent.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `group_id` | yes | string |  |
| `action` | yes | enum: approve, reject |  |
| `expected_pending_version` | yes | integer |  |
| `rationale` | no | string |  |
| `resolved_by` | no | enum: human, agent |  |

**Returns:** Top-level fields: `group_id`, `action`, `edges_created`, `edges_skipped`, `resolution_id`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_update_trust_status

**Permission:** `GRAPH_WRITE`

**Purpose:** Update the trust status on a confirmed approved resolution. Trust is thesis-scoped: the latest confirmed approval for a signature governs auto-resolve eligibility. Promote ``watch`` to ``trusted`` to enable auto-resolve. Set ``invalidated`` to block auto-resolve and escalate future proposals to critical priority.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `resolution_id` | yes | string |  |
| `trust_status` | yes | enum: trusted, watch, invalidated |  |
| `reason` | no | string |  |

**Returns:** Top-level fields: `resolution_id`, `trust_status`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_get_group

**Permission:** `READ_ONLY`

**Purpose:** Get a candidate group by ID, including its members, optional resolution, bucket lifecycle status, and per-member review state. The member review payload includes proposed tuples/properties, current edge count, current edge details when exactly one matching edge exists, and deterministic property deltas.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `group_id` | yes | string |  |

**Returns:** Top-level fields: `group`, `members`, `resolution`, `bucket_status`, `member_review`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list_groups

**Permission:** `READ_ONLY`

**Purpose:** List candidate groups with optional filters. Results are sorted by review_priority descending (critical first). Use ``status`` to filter by lifecycle state (pending_review, auto_resolved, applying, resolved). Use ``relationship_type`` to filter by edge type.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `relationship_type` | no | string | null |  |
| `status` | no | enum: pending_review, auto_resolved, applying, resolved, suppressed | null |  |
| `limit` | no | integer |  |

**Returns:** Top-level fields: `groups`, `total`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list_resolutions

**Permission:** `READ_ONLY`

**Purpose:** List group resolutions with optional filters. Returns stored resolutions including analysis_state (for agent reuse), thesis_facts, trust_status, and trust_reason. Use ``action`` to filter by approve/reject. Use ``relationship_type`` to scope to a specific edge type.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `relationship_type` | no | string | null |  |
| `action` | no | enum: approve, reject | null |  |
| `limit` | no | integer |  |

**Returns:** Top-level fields: `resolutions`, `total`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_group_status

**Permission:** `READ_ONLY`

**Purpose:** Show lifecycle status for a signature bucket or concrete group.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `group_id` | no | string | null |  |
| `signature` | no | string | null |  |

**Returns:** Top-level fields: `signature`, `relationship_type`, `thesis_text`, `thesis_facts`, `latest_trust_status`, `accepted_tuple_count`, `pending_delta_count`, `pending_group_id`, `pending_version`, `latest_approved_resolution_id`, `approved_history`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_world_publish

**Permission:** `ADMIN`

**Purpose:** Publish a root world-model instance as an immutable release bundle.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `transport_ref` | yes | string |  |
| `world_id` | yes | string |  |
| `release_id` | yes | string |  |
| `compatibility` | yes | enum: data_only, additive_schema, breaking |  |

**Returns:** Top-level fields: `manifest`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_create_snapshot

**Permission:** `ADMIN`

**Purpose:** Create an immutable snapshot for the current instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `label` | no | string | null |  |

**Returns:** Top-level fields: `snapshot`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list_snapshots

**Permission:** `READ_ONLY`

**Purpose:** List immutable snapshots for the current instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |

**Returns:** Top-level fields: `snapshots`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_clone_snapshot

**Permission:** `ADMIN`

**Purpose:** Create a point-in-time clone from an immutable snapshot.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `snapshot_id` | yes | string |  |
| `root_dir` | yes | string |  |

**Returns:** Top-level fields: `instance_id`, `snapshot`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_world_status

**Permission:** `READ_ONLY`

**Purpose:** Return upstream tracking metadata for a release-backed overlay.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |

**Returns:** Top-level fields: `upstream`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_world_pull_preview

**Permission:** `READ_ONLY`

**Purpose:** Preview pulling a newer upstream release into a release-backed overlay.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |

**Returns:** Top-level fields: `current_release_id`, `target_release_id`, `compatibility`, `apply_digest`, `warnings`, `conflicts`, `lock_changed`, `upstream_entity_delta`, `upstream_edge_delta`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_world_pull_apply

**Permission:** `ADMIN`

**Purpose:** Apply a previewed upstream release into a release-backed overlay.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `expected_apply_digest` | yes | string |  |

**Returns:** Top-level fields: `release_id`, `apply_digest`, `pre_pull_snapshot_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_get_entity

**Permission:** `READ_ONLY`

**Purpose:** Look up a specific entity by type and ID. Returns its properties.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `entity_type` | yes | string |  |
| `entity_id` | yes | string |  |

**Returns:** Top-level fields: `found`, `entity_type`, `entity_id`, `properties`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_get_relationship

**Permission:** `READ_ONLY`

**Purpose:** Look up a specific relationship by its endpoints and type. Returns its properties. If multiple same-type edges exist between the same endpoints, pass edge_key to select a specific one. Without edge_key, raises an error if ambiguous.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `from_type` | yes | string |  |
| `from_id` | yes | string |  |
| `relationship_type` | yes | string |  |
| `to_type` | yes | string |  |
| `to_id` | yes | string |  |
| `edge_key` | no | integer | null |  |

**Returns:** Top-level fields: `found`, `from_type`, `from_id`, `relationship_type`, `to_type`, `to_id`, `edge_key`, `properties`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_relationship_lineage

**Permission:** `READ_ONLY`

**Purpose:** Look up a relationship and follow group provenance when available.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID or local instance root. |
| `from_type` | yes | string | Source entity type. |
| `from_id` | yes | string | Source entity ID. |
| `relationship_type` | yes | string | Relationship type. |
| `to_type` | yes | string | Target entity type. |
| `to_id` | yes | string | Target entity ID. |
| `edge_key` | no | integer | null | Edge key for multi-edge disambiguation. |

**Returns:** Top-level fields: `found`, `relationship`, `_provenance`, `group`, `resolution`, `source_workflow_receipt_id`, `source_trace_ids`, `warnings`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Ambiguous relationship tuple without `edge_key`.
