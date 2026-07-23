# MCP Tools Reference

This is the full searchable reference for Cruxible MCP tools. MCP is a curated agent connector, not full CLI parity. The HTTP API/client remain the broader remote product surface; CLI keeps shell-only utilities such as `context`, `config views --update-readme`, `export edges`, and local receipt `explain`.

## Permission Modes

| Mode | Env value | Meaning |
| --- | --- | --- |
| READ_ONLY | `read_only` | Query, inspect, receipts, samples, evaluation, lint, snapshots listing. |
| GOVERNED_WRITE | `governed_write` | READ_ONLY plus workflow and procedure runs, procedure/group proposals, feedback, outcomes, decision records, snapshot creation, and source artifact registration. |
| GRAPH_WRITE | `graph_write` | GOVERNED_WRITE plus raw graph mutation, canonical workflow apply, group resolution/trust updates, and procedure resolution/retirement. |
| ADMIN | `admin` | Full lifecycle, config reload, locks, snapshots, clone, state publication/pull, ingest, constraints, and policies. |

`tools/list` advertises only tools allowed by the active `CRUXIBLE_MODE`; call-time permission checks still enforce the same tiers as a backstop.

## Tool Catalog Curation

Set `CRUXIBLE_MCP_PROFILE` to shrink the advertised catalog for focused clients:

| Profile | Meaning |
| --- | --- |
| `full` | Default. Advertise every tool allowed by the active permission mode. |
| `state_authoring` | Tools for creating, inspecting, querying, and directly loading state. |
| `review` | Tools for queries, receipts, feedback, outcomes, and proposal-group review. |

Set `CRUXIBLE_MCP_TOOLS` or `CRUXIBLE_MCP_TOOL_ALLOWLIST` to a comma-separated list of exact tool names for an explicit allowlist. Profile and allowlist curation are both intersected with `CRUXIBLE_MODE`.

## Tool Prompt Style

Tool descriptions are written for non-coding MCP clients. Each description starts with when to use the tool, uses kit-user vocabulary, and avoids implementation details that do not help with tool choice.

## Working-Set Capture

Set `CRUXIBLE_WORKING_SET_DIR` to a directory path to opt the MCP server into agent-local working-set capture: entity/edge-shaped results returned by the read tools (`cruxible_query`, `cruxible_query_inline`, `cruxible_get_entity`, `cruxible_inspect_entity`, `cruxible_list`, `cruxible_sample`, `cruxible_get_relationship`) are ALSO recorded as revision-stamped working-set records rooted at that directory — the same record format, dedupe, and credential-scoped instance keys as the CLI's `--ws` capture (see the `cruxible ws` section of `docs/cli-reference.md`). Capture happens in the MCP server process, which is a client co-located with the agent; the daemon stays blind to it, and tool results are never changed by it.

When the variable is unset, capture is a hard no-op — zero behavior or performance change. Precedence for the cache root everywhere (including the `cruxible ws` verbs): explicit `CRUXIBLE_WORKING_SET_DIR` > the default `~/.cruxible/working-set`.

The cache is NON-AUTHORITATIVE and its files are same-user-writable by design: any same-user process can rewrite records undetected. Directory/file permission hygiene (0700/0600) and symlink refusal reduce accidents, not adversaries; verify records with `cruxible ws verify` before trusting them.

## cruxible_version

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to confirm which cruxible build this MCP server is running.

**Arguments:** none.

**Returns:** Returns a JSON object with dynamic keys.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_server_info

**Permission:** `READ_ONLY`

**Purpose:** Use when you need live daemon details such as state directory, version, and how many instances are loaded.

**Arguments:** none.

**Returns:** Top-level fields: `server_required`, `state_dir`, `version`, `instance_count`, `auth_enabled`, `auth_required`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_init

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to create a governed instance from a config or reconnect to an existing instance after a daemon restart.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `root_dir` | yes | string |  |
| `config_path` | no | string | null |  |
| `config_yaml` | no | string | null |  |
| `data_dir` | no | string | null |  |
| `kits` | no | array | null | Ordered kit refs: standalone base first, overlay kits after. |
| `bare` | no | boolean | Skip the configured default base kit on kit init. |

**Returns:** Top-level fields: `instance_id`, `status`, `warnings`

**Side Effects:** Creates a new instance or reloads an existing one.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_instance_backup

**Permission:** `ADMIN`

**Purpose:** Use when you need a portable same-identity backup of an instance, including its authoritative state database.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `artifact_path` | yes | string |  |
| `label` | no | string | null |  |

**Returns:** The backup artifact path and the instance identity it captured.

**Side Effects:** Writes a portable backup artifact to disk; does not mutate instance state.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_instance_restore

**Permission:** `ADMIN`

**Purpose:** Use when you need to restore a daemon-backed instance from a same-identity backup artifact.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `artifact_path` | yes | string |  |
| `root_dir` | no | string | null |  |

