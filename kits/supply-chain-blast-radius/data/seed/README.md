# Supply Chain Seed Bundle

Pinned demo seed for the supply-chain blast-radius kit.

Bundle digest:
`sha256:2bec723777b92596c09b56dbd03c5c2585627b57d5c0597fb017d289a53e833d`

The digest is computed over all non-README files in this directory tree,
including `bom/`, by sorting their paths, hashing each file with SHA-256,
then hashing the resulting `shasum -a 256` manifest:

```bash
find . -type f ! -name README.md | sort | xargs shasum -a 256 | shasum -a 256
```

The engine digest of record is the `supply_chain_seed_bundle` artifact digest
in `cruxible.lock.yaml`; this README digest is a human audit aid.

## Source Basis

The physical BOM shape is curated from the public VORON 2.4 R2 open-hardware
printer structure. The pinned public artifacts live in `bom/`:

- `voron_2_readme.md`: official Voron-2 README, which points builders to the
  VORON2.4 configurator as the BOM source.
- `voron_docs_sourcing.md`: official sourcing documentation, including the
  statement that the configurator BOM is the absolute guide for required parts.
- `voron_docs_sourcing_faq.md`: official sourcing FAQ with public generic
  build facts for rails, Misumi extrusions, IGUS, wiring gauges, MIC6 aluminum,
  Raspberry Pi, stepper motors, hotend choices, and printed parts.
- `manifest.json`: retrieval timestamp, raw source URLs, file sizes, SHA-256
  hashes, attribution, and GPL-3.0 license note.

Component rows marked `provenance: "voron_bom"` retain only generic public
BOM/build claims that the pinned artifacts actually contain. Unpinned
manufacturer/MPN claims were removed from those rows. Component rows marked
`provenance: "synthetic"` are deliberate demo-specific identities; they do not
carry both manufacturer and MPN fields.

The operating layer is synthetic: supplier companies, facility locations,
shipments, demand, inventory positions, incidents, risks, and work routing are
fictional but placed in real geographies that match plausible sourcing lanes.

The split is intentional: real where public, synthetic where private. The
LDO/Hiwin product variants, supplier identities, incidents, and SKU names are
demo choreography, not public Voron claims.

## Provenance Trace Test

`tests/test_providers/test_supply_chain_kit.py` expands all 206 Component rows,
checks every component has `provenance`, verifies every `voron_bom` row has a
normalized name/manufacturer/MPN phrase present in the pinned `bom/` corpus,
verifies manifest file size and SHA-256 values, and rejects synthetic component
rows that still carry both manufacturer and MPN fields.

## Files

- `bundle.json`: canonical seed graph rows and compact component group
  definitions. The provider expands `components` plus `component_groups` into
  206 Component rows and deterministic sourcing/BOM/fulfillment edges.
- `bom/`: pinned public Voron Design source artifacts and manifest.
- `incidents.json`: pinned incident feed for Incident ingestion.
- `inventory_positions.json`: pinned fixture for `load_inventory_positions`
  (live acquisition is `scripts/fetch_inventory.py`, outside the workflow
  boundary)
  when no HTTP `base_url` is provided.
- `demand_context.json`: deterministic open-demand and rate inputs used by
  `assess_buffer_coverage` when the caller does not pass demand context.
- `operations_routing.json`: work-item/risk choreography fixture for demos.
  The `analyze_operations_routing` and `apply_operations_routing` workflows
  ingest these rows as base WorkItems/Risks and incident response links; the
  supplier-risk proposal workflow uses the routed risks.

## Expanded Row Counts

Entity rows:

- Supplier: 13
- Component: 206
- Assembly: 13
- Product: 3
- Shipment: 6
- Incident feed: 4
- Location fixture: 4
- InventoryPosition fixture: 12

Deterministic relationship rows from `load_seed_data`:

- supplier_supplies_component: 411
- supplier_supplies_assembly: 2
- component_part_of_assembly: 211
- assembly_part_of_assembly: 5
- assembly_part_of_product: 24
- product_in_shipment: 6

Inventory fixture relationship rows:

- component_inventory_position: 8
- assembly_inventory_position: 2
- product_inventory_position: 2
- inventory_position_location: 12

## Incident Choreography

- `INC-GD-STEPPER-2026-07` is an open critical Guangdong geography incident.
  It matches the stepper, electronics, harness, and frame/panel suppliers from
  structured incident/supplier geography fields, so supplier-scope proposals
  are review-gated. After those scope rows are accepted, LDO motor rows are
  single-source through the Guangdong motor supplier, so the direct supply/BOM
  cascade rows emit supported component proposals. After operator review
  accepts those stage-2 cascade rows, the fixture buffer path surfaces
  low-buffer product exposure for the 350 mm LDO printer configuration; the
  350 mm Hiwin configuration does not carry those LDO motor/rail BOM rows.
- `INC-TW-RAIL-2026-07` is an open medium supplier incident for Taichung rails.
  Its supplier-scope row is a direct id match, but rail components have a
  conditional standby US distributor path, so component cascade rows are
  intentionally `unsure` and remain reviewable.
- `INC-SH-TOOLHEAD-2026-07` is an open high supplier incident against direct
  Stealthburner toolhead assembly supply.
- `INC-UK-HOTEND-2026-06` is closed and remains in the feed as historical
  context; proposal workflows filter it out.

Supplier-risk attachment proposals require accepted supplier impact plus
cascade evidence. A routed risk with only supplier evidence stays `unsure`;
once direct component or assembly cascade rows are present, the maintainer
judgment emits `support`.

## Format Notes

All rows are JSON objects. Provider outputs deliberately carry every property
declared by the configured entity or relationship type at the top level because
the kit workflows use `properties: auto`. Optional values are explicit `null`.

`component_groups` compact repeated BOM families. Each group supplies a
`component_id_template`, `name_template`, category, assembly assignment, and
either `count` or explicit `items`. The provider expands these deterministically
before returning Component and component_part_of_assembly rows.
