"""Client-only Claim -> reviewed approval -> acceptance -> drift -> repair demo.

Run only in a disposable, initialized instance with an empty demo namespace and
an operator-provisioned reviewer signer. The CLI prompts before each approval
and before each acceptance. Server initialization and key provisioning are
operator responsibilities; this program never creates or discovers keys.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from cruxible_client import (
    Cardinality,
    ClaimObjectKind,
    ClaimRole,
    Disposition,
    Duration,
    LocalEd25519ApprovalSigner,
    Playbill,
    ReferentSensitivity,
    ReviewedProposal,
)
from cruxible_client.authoring.signing import ApprovalSigner
from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.policies import ClaimAdmissionPolicyV1, ClaimResolutionPolicyV1

SUBJECT = "sdk.demo/patch-sla"
PREDICATE = "sdk.demo.patch_sla"
SOURCE = "corpus.sdk-demo"


def run(
    pb: Playbill,
    workspace: Path,
    signer: ApprovalSigner,
    *,
    review_decision: Callable[[ReviewedProposal], bool],
    accept_decision: Callable[[str], bool],
) -> dict[str, object]:
    """Callbacks make review and activation decisions explicit to the consumer."""
    source = workspace / "sdk-demo.md"
    catalog = workspace / ".playbill" / "sources.yaml"
    if source.exists() or catalog.exists():
        raise ValueError("Use a fresh disposable workspace; demo source/catalog already exists")
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "tag: playbill-source-catalog-v1\ncatalog_kind: portable\nentries:\n"
        f"  - name: {SOURCE}\n    locator: sdk-demo.md\n"
        "    document_id: sdk-demo\n    document_kind: runbook\n"
        "    title: SDK demo\n    media_type: text/markdown\n"
        "    governance_scope: [Document:sdk-demo]\n",
        encoding="utf-8",
    )
    source.write_text("Patch within forty-eight hours.\n", encoding="utf-8")
    subject = pb.subject(subject=SUBJECT, pins=(), lifecycle=ArtifactLifecycle())
    claim_type = pb.claim_type(
        predicate=PREDICATE,
        subject_kinds=("sdk.demo",),
        object_kind=ClaimObjectKind.LITERAL,
        value_schema={"type": "integer"},
        object_subject_kinds=(),
        cardinality=Cardinality.ONE,
        permitted_roles=(ClaimRole.NORMATIVE,),
        referent_sensitivity=ReferentSensitivity.IDENTITY,
        sources=(SOURCE,),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one", eligible_verdicts=("supported",), selector="only_contender"
        ),
        pins=(),
        evidence_freshness=None,
    )

    def settle(intent):
        if intent.refused:
            raise ValueError(f"Preflight refused: {intent.diagnostics}")
        intent.submit()
        proposal_id = intent.status().proposal_id
        if proposal_id is None:
            raise ValueError("Submission did not produce a proposal")
        proposal = pb.proposal(proposal_id)
        reviewed = proposal.review()
        if not review_decision(reviewed):
            raise RuntimeError(f"Left proposal {proposal_id} pending after review")
        proposal.approve(signer=signer, reviewed=reviewed)
        if not accept_decision(proposal_id):
            raise RuntimeError(f"Left approved proposal {proposal_id} pending acceptance")
        receipt = pb.accept(proposal_id)
        if receipt.status != "accepted":
            raise RuntimeError(f"Acceptance did not win: {receipt.status}")
        world = pb.world()
        assert receipt.accepted_coordinate is not None
        assert world.coordinate.model_dump(mode="json") == receipt.accepted_coordinate.model_dump(
            mode="json"
        )
        rows = world.prefetch(subjects=(SUBJECT,), predicates=(PREDICATE,))
        if len(rows) != 1 or rows[0].verdict != "supported":
            raise ValueError("Expected one supported demo contender after acceptance")
        return rows[0]

    first = settle(
        pb.claim(
            subject=subject.address,
            predicate=claim_type.predicate,
            value=48,
            role=ClaimRole.NORMATIVE,
            rationale="Runbook gives the initial deadline.",
            supported_by=pb.file("sdk-demo.md").anchor("forty-eight hours"),
            copied_from=None,
            self_source=None,
            qualifier=None,
            effective_period=None,
            revises=None,
            dispositions={},
            subject_definition=subject,
            claim_type_definition=claim_type,
        ).prepare()
    )
    pb.audit()  # Free deterministic audit worklist.
    source.write_text("Patch within forty-nine hours.\n", encoding="utf-8")
    drift = pb.next(expiring_within=Duration.days(count=7))
    assert any(row["reason"] == "citation_drifted" for row in drift.items)
    repaired = settle(
        pb.claim(
            subject=SUBJECT,
            predicate=PREDICATE,
            value=49,
            role=ClaimRole.NORMATIVE,
            rationale="The source deadline changed; revise the same Claim with new evidence.",
            supported_by=pb.file("sdk-demo.md").anchor("forty-nine hours"),
            copied_from=None,
            self_source=None,
            qualifier=None,
            effective_period=None,
            revises=first.claim_id,
            dispositions={first.claim_id: Disposition.CONTRADICT},
            subject_definition=None,
            claim_type_definition=None,
        ).prepare()
    )
    assert repaired.claim_id == first.claim_id and repaired.value == 49
    after = pb.next(expiring_within=Duration.days(count=7))
    assert not any(row["reason"] == "citation_drifted" for row in after.items)
    return {
        "claim_id": repaired.claim_id,
        "value": repaired.value,
        "verdict": repaired.verdict,
        "revision": repaired.revision,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--signer-id", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--forbidden-root", type=Path, action="append", required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    signer = LocalEd25519ApprovalSigner.open(
        signer_id=args.signer_id,
        private_key_path=args.private_key,
        expected_public_key=args.public_key,
        forbidden_roots=(workspace, *args.forbidden_root),
    )
    pb = Playbill.connect(target=args.target, instance=args.instance, workspace=workspace)

    def decide(reviewed: ReviewedProposal) -> bool:
        print(reviewed.details.model_dump_json(indent=2))
        return input("Approve this exact reviewed candidate? [yes/no] ") == "yes"

    result = run(
        pb,
        workspace,
        signer,
        review_decision=decide,
        accept_decision=lambda proposal: input(f"Accept {proposal}? [yes/no] ") == "yes",
    )
    print(result)


if __name__ == "__main__":
    main()
