# SDK first-use design — 2026-09-06

Design/probe branch: `codex/sdk-first-use`, based on Core `097d175594279f2735458de0a87a3fe58ed11e83`. No live instance access, deployment, signing-policy changes or SDK API changes were made in this pass. Root owns performance and Fable owns Procedure proposal/reading work. Proposed APIs below require agreement before implementation.

## First-use result and actual friction

The existing Claim → source-backed admission → accepted readback → drift → same-lineage repair journey works in the inspected public service/SDK integration tests. The authoring package ships in `cruxible-client`, but its README described only HTTP/contracts. That omission is corrected in this branch, together with snapshot, contender and evidence semantics.

A client-only user can author via `Playbill`, then must use a separately configured `CruxibleClient` for structured review and approval challenge/submission. The existing `LocalEd25519ApprovalSigner` lives in `cruxible_core.playbill.signing`; therefore its implementation cannot be imported in a client-only installation. The client already ships a Claim-attestation signer, but that signs a different statement domain and must not be used as an approval substitute.

The existing raw client also offers `approve_playbill_proposal(callback)`. It obtains a fresh challenge and invokes the callback without a separately supplied reviewed-candidate expectation. Do not teach that method as proof of approving a previously reviewed candidate. This slice need not change its compatibility behavior.

Other friction is smaller and can be removed without new authority: constructing a Proposal handle from its public ID; obtaining a structured review; clear candidate versus intent identity; knowing that accept does not refresh; and showing complete coordinate-pinned prefetch rather than private intent fields to find a newly accepted Claim. Scalar selection, actor delegation, derived backing and portable cross-instance bundles are separate designs.

## Before: public but split across layers

Existing API shape, with an operator-configured client and explicit signer custody:

```python
intent = draft.prepare()
if intent.refused:
    raise RuntimeError(intent.diagnostics)
status = intent.submit().status()
proposal_id = status.proposal_id
assert proposal_id is not None

review = client.review_playbill_proposal(instance_id, proposal_id)
# The reviewer examines review.members, evidence, redactions and governance.
challenge = client.prepare_playbill_approval(
    instance_id, proposal_id, signer_id="reviewer",
)
statement = ApprovalStatement.model_validate(challenge.statement)
assert challenge.proposal_id == proposal_id
assert challenge.review.candidate_digest == review.candidate_digest
assert statement.payload_digest == review.candidate_digest
assert statement.signing_semantic_root == challenge.review.parent_semantic_root
# signer currently comes from cruxible_core, despite being client-held.
signed = signer.sign(statement)
client.submit_playbill_approval(
    instance_id, proposal_id, attestation=signed.model_dump(mode="json"),
)
receipt = pb.accept(proposal_id)
```

The application must supply the expected-ID guards and carry instance/client context separately. Review is a meaningful action by the caller, not something a method call can assert has occurred.

## After: proposed common path

```python
proposal = pb.proposal(status.proposal_id)
reviewed = proposal.review()
# The caller inspects this exact candidate before proceeding.
approval = proposal.approve(signer=configured_signer, reviewed=reviewed)
receipt = pb.accept(proposal.proposal_id)
```

The helper obtains a fresh challenge and checks the handle/review/challenge
proposal IDs, the reviewed candidate digest against both challenge review and
statement payload digest, and the signing root against both review parents.
It also checks the caller-supplied signer's ID/public key against the challenge
principal before calling `sign`. The returned attestation must reproduce the
exact requested statement before submission. A mismatch stops before signing or
submission, requires a fresh review, and never selects a replacement candidate.

The configured signer comes from operator-provisioned key/public-key/custody
settings. The helper neither chooses a key nor reads a private path from server
responses. Acceptance and read refresh remain separate. This proposal removes
challenge dictionary/model parsing from the common guide while retaining the
following primitives for advanced/external-signing callers. It remains pending
joint API/custody agreement, not an implemented feature.

## Advanced explicit primitives

```python
proposal = pb.proposal(status.proposal_id)
review = proposal.review(include_body=False)
# The reviewer examines this exact candidate before continuing.
challenge = proposal.prepare_approval(
    signer_id="reviewer",
    reviewed_candidate_digest=review.candidate_digest,
)
# Client-held signer relocation is pending the maintainer's design choice.
signer = LocalEd25519ApprovalSigner.open(
    signer_id="reviewer",
    private_key_path=reviewer_key_path,
    expected_public_key=challenge.signer_principal["public_key"],
    forbidden_roots=(workspace,),  # Include other existing forbidden roots as applicable.
)
signed = signer.sign(ApprovalStatement.model_validate(challenge.statement))
proposal.submit_approval(
    signed,
    reviewed_candidate_digest=review.candidate_digest,
)
receipt = pb.accept(proposal.proposal_id)  # Separate explicit acceptance.
```

These primitives preserve challenge → local sign → submit for applications that need those separate boundaries. The proposed common helper above performs the same reviewed-candidate checks without teaching every caller the wire dictionaries.

