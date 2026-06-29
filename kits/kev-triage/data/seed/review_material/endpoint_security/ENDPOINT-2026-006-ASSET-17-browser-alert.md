# Endpoint Security Alert: ASSET-17 Browser Exploit Attempt

Source: endpoint security alert
Observed at: 2026-03-23
Asset: `ASSET-17` / `exec-laptop-01`

## Summary

The endpoint security tool reported a blocked browser exploit chain on an
executive laptop. The laptop also has browser isolation coverage. This is
evidence that endpoint controls matter for user-device risk, but it should not
be treated like internet-facing service exposure.

## Example Alert Payload

```json
{
  "asset_id": "ASSET-17",
  "hostname": "exec-laptop-01",
  "software_name": "Google Chrome",
  "control_id": "CTRL-6",
  "alert_status": "blocked",
  "finding_basis": "Browser isolation contained a suspicious renderer process",
  "observed_at": "2026-03-23"
}
```

## Review Use

Use this as an endpoint-only contrast case: the evidence may support a control
or software review, but it should not imply that a business service is exposed.
