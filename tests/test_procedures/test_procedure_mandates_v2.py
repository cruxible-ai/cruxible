"""Conditional settle grants: exact pins, fail-closed predicates, governed widening."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedure_mandates import (
    AcceptedProcedureMandateV1,
    MandateClaimScopeV1,
    MandateConditionV1,
    ProcedureMandateError,
    ProcedureMandateV2,
    ScopedClaimTypeV1,
    evaluate_procedure_mandate_v2_law,
    mandate_change_is_narrowing,
    mandate_grant,
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinitionV1,
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
    query_definition_digest,
    query_definition_path,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryClaimPresenceFilterV1,
    QueryClaimValueRefV1,
    QueryComparisonFilterV1,
    QueryConjunctionFilterV1,
    QueryEntryV1,
    QueryLiteralRefV1,
    QueryMembershipFilterV1,
    QueryNegationFilterV1,
    QueryParameterDeclarationV1,
    QueryParameterRefV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
)
from tests.test_procedures.test_procedure_mandates import _caps, _mandate, _procedure

ASSET = "sec.asset"
EXPOSURE = ArtifactIdentity(kind="ClaimType", name="sec.exposure.status")
EXPOSURE_DIGEST = "sha256:" + "1" * 64


def _query(*, where=None, entry_parameter: str = "asset_id") -> QueryDefinitionV1:
    return QueryDefinitionV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name="sec.asset-settle-condition"),
        entry=QueryEntryV1(
            binding="asset",
            subject_kinds=(ASSET,),
            subject_id=QueryParameterRefV1(parameter=entry_parameter),
        ),
        where=where,
        result_binding="asset",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="criticality",
                    value=QueryClaimValueRefV1(binding="asset", predicate="sec.asset.criticality"),
                ),
                QueryProjectionFieldV1(
                    name="environment",
                    value=QueryClaimValueRefV1(binding="asset", predicate="sec.asset.environment"),
                ),
            )
        ),
        parameters=(
            QueryParameterDeclarationV1(name="asset_id", value_type="string"),
            QueryParameterDeclarationV1(name="environment", value_type="string"),
        ),
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="refuse_on_conflict",
        ),
        default_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        pins=tuple(
            ArtifactPin(
                role="claim-type",
                target=ArtifactIdentity(kind="ClaimType", name=predicate),
                artifact_digest="sha256:" + digit * 64,
            )
            for predicate, digit in (("sec.asset.criticality", "3"), ("sec.asset.environment", "4"))
        ),
    )


def _accepted_query(query: QueryDefinitionV1) -> AcceptedQueryDefinitionV1:
    return AcceptedQueryDefinitionV1(
        path=query_definition_path(query.identity.name),
        query=query,
        artifact_digest=query_definition_digest(query).tagged,
    )


def _environment_is(value: str) -> QueryComparisonFilterV1:
    return QueryComparisonFilterV1(
        left=QueryClaimValueRefV1(binding="asset", predicate="sec.asset.environment"),
        operator="eq",
        right=QueryLiteralRefV1(value=value),
        value_type="string",
    )


def _settle(
    query: AcceptedQueryDefinitionV1,
    *,
    procedure: AcceptedProcedureV1 | None = None,
    change_kinds: tuple[str, ...] = ("revise",),
    required_fields: tuple[str, ...] = ("criticality", "environment"),
    **updates: object,
) -> ProcedureMandateV2:
    accepted = procedure or _procedure()
    values: dict[str, object] = dict(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="triage"),
        procedure=ArtifactPin(
            role="procedure",
            target=accepted.procedure.identity,
            artifact_digest=accepted.artifact_digest,
        ),
        grants="settle",
        resource_ceiling=_caps(),
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        scope=(
            MandateClaimScopeV1(
                claim_type=ArtifactPin(
                    role="claim-type", target=EXPOSURE, artifact_digest=EXPOSURE_DIGEST
                ),
                change_kinds=change_kinds,
            ),
        ),
        condition=MandateConditionV1(
            query=ArtifactPin(
                role="condition-query",
                target=query.query.identity,
                artifact_digest=query.artifact_digest,
            ),
            binding_parameter="asset_id",
            fixed_parameters={"environment": "nonproduction"},
            required_fields=required_fields,
            fallback="propose",
        ),
    )
    values.update(updates)
    return ProcedureMandateV2.model_validate(values)


def _claim_types() -> dict[ArtifactIdentity, ScopedClaimTypeV1]:
    return {
        EXPOSURE: ScopedClaimTypeV1(
            identity=EXPOSURE,
            artifact_digest=EXPOSURE_DIGEST,
            object_kind="literal",
            allowed_subject_kinds=(ASSET,),
        )
    }


def _law(mandate: ProcedureMandateV2, query: AcceptedQueryDefinitionV1, predecessor=None):
    return evaluate_procedure_mandate_v2_law(
        mandate,
        path=procedure_mandate_path(mandate.identity.name),
        predecessor=predecessor,
        procedure=_procedure(),
        claim_types=_claim_types(),
        condition_query=query,
    )


def _accepted(mandate) -> AcceptedProcedureMandateV1:
    return AcceptedProcedureMandateV1(
        path=procedure_mandate_path(mandate.identity.name),
        mandate=mandate,
        artifact_digest=procedure_mandate_digest(mandate).tagged,
    )


def _code(result) -> str:
    assert result.verdict == "refused", result
    return result.diagnostics[0].code


def test_v2_round_trips_and_parses_beside_v1() -> None:
    mandate = _settle(_accepted_query(_query()))
    path = procedure_mandate_path("triage")
    assert parse_procedure_mandate_any(render_procedure_mandate(mandate), path=path) == mandate
    legacy = _mandate()
    assert parse_procedure_mandate_any(render_procedure_mandate(legacy), path=path) == legacy
    assert (mandate_grant(mandate), mandate_grant(legacy)) == ("settle", "settle")
    with pytest.raises(ProcedureMandateError, match="canonical wire"):
        parse_procedure_mandate_any(render_procedure_mandate(mandate) + b"\n", path=path)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"scope": ()}, "settle grant requires"),
        ({"condition": None}, "settle grant requires"),
        ({"namespace": ("claims", "procedure-mandates")}, "only reach accepted Claims"),
        ({"namespace": ("governance",)}, "only reach accepted Claims"),
    ],
)
def test_a_settle_grant_is_scoped_conditioned_and_limited_to_claims(updates, message) -> None:
    with pytest.raises(ValueError, match=message):
        _settle(_accepted_query(_query()), **updates)


def test_a_propose_grant_carries_no_settle_terms() -> None:
    query = _accepted_query(_query())
    with pytest.raises(ValueError, match="propose grant carries no settle"):
        _settle(query, grants="propose")
    propose = _settle(query, grants="propose", scope=(), condition=None, namespace=("documents",))
    assert propose.pins == (propose.procedure,)


def test_the_binding_parameter_is_bound_by_core_never_fixed() -> None:
    with pytest.raises(ValueError, match="bound by Core"):
        MandateConditionV1(
            query=ArtifactPin(
                role="condition-query",
                target=ArtifactIdentity(kind="QueryDefinition", name="q"),
                artifact_digest="sha256:" + "2" * 64,
            ),
            binding_parameter="asset_id",
            fixed_parameters={"asset_id": "someone-else"},
            required_fields=("criticality",),
            fallback="refuse",
        )


def test_law_accepts_an_exact_fail_closed_condition() -> None:
    query = _accepted_query(_query(where=_environment_is("nonproduction")))
    result = _law(_settle(query), query)
    assert result.verdict == "accepted" and not result.narrowing


@pytest.mark.parametrize(
    "where",
    [
        QueryNegationFilterV1(operand=_environment_is("production")),
        QueryMembershipFilterV1(
            left=QueryClaimValueRefV1(binding="asset", predicate="sec.asset.environment"),
            values=(QueryLiteralRefV1(value="production"),),
            value_type="string",
            negated=True,
        ),
        QueryConjunctionFilterV1(
            filters=tuple(
                sorted(
                    (
                        _environment_is("nonproduction"),
                        QueryClaimPresenceFilterV1(
                            binding="asset", predicate="sec.asset.criticality", negated=True
                        ),
                    ),
                    key=lambda item: canonical_bytes(item.model_dump(mode="json")),
                )
            )
        ),
    ],
)
def test_law_refuses_a_condition_that_an_absent_fact_would_satisfy(where) -> None:
    query = _accepted_query(_query(where=where))
    assert _code(_law(_settle(query), query)) == ("playbill.procedure_mandate.condition_fails_open")


def test_law_refuses_unbound_unpinned_or_unprojected_conditions() -> None:
    query = _accepted_query(_query())
    other = _accepted_query(_query(entry_parameter="environment"))
    assert _code(_law(_settle(other), other)) == (
        "playbill.procedure_mandate.condition_not_target_bound"
    )
    assert _code(_law(_settle(query), other)) == (
        "playbill.procedure_mandate.condition_query_unresolved"
    )
    assert _code(_law(_settle(query, required_fields=("owner",)), query)) == (
        "playbill.procedure_mandate.condition_fields_unprojected"
    )
    unfixed = _settle(query).model_copy(
        update={"condition": _settle(query).condition.model_copy(update={"fixed_parameters": {}})}
    )
    assert _code(_law(unfixed, query)) == (
        "playbill.procedure_mandate.condition_parameters_mismatch"
    )


def test_law_refuses_scope_the_condition_cannot_bind() -> None:
    query = _accepted_query(_query())
    wrong_digest = _settle(query).model_copy(
        update={
            "scope": (
                MandateClaimScopeV1(
                    claim_type=ArtifactPin(
                        role="claim-type", target=EXPOSURE, artifact_digest="sha256:" + "9" * 64
                    ),
                    change_kinds=("revise",),
                ),
            )
        }
    )
    assert _code(_law(wrong_digest, query)) == (
        "playbill.procedure_mandate.scope_claim_type_unresolved"
    )
    object_bound = _settle(query).model_copy(
        update={
            "scope": (
                MandateClaimScopeV1(
                    claim_type=ArtifactPin(
                        role="claim-type", target=EXPOSURE, artifact_digest=EXPOSURE_DIGEST
                    ),
                    change_kinds=("revise",),
                    binding_subject_role="object",
                ),
            )
        }
    )
    assert _code(_law(object_bound, query)) == (
        "playbill.procedure_mandate.binding_role_unavailable"
    )


def test_narrowing_takes_the_fast_path_and_widening_does_not() -> None:
    query = _accepted_query(_query())
    base = _settle(query, change_kinds=("create", "revise"))
    predecessor = _accepted(base)

    def successor(**updates: object) -> ProcedureMandateV2:
        return base.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(predecessor_digest=predecessor.artifact_digest),
                **updates,
            }
        )

    narrowing = {
        "suspend": successor(suspended=True),
        "fewer change kinds": successor(
            scope=(base.scope[0].model_copy(update={"change_kinds": ("revise",)}),)
        ),
        "earlier expiry": successor(expires_at=datetime(2026, 6, 1, tzinfo=timezone.utc)),
        "demote to propose": successor(
            grants="propose", scope=(), condition=None, subject_scope=None
        ),
        "retire": successor(
            lifecycle=ArtifactLifecycle(
                state="retired", predecessor_digest=predecessor.artifact_digest
            )
        ),
    }
    widening = {
        "later expiry": successor(expires_at=datetime(2028, 1, 1, tzinfo=timezone.utc)),
        "more change kinds": successor(
            scope=(
                base.scope[0].model_copy(update={"change_kinds": ("create", "revise", "retire")}),
            )
        ),
        "changed condition": successor(
            condition=base.condition.model_copy(update={"fallback": "refuse"})
        ),
    }
    for label, mandate in narrowing.items():
        result = _law(mandate, query, predecessor)
        assert result.verdict == "accepted" and result.narrowing, label
    for label, mandate in widening.items():
        result = _law(mandate, query, predecessor)
        assert result.verdict == "accepted" and not result.narrowing, label
    # Lifting a suspension grants authority again, so it is governed like widening.
    suspended = _accepted(successor(suspended=True))
    lifted = base.model_copy(
        update={"lifecycle": ArtifactLifecycle(predecessor_digest=suspended.artifact_digest)}
    )
    assert not mandate_change_is_narrowing(lifted, suspended.mandate)


# -- Proposal evaluation on a revision-30 instance ---------------------------------


def _world(tmp_path):
    from cruxible_client.contracts.claim_types import claim_type_digest, claim_type_path
    from cruxible_client.contracts.procedures.artifacts import (
        procedure_artifact_digest,
        render_procedure,
    )
    from cruxible_client.contracts.query.definitions import render_query_definition
    from tests.core_support._support import initialize_local
    from tests.test_authoring.test_authoring_preflight import _seed_claim_surface
    from tests.test_claims.test_claims import _claim_type
    from tests.test_procedures.test_procedure_artifacts import _artifact, _definition

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    current = instance.accepted_coordinate()
    tree = instance.tree_at(current.git_oid)
    claim_type = next(
        _claim_type().__class__.model_validate_json(tree[path])
        for path in tree
        if path == claim_type_path(_claim_type().predicate)
    )
    status = claim_type.predicate
    query = _query().model_copy(
        update={
            "identity": ArtifactIdentity(kind="QueryDefinition", name="project.settle-condition"),
            "entry": QueryEntryV1(
                binding="item",
                subject_kinds=tuple(claim_type.allowed_subject_kinds),
                subject_id=QueryParameterRefV1(parameter="asset_id"),
            ),
            "result_binding": "item",
            "projection": QueryProjectionV1(
                fields=(
                    QueryProjectionFieldV1(
                        name="status", value=QueryClaimValueRefV1(binding="item", predicate=status)
                    ),
                )
            ),
            "parameters": (QueryParameterDeclarationV1(name="asset_id", value_type="string"),),
            "pins": (
                ArtifactPin(
                    role="claim-type",
                    target=claim_type.identity,
                    artifact_digest=claim_type_digest(claim_type).tagged,
                ),
            ),
        }
    )
    procedure = _artifact(_definition(terminal_capability=3))
    accepted_procedure = AcceptedProcedureV1(
        path="procedures/triage.json",
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    accepted_query = _accepted_query(query)
    mandate = _settle(
        accepted_query,
        procedure=accepted_procedure,
        required_fields=("status",),
        scope=(
            MandateClaimScopeV1(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=claim_type.identity,
                    artifact_digest=claim_type_digest(claim_type).tagged,
                ),
                change_kinds=("create", "revise"),
            ),
        ),
    )
    mandate = mandate.model_copy(
        update={"condition": mandate.condition.model_copy(update={"fixed_parameters": {}})}
    )
    grown = {
        **tree,
        accepted_procedure.path: render_procedure(procedure),
        accepted_query.path: render_query_definition(query),
    }
    return instance, current, tree, grown, mandate, query


def _evaluate(instance, current, base, proposed):
    from cruxible_core.proposals.proposals import evaluate_proposal_tree

    return evaluate_proposal_tree(
        base_tree=base,
        current_tree=base,
        proposed_tree=proposed,
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-09-01T12:00:00.000000Z",
        rebased=False,
        actor_id="owner",
    )


def test_a_settle_mandate_is_accepted_through_proposal_evaluation(tmp_path) -> None:
    instance, current, tree, grown, mandate, _query_definition = _world(tmp_path)
    path = procedure_mandate_path(mandate.identity.name)
    accepted = _evaluate(
        instance, current, tree, {**grown, path: render_procedure_mandate(mandate)}
    )
    assert accepted.diagnostics == ()
    assert accepted.candidate is not None


def test_a_fail_open_condition_refuses_during_proposal_evaluation(tmp_path) -> None:
    from cruxible_client.contracts.query.definitions import render_query_definition

    instance, current, tree, grown, mandate, query = _world(tmp_path)
    negated = query.model_copy(
        update={
            "where": QueryNegationFilterV1(
                operand=QueryClaimPresenceFilterV1(
                    binding="item", predicate=query.pins[0].target.name
                )
            )
        }
    )
    accepted_negated = _accepted_query(negated)
    pinned = mandate.model_copy(
        update={
            "condition": mandate.condition.model_copy(
                update={
                    "query": mandate.condition.query.model_copy(
                        update={"artifact_digest": accepted_negated.artifact_digest}
                    )
                }
            )
        }
    )
    refused = _evaluate(
        instance,
        current,
        tree,
        {
            **grown,
            accepted_negated.path: render_query_definition(negated),
            procedure_mandate_path(pinned.identity.name): render_procedure_mandate(pinned),
        },
    )
    assert "playbill.procedure_mandate.condition_fails_open" in {
        item.code for item in refused.diagnostics
    }


def test_only_a_purely_narrowing_candidate_skips_independent_approval(
    tmp_path, monkeypatch
) -> None:
    from cruxible_client.contracts.governance import INDEPENDENT_APPROVAL_REQUIREMENTS
    from cruxible_core.proposals import proposals

    instance, current, tree, grown, mandate, _query_definition = _world(tmp_path)
    monkeypatch.setattr(
        proposals, "_approval_requirements", lambda _tree: INDEPENDENT_APPROVAL_REQUIREMENTS
    )
    path = procedure_mandate_path(mandate.identity.name)
    first = {**grown, path: render_procedure_mandate(mandate)}
    granted = _evaluate(instance, current, tree, first)
    assert granted.candidate is not None
    assert granted.candidate.approval_requirements == INDEPENDENT_APPROVAL_REQUIREMENTS

    lineage = ArtifactLifecycle(predecessor_digest=procedure_mandate_digest(mandate).tagged)
    suspended = mandate.model_copy(update={"suspended": True, "lifecycle": lineage})
    fast = _evaluate(instance, current, first, {**first, path: render_procedure_mandate(suspended)})
    assert fast.diagnostics == ()
    assert fast.candidate is not None and fast.candidate.approval_requirements == ()

    retired = mandate.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=lineage.predecessor_digest
            )
        }
    )
    retiring = _evaluate(
        instance, current, first, {**first, path: render_procedure_mandate(retired)}
    )
    assert retiring.candidate is not None and retiring.candidate.approval_requirements == ()
    after_retirement = {**first, path: render_procedure_mandate(retired)}
    revived = mandate.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(retired).tagged
            )
        }
    )
    revival = _evaluate(
        instance,
        current,
        after_retirement,
        {**after_retirement, path: render_procedure_mandate(revived)},
    )
    assert revival.candidate is not None
    assert revival.candidate.approval_requirements == INDEPENDENT_APPROVAL_REQUIREMENTS

    # A narrowing mandate beside any ordinary change keeps the policy's approval.
    from cruxible_client.contracts.query.definitions import render_query_definition

    unrelated = _query_definition.model_copy(
        update={"identity": ArtifactIdentity(kind="QueryDefinition", name="project.unrelated")}
    )
    mixed = _evaluate(
        instance,
        current,
        first,
        {
            **first,
            path: render_procedure_mandate(suspended),
            _accepted_query(unrelated).path: render_query_definition(unrelated),
        },
    )
    assert mixed.candidate is not None
    assert mixed.candidate.approval_requirements == INDEPENDENT_APPROVAL_REQUIREMENTS

    widened = mandate.model_copy(
        update={"expires_at": datetime(2030, 1, 1, tzinfo=timezone.utc), "lifecycle": lineage}
    )
    governed = _evaluate(
        instance, current, first, {**first, path: render_procedure_mandate(widened)}
    )
    assert governed.candidate is not None
    assert governed.candidate.approval_requirements == INDEPENDENT_APPROVAL_REQUIREMENTS


def test_v2_mandates_require_compiler_revision_31(tmp_path) -> None:
    from cruxible_client.contracts.errors import ProjectionFormatError
    from cruxible_core.compiler.compiler import (
        TRIGGER_CAPTURE_COMPILER,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.compiler.projection_artifacts import parse_projection_tree

    mandate = _settle(_accepted_query(_query()))
    with pytest.raises(ProjectionFormatError, match="ProcedureMandate v2 requires"):
        parse_projection_tree(
            {procedure_mandate_path("triage"): render_procedure_mandate(mandate)},
            registry=projection_registry_for_compiler(TRIGGER_CAPTURE_COMPILER),
            artifact_kinds=artifact_kinds_for_compiler(TRIGGER_CAPTURE_COMPILER),
        )


def test_settle_authoring_names_its_scope_and_condition_and_lowering_pins_them(tmp_path) -> None:
    from cruxible_client.contracts.authoring.models import (
        MandateConditionAuthoringV1,
        MandateScopeAuthoringV1,
        ProcedureMandateAuthoringPayloadV1,
    )
    from cruxible_core.authoring.lowering import (
        AuthoringLoweringError,
        _render_procedure_mandate_member,
    )

    _instance, _current, _tree, grown, mandate, query = _world(tmp_path)
    payload = ProcedureMandateAuthoringPayloadV1(
        name="triage",
        procedure_name=mandate.procedure.target.name,
        grants="settle",
        resource_ceiling=mandate.resource_ceiling,
        namespace=("claims",),
        valid_from=mandate.valid_from,
        expires_at=mandate.expires_at,
        scope=(
            MandateScopeAuthoringV1(
                claim_type=mandate.scope[0].claim_type.target.name,
                change_kinds=("revise", "create"),
            ),
        ),
        condition=MandateConditionAuthoringV1(
            query_name=query.identity.name,
            binding_parameter="asset_id",
            required_fields=("status",),
            fallback="propose",
        ),
    )
    path, content, _digest = _render_procedure_mandate_member(payload, tree=grown)
    authored = parse_procedure_mandate_any(content, path=path)
    # Author order does not matter; lowering canonicalizes and pins exact digests.
    assert authored == mandate

    missing = payload.model_copy(
        update={"condition": payload.condition.model_copy(update={"query_name": "project.absent"})}
    )
    with pytest.raises(AuthoringLoweringError) as refused:
        _render_procedure_mandate_member(missing, tree=grown)
    assert refused.value.code == "playbill.authoring.procedure_mandate_condition_query_missing"
