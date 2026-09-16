"""A compiler transition is approved state and survives loss of every projection."""

import pytest

from cruxible_client.contracts.compiler_upgrade import COMPILER_UPGRADE_PATH
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.compiler import (
    ATTESTATION_COMPILER,
    ONTOLOGY_COMPILER,
    PC_HR_COMPILER,
    PROVIDER_CONTRACT_COMPILER,
    RESOLUTION_COMPILER,
    UPGRADE_COMPILER,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    PlaybillAcceptedCoordinate,
    service_activate_playbill_proposal,
    service_propose_compiler_upgrade,
    service_submit_playbill_approval,
)
from tests.test_ledger.test_activation import TIMESTAMP, _instance, _sign


def old_instance(tmp_path, monkeypatch, compiler=RESOLUTION_COMPILER):
    with monkeypatch.context() as patch:
        patch.setattr(
            "cruxible_core.runtime.instance.current_compiler_coordinate",
            lambda: compiler,
        )
        return _instance(tmp_path)


def propose(instance, target=UPGRADE_COMPILER):
    return service_propose_compiler_upgrade(
        instance,
        target=target,
        actor_id="owner",
        proposal_name="upgrade",
        timestamp=TIMESTAMP,
        base=PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    ).proposal


def approve(instance, proposal, reviewer):
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    candidate = proposal.candidate
    signature = _sign(
        reviewer, candidate.candidate_digest, candidate.candidate.parent_semantic_root
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=signature.attestation,
        authenticated_submitter="reviewer",
    )


@pytest.mark.parametrize(
    "source,target",
    [
        (source, UPGRADE_COMPILER)
        for source in [PC_HR_COMPILER, ATTESTATION_COMPILER, RESOLUTION_COMPILER, ONTOLOGY_COMPILER]
    ]
    + [
        (UPGRADE_COMPILER, PROVIDER_CONTRACT_COMPILER),
        (RESOLUTION_COMPILER, PROVIDER_CONTRACT_COMPILER),
    ],
)
def test_upgrade_preserves_historical_coordinates_and_reopens(
    tmp_path, monkeypatch, source, target
):
    instance, owner, reviewer = old_instance(tmp_path, monkeypatch, source)
    before = instance.accepted_coordinate()
    proposal = propose(instance, target)
    approve(instance, proposal, reviewer)
    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=proposal.admission.proposal_id,
        activated_by="owner",
    )
    assert receipt.status == "accepted"
    assert instance.accepted_coordinate().compiler == target
    assert instance.coordinate_for_oid(before.git_oid) == before
    assert instance.descriptor.compiler == source
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
    assert reopened.coordinate_for_oid(before.git_oid) == before
    assert reopened.inspect().compiler == target
    with reopened._history_reader_for_epoch(reopened._recovered) as reader:
        assert (
            reader.resolve(PlaybillAcceptedCoordinate.from_internal(before)).compiler_digest
            == source.rule_digest
        )


@pytest.mark.parametrize(
    "target", [RESOLUTION_COMPILER, CompilerCoordinate(rule_digest="sha256:" + "ff" * 32)]
)
def test_unsupported_transition_does_not_change_state(tmp_path, monkeypatch, target):
    instance, _, _ = old_instance(tmp_path, monkeypatch)
    before = instance.accepted_coordinate()
    with pytest.raises(ValueError, match="unsupported compiler transition"):
        propose(instance, target)
    assert instance.accepted_coordinate() == before


def test_upgrade_requires_signed_approval_and_admin_activation(tmp_path, monkeypatch):
    from cruxible_client.contracts.errors import SettlementIntegrityError
    from cruxible_core.errors import PermissionDeniedError
    from cruxible_core.runtime.permissions import reset_permissions

    instance, _, reviewer = old_instance(tmp_path, monkeypatch)
    before = instance.accepted_coordinate()
    proposal = propose(instance)
    with pytest.raises(SettlementIntegrityError, match="signed client approval"):
        service_activate_playbill_proposal(
            instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
        )
    approve(instance, proposal, reviewer)
    with monkeypatch.context() as patch:
        patch.setenv("CRUXIBLE_MODE", "graph_write")
        reset_permissions()
        with pytest.raises(PermissionDeniedError):
            service_activate_playbill_proposal(
                instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
            )
    reset_permissions()
    assert instance.accepted_coordinate() == before


