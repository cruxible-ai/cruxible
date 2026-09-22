"""Execution provenance is minted by a terminal, never by an ordinary author."""

from types import SimpleNamespace

import pytest

from cruxible_client.authoring.inputs import CarriedContractInput
from cruxible_client.authoring.source import claim_candidate, halt, procedure, propose_change_set
from cruxible_client.contracts.acquisition_policies import (
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.authoring.models import (
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ClaimDerivationBindingV1,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    capture_contract_digest,
)
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.errors import ProposalAdmissionError
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV2,
    ClaimEvidenceAdmissionRuleV2,
)
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.proposal_models import ProposalAdmissionRequest
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import lower_authoring
from cruxible_core.proposals.proposals import AuthenticatedActor, evaluate_proposal_tree
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from tests.test_authoring.test_authoring_preflight import _self_source_payload
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world
from tests.test_claims.test_claims import _claim_type
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_nested_source_runs import accept_blueprint
from tests.test_procedures.test_procedure_execution import _budget, _hard_caps
from tests.test_procedures.test_procedure_proposal_delivery import run_line
from tests.test_procedures.test_procedure_source_runs import (
    _accept_more,
    _line_mandate,
    _policy,
    _served_line,
)

TIMESTAMP = "2026-09-12T12:00:00.000000Z"
REFUSAL = "playbill.authoring.derivation_requires_execution"


