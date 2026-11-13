"""Conditional settle grants: exact pins, fail-closed predicates, governed widening."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.mandate_scope import MandateChanges, MandateValueCondition
from cruxible_client.contracts.procedure_mandates import (
    AcceptedProcedureMandate,
    MandateClaimScope,
    MandateCondition,
    ProcedureMandate,
    ProcedureMandateError,
    ScopedClaimType,
    evaluate_procedure_mandate_v2_law,
    mandate_change_is_narrowing,
    mandate_grant,
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.line_specs import AcceptedLineSpec, line_spec_digest
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinition,
    QueryDefinition,
    QueryEvaluationPolicy,
    query_definition_digest,
    query_definition_path,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimPresenceFilter,
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryConjunctionFilter,
    QueryEntry,
    QueryLiteralRef,
    QueryMembershipFilter,
    QueryNegationFilter,
    QueryParameterDeclaration,
    QueryParameterRef,
    QueryProjection,
    QueryProjectionField,
)
from cruxible_client.contracts.shapes import parse_type
from tests.support.mandates import procedure_mandate, scope_changes
from tests.support.procedures import procedure_members
from tests.test_procedures.test_procedure_mandates import (
    _accepted_line,
    _line,
    _mandate,
    _procedure,
)

ASSET = "sec.asset"
EXPOSURE = ArtifactIdentity(kind="ClaimType", name="sec.exposure.status")
EXPOSURE_DIGEST = "sha256:" + "1" * 64


def _query(*, where=None, entry_parameter: str = "asset_id") -> QueryDefinition:
    return QueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name="sec.asset-settle-condition"),
        entry=QueryEntry(
            binding="asset",
            subject_kinds=(ASSET,),
            subject_id=QueryParameterRef(parameter=entry_parameter),
        ),
        where=where,
        result_binding="asset",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="criticality",
                    value=QueryClaimValueRef(binding="asset", predicate="sec.asset.criticality"),
                ),
                QueryProjectionField(
                    name="environment",
                    value=QueryClaimValueRef(binding="asset", predicate="sec.asset.environment"),
                ),
            )
        ),
        parameters=(
            QueryParameterDeclaration(name="asset_id", value_type="string"),
            QueryParameterDeclaration(name="environment", value_type="string"),
        ),
        evaluation_policy=QueryEvaluationPolicy(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="refuse_on_conflict",
        ),
        default_budgets=QueryBudgets(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=1, max_traversal_depth=0),
    )


def _accepted_query(query: QueryDefinition) -> AcceptedQueryDefinition:
    return AcceptedQueryDefinition(
        path=query_definition_path(query.identity.name),
        query=query,
        artifact_digest=query_definition_digest(query).tagged,
    )


def _environment_is(value: str) -> QueryComparisonFilter:
    return QueryComparisonFilter(
        left=QueryClaimValueRef(binding="asset", predicate="sec.asset.environment"),
        operator="eq",
        right=QueryLiteralRef(value=value),
        value_type="string",
    )


def _settle(
    query: AcceptedQueryDefinition,
    *,
    procedure: AcceptedProcedure | None = None,
    line: object = None,
    change_kinds: tuple[str, ...] = ("revise",),
    required_fields: tuple[str, ...] = ("criticality", "environment"),
    **updates: object,
) -> ProcedureMandate:
    accepted = procedure or _procedure()
    bound_line = line or _line(procedure=accepted)
    return procedure_mandate(
        "triage",
        changes=updates,
        line=ArtifactPin(
            role="line",
            target=bound_line.identity,
            artifact_digest=line_spec_digest(bound_line).tagged,
        ),
        grants="settle",
        scope=(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type", target=EXPOSURE, artifact_digest=EXPOSURE_DIGEST
                ),
                changes=scope_changes(*change_kinds),
            ),
        ),
        condition=MandateCondition(
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


def _claim_types() -> dict[ArtifactIdentity, ScopedClaimType]:
    return {
        EXPOSURE: ScopedClaimType(
            identity=EXPOSURE,
            artifact_digest=EXPOSURE_DIGEST,
            value={"kind": "string", "max_len": 256},
            allowed_subject_kinds=(ASSET,),
        )
    }


def _law(
    mandate: ProcedureMandate,
    query: AcceptedQueryDefinition,
    predecessor=None,
    *,
    procedure: AcceptedProcedure | None = None,
    line: AcceptedLineSpec | None = None,
    value: object = None,
):
    accepted_procedure = procedure or _procedure()
    accepted_line = line or _accepted_line(_line(procedure=accepted_procedure))
    claim_types = _claim_types()
    if value is not None:
        claim_types = {
            EXPOSURE: claim_types[EXPOSURE].model_copy(update={"value": parse_type(value)})
        }
    return evaluate_procedure_mandate_v2_law(
        mandate,
        path=procedure_mandate_path(mandate.identity.name),
        predecessor=predecessor,
        line=accepted_line,
        procedure=accepted_procedure,
        claim_types=claim_types,
        condition_query=query,
    )


def _accepted(mandate) -> AcceptedProcedureMandate:
    return AcceptedProcedureMandate(
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
    legacy = _mandate(grants="settle")
    assert parse_procedure_mandate_any(render_procedure_mandate(legacy), path=path) == legacy
    assert (mandate_grant(mandate), mandate_grant(legacy)) == ("settle", "settle")
    with pytest.raises(ProcedureMandateError, match="canonical wire"):
        parse_procedure_mandate_any(render_procedure_mandate(mandate) + b"\n", path=path)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"scope": ()}, "settle grant requires"),
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
    assert propose.pins == (propose.line,)


def test_the_binding_parameter_is_bound_by_core_never_fixed() -> None:
    with pytest.raises(ValueError, match="bound by Core"):
        MandateCondition(
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
        QueryNegationFilter(operand=_environment_is("production")),
        QueryMembershipFilter(
            left=QueryClaimValueRef(binding="asset", predicate="sec.asset.environment"),
            values=(QueryLiteralRef(value="production"),),
            value_type="string",
            negated=True,
        ),
        QueryConjunctionFilter(
            filters=tuple(
                sorted(
                    (
                        _environment_is("nonproduction"),
                        QueryClaimPresenceFilter(
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
    assert _code(_law(_settle(query), query)) == ("cruxible.procedure_mandate.condition_fails_open")


def test_law_refuses_unbound_unpinned_or_unprojected_conditions() -> None:
    query = _accepted_query(_query())
    other = _accepted_query(_query(entry_parameter="environment"))
    assert _code(_law(_settle(other), other)) == (
        "cruxible.procedure_mandate.condition_not_target_bound"
    )
    assert _code(_law(_settle(query), other)) == (
        "cruxible.procedure_mandate.condition_query_unresolved"
    )
    assert _code(_law(_settle(query, required_fields=("owner",)), query)) == (
        "cruxible.procedure_mandate.condition_fields_unprojected"
    )
    unfixed = _settle(query).model_copy(
        update={"condition": _settle(query).condition.model_copy(update={"fixed_parameters": {}})}
    )
    assert _code(_law(unfixed, query)) == (
        "cruxible.procedure_mandate.condition_parameters_mismatch"
    )


def test_law_refuses_scope_the_condition_cannot_bind() -> None:
    query = _accepted_query(_query())
    wrong_digest = _settle(query).model_copy(
        update={
            "scope": (
                MandateClaimScope(
                    claim_type=ArtifactPin(
                        role="claim-type", target=EXPOSURE, artifact_digest="sha256:" + "9" * 64
                    ),
                    changes=scope_changes("revise"),
                ),
            )
        }
    )
    assert _code(_law(wrong_digest, query)) == (
        "cruxible.procedure_mandate.scope_claim_type_unresolved"
    )
    object_bound = _settle(query).model_copy(
        update={
            "scope": (
                MandateClaimScope(
                    claim_type=ArtifactPin(
                        role="claim-type", target=EXPOSURE, artifact_digest=EXPOSURE_DIGEST
                    ),
                    changes=scope_changes("revise"),
                    binding_subject_role="object",
                ),
            )
        }
    )
    assert _code(_law(object_bound, query)) == (
        "cruxible.procedure_mandate.binding_role_unavailable"
    )


def test_narrowing_takes_the_fast_path_and_widening_does_not() -> None:
    query = _accepted_query(_query())
    base = _settle(query, change_kinds=("create", "revise"))
    predecessor = _accepted(base)

    def successor(**updates: object) -> ProcedureMandate:
        return base.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(predecessor_digest=predecessor.artifact_digest),
                **updates,
            }
        )

    narrowing = {
        "suspend": successor(suspended=True),
        "fewer change kinds": successor(
            scope=(base.scope[0].model_copy(update={"changes": scope_changes("revise")}),)
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
                base.scope[0].model_copy(
                    update={"changes": scope_changes("create", "revise", "retire")}
                ),
            )
        ),
        "changed condition": successor(
            condition=base.condition.model_copy(update={"fallback": "refuse"})
        ),
        "another basis": successor(
            scope=(
                base.scope[0].model_copy(
                    update={
                        "changes": scope_changes(
                            "create", "revise", basis=("extracted", "measured")
                        )
                    }
                ),
            )
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


def test_a_settle_grant_names_the_subject_kinds_it_may_create() -> None:
    """``subject_creation`` (identity D1): settle-only, sorted kinds, narrowed by subset."""

    from cruxible_client.contracts.procedure_mandates import (
        ProcedureMandateInvocation,
        evaluate_procedure_mandate,
        procedure_mandate_digest,
    )

    query = _accepted_query(_query())
    base = _settle(query, subject_creation=("ops.asset", "ops.host"))
    path = procedure_mandate_path("triage")
    assert parse_procedure_mandate_any(render_procedure_mandate(base), path=path) == base
    # An empty list leaves the wire (and every existing digest) as it was.
    assert b"subject_creation" not in render_procedure_mandate(_settle(query))
    for bad in (("ops.host", "ops.asset"), ("ops.asset", "ops.asset"), ("Ops",)):
        with pytest.raises(ValueError, match="subject_creation"):
            _settle(query, subject_creation=bad)
    with pytest.raises(ValueError, match="propose grant carries no settle"):
        _settle(query, grants="propose", scope=(), condition=None, subject_creation=("ops.asset",))

    # The grant reaches a Subject of a named kind beside its Claims, and no other.
    def evaluated(*targets: str) -> tuple[str, ...]:
        return evaluate_procedure_mandate(
            base,
            ProcedureMandateInvocation(
                line_identity=base.line.target,
                line_artifact_digest=base.line.artifact_digest,
                requested_rung=3,
                target_paths=targets,
                evaluation_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
                accepted_mandate_digest=procedure_mandate_digest(base).tagged,
            ),
        ).refusal_codes

    assert evaluated("claims/ab/CLM-1.json", "subjects/ops.asset/web.json") == ()
    assert evaluated("subjects/ops.other/web.json") == ("procedure_mandate_namespace_mismatch",)

    predecessor = _accepted(base)
    fewer = base.model_copy(
        update={
            "subject_creation": ("ops.asset",),
            "lifecycle": ArtifactLifecycle(predecessor_digest=predecessor.artifact_digest),
        }
    )
    more = base.model_copy(
        update={
            "subject_creation": ("ops.asset", "ops.host", "ops.zone"),
            "lifecycle": ArtifactLifecycle(predecessor_digest=predecessor.artifact_digest),
        }
    )
    assert mandate_change_is_narrowing(fewer, base)
    assert not mandate_change_is_narrowing(more, base)


# -- Proposal evaluation on a revision-30 instance ---------------------------------


def _world(tmp_path):
    from cruxible_client.contracts.claim_types import claim_type_digest, claim_type_path
    from cruxible_client.contracts.procedures.artifacts import (
        procedure_artifact_digest,
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

    from tests.support.procedures import revalidated

    query = _query().model_copy(
        update={
            "artifact_format": "cruxible-query-definition-v3",
            "identity": ArtifactIdentity(kind="QueryDefinition", name="project.settle-condition"),
            "entry": QueryEntry(
                binding="item",
                subject_kinds=tuple(claim_type.allowed_subject_kinds),
                subject_id=QueryParameterRef(parameter="asset_id"),
            ),
            "result_binding": "item",
            "projection": QueryProjection(
                fields=(
                    QueryProjectionField(
                        name="status", value=QueryClaimValueRef(binding="item", predicate=status)
                    ),
                )
            ),
            "parameters": (QueryParameterDeclaration(name="asset_id", value_type="string"),),
        }
    )
    procedure = revalidated(
        _artifact(
            _definition(
                terminal_capability=3,
                query=ArtifactPin(
                    role="query",
                    target=query.identity,
                    artifact_digest=query_definition_digest(query).tagged,
                ),
            )
        )
    )
    accepted_procedure = AcceptedProcedure(
        path="procedures/triage.json",
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    accepted_query = _accepted_query(query)
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_path,
        render_line_spec,
    )

    this_line = _line(procedure=accepted_procedure)
    mandate = _settle(
        accepted_query,
        procedure=accepted_procedure,
        line=this_line,
        required_fields=("status",),
        scope=(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=claim_type.identity,
                    artifact_digest=claim_type_digest(claim_type).tagged,
                ),
                changes=scope_changes("create", "revise"),
            ),
        ),
    )
    mandate = mandate.model_copy(
        update={"condition": mandate.condition.model_copy(update={"fixed_parameters": {}})}
    )
    grown = {
        **tree,
        **procedure_members(procedure),
        accepted_query.path: render_query_definition(query),
        line_spec_path(this_line.identity.name): render_line_spec(this_line),
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
            "where": QueryNegationFilter(
                operand=QueryClaimPresenceFilter(
                    binding="item", predicate=query.referenced_predicates[0]
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
    assert "cruxible.procedure_mandate.condition_fails_open" in {
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


def test_settle_authoring_names_its_scope_and_condition_and_lowering_pins_them(tmp_path) -> None:
    from cruxible_client.contracts.authoring.models import (
        MandateConditionAuthoring,
        MandateScopeAuthoring,
        ProcedureMandateAuthoringPayload,
    )
    from cruxible_core.authoring.lowering import (
        AuthoringLoweringError,
        _render_procedure_mandate_member,
    )

    _instance, _current, _tree, grown, mandate, query = _world(tmp_path)
    payload = ProcedureMandateAuthoringPayload(
        name="triage",
        line_name=mandate.line.target.name,
        grants="settle",
        namespace=("claims",),
        valid_from=mandate.valid_from,
        expires_at=mandate.expires_at,
        scope=(
            MandateScopeAuthoring(
                claim_type=mandate.scope[0].claim_type.target.name,
                changes=scope_changes("create", "revise"),
            ),
        ),
        condition=MandateConditionAuthoring(
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
    assert refused.value.code == "cruxible.authoring.procedure_mandate_condition_query_missing"


# --- B4c: basis scope, value conditions, typed target values, subjects ---------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({}, "at least one change kind"),
        ({"retire": {"basis": ["extracted"]}}, "retire scope settles only on basis absence"),
        ({"create": {"basis": []}}, "nonempty, unique and in canonical order"),
        ({"create": {"basis": ["derived", "extracted"]}}, "canonical order"),
        ({"create": {"basis": ["inferred"]}}, "basis"),
    ],
)
def test_scope_changes_name_bases_per_change_kind(changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        MandateChanges.model_validate(changes)


def test_a_settle_grant_may_omit_its_condition() -> None:
    query = _accepted_query(_query())
    mandate = _settle(query, condition=None)
    path = procedure_mandate_path("triage")
    assert parse_procedure_mandate_any(render_procedure_mandate(mandate), path=path) == mandate
    assert _law(mandate, None).verdict == "accepted"


def test_the_scope_wire_names_each_basis_per_change_kind() -> None:
    query = _accepted_query(_query())
    mandate = _settle(
        query,
        scope=(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type", target=EXPOSURE, artifact_digest=EXPOSURE_DIGEST
                ),
                changes=MandateChanges.model_validate(
                    {"create": {"basis": ["extracted"]}, "retire": {"basis": ["absence"]}}
                ),
                value=MandateValueCondition.model_validate({"from": ["open"], "to": ["fixed"]}),
            ),
        ),
    )
    wire = render_procedure_mandate(mandate)
    assert b'"changes":{"create":{"basis":["extracted"]},"retire":{"basis":["absence"]}}' in (
        wire.replace(b" ", b"").replace(b"\n", b"")
    )
    assert b'"from"' in wire and b"from_" not in wire
    path = procedure_mandate_path("triage")
    assert parse_procedure_mandate_any(wire, path=path) == mandate


def _valued(query, value: dict) -> ProcedureMandate:  # type: ignore[no-untyped-def]
    return _settle(
        query,
        scope=(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type", target=EXPOSURE, artifact_digest=EXPOSURE_DIGEST
                ),
                changes=scope_changes("revise"),
                value=MandateValueCondition.model_validate(value),
            ),
        ),
    )


def test_a_value_condition_names_members_of_an_enum_or_bool_type_only() -> None:
    query = _accepted_query(_query())
    enum = "enum[fixed, open]"
    assert _law(_valued(query, {"from": ["open"], "to": ["fixed"]}), query, value=enum).verdict == (
        "accepted"
    )
    assert _law(_valued(query, {"to": [True]}), query, value="bool").verdict == "accepted"
    assert _code(_law(_valued(query, {"to": ["fixed"]}), query)) == (
        "cruxible.procedure_mandate.value_condition_unsupported"
    )
    assert _code(_law(_valued(query, {"to": ["closed"]}), query, value=enum)) == (
        "cruxible.procedure_mandate.value_condition_member_unknown"
    )
    assert _code(_law(_valued(query, {"to": ["true"]}), query, value="bool")) == (
        "cruxible.procedure_mandate.value_condition_member_unknown"
    )
    with pytest.raises(ValueError, match="declares from, to, or both"):
        MandateValueCondition.model_validate({})


def test_value_conditions_narrow_only_by_subset() -> None:
    query = _accepted_query(_query())
    base = _valued(query, {"to": ["fixed", "open"]})
    narrower = _valued(query, {"to": ["fixed"]})
    wider = _valued(query, {"from": ["open"]})
    assert mandate_change_is_narrowing(narrower, base)
    assert not mandate_change_is_narrowing(wider, base)
    unbounded = base.model_copy(
        update={"scope": (base.scope[0].model_copy(update={"value": None}),)}
    )
    assert not mandate_change_is_narrowing(unbounded, base)
    assert mandate_change_is_narrowing(base, unbounded)


def _target_query(value_type: str = "string", *, required: bool = False) -> QueryDefinition:
    query = _query()
    return query.model_copy(
        update={
            "parameters": (
                *query.parameters,
                QueryParameterDeclaration(
                    name="status_value", value_type=value_type, required=required
                ),
            )
        }
    )


def _bind_value(query: AcceptedQueryDefinition, **condition: object) -> ProcedureMandate:
    mandate = _settle(query)
    bound = MandateCondition.model_validate(
        {**mandate.condition.model_dump(mode="python"), **condition}
    )
    return mandate.model_copy(update={"condition": bound})


def test_target_value_and_before_bind_typed_parameters() -> None:
    query = _accepted_query(_target_query())
    assert _law(_bind_value(query, value_parameter="status_value"), query).verdict == "accepted"
    assert _law(_bind_value(query, before_parameter="status_value"), query).verdict == ("accepted")
    mistyped = _accepted_query(_target_query("integer"))
    assert _code(_law(_bind_value(mistyped, value_parameter="status_value"), mistyped)) == (
        "cruxible.procedure_mandate.condition_value_type_mismatch"
    )
    # A target parameter must be declared, and a required one must be bound.
    assert _code(_law(_bind_value(query, value_parameter="undeclared"), query)) == (
        "cruxible.procedure_mandate.condition_parameters_mismatch"
    )
    required = _accepted_query(_target_query(required=True))
    assert _code(_law(_settle(required), required)) == (
        "cruxible.procedure_mandate.condition_parameters_mismatch"
    )
    with pytest.raises(ValueError, match="distinct"):
        _bind_value(query, value_parameter="asset_id")
    with pytest.raises(ValueError, match="bound by Core"):
        _bind_value(query, value_parameter="environment")


def test_subjects_expected_is_settle_only_and_narrows_away_only_by_keeping_it() -> None:
    query = _accepted_query(_query())
    expected = _settle(query, subjects="expected")
    assert _law(expected, query).verdict == "accepted"
    open_ = _settle(query)
    assert mandate_change_is_narrowing(expected, open_)
    assert not mandate_change_is_narrowing(open_, expected)
    with pytest.raises(ValueError, match="propose grant carries no settle"):
        _settle(query, grants="propose", scope=(), condition=None, subjects="expected")
