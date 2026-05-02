# Common Provider Catalog

This is a placeholder for the focused common-provider documentation pass.

Common providers live in `cruxible_core.providers.common` and are intended to be
reused by kits before domain-specific normalization providers run. They are
normal Python providers: configs still declare the input/output contracts,
provider entries, artifacts, and workflow steps explicitly.

Use built-in workflow step types for engine-owned deterministic row mechanics
such as shaping, joining, filtering, deduping, building graph objects, and
applying canonical state. Use common providers for reusable but opaque
adapters, external integrations, model calls, source parsing, or domain-policy
modules.

For `0.2`, do not add schema magic for these providers. Document the contract
snippets agents should copy into configs.

## What This Doc Should Eventually Include

Each common provider should have:

- Provider ref, for example `cruxible_core.providers.common.tabular.load_tabular_artifact_bundle`
- Intended workflow placement
- Suggested `contract_in` and `contract_out` YAML snippets
- Input options and defaults
- Output shape
- Artifact requirements
- Example workflow step
- Notes on when to use a domain-specific provider instead

## Initial Providers To Document

- `load_tabular_artifact_bundle`: parse CSV, JSON, JSONL, NDJSON, and Excel files from a pinned artifact into provenance-rich generic tables.
- `source_diff`: compare previous and current parsed table bundles by configured keys.
- `document_to_markdown`: normalize text, Markdown, and simple HTML artifacts into Markdown.
- `pdf_to_markdown`: convert a PDF artifact to Markdown using `docling` for local layout-aware parsing, `pypdf` for lightweight local text extraction, or `firecrawl` for hosted parsing.
- `extract_document_tables`: extract Markdown pipe tables into structured rows.
- `resolve_entities_by_alias`: match generic source records to existing entities using alias fields.
- `normalize_identifiers`: normalize common identifiers such as CVEs, GTIN/UPC/EAN, SKUs, slugs, dates, and CPE strings.

## Example Contract Snippet

```yaml
contracts:
  TabularParseOptions:
    fields:
      expected_tables: {type: json, optional: true}
      table_names: {type: json, optional: true}
      extensions: {type: json, optional: true}
      normalize_headers: {type: bool, optional: true}

  ParsedTabularBundle:
    fields:
      artifact: {type: json}
      tables: {type: json}
      files: {type: json}
      diagnostics: {type: json}
```

## Example Provider Snippet

```yaml
providers:
  parse_seed_bundle:
    kind: function
    description: Parse a pinned source artifact into generic tables.
    contract_in: TabularParseOptions
    contract_out: ParsedTabularBundle
    ref: cruxible_core.providers.common.tabular.load_tabular_artifact_bundle
    version: "1.0.0"
    deterministic: true
    runtime: python
    artifact: seed_bundle
```

Typical workflow shape:

```text
pinned artifact
  -> common provider parses generic source shape
  -> dataflow steps shape/filter/join/dedupe rows where possible
  -> domain provider handles source-specific policy only when needed
  -> workflow creates entities, relationships, candidates, or signals
```
