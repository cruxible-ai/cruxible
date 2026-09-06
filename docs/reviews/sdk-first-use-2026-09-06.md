# Code Review

## Verdict

Approved

Approved at `26db6ff2cbe96210f703c7920cdd71b16bfa4fe6`, including qualified Claim normalization in `001b58ae9c645c5d6b6ce65d9d31995186ccfd23`, against `adef574dae621130dde54459b396d99861691e9d`. The SDK binds approval to an exact detached review and explicit caller-held signer while preserving frozen signature law, historical key authority, and separate acceptance. Review-time completeness and token-consistency concerns were fixed with regressions; no remaining blocker was found.

## Manual Review Priority

- Priority: P1
- Reason: Public signing ergonomics, exact review identity, private-key custody relocation, and a narrowly amended dependency boundary deserve focused review.
- Suggested Human Review Focus: Immutable review and challenge binding; signer-returned attestation and receipt checks; historical signer authority versus authenticated submitter; caller-only compatibility bridge; normalized typed Claim references and duplicate refusal.

## Scope Reviewed

- Changed files: `packages/cruxible-client/src/cruxible_client/{__init__.py,authoring/__init__.py,authoring/sdk.py}`, `src/cruxible_core/playbill/signing.py`, `tests/test_architecture/test_client_package_boundaries.py`, `packages/cruxible-client/README.md`, and `docs/reviews/sdk-first-use-design-2026-09-06.md`.
- Untracked files: At initial review, new `packages/cruxible-client/src/cruxible_client/authoring/{approval.py,signing.py}`, `packages/cruxible-client/examples/claim_review_repair.py`, `tests/test_client/test_playbill_sdk_approval.py`, `tests/test_client/test_playbill_sdk_claim_ids.py`, and `tests/test_server/test_playbill_sdk_approval_workflow.py`; all are included in the final reviewed commits. The checkout was clean at final source verification before creating this report.
- Tests examined: New approval, Claim-ID normalization, and HTTP example tests; package-boundary tests; relevant existing `tests/test_service/test_playbill_review.py`, `tests/test_playbill/test_approval_attestations.py`, and `tests/test_client/test_playbill_signing.py`. Existing server review/challenge/submission code and shared candidate, principal, and attestation contracts were inspected to establish authority and wire parity.
- Commands run: Targeted `git status`, `git diff`, `git log`, `git show --stat`, `git rev-parse`, `rg`, `sed`, `cat`, and file SHA-256 inspection. Independently ran `PYTHONPATH=src:packages/cruxible-client/src /Users/robertmalone/Git/cruxible-core-0.2-stretch-goals/.venv/bin/python -m pytest tests/test_architecture/test_client_package_boundaries.py -q` in the isolated worktree: **10 passed in 3.16 s**.

Implementer-reported verification covers **102 distinct tests** across the existing SDK (33), new approval guards (19), Claim normalization (3), package boundaries (10), existing review/signing/bootstrap (32), Claim attestations (4), and real HTTP example (1). The final changed approval/normalization/HTTP scope was rerun: **23 passed in 6.72 s**. Scoped mypy for four sources, Ruff check/format for eleven files, and diff checks passed. These reports are not presented as reviewer-run tests; the independent boundary run overlaps the ten already counted.

Named additional test files: `tests/test_client/test_playbill_sdk.py`, `tests/test_playbill/test_signing_bootstrap.py`, and `tests/test_playbill/test_claim_attestations.py`. The first example run exposed the qualified-ID repair failure now fixed by the separately reviewed normalization commit; its final regression passed.

Root separately ran the final installed client wheel against a fresh disposable Unix HTTP daemon. The inspected evidence at `docs/reviews/sdk-first-use-wheel-2026-09-06.json` names exact source head `26db6ff2cbe96210f703c7920cdd71b16bfa4fe6`, records matching installed/source hashes for five client modules, Core/tests unavailable to the client interpreter, two exact V3 reviews, and a supported same-Claim revision 2 with value 49. The full tiny example completed in 5.666 seconds excluding setup/connection; this is a single functionality proof, not a scale or percentile benchmark. This review approves code independently of timing measurements and does not claim independent execution of the wheel workflow. No full suite, golden corpus, canonical-checkout tests, benchmark, live state mutation, or source edits were performed by this reviewer.

## Findings

No findings.

## Complexity Assessment

Review and approval perform work proportional to the complete candidate/review payload: typed candidate validation, canonical serialization, detached parsing, and ordered member comparisons. The immutable token retains one canonical byte snapshot plus scalar identifiers; each `details` access returns new containers. This avoids persistent mutable model retention but is not a compact review format or an optimization of large-candidate verification. Cached scalar identifiers avoid unnecessary full snapshot parsing for simple property reads, and approval explicitly checks the cached digest against the snapshot.

Approval requires a fresh challenge and a submission request; Ed25519 verification is constant-size work. Local signing reloads and validates the key on each signature, preserving custody checks instead of caching private bytes. Qualified-ID normalization is linear in disposition count before the existing sort; a direct original-reference map replaces the old repeated lookup. No process-global cache or accepted-state index is introduced.

## Architecture Assessment

The implementation stays on the client side and keeps server verification authoritative. Read the changed logic in this order:

