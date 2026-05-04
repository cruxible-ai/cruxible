# CLI Reference

Cruxible Core provides a command-line interface for all core operations. The CLI mirrors the MCP tools for terminal usage.

```bash
cruxible --help
cruxible --version
```

---

## cruxible validate

Validate a config YAML file without creating an instance.

```bash
cruxible validate --config <path>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--config` | **yes** | Path to config YAML file |

**Example:**

```bash
cruxible validate --config demos/drug-interactions/config.yaml
# Config 'drug_interactions_demo' is valid.
#   2 entity types, 5 relationships, 4 queries
```

---

## cruxible init

Initialize a new `.cruxible/` instance in the current directory.

```bash
cruxible init --config <path> [--data-dir <dir>]
cruxible init --kit <standalone-kit>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--config` | conditional | Path to config YAML file |
| `--kit` | conditional | Standalone kit alias or ref to materialize |
| `--data-dir` | no | Directory for data files |

Provide exactly one of `--config` or `--kit`.

**Example:**

```bash
cruxible init --config config.yaml --data-dir ./data
# Initialized .cruxible/ in /path/to/project

cruxible init --kit kev-reference
# Initialized .cruxible/ in /path/to/project
```

---

## cruxible ingest

Ingest data from a file using a named mapping from the config.

```bash
cruxible ingest --mapping <name> --file <path>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--mapping` | **yes** | Ingestion mapping name from config |
| `--file` | **yes** | Path to a CSV or JSON data file |

Ingest entity mappings before relationship mappings — edges reference entity IDs.

**Example:**

```bash
cruxible ingest --mapping drugs --file data/drugs.csv
# Ingested 46 records via mapping 'drugs'.

cruxible ingest --mapping interactions --file data/interactions.csv
# Ingested 484 records via mapping 'interactions'.
```

---

## cruxible query

Execute a named query and save the receipt.

```bash
cruxible query --query <name> [--param KEY=VALUE ...]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--query` | **yes** | Named query from config |
| `--param` | no | Query parameter as `KEY=VALUE` (repeatable) |
| `--limit` | no | Maximum results to display |

**Example:**

```bash
cruxible query --query check_interactions --param drug_id=warfarin
# [table of interacting drugs with severity]
# 12 result(s), 1 step(s) executed.
# Receipt: RCP-abc123
```

---

## cruxible explain

Explain a query result using its receipt. Supports JSON, Markdown, and Mermaid output.

```bash
cruxible explain --receipt <receipt_id> [--format json|markdown|mermaid]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--receipt` | **yes** | — | Receipt ID to explain |
| `--format` | no | `markdown` | Output format: `json`, `markdown`, or `mermaid` |

**Example:**

```bash
cruxible explain --receipt RCP-abc123 --format mermaid
```

---

## cruxible feedback

Submit feedback on a specific edge from a query result.

```bash
cruxible feedback --receipt <id> --action <action> \
  --from-type <type> --from-id <id> \
  --relationship <rel> \
  --to-type <type> --to-id <id> \
  [--edge-key <int>] [--reason <text>] [--corrections <json>] \
  [--source <human|agent>]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--receipt` | **yes** | Receipt ID |
| `--action` | **yes** | `approve`, `reject`, `correct`, or `flag` |
| `--from-type` | **yes** | Source entity type |
| `--from-id` | **yes** | Source entity ID |
| `--relationship` | **yes** | Relationship type |
| `--to-type` | **yes** | Target entity type |
| `--to-id` | **yes** | Target entity ID |
| `--edge-key` | no | Edge key for multi-edge disambiguation |
| `--reason` | no | Reason for feedback |
| `--corrections` | no | JSON object of edge property corrections (for `correct`) |
| `--source` | no | `human` (default) or `agent` |

**Example:**

```bash
cruxible feedback --receipt RCP-abc123 --action reject \
  --from-type Drug --from-id fluoxetine \
  --relationship inhibits \
  --to-type Enzyme --to-id CYP2D6 \
  --reason "Confidence too low, insufficient evidence"
# Feedback fb-xyz789 applied to graph.
```

---

## cruxible outcome

Record the outcome of a decision.

```bash
cruxible outcome --receipt <id> --outcome <value> [--detail <json>]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--receipt` | **yes** | Receipt ID |
| `--outcome` | **yes** | `correct`, `incorrect`, `partial`, or `unknown` |
| `--detail` | no | JSON string with outcome details |

