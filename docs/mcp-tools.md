# MCP Tools Reference

Cruxible Core exposes MCP tools through the [Model Context Protocol](https://modelcontextprotocol.io) (MCP). AI agents (Claude Code, Cursor, Codex, etc.) use these tools to orchestrate the full decision lifecycle: validate configs, lock and execute workflows, query the graph, provide feedback, and evaluate quality.

For local instances, MCP and CLI are the same shape over the same runtime. For remote or governed instances, the authenticated HTTP API is the primary execution surface and MCP is one client adapter over it.

## Setup

Install the MCP runtime with:

```bash
pip install "cruxible-core[mcp]"
```

If you are writing a separate HTTP client that talks to an already-running daemon, install `cruxible-client` in that agent environment instead of `cruxible-core`.

Add to your MCP client config (Claude Code / Cursor use `.mcp.json`; see [README](../README.md#mcp-setup) for Codex):

```json
{
  "mcpServers": {
    "cruxible": {
      "command": "cruxible-mcp",
      "env": {
        "CRUXIBLE_MODE": "admin"
      }
    }
  }
}
```

## Permission Modes

Each tool requires a minimum permission tier. Set via the `CRUXIBLE_MODE` environment variable.

| Mode | Env Value | Description |
|------|-----------|-------------|
| `READ_ONLY` | `read_only` | Query, inspect, validate — no mutations |
| `GOVERNED_WRITE` | `governed_write` | READ_ONLY + receipt-persisting workflow runs, governed proposal, feedback |
| `GRAPH_WRITE` | `graph_write` | GOVERNED_WRITE + raw graph mutation and proposal resolution |
| `ADMIN` | `admin` | All tools including canonical workflow apply, ingest, and config mutation |

Default is `ADMIN` if unset.

These tiers are enforced at the daemon boundary. They are meaningful when an agent talks to a running Cruxible daemon through MCP/HTTP, not when it can import `cruxible-core` runtime modules directly in the same environment.

---

## Utility Tools

### cruxible_version

Return the cruxible-core version. Use this to confirm which build is running.

**Permission:** READ_ONLY

_No parameters._

**Returns:**

| Field | Type | Description |
|-------|------|-------------|
| `version` | string | Installed cruxible-core version (e.g., `"0.3.3"`) |

---

## Lifecycle Tools

### cruxible_validate

Validate a config without creating an instance. Always run this before `cruxible_init`.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `config_path` | string | conditional | Path to a YAML config file |
| `config_yaml` | string | conditional | Raw YAML string |

Provide exactly one of `config_path` or `config_yaml`.

**Returns:** `ValidateResult`

| Field | Type | Description |
|-------|------|-------------|
| `valid` | bool | Whether the config passed validation |
| `name` | string | Config name |
| `entity_types` | list[string] | Entity type names |
| `relationships` | list[string] | Relationship names |
| `named_queries` | list[string] | Query names |
| `warnings` | list[string] | Non-fatal warnings |

---

### cruxible_init

Create a new instance or reload an existing one.

**Permission:** READ_ONLY (reload) / ADMIN (create)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `root_dir` | string | **yes** | Directory for the `.cruxible/` instance |
| `config_path` | string | conditional | Path to a YAML config file (new instance) |
| `config_yaml` | string | conditional | Raw YAML string (new instance) |
| `data_dir` | string | no | Directory for data files |
| `kit` | string | conditional | Standalone kit alias or ref to materialize |

To create a new instance, provide exactly one of `config_path`, `config_yaml`, or `kit`.
`kit` accepts standalone kits only. To reload, omit all three.

**Returns:** `InitResult`

| Field | Type | Description |
|-------|------|-------------|
| `instance_id` | string | Unique instance identifier (use in all subsequent calls) |
| `status` | string | `"initialized"` or `"loaded"` |

---

### cruxible_lock_workflow

Generate the workflow lock file for the current instance config.

**Permission:** ADMIN

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID from `cruxible_init` |

Use this after changing workflow config, providers, or artifacts and before planning or executing workflows.

---

### cruxible_plan_workflow

Compile a configured workflow into a concrete execution plan.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `workflow_name` | string | **yes** | Workflow name from config |
| `input_payload` | dict | no | Structured workflow input |

---

### cruxible_run_workflow

Execute a configured workflow and return its output, receipt, and traces.

**Permission:** GOVERNED_WRITE

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `workflow_name` | string | **yes** | Workflow name from config |
| `input_payload` | dict | no | Structured workflow input |

Canonical workflows run in preview mode and return `apply_digest` plus `head_snapshot_id`. Pass those to `cruxible_apply_workflow` to commit.

---

### cruxible_apply_workflow

Apply a canonical workflow after verifying the preview identity.

**Permission:** ADMIN

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `workflow_name` | string | **yes** | Canonical workflow name |
| `expected_apply_digest` | string | **yes** | Preview digest from `cruxible_run_workflow` |
| `expected_head_snapshot_id` | string | no | Snapshot ID returned by preview |
| `input_payload` | dict | no | Structured workflow input |

---

### cruxible_ingest

Ingest data through a named ingestion mapping.

**Permission:** ADMIN

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID from `cruxible_init` |
| `mapping_name` | string | **yes** | Ingestion mapping name from config |
| `file_path` | string | conditional | Path to a CSV or JSON file |
| `data_csv` | string | conditional | Inline CSV string |
| `data_json` | string or list | conditional | Inline JSON array of row objects |
| `data_ndjson` | string | conditional | Inline NDJSON string (one JSON object per line) |
| `upload_id` | string | conditional | Reserved for cloud mode |

Deprecated for new configs: prefer workflow-based deterministic loading with `cruxible_lock_workflow`, `cruxible_run_workflow`, and `cruxible_apply_workflow`.

Provide exactly one data source. Ingest entity mappings before relationship mappings.

**Returns:** `IngestResult`

| Field | Type | Description |
|-------|------|-------------|
| `records_ingested` | int | Number of records loaded |
| `mapping` | string | Mapping name used |
| `entity_type` | string or null | Entity type (if entity mapping) |
| `relationship_type` | string or null | Relationship type (if relationship mapping) |

---

## Query Tools

### cruxible_query

Run a named query and return results with a receipt.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `query_name` | string | **yes** | Named query from config |
| `params` | dict | no | Query parameters (e.g., `{"drug_id": "warfarin"}`) |
| `limit` | int | no | Maximum results to return |

**Returns:** `QueryToolResult`

| Field | Type | Description |
|-------|------|-------------|
| `results` | list[dict] | Matched entities with properties |
| `receipt_id` | string or null | Receipt ID for provenance tracking |
| `receipt` | dict or null | Inline receipt data |
| `total_results` | int | Total number of results |
| `steps_executed` | int | Number of traversal steps executed |

---

### cruxible_receipt

Fetch a stored receipt from a previous query.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `receipt_id` | string | **yes** | Receipt ID from a prior `cruxible_query` |

**Returns:** Full receipt dict with traversal evidence, timestamps, and provenance chain.

---

### cruxible_find_candidates

Find missing-relationship candidates using deterministic strategies.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `relationship_type` | string | **yes** | Relationship type to find candidates for |
| `strategy` | string | **yes** | `"property_match"` or `"shared_neighbors"` |
| `match_rules` | list[dict] | conditional | Rules for `property_match` (each: `{from_property, to_property, operator}`) |
| `via_relationship` | string | conditional | Relationship for `shared_neighbors` |
| `min_overlap` | float | no | Minimum neighbor overlap (default: `0.5`) |
| `min_confidence` | float | no | Minimum confidence threshold (default: `0.5`) |
| `limit` | int | no | Maximum candidates to return (default: `20`) |
| `min_distinct_neighbors` | int | no | Minimum neighbors per entity for `shared_neighbors` (default: `2`) |

**Strategy: `property_match`** — Requires `match_rules`. Operators:
- `equals` (default): Type-strict hash-join, O(n+m)
- `iequals`: Case-insensitive hash-join, O(n+m)
- `contains`: Substring match, brute-force scan

**Strategy: `shared_neighbors`** — Requires `via_relationship`. Finds entity pairs sharing common neighbors through the specified relationship.

**Returns:** `CandidatesResult`

| Field | Type | Description |
|-------|------|-------------|
| `candidates` | list[dict] | Candidate relationship pairs with confidence scores |
| `total` | int | Total candidates found |

---

## Feedback Tools

### cruxible_feedback

Record edge-level feedback tied to a receipt.

**Permission:** GRAPH_WRITE

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `receipt_id` | string | **yes** | Receipt ID the feedback applies to |
| `action` | string | **yes** | `"approve"`, `"reject"`, `"correct"`, or `"flag"` |
| `source` | string | **yes** | `"human"` or `"agent"` |
| `from_type` | string | **yes** | Source entity type |
| `from_id` | string | **yes** | Source entity ID |
| `relationship` | string | **yes** | Relationship type |
| `to_type` | string | **yes** | Target entity type |
| `to_id` | string | **yes** | Target entity ID |
| `edge_key` | int | no | Edge key for multi-edge disambiguation |
| `reason` | string | no | Reason for the feedback (default: `""`) |
| `corrections` | dict | no | Property corrections (for `action="correct"`) |

**Returns:** `FeedbackResult`

| Field | Type | Description |
|-------|------|-------------|
| `feedback_id` | string | Unique feedback record ID |
| `applied` | bool | Whether the feedback was applied to the graph edge |

**Behavior:**
- `reject`: Excluded from future query results
- `approve`: Trusted in traversals
- `correct`: Updates edge properties (pass `corrections` dict)
- `flag`: Marks for review without changing behavior

---

### cruxible_outcome

Record the outcome of a decision (query result accuracy).

**Permission:** GRAPH_WRITE

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `receipt_id` | string | **yes** | Receipt ID |
| `outcome` | string | **yes** | `"correct"`, `"incorrect"`, `"partial"`, or `"unknown"` |
| `detail` | dict | no | Additional outcome details |

**Returns:** `OutcomeResult`

| Field | Type | Description |
|-------|------|-------------|
| `outcome_id` | string | Unique outcome record ID |

---

## Inspection Tools

### cruxible_list

List entities, edges, receipts, feedback, or outcomes with optional filters.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `resource_type` | string | **yes** | `"entities"`, `"edges"`, `"receipts"`, `"feedback"`, or `"outcomes"` |
| `entity_type` | string | conditional | Required when `resource_type="entities"` |
| `relationship_type` | string | no | Filter edges by relationship type (only for `resource_type="edges"`) |
| `query_name` | string | no | Filter receipts by query name |
| `receipt_id` | string | no | Filter feedback/outcomes by receipt |
| `limit` | int | no | Maximum items (default: `50`) |
| `property_filter` | dict | no | Exact property matches, AND semantics (entities and edges only) |

**Returns:** `ListResult`

| Field | Type | Description |
|-------|------|-------------|
| `items` | list[dict] | Resource items |
| `total` | int | Total count |

**Edge items** (when `resource_type="edges"`):

| Field | Type | Description |
|-------|------|-------------|
| `from_type` | string | Source entity type |
| `from_id` | string | Source entity ID |
| `to_type` | string | Target entity type |
| `to_id` | string | Target entity ID |
| `relationship_type` | string | Relationship type |
| `edge_key` | int | Edge key for use with `cruxible_feedback` |
| `properties` | dict | [Edge properties](concepts.md#edge-properties) |

---

### cruxible_schema

Return the active config schema for an instance.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |

**Returns:** Full config schema dict including entity types, relationships, queries, and constraints.

---

### cruxible_sample

Return a sample of entities for quick data inspection.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `entity_type` | string | **yes** | Entity type to sample |
| `limit` | int | no | Max entities (default: `5`) |

**Returns:** `SampleResult`

| Field | Type | Description |
|-------|------|-------------|
| `entities` | list[dict] | Sampled entity records |
| `entity_type` | string | Entity type sampled |
| `count` | int | Number returned |

---

### cruxible_evaluate

Run graph quality checks: orphan entities, coverage gaps, constraint violations,
candidate opportunities, governed support state, and unreviewed co-members.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `max_findings` | int | no | Maximum findings to return (default: `100`) |
| `exclude_orphan_types` | list[string] | no | Entity types to skip in orphan checks (for reference/taxonomy types) |

**Returns:** `EvaluateResult`

| Field | Type | Description |
|-------|------|-------------|
| `entity_count` | int | Total entities in graph |
| `edge_count` | int | Total edges in graph |
| `findings` | list[dict] | Quality findings (orphans, gaps, violations) |
| `summary` | dict | Counts by finding category |

---

### cruxible_get_entity

Look up a specific entity by type and ID.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `entity_type` | string | **yes** | Entity type |
| `entity_id` | string | **yes** | Entity ID |

**Returns:** `GetEntityResult`

| Field | Type | Description |
|-------|------|-------------|
| `found` | bool | Whether the entity exists |
| `entity_type` | string | Entity type |
| `entity_id` | string | Entity ID |
| `properties` | dict | Entity properties |

---

### cruxible_get_relationship

Look up a specific relationship by its endpoints and type.

**Permission:** READ_ONLY

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `from_type` | string | **yes** | Source entity type |
| `from_id` | string | **yes** | Source entity ID |
| `relationship_type` | string | **yes** | Relationship type |
| `to_type` | string | **yes** | Target entity type |
| `to_id` | string | **yes** | Target entity ID |
| `edge_key` | int | no | Edge key for multi-edge disambiguation |

Pass `edge_key` when multiple same-type edges exist between the same endpoints. Without it, an error is raised if ambiguous.

**Returns:** `GetRelationshipResult`

| Field | Type | Description |
|-------|------|-------------|
| `found` | bool | Whether the relationship exists |
| `from_type` | string | Source entity type |
| `from_id` | string | Source entity ID |
| `relationship_type` | string | Relationship type |
| `to_type` | string | Target entity type |
| `to_id` | string | Target entity ID |
| `edge_key` | int or null | Edge key |
| `properties` | dict | [Edge properties](concepts.md#edge-properties) |

---

## Mutation Tools

### cruxible_add_entity

Add or update entities in the graph (upsert).

**Permission:** GRAPH_WRITE

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `entities` | list[EntityInput] | **yes** | Entities to add/update |

Each `EntityInput`:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_type` | string | **yes** | Entity type name |
| `entity_id` | string | **yes** | Entity ID |
| `properties` | dict | no | Entity properties (default: `{}`) |

Re-submitting an existing entity replaces all its properties (full overwrite, not merge).

**Returns:** `AddEntityResult`

| Field | Type | Description |
|-------|------|-------------|
| `entities_added` | int | New entities created |
| `entities_updated` | int | Existing entities updated |

---

### cruxible_add_relationship

Add or update relationships in the graph (upsert).

**Permission:** GRAPH_WRITE

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `relationships` | list[RelationshipInput] | **yes** | Relationships to add/update |

Each `RelationshipInput`:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `from_type` | string | **yes** | Source entity type |
| `from_id` | string | **yes** | Source entity ID |
| `relationship` | string | **yes** | Relationship type name |
| `to_type` | string | **yes** | Target entity type |
| `to_id` | string | **yes** | Target entity ID |
| `properties` | dict | no | [Edge properties](concepts.md#edge-properties) (default: `{}`) |

Entities must already exist. Re-submitting an existing edge merges declared domain
properties while preserving system review metadata. Properties must be declared
by the relationship schema. For governed judgment relationships, prefer
candidate group proposal flows so Cruxible can preserve tri-state integration
signals and review history.

**Returns:** `AddRelationshipResult`

| Field | Type | Description |
|-------|------|-------------|
| `added` | int | New relationships created |
| `updated` | int | Existing relationships updated |

---

### cruxible_add_constraint

Add a constraint rule to the config and write it back to YAML.

**Permission:** ADMIN

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `instance_id` | string | **yes** | Instance ID |
| `name` | string | **yes** | Unique constraint name |
| `rule` | string | **yes** | Rule expression (see [Config Reference](config-reference.md#rule-syntax)) |
| `severity` | string | no | `"warning"` (default) or `"error"` |
| `description` | string | no | Human-readable description |

**Returns:** `AddConstraintResult`

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Constraint name |
| `added` | bool | Whether the constraint was added |
| `config_updated` | bool | Whether the YAML file was updated |
| `warnings` | list[string] | Warnings (e.g., unknown property names) |