**Returns:** The restored instance id and status.

**Side Effects:** Creates an instance directory from the artifact and registers it with the daemon.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_instance_relocate

**Permission:** `ADMIN`

**Purpose:** Use when you need to move a healthy daemon-backed instance to a new directory while preserving its identity; the registry is repointed to the new location.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `to_dir` | yes | string |  |
| `remove_source` | no | boolean |  |

**Returns:** The instance id and its new on-disk location.

**Side Effects:** Moves the instance directory and repoints the registry to the new location.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_validate

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to check whether a Cruxible config is valid before creating or reloading an instance.

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

## cruxible_state_create_overlay

**Permission:** `ADMIN`

**Purpose:** Use when you need a local overlay instance based on a published upstream state release.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `root_dir` | yes | string |  |
| `transport_ref` | no | string | null |  |
| `state_ref` | no | string | null |  |
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

**Purpose:** Use when workflow inputs, providers, or artifacts changed and you need to refresh the workflow lock before running it.

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

**Purpose:** Use when you need to preview the concrete steps a configured workflow would run without executing those steps.

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

**Purpose:** Use when you need to execute a configured workflow and receive its output, receipts, traces, and apply instructions if it is a preview.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `workflow_name` | yes | string |  |
| `input_payload` | no | object | null |  |
| `decision_record_id` | no | string | null |  |

**Returns:** Top-level fields: `workflow`, `output`, `receipt_id`, `mode`, `workflow_type`, `canonical`, `apply_digest`, `head_snapshot_id`, `committed_snapshot_id`, `apply_previews`, `query_receipt_ids`, `read_metadata`, `trace_ids`, `receipt`, `traces`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_apply_workflow

**Permission:** `GRAPH_WRITE`

**Purpose:** Use when a workflow preview returned an apply digest and you are ready to commit that exact workflow result.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `workflow_name` | yes | string |  |
| `expected_apply_digest` | yes | string |  |
| `expected_head_snapshot_id` | no | string | null |  |
| `input_payload` | no | object | null |  |
| `decision_record_id` | no | string | null |  |

**Returns:** Top-level fields: `workflow`, `output`, `receipt_id`, `mode`, `workflow_type`, `canonical`, `apply_digest`, `head_snapshot_id`, `committed_snapshot_id`, `apply_previews`, `query_receipt_ids`, `read_metadata`, `trace_ids`, `receipt`, `traces`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_test_workflow

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to run workflow tests declared by the active config.

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

**Purpose:** Use when you need to run a named query from the active config and receive matching items plus a receipt. First call cruxible_list_queries or cruxible_describe_query when you do not know the query name, required params, result shape, or examples. For traversal queries, params must include the entry_point primary-key field, such as {'vehicle_id': 'V-123'} when the entry point is Vehicle and its primary key is vehicle_id; cruxible_schema shows entity primary keys. Items default to the compact output profile; ask for profile='standard' or 'full' when you need provenance or actor context. Pass layout='graph' for multi-row traversal reads: it returns each entity and relationship once as nodes/edges with results as ordered references, instead of duplicating them per row.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `query_name` | yes | string |  |
| `params` | no | object | null |  |
| `limit` | no | integer | null |  |
| `offset` | no | integer | Number of results to skip before the returned window. |
| `relationship_state` | no | string | null | Read-visibility state: one of `live`, `accepted`, `all`, `not-live`, `pending`, or `reviewable`. Gates entities by lifecycle and edges by review+lifecycle. |
| `decision_record_id` | no | string | null |  |
| `profile` | no | string | null | Output profile: `compact` (default on this surface), `standard`, or `full`. Compact returns bounded identity cards that keep lifecycle/review markers; standard/full include provenance and actor context. |
| `layout` | no | string | Transport layout: `rows` (default, per-row items) or `graph` (normalized transport: `nodes`/`edges` carry each unique entity and relationship once, `results` preserves row order as index references, `paths` holds step-ref sequences (edge index + traversal-step alias) for path-shaped results). |

**Returns:** Top-level fields: `items`, `receipt_id`, `receipt`, `total`, `limit`, `offset`, `truncated`, `limit_truncated`, `path_truncated`, `truncation_reasons`, `max_paths`, `max_paths_per_result`, `total_path_count`, `retained_path_count`, `steps_executed`, `result_shape`, `dedupe`, `relationship_state`, `param_hints`, `policy_summary` (rows layout). With `layout='graph'` the `items` field is replaced by `layout`, `nodes`, `edges`, `results`, and `paths`; every other field is unchanged.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_query_inline

**Permission:** `READ_ONLY`