**Example:**

```bash
cruxible outcome --receipt RCP-abc123 --outcome correct
# Outcome out-def456 recorded.
```

---

## cruxible get-entity

Look up a specific entity by type and ID.

```bash
cruxible get-entity --type <entity_type> --id <entity_id>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--type` | **yes** | Entity type |
| `--id` | **yes** | Entity ID |

Prints "Not found." and exits 0 when the entity doesn't exist.

**Example:**

```bash
cruxible get-entity --type Drug --id warfarin
# [table showing entity properties]

cruxible get-entity --type Drug --id NONEXISTENT
# Not found.
```

---

## cruxible get-relationship

Look up a specific relationship by its endpoints and type.

```bash
cruxible get-relationship --from-type <type> --from-id <id> \
  --relationship <rel> --to-type <type> --to-id <id> [--edge-key <int>]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--from-type` | **yes** | Source entity type |
| `--from-id` | **yes** | Source entity ID |
| `--relationship` | **yes** | Relationship type |
| `--to-type` | **yes** | Target entity type |
| `--to-id` | **yes** | Target entity ID |
| `--edge-key` | no | Edge key for multi-edge disambiguation |

Prints "Not found." and exits 0 when the relationship doesn't exist. Errors with exit 1 when multiple edges exist and `--edge-key` is not specified.

**Example:**

```bash
cruxible get-relationship --from-type Drug --from-id warfarin \
  --relationship interacts_with --to-type Drug --to-id simvastatin
# [table showing relationship properties]
```

---

## cruxible add-entity

Add or update an entity in the graph (upsert).

```bash
cruxible add-entity --type <entity_type> --id <entity_id> [--props <json>]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--type` | **yes** | Entity type (must exist in config) |
| `--id` | **yes** | Entity ID |
| `--props` | no | JSON object of entity properties |

**Example:**

```bash
cruxible add-entity --type Drug --id metoprolol \
  --props '{"drug_id": "metoprolol", "name": "Metoprolol", "therapeutic_class": "beta_blockers"}'
# Entity Drug:metoprolol added.
```

---

## cruxible add-relationship

Add or update a relationship in the graph (upsert).

```bash
cruxible add-relationship --from-type <type> --from-id <id> \
  --relationship <rel> --to-type <type> --to-id <id> [--props <json>]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--from-type` | **yes** | Source entity type |