| Proposed operation | Semantics and ownership |
|---|---|
| `pb.proposal(proposal_id)` | Construct a same-instance handle; no fetch, acceptance, mutation, or inferred delegated authority. |
| `Proposal.review(include_body=False, workspace_observation=None)` | Delegate to existing structured review. Do not silently scan files, hide redactions or render prose. No changed review wire. |
| `Proposal.prepare_approval(signer_id, reviewed_candidate_digest, include_body=False)` | Obtain existing challenge. Before returning signable content, require response proposal ID, nested review candidate digest and parsed statement payload digest to match the handle/reviewed expectation, and signing root to match the challenge review parent. Preserve service-side signer eligibility checks. A mismatch refuses and requires another review. |
| `Proposal.submit_approval(attestation, reviewed_candidate_digest)` | Take an existing typed attestation; verify payload digest matches explicit expectation before sending. Do not read private keys, select a signer, accept, refresh, or claim signatures are approved merely because supplied. The service remains signature/authority verifier. |
| Existing `pb.accept(proposal_id)` | Keep unchanged and separate. Return exact accepted coordinate; explicit `world()`/`refresh()` moves read orientation. Mirror publication, floor maintenance and block sync remain separately visible. |

The reviewer-visible candidate must include the complete containing changeset, including generated/closure members. If an attestation is stale or loses a race, the existing server refusal remains decisive. Typed response validation alone cannot establish that a human or agent actually inspected the review.

## Design choice awaiting joint agreement

**Recommended:** relocate the existing approval signer and the minimum existing custody checks into a client-owned module, preserve the core import as a compatibility re-export, and add explicit guarded review/challenge/submission conveniences. Keep exact signature domain, accepted public-key matching, regular-file/no-follow checks and permission rules. No key material goes into wire models or server custody.

**Alternative:** ship only review/challenge/submission wrappers and leave signing to caller-produced attestations or the existing CLI. This is smaller but still forces a client-only user to implement or obtain a signer.

Do not invent new credential delegation, loosen forbidden-root behavior, automatically discover private keys, choose approvers or change independent-approval policy. A pure local-profile first-use example may explicitly accept without an approval only when its initialized instance policy already permits that; it must not switch policy to make the example pass.

## Runnable example shape and boundaries

1. Operator setup documents two existing profiles: ordinary local acceptance, or explicit independent approval. Credentials/principals are provisioned using current CLI/public setup, outside the authoring example. The script does not mint its own authority.
2. A disposable workspace declares one source file in `.playbill/sources.yaml`; the example explicitly authors Subject, ClaimType/evidence rules and one normative Claim citing an exact source span. No private fixture seeding or arbitrary source digest is used.
3. Prepare, show diagnostics, submit, inspect exact review and act through the selected existing approval policy. Stop on refusal; do not assume that a proposal ID proves readiness.
4. Read a new World, prefetch the known Subject/predicate and inspect all live contenders. Assert the intended statement and evidence verdict without a hidden scalar selector. Preserve the receipt coordinate; a later world is explicitly current, not silently described as the exact old receipt if another writer advanced.
5. Call free audit. Change the cited span, call `next()` with the real workspace observation, inspect the drift repair, and author the same-lineage successor citing the new span. Explain that dispositions acknowledge existing Claims; they do not themselves append attestations or settle disagreements.
6. Review/accept the repair and check complete readback plus resolved drift. Return public intent/proposal/candidate IDs and Capture references for a manager. A cross-instance bundle is out of scope.

The current example inputs in `authoring/examples.py` are valid decision templates, not a complete runnable customer program. The new guide should link them without implying their `replace-me` fields are ready to submit. It should not require `tests` imports, `Playbill._from_client`, `intent._raw`, `pb._client` or server internals in consumer code.

## Verification performed and next checks

Completed in the isolated worktree with the canonical interpreter and worktree `PYTHONPATH`:

```text
PYTHONPATH=src:packages/cruxible-client/src <canonical .venv>/bin/python -m pytest \
  tests/test_server/test_playbill_sdk_demo_world.py::test_shipped_claim_type_and_flow_a_examples_compose_to_a_supported_claim \
  tests/test_server/test_playbill_sdk_demo_world.py::test_sdk_revises_an_existing_claim_using_refs_without_dependency_drafts \
  -q --tb=short
2 passed in 19.66s
```

These tests use disposable HTTP fixtures and establish existing journey behavior; they are **not** clean installed-package end-to-end proof.

A subsequent offline wheel probe succeeded after the uv approaches failed. The
canonical Python invoked already-cached `hatchling.build.build_wheel` against the
client package. A fresh stdlib venv received that wheel via
`pip install --no-index --no-deps`; its dependencies were copied from the existing
interpreter's installed distributions, excluding `.pth` files and Core/test
packages. No dependency resolution or network fetch was claimed.

The isolated interpreter (`python -I`, cwd `/private/tmp`) successfully imported
`Playbill`, `World`, `CruxibleClient`, approval contracts, and constructed shipped
`claim-flow-a` input. It asserted `find_spec("cruxible_core") is None`,
`find_spec("tests") is None`, and that `cruxible_client.__file__` was inside the
fresh venv. Artifact: `/private/tmp/playbill-sdk-first-use-dist/cruxible_client-0.5.1-py3-none-any.whl`;
environment: `/private/tmp/playbill-sdk-first-use-client-env`.

This proves the built wheel's client-only import/example boundary, not fresh
online installation, independent dependency resolution, or the full standalone
HTTP repair journey. Initial uv cache denial and macOS system-configuration panic
were bypassed by the Python build; packaging is not considered blocked.

After agreement, add focused tests for reviewed-candidate mismatch before signer invocation/network submission; statement/root/signer mismatch; body/observation forwarding; no implicit acceptance/refresh; independent approval refusal; and successful local signing with the existing custody failure cases. Then run the standalone example in an environment containing the built client wheel without Core importability, against a disposable daemon installed separately. Keep this named scope isolated; no full suite or golden corpus is needed for the convenience slice.