**Purpose:** Use when you need a one-off bounded graph query without adding it to the config. Inline definitions use the configured named-query JSON shape plus a required name; promote repeated or workflow-critical queries into config. Items default to the compact output profile; ask for profile='standard' or 'full' when you need provenance or actor context. Pass layout='graph' to receive deduplicated nodes/edges with results as ordered references instead of per-row items.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `definition` | yes | InlineQueryDefinition | Inline query definition object: same JSON shape as a configured named query (`mode`, `returns`, `traversal`, `where`, `select`, `order_by`, `include`, `limit`, `max_paths`, `max_paths_per_result`, ...) plus a required `name`. |
| `params` | no | object | null |  |
| `limit` | no | integer | null |  |
| `relationship_state` | no | string | null | Read-visibility state: one of `live`, `accepted`, `all`, `not-live`, `pending`, or `reviewable`. Gates entities by lifecycle and edges by review+lifecycle. |
| `decision_record_id` | no | string | null |  |
| `profile` | no | string | null | Output profile: `compact` (default on this surface), `standard`, or `full`. Compact returns bounded identity cards that keep lifecycle/review markers; standard/full include provenance and actor context. |
| `layout` | no | string | Transport layout: `rows` (default, per-row items) or `graph` (normalized transport: `nodes`/`edges` carry each unique entity and relationship once, `results` preserves row order as index references, `paths` holds step-ref sequences (edge index + traversal-step alias) for path-shaped results). |

**Returns:** Top-level fields: `items`, `receipt_id`, `receipt`, `total`, `limit`, `offset`, `truncated`, `limit_truncated`, `path_truncated`, `truncation_reasons`, `max_paths`, `max_paths_per_result`, `total_path_count`, `retained_path_count`, `steps_executed`, `result_shape`, `dedupe`, `relationship_state`, `param_hints`, `policy_summary` (rows layout). With `layout='graph'` the `items` field is replaced by `layout`, `nodes`, `edges`, `results`, and `paths`; every other field is unchanged.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list_queries

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to discover the named queries available in the active config. Returns bounded summaries (name, entry point, required params); call cruxible_describe_query for one query's full definition. Pass detail='full' only when you truly need every definition expanded. If truncated is true, pass the returned continuation_token back as continuation to fetch the next page.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `detail` | no | string | `summary` (default) returns bounded discovery cards; `full` returns complete definitions. |
| `limit` | no | integer |  |
| `offset` | no | integer |  |
| `continuation` | no | string | null | Continuation token from a previous truncated page (same `detail`). Bound to the instance, config, and `read_revision`; replay after a mutation fails with a typed 409 stale-continuation error — restart the read. |

**Returns:** Top-level fields: `items`, `total`, `limit`, `offset`, `truncated`, `read_revision`, `continuation_token`

`read_revision` is the instance's monotonic state revision at read time — the
freshness marker for pagination and caching. Receipts prove a computation
happened; they never prove its inputs are still current.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Stale continuation token after a mutation (409) or malformed token (422).
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_describe_query

**Permission:** `READ_ONLY`

**Purpose:** Use when you need the purpose, parameters, and result shape for one named query.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `query_name` | yes | string |  |

**Returns:** Top-level fields: `name`, `mode`, `entry_point`, `required_params`, `returns`, `result_shape`, `dedupe`, `relationship_state`, `allow_relationship_state_override`, `select`, `order_by`, `include`, `limit`, `max_paths`, `max_paths_per_result`, `description`, `example_ids`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_receipt

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to inspect the proof record for a previous query, write, workflow, feedback, or outcome.

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

**Purpose:** Use when you need the execution trace for one provider or workflow step.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID or local instance root. |
| `trace_id` | yes | string | Provider execution trace ID, usually returned by workflow run/apply/propose results. |

**Returns:** Returns the persisted trace with provider metadata, retained input/output payload fields, payload digest/size metadata, status, timings, and error details when present. Payload fields follow the instance config's `runtime.trace_payloads` retention policy.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Trace ID not found.
- Permission mode too low for this tool.

## cruxible_list_traces

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to browse execution traces by workflow, provider, or page.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID or local instance root. |
| `workflow_name` | no | string | null | Filter by workflow name. |
| `provider_name` | no | string | null | Filter by provider name. |
| `limit` | no | integer | Maximum trace summaries to return. |
| `offset` | no | integer | Number of summaries to skip. |

**Returns:** Top-level fields: `items`, `total`, `limit`, `offset`, `truncated`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Invalid limit or offset.

## cruxible_feedback

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when a person or reviewer agent adjudicated one explicit relationship and you need to record support, rejection, flagging, or a correction. Use edge_key only to disambiguate multiple stored edges with the same relationship tuple; receipt_id is optional for explicit-coordinate feedback.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `receipt_id` | no | string |  |
| `action` | yes | enum: approve, reject, correct, flag |  |
| `source` | yes | enum: human, agent |  |
| `from_type` | yes | string |  |
| `from_id` | yes | string |  |
| `relationship_type` | yes | string |  |
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