@pytest.mark.parametrize(
    "point",
    ["before:main.cas", "after:main.cas", "before:generation.note", "before:serving.publication"],
)
def test_upgrade_recovers_publication_crashes(tmp_path, monkeypatch, point):
    from cruxible_core.ledger.activation import ActivationPublisher

    instance, _, reviewer = old_instance(tmp_path, monkeypatch)
    before = instance.accepted_coordinate()
    proposal = propose(instance)
    approve(instance, proposal, reviewer)
    activate = ActivationPublisher.activate

    class Crash(BaseException):
        pass

    def crashing(self, *args, **kwargs):
        def hook(checkpoint):
            if checkpoint == point:
                raise Crash(point)

        return activate(self, *args, **{**kwargs, "crash_hook": hook})

    with monkeypatch.context() as patch:
        patch.setattr(ActivationPublisher, "activate", crashing)
        with pytest.raises(Crash):
            service_activate_playbill_proposal(
                instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
            )
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    expected = RESOLUTION_COMPILER if point == "before:main.cas" else UPGRADE_COMPILER
    assert reopened.accepted_coordinate().compiler == expected
    assert reopened.coordinate_for_oid(before.git_oid) == before


def test_upgrade_rebuild_and_new_query_preserve_old_receipts(tmp_path, monkeypatch):
    import shutil

    from cruxible_client.contracts.query.definitions import (
        query_definition_path,
        render_query_definition,
    )
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from cruxible_core.proposals.settlement import ChangeActorBinding
    from tests.test_ledger.test_activation import _candidate
    from tests.test_query.test_artifact_queries import definition, run

    instance, _, reviewer = old_instance(tmp_path, monkeypatch)
    base, tree, candidate = _candidate(instance)
    approved = _sign(reviewer, candidate.candidate_digest, base.semantic_root)
    result = instance.settle_and_activate(
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approvals=(approved,),
        actor_binding=ChangeActorBinding(actor_id="owner", source_compilation_digest=None),
        proposal_actor_id="owner",
    )
    old = result.accepted
    assert old is not None
    old_record = instance.accepted_history()[-1].record
    proposal = propose(instance)
    approve(instance, proposal, reviewer)
    service_activate_playbill_proposal(
        instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
    )
    query = definition()
    tree = dict(instance.tree_at(instance.accepted_coordinate().git_oid))
    tree[query_definition_path(query.identity.name)] = render_query_definition(query)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/new-query",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    approve(instance, result, reviewer)
    service_activate_playbill_proposal(
        instance, proposal_id=result.admission.proposal_id, activated_by="owner"
    )
    run(instance, query)
    before = instance.accepted_coordinate()
    # Both verified-checkpoint and genesis-rooted recovery must agree.
    assert (
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root).accepted_coordinate()
        == before
    )
    shutil.rmtree(instance.root / "projections")
    (instance.root / "projections").mkdir()
    shutil.rmtree(instance._checkpoint_directory(instance.root))
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == before
    assert reopened.coordinate_for_oid(old.git_oid) == old
    assert reopened.accepted_history()[1].record == old_record
    run(reopened, query)


