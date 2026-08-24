"""PC-F QueryDefinition artifact, grammar, verdict-policy, and closure tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest, render_claim_type
from cruxible_client.contracts.errors import CanonicalEncodingError, ProjectionFormatError
from cruxible_client.contracts.laws import PLAYBILL_ACCEPTANCE_LAWS, QUERY_DEFINITION_ACCEPTANCE_LAW
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.projection_extensions import playbill_runtime_extension_registry
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinitionV1,
    QueryDefinitionFormatError,
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
    evaluate_query_definition_law,
    parse_query_definition,
    query_definition_address,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryClaimPresenceFilterV1,
    QueryClaimValueRefV1,
    QueryComparisonFilterV1,
    QueryConjunctionFilterV1,
    QueryEntryV1,
    QueryIncludeV1,
    QueryLiteralRefV1,
    QueryOrderingV1,
    QueryParameterDeclarationV1,
    QueryParameterRefV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
    QuerySubjectFieldRefV1,
    QueryTraversalStepV1,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.closure import evaluate_dependency_closure, parse_dependency_artifact
from cruxible_core.playbill.compiler import PC_D_COMPILER, projection_registry_for_compiler
from cruxible_core.playbill.projection_artifacts import (
    PLAYBILL_ARTIFACT_KINDS,
    parse_projection_tree,
    registered_path_kind,
)
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    evaluate_proposal_tree,
)
from tests.test_playbill._support import initialize_local

GOLDEN = Path(__file__).parents[1] / "goldens" / "playbill" / "query-definition-v1.json"
QUERY_PATH = "query-definitions/project.active_work.yaml"
STATUS_PREDICATE = "project.work_item.status"
REVIEWER_PREDICATE = "project.work_item.reviewed_by"
STATUS_CLAIM_TYPE_PATH = "claim-types/project.work_item/status.yaml"
REVIEWER_CLAIM_TYPE_PATH = "claim-types/project.work_item/reviewed_by.yaml"
TIMESTAMP = "2026-08-16T14:00:00.000000Z"

OWNER_AUTHORITY = ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",))


def claim_type(predicate: str, *, object_kind: str = "literal") -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=("project.work_item",),
        object_kind=object_kind,  # type: ignore[arg-type]
        literal_schema={"type": "string"} if object_kind == "literal" else None,
        allowed_object_subject_kinds=() if object_kind == "literal" else ("project.person",),
        cardinality="one" if object_kind == "literal" else "many",
        permitted_roles=("normative",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one" if object_kind == "literal" else "many",
            eligible_verdicts=("supported",),
            selector="only_contender" if object_kind == "literal" else "all",
        ),
        authority=OWNER_AUTHORITY,
    )


def claim_type_pin(predicate: str, *, object_kind: str = "literal") -> ArtifactPin:
    return ArtifactPin(
        role="claim-type",
        target=ArtifactIdentity(kind="ClaimType", name=predicate),
        artifact_digest=claim_type_digest(claim_type(predicate, object_kind=object_kind)).tagged,
    )


def active_work_query(**overrides: object) -> QueryDefinitionV1:
    """The reviewed reference query: entry, traversal, filter, include, budgets."""

    fields: dict[str, object] = {
        "identity": ArtifactIdentity(kind="QueryDefinition", name="project.active_work"),
        "description": "Active work items with their reviewers and latest status.",
        "entry": QueryEntryV1(binding="item", subject_kinds=("project.work_item",)),
        "traversal": (
            QueryTraversalStepV1(
                binding="reviewer",
                from_binding="item",
                predicate=REVIEWER_PREDICATE,
                direction="forward",
                target_subject_kinds=("project.person",),
            ),
        ),
        "where": QueryConjunctionFilterV1(
            filters=(
                QueryClaimPresenceFilterV1(binding="item", predicate=STATUS_PREDICATE),
                QueryComparisonFilterV1(
                    left=QueryClaimValueRefV1(binding="item", predicate=STATUS_PREDICATE),
                    operator="eq",
                    right=QueryParameterRefV1(parameter="status"),
                    value_type="string",
                ),
            ),
        ),
        "result_binding": "reviewer",
        "result_shape": "path",
        "result_cardinality": "many",
        "dedupe": "path",
        "projection": QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="item_id",
                    value=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                ),
                QueryProjectionFieldV1(
                    name="reviewer_id",
                    value=QuerySubjectFieldRefV1(binding="reviewer", field="subject_id"),
                ),
            )
        ),
        "orderings": (
            QueryOrderingV1(
                key=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                value_type="string",
            ),
        ),
        "includes": (
            QueryIncludeV1(
                name="status",
                binding="status_holder",
                from_binding="item",
                predicate=STATUS_PREDICATE,
                direction="forward",
                max_items=1,
            ),
        ),
        "parameters": (QueryParameterDeclarationV1(name="status", value_type="string"),),
        "evaluation_policy": QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
            result_expiry=CanonicalDurationV1(microseconds=3_600_000_000),
        ),
        "default_budgets": QueryBudgetsV1(
            max_results=50,
            max_traversal_depth=2,
            max_paths=200,
            max_paths_per_result=5,
        ),
        "maximum_budgets": QueryBudgetsV1(
            max_results=500,
            max_traversal_depth=4,
            max_paths=2000,
            max_paths_per_result=50,
        ),
        "authority": OWNER_AUTHORITY,
        "pins": (
            claim_type_pin(REVIEWER_PREDICATE, object_kind="subject"),
            claim_type_pin(STATUS_PREDICATE),
        ),
    }
    fields.update(overrides)
    return QueryDefinitionV1(**fields)  # type: ignore[arg-type]


def single_status_query(**overrides: object) -> QueryDefinitionV1:
    """A one-cardinality Subject read that refuses rather than picking a winner."""

    fields: dict[str, object] = {
        "identity": ArtifactIdentity(kind="QueryDefinition", name="project.work_item_status"),
        "entry": QueryEntryV1(
            binding="item",
            subject_kinds=("project.work_item",),
            subject_id=QueryParameterRefV1(parameter="item_id"),
        ),
        "result_binding": "item",
        "result_shape": "subject",
        "result_cardinality": "one",
        "dedupe": "subject",
        "projection": QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="status",
                    value=QueryClaimValueRefV1(binding="item", predicate=STATUS_PREDICATE),
                ),
            )
        ),
        "parameters": (QueryParameterDeclarationV1(name="item_id", value_type="string"),),
        "evaluation_policy": QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="refuse_on_conflict",
        ),
        "default_budgets": QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        "maximum_budgets": QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        "authority": OWNER_AUTHORITY,
        "pins": (claim_type_pin(STATUS_PREDICATE),),
    }
    fields.update(overrides)
    return QueryDefinitionV1(**fields)  # type: ignore[arg-type]


def accepted_query(query: QueryDefinitionV1) -> AcceptedQueryDefinitionV1:
    return AcceptedQueryDefinitionV1(
        path=query_definition_path(query.identity.name),
        query=query,
        artifact_digest=query_definition_digest(query).tagged,
    )


# -- frozen wire ---------------------------------------------------------


def test_query_definition_parse_render_digest_and_path_match_frozen_golden() -> None:
    fixture = json.loads(GOLDEN.read_bytes())
    query = QueryDefinitionV1.model_validate(fixture["query_definition"])

    assert query == active_work_query()
    assert query_definition_path(query.identity.name) == QUERY_PATH
    rendered = render_query_definition(query)
    assert rendered.decode() == fixture["canonical_wire"]
    assert query_definition_digest(query).tagged == fixture["artifact_digest"]
    assert parse_query_definition(rendered, path=QUERY_PATH) == query


def test_query_definition_digest_uses_the_shared_envelope_domain() -> None:
    from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest

    query = active_work_query()
    assert query_definition_digest(query) == typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        query.model_dump(mode="json"),
    )


def test_query_definition_refuses_noncanonical_wire_unknown_format_and_wrong_path() -> None:
    query = active_work_query()
    rendered = render_query_definition(query)

    with pytest.raises(QueryDefinitionFormatError, match="canonical wire form"):
        parse_query_definition(rendered.replace(b"\n", b" \n"), path=QUERY_PATH)
    with pytest.raises(QueryDefinitionFormatError, match="unsupported QueryDefinition"):
        parse_query_definition(b'{"artifact_format":"playbill-named-query-v1"}', path=QUERY_PATH)
    with pytest.raises(QueryDefinitionFormatError, match="strict JSON"):
        parse_query_definition(b"not json", path=QUERY_PATH)
    with pytest.raises(QueryDefinitionFormatError, match="identity/path disagreement"):
        parse_query_definition(rendered, path="query-definitions/other.yaml")


def test_query_definition_identity_and_semantic_address_follow_the_pc_a1_grammar() -> None:
    query = active_work_query()

    assert query.identity.qualified == "QueryDefinition:project.active_work"
    assert query_definition_address(QUERY_PATH) == SemanticAddress.whole_artifact(QUERY_PATH)
    assert query_definition_address(QUERY_PATH).selector.scheme == "artifact-v1"
    with pytest.raises(ValidationError, match="kind QueryDefinition"):
        active_work_query(identity=ArtifactIdentity(kind="NamedQuery", name="project.active_work"))
    with pytest.raises(QueryDefinitionFormatError, match="path-addressable"):
        query_definition_path("Project.Active")


# -- registry activation -------------------------------------------------


def test_pc_f_activates_the_query_definition_path_kind_and_fails_closed_elsewhere() -> None:
    assert registered_path_kind(QUERY_PATH) == "query-definition"
    assert "query-definition" in PLAYBILL_ARTIFACT_KINDS.implemented_kinds()
    with pytest.raises(ProjectionFormatError, match="no registered format"):
        registered_path_kind("query-definitions/Project.yaml")
    with pytest.raises(ProjectionFormatError, match="no registered format"):
        registered_path_kind("queries/project.active_work.yaml")
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-query-definition-v1")
        == QUERY_DEFINITION_ACCEPTANCE_LAW
    )


# -- closed-union grammar ------------------------------------------------


def test_query_grammar_refuses_unknown_filter_ref_and_policy_kinds() -> None:
    payload = active_work_query().model_dump(mode="json")

    unknown_filter = json.loads(json.dumps(payload))
    unknown_filter["where"] = {
        "tag": "playbill-query-regex-filter-v1",
        "kind": "regex",
        "binding": "item",
        "pattern": ".*",
    }
    with pytest.raises(ValidationError):
        QueryDefinitionV1.model_validate(unknown_filter)

    unknown_ref = json.loads(json.dumps(payload))
    unknown_ref["orderings"][0]["key"] = {
        "tag": "playbill-query-entity-property-ref-v1",
        "kind": "entity_property",
        "entity": "item",
        "property": "status",
    }
    with pytest.raises(ValidationError):
        QueryDefinitionV1.model_validate(unknown_ref)

    unknown_policy = json.loads(json.dumps(payload))
    unknown_policy["evaluation_policy"]["conflict_behavior"] = "last_write_wins"
    with pytest.raises(ValidationError):
        QueryDefinitionV1.model_validate(unknown_policy)

    unknown_tag = json.loads(json.dumps(payload))
    unknown_tag["evaluation_policy"]["tag"] = "playbill-query-evaluation-policy-v2"
    with pytest.raises(ValidationError):
        QueryDefinitionV1.model_validate(unknown_tag)

    extra_field = json.loads(json.dumps(payload))
    extra_field["relationship_state"] = "pending"
    with pytest.raises(ValidationError):
        QueryDefinitionV1.model_validate(extra_field)


def test_query_grammar_requires_canonical_ordering_and_unique_declarations() -> None:
    with pytest.raises(ValidationError, match="sorted"):
        active_work_query(
            pins=(
                claim_type_pin(STATUS_PREDICATE),
                claim_type_pin(REVIEWER_PREDICATE, object_kind="subject"),
            )
        )
    with pytest.raises(ValidationError, match="at least two operands"):
        QueryConjunctionFilterV1(
            filters=(QueryClaimPresenceFilterV1(binding="item", predicate=STATUS_PREDICATE),)
        )
    with pytest.raises(ValidationError, match="sorted"):
        QueryConjunctionFilterV1(
            filters=(
                QueryClaimPresenceFilterV1(binding="item", predicate=STATUS_PREDICATE),
                QueryClaimPresenceFilterV1(binding="item", predicate=STATUS_PREDICATE),
            )
        )
    with pytest.raises(ValidationError, match="not repeat an ordering key"):
        active_work_query(
            orderings=(
                QueryOrderingV1(
                    key=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                    value_type="string",
                ),
                QueryOrderingV1(
                    key=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                    direction="descending",
                    value_type="string",
                ),
            )
        )


def test_query_grammar_closes_binding_parameter_and_predicate_references() -> None:
    with pytest.raises(ValidationError, match="undeclared parameter"):
        active_work_query(parameters=())
    with pytest.raises(ValidationError, match="extend an earlier declared binding"):
        active_work_query(
            traversal=(
                QueryTraversalStepV1(
                    binding="reviewer",
                    from_binding="missing",
                    predicate=REVIEWER_PREDICATE,
                    direction="forward",
                ),
            )
        )
    with pytest.raises(ValidationError, match="result_binding"):
        active_work_query(result_binding="status_holder")
    with pytest.raises(ValidationError, match="own source and target binding"):
        QueryIncludeV1(
            name="status",
            binding="status_holder",
            from_binding="item",
            predicate=STATUS_PREDICATE,
            direction="forward",
            max_items=1,
            where=QueryClaimPresenceFilterV1(binding="reviewer", predicate=STATUS_PREDICATE),
        )


def test_query_definition_pins_exactly_the_claim_types_it_references() -> None:
    with pytest.raises(ValidationError, match="pin exactly the ClaimTypes"):
        active_work_query(pins=(claim_type_pin(STATUS_PREDICATE),))
    with pytest.raises(ValidationError, match="pin exactly the ClaimTypes"):
        active_work_query(
            pins=(
                claim_type_pin("project.work_item.owner"),
                claim_type_pin(REVIEWER_PREDICATE, object_kind="subject"),
                claim_type_pin(STATUS_PREDICATE),
            )
        )
    with pytest.raises(ValidationError, match="target ClaimType identities"):
        active_work_query(
            pins=(
                claim_type_pin(REVIEWER_PREDICATE, object_kind="subject"),
                ArtifactPin(
                    role="claim-type",
                    target=ArtifactIdentity(kind="Subject", name=STATUS_PREDICATE),
                    artifact_digest=claim_type_pin(STATUS_PREDICATE).artifact_digest,
                ),
            )
        )
    query = active_work_query()
    assert query.referenced_predicates == (REVIEWER_PREDICATE, STATUS_PREDICATE)
    assert query.subject_kinds == ("project.person", "project.work_item")


def test_query_definition_refuses_contract_pins_that_do_not_target_contracts() -> None:
    with pytest.raises(ValidationError, match="target Contract identities"):
        active_work_query(
            pins=(
                claim_type_pin(REVIEWER_PREDICATE, object_kind="subject"),
                claim_type_pin(STATUS_PREDICATE),
                ArtifactPin(
                    role="result-contract",
                    target=ArtifactIdentity(kind="ClaimType", name=STATUS_PREDICATE),
                    artifact_digest=claim_type_pin(STATUS_PREDICATE).artifact_digest,
                ),
            )
        )


# -- shape, cardinality, conflict, and budget law -------------------------


def test_query_result_shape_dedupe_and_traversal_rules_stay_consistent() -> None:
    with pytest.raises(ValidationError, match="Subject dedupe"):
        single_status_query(dedupe="none")
    with pytest.raises(ValidationError, match="at least one traversal step"):
        single_status_query(
            result_shape="path",
            dedupe="path",
            default_budgets=QueryBudgetsV1(
                max_results=1, max_traversal_depth=0, max_paths=1, max_paths_per_result=1
            ),
            maximum_budgets=QueryBudgetsV1(
                max_results=1, max_traversal_depth=0, max_paths=1, max_paths_per_result=1
            ),
        )
    with pytest.raises(ValidationError, match="path or none dedupe"):
        active_work_query(dedupe="subject")
    with pytest.raises(ValidationError, match="resolve to a traversal binding"):
        active_work_query(result_shape="relation_claim", result_binding="item")
    with pytest.raises(ValidationError, match="optional traversal steps"):
        active_work_query(
            result_shape="subject",
            result_binding="item",
            dedupe="subject",
            traversal=(
                QueryTraversalStepV1(
                    binding="reviewer",
                    from_binding="item",
                    predicate=REVIEWER_PREDICATE,
                    direction="forward",
                    required=False,
                ),
            ),
        )


def test_one_cardinality_queries_refuse_or_surface_conflicts_but_never_pick_a_winner() -> None:
    query = single_status_query()

    assert query.result_cardinality == "one"
    assert query.evaluation_policy.conflict_behavior == "refuse_on_conflict"
    assert query.default_budgets.max_results == 1
    with pytest.raises(ValidationError, match="bound both budgets to one result"):
        single_status_query(
            default_budgets=QueryBudgetsV1(max_results=2, max_traversal_depth=0),
            maximum_budgets=QueryBudgetsV1(max_results=2, max_traversal_depth=0),
        )
    surfaced = single_status_query(
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("contradicted", "supported"),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        )
    )
    assert surfaced.evaluation_policy.conflict_behavior == "surface_conflicts"


def test_many_cardinality_queries_must_surface_rather_than_refuse_conflicts() -> None:
    with pytest.raises(ValidationError, match="surface conflicts rather than refuse"):
        active_work_query(
            evaluation_policy=QueryEvaluationPolicyV1(
                visible_verdicts=("supported",),
                visible_currency=("current",),
                conflict_behavior="refuse_on_conflict",
            )
        )


def test_verdict_policy_reuses_the_accepted_claim_verdict_and_currency_vocabulary() -> None:
    policy = QueryEvaluationPolicyV1(
        visible_verdicts=("contradicted", "stale", "supported", "unresolved"),
        visible_currency=("current", "stale"),
        conflict_behavior="surface_conflicts",
    )

    assert policy.visible_verdicts == ("contradicted", "stale", "supported", "unresolved")
    assert policy.visible_currency == ("current", "stale")
    with pytest.raises(ValidationError):
        QueryEvaluationPolicyV1(
            visible_verdicts=("believed",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        )
    with pytest.raises(ValidationError):
        QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("fresh",),
            conflict_behavior="surface_conflicts",
        )
    with pytest.raises(ValidationError, match="at least one Claim verdict"):
        QueryEvaluationPolicyV1(
            visible_verdicts=(),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        )
    with pytest.raises(ValidationError, match="sorted"):
        QueryEvaluationPolicyV1(
            visible_verdicts=("supported", "contradicted"),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        )


def test_execution_must_bind_an_explicit_accepted_coordinate_and_evaluation_time() -> None:
    policy = active_work_query().evaluation_policy

    assert policy.requires_accepted_coordinate is True
    assert policy.requires_explicit_evaluation_time is True
    assert policy.result_expiry == CanonicalDurationV1(microseconds=3_600_000_000)
    payload = policy.model_dump(mode="json")
    with pytest.raises(ValidationError):
        QueryEvaluationPolicyV1.model_validate({**payload, "requires_accepted_coordinate": False})
    with pytest.raises(ValidationError):
        QueryEvaluationPolicyV1.model_validate(
            {**payload, "requires_explicit_evaluation_time": False}
        )
    assert (
        QueryEvaluationPolicyV1.model_validate({**payload, "result_expiry": None}).result_expiry
        is None
    )


def test_query_budgets_are_explicit_bounded_and_ceilinged() -> None:
    with pytest.raises(ValidationError, match="exceed their declared ceiling"):
        active_work_query(
            default_budgets=QueryBudgetsV1(
                max_results=5000,
                max_traversal_depth=2,
                max_paths=200,
                max_paths_per_result=5,
            )
        )
    with pytest.raises(ValidationError, match="exceeds its declared depth budget"):
        active_work_query(
            default_budgets=QueryBudgetsV1(
                max_results=50,
                max_traversal_depth=0,
                max_paths=200,
                max_paths_per_result=5,
            )
        )
    with pytest.raises(ValidationError, match="path budgets"):
        single_status_query(
            default_budgets=QueryBudgetsV1(
                max_results=1, max_traversal_depth=0, max_paths=2, max_paths_per_result=1
            ),
            maximum_budgets=QueryBudgetsV1(
                max_results=1, max_traversal_depth=0, max_paths=2, max_paths_per_result=1
            ),
        )
    with pytest.raises(ValidationError, match="path budgets"):
        active_work_query(
            default_budgets=QueryBudgetsV1(max_results=50, max_traversal_depth=2),
            maximum_budgets=QueryBudgetsV1(max_results=500, max_traversal_depth=4),
        )
    with pytest.raises(ValidationError, match="declared together"):
        QueryBudgetsV1(max_results=1, max_traversal_depth=0, max_paths=2)


# -- acceptance law ------------------------------------------------------


def test_query_definition_law_accepts_a_genesis_declaration_with_resolved_pins() -> None:
    query = active_work_query()
    accepted_artifacts = {
        pin.target.qualified: (pin.target, pin.artifact_digest) for pin in query.pins
    }

    result = evaluate_query_definition_law(
        query,
        path=QUERY_PATH,
        actor_roles=("owner",),
        predecessor=None,
        accepted_artifacts=accepted_artifacts,
    )

    assert result.verdict == "accepted"
    assert result.artifact_digest == query_definition_digest(query).tagged
    assert result.required_tier == "governed_write"
    assert result.approval_scope == ("owner",)
    assert result.diagnostics == ()


def test_query_definition_law_refuses_path_pin_authority_and_predecessor_drift() -> None:
    query = active_work_query()
    codes: list[str] = []

    for candidate, kwargs in (
        (query, {"path": "query-definitions/other.yaml"}),
        (query, {"actor_roles": ("reader",)}),
        (
            active_work_query(
                lifecycle=ArtifactLifecycle(
                    state="live",
                    predecessor_digest=query_definition_digest(query).tagged,
                )
            ),
            {},
        ),
    ):
        result = evaluate_query_definition_law(
            candidate,
            path=str(kwargs.get("path", QUERY_PATH)),
            actor_roles=tuple(kwargs.get("actor_roles", ("owner",))),  # type: ignore[arg-type]
            predecessor=None,
        )
        assert result.verdict == "refused"
        assert result.artifact_digest is None
        codes.extend(item.code for item in result.diagnostics)

    unresolved = evaluate_query_definition_law(
        query,
        path=QUERY_PATH,
        actor_roles=("owner",),
        predecessor=None,
        accepted_artifacts={},
    )
    codes.append(unresolved.diagnostics[0].code)

    assert codes == [
        "playbill.query_definition.path_mismatch",
        "playbill.query_definition.actor_unauthorized",
        "playbill.query_definition.unexpected_predecessor",
        "playbill.query_definition.pin_unresolved",
    ]
    assert all(
        item.subject == SemanticAddress.whole_artifact(QUERY_PATH)
        for item in unresolved.diagnostics
    )


def test_query_definition_successor_law_binds_the_exact_live_predecessor() -> None:
    original = active_work_query()
    predecessor = accepted_query(original)
    successor = active_work_query(
        description="Active work items, reviewers, and status, ordered by item.",
        lifecycle=ArtifactLifecycle(
            state="live",
            predecessor_digest=predecessor.artifact_digest,
        ),
    )

    accepted_result = evaluate_query_definition_law(
        successor,
        path=QUERY_PATH,
        actor_roles=("owner",),
        predecessor=predecessor,
    )
    assert accepted_result.verdict == "accepted"

    stale = evaluate_query_definition_law(
        active_work_query(
            description="Drifted successor.",
            lifecycle=ArtifactLifecycle(state="live", predecessor_digest="sha256:" + "11" * 32),
        ),
        path=QUERY_PATH,
        actor_roles=("owner",),
        predecessor=predecessor,
    )
    assert stale.diagnostics[0].code == "playbill.query_definition.stale_predecessor"

    resubmitted = evaluate_query_definition_law(
        original,
        path=QUERY_PATH,
        actor_roles=("owner",),
        predecessor=predecessor,
    )
    assert resubmitted.diagnostics[0].code == "playbill.query_definition.no_semantic_change"

    other_identity = evaluate_query_definition_law(
        successor,
        path=QUERY_PATH,
        actor_roles=("owner",),
        predecessor=accepted_query(
            active_work_query(
                identity=ArtifactIdentity(kind="QueryDefinition", name="project.other_work")
            )
        ),
    )
    assert other_identity.diagnostics[0].code == (
        "playbill.query_definition.predecessor_identity_mismatch"
    )

    retired = accepted_query(
        active_work_query(
            lifecycle=ArtifactLifecycle(
                state="retired",
                predecessor_digest=predecessor.artifact_digest,
            )
        )
    )
    revived = evaluate_query_definition_law(
        active_work_query(
            description="Revived.",
            lifecycle=ArtifactLifecycle(
                state="live",
                predecessor_digest=retired.artifact_digest,
            ),
        ),
        path=QUERY_PATH,
        actor_roles=("owner",),
        predecessor=retired,
    )
    assert revived.diagnostics[0].code == "playbill.query_definition.lifecycle_invalid"

    reauthorized = evaluate_query_definition_law(
        active_work_query(
            authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("reviewer",)),
            lifecycle=ArtifactLifecycle(
                state="live",
                predecessor_digest=predecessor.artifact_digest,
            ),
        ),
        path=QUERY_PATH,
        actor_roles=("owner",),
        predecessor=predecessor,
    )
    assert reauthorized.diagnostics[0].code == (
        "playbill.query_definition.authority_change_unsupported"
    )


def test_accepted_query_definition_refuses_a_digest_or_path_that_does_not_reproduce() -> None:
    query = active_work_query()

    with pytest.raises(ValidationError, match="does not reproduce|differs from its exact envelope"):
        AcceptedQueryDefinitionV1(
            path=QUERY_PATH,
            query=query,
            artifact_digest="sha256:" + "22" * 32,
        )
    with pytest.raises(QueryDefinitionFormatError, match="identity/path disagreement"):
        AcceptedQueryDefinitionV1(
            path="query-definitions/other.yaml",
            query=query,
            artifact_digest=query_definition_digest(query).tagged,
        )


# -- closure and projection ----------------------------------------------


def test_query_definition_participates_in_dependency_closure_with_its_claim_types() -> None:
    query = active_work_query()
    tree = {
        QUERY_PATH: render_query_definition(query),
        REVIEWER_CLAIM_TYPE_PATH: render_claim_type(
            claim_type(REVIEWER_PREDICATE, object_kind="subject")
        ),
        STATUS_CLAIM_TYPE_PATH: render_claim_type(claim_type(STATUS_PREDICATE)),
    }

    state = parse_dependency_artifact(QUERY_PATH, tree[QUERY_PATH])
    assert state is not None
    assert state.artifact_kind == "query-definition"
    assert state.artifact_tag == "playbill-query-definition-v1"
    assert state.identity == query.identity
    assert state.address == SemanticAddress.whole_artifact(QUERY_PATH)

    closure = evaluate_dependency_closure(
        parent_tree={},
        candidate_tree=tree,
        scope=(REVIEWER_CLAIM_TYPE_PATH, STATUS_CLAIM_TYPE_PATH, QUERY_PATH),
    )
    assert closure.verdict == "complete"
    assert tuple((item.target_path, item.pin_role) for item in closure.proofs_for(QUERY_PATH)) == (
        (STATUS_CLAIM_TYPE_PATH, "claim-type"),
        (REVIEWER_CLAIM_TYPE_PATH, "claim-type"),
    )

    incomplete = evaluate_dependency_closure(
        parent_tree={},
        candidate_tree={
            QUERY_PATH: tree[QUERY_PATH],
            STATUS_CLAIM_TYPE_PATH: tree[STATUS_CLAIM_TYPE_PATH],
        },
        scope=(QUERY_PATH,),
    )
    assert incomplete.verdict == "refused"
    assert tuple(item.target_identity.name for item in incomplete.unresolved_pins) == (
        REVIEWER_PREDICATE,
    )
    assert incomplete.unresolved_pins[0].reason == "missing_or_digest_mismatch"


def test_query_definition_closure_refuses_a_retired_claim_type_under_a_live_query() -> None:
    query = active_work_query()
    retired_status = claim_type(STATUS_PREDICATE).model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest="sha256:" + "33" * 32,
            )
        }
    )
    tree = {
        QUERY_PATH: render_query_definition(
            active_work_query(
                pins=(
                    claim_type_pin(REVIEWER_PREDICATE, object_kind="subject"),
                    ArtifactPin(
                        role="claim-type",
                        target=retired_status.identity,
                        artifact_digest=claim_type_digest(retired_status).tagged,
                    ),
                )
            )
        ),
        REVIEWER_CLAIM_TYPE_PATH: render_claim_type(
            claim_type(REVIEWER_PREDICATE, object_kind="subject")
        ),
        STATUS_CLAIM_TYPE_PATH: render_claim_type(retired_status),
    }
    assert query.lifecycle.state == "live"

    closure = evaluate_dependency_closure(
        parent_tree={},
        candidate_tree=tree,
        scope=(QUERY_PATH,),
    )

    assert closure.verdict == "refused"
    assert closure.unresolved_pins[0].reason == "live_source_targets_retired"


def test_query_definition_projects_its_declaration_policy_and_references() -> None:
    query = active_work_query()

    projection = parse_projection_tree(
        {QUERY_PATH: render_query_definition(query)},
        registry=playbill_runtime_extension_registry(),
    )

    assert tuple((row.kind, row.identity) for row in projection.envelopes) == (
        ("query-definition", "QueryDefinition:project.active_work"),
    )
    assert projection.envelopes[0].artifact_digest == query_definition_digest(query).tagged
    assert tuple((row.source_identity, row.target_identity) for row in projection.pins) == (
        ("QueryDefinition:project.active_work", f"ClaimType:{REVIEWER_PREDICATE}"),
        ("QueryDefinition:project.active_work", f"ClaimType:{STATUS_PREDICATE}"),
    )
    facts = {fact.schema_id: fact for fact in projection.semantic_facts}
    assert {
        "playbill.query_definition.definition",
        "playbill.query_definition.policy",
        "playbill.query_definition.references",
    } <= set(facts)
    declaration = facts["playbill.query_definition.definition"].value
    assert isinstance(declaration, dict)
    assert declaration["address"] == SemanticAddress.whole_artifact(QUERY_PATH).model_dump(
        mode="json"
    )
    assert declaration["referenced_predicates"] == [REVIEWER_PREDICATE, STATUS_PREDICATE]
    assert declaration["subject_kinds"] == ["project.person", "project.work_item"]
    policy = facts["playbill.query_definition.policy"].value
    assert isinstance(policy, dict)
    assert policy["result_cardinality"] == "many"
    assert policy["evaluation_policy"] == query.evaluation_policy.model_dump(mode="json")
    assert (
        projection_registry_for_compiler(PC_D_COMPILER).supports(
            "playbill.query_definition.definition",
            1,
            classification="semantic",
        )
        is False
    )


def test_query_definition_projection_refuses_a_malformed_registered_artifact() -> None:
    with pytest.raises(QueryDefinitionFormatError):
        parse_projection_tree(
            {QUERY_PATH: b'{"artifact_format":"playbill-query-definition-v1"}\n'},
            registry=playbill_runtime_extension_registry(),
        )


# -- proposal admission --------------------------------------------------


def test_a_query_definition_change_set_is_authorable_and_law_evaluated(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    base_tree = instance.tree_at(current.git_oid)
    query = single_status_query()
    query_path = query_definition_path(query.identity.name)
    candidate_tree = {
        **base_tree,
        STATUS_CLAIM_TYPE_PATH: render_claim_type(claim_type(STATUS_PREDICATE)),
        query_path: render_query_definition(query),
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
    assert evaluation.candidate is not None
    members = {item.path: item for item in evaluation.candidate.members}
    assert set(members) == {STATUS_CLAIM_TYPE_PATH, query_path}
    member = members[query_path]
    assert member.artifact_kind == "query-definition"
    assert member.law_identifier == "playbill.query-definition.v1"
    assert member.candidate_artifact_digest == query_definition_digest(query).tagged
    law_result = next(
        item.result for item in evaluation.candidate.law_evidence if item.path == query_path
    )
    assert law_result["verdict"] == "accepted"
    assert law_result["result_cardinality"] == "one"


def test_a_query_definition_proposal_is_admitted_as_an_authorable_path(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    query = single_status_query()
    tree = {
        **instance.tree_at(base.git_oid),
        STATUS_CLAIM_TYPE_PATH: render_claim_type(claim_type(STATUS_PREDICATE)),
        query_definition_path(query.identity.name): render_query_definition(query),
    }

    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/query-definition",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )

    assert proposed.candidate is not None
    assert query_definition_path(query.identity.name) in {
        item.path for item in proposed.candidate.members
    }


def test_query_definition_grammar_never_imports_donor_query_symbols() -> None:
    import cruxible_client.contracts.query.definitions as definitions
    import cruxible_client.contracts.query.grammar as grammar

    for module in (grammar, definitions):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "cruxible_core.query" not in source
        assert "cruxible_core.config" not in source
        assert "cruxible_core.graph" not in source
        assert "cruxible_core.predicate" not in source
        assert "cruxible_core.runtime.instance" not in source


def test_query_literal_values_stay_inside_the_canonical_value_set() -> None:
    assert QueryLiteralRefV1(value={"nested": ["a", 1, True, None]}).value == {
        "nested": ["a", 1, True, None]
    }
    with pytest.raises(CanonicalEncodingError, match="floating-point"):
        QueryLiteralRefV1(value=1.5)
