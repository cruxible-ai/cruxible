# Cruxible Kits

Kits are maintained Cruxible state models intended to be used, overlaid, and
iterated with agents. Each kit includes a YAML config and a README with generated
views for the ontology, governed relationships, workflows, and named queries.

## Maintained Kits

| Kit | Domain | Status | Purpose |
|---|---|---|---|
| [kev-reference](kev-reference/) | Cybersecurity | ready | Standalone public KEV, NVD, EPSS, vendor, product, and vulnerability reference state. |
| [kev-triage](kev-triage/) | Cybersecurity | ready | Overlay kit for local assets, exposure triage, remediation, incidents, and controls. |
| [agent-operation](agent-operation/) | Agent Operations | ready | Reusable operating-state overlay for agent/human work, reviews, decisions, blockers, dependencies, composition, and lineage. |
| [supply-chain-blast-radius](supply-chain-blast-radius/) | Supply Chain | in_progress | Supplier, component, product, shipment, and incident blast-radius modeling. |
| [case-law-monitoring](case-law-monitoring/) | Legal | in_progress | Matter-centered case-law monitoring and authority impact modeling. |

*ready* kits ship working providers (KEV also ships public reference data).
*in_progress* means the ontology, governance, queries, and feedback/outcome loops
are complete and validated, but the data-ingest and assessment providers are
placeholders — implement them or wire your own data before running their
workflows.

## Working With A Kit

Use the generated README views as the review surface while drafting or fitting a
kit. Regenerate them after config changes:

```bash
uv run cruxible config views --config kits/<kit>/config.yaml --update-readme kits/<kit>/README.md
```

For layered kits such as KEV triage, include `--runtime` so generated views use
the composed runtime config.

Standalone kits can be initialized with `cruxible init --kit <kit>`. Overlay
kits are created with `cruxible state create-overlay --kit <kit>`.

When rendering a runtime wiki for a layered kit, use local scope so only the
local state plus directly used upstream context is emitted:

```bash
uv run cruxible wiki render --output wiki --scope local
```
