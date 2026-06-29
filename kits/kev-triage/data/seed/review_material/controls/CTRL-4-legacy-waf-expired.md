# Control Review: CTRL-4 Legacy WAF

Control: `CTRL-4` / `Legacy WAF`
Observed at: 2026-01-31
Status: inactive

## Summary

The legacy WAF previously had path traversal rules for the old partner portal,
but the appliance is no longer in the active traffic path. Its last validation
expired before the current review period.

## Review Use

This control is intentionally present as a stale contrast case. A reviewer or
agent may see that it once blocked path traversal, but current exposure
assessment should not rely on it while the control status is inactive.
