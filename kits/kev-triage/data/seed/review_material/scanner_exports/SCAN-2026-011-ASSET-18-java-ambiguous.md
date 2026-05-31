# Scanner Export: ASSET-18 Java Inventory Ambiguity

Source: endpoint software inventory export
Observed at: 2026-03-22
Asset: `ASSET-18` / `dev-laptop-01`

## Summary

The scanner reported `Java Development Kit 8.0.202` from Oracle. This is a
useful ambiguity case because endpoint inventory names do not always line up
cleanly with reference product names such as Java SE.

## Example Finding Payload

```json
{
  "asset_id": "ASSET-18",
  "hostname": "dev-laptop-01",
  "software_name": "Java Development Kit",
  "vendor": "Oracle",
  "version": "8.0.202",
  "scanner_status": "needs_review",
  "finding_basis": "Product family appears related to Java SE but the inventory label is not exact",
  "observed_at": "2026-03-22"
}
```

## Review Use

Use this when testing whether the software matching workflow creates a
reviewable item instead of silently treating the match as certain.
