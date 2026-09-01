"""PC-A2 multi-member closure, law evidence, and deterministic rebase tests."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.candidates import CandidateRecordV3
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    render_claim_type,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_digest
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.closure import evaluate_dependency_closure
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    deterministic_rebase_v2,
    evaluate_proposal_tree,
)
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.service.review import (
    render_playbill_proposal_review,
    service_review_playbill_proposal,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign

TIMESTAMP = "2026-08-16T14:00:00.000000Z"
SUBJECT_PATH = "subjects/project.work_item/wi-closure.json"


def subject(*, lifecycle: ArtifactLifecycle = ArtifactLifecycle()) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-closure"),
        subject_kind="project.work_item",
        subject_id="wi-closure",
        lifecycle=lifecycle,
    )


def claim_type(
    predicate: str,
    *,
    pins: tuple[ArtifactPin, ...] = (),
) -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        permitted_roles=("normative",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        pins=pins,
    )


def test_multi_kind_candidate_scope_member_and_closure_paths_are_identical(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    base_tree = instance.tree_at(current.git_oid)
    shell = subject()
    status = claim_type(
        "project.work_item.status",
        pins=(
            ArtifactPin(
                role="example-subject",
                target=shell.identity,
                artifact_digest=subject_digest(shell).tagged,
            ),
        ),
    )
    claim_type_path = "claim-types/project.work_item/status.json"
    candidate_tree = {
        **base_tree,
        SUBJECT_PATH: render_subject(shell),
        claim_type_path: render_claim_type(status),
    }

    evaluation = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=base_tree,
        proposed_tree=candidate_tree,
        current=current,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
    )

    assert evaluation.diagnostics == ()
    assert isinstance(evaluation.candidate, CandidateRecordV3)
    assert evaluation.candidate.candidate.scope == (
        claim_type_path,
        SUBJECT_PATH,
    )
    assert tuple(item.path for item in evaluation.candidate.members) == (
        claim_type_path,
        SUBJECT_PATH,
    )
    assert evaluation.candidate.closure_proof.paths == evaluation.candidate.candidate.scope
    assert tuple(item.path for item in evaluation.candidate.law_evidence) == (
        claim_type_path,
        SUBJECT_PATH,
    )
    assert evaluation.candidate.law_evidence[0].evaluation_coordinate.git_oid == (current.git_oid)
    assert evaluation.candidate.law_evidence[0].evaluation_coordinate.generation_root == (
        current.generation_root
    )
    assert evaluation.candidate.members[0].dependency_proof_refs[0].target_path == (SUBJECT_PATH)


def test_changed_dependency_reports_exact_sorted_missing_dependents() -> None:
    original = subject()
    pin = ArtifactPin(
        role="subject-contract",
        target=original.identity,
        artifact_digest=subject_digest(original).tagged,
    )
    status_path = "claim-types/project.work_item/status.json"
    priority_path = "claim-types/project.work_item/priority.json"
    parent = {
        SUBJECT_PATH: render_subject(original),
        status_path: render_claim_type(claim_type("project.work_item.status", pins=(pin,))),
        priority_path: render_claim_type(claim_type("project.work_item.priority", pins=(pin,))),
    }
    retired = subject(
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=subject_digest(original).tagged,
        )
    )
    candidate = {**parent, SUBJECT_PATH: render_subject(retired)}

    closure = evaluate_dependency_closure(
        parent_tree=parent,
        candidate_tree=candidate,
        scope=(SUBJECT_PATH,),
    )

    assert closure.verdict == "refused"
    assert tuple(item.path for item in closure.missing_dependents) == (
        priority_path,
        status_path,
    )
    assert all(
        item.triggering_dependency_digest == subject_digest(original).tagged
        for item in closure.missing_dependents
    )
    assert all(
        item.permitted_dispositions == ("invalidation", "retire", "successor")
        for item in closure.missing_dependents
    )


def test_three_way_rebase_drops_noop_and_reports_all_exact_conflict_digests() -> None:
    old = {"documents/a.json": b"old", "documents/b.json": b"stable"}
    proposed = {"documents/a.json": b"proposed", "documents/b.json": b"same-new"}
    new = {"documents/a.json": b"concurrent", "documents/b.json": b"same-new"}

    result = deterministic_rebase_v2(
        old_parent_tree=old,
        new_parent_tree=new,
        proposed_tree=proposed,
    )

    assert tuple(item.path for item in result.conflicts) == ("documents/a.json",)
    conflict = result.conflicts[0]
    assert conflict.code == "playbill.rebase.member_conflict"
    assert conflict.old_parent_digest is not None
    assert conflict.proposed_digest is not None
    assert conflict.new_parent_digest is not None
    assert result.tree["documents/b.json"] == b"same-new"
    assert result.approvals_invalidated is True


def test_candidate_tree_reuse_lookup_blocks_two_simultaneous_adjacent_types(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    base_tree = instance.tree_at(current.git_oid)
    first_path = "claim-types/ops.work_item/status.json"
    second_path = "claim-types/project.work_item/status.json"
    candidate_tree = {
        **base_tree,
        first_path: render_claim_type(claim_type("ops.work_item.status")),
        second_path: render_claim_type(claim_type("project.work_item.status")),
    }

    evaluation = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=base_tree,
        proposed_tree=candidate_tree,
        current=current,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
    )

    assert evaluation.candidate is None
    assert [item.code for item in evaluation.diagnostics] == [
        "playbill.reuse.distinction_claim_missing",
        "playbill.reuse.distinction_claim_missing",
    ]


def test_multi_member_malformed_artifact_is_a_typed_refusal(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    base_tree = instance.tree_at(current.git_oid)
    evaluation = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=base_tree,
        proposed_tree={
            **base_tree,
            SUBJECT_PATH: render_subject(subject()),
            "claim-types/project.work_item/status.json": b"not-json\n",
        },
        current=current,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
    )

    assert evaluation.candidate is None
    # The refusal is the ClaimType kind's own, which the one evaluator now states
    # for a malformed member wherever it appears -- alone or beside others.
    assert [item.code for item in evaluation.diagnostics] == ["playbill.claim_type.format_invalid"]


def test_claim_type_rebase_reports_exact_conflict_evidence_and_no_candidate(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    base_tree = instance.tree_at(base.git_oid)
    path = "claim-types/project.work_item/status.json"
    proposed = {**base_tree, path: render_claim_type(claim_type("project.work_item.status"))}
    concurrent = {
        **base_tree,
        path: render_claim_type(
            claim_type("project.work_item.status").model_copy(
                update={"permitted_roles": ("normative", "observation")}
            )
        ),
    }
    moved = AcceptedProjectionCoordinate(
        **{
            **base.model_dump(),
            "git_oid": "3" * len(base.git_oid),
            "semantic_root": "sha256:" + "44" * 32,
            "generation_root": "sha256:" + "55" * 32,
        }
    )

    evaluation = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=concurrent,
        proposed_tree=proposed,
        current=moved,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=True,
        actor_id="owner",
    )

    assert evaluation.candidate is None
    assert [item.code for item in evaluation.diagnostics] == ["playbill.rebase.member_conflict"]
    assert "old_parent_digest" in evaluation.diagnostics[0].message
    assert "new_parent_digest" in evaluation.diagnostics[0].message
    assert "proposed_digest" in evaluation.diagnostics[0].message


def test_atomic_review_cannot_hide_invalidation_members(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    original_subject = subject()
    original_claim_type = claim_type(
        "project.work_item.status",
        pins=(
            ArtifactPin(
                role="example-subject",
                target=original_subject.identity,
                artifact_digest=subject_digest(original_subject).tagged,
            ),
        ),
    )
    claim_path = "claim-types/project.work_item/status.json"
    initial = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/initial-closure",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree={
            **instance.tree_at(base.git_oid),
            SUBJECT_PATH: render_subject(original_subject),
            claim_path: render_claim_type(original_claim_type),
        },
        timestamp=TIMESTAMP,
    )
    assert isinstance(initial.candidate, CandidateRecordV3)
    approver = client_material(instance.root.parent, instance)
    initial_approval = _sign(
        approver,
        initial.candidate.candidate_digest,
        base.semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=initial.admission.proposal_id,
        attestation=initial_approval.attestation,
        authenticated_submitter=approver.principal.principal_id,
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=initial.admission.proposal_id,
            activated_by="owner",
        ).status
        == "accepted"
    )

    current = instance.accepted_coordinate()
    retired_subject = subject(
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=subject_digest(original_subject).tagged,
        )
    )
    retired_claim_type = original_claim_type.model_copy(
        update={
            "pins": (
                ArtifactPin(
                    role="example-subject",
                    target=retired_subject.identity,
                    artifact_digest=subject_digest(retired_subject).tagged,
                ),
            ),
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=claim_type_digest(original_claim_type).tagged,
            ),
        }
    )
    invalidation = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/retire-closure",
            proposed_base_oid=current.git_oid,
        ),
        candidate_tree={
            **instance.tree_at(current.git_oid),
            SUBJECT_PATH: render_subject(retired_subject),
            claim_path: render_claim_type(retired_claim_type),
        },
        timestamp="2026-08-16T14:01:00.000000Z",
    )
    assert isinstance(invalidation.candidate, CandidateRecordV3)
    review = service_review_playbill_proposal(
        instance,
        proposal_id=invalidation.admission.proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=False),
    )

    assert tuple(item.closure_role for item in review.members) == (
        "invalidation",
        "invalidation",
    )
    rendered = render_playbill_proposal_review(review)
    assert rendered.count("Closure role: invalidation") == 2
    assert rendered.count("Semantic delta:") == 2
    assert all(member.semantic_delta for member in review.members)
