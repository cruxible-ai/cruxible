"""A settle mandate replaces candidate approval only when it covers and its condition holds."""

from __future__ import annotations

from datetime import UTC, datetime

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.claim_types import claim_type_digest, claim_type_path
from cruxible_client.contracts.claims import ClaimType
from cruxible_client.contracts.governance import INDEPENDENT_APPROVAL_REQUIREMENTS
from cruxible_client.contracts.procedure_mandates import (
    MandateClaimScopeV1,
    MandateConditionV1,
    ProcedureMandateV2,
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryComparisonFilterV1,
    QueryEntryV1,
    QueryLiteralRefV1,
    QueryParameterDeclarationV1,
    QueryParameterRefV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
    QuerySubjectFieldRefV1,
)
from cruxible_core.authoring import lowering
from cruxible_core.proposals import proposals
from cruxible_core.proposals.proposals import AuthenticatedActor, evaluate_proposal_tree
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_change_set_intents import (
    SUBJECT_KIND,
    TIMESTAMP,
    _claim,
    _coordinator,
)
from tests.test_authoring.test_authoring_preflight import _seed_claim_surface
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_procedure_artifacts import _artifact, _definition
from tests.test_procedures.test_procedure_mandates import _caps

STATUS = "project.work_item.status"


def _condition_query(*, only_subject: str | None = None) -> QueryDefinitionV1:
    where = (
        None
        if only_subject is None
        else QueryComparisonFilterV1(
            left=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
            operator="eq",
            right=QueryLiteralRefV1(value=only_subject),
            value_type="string",
        )
    )
    return QueryDefinitionV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name="project.settle-condition"),
        entry=QueryEntryV1(
            binding="item",
            subject_kinds=(SUBJECT_KIND,),
            subject_id=QueryParameterRefV1(parameter="item_id"),
        ),
        where=where,
        result_binding="item",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="id", value=QuerySubjectFieldRefV1(binding="item", field="subject_id")
                ),
            )
        ),
        parameters=(QueryParameterDeclarationV1(name="item_id", value_type="string"),),
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="refuse_on_conflict",
        ),
        default_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
    )


def _world(tmp_path, *, only_subject: str | None = None, change_kinds=("create", "revise")):
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    status_type = ClaimType.model_validate_json(tree[claim_type_path(STATUS)])
    procedure = _artifact(_definition(terminal_capability=3))
    query = _condition_query(only_subject=only_subject)
    mandate = ProcedureMandateV2(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="triage"),
        procedure=ArtifactPin(
            role="procedure",
            target=procedure.identity,
            artifact_digest=procedure_artifact_digest(procedure).tagged,
        ),
        grants="settle",
        resource_ceiling=_caps(),
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        scope=(
            MandateClaimScopeV1(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=status_type.identity,
                    artifact_digest=claim_type_digest(status_type).tagged,
                ),
                change_kinds=change_kinds,
            ),
        ),
        condition=MandateConditionV1(
            query=ArtifactPin(
                role="condition-query",
                target=query.identity,
                artifact_digest=query_definition_digest(query).tagged,
            ),
            binding_parameter="item_id",
            required_fields=("id",),
            fallback="propose",
        ),
    )
    _accept_tree(
        instance,
        owner,
        {
            **tree,
            "procedures/triage.json": render_procedure(procedure),
            query_definition_path(query.identity.name): render_query_definition(query),
            procedure_mandate_path("triage"): render_procedure_mandate(mandate),
        },
        timestamp=TIMESTAMP,
        proposal_name="settle-world",
    )
    return instance, mandate


def _settle(instance, mandate, *, digest: str | None = None, payload=None):
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor, payload=payload or _claim(), canonical_timestamp=TIMESTAMP
    ).intent
    lowered = lowering.lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    current = instance.accepted_coordinate()
    base = instance.tree_at(current.git_oid)
    return evaluate_proposal_tree(
        base_tree=base,
        current_tree=base,
        proposed_tree=lowered.proposed_tree,
        current=current,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id=actor.actor_id,
        query_facts_provider=lambda coordinate: instance._accepted_query_facts(
            instance, coordinate
        ),
        delegated_mandate_digest=digest or procedure_mandate_digest(mandate).tagged,
    )


def _codes(evaluation) -> set[str]:
    return {item.code for item in evaluation.diagnostics}


def test_a_covered_true_condition_settles_without_candidate_approval(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        proposals, "_approval_requirements", lambda _tree: INDEPENDENT_APPROVAL_REQUIREMENTS
    )
    instance, mandate = _world(tmp_path)
    settled = _settle(instance, mandate)
    assert settled.diagnostics == ()
    assert settled.candidate is not None and settled.candidate.approval_requirements == ()


def test_a_false_condition_grants_nothing(tmp_path) -> None:
    instance, mandate = _world(tmp_path, only_subject="someone-else")
    refused = _settle(instance, mandate)
    assert refused.candidate is None
    assert "playbill.settle.condition_false" in _codes(refused)


def test_an_uncovered_change_kind_grants_nothing(tmp_path) -> None:
    instance, mandate = _world(tmp_path, change_kinds=("revise",))
    refused = _settle(instance, mandate)
    assert refused.candidate is None
    assert "playbill.settle.scope_uncovered" in _codes(refused)


def test_an_unknown_mandate_digest_grants_nothing(tmp_path) -> None:
    instance, mandate = _world(tmp_path)
    refused = _settle(instance, mandate, digest="sha256:" + "e" * 64)
    assert refused.candidate is None
    assert _codes(refused) == {"playbill.settle.mandate_unresolved"}


def test_a_settled_generation_publishes_without_approvals_and_replays(
    tmp_path, monkeypatch
) -> None:
    import shutil

    import pytest

    from cruxible_core.proposals.settlement import ChangeActorBinding, SettlementIntegrityError
    from cruxible_core.runtime.instance import PlaybillInstance

    monkeypatch.setattr(
        proposals, "_approval_requirements", lambda _tree: INDEPENDENT_APPROVAL_REQUIREMENTS
    )
    instance, mandate = _world(tmp_path)
    digest = procedure_mandate_digest(mandate).tagged
    settled = _settle(instance, mandate)
    assert settled.candidate is not None
    base = instance.accepted_coordinate()
    publish = dict(
        base=base,
        candidate_tree=dict(settled.tree),
        candidate=settled.candidate,
        approvals=(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
    )
    # Without the mandate, the approval-free candidate cannot be reproduced.
    with pytest.raises(SettlementIntegrityError):
        instance.settle_and_activate(**publish)
    result = instance.settle_and_activate(**publish, mandate_digest=digest)
    assert result.status == "accepted"
    head = instance.accepted_coordinate()
    assert instance.accepted_history()[-1].record.mandate_digest == digest

    # Replay from genesis re-derives the delegated authority from the parent state.
    shutil.rmtree(instance.root / "projections")
    (instance.root / "projections").mkdir()
    shutil.rmtree(instance._checkpoint_directory(instance.root))
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == head