def test_changed_base_and_mixed_proposal_refuse_upgrade(tmp_path, monkeypatch):
    from cruxible_client.contracts.compiler_upgrade import (
        parse_compiler_upgrade,
        render_compiler_upgrade,
    )
    from cruxible_core.proposals.proposals import evaluate_proposal_tree
    from tests.test_ledger.test_activation import _candidate

    instance, _, _ = old_instance(tmp_path, monkeypatch)
    original = instance.accepted_coordinate()
    proposal = propose(instance)
    tree = instance.proposal_tree(proposal.evaluation.evaluated_tree_oid)
    transition = parse_compiler_upgrade(tree[COMPILER_UPGRADE_PATH])
    stale = transition.model_copy(
        update={"base": transition.base.model_copy(update={"git_oid": "a" * len(original.git_oid)})}
    )
    _, mixed_tree, _ = _candidate(instance)
    for candidate_tree in (
        {**tree, COMPILER_UPGRADE_PATH: render_compiler_upgrade(stale)},
        {**mixed_tree, COMPILER_UPGRADE_PATH: tree[COMPILER_UPGRADE_PATH]},
    ):
        result = evaluate_proposal_tree(
            base_tree=instance.tree_at(original.git_oid),
            current_tree=instance.tree_at(original.git_oid),
            proposed_tree=candidate_tree,
            current=original,
            bodies=instance.body_store(),
            timestamp=TIMESTAMP,
            rebased=False,
            actor_id="owner",
        )
        assert result.candidate is None
        assert any(d.code == "playbill.compiler_upgrade.invalid" for d in result.diagnostics)


def test_upgrade_stale_candidate_cannot_cross_another_acceptance(tmp_path, monkeypatch):
    from cruxible_client.contracts.errors import SettlementIntegrityError
    from cruxible_core.proposals.settlement import ChangeActorBinding
    from tests.test_ledger.test_activation import _candidate

    instance, _, reviewer = old_instance(tmp_path, monkeypatch)
    proposal = propose(instance)
    approve(instance, proposal, reviewer)
    base, tree, candidate = _candidate(instance)
    instance.settle_and_activate(
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approvals=(_sign(reviewer, candidate.candidate_digest, base.semantic_root),),
        actor_binding=ChangeActorBinding(actor_id="owner", source_compilation_digest=None),
        proposal_actor_id="owner",
    )
    head = instance.accepted_coordinate()
    with pytest.raises(SettlementIntegrityError, match="not the current main ref"):
        service_activate_playbill_proposal(
            instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
        )
    assert instance.accepted_coordinate() == head
    assert (
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root).accepted_coordinate()
        == head
    )


def test_failed_target_prebuild_leaves_original_compiler_and_serving(tmp_path, monkeypatch):
    from cruxible_client.contracts.errors import ProjectionFormatError
    from cruxible_core.compiler.assembler import ProjectionAssembler

    instance, _, reviewer = old_instance(tmp_path, monkeypatch)
    before = instance.accepted_coordinate()
    proposal = propose(instance)
    approve(instance, proposal, reviewer)
    assemble = ProjectionAssembler.assemble

    def refuse_target(self, request, **kwargs):
        if request.compiler_digest == UPGRADE_COMPILER.rule_digest:
            assert kwargs.get("delta") is None
            raise ProjectionFormatError("target cannot interpret accepted state")
        return assemble(self, request, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ProjectionAssembler, "assemble", refuse_target)
        with pytest.raises(ProjectionFormatError, match="target cannot"):
            service_activate_playbill_proposal(
                instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
            )
    assert instance.accepted_coordinate() == before
    assert (
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root).accepted_coordinate()
        == before
    )


def test_decommissioned_instance_cannot_propose_upgrade(tmp_path, monkeypatch):
    from cruxible_client.contracts.errors import PlaybillInstanceDecommissioned

    instance, _, _ = old_instance(tmp_path, monkeypatch)
    instance.decommission(reason="retired", decommissioned_by="owner")
    with pytest.raises(PlaybillInstanceDecommissioned):
        propose(instance)


