"""Served proposal operations recompile retained writes and require fresh review."""

from pathlib import Path

import pytest

from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.recompilation import ProposalRecompiled
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.preflight import (
    authoring_intent_id_for_proposal_ref,
    compute_preflight,
)
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.change_preview import preview_guards
from cruxible_core.service.proposals.proposals import (
    _proposal_result,
    service_readmit_playbill_proposal,
)
from tests.core_support._support import client_material
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_ledger.test_activation import _sign
from tests.test_service.test_write_verbs import _set, _write


@pytest.mark.parametrize("operation", ["activate", "readmit"])
def test_stale_write_is_fresh_lowering_at_head_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    instance, _ = seed_write_surface(tmp_path)
    pending = _write(instance, _set(f"{KIND}/wi-1", "title", "parallel"), accept="never")
    assert pending.proposal is not None
    source = _proposal_result(instance, pending.proposal.proposal_id)
    intent_id = authoring_intent_id_for_proposal_ref(source.admission.target_ref, actor_id="owner")
    assert intent_id is not None
    intent = AuthoringIntentCoordinator.for_instance(instance).store.get(
        intent_id, actor_id="owner"
    )
    assert _write(instance, _set(f"{KIND}/wi-2", "title", "unrelated")).status == "accepted"
    head = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    original = instance.immutable_tree_at

    def tree_at(oid: str):  # type: ignore[no-untyped-def]
        assert oid != intent.base_coordinate.git_oid
        return original(oid)

    with monkeypatch.context() as tripwire:
        tripwire.setattr(instance, "immutable_tree_at", tree_at)
        with preview_guards(True):
            oracle = compute_preflight(
                instance,
                intent=intent.model_copy(update={"base_coordinate": head}),
                actor=AuthenticatedActor(actor_id="owner"),
            )
        if operation == "activate":
            activated = service_activate_playbill_proposal(
                instance, proposal_id=source.admission.proposal_id, activated_by="owner"
            )
        else:
            readmitted = service_readmit_playbill_proposal(
                instance, proposal_id=source.admission.proposal_id, actor_id="owner"
            )
            activated = service_activate_playbill_proposal(
                instance,
                proposal_id=readmitted.proposal.proposal.admission.proposal_id,
                activated_by="owner",
            )
    assert activated.status == "accepted" and activated.proposal_id != source.admission.proposal_id
    successor = _proposal_result(instance, activated.proposal_id)
    assert oracle.evaluation is not None and oracle.evaluation.candidate is not None
    assert successor.candidate is not None
    assert successor.candidate.members == oracle.evaluation.candidate.members
    # Cold ledger verification consumes the ordinary changeset: no retained
    # recompilation input or replay-specific format is needed.
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()


def test_stale_approve_returns_new_pending_id_without_recording_old_signature(
    tmp_path: Path,
) -> None:
    instance, _ = seed_write_surface(tmp_path, self_approval=False)
    reviewer = client_material(tmp_path, instance)
    pending = _write(instance, _set(f"{KIND}/wi-1", "title", "review this"))
    assert pending.proposal is not None
    source = _proposal_result(instance, pending.proposal.proposal_id)
    assert source.candidate is not None
    signed = _sign(
        reviewer, source.candidate.candidate_digest, instance.accepted_coordinate().semantic_root
    )
    moving = _write(instance, _set(f"{KIND}/wi-2", "title", "move head"))
    assert moving.proposal is not None
    proposal = _proposal_result(instance, moving.proposal.proposal_id)
    assert proposal.candidate is not None
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=_sign(
            reviewer,
            proposal.candidate.candidate_digest,
            instance.accepted_coordinate().semantic_root,
        ).attestation,
        authenticated_submitter="reviewer",
        activate_when_complete=True,
        may_activate=True,
    )
    result = service_submit_playbill_approval(
        instance,
        proposal_id=source.admission.proposal_id,
        attestation=signed.attestation,
        authenticated_submitter="reviewer",
        activate_when_complete=True,
        may_activate=True,
    )
    assert isinstance(result, ProposalRecompiled) and result.status == "pending_review"
    assert not result.members_identical  # self-source Capture now binds the new head
    successor = _proposal_result(instance, result.proposal_id)
    assert successor.candidate is not None
    before = {member.path: member.candidate_artifact_digest for member in source.candidate.members}
    after = {
        member.path: member.candidate_artifact_digest for member in successor.candidate.members
    }
    expected = sorted(
        (path, "added" if path not in before else "replaced")
        for path, digest in after.items()
        if path not in before or before[path] != digest
    ) + sorted((path, "removed") for path in before if path not in after)
    assert [(item.path, item.change) for item in result.changed_members] == sorted(expected)
    assert not result.changed_members_truncated
    assert instance.proposal_evidence().read_approvals(result.candidate_digest) == ()
    assert instance.proposal_evidence().read_approvals(source.candidate.candidate_digest) == ()
    fresh = service_submit_playbill_approval(
        instance,
        proposal_id=result.proposal_id,
        attestation=_sign(
            reviewer, result.candidate_digest, instance.accepted_coordinate().semantic_root
        ).attestation,
        authenticated_submitter="reviewer",
        activate_when_complete=True,
        may_activate=True,
    )
    assert fresh.activation is not None and fresh.activation.status == "accepted"


