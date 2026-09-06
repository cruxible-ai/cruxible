# Code Review

## Verdict

Approved. Reviewed commit `29989c0cecd6653c0e593430c121eb814bfd4e04` preserves
version-specific assertions in all six authoring response wrappers. It changes
the advertised response schema without changing stored intent or receipt rules.

## Manual Review Priority

- Priority: P1
- Reason: Public nested wire contracts must preserve reference assertions.
- Suggested Human Review Focus: Discriminator selection, V1 compatibility,
  required V2 assertions, wire catalog succession.

## Scope Reviewed

- Changed files: `contracts/authoring/models.py`, `contracts/authoring/wire_catalog.py`,
  and the three client/server test files in the exact commit.
- Untracked files: None in the implementation scope.
- Tests examined: Response-wrapper V1/V2 JSON round trips, public HTTP create/get/
  resume/list/submit, stale reference tests, authoring and attestation catalogs.
- Commands run: Parent inspected the exact commit and surrounding version
  definitions. Implementer ran 26 named tests, scoped Ruff/format and model Mypy;
  all passed. Parent did not duplicate those checks.

## Findings

No findings.

## Complexity Assessment

Tagged union selection is bounded and avoids guessing a response version. V2
responses include their actual assertions; that additional size is required data.
No new cache or whole-world scan is introduced.

## Architecture Assessment

The private discriminated alias makes nested serialization and parsing agree.
It covers view, list, submit and insertion wrappers consistently. The change
does not use serialize-as-any to bypass version validation. The independent
authoring catalog advances under `2026-09-06:write-loop-latency`; historical
intent/receipt digest rules and the SDK handshake are unchanged.

## Test Coverage Assessment

All six wrappers test both versions, exact nested fields, restored types, and
refusal of V2 documents missing required assertions. Public HTTP checks exercise
actual response serialization. Existing stale-reference checks preserve the
underlying preflight behavior. The HTTP submit fixture intentionally refuses
an unseeded Claim; its response still must preserve the intent.

## Documentation Assessment

The short alias comment explains the serialization requirement. Test names and
the catalog update make the scope clear; no additional user workflow is required.

## Overall Contribution

Closes the confirmed round-trip defect and enables complete diagnostic payloads
without weakening reference validation.

## Open Questions

None.

## Suggested Follow-Ups

Use the complete V2 response in subsequent end-to-end benchmark fixtures.
