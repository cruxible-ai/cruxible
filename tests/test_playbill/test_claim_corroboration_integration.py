"""Proposal, settlement, and recovery integration for Claim corroboration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    build_direct_claim_capture,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import ClaimArtifactV2, claim_path, render_claim
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    CorroborationRequirementV1,
)
from cruxible_client.contracts.proposal_models import ProposalResult
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryEntryV1,
    QueryParameterDeclarationV1,
    QueryParameterRefV1,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.settlement import ChangeActorBinding
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim, _claim_type, _subject
from tests.test_playbill.test_resolution_contracts import _accept_tree

SEED_TIMESTAMP = "2026-08-28T12:00:00.000000Z"
PROPOSAL_TIMESTAMP = "2026-08-28T12:01:00.000000Z"
OBSERVED_AT = datetime(2026, 8, 28, 11, 59, tzinfo=timezone.utc)


def _query(*, missing_parameter: bool = False) -> QueryDefinitionV1:
    parameters = [
        QueryParameterDeclarationV1(name="claim_subject_id", value_type="string"),
    ]
    if missing_parameter:
        parameters.append(QueryParameterDeclarationV1(name="required_extra", value_type="string"))
    return QueryDefinitionV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name="project.subject_exists"),
        entry=QueryEntryV1(
            binding="item",
            subject_kinds=("project.work_item",),
            subject_id=QueryParameterRefV1(parameter="claim_subject_id"),
        ),
        result_binding="item",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        parameters=tuple(parameters),
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="refuse_on_conflict",
        ),
        default_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
    )


def _corroborated_type(
    query_digest: str,
    *,
    predicate: str = "project.work_item.status",
    min_count: int = 1,
    predecessor_digest: str | None = None,
) -> ClaimType:
    base = _claim_type()
    return base.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
            "predicate": predicate,
            "admission_policy": ClaimAdmissionPolicyV1(
                corroboration_requirements=(
                    CorroborationRequirementV1(
                        requirement_id="subject-exists",
                        query_definition_digest=query_digest,
                        min_count=min_count,
                    ),
                )
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=predecessor_digest),
        }
    )


def _seed_vocabulary(
    instance: PlaybillInstance,
    owner: object,
    *,
    claim_types: tuple[ClaimType, ...],
    query: QueryDefinitionV1 | None,
    proposal_name: str = "corroboration-vocabulary",
    timestamp: str = SEED_TIMESTAMP,
) -> None:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name)] = (
        render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
    )
    subject = _subject()
    from cruxible_client.contracts.subjects import render_subject, subject_path

    tree[subject_path(subject.subject_kind, subject.subject_id)] = render_subject(subject)
    for claim_type in claim_types:
        tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    if query is not None:
        tree[query_definition_path(query.identity.name)] = render_query_definition(query)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=timestamp,
        proposal_name=proposal_name,
    )


def _claim_for_type(
    instance: PlaybillInstance,
    claim_type: ClaimType,
    *,
    claim_id: str,
) -> ClaimArtifactV2:
    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="The governed work item is ready.",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    length = capture.envelope.commitment.byte_length
    assert length is not None
    claim = _claim(
        claim_id=claim_id,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=length,
    )
    type_digest = claim_type_digest(claim_type).tagged
    return claim.model_copy(
        update={
            "statement": claim.statement.model_copy(
                update={
                    "claim_type": claim_type.identity,
                    "claim_type_digest": type_digest,
                    "predicate": claim_type.predicate,
                }
            ),
            "pins": tuple(
                pin.model_copy(
                    update={"target": claim_type.identity, "artifact_digest": type_digest}
                )
                if pin.role == "claim-type"
                else pin
                for pin in claim.pins
            ),
        }
    )


def _submit_claim(
    instance: PlaybillInstance,
    claim: ClaimArtifactV2,
    *,
    proposal_name: str,
    extra_tree: dict[str, bytes] | None = None,
) -> tuple[ProposalResult, dict[str, bytes]]:
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree[claim_path(claim.identity.name)] = render_claim(claim)
    tree.update(extra_tree or {})
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{proposal_name}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=PROPOSAL_TIMESTAMP,
    )
    return result, tree


def _activate(instance: PlaybillInstance, result: ProposalResult, tree: dict[str, bytes]) -> None:
    candidate = result.candidate
    assert candidate is not None
    base = instance.accepted_coordinate()
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=len(instance.accepted_history()),
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"


def test_proposal_pass_persists_only_the_authored_type_account_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, owner = initialize_local(tmp_path)
    query = _query()
    query_digest = query_definition_digest(query).tagged
    status_type = _corroborated_type(query_digest)
    summary_type = _corroborated_type(query_digest, predicate="project.work_item.summary")
    _seed_vocabulary(
        instance,
        owner,
        claim_types=(summary_type, status_type),
        query=query,
    )
    claim = _claim_for_type(instance, status_type, claim_id="CLM-" + "a1" * 16)

    import cruxible_core.service.playbill_query as query_service

    original = query_service.build_accepted_query_facts
    calls: list[str] = []

    def observed_builder(source, *, coordinate, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(coordinate.git_oid)
        return original(source, coordinate=coordinate, **kwargs)

    monkeypatch.setattr(query_service, "build_accepted_query_facts", observed_builder)
    result, tree = _submit_claim(instance, claim, proposal_name="corroboration-pass")

    assert result.evaluation.verdict == "candidate"
    assert [item.claim_type_identity for item in result.evaluation.claim_admission_accounts] == [
        "ClaimType:project.work_item.status"
    ]
    persisted = instance.proposal_evidence().read_evaluation(result.admission.proposal_id)
    assert persisted.claim_admission_accounts == result.evaluation.claim_admission_accounts
    calls.clear()
    _activate(instance, result, tree)
    assert calls == [instance.accepted_coordinate().git_oid]
    calls.clear()
    instance.refresh()
    assert calls == [instance.accepted_history()[-2].oid]


def test_unsatisfiable_type_does_not_gate_another_type_and_can_retire(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    query = _query()
    query_digest = query_definition_digest(query).tagged
    authored_type = _claim_type()
    blocker_type = _corroborated_type(
        query_digest,
        predicate="project.work_item.corrob99",
        min_count=99,
    )
    _seed_vocabulary(
        instance,
        owner,
        claim_types=(authored_type, blocker_type),
        query=query,
    )
    claim = _claim_for_type(instance, authored_type, claim_id="CLM-" + "99" * 16)
    retired_blocker = blocker_type.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=claim_type_digest(blocker_type).tagged,
            )
        }
    )

    result, _tree = _submit_claim(
        instance,
        claim,
        proposal_name="unrelated-type-and-retirement",
        extra_tree={claim_type_path(retired_blocker.predicate): render_claim_type(retired_blocker)},
    )

    assert result.evaluation.verdict == "candidate"
    assert result.evaluation.diagnostics == ()
    assert result.evaluation.claim_admission_accounts == ()


def test_insufficient_refusal_persists_its_account(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    query = _query()
    claim_type = _corroborated_type(query_definition_digest(query).tagged, min_count=2)
    _seed_vocabulary(instance, owner, claim_types=(claim_type,), query=query)
    claim = _claim_for_type(instance, claim_type, claim_id="CLM-" + "a2" * 16)

    result, _tree = _submit_claim(instance, claim, proposal_name="corroboration-insufficient")

    assert result.evaluation.verdict == "refused"
    assert [item.code for item in result.evaluation.diagnostics] == [
        "playbill.claim.corroboration_insufficient"
    ]
    account = result.evaluation.claim_admission_accounts[0]
    assert account.corroboration_results[0].observed_count == 1
    assert account.satisfied is False
    assert instance.proposal_evidence().read_evaluation(
        result.admission.proposal_id
    ).claim_admission_accounts == (account,)


def test_query_refusal_is_persisted_through_the_real_proposal_surface(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    query = _query(missing_parameter=True)
    claim_type = _corroborated_type(query_definition_digest(query).tagged)
    _seed_vocabulary(instance, owner, claim_types=(claim_type,), query=query)
    claim = _claim_for_type(instance, claim_type, claim_id="CLM-" + "a3" * 16)

    result, _tree = _submit_claim(instance, claim, proposal_name="corroboration-query-refused")

    assert result.evaluation.verdict == "refused"
    assert [item.code for item in result.evaluation.diagnostics] == [
        "playbill.claim.corroboration_query_refused"
    ]
    corroboration = result.evaluation.claim_admission_accounts[0].corroboration_results[0]
    assert corroboration.query_verdict == "refused"
    assert corroboration.query_refusal_code == "playbill.query.parameter_missing"


def test_same_candidate_query_definition_cannot_corroborate_its_claim(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    query = _query()
    claim_type = _corroborated_type(query_definition_digest(query).tagged)
    _seed_vocabulary(instance, owner, claim_types=(claim_type,), query=None)
    claim = _claim_for_type(instance, claim_type, claim_id="CLM-" + "a4" * 16)

    result, _tree = _submit_claim(
        instance,
        claim,
        proposal_name="corroboration-same-candidate",
        extra_tree={query_definition_path(query.identity.name): render_query_definition(query)},
    )

    assert result.evaluation.verdict == "refused"
    assert [item.code for item in result.evaluation.diagnostics] == [
        "playbill.claim.corroboration_query_unresolved"
    ]
    assert result.evaluation.claim_admission_accounts[0].corroboration_results == ()


def test_retired_query_definition_does_not_resolve_for_corroboration(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    live_query = _query()
    live_type = _corroborated_type(query_definition_digest(live_query).tagged)
    _seed_vocabulary(instance, owner, claim_types=(live_type,), query=live_query)
    retired_query = live_query.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=query_definition_digest(live_query).tagged,
            )
        }
    )
    retired_type = _corroborated_type(
        query_definition_digest(retired_query).tagged,
        predecessor_digest=claim_type_digest(live_type).tagged,
    )
    _seed_vocabulary(
        instance,
        owner,
        claim_types=(retired_type,),
        query=retired_query,
        proposal_name="retire-corroboration-query",
        timestamp="2026-08-28T12:00:30.000000Z",
    )
    claim = _claim_for_type(instance, retired_type, claim_id="CLM-" + "a5" * 16)

    result, _tree = _submit_claim(instance, claim, proposal_name="corroboration-retired")

    assert result.evaluation.verdict == "refused"
    assert [item.code for item in result.evaluation.diagnostics] == [
        "playbill.claim.corroboration_query_unresolved"
    ]


def test_settlement_refuses_typed_when_rederived_account_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, owner = initialize_local(tmp_path)
    query = _query()
    claim_type = _corroborated_type(query_definition_digest(query).tagged)
    _seed_vocabulary(instance, owner, claim_types=(claim_type,), query=query)
    claim = _claim_for_type(instance, claim_type, claim_id="CLM-" + "a6" * 16)
    result, tree = _submit_claim(instance, claim, proposal_name="corroboration-rederive")

    import cruxible_core.service.playbill_query as query_service

    original = query_service.build_accepted_query_facts

    def empty_subjects(source, *, coordinate, **kwargs):  # type: ignore[no-untyped-def]
        facts = original(source, coordinate=coordinate, **kwargs)
        return facts.model_copy(update={"subjects": ()})

    monkeypatch.setattr(query_service, "build_accepted_query_facts", empty_subjects)

    with pytest.raises(ProposalIntegrityError, match="accepted query re-derivation"):
        _activate(instance, result, tree)