**Purpose:** Use when a query receipt and result index identify the relationship that needs feedback. This path requires receipt_id because the receipt/result selection is the target selector.

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
| `group_override` | no | boolean | Mark the selected edge assertion metadata as a group override. |
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

**Purpose:** Use when you need to record several relationship feedback decisions from the same review session.

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

**Purpose:** Use when you need to record what happened after a decision, query, workflow, or reviewed relationship.

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

**Purpose:** Use when you need a paged list of entities, relationships, receipts, feedback, or outcomes with optional filters. Use resource_type='entities' with entity_type and optional fields to reduce payload size; use where for bounded property predicates such as {'status': {'eq': 'active'}}. Entity and edge items default to the compact output profile; ask for profile='standard' or 'full' when you need provenance or actor context. Always check truncated: when true, pass the returned continuation_token back as continuation (same filters) to fetch the next page; a stale-continuation error means state changed - restart from the first page. read_revision on the envelope is the state freshness marker; receipts prove computation, never freshness.

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
| `offset` | no | integer |  |
| `property_filter` | no | object | null |  |
| `where` | no | object | null | Bounded entity/edge property predicates such as `{"status": {"eq": "active"}}`, `{"title": {"contains": "query"}}`, or `{"status": {"in": ["active", "planned"]}}`. |
| `operation_type` | no | string | null |  |
| `fields` | no | array[string] | null | Entity property fields to include for `resource_type="entities"`. |
| `relationship_state` | no | string | null | Read-visibility state (`live`, `accepted`, `all`, `not-live`, `pending`, `reviewable`). For entities it gates by lifecycle (default `live`); for edges by review+lifecycle (default returns all stored edges). |
| `profile` | no | string | null | Output profile: `compact` (default on this surface), `standard`, or `full`. Compact returns bounded identity cards that keep lifecycle/review markers; standard/full include provenance and actor context. |
| `continuation` | no | string | null | Continuation token from a previous truncated page; repeat the SAME filters. Bound to the instance, config, and `read_revision`; replay after a mutation fails with a typed 409 stale-continuation error — restart the read. Malformed or re-bound tokens fail with 422. |

**Returns:** Top-level fields: `items`, `total`, `limit`, `offset`, `truncated`, `read_revision`, `continuation_token`

`continuation_token` is present iff the page is truncated and resumable —
the pagination loop is: check `truncated`, pass `continuation_token` back as
`continuation` with the same filters. `read_revision` is the instance's
monotonic state revision at read time; receipts prove computation, never
freshness.

**Side Effects:** Read-only.

For entity lists, `fields` is an opt-in projection that reduces payload size
after the caller has selected an entity type. It trims entity `properties` but
always keeps `entity_type` and `entity_id`; it is not topic search.
Use `where` for bounded property predicates on entity or edge lists, for
example `{"status": {"eq": "active"}}` or
`{"dependency_basis": {"contains": "schema"}}`. This is not semantic search.
For `resource_type="edges"`, this is a stored-relationship inspection surface:
it may return pending, rejected, or otherwise non-live stored edges. Named
queries are logical-state reads and apply `relationship_state` filtering, so
use `cruxible_query` when you need live/reviewable truth rather than store
inspection.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_evaluate

**Permission:** `READ_ONLY`

**Purpose:** Use when you need graph quality findings such as orphaned entities, coverage gaps, constraint issues, or candidate opportunities.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `max_findings` | no | integer |  |
| `exclude_orphan_types` | no | array or null |  |
| `severity_filter` | no | array | Optional list of `error`, `warning`, or `info` severities to return. |
| `category_filter` | no | array | Optional list of evaluate categories to return. |

**Returns:** Top-level fields: `entity_count`, `edge_count`, `findings`, `summary`, `constraint_summary`, `quality_summary`

Filtered calls still return full pre-filter `summary`, `constraint_summary`,
and `quality_summary` counts. Agent triage example: request
`severity_filter=["error"]` with `max_findings=1` to check whether any
error-level finding exists.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_stats

**Permission:** `READ_ONLY`

**Purpose:** Use when you need quick counts of entity and relationship types in an instance.

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

**Purpose:** Use when you need a combined quality report for config, graph state, feedback, and outcome coverage.

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

**Purpose:** Use when you need the allowed feedback codes and guidance for a relationship type.

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

**Purpose:** Use when you need patterns from recorded feedback, such as common corrections or recurring review issues.

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

**Purpose:** Use when you need the allowed outcome codes and guidance for a decision surface.

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

**Purpose:** Use when you need patterns from recorded outcomes for a query, workflow, relationship, or decision surface.

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

**Purpose:** Use when you need the active entity types, relationships, queries, workflows, and governance settings.

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

**Purpose:** Use when you need example entities of one type before writing a query or review. Items default to the compact output profile; ask for profile='standard' or 'full' for complete property bags and metadata.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `entity_type` | yes | string |  |
| `limit` | no | integer |  |
| `fields` | no | array[string] | null | Entity property fields to include in sampled entities. |
| `profile` | no | string | null | Output profile: `compact` (default on this surface), `standard`, or `full`. Compact returns bounded identity cards that keep lifecycle/review markers; standard/full include provenance and actor context. |

