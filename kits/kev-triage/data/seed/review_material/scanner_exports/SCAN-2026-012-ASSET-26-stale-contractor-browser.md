# Scanner Export: ASSET-26 Stale Contractor Browser

Source: contractor endpoint inventory export
Observed at: 2026-02-12
Asset: `ASSET-26` / `contractor-laptop-01`

## Summary

The contractor device reported an old Chrome version from a stale inventory
sync. The device is not linked to endpoint detection coverage in the local seed
data. This is intentionally messy: it should prompt review of evidence age,
control coverage, and ownership before treating the row as current truth.

## Example Finding Payload

```json
{
  "asset_id": "ASSET-26",
  "hostname": "contractor-laptop-01",
  "software_name": "Google Chrome",
  "vendor": "Google",
  "version": "118.0.5993.70",
  "scanner_status": "stale_needs_review",
  "finding_basis": "Inventory is more than 30 days old and no endpoint control mapping is present",
  "observed_at": "2026-02-12"
}
```

## Review Use

Use this to test whether an agent can avoid over-trusting stale endpoint
inventory while still surfacing a potentially important unmanaged-device risk.
