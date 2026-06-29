# KEV Triage Review Material

These documents describe a fictional mid-sized SaaS company preparing a KEV
remediation review. The company runs a customer portal, billing platform,
partner API, internal operations tools, and a small set of legacy services.

The seed data should feel like a realistic company snapshot rather than a toy
graph. It should include enough assets, owners, services, controls, patch
windows, software inventory, exceptions, and review artifacts to exercise the
full triage loop while staying small enough for a person to inspect.

## Company Scenario

- The fixture should be server-heavy, with laptops included as contrast cases.
  Servers represent service exposure, patch windows, controls, and owner queues.
  Laptops represent endpoint-security findings, browser or tooling
  vulnerabilities, and cases where vulnerable software does not imply service
  exposure.
- The internet-facing web tier hosts the customer portal and includes Apache
  HTTP Server assets with different patch states.
- The partner API depends on WebLogic and has stricter network controls, but it
  has a history of emergency access changes during partner onboarding.
- Billing and batch-processing systems are business-critical, but some patching
  is constrained by freeze windows, testing requirements, and rollback plans.
- Corporate laptops and internal servers provide lower-priority contrast cases
  for scanner findings and endpoint-security alerts.
- Controls are intentionally mixed: some block relevant web attacks, some only
  detect post-exploitation activity, and some are inactive or out of scope.
- Exceptions are intentionally scoped: an asset can have a broad exception
  record without every vulnerability on that asset being waived.
- Remediation evidence should show the difference between an accepted current
  posture and a historical incident report or closure note.
- The data should be scenario-dense: every asset should exercise a review path
  such as exposed, mitigated, fixed, ambiguous, exception-scoped, stale, or
  low-impact endpoint-only.

## Evidence Artifacts

Review artifacts are stored next to the deterministic CSV seed bundle, but they
are not loaded automatically by the local-state build. They are examples of the
material a reviewer or agent can inspect before proposing durable conclusions.

- Incident reports explain past exploitation or suspected compromise.
- Control reviews explain why a control does or does not mitigate a class of
  vulnerability.
- Exception requests explain why a patch delay is scoped and temporary.
- Remediation notes explain why an accepted exposure should be closed.
- Scanner or endpoint-security exports should be treated as evidence inputs,
  not as permanent graph entities.

## Current Artifacts

- `incidents/INC-2021-001-apache-path-traversal.md`
  Historical exploitation of `CVE-2021-41773` on `ASSET-1` / `prod-web-01`.
- `incidents/INC-2024-002-apache-rewrite-rce.md`
  Later exploitation of `CVE-2024-38475` on `ASSET-6` / `prod-web-02`.
- `incidents/INC-2025-003-weblogic-admin-console-rce.md`
  WebLogic admin console compromise on `ASSET-8` / `partner-api-01`.
- `exceptions/EXC-2026-001-billing-freeze.md`
  Patch waiver request for `ASSET-5` tied to a production freeze.
- `controls/CTRL-1-edge-waf-review.md`
  Evidence that `CTRL-1` can block Apache path traversal exploitation.
- `controls/CTRL-3-partner-allowlist-review.md`
  Evidence that `CTRL-3` limits WebLogic admin-console exposure.
- `controls/CTRL-4-legacy-waf-expired.md`
  Evidence that a stale WAF should not count as active mitigation.
- `scanner_exports/SCAN-2026-010-ASSET-9-apache-fixed.md`
  Scanner evidence for an internet-facing web asset that is already on a fixed
  Apache version.
- `scanner_exports/SCAN-2026-011-ASSET-18-java-ambiguous.md`
  Scanner evidence for an ambiguous Java endpoint software match.
- `endpoint_security/ENDPOINT-2026-006-ASSET-17-browser-alert.md`
  Endpoint alert evidence for a laptop/browser case that should not be treated
  like service exposure.
- `endpoint_security/ENDPOINT-2026-007-ASSET-25-admin-tooling-alert.md`
  Endpoint containment evidence for a managed security admin laptop.
- `endpoint_security/ENDPOINT-2026-008-ASSET-28-document-handler-alert.md`
  Endpoint control evidence for a high-context finance administrator laptop.
- `scanner_exports/SCAN-2026-012-ASSET-26-stale-contractor-browser.md`
  Stale contractor endpoint inventory that should require evidence-age review.
- `remediations/ASSET-8-CVE-2020-14882-closure.md`
  Closure evidence for the WebLogic partner API exposure after response and
  rebuild work.
- `exceptions/EXC-CHROME-OLD-004-expired.md`
  Evidence that an expired endpoint exception is audit context only.