def test_old_query_receipt_is_identical_after_upgrade_and_rebuild(tmp_path, monkeypatch):
    import shutil

    from cruxible_core.service.discovery.query import service_run_playbill_query
    from tests.core_support._knowledge_loop_support import QUERY_NAME
    from tests.test_query.test_query_execution_service import READ_TIME, _instance_with_query

    with monkeypatch.context() as patch:
        patch.setattr(
            "cruxible_core.runtime.instance.current_compiler_coordinate",
            lambda: RESOLUTION_COMPILER,
        )
        instance, owner = _instance_with_query(tmp_path)
    old = service_run_playbill_query(instance, name=QUERY_NAME, evaluation_time=READ_TIME)
    at = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    proposal = propose(instance)
    candidate = proposal.candidate
    signature = _sign(owner, candidate.candidate_digest, candidate.candidate.parent_semantic_root)
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=signature.attestation,
        authenticated_submitter="owner",
    )
    service_activate_playbill_proposal(
        instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
    )
    shutil.rmtree(instance.root / "projections")
    (instance.root / "projections").mkdir()
    shutil.rmtree(instance._checkpoint_directory(instance.root))
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    replay = service_run_playbill_query(reopened, name=QUERY_NAME, evaluation_time=READ_TIME, at=at)
    assert replay.receipt.model_dump_json() == old.receipt.model_dump_json()
    assert replay.result == old.result


@pytest.mark.parametrize("field", ["target", "base"])
def test_approved_transition_cannot_be_retargeted(tmp_path, monkeypatch, field):
    from cruxible_client.contracts.compiler_upgrade import (
        parse_compiler_upgrade,
        render_compiler_upgrade,
    )
    from cruxible_client.contracts.errors import SettlementIntegrityError
    from cruxible_core.proposals.settlement import ChangeActorBinding

    instance, _, reviewer = old_instance(tmp_path, monkeypatch)
    base = instance.accepted_coordinate()
    proposal = propose(instance)
    candidate = proposal.candidate
    tree = dict(instance.proposal_tree(proposal.evaluation.evaluated_tree_oid))
    transition = parse_compiler_upgrade(tree[COMPILER_UPGRADE_PATH])
    changed = (
        RESOLUTION_COMPILER
        if field == "target"
        else transition.base.model_copy(update={"semantic_root": "sha256:" + "ff" * 32})
    )
    tree[COMPILER_UPGRADE_PATH] = render_compiler_upgrade(
        transition.model_copy(update={field: changed})
    )
    with pytest.raises(SettlementIntegrityError, match="candidate"):
        instance.prepare_generation(
            base=base,
            candidate_tree=tree,
            candidate=candidate,
            approvals=(_sign(reviewer, candidate.candidate_digest, base.semantic_root),),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            proposal_actor_id="owner",
            sequence=1,
        )
    assert instance.accepted_coordinate() == base


def test_prebuilt_upgrade_loses_cas_to_an_ordinary_writer(tmp_path, monkeypatch):
    from cruxible_core.ledger.activation import ActivationPublisher
    from cruxible_core.proposals.settlement import ChangeActorBinding
    from tests.test_ledger.test_activation import _candidate

    instance, _, reviewer = old_instance(tmp_path, monkeypatch)
    other = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    proposal = propose(instance)
    approve(instance, proposal, reviewer)
    base, tree, candidate = _candidate(other)
    activate = ActivationPublisher.activate

    def competing_writer(self, bundle, projection, **kwargs):
        if bundle.record.members[0].artifact_kind == "compiler-upgrade":
            other.settle_and_activate(
                base=base,
                candidate_tree=tree,
                candidate=candidate,
                approvals=(_sign(reviewer, candidate.candidate_digest, base.semantic_root),),
                actor_binding=ChangeActorBinding(actor_id="owner"),
                proposal_actor_id="owner",
            )
        return activate(self, bundle, projection, **kwargs)

    monkeypatch.setattr(ActivationPublisher, "activate", competing_writer)
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "lost_cas"
    assert instance.accepted_coordinate() == other.accepted_coordinate()
    assert instance.accepted_coordinate().compiler == RESOLUTION_COMPILER
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        assert projection.accepted == other.accepted_coordinate()