| `--from-id` | **yes** | Source entity ID |
| `--relationship` | **yes** | Relationship type (must exist in config) |
| `--to-type` | **yes** | Target entity type |
| `--to-id` | **yes** | Target entity ID |
| `--props` | no | JSON object of [edge properties](concepts.md#edge-properties) |

Both endpoint entities must exist. Direction must match config (from_type matches the relationship's `from` entity).

**Example:**

```bash
cruxible add-relationship --from-type Drug --from-id metoprolol \
  --relationship metabolized_by --to-type Enzyme --to-id CYP2D6 \
  --props '{"source": "manual"}'
# Relationship added: Drug:metoprolol -[metabolized_by]-> Enzyme:CYP2D6
```

---

## cruxible add-constraint

Add a constraint rule to the config YAML.

```bash
cruxible add-constraint --name <name> --rule <rule> \
  [--severity warning|error] [--description <text>]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--name` | **yes** | — | Constraint name (must be unique) |
| `--rule` | **yes** | — | Rule expression |
| `--severity` | no | `warning` | `warning` or `error` |
| `--description` | no | — | Description of the constraint |

Rule syntax: `RELATIONSHIP.FROM.property <op> RELATIONSHIP.TO.property`

Supported operators: `==`, `!=`, `>`, `>=`, `<`, `<=`

**Example:**

```bash
cruxible add-constraint --name no_self_interaction \
  --rule "interacts_with.FROM.drug_id != interacts_with.TO.drug_id" \
  --severity error \
  --description "A drug should not interact with itself"
# Constraint 'no_self_interaction' added to config.
```

---

## cruxible list

List entities, edges, receipts, feedback, or outcomes.

### cruxible list entities

```bash
cruxible list entities --type <entity_type> [--limit <n>]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--type` | **yes** | — | Entity type to list |
| `--limit` | no | `50` | Max entities to show |

### cruxible list edges

```bash
cruxible list edges [--relationship <type>] [--limit <n>]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--relationship` | no | — | Filter by relationship type |
| `--limit` | no | `50` | Max edges to show |

### cruxible list receipts

```bash
cruxible list receipts [--query-name <name>] [--limit <n>]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--query-name` | no | — | Filter by query name |
| `--limit` | no | `50` | Max receipts to show |

### cruxible list feedback

```bash
cruxible list feedback [--receipt <id>] [--limit <n>]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--receipt` | no | — | Filter by receipt ID |
| `--limit` | no | `50` | Max records to show |

### cruxible list outcomes

```bash
cruxible list outcomes [--receipt <id>] [--limit <n>]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--receipt` | no | — | Filter by receipt ID |
| `--limit` | no | `50` | Max records to show |

---

## cruxible export

Export graph data to files. Unlike `list` (which renders Rich tables to stdout with a default limit of 50), `export` writes all matching data to a file.

### cruxible export edges

Export all edges to CSV.

```bash
cruxible export edges -o <path> [--relationship <type>] [--exclude-rejected]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--output` / `-o` | **yes** | Output file path |
| `--relationship` | no | Filter by relationship type |
| `--exclude-rejected` | no | Omit edges with `human_rejected` or `agent_rejected` review status |

**Columns:** `from_type`, `from_id`, `to_type`, `to_id`, `relationship_type`, `edge_key`, `properties_json`

The `properties_json` column is the full edge properties dict as JSON with deterministic key ordering (`sort_keys=True`). This includes config-defined properties, `review_status`, and `_provenance`. See [Edge Properties](concepts.md#edge-properties) for what these contain.

By default all edges are exported regardless of `review_status`. Use `--exclude-rejected` to omit rejected edges.

**Example:**

```bash
cruxible export edges -o edges.csv
# Exported 704 edge(s) to edges.csv

cruxible export edges -o metabolized.csv --relationship metabolized_by
# Exported 58 edge(s) to metabolized.csv

# Verify round-trip:
python -c "import csv, json; [json.loads(r['properties_json']) for r in csv.DictReader(open('edges.csv'))]; print('OK')"
```

---

## cruxible find-candidates

Find candidate relationships using deterministic strategies.

```bash
cruxible find-candidates --relationship <type> --strategy <strategy> \
  [--rule FROM_PROP=TO_PROP ...] [--via <relationship>] [--limit <n>]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--relationship` | **yes** | Relationship type to find candidates for |
| `--strategy` | **yes** | `property_match` or `shared_neighbors` |
| `--rule` | conditional | Match rule as `FROM_PROP=TO_PROP` (repeatable, for `property_match`) |
| `--via` | conditional | Via relationship (for `shared_neighbors`) |
| `--limit` | no | Max candidates (default: `20`) |

**Example:**

```bash
cruxible find-candidates --relationship interacts_with \
  --strategy shared_neighbors \
  --via metabolized_by
# [table of candidate drug pairs sharing enzymes]
# 15 candidate(s) found.
```

---

## cruxible schema

Display the config schema for the current instance.

```bash
cruxible schema
```

No options. Reads the `.cruxible/` instance in the current directory.

---

## cruxible sample

Show a sample of entities of a given type for quick inspection.

```bash
cruxible sample --type <entity_type> [--limit <n>]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--type` | **yes** | — | Entity type to sample |
| `--limit` | no | `5` | Number of entities to show |

**Example:**

```bash
cruxible sample --type Drug --limit 3
# [table showing 3 sample drugs]
```

---

## cruxible evaluate

Assess graph quality: orphan entities, coverage gaps, constraint violations,
candidate opportunities, governed support state, and unreviewed co-members.

```bash
cruxible evaluate [--limit <n>]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--limit` | no | `100` | Max findings to show |

**Example:**

```bash
cruxible evaluate
# Graph: 52 entities, 704 edges
# Findings: 3
#   orphan: 1
#   constraint_violation: 2
#   [ERROR] interacts_with edge warfarin → warfarin violates no_self_interaction
```

---

## Error Handling

All commands catch `CoreError` exceptions and print a user-friendly error message to stderr with a non-zero exit code. Use `--help` on any command for usage details.
