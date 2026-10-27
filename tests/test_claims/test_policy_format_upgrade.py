"""Compiler revision 36 writes v4 identity evidence policies; v3 ones wait for an upgrade.

An instance whose ClaimTypes were accepted with v3 policies keeps working after
`compiler upgrade`: only a ClaimType a write creates or changes must state the
revision's format, and its refusal names the governed command that fixes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1,
    capture_contract_path,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimEvidenceAdmissionPolicyV3,
    ClaimEvidenceAdmissionRuleV3,
)
from cruxible_client.contracts.subjects import render_subject
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.compiler.compiler import CLAIM_REBIND_COMPILER, LIGHT_ARTIFACTS_COMPILER
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_inspect_playbill_proposal,
    service_propose_compiler_upgrade,
    service_submit_playbill_approval,
)
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from tests.core_support._subject_kind_support import core_assigned
from tests.core_support._write_support import caller
from tests.core_support.legacy_capture_builders import initialize_legacy_local
from tests.test_claims.test_claims import TIMESTAMP, _claim_type, _subject
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_ledger.test_activation import _sign

STATUS = "project.work_item.status"
SLUG = "project.work_item.slug"


def _v3_rule(*, rule_id: str = "self", kinds: tuple[str, ...] = ("self_asserted",)) -> Any:
    return ClaimEvidenceAdmissionRuleV3(
        rule_id=rule_id,
        claim_roles=("normative", "observation"),
        capture_contracts=(
            {
                "role": CAPTURE_CONTRACT_REF_ROLE,
                "target": COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1.identity,
            },
        ),
        evidence_kinds=kinds,
        admission="direct",
        subject_binding="exact_claim_subject",
    )


def _v3_type(predicate: str = STATUS, **rule: Any) -> ClaimType:
    """A v7 ClaimType stating a v3 identity evidence policy, as revision 35 wrote them."""

    return _claim_type().model_copy(
        update={
            "artifact_format": "playbill-claim-type-v7",
            "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
            "predicate": predicate,
            "evidence_requirement": "self",
            "revision_evidence": "replace",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV3(rules=(_v3_rule(**rule),)),
        }
    )


def _tree(instance: PlaybillInstance) -> dict[str, bytes]:
    return dict(instance.tree_at(instance.accepted_coordinate().git_oid))


def _claim_type_at(tree: dict[str, bytes], predicate: str) -> ClaimType:
    path = claim_type_path(predicate)
    return parse_claim_type(tree[path], path=path)


def _write(instance: PlaybillInstance, subject: str, value: str) -> Any:
    request = WriteRequest.model_validate(
        {
            "because": "The writer checked it.",
            "changes": [
                {
                    "op": "set",
                    "subject": subject,
                    "field": "status",
                    "value": value,
                    "role": "observation",
                },
            ],
        }
    )
    return service_playbill_write(instance, request=request, caller=caller())


def _approve_and_activate(instance: PlaybillInstance, owner: Any, proposal_id: str) -> None:
    candidate = service_inspect_playbill_proposal(
        instance, proposal_id=proposal_id
    ).proposal.candidate
    assert candidate is not None
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=_sign(
            owner, candidate.candidate_digest, instance.accepted_coordinate().semantic_root
        ).attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance, proposal_id=proposal_id, activated_by="owner"
    )
    assert activated.status == "accepted"
    instance.refresh()


def _upgrade_compiler(instance: PlaybillInstance, owner: Any) -> None:
    proposal = service_propose_compiler_upgrade(
        instance, target=LIGHT_ARTIFACTS_COMPILER, actor_id="owner", timestamp=TIMESTAMP
    ).proposal
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    _approve_and_activate(instance, owner, proposal.admission.proposal_id)
    assert instance.accepted_coordinate().compiler == LIGHT_ARTIFACTS_COMPILER


@pytest.fixture
def revision_35(tmp_path: Path) -> tuple[PlaybillInstance, Any, str]:
    """A revision-35 instance with v3-policy ClaimTypes and one Claim citing its writer."""

    instance, owner = initialize_legacy_local(tmp_path, compiler=CLAIM_REBIND_COMPILER)
    subject = _subject()
    tree = core_assigned(
        instance,
        {
            **_tree(instance),
            "subjects/project.work_item/wi-42.json": render_subject(subject),
            claim_type_path(STATUS): render_claim_type(_v3_type()),
        },
    )
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="pre-light-types")
    written = _write(instance, "project.work_item/wi-42", "ready")
    assert written.status == "accepted", written
    claim_id = written.changes[0].claim.removeprefix("Claim:")
    return instance, owner, claim_id


def test_old_v3_claim_types_keep_working_after_the_compiler_upgrade(
    revision_35: tuple[PlaybillInstance, Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner, _claim_id = revision_35
    # Revision 35 authored both ClaimTypes (the slug key and the status) with v3.
    tree = _tree(instance)
    for predicate in (STATUS, SLUG):
        policy = _claim_type_at(tree, predicate).evidence_admission_policy
        assert isinstance(policy, ClaimEvidenceAdmissionPolicyV3)
    _upgrade_compiler(instance, owner)

    import cruxible_core.proposals.proposals as proposals

    gate = proposals._validate_identity_evidence_policy_format
    judged: list[str] = []

    def counted(claim_type: ClaimType, compiler: object) -> None:
        judged.append(claim_type.identity.qualified)
        gate(claim_type, compiler)

    monkeypatch.setattr(proposals, "_validate_identity_evidence_policy_format", counted)
    # The first revision-36 write installs the v3 coordinator self-source contract,
    # whose referrers are the v3 ClaimTypes: they are rechecked, not re-gated.
    written = _write(instance, "project.work_item/wi-43", "done")
    assert written.status == "accepted", written
    path = capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1.identity.name)
    assert _tree(instance)[path] != tree[path]
    # No ClaimType was written, so the gate judged none (the recheck adds no work).
    assert judged == []
    assert _write(instance, "project.work_item/wi-42", "blocked").status == "accepted"


def test_an_edit_of_a_v3_claim_type_on_revision_36_names_the_upgrade(
    revision_35: tuple[PlaybillInstance, Any, str],
) -> None:
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    instance, owner, _claim_id = revision_35
    note = "project.work_item.note"
    tree = _tree(instance)
    tree[claim_type_path(note)] = render_claim_type(_v3_type(note))
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="note")
    _upgrade_compiler(instance, owner)
    tree = _tree(instance)
    previous = _claim_type_at(tree, note)
    edited = previous.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(previous).tagged),
            "literal_schema": {"enum": ["blocked", "done", "ready", "review"], "type": "string"},
        }
    )
    tree[claim_type_path(note)] = render_claim_type(edited)
    base = instance.accepted_coordinate()
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/v3-edit", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    assert result.candidate is None
    (diagnostic,) = result.evaluation.diagnostics
    assert diagnostic.code == "cruxible.claim_type.format_invalid"
    assert "`cruxible claim-type upgrade`" in diagnostic.message
    assert "Broken referrer" not in diagnostic.message