**Returns:** Top-level fields: `items`, `total`, `limit`, `offset`, `truncated`, `entity_type`

**Side Effects:** Read-only.

`fields` is an opt-in projection for compact samples. It trims entity
`properties` but always keeps `entity_type` and `entity_id`.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_inspect_entity

**Permission:** `READ_ONLY`

**Purpose:** Use when you need everything relevant about one entity within a bounded number of hops — the generic neighborhood read beneath named queries. Anchor on the entity, then expand: depth (1-4) sets the hop horizon; max_nodes and max_edges are explicit budgets and the response reports truncated with truncation_reasons instead of silently clipping. Filter with relationship_types and target_types; state selects relationship visibility exactly like query traversal (default all — every stored edge with its review/lifecycle markers, so pending edges in governed overlays are visible by default; an explicit state filters like traversal and edges_hidden_by_state counts edges at the explored frontier hidden by state alone). projection trims neighbor properties; payloads default to the compact output profile — ask for profile='standard' or 'full' when you need provenance or actor context. When the expanded read reports truncated on a budget, pass the returned continuation_token back as continuation (same parameters) to resume the expansion where it stopped; a stale-continuation error means state changed - restart the read.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `entity_type` | yes | string |  |
| `entity_id` | yes | string |  |
| `direction` | no | string | `incoming`, `outgoing`, or `both` (default). |
| `relationship_type` | no | string | null | Legacy single relationship filter; unions with `relationship_types` when both are given. |
| `limit` | no | integer | null | Legacy single-hop neighbor cap; maps to `max_nodes` on expanded reads so the cap reports `truncated` instead of clipping silently. |
| `depth` | no | integer | null | Hop horizon for the expanded read (1-4, default 1). Providing it — even `depth=1` — opts into the expanded nodes/edges shape. |
| `relationship_types` | no | array | null | Repeatable relationship filter for the expanded read; unions with `relationship_type`. |
| `target_types` | no | array | null | Only expand into/return entities of these types (the anchor is exempt). |
| `state` | no | string | null | Relationship visibility for the expanded read: `all` (default — every stored edge, the inspection contract), `live`, `accepted`, `not-live`, `pending`, `reviewable` — explicit states filter with identical semantics to named-query traversal. |
| `projection` | no | array | null | Neighbor property names to keep (the anchor keeps full properties). Composes with `profile`: projection selects properties, profile shapes metadata. |
| `max_nodes` | no | integer | null | Node budget for the expanded read (default 100, hard cap 500). |
| `max_edges` | no | integer | null | Edge budget for the expanded read (default 200, hard cap 1000). |
| `profile` | no | string | null | Output profile: `compact` (default on this surface), `standard`, or `full`. Compact returns bounded identity cards that keep lifecycle/review markers; standard/full include provenance and actor context. |
| `continuation` | no | string | null | Continuation token from a previous budget-truncated expanded read; repeat the SAME structural parameters (entity, depth, direction, filters, state). Bound to the instance, config, and `read_revision`; replay after a mutation fails with a typed 409 stale-continuation error — restart the read. |

**Returns:** Legacy calls (no neighborhood argument): `found`, `entity_type`, `entity_id`, `properties`, `metadata`, `neighbors`, `total_neighbors`, `read_revision`. Expanded calls: `found`, `entity_type`, `entity_id`, `properties`, `metadata` (the anchor card), `depth`, `state`, `nodes` (each with `depth` and lifecycle markers), `edges` (each with review/lifecycle markers), `truncated`, `truncation_reasons` (`node_budget`/`edge_budget`/`depth`), `nodes_returned`, `edges_returned`, `edges_hidden_by_state` (edges at the explored frontier that passed every other filter but were excluded solely by an explicit state; always present, 0 under `state=all`), `read_revision`, `continuation_token` (present iff truncated on a budget — depth-horizon truncation is a different read, not a resumable page; the resumed pages are disjoint and their union is exactly the untruncated result set). `read_revision` marks state freshness; receipts prove computation, never freshness.

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_inspect_entity_history

**Permission:** `READ_ONLY`

**Purpose:** Use when you need receipt-derived property changes for one entity type or entity.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `entity_type` | yes | string |  |
| `entity_id` | no | string | null |  |
| `limit` | no | integer |  |
| `offset` | no | integer |  |

**Returns:** Top-level fields: `entity_type`, `entity_id`, `items`, `total`, `legacy_entity_write_count`, `warnings`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_inspect_ontology

**Permission:** `READ_ONLY`

**Purpose:** Use when you need a compact overview of entity types, relationships, and rules.

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

**Purpose:** Use when you need to understand the workflows declared by the active config.

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

