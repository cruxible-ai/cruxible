from __future__ import annotations

from pathlib import Path

from cruxible_core.playbill.acquisition_policies import (
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_core.playbill.assembler import ProjectionAssembler
from cruxible_core.playbill.candidates import CandidateRecordV3
from cruxible_core.playbill.captures import capture_contract_path, render_capture_contract
from cruxible_core.playbill.claim_types import claim_type_path, render_claim_type
from cruxible_core.playbill.compiler import projection_registry_for_compiler
from cruxible_core.playbill.projection_artifacts import parse_projection_tree
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.providers import provider_path, render_provider
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.standing_mandates import (
    render_standing_mandate,
    standing_mandate_path,
)
from cruxible_core.service.playbill_evidence import service_get_playbill_standing_mandate
from tests.test_playbill._pc_c_support import capture_contract, provider
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_acquisition_policies import _policy, _rule
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim_type
from tests.test_playbill.test_standing_mandates import _mandate

TIMESTAMP = "2026-08-16T20:00:00.000000Z"


def test_evidence_artifacts_share_acceptance_closure_and_projection(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    contract = capture_contract()
    provider_artifact = provider(contract)
    policy = _policy(_rule("orders"))
    claim_type = _claim_type()
    mandate = _mandate()
    candidate_tree = {
        **instance.tree_at(base.git_oid),
        capture_contract_path(contract.identity.name): render_capture_contract(contract),
        provider_path(provider_artifact.identity.name): render_provider(provider_artifact),
        claim_type_path(claim_type.predicate): render_claim_type(claim_type),
        acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
        standing_mandate_path(mandate.identity.name): render_standing_mandate(mandate),
    }
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/evidence-artifacts",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=TIMESTAMP,
    )
    assert isinstance(proposed.candidate, CandidateRecordV3)
    assert {member.artifact_kind for member in proposed.candidate.members} == {
        "capture-contract",
        "claim-type",
        "provider",
        "source-acquisition-policy",
        "standing-mandate",
    }
    approval = _sign(owner, proposed.candidate.candidate_digest, base.semantic_root)
    service_submit_playbill_approval(
        instance,
        proposal_id=proposed.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=proposed.admission.proposal_id,
    )
    assert activated.status == "accepted"

    coordinate = instance.accepted_coordinate()
    publication = tmp_path / "manual-projection"
    publication.mkdir()
    assembler = ProjectionAssembler(
        instance._ledger,
        accepted=coordinate,
        publication_directory=publication,
        bodies=instance.body_store(),
    )
    projected = parse_projection_tree(
        instance.tree_at(coordinate.git_oid),
        registry=projection_registry_for_compiler(coordinate.compiler),
        bodies=instance.body_store(),
        coordinate=assembler.request(output_staging_directory=publication / ".stage"),
    )
    kinds = {row.kind for row in projected.envelopes}
    assert {
        "capture-contract",
        "claim-type",
        "provider",
        "source-acquisition-policy",
        "standing-mandate",
    }.issubset(kinds)
    schemas = {fact.schema_id for fact in projected.semantic_facts}
    assert {
        "playbill.provider.identity",
        "playbill.provider.keys",
        "playbill.provider.provenance",
        "playbill.source_acquisition_policy.policy",
        "playbill.standing_mandate.authority",
    }.issubset(schemas)
    queried = service_get_playbill_standing_mandate(
        instance,
        identity=mandate.identity.qualified,
    )
    assert queried.coordinate.git_oid == coordinate.git_oid
    assert queried.mandate == mandate
