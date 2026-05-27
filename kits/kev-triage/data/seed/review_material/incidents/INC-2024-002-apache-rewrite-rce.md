# Post-Mortem: INC-2024-002

## Summary

On 2026-02-18, `prod-web-02` (`ASSET-6`) served attacker-controlled rewrite
content that mapped requests to filesystem locations outside intended routes.
The host was internet-facing and running Apache HTTP Server 2.4.58.

The response team attributed the exploit path to `CVE-2024-38475`. Traffic was
contained by enabling the emergency WAF policy and deploying a patched Apache
build during the next web patch window.

## Evidence refs to cite

- `source=pagerduty`
- `source_record_id=INC-2024-002`
- `asset_id=ASSET-6`
- `cve_id=CVE-2024-38475`
- `owner_id=OWNER-2`

Use this postmortem as supporting evidence for governed proposals such as
`asset_vulnerability_posture`, `asset_remediated_vulnerability`,
`asset_patch_exception_for`, and `control_mitigates_class`. Do not create a
separate graph object for the source report.

## Lessons to preserve in evidence

- `title=Unsafe rewrite rule pattern deployed without security review`
- `category=misconfiguration`
- `status=remediated`
- `remediation_action=Require security review for Apache rewrite rules that resolve to filesystem paths`

### FIND-2024-011

- `title=Emergency WAF policy was not pre-enabled on secondary production web tier`
- `category=process_gap`
- `status=open`
- `remediation_action=Pre-stage emergency WAF policy on all production web assets`
