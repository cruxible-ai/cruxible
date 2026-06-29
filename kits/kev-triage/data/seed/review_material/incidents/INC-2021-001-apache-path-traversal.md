# Security Report: INC-2021-001

## Summary

On 2025-10-04, the SOC observed successful path traversal requests against the
internet-facing host `prod-web-01` (`ASSET-1`) serving the Billing customer
entry point. The host was running Apache HTTP Server 2.4.49 and exposed an
alias-backed file path that should not have been directly reachable.

Investigation concluded the attacker exploited `CVE-2021-41773`. The impacted
service owner accepted a temporary outage window while the host was rebuilt and
the vulnerable version removed.

## Evidence refs to cite

- `source=siem`
- `source_record_id=INC-2021-001`
- `asset_id=ASSET-1`
- `cve_id=CVE-2021-41773`
- `owner_id=OWNER-2`

Use this report as supporting evidence for governed proposals such as
`asset_vulnerability_posture`, `asset_remediated_vulnerability`, and
`vulnerability_classified_as`. Do not create a separate graph object for the
source report.

## Lessons to preserve in evidence

- `title=Apache 2.4.49 remained internet-exposed on prod-web-01`
- `category=stale_data`
- `status=remediated`
- `remediation_action=Rebuild prod-web-01 on Apache 2.4.54 and validate package pinning`

### FIND-2021-002

- `title=New virtual host bypassed standard WAF path traversal rules`
- `category=missing_control`
- `status=open`
- `remediation_action=Extend Edge WAF policy set to all internet-facing Apache virtual hosts`
