# KEV Triage Review Material

These documents are synthetic source material for the governed agent flow.

They are intentionally stored alongside the deterministic CSV seed bundle, but
they are not loaded by `build_local_state`. The intended use is:

1. Run the deterministic workflows to build the internal local state.
2. Read the review material below.
3. Use `add-entity` for incident, finding, exception, or control records that
   are not already present in the graph.
4. Use `group propose` for the governed relationships described in each file.

## Included scenarios

- `incidents/INC-2021-001-apache-path-traversal.md`
  Historical exploitation of `CVE-2021-41773` on `ASSET-1` / `prod-web-01`.
- `incidents/INC-2024-002-apache-rewrite-rce.md`
  Later exploitation of `CVE-2024-38475` on `ASSET-6` / `prod-web-02`.
- `incidents/INC-2025-003-weblogic-admin-console-rce.md`
  WebLogic admin console compromise on `ASSET-8` / `partner-api-01`.
- `exceptions/EXC-2026-001-billing-freeze.md`
  Patch waiver request for `ASSET-5` tied to a production freeze.
- `controls/CTRL-1-edge-waf-review.md`
  Evidence supporting a proposal that `CTRL-1` materially reduces exposure for
  Apache path traversal exploits.
- `controls/CTRL-3-partner-allowlist-review.md`
  Evidence supporting a proposal that `CTRL-3` reduces exposure to WebLogic
  admin-console attacks on the partner API edge.
- `remediations/ASSET-8-CVE-2020-14882-closure.md`
  Closure evidence for recording explicit remediation of the WebLogic partner
  API exposure after the incident response and rebuild.