1. `authoring/signing.py` relocates the existing signer protocol, Ed25519 signer, and key loader into the standalone client package. Private material is not added to any wire model. Existing forbidden-root resolution, nonsymlink/file permissions, no-follow descriptor reads, inode checks, Ed25519-only parsing, temporary buffer clearing, and per-signature public-key comparison remain intact.
2. `ReviewedProposal` stores canonical review bytes, originating SDK object and instance, and validated scalar identity. `details` reparses a detached response. This is a process-local convenience token, not a portable proof that someone actually inspected the review.
3. `_checked_review` validates the versioned candidate and digest, parent/base consistency, complete candidate member roll, ordered rendered member coverage and identifying metadata, Document coverage, and unredacted visibility. It permits legitimate non-text Documents without requiring a readable diff.
4. `review_proposal` captures the full review. `approve_reviewed` refuses a foreign session/instance/proposal or contradictory token identity, fetches a fresh challenge, and compares stable reviewed content. Approval coverage and projection advice can change without invalidating an unchanged candidate.
5. The challenge must name the configured signer/public key and active historical principal, exact parent root, candidate digest, and key-history reference. Daemon signing and ordinary-artifact recovery signing refuse. The authenticated submitter remains distinct from the cryptographic signer, matching existing law.
6. Canonical expected statement bytes are fixed before caller-owned signer code runs. The returned attestation is reconstructed through validation, compared against that preimage, and signature-verified before submission. The returned receipt must match proposal, candidate, signer, root, attestation digest, and key history. A post-submission identity mismatch explicitly reports that submission occurred.
7. `Proposal.review`, `Proposal.approve`, `Playbill.proposal`, and lazy exports expose the path without implicit approval decisions, replacement-candidate review, key discovery, acceptance, or refresh. The existing raw HTTP APIs remain available.
8. Revision and disposition inputs normalize qualified Claim IDs using the existing helper. Typed reference expectations preserve their coordinate while using the canonical bare address; program stamps use the same normalization. Duplicate normalized keys refuse instead of silently overwriting an entry.
9. The public example explicitly separates review, signing, acceptance, current World readback, source drift observation, and same-identity repair. It provisions neither server nor authority.

The dependency exception is appropriate and narrow. `playbill/signing.py` was already a caller-held signer seam whose sole production importer is CLI; the exact-file bridge preserves old imports. Daemon code remains restricted to client contracts, and the new guard separately prohibits either custody module through direct, package-style, and relative imports. No broad Playbill exception or file I/O in frozen contracts was added.

The frozen approval statement still signs exactly tag, signer ID, parent semantic root, and candidate digest. It does not cryptographically contain an instance/proposal ID; the SDK origin checks prevent accidental session mixing without changing that historical law. The daemon still owns access checks, eligibility, persistence, and activation CAS. A successful approval remains distinct from acceptance.

## Test Coverage Assessment

The new guard tests cover detached nested review mutation, foreign session/instance/proposal tokens, changed challenge identity/root/candidate/key/history/member roll/redaction, signer mutation/substitution and invalid signatures, receipt mismatch after submission, missing rendered member/Document coverage, and contradictory cached token identity. They also preserve legitimate submitter/signer separation and newly added approval coverage.

Normalization tests check bare and qualified typed/untyped parity for payloads, retained coordinate assertions, and program stamps, plus alias collision refusal. The real HTTP example exercises current V3 candidates through two independent-review approvals and separate acceptances, verifies supported readback, detects cited-source drift, and repairs the same Claim. This closes the compatibility gap that synthetic legacy-candidate tests alone would leave.

The independent package-boundary run verifies both standalone import direction and the narrow custody bridge, with synthetic absolute, package-style, same-package, package-initializer, and parent-relative import cases. Existing signing tests remain relevant because the loader was relocated rather than rewritten. No flaky time-dependent assertion was introduced in the reviewed tests.

## Documentation Assessment

README and implementation notes explain operator-configured signer capabilities, detached review details, complete-visibility requirements for this convenience path, partial-visibility access through existing raw APIs, historical key authority, distinct submitter identity, and separate acceptance. They accurately state that the token cannot prove actual review. The executable example requires a disposable initialized instance and explicit custody configuration; its decision callbacks are usable by an agent or interactive caller.

The implementation notes distinguish installed-wheel HTTP evidence from fresh dependency installation, managed authentication, and production-scale latency. Final exact-head wheel evidence is recorded separately and was inspected as root-provided verification; no additional code change is required for this review.

## Overall Contribution

The scope is cohesive: it removes repetitive approval ceremony while making exact review and signing expectations explicit, preserves local custody in the independently installable client, and fixes the concrete read-to-revision composition failure encountered by the example. It strengthens checks around an existing signature protocol without creating a competing authority plane. The remaining large-review serialization cost and explicit operator provisioning are clear limits, not hidden functionality.

## Open Questions

None.

## Suggested Follow-Ups

- If large candidate reviews become a measured bottleneck, profile repeated candidate parsing and snapshot serialization while retaining exact bytes, full visibility, and detached outputs.
- Keep future signer backends behind the existing capability protocol and preserve the same preimage, public-key, returned-attestation, and receipt checks.
