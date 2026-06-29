# Waiver Request: EXC-2026-001

## Summary

The Billing team requests a temporary patch exception for `batch-worker-01`
(`ASSET-5`) because the monthly financial close window overlaps the next
available maintenance slot. The request covers Apache HTTP Server exposure
linked to `CVE-2024-38475` until the next approved patch window.

## Entity to create

- `exception_id=EXC-2026-001`
- `reason=Billing month-end freeze delays Apache remediation on batch-worker-01`
- `review_due_at=2026-05-03`
- `status=approved`

## Candidate graph facts

- Proposed `asset_patch_exception_for`:
  `ASSET-5 -> CVE-2024-38475`
  `exception_id=EXC-2026-001`
  `review_due_at=2026-05-03`

## Notes

The deterministic seed already includes a legacy exception on `ASSET-7` to show
that some exception records may come from a source-of-record system. This file
represents a new waiver request that the agent should add during the governed
flow.