**Purpose:** Use when you need to understand configured queries and their parameters.

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

**Purpose:** Use when you need to review feedback, outcome, group, and policy settings.

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

**Purpose:** Use when you need a single high-level summary of the instance.

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

## cruxible_add_relationship

**Permission:** `GRAPH_WRITE`

**Purpose:** Use when you need to add or update a small number of explicit relationships and the endpoint entities already exist. Set pending=true when the edge should enter relationship review state instead of immediately becoming live.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `relationships` | yes | array |  |
| `dry_run` | no | boolean | false | Validate (schema + mutation guards) without mutating graph state |

**Returns:** Top-level fields: `added`, `updated`, `pending_conflicts`, `updated_group_backed_edges`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_add_entity

**Permission:** `GRAPH_WRITE`

**Purpose:** Use when you need to add or update a small number of explicit entities.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `entities` | yes | array |  |
| `dry_run` | no | boolean | false | Validate (schema + mutation guards) without mutating graph state |

**Returns:** Top-level fields: `entities_added`, `entities_updated`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_batch_direct_write

**Permission:** `GRAPH_WRITE`

**Purpose:** Use when you need to validate or apply one coherent batch of explicit entities and relationships; set dry_run first.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `payload` | yes | BatchDirectWritePayload | Object with `entities` (entity inputs), `relationships` (relationship inputs, each optionally referencing `shared_evidence_keys`), and `shared_evidence` (map of key to shared evidence refs/source evidence). |
| `dry_run` | no | boolean | Validate the payload without mutating graph state. |

**Returns:** Top-level fields: `dry_run`, `valid`, `entities_added`, `entities_updated`, `relationships_added`, `relationships_updated`, `validation_errors`, `validation_warnings`, `evidence_sources_used`, `pending_conflicts`, `updated_group_backed_edges`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_add_constraint

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to add a graph quality rule that future evaluations should check.

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

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to record a policy that affects how a decision surface should be handled.

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

**Purpose:** Use when you need to replace or reload the active config for an instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `config_path` | no | string | null |  |
| `config_yaml` | no | string | null |  |
| `allow_orphans` | no | boolean | Allow stored graph types absent from the incoming config (default false: strandings refuse the reload with per-type counts). |
| `config_source_manifest` | no | object | Source paths and digests for uploaded composed YAML. File-based handlers build this automatically. |

**Returns:** Top-level fields: `config_path`, `updated`, `warnings`, `type_delta`, `strandings`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_config_status

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to check source drift or active config integrity.

**Arguments:** `instance_id` is required. `current_source_manifest` is optional;
without it, only the active materialized digest is checked.

**Returns:** `status`, `config_path`, `materialized_matches`, `sources_checked`,
`composed_matches`, `changed_sources`, and recorded `provenance`.

## cruxible_propose_workflow

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when a workflow proposes reviewable relationship changes instead of writing them directly.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `workflow_name` | yes | string |  |
| `input_payload` | no | object | null |  |
| `decision_record_id` | no | string | null |  |

**Returns:** Top-level fields: `workflow`, `output`, `receipt_id`, `mode`, `workflow_type`, `canonical`, `group_id`, `group_status`, `review_priority`, `suppressed`, `suppressed_members`, `query_receipt_ids`, `read_metadata`, `trace_ids`, `prior_resolution`, `policy_summary`, `receipt`, `traces`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_create_decision_record

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to open a tracked decision before gathering evidence, running workflows, or recording outcomes.

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

**Purpose:** Use when you need the current state and optional event history for one decision.

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

**Purpose:** Use when you need to find decision records by status, subject, class, or page.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `status` | no | string | null |  |
| `subject_type` | no | string | null |  |
| `subject_id` | no | string | null |  |
| `decision_class` | no | string | null |  |
| `limit` | no | integer |  |
| `offset` | no | integer |  |

**Returns:** Top-level fields: `items`, `total`, `limit`, `offset`, `truncated`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list_decision_events

**Permission:** `READ_ONLY`

**Purpose:** Use when you need the event timeline for decisions, optionally filtered by receipt.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `decision_record_id` | no | string | null |  |
| `receipt_id` | no | string | null |  |
| `trace_id` | no | string | null |  |
| `status` | no | string | null |  |
| `limit` | no | integer |  |
| `offset` | no | integer |  |

**Returns:** Top-level fields: `items`, `total`, `limit`, `offset`, `truncated`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_finalize_decision_record

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when a tracked decision has a final answer and rationale.

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

**Purpose:** Use when a tracked decision should be closed without a final decision.

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

## cruxible_propose_procedure

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to propose a bounded composition of procedure-exported actions for independent review.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID. |
| `definition` | yes | object | Restricted procedure definition. |
| `supersedes_procedure_id` | no | string or null | Immutable procedure being replaced. |
| `evidence_refs` | no | array or null | Distillation evidence refs. |