def test_same_request_write_recompiles_once_after_unrelated_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.authoring import documents

    instance, _ = seed_write_surface(tmp_path)
    activate = documents.service_activate_playbill_proposal
    moved = []

    def racing(target: PlaybillInstance, **kwargs):  # type: ignore[no-untyped-def]
        if not moved:
            moved.append(True)
            assert _write(target, _set(f"{KIND}/wi-2", "title", "contender")).status == "accepted"
        return activate(target, **kwargs)

    monkeypatch.setattr(documents, "service_activate_playbill_proposal", racing)
    outcome = _write(instance, _set(f"{KIND}/wi-1", "title", "survives the race"))
    assert outcome.status == "accepted", outcome
    assert outcome.proposal is not None
    result = _proposal_result(instance, outcome.proposal.proposal_id)
    assert result.admission.readmits is not None
    source_id = result.admission.readmits.source_proposal_id
    withdrawal = instance.proposal_evidence().read_withdrawal(source_id)
    assert withdrawal is not None and withdrawal.superseded_by == outcome.proposal.proposal_id


@pytest.mark.parametrize("size", (0, 1, 50, 51, 500))
def test_changed_member_preview_is_sorted_bounded_and_marks_truncation(size: int) -> None:
    from cruxible_client.contracts.recompilation import RECOMPILED_MEMBER_PATH_LIMIT
    from cruxible_core.service.proposals.recompile import _changed_members

    before = {"unchanged": "same", "replaced": "old", "removed": None}
    after = {"unchanged": "same", "replaced": "new"}
    after.update({f"added/{i:04}": "new" for i in reversed(range(size))})
    changes, truncated = _changed_members(before, after)
    expected = sorted(
        [(f"added/{i:04}", "added") for i in range(size)]
        + [("replaced", "replaced"), ("removed", "removed")]
    )
    assert [(item.path, item.change) for item in changes] == expected[:RECOMPILED_MEMBER_PATH_LIMIT]
    assert truncated == (len(expected) > RECOMPILED_MEMBER_PATH_LIMIT)
    assert _changed_members(before, before) == ((), False)


@pytest.mark.parametrize(
    ("hold", "may_activate"), [(False, True), (True, True), (False, False)]
)
def test_stale_approve_lands_no_approval_candidate_when_permitted(
    tmp_path: Path, hold: bool, may_activate: bool
) -> None:
    from cruxible_client import contracts

    instance, owner = seed_write_surface(tmp_path)
    pending = _write(instance, _set(f"{KIND}/wi-1", "title", "land on approve"), accept="never")
    assert pending.proposal is not None
    source = _proposal_result(instance, pending.proposal.proposal_id)
    assert source.candidate is not None
    signed = _sign(owner, source.candidate.candidate_digest, instance.accepted_coordinate().semantic_root)
    assert _write(instance, _set(f"{KIND}/wi-2", "title", "move head")).status == "accepted"
    before = instance.accepted_coordinate()
    result = service_submit_playbill_approval(
        instance,
        proposal_id=source.admission.proposal_id,
        attestation=signed.attestation,
        authenticated_submitter="owner",
        activate_when_complete=not hold,
        may_activate=may_activate,
    )
    assert result.proposal_id != source.admission.proposal_id
    successor = _proposal_result(instance, result.proposal_id)
    assert successor.candidate is not None
    assert instance.proposal_evidence().read_approvals(successor.candidate.candidate_digest) == ()
    assert instance.proposal_evidence().read_approvals(source.candidate.candidate_digest) == ()
    if not hold and may_activate:
        assert isinstance(result, contracts.ActivationReceipt)
        assert result.status == "accepted"
        assert result.accepted_coordinate == contracts.AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    else:
        assert isinstance(result, ProposalRecompiled)
        assert result.status == "ready_to_activate"
        assert instance.accepted_coordinate() == before
