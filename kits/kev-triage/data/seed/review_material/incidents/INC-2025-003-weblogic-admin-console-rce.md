# Post-Mortem: INC-2025-003

## Summary

On 2026-01-11, `partner-api-01` (`ASSET-8`) received a sequence of crafted
requests against the WebLogic administrative path after a temporary partner
network routing change widened the allowed source range. The host was running
Oracle WebLogic Server 12.2.1.4.0.

The response team attributed the exploit attempt and follow-on code execution
to `CVE-2020-14882`. Access was contained by restoring the stricter partner
allowlist, rotating credentials, and rebuilding the middleware image during the
next approved patch window.

## Evidence refs to cite

- `source=pagerduty`
- `source_record_id=INC-2025-003`
- `asset_id=ASSET-8`
- `cve_id=CVE-2020-14882`
- `owner_id=OWNER-3`

Use this postmortem as supporting evidence for governed proposals such as
`asset_vulnerability_posture`, `asset_remediated_vulnerability`,
`asset_patch_exception_for`, and `control_mitigates_class`. Do not create a
separate graph object for the source report.

## Lessons to preserve in evidence

- `title=Temporary partner route expansion exposed WebLogic admin path to a broader source range`
- `category=exposure_gap`
- `status=remediated`
- `remediation_action=Require security approval and automatic rollback timers for partner allowlist expansions`

### FIND-2025-021

- `title=Credential rotation for middleware administrators was not automated after emergency network changes`
- `category=process_gap`
- `status=open`
- `remediation_action=Automate post-change credential rotation for externally reachable administrative services`