**Returns:** The pending procedure record and transition receipt ID.

**Side Effects:** Persists a pending procedure and receipt.

## cruxible_list_procedures

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to find governed procedures by lifecycle status or page.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID. |
| `status` | no | string or null | `pending`, `live`, `rejected`, or `retired`. |
| `limit` | no | integer | Maximum records. |
| `offset` | no | integer | Records to skip. |

**Returns:** Standard list envelope with procedure records.

**Side Effects:** Read-only.

## cruxible_get_procedure

**Permission:** `READ_ONLY`

**Purpose:** Use when you need one procedure's definition, budget, precondition, and lifecycle.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID. |
| `procedure_id` | yes | string | Procedure ID. |

**Returns:** A `procedure` object envelope.

**Side Effects:** Read-only.

## cruxible_resolve_procedure

**Permission:** `GRAPH_WRITE`

**Purpose:** Use when an independent reviewer needs to promote or reject a pending procedure.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID. |
| `procedure_id` | yes | string | Procedure ID. |
| `action` | yes | string | `promote` or `reject`. |
| `expected_version` | yes | integer | Optimistic lifecycle version. |
| `reason` | no | string or null | Required when rejecting. |

**Returns:** The transitioned procedure and receipt ID.

**Side Effects:** Promotes or rejects a pending procedure and writes a receipt.

## cruxible_retire_procedure

**Permission:** `GRAPH_WRITE`

**Purpose:** Use when a reviewer needs to retire a live immutable procedure.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID. |
| `procedure_id` | yes | string | Procedure ID. |
| `expected_version` | yes | integer | Optimistic lifecycle version. |
| `reason` | yes | string | Non-empty retirement reason. |

**Returns:** The retired procedure and receipt ID.

**Side Effects:** Retires a live procedure and writes a receipt.

## cruxible_run_procedure

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to execute one live procedure through the generic receipted runner.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID. |
| `procedure_id` | yes | string | Procedure ID. |
| `input_payload` | yes | object | Input validated against the procedure contract. |

**Returns:** Procedure, run, output, receipt, and step-output fields.

**Side Effects:** Writes a crash-safe run record, provider traces where applicable, and a receipt.

## cruxible_list_procedure_runs

**Permission:** `READ_ONLY`

**Purpose:** Use when you need invocation history or crash-visible started tombstones for one procedure.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string | Governed instance ID. |
| `procedure_id` | yes | string | Procedure ID. |
| `limit` | no | integer | Maximum records. |
| `offset` | no | integer | Records to skip. |

**Returns:** Standard list envelope containing finalized runs and
`status: started`/`verdict: null` tombstones.

**Side Effects:** Read-only.

## cruxible_propose_group

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to create a review group for candidate relationship changes.

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

**Purpose:** Use when a reviewer approves, rejects, or otherwise resolves a pending group.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `group_id` | yes | string |  |
| `action` | yes | enum: approve, reject |  |
| `expected_pending_version` | yes | integer |  |
| `rationale` | no | string |  |
| `resolved_by` | no | enum: human, agent |  |
| `stamp_existing` | no | boolean | On approve, bless each surviving pre-existing edge (member tuple already live) with this group's review status and provenance instead of skipping it. |

**Returns:** Top-level fields: `group_id`, `action`, `edges_created`, `edges_skipped`, `resolution_id`, `receipt_id`, `skipped_members` (per-member skip explanations: identity plus `skip_kind`, `reason`, `stamped`), `edges_stamped`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_update_trust_status

**Permission:** `GRAPH_WRITE`

**Purpose:** Use when you need to mark a prior group resolution as trusted, invalidated, or otherwise updated.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `resolution_id` | yes | string |  |
| `trust_status` | yes | enum: trusted, watch, invalidated |  |
| `reason` | no | string |  |

**Returns:** Top-level fields: `resolution_id`, `trust_status`, `receipt_id`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_get_group

**Permission:** `READ_ONLY`

**Purpose:** Use when you need the details and members for one candidate relationship group.

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

**Purpose:** Use when you need to find candidate relationship groups by type, status, or page.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `relationship_type` | no | string | null |  |
| `status` | no | enum: pending_review, auto_resolved, applying, resolved | null |  |
| `limit` | no | integer |  |
| `offset` | no | integer |  |

**Returns:** Top-level fields: `items`, `total`, `limit`, `offset`, `truncated`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_list_resolutions

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to review past group decisions by relationship type or action.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `relationship_type` | no | string | null |  |
| `action` | no | enum: approve, reject | null |  |
| `limit` | no | integer |  |
| `offset` | no | integer |  |

**Returns:** Top-level fields: `items`, `total`, `limit`, `offset`, `truncated`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_group_status

**Permission:** `READ_ONLY`

**Purpose:** Use when you need the latest status for a group or for a known group signature.

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

## cruxible_state_publish

**Permission:** `ADMIN`

