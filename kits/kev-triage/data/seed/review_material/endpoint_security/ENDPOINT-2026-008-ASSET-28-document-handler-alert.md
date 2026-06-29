# Endpoint Security Alert: ASSET-28 Document Handler

Source: endpoint security alert
Observed at: 2026-03-24
Asset: `ASSET-28` / `finance-admin-laptop-01`

## Summary

The endpoint tool reported a blocked document-handler exploit attempt on a
finance administrator laptop. The device has managed endpoint detection and
browser isolation. The useful review question is whether the software and
controls reduce endpoint risk, not whether a production service is exposed.

## Example Alert Payload

```json
{
  "asset_id": "ASSET-28",
  "hostname": "finance-admin-laptop-01",
  "software_name": "Adobe Acrobat Reader",
  "control_ids": ["CTRL-2", "CTRL-6"],
  "alert_status": "blocked",
  "finding_basis": "Document handler exploit attempt was blocked before payload execution",
  "observed_at": "2026-03-24"
}
```

## Review Use

Use this as a high-priority endpoint contrast case. It should preserve evidence
and owner context without treating the laptop like an internet-facing server.
