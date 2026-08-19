"""PC-A2 v2 settlement, projection, and replay parity tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity
from cruxible_core.playbill.authoring_profiles import (
    CLAIM_TYPE_AUTHORING_PROFILES,
    AuthorityProfileParametersV1,
    ClaimTypeProfileInputV1,
    expand_claim_type_profile,
)
from cruxible_core.playbill.candidates import CandidateRecordV3, candidate_digest
from cruxible_core.playbill.claim_types import ClaimType, claim_type_digest, render_claim_type
from cruxible_core.playbill.compiler import current_compiler_coordinate
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.serving import bind_current_projection
from cruxible_core.playbill.settlement import (
    ChangeActorBinding,
    ChangeSetRecordV3,
    parse_change_set_record,
    prepare_generation,
    render_change_set,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign

TIMESTAMP = "2026-08-16T16:00:00.000000Z"
CLAIM_TYPE_PATH = "claim-types/project.work_item/status.yaml"


def claim_type() -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.status"),
        predicate="project.work_item.status",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"enum": ["blocked", "done", "ready"], "type": "string"},
        cardinality="one",
        permitted_roles=("normative",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        authority=ArtifactAuthority(
            propose_roles=("owner",),
            approve_roles=("owner",),
        ),
    )


def test_v2_changeset_keeps_frozen_candidate_and_approval_preimages(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    exact_claim_type = claim_type()
    tree = {
        **instance.tree_at(base.git_oid),
        CLAIM_TYPE_PATH: render_claim_type(exact_claim_type),
    }
    proposal = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/claim-type-status",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    assert isinstance(proposal.candidate, CandidateRecordV3)
    candidate = proposal.candidate
    approval = _sign(owner, candidate.candidate_digest, base.semantic_root)

    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=(approval,),
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=1,
    )

    assert isinstance(bundle.record, ChangeSetRecordV3)
    assert candidate_digest(bundle.record.candidate).tagged == candidate.candidate_digest
    assert approval.attestation.payload_digest == candidate.candidate_digest
    assert bundle.record.closure_proof.paths == candidate.candidate.scope
    assert tuple(item.path for item in bundle.record.members) == candidate.candidate.scope
    assert (
        parse_change_set_record(render_change_set(bundle.record), path=bundle.record_path)
        == bundle.record
    )
    tampered = bundle.record.model_dump(mode="json")
    tampered["law_evidence"][0]["law_digest"] = "sha256:" + "00" * 32
    with pytest.raises(ValidationError, match="structured law evidence"):
        ChangeSetRecordV3.model_validate(tampered)


def test_claim_type_v2_generation_projects_and_replays_after_restart(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    exact_claim_type = claim_type()
    tree = {
        **instance.tree_at(base.git_oid),
        CLAIM_TYPE_PATH: render_claim_type(exact_claim_type),
    }
    evaluated = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/claim-type-replay",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    assert isinstance(evaluated.candidate, CandidateRecordV3)
    candidate = evaluated.candidate
    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=(_sign(owner, candidate.candidate_digest, base.semantic_root),),
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=1,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate().git_oid == bundle.oid
    assert isinstance(reopened.accepted_history()[-1].record, ChangeSetRecordV3)
    publication = Path(reopened.inspect().storage_directories["projections"])
    with bind_current_projection(publication, expected=reopened.accepted_coordinate()) as handle:
        connection = sqlite3.connect(handle.index_path)
        try:
            envelope = connection.execute(
                "SELECT kind,artifact_digest FROM artifact_envelopes WHERE identity = ?",
                (exact_claim_type.identity.qualified,),
            ).fetchone()
            schemas = {
                row[0]
                for row in connection.execute(
                    "SELECT schema_id FROM semantic_facts WHERE subject_identity = ?",
                    (exact_claim_type.identity.qualified,),
                )
            }
        finally:
            connection.close()
    assert envelope == ("claim-type", claim_type_digest(exact_claim_type).tagged)
    assert {
        "playbill.claim_type.attestation_coverage",
        "playbill.claim_type.governance",
        "playbill.claim_type.history",
        "playbill.claim_type.identity",
        "playbill.claim_type.policies",
        "playbill.claim_type.provenance",
        "playbill.claim_type.references",
    } == schemas


def test_profile_law_evidence_reproduces_during_settlement(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    direct = claim_type()
    profile = next(
        item
        for item in CLAIM_TYPE_AUTHORING_PROFILES
        if item.profile_id == "ordinary-project-fact-v1"
    )
    expansion = expand_claim_type_profile(
        ClaimTypeProfileInputV1(
            profile_id=profile.profile_id,
            profile_digest=profile.profile_digest,
            authoring_source_digest="sha256:" + "81" * 32,
            compiler_digest=current_compiler_coordinate().rule_digest,
            structure=direct.structure,
            authority_parameters=AuthorityProfileParametersV1(
                propose_roles=("owner",),
                approve_roles=("owner",),
            ),
        )
    )
    tree = {**instance.tree_at(base.git_oid), CLAIM_TYPE_PATH: render_claim_type(direct)}
    proposal = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/profile-settlement",
            proposed_base_oid=base.git_oid,
            claim_type_expansions=(expansion.evidence,),
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    assert isinstance(proposal.candidate, CandidateRecordV3)
    candidate = proposal.candidate

    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=(_sign(owner, candidate.candidate_digest, base.semantic_root),),
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=1,
    )

    assert isinstance(bundle.record, ChangeSetRecordV3)
    assert bundle.record.law_evidence[0].result["authoring_expansion"] == (
        expansion.evidence.model_dump(mode="json")
    )