**Purpose:** Use when you need to publish the current instance state as an immutable release.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `transport_ref` | yes | string |  |
| `state_id` | yes | string |  |
| `release_id` | yes | string |  |
| `compatibility` | yes | enum: data_only, additive_schema, breaking |  |

**Returns:** Top-level fields: `manifest`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_create_snapshot

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to mark the current state with a named snapshot.

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

**Purpose:** Use when you need to browse available snapshots for an instance.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `limit` | no | integer |  |
| `offset` | no | integer |  |

**Returns:** Top-level fields: `items`, `total`, `limit`, `offset`, `truncated`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_register_source_artifact

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to register a source document so relationship evidence can cite stable chunks from it.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `source_path` | yes | string | Path to the local source document. |
| `source_artifact_id` | no | string | null | Caller-supplied artifact id so pinned evidence locators can reference it deterministically; server-generated when omitted. Must be 3-64 chars of `[A-Za-z0-9._-]` starting with an alphanumeric. Duplicate ids are refused by the service. |
| `source_kind` | no | enum: markdown | Only `markdown` is currently supported. |
| `source_retention` | no | enum: manifest_only, archive | `manifest_only` stores chunk hashes only; `archive` also stores the document content. |
| `original_uri` | no | string | null | Original document location for provenance. |
| `label` | no | string | null | Human-readable label for the artifact. |

**Returns:** Top-level fields: `source_artifact_id`, `source_kind`, `source_retention`, `original_uri`, `label`, `content_hash`, `byte_count`, `parser_version`, `archived`, `archive_content_hash`, `chunks`

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Invalid or duplicate caller-supplied `source_artifact_id`.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_dereference_source_evidence

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to read back a registered source evidence chunk and verify its expected content hash.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `source_artifact_id` | yes | string | Artifact ID returned by `cruxible_register_source_artifact`. |
| `chunk_id` | no | string | null | Chunk ID locator. |
| `heading_path` | no | array | null | Heading-path locator (used with `block_selector`). |
| `block_selector` | no | string | null | Block selector within the heading path. |
| `expected_content_hash` | no | string | null | Expected chunk content hash for drift detection. |

**Returns:** Top-level fields: `status` (one of `available`, `drifted`, `unavailable`), `source_artifact_id`, `chunk_id`, `content_hash`, `expected_artifact_hash`, `current_artifact_hash`, `body_origin`, `body`, `reason`, `chunk`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_clone_snapshot

**Permission:** `ADMIN`

**Purpose:** Use when you need a new local instance created from an existing snapshot. On auth-enabled daemons the result carries a one-time admin_credential token for the new instance - save it immediately; it is never shown again.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `snapshot_id` | yes | string |  |
| `root_dir` | yes | string |  |

**Returns:** Top-level fields: `instance_id`, `snapshot`, `admin_credential` (auth-enabled daemons only: a one-time ADMIN token for the new instance — deliver it to the operator immediately; it is never shown again)

**Side Effects:** May create governed state, graph state, config changes, snapshots, or audit records according to its permission tier.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_state_status

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to see whether an overlay is connected to an upstream state and whether pulls are available.

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

## cruxible_state_pull_preview

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to preview upstream state changes before applying them.

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

## cruxible_state_pull_apply

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when a pull preview returned an apply digest and you are ready to apply it.

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

**Purpose:** Use when you need to fetch one entity by type and ID. The payload defaults to the compact output profile; ask for profile='standard' or 'full' for the complete property bag and metadata.

**Arguments:**

| Name | Required | Type | Description |
| --- | --- | --- | --- |
| `instance_id` | yes | string |  |
| `entity_type` | yes | string |  |
| `entity_id` | yes | string |  |
| `profile` | no | string | null | Output profile: `compact` (default on this surface), `standard`, or `full`. Compact returns bounded identity cards that keep lifecycle/review markers; standard/full include provenance and actor context. |

**Returns:** Top-level fields: `found`, `entity_type`, `entity_id`, `properties`, `metadata`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_get_relationship

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to fetch one relationship by endpoints and relationship type.

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

**Returns:** Top-level fields: `found`, `from_type`, `from_id`, `relationship_type`, `to_type`, `to_id`, `edge_key`, `properties`, `metadata`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Missing config names, stale locks, invalid workflow/query/group identifiers, or invalid request shape where applicable.

## cruxible_relationship_lineage

**Permission:** `READ_ONLY`

**Purpose:** Use when you need the provenance, review state, feedback, and receipts for one relationship.

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

**Returns:** Top-level fields: `found`, `relationship`, `provenance`, `group`, `resolution`, `source_workflow_receipt_id`, `source_trace_ids`, `warnings`

**Side Effects:** Read-only.

**Common Errors:**
- Unknown `instance_id` or missing daemon configuration.
- Permission mode too low for this tool.
- Ambiguous relationship tuple without `edge_key`.
