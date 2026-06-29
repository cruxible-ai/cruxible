# Endpoint Security Alert: ASSET-25 Admin Tooling

Source: endpoint security alert
Observed at: 2026-03-24
Asset: `ASSET-25` / `security-admin-laptop-01`

## Summary

The endpoint tool reported suspicious PowerShell child-process behavior after a
remote administration session. The device is managed by Security Engineering
and has endpoint detection plus the admin VPN ACL, so it is a high-context
endpoint case rather than a service-exposure case.

## Example Alert Payload

```json
{
  "asset_id": "ASSET-25",
  "hostname": "security-admin-laptop-01",
  "software_name": "PowerShell",
  "control_ids": ["CTRL-2", "CTRL-5"],
  "alert_status": "contained",
  "finding_basis": "Suspicious child process contained during an administrative session",
  "observed_at": "2026-03-24"
}
```

## Review Use

Use this to distinguish endpoint containment evidence from proof that a
business service is exposed. It may support a local privilege or endpoint
software review, but it should not create an internet-facing service posture.
