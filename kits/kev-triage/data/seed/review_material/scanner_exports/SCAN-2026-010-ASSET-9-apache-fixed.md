# Scanner Export: ASSET-9 Apache Fixed Version

Source: external vulnerability scanner export
Observed at: 2026-03-20
Asset: `ASSET-9` / `prod-web-03`

## Summary

The scanner observed Apache HTTP Server `2.4.59` on `prod-web-03`. The host is
internet-facing and part of the customer portal web tier, but this specific
Apache version is newer than the vulnerable versions used in the path traversal
and rewrite-rule review examples.

## Example Finding Payload

```json
{
  "asset_id": "ASSET-9",
  "hostname": "prod-web-03",
  "software_name": "Apache HTTP Server",
  "vendor": "Apache",
  "version": "2.4.59",
  "scanner_status": "not_vulnerable",
  "finding_basis": "Installed version is outside the affected range",
  "observed_at": "2026-03-20"
}
```

## Review Use

Use this as contrast evidence: internet exposure alone should not create an
actionable posture when the installed product version is already fixed.
