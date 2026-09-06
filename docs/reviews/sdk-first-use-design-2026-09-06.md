# SDK first-use implementation — 2026-09-06

Implemented on `codex/sdk-first-use`, starting from design commit `50b9817d`.
Root owns consumption performance; Fable owns the Procedure rung-2 lane. This
slice changes client ergonomics and preserves the existing approval wire,
signing algorithm, custody checks, server authority, and explicit acceptance.
No live instance, deployment, policy adoption, or key discovery was involved.

## Before and after

| Operation | Before | Implemented |
|---|---|---|
| Existing proposal handle | Manually construct an SDK object or retain raw client context | `pb.proposal(proposal_id)` |
| Exact review | Raw HTTP client, separate instance argument, mutable nested response | `proposal.review()` returns a process-local `ReviewedProposal`; inspect detached `details` |
| Approval | Parse challenge statement, compare candidate/root/signer, invoke signer, submit manually | `proposal.approve(signer=configured_signer, reviewed=reviewed)` performs those guards and verifies returned signature/receipt |
| Local approval signer | Import a client-held signer from Core | Public `cruxible_client.LocalEd25519ApprovalSigner`; original Core import remains compatible |
| Acceptance and readback | Explicit acceptance, then refresh/read choice | Unchanged: `pb.accept(...)`, then `pb.world()` and bounded `prefetch` |
| World → revision authoring | Qualified `Claim:CLM-...` read identities failed bare-ID inputs | Bare/qualified `revises` and disposition keys normalize consistently, including typed assertions/program stamps; ambiguous duplicates refuse |
| Runnable customer flow | Decision templates and server integration tests | [Public client-only example](../../packages/cruxible-client/examples/claim_review_repair.py) and a real HTTP regression |

```python
intent = draft.prepare()
if intent.refused:
    raise RuntimeError(intent.diagnostics)
status = intent.submit().status()
assert status.proposal_id is not None
proposal = pb.proposal(status.proposal_id)
reviewed = proposal.review()
# Inspect reviewed.details and make an explicit approval decision.
approval = proposal.approve(signer=configured_signer, reviewed=reviewed)
# Make the separate acceptance decision.
receipt = pb.accept(proposal.proposal_id)
world = pb.world()
rows = world.prefetch(subjects=(subject_address,), predicates=(predicate,))
```

The previous raw `CruxibleClient.review_playbill_proposal`,
`prepare_playbill_approval`, `submit_playbill_approval`, and callback-based
`approve_playbill_proposal` remain compatible. No second set of advanced raw
wrappers was added: the guarded common path removes that ceremony, while existing
primitives already support external signing and partial-visibility applications.

## Identity, authority and ownership

A review token stores immutable canonical review bytes plus validated scalar
identity. It belongs to its originating SDK session and instance, and is not a
portable cross-process approval certificate. Each `details` access returns a
detached typed response. The token prevents accidental identity mismatch; it
cannot establish that an agent or human actually examined the candidate.

The helper validates the typed candidate digest, parent and settlement coordinate,
complete member roll, rendered member coverage and Document coverage. It requires
an unredacted review specifically for this convenience path. That requirement is
not a new admission or server approval gate. Binary Documents may legitimately
lack a readable text diff.

Before invoking the signer, the fresh challenge must match proposal, candidate,
root, stable reviewed content and governance, configured signer ID/public key,
and historical key reference. Another approver changing coverage does not require
a new candidate review. The accepted principal at the candidate's historical root
is the relevant key; a newer unrelated head does not silently change it. The
server retains final signature, eligibility, submission-authority and CAS checks.
The authenticated submitter need not be the signer.

Canonical statement bytes are fixed before caller-owned signer code runs. The
returned attestation is revalidated, compared with those bytes, and independently
signature-verified before transport. Receipt identity is checked afterward. Typed
`ApprovalReviewMismatch` refusals explain repair; a receipt mismatch explicitly
says submission already occurred and directs status inspection before retrying.
There is no automatic replacement review, retry, key selection, or acceptance.

The existing Ed25519/OpenSSH signer and its exact loader/root checks moved into
[client signing](../../packages/cruxible-client/src/cruxible_client/authoring/signing.py).
The [Core compatibility import](../../src/cruxible_core/playbill/signing.py) remains
caller-only; its only production importer is the CLI. Package-boundary guards
permit exactly that historical bridge and prohibit daemon imports of either
custody module, including package-style and relative imports. No file I/O moved
into frozen contracts. Operator-provisioned signer capabilities are explicit;
hardware/broker implementations, delegation and credential discovery remain later work.

## Verified customer loop and scope

The example uses public client imports only. It explicitly declares a workspace
source, authors Subject/ClaimType/Claim, reviews and approves the full candidate,
accepts, acquires current World state, checks supported evidence, invokes free
audit, changes the cited span, observes citation drift, and revises the same
Claim with new evidence. It requires separate review and accept decisions for
both proposals. Acceptance is distinct from support, and all returned contenders
are inspected; no scalar selector or private intent field is used.

The isolated real HTTP test exercises current V3 candidates with separate
operator and reviewer identities. It checks two distinct reviewed candidates,
stable Claim identity, revision 2, value 49, supported verdict, exact readback
coordinate, and resolved source drift. Reference-normalization tests check bare
and qualified typed/untyped payload parity, retained assertion coordinates and
program stamps, and explicit duplicate refusal.

Named validation covers **102 unique cases**: SDK/approval/normalization/package
boundary tests (65), existing service review/approval/signing/bootstrap scopes
(32), Claim-attestation tests (4), and the real HTTP example (1). Scoped Mypy
checks the four changed implementation modules; Ruff checks changed source,
tests and example. No full suite,
golden corpus, canonical-checkout tests or live state access was used. Earlier
first-use reconnaissance also passed the two existing shipped-example/revision
integration tests (19.66 seconds).

A fresh stdlib venv installed the actual client wheel offline using already
installed dependency distributions (no `.pth`, Core or tests). A separate local
Unix-socket daemon hosted a disposable instance. The isolated client interpreter
used `python -I`, verified that Core/tests were unavailable, and completed the
public workflow including independent approval. This is installed-package HTTP
proof, not online dependency resolution or managed authentication/remote deployment
coverage. The example script was read from the repository; its imports resolve
exclusively to the installed client wheel.

The final committed-source run at `26db6ff2cbe96210f703c7920cdd71b16bfa4fe6` took **5.666 seconds** for the complete two-write
workflow, excluding server/bootstrap/connection setup. Per-operation observations:
review 0.043–0.048 s; challenge 0.047–0.049 s; approval submission 0.46–0.50 s;
acceptance 0.993–0.996 s; Claim batch readback 0.0096–0.0099 s. These are individual
intervals within that workflow, not additive independent benchmarks or scale
claims. Signature and SDK guard overhead is included in total workflow time.
[Final wheel evidence](sdk-first-use-wheel-2026-09-06.json) records the source commit,
wheel digest and exact installed/source module parity. The preceding pre-typecheck
proof took 5.515 seconds; the final-source rerun replaces it as the implementation evidence. The isolated client's
package root is `/private/tmp/playbill-sdk-first-use-client-env`.

Remaining first-use work is practical setup documentation and broader packaged
examples. Scalar selection, signer backend implementations, portable review
bundles, actor delegation and Procedure output support require separate designs;
this slice does not claim to finish them.
