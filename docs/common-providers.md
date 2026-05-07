# Common Providers And Dataflow Steps

Cruxible has two reusable mechanisms that can look similar in configs but serve
different purposes.

## Built-In Step Types

Step types are engine-owned deterministic workflow mechanics. They are visible
to the compiler and executor, have stable semantics, and do not hide graph
writes or external side effects inside Python code.

Use built-in step types for generic row and state mechanics:

- `shape_items`: project rows, rename keys, require fields, and cast values
- `join_items`: indexed inner joins over two item sets
- `filter_items`: exact/list filters and comparison predicates
- `dedupe_items`: deterministic row deduplication
- graph construction steps that make entities, relationships, and proposal
  members
- canonical apply steps that preview and apply accepted state

These are the preferred building blocks for deterministic state loading in new
kits.

## Common Providers

Common providers are reusable Python providers under `cruxible_core.providers`
that remain opaque to the workflow engine. Configs still declare contracts,
provider entries, artifacts, and workflow steps explicitly.

Use common providers for reusable adapters and external source mechanics:

- parsing a pinned artifact into generic source rows
- converting documents into Markdown
- extracting tables from documents
- normalizing identifiers
- calling an external parser or model behind an explicit provider contract

Common plumbing contracts are built in. Use `cruxible.JsonObject` for flexible
provider options, `cruxible.ParsedTabularBundle` for tabular parser output, and
`cruxible.EmptyInput` when a workflow or provider takes no input.

Providers should return data to the workflow. They should not directly mutate
Cruxible graph state, SQLite state, snapshots, decision logs, or group stores.
State changes should go through workflow steps, proposal groups, feedback tools,
or canonical apply surfaces.

## Domain Providers

Use kit-local providers for logic that is genuinely domain-specific or
customer-specific:

- source-specific normalization
- match scoring that depends on local inventory conventions
- policy interpretation
- classification using a kit-owned enum or taxonomy
- external system adapters whose input shape is not knowable in core

If provider logic becomes generic across multiple kits, consider moving it into
a common provider or a built-in step type. Promote conservatively: step types
should be deterministic, graph-side-effect free, and useful beyond one kit.

## Typical Workflow Shape

```text
pinned artifact
  -> common provider parses generic source shape
  -> built-in steps shape/filter/join/dedupe rows
  -> kit provider handles source-specific policy only when needed
  -> workflow creates entities, relationships, proposal members, or signals
  -> canonical apply or governed group resolution changes accepted state
```

## Initial Common Providers

- `load_tabular_artifact_bundle`: parse CSV, JSON, JSONL, NDJSON, and Excel
  files from a pinned artifact into provenance-rich generic tables.
- `source_diff`: compare previous and current parsed table bundles by
  configured keys.
- `document_to_markdown`: normalize text, Markdown, and simple HTML artifacts
  into Markdown.
- `pdf_to_markdown`: convert a PDF artifact to Markdown using a configured
  local or hosted parser.
- `extract_document_tables`: extract Markdown pipe tables into structured rows.
- `resolve_entities_by_alias`: match generic source records to existing
  entities using alias fields.
- `normalize_identifiers`: normalize common identifiers such as CVEs,
  GTIN/UPC/EAN, SKUs, slugs, dates, and CPE strings.

## Example Provider Snippet

```yaml
providers:
  parse_seed_bundle:
    kind: function
    description: Parse a pinned source artifact into generic tables.
    contract_in: cruxible.JsonObject
    contract_out: cruxible.ParsedTabularBundle
    ref: cruxible_core.providers.common.tabular.load_tabular_artifact_bundle
    version: "1.0.0"
    deterministic: true
    runtime: python
    artifact: seed_bundle
```

Follow this with dataflow steps such as `shape_items`, `join_items`,
`filter_items`, and `dedupe_items` before creating graph objects.
