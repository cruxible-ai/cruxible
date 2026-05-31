# KEV Triage Source-Native Fixtures

This directory is a source-native mirror of the active graph-shaped seed bundle
under `data/seed/`. The active workflows still read `data/seed/`; these files
show the same company scenario as source-system exports.

The goal here is representativeness: these files are shaped like the exports a
customer would actually have on hand, grouped by source system rather than by
graph relationship type.

No config or provider wiring points at this directory yet. Keep the overlapping
fields and deterministic relationships aligned with the active seed bundle so
the later migration can be mechanical.

## Intended mapping

- `cmdb_assets.csv`
  Produces `Asset` entities plus deterministic `asset_owned_by` and
  `asset_patch_window` relationships.
- `owners.csv`
  Produces `Owner` entities.
- `service_catalog.csv`
  Produces `BusinessService` entities.
- `service_dependencies.csv`
  Produces deterministic `service_depends_on_asset` relationships.
- `software_inventory.csv`
  Feeds governed `asset_runs_product` proposals through fuzzy matching.
- `control_inventory.csv`
  Produces `CompensatingControl` entities.
- `control_coverage.csv`
  Produces deterministic `asset_has_control` relationships.
- `patch_windows.csv`
  Produces `PatchWindow` entities.
- `grc_exceptions.csv`
  Produces `Exception` entities plus deterministic `asset_has_exception`
  relationships from a source-of-record system.

Human-readable reviewer context for governed actions (incidents, waiver
requests, control reviews) lives at `data/seed/review_material/` rather than
being duplicated here.

## Notes

- `data/seed/` remains the active demo fixture bundle until the loader and
  workflow config are updated.
- Keep these rows aligned with `data/seed/`; they intentionally mirror the same
  underlying scenario so the later migration can be mechanical.