@pytest.fixture
def world(tmp_path):
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    definition = _claim_type().model_copy(
        update={
            "artifact_format": "playbill-claim-type-v5",
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.ready"),
            "predicate": "project.work_item.ready",
            "literal_schema": {"type": "boolean"},
            "permitted_roles": ("derivation",),
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV2(
                rules=(
                    ClaimEvidenceAdmissionRuleV2(
                        rule_id="derived",
                        claim_roles=("derivation",),
                        capture_contract_digests=(
                            capture_contract_digest(
                                COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
                            ).tagged,
                        ),
                        evidence_kinds=("self_asserted",),
                        admission="derivational",
                        subject_binding="exact_claim_subject",
                    ),
                )
            ),
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[claim_type_path(definition.predicate)] = render_claim_type(definition)
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="derived-type")
    Request = CarriedContractInput(name="derive.request", fields={})
    Result = CarriedContractInput(
        name="derive.result", fields={"ready": PropertySchema(type="bool")}
    )

    @procedure(
        name="derive-ready",
        input=Request,
        output=Result,
        budget=_budget().model_copy(update={"max_items": None}),
        hard_caps=_hard_caps(),
        terminal_capability=2,
    )
    def derive(request, world):
        basis = world.project.work_item["wi-42"].status.one()
        if basis.value != "ready":
            return halt("The work item is not ready.")
        candidate = claim_candidate(
            subject=world.project.work_item["wi-42"],
            predicate=world.claim_type("project.work_item.ready"),
            value=True,
            role="derivation",
            rationale="Readiness follows from the accepted status.",
            self_source="Computed from the accepted status.",
            basis=(basis,),
        )
        return propose_change_set(candidates=[candidate], result=Result.value(ready=True))

    accept_blueprint(instance, owner, derive)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path(derive.name)
    reducer = parse_procedure(tree[path], path=path)
    basis = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    template = _self_source_payload()
    payload = ClaimAuthoringPayloadV3(
        statement=template.statement.model_copy(
            update={
                "predicate": definition.predicate,
                "object": LiteralClaimObject(value=True),
                "role": "derivation",
            }
        ),
        rationale="Attempt to attribute a computation to a Procedure that never ran.",
        source=template.source,
        dependency_drafts=ClaimDependencyDraftsV1(),
        derivation=ClaimDerivationBindingV1(
            procedure=ArtifactPin(
                role="reducer",
                target=reducer.identity,
                artifact_digest=procedure_artifact_digest(reducer).tagged,
            ),
            inputs=(
                ArtifactPin(
                    role="input-claim",
                    target=basis.identity,
                    artifact_digest=claim_artifact_digest(basis).tagged,
                ),
            ),
        ),
    )
    return SimpleNamespace(
        instance=instance,
        owner=owner,
        reducer=reducer,
        payload=payload,
        actor=AuthenticatedActor(actor_id="owner"),
        coordinator=AuthoringIntentCoordinator.for_instance(instance),
        root=tmp_path / "workspace",
    )


@pytest.mark.parametrize("change_set", [False, True])
@pytest.mark.parametrize("carry_binding", [False, True])
def test_generic_authoring_refuses_derivation_before_lowering(world, change_set, carry_binding):
    payload = world.payload
    if not carry_binding:
        payload = payload.model_copy(update={"derivation": None})
    if change_set:
        payload = ChangeSetAuthoringPayloadV1(members=(payload,), rationale="Raw derivation.")
    before = world.instance.accepted_coordinate()
    admissions = world.instance.proposal_evidence().list_admissions()
    result = world.coordinator.compile(
        actor=world.actor, payload=payload, canonical_timestamp=TIMESTAMP
    )
    assert result.verdict == "refused"
    assert REFUSAL in {diagnostic.code for diagnostic in result.frontier.diagnostics}
    assert world.instance.accepted_coordinate() == before
    assert world.instance.proposal_evidence().list_admissions() == admissions


def _forged_candidate(world, payload):
    intent = world.coordinator.create(
        actor=world.actor, payload=payload, canonical_timestamp=TIMESTAMP
    ).intent
    # Manufacture bytes using the internal lowerer to model a raw Git/HTTP author.
    # This is deliberately NOT the terminal, and confers no submission authority.
    return lower_authoring(
        world.instance,
        intent=intent,
        actor_id=world.actor.actor_id,
        derivation_procedure=world.payload.derivation.procedure,
    ).proposed_tree


def test_raw_proposal_cannot_forge_terminal_authority(world):
    instance = world.instance
    coordinate = instance.accepted_coordinate()
    candidate = _forged_candidate(world, world.payload)
    base = instance.tree_at(coordinate.git_oid)
    # Frozen content laws still verify old records: execution authority is a live
    # admission boundary, independently enforced after evaluation/cached handoff.
    evaluated = evaluate_proposal_tree(
        base_tree=base,
        current_tree=base,
        proposed_tree=candidate,
        current=coordinate,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        actor_id="owner",
        rebased=False,
    )
    assert evaluated.candidate is not None, evaluated.diagnostics
    service = instance.proposal_service()
    request = ProposalAdmissionRequest(
        target_ref="refs/proposals/owner/procedure-" + "f" * 64,
        proposed_base_oid=coordinate.git_oid,
        source_compilation_digest="sha256:" + "f" * 64,
    )
    before = instance.proposal_evidence().list_admissions()
    (derived_path,) = [
        path
        for path in candidate
        if path.startswith("claims/") and base.get(path) != candidate[path]
    ]
    for prepared in (None, SimpleNamespace(take=lambda **kwargs: evaluated)):
        for authorized in (
            None,
            {"claims/another.json": candidate[derived_path]},
            {derived_path: b"different output"},
        ):
            with pytest.raises(ProposalAdmissionError, match=REFUSAL):
                service.submit(
                    actor=world.actor,
                    request=request,
                    candidate_tree=candidate,
                    timestamp=TIMESTAMP,
                    authorize=lambda coordinate, tree: authorized,
                    prepared=prepared,
                )
    assert instance.proposal_ref_target(request.target_ref) is None
    assert instance.proposal_evidence().list_admissions() == before
    assert instance.accepted_coordinate() == coordinate


def _install_line(world):
    policy = _policy()
    line = _served_line(world.reducer, policy).model_copy(update={"requested_terminal_rung": 2})
    mandate = _line_mandate(world.reducer)
    _accept_more(
        world.instance,
        world.owner,
        {
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="derive-authority",
    )
    return line


def _run_and_accept(world):
    from tests.test_ledger.test_active_review_publication import _settle

    run = run_line(world.instance, world.root, _install_line(world))
    assert run.status == "succeeded", run.model_dump_json()
    assert run.result == {"ready": True}
    (egress,) = run.terminal_egress
    assert egress.verdict == "delivered"
    proposal = service_inspect_playbill_proposal(world.instance, proposal_id=egress.proposal_id)
    _settle(world.instance, world.owner, proposal.proposal, "activation")
    tree = world.instance.tree_at(world.instance.accepted_coordinate().git_oid)
    return next(
        parse_claim(content, path=path)
        for path, content in tree.items()
        if path.startswith("claims/")
        and path.endswith(".json")
        and parse_claim(content, path=path).statement.role == "derivation"
    )


def test_real_execution_accepts_but_manual_revision_cannot_reuse_its_provenance(world):
    derived = _run_and_accept(world)
    assert derived.backing.reducer_digest == world.payload.derivation.procedure.artifact_digest
    assert derived.backing.input_claim_digests == tuple(
        pin.artifact_digest for pin in world.payload.derivation.inputs
    )
    revised = world.payload.model_copy(
        update={
            "claim_ref": derived.identity.name,
            "statement": world.payload.statement.model_copy(
                update={"object": LiteralClaimObject(value=False)}
            ),
        }
    )
    for payload in (revised, revised.model_copy(update={"derivation": None})):
        result = world.coordinator.compile(
            actor=world.actor, payload=payload, canonical_timestamp=TIMESTAMP
        )
        assert result.verdict == "refused"
        assert REFUSAL in {diagnostic.code for diagnostic in result.frontier.diagnostics}
    instance = world.instance
    with pytest.raises(ProposalAdmissionError, match=REFUSAL):
        instance.proposal_service().submit(
            actor=world.actor,
            request=ProposalAdmissionRequest(
                target_ref="refs/proposals/owner/forged-revision",
                proposed_base_oid=instance.accepted_coordinate().git_oid,
            ),
            candidate_tree=_forged_candidate(world, revised),
            timestamp=TIMESTAMP,
        )
    # Withdrawing a derived assertion is still ordinary governed authoring; it
    # preserves the original computation rather than claiming a different output.
    from cruxible_core.claims.claim_retirement import service_retire_claim
    from tests.test_claims.test_claim_retirement import _activate, _request

    retirement = service_retire_claim(
        instance,
        claim_id=derived.identity.name,
        request=_request(instance, mode="submit"),
        actor=world.actor,
    )
    _activate(instance, world.owner, retirement)
    path = claim_path(derived.identity.name)
    retired = parse_claim(instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path)
    assert retired.lifecycle.state == "retired"
    assert retired.backing == derived.backing


@pytest.mark.parametrize("after_submit", [False, True])
def test_derived_proposal_recovers_from_retained_execution_after_restart(
    world, monkeypatch, after_submit
):
    from datetime import timedelta

    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.procedures.procedure_runs import service_get_playbill_procedure_run
    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress
    from tests.test_ledger.test_active_review_publication import _settle
    from tests.test_procedures.test_procedure_proposal_delivery import (
        NOW,
        _Crash,
        _crash_after,
        _proposal_refs,
    )

    line = _install_line(world)
    _crash_after(monkeypatch, after_submit=after_submit)
    with pytest.raises(_Crash):
        run_line(world.instance, world.root, line)
    monkeypatch.undo()
    instance = PlaybillInstance.open(world.instance.root, trust_root=world.instance.trust_root)
    recovered = service_recover_proposal_egress(instance, recorded_at=NOW + timedelta(minutes=1))
    ((run_id, disposition),) = recovered.items()
    assert disposition == "delivered"
    assert len(_proposal_refs(instance)) == 1
    (egress,) = service_get_playbill_procedure_run(instance, run_id=run_id).terminal_egress
    proposal = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    _settle(instance, world.owner, proposal.proposal, "activation")
    path = egress.children[0].path
    claim = parse_claim(instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path)
    assert claim.backing.reducer_digest == world.payload.derivation.procedure.artifact_digest
    assert service_recover_proposal_egress(instance, recorded_at=NOW + timedelta(minutes=2)) == {}
