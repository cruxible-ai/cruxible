"""A Claim's role and verdict as query columns: projections, filters, orderings and state taps.

``claim_field`` reads the Claims a predicate's ``claim_value`` reads, under the
same evaluation policy: the role from each statement, the verdict from the
visibility the shared verdict path admitted it under. These tests read the
columns through every surface that carries a QueryDefinition's rows (the
engine, the spec and named answers, a Procedure's state tap), check that the
verdict is the one Layer 1 serves and the verdict service derives (before and
after the Claim's evidence moves), what the columns say at a past coordinate,
and that the columns cost nothing beyond the read that already holds them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.claim_verdicts import claim_verdict_v1_compat
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinition,
    QueryDefinition,
    QueryDefinitionSpec,
    QueryEvaluationPolicy,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QUERY_CLAIM_FIELD_VALUES,
    QueryBudgets,
    QueryClaimField,
    QueryClaimFieldRef,
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryEntry,
    QueryFilter,
    QueryLiteralRef,
    QueryMembershipFilter,
    QueryOrdering,
    QueryParameterDeclaration,
    QueryParameterRef,
    QueryProjection,
    QueryProjectionField,
    QuerySubjectFieldRef,
)
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.consumers.next import facts
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.query import backends
from cruxible_core.query.engine import CLAIM_CONFLICT, VALUE_TYPE_MISMATCH, evaluate_claim_query
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.query import (
    build_accepted_query_facts,
    evaluate_accepted_query,
    service_run_playbill_query,
)
from cruxible_core.service.evidence.evidence import service_evaluate_playbill_claim_verdict
from cruxible_core.service.procedures.procedures import (
    PlaybillProcedureStateTapReader,
    state_tap_value,
)
from tests.core_support._write_support import KIND, OWNER, caller, report_evidence
from tests.core_support._write_support import seed_write_surface as _seed

WI1, WI2, WI3 = (f"{KIND}/wi-{index}" for index in (1, 2, 3))
PRIORITY, LABELS, MEASURED = (f"{KIND}.{field}" for field in ("priority", "labels", "measured"))
ALL_VERDICTS = QUERY_CLAIM_FIELD_VALUES["verdict"]


def _now() -> datetime:
    return datetime.now(UTC)


def _write(instance: PlaybillInstance, *changes: dict[str, Any]) -> WriteOutcome:
    request = WriteRequest.model_validate({"because": "The writer checked it.", "changes": changes})
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return outcome


def _set(subject: str, field: str, value: object, **extra: object) -> dict[str, Any]:
    return {"op": "set", "subject": subject, "field": field, "value": value, **extra}


@pytest.fixture
def world(tmp_path: Path) -> SimpleNamespace:
    """wi-1 observes a priority and two labels in its own words (uncovered), wi-2 judges
    a priority (normative), and wi-3 measures in its own words (uncovered)."""

    instance = _seed(tmp_path)[0]
    _write(
        instance,
        _set(WI1, "priority", "high", role="observation"),
        {"op": "add", "subject": WI1, "field": "labels", "value": "later"},
        {"op": "add", "subject": WI1, "field": "labels", "value": "urgent"},
        _set(WI2, "priority", "low", role="normative"),
    )
    measured = _write(instance, _set(WI3, "measured", 3)).changes[0].claim
    assert measured is not None
    return SimpleNamespace(instance=instance, root=tmp_path, measured=f"Claim:{measured}")


def _field(name: str, predicate: str, field: QueryClaimField) -> QueryProjectionField:
    return QueryProjectionField(
        name=name, value=QueryClaimFieldRef(binding="item", predicate=predicate, field=field)
    )


def _columns() -> tuple[QueryProjectionField, ...]:
    return (
        QueryProjectionField(
            name="item", value=QuerySubjectFieldRef(binding="item", field="subject_id")
        ),
        QueryProjectionField(
            name="priority", value=QueryClaimValueRef(binding="item", predicate=PRIORITY)
        ),
        _field("priority_role", PRIORITY, "role"),
        _field("priority_verdict", PRIORITY, "verdict"),
        _field("labels_role", LABELS, "role"),
        _field("labels_verdict", LABELS, "verdict"),
        _field("measured_verdict", MEASURED, "verdict"),
    )


def _spec(
    *fields: QueryProjectionField,
    where: QueryFilter | None = None,
    orderings: tuple[QueryOrdering, ...] = (),
    parameters: tuple[QueryParameterDeclaration, ...] = (),
    name: str = "test.claim_fields",
) -> QueryDefinitionSpec:
    return QueryDefinitionSpec.model_validate(
        dict(
            identity=ArtifactIdentity(kind="QueryDefinition", name=name),
            entry=QueryEntry(binding="item", subject_kinds=(KIND,)),
            result_binding="item",
            result_shape="subject",
            result_cardinality="many",
            dedupe="subject",
            projection=QueryProjection(
                fields=tuple(sorted(fields or _columns(), key=lambda item: item.name))
            ),
            where=where,
            orderings=orderings,
            parameters=parameters,
            evaluation_policy=QueryEvaluationPolicy(
                visible_verdicts=ALL_VERDICTS,
                visible_currency=("current", "not_applicable", "stale"),
                conflict_behavior="surface_conflicts",
            ),
            default_budgets=QueryBudgets(max_results=50, max_traversal_depth=0),
            maximum_budgets=QueryBudgets(max_results=50, max_traversal_depth=0),
        )
    )


def _accepted(
    instance: PlaybillInstance,
    spec: QueryDefinitionSpec,
    coordinate: AcceptedProjectionCoordinate | None = None,
) -> AcceptedQueryDefinition:
    """The spec as a strict definition, pinned to the ClaimTypes accepted at the coordinate."""

    with instance.bind_accepted_projection(
        coordinate or instance.accepted_coordinate()
    ) as projection:
        pins = tuple(
            ArtifactPin(
                role="claim-type",
                target=ArtifactIdentity(kind="ClaimType", name=predicate),
                artifact_digest=projection.typed.envelope("ClaimType:" + predicate).artifact_digest,
            )
            for predicate in sorted(spec.referenced_predicates)
        )
    query = QueryDefinition.model_validate({**spec.model_dump(mode="json"), "pins": pins})
    return AcceptedQueryDefinition(
        path=query_definition_path(query.identity.name),
        query=query,
        artifact_digest=query_definition_digest(query).tagged,
    )


def _rows(page: Any) -> dict[str, dict[str, Any]]:
    return {row["item"]: row for row in page.rows}


def _engine_rows(result: Any) -> list[dict[str, Any]]:
    assert result.verdict == "completed", result.refusal
    return [
        {
            field.name: (field.value if field.state == "present" else field.state)
            for field in row.fields
        }
        for row in result.rows
    ]


def _oracle(instance: PlaybillInstance, claim: str, *, at: Any = None, when: datetime) -> str:
    """The verdict service's answer for one Claim, in the query vocabulary."""

    found = service_evaluate_playbill_claim_verdict(
        instance, claim_identity=claim, evaluation_time=when, at=at
    )
    return claim_verdict_v1_compat(found.verdict).verdict


def _catch_up(instance: PlaybillInstance, at: datetime) -> None:
    part = facts._PART
    part.match(instance, now=at, daemon_id="test")
    manager = SimpleNamespace(get=lambda _id: instance)
    for work in part.due(instance, now=at):
        part.run(manager, "instance", work, now=at)


def _served_verdicts(
    instance: PlaybillInstance, at: datetime, coordinate: AcceptedProjectionCoordinate | None = None
) -> dict[str, str] | None:
    """Layer 1's per-Claim verdicts, read at one point in time (None: it cannot answer)."""

    served = facts._serving(
        instance,
        coordinate=coordinate,
        door_head=None,
        evaluation_time=at,
        read=lambda connection, _coordinate: {
            str(claim): claim_verdict_v1_compat_name(str(verdict))
            for claim, verdict in connection.execute(
                "SELECT claim_id, verdict FROM claim_facts "
                "WHERE lifecycle='live' AND verdict IS NOT NULL"
            )
        },
    )
    return None if served is None else served[0]


def claim_verdict_v1_compat_name(verdict: str) -> str:
    return "stale" if verdict == "stale_evidence" else verdict


# -- the grammar ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"), [("role", "normtive"), ("verdict", "stale_evidence"), ("role", 1)]
)
def test_a_literal_outside_the_fields_value_set_is_refused_when_declared(
    field: QueryClaimField, value: object
) -> None:
    ref = QueryClaimFieldRef(binding="item", predicate=PRIORITY, field=field)
    with pytest.raises(ValidationError, match=f"a Claim {field} is one of"):
        QueryComparisonFilter(
            left=ref, operator="eq", right=QueryLiteralRef(value=value), value_type="string"
        )
    with pytest.raises(ValidationError, match=f"a Claim {field} is one of"):
        QueryMembershipFilter(left=ref, values=(QueryLiteralRef(value=value),), value_type="string")


def test_a_claim_field_compares_and_orders_as_a_string_and_names_its_predicate() -> None:
    ref = QueryClaimFieldRef(binding="item", predicate=PRIORITY, field="role")
    with pytest.raises(ValidationError, match="compares as value_type string"):
        QueryComparisonFilter(
            left=ref,
            operator="eq",
            right=QueryLiteralRef(value="normative"),
            value_type="subject_reference",
        )
    with pytest.raises(ValidationError, match="compares as value_type string"):
        QueryOrdering(key=ref, value_type="integer")
    assert QueryOrdering(key=ref, value_type="string").key == ref
    # A definition reading a Claim field pins that predicate's ClaimType like any read.
    assert _spec(_field("priority_role", PRIORITY, "role")).referenced_predicates == (PRIORITY,)
    assert ref.vocabulary == ("derivation", "environment_binding", "normative", "observation")
    assert QUERY_CLAIM_FIELD_VALUES["verdict"] == (
        "contradicted",
        "stale",
        "supported",
        "uncovered",
        "unresolved",
    )


# -- reading the columns ----------------------------------------------------------------


def test_the_engine_projects_each_claims_role_and_verdict(world: SimpleNamespace) -> None:
    instance = world.instance
    result = evaluate_accepted_query(
        instance,
        _accepted(instance, _spec()),
        coordinate=instance.accepted_coordinate(),
        evaluation_time=_now(),
    )
    assert _engine_rows(result) == [
        {
            "item": "wi-1",
            "labels_role": ["observation"],
            "labels_verdict": ["uncovered"],
            "measured_verdict": "absent",
            "priority": "high",
            "priority_role": "observation",
            "priority_verdict": "supported",
        },
        {
            "item": "wi-2",
            "labels_role": "absent",
            "labels_verdict": "absent",
            "measured_verdict": "absent",
            "priority": "low",
            "priority_role": "normative",
            "priority_verdict": "supported",
        },
        {
            "item": "wi-3",
            "labels_role": "absent",
            "labels_verdict": "absent",
            "measured_verdict": "uncovered",
            "priority": "absent",
            "priority_role": "absent",
            "priority_verdict": "absent",
        },
    ]
    # Each verdict column is exactly the verdict the row's read_claims record.
    for row in result.rows:
        recorded = {item.predicate: item.verdict for item in row.read_claims}
        for field in row.fields:
            if field.name.endswith("_verdict") and field.state == "present":
                predicate = f"{KIND}.{field.name.removesuffix('_verdict')}"
                value = field.value if isinstance(field.value, list) else [field.value]
                assert value == [recorded[predicate]]


def test_the_answer_types_the_columns_and_serves_their_values(world: SimpleNamespace) -> None:
    page = service_playbill_query(world.instance, request=QueryRequest(spec=_spec()))
    columns = {column.name: column for column in page.columns}
    assert (columns["priority_role"].type, columns["priority_role"].members) == (
        "string",
        QUERY_CLAIM_FIELD_VALUES["role"],
    )
    assert columns["priority_role"].predicate == PRIORITY
    assert columns["labels_verdict"].members == ALL_VERDICTS
    assert columns["labels_verdict"].cardinality == "many"
    rows = _rows(page)
    assert (rows["wi-1"]["priority_role"], rows["wi-2"]["priority_role"]) == (
        "observation",
        "normative",
    )
    assert rows["wi-1"]["labels_verdict"] == ["uncovered"]
    assert rows["wi-3"]["measured_verdict"] == "uncovered"
    assert rows["wi-3"]["priority_role"] is None


def test_where_filters_on_role_and_verdict(world: SimpleNamespace) -> None:
    role = QueryClaimFieldRef(binding="item", predicate=PRIORITY, field="role")
    judged = _spec(
        where=QueryComparisonFilter(
            left=role, operator="eq", right=QueryLiteralRef(value="normative"), value_type="string"
        )
    )
    assert set(
        _rows(service_playbill_query(world.instance, request=QueryRequest(spec=judged)))
    ) == {"wi-2"}
    uncovered = _spec(
        where=QueryMembershipFilter(
            left=QueryClaimFieldRef(binding="item", predicate=MEASURED, field="verdict"),
            values=(QueryLiteralRef(value="stale"), QueryLiteralRef(value="uncovered")),
            value_type="string",
        )
    )
    assert set(
        _rows(service_playbill_query(world.instance, request=QueryRequest(spec=uncovered)))
    ) == {"wi-3"}
    # ne: a Subject without the Claim has no field, so it never matches a comparison.
    sourced = _spec(
        where=QueryComparisonFilter(
            left=role, operator="ne", right=QueryLiteralRef(value="normative"), value_type="string"
        )
    )
    assert set(
        _rows(service_playbill_query(world.instance, request=QueryRequest(spec=sourced)))
    ) == {"wi-1"}


def test_orderings_sort_by_role_with_absent_fields_last(world: SimpleNamespace) -> None:
    role = QueryClaimFieldRef(binding="item", predicate=PRIORITY, field="role")
    for direction, expected in (
        ("ascending", ["wi-2", "wi-1", "wi-3"]),
        ("descending", ["wi-1", "wi-2", "wi-3"]),
    ):
        spec = _spec(orderings=(QueryOrdering(key=role, direction=direction, value_type="string"),))
        page = service_playbill_query(world.instance, request=QueryRequest(spec=spec))
        assert [row["item"] for row in page.rows] == expected, direction


def test_a_parameter_outside_the_fields_value_set_refuses_the_run(world: SimpleNamespace) -> None:
    instance = world.instance
    spec = _spec(
        where=QueryComparisonFilter(
            left=QueryClaimFieldRef(binding="item", predicate=PRIORITY, field="role"),
            operator="eq",
            right=QueryParameterRef(parameter="role"),
            value_type="string",
        ),
        parameters=(QueryParameterDeclaration(name="role", value_type="string"),),
    )
    definition = _accepted(instance, spec)

    def run(role: str) -> Any:
        return evaluate_accepted_query(
            instance,
            definition,
            coordinate=instance.accepted_coordinate(),
            evaluation_time=_now(),
            parameters={"role": role},
        )

    assert [row["item"] for row in _engine_rows(run("observation"))] == ["wi-1"]
    refused = run("analyst")
    assert refused.verdict == "refused" and refused.refusal is not None
    assert refused.refusal.code == VALUE_TYPE_MISMATCH


def test_a_named_query_and_a_state_tap_carry_the_columns(world: SimpleNamespace) -> None:
    instance = world.instance
    definition = _accepted(instance, _spec(name="test.claim_fields_named"))
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree[definition.path] = render_query_definition(definition.query)
    proposed = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/claim-fields-query", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp=_now().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    at = _now()

    run = service_run_playbill_query(
        instance, name=definition.query.identity.name, evaluation_time=at
    )
    named = _engine_rows(run.result)
    assert [(row["item"], row["priority_role"]) for row in named] == [
        ("wi-1", "observation"),
        ("wi-2", "normative"),
        ("wi-3", "absent"),
    ]
    page = service_playbill_query(
        instance, request=QueryRequest(name=definition.query.identity.name, evaluation_time=at)
    )
    assert _rows(page)["wi-2"]["priority_role"] == "normative"

    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    reader = PlaybillProcedureStateTapReader(instance=instance, evaluation_time=at)
    tapped = reader.read_accepted_state(
        query=ArtifactPin(
            role="state-tap",
            target=definition.query.identity,
            artifact_digest=definition.artifact_digest,
        ),
        parameters={},
        coordinate=coordinate,
    ).value
    assert tapped == state_tap_value(run.result)
    fields = [
        {item["name"]: item.get("value") for item in row["fields"]}
        for row in tapped["rows"]  # type: ignore[index]
    ]
    assert [(row["item"], row["priority_role"], row["priority_verdict"]) for row in fields] == [
        ("wi-1", "observation", "supported"),
        ("wi-2", "normative", "supported"),
        ("wi-3", None, None),
    ]


# -- the verdict moves with the facts, at the head and at a past coordinate ------------


def test_the_verdict_column_is_what_layer_1_serves_and_the_verdict_service_derives(
    world: SimpleNamespace,
) -> None:
    instance = world.instance
    spec = _spec(
        _field("measured_verdict", MEASURED, "verdict"),
        _field("priority_verdict", PRIORITY, "verdict"),
        QueryProjectionField(
            name="item", value=QuerySubjectFieldRef(binding="item", field="subject_id")
        ),
    )

    def column(at: datetime, coordinate: Any = None) -> dict[str, Any]:
        page = service_playbill_query(
            instance,
            request=QueryRequest(
                spec=spec,
                evaluation_time=at,
                at=None if coordinate is None else AcceptedCoordinate.from_internal(coordinate),
            ),
        )
        return _rows(page)

    first = _now()
    _catch_up(instance, first)
    before = instance.accepted_coordinate()
    served = _served_verdicts(instance, first)
    assert served is not None
    assert column(first)["wi-3"]["measured_verdict"] == "uncovered"
    assert served[world.measured] == _oracle(instance, world.measured, when=first) == "uncovered"

    # New evidence revises the Claim: the column follows the facts.
    revised = _write(
        instance,
        _set(WI3, "measured", 4, evidence=report_evidence(world.root, "Count: 4")),
    ).changes[0]
    assert f"Claim:{revised.claim}" == world.measured
    later = _now()
    _catch_up(instance, later)
    served = _served_verdicts(instance, later)
    assert served is not None
    assert column(later)["wi-3"]["measured_verdict"] == "supported"
    assert served[world.measured] == _oracle(instance, world.measured, when=later) == "supported"
    for claim, verdict in served.items():
        assert verdict == _oracle(instance, claim, when=later), claim

    # At the past coordinate the column is that coordinate's verdict, derived by the
    # evaluation there; Layer 1's cursors have passed it, so it has no answer.
    at_before = AcceptedCoordinate.from_internal(before)
    assert column(later, before)["wi-3"]["measured_verdict"] == "uncovered"
    assert _oracle(instance, world.measured, at=at_before, when=later) == "uncovered"
    assert _served_verdicts(instance, later, before) is None


# -- conflicts --------------------------------------------------------------------------


def _with_second_role(
    instance: PlaybillInstance, facts_read: backends.ClaimQueryFactsV1
) -> backends.ClaimQueryFactsV1:
    """wi-1's priority stated a second time, same value, by another role."""

    from cruxible_client.contracts.claims import (
        AcceptedClaim,
        claim_artifact_digest,
        claim_path,
        claim_statement_digest,
    )

    (row,) = (
        item
        for item in facts_read.claims
        if item.accepted.claim.statement.predicate == PRIORITY
        and item.subject_path.endswith("/wi-1.json")
    )
    claim = row.accepted.claim
    twin = claim.model_copy(
        update={
            "identity": ArtifactIdentity(kind="Claim", name="CLM-" + "0" * 32),
            "statement": claim.statement.model_copy(update={"role": "normative"}),
        }
    )
    second = row.model_copy(
        update={
            # A fixture row, never an accepted artifact: its citations still name
            # the original Claim, which only the artifact law checks.
            "accepted": AcceptedClaim.model_construct(
                path=claim_path(twin.identity.name),
                claim=twin,
                statement_digest=claim_statement_digest(twin.statement).tagged,
                artifact_digest=claim_artifact_digest(twin).tagged,
            )
        }
    )
    claims = sorted(
        (*facts_read.claims, second), key=lambda item: item.accepted.path.encode("utf-8")
    )
    return facts_read.model_copy(update={"claims": tuple(claims)})


@pytest.mark.parametrize("conflict", ["surface_conflicts", "refuse_on_conflict"])
def test_claims_agreeing_on_a_value_but_not_a_role_state_no_single_role(
    world: SimpleNamespace, conflict: str
) -> None:
    instance = world.instance
    coordinate = instance.accepted_coordinate()
    spec = _spec(
        QueryProjectionField(
            name="priority", value=QueryClaimValueRef(binding="item", predicate=PRIORITY)
        ),
        _field("priority_role", PRIORITY, "role"),
        where=QueryComparisonFilter(
            left=QuerySubjectFieldRef(binding="item", field="subject_id"),
            operator="eq",
            right=QueryLiteralRef(value="wi-1"),
            value_type="string",
        ),
    )
    if conflict == "refuse_on_conflict":
        # Refusing on a conflict is a one-cardinality read's choice.
        declared = spec.model_dump(mode="json")
        declared["evaluation_policy"]["conflict_behavior"] = conflict
        spec = QueryDefinitionSpec.model_validate(
            {
                **declared,
                "result_cardinality": "one",
                "default_budgets": {"max_results": 1, "max_traversal_depth": 0},
                "maximum_budgets": {"max_results": 1, "max_traversal_depth": 0},
            }
        )
    definition = _accepted(instance, spec)
    doubled = _with_second_role(
        instance,
        build_accepted_query_facts(
            instance,
            coordinate=coordinate,
            predicates=definition.query.referenced_predicates,
            subject_kinds=(KIND,),
        ),
    )
    result = evaluate_claim_query(
        definition, facts=doubled, coordinate=coordinate, evaluation_time=_now()
    )
    if conflict == "refuse_on_conflict":
        assert result.verdict == "refused" and result.refusal is not None
        assert result.refusal.code == CLAIM_CONFLICT
        assert len(result.refusal.statement_digests) == 2
        return
    first = _engine_rows(result)[0]
    # The value is one value; which role states it has no single answer.
    assert (first["priority"], first["priority_role"]) == ("high", "conflict")


# -- cost ---------------------------------------------------------------------------------


@contextmanager
def _verdict_evaluations() -> Iterator[list[str]]:
    """Every verdict the backend derives, and a Layer 1 read refused outright."""

    calls: list[str] = []
    original = backends.evaluate_claim_verdict

    def counted(**kwargs: Any) -> Any:
        calls.append(str(kwargs["claim_statement_digest"]))
        return original(**kwargs)

    def no_layer_1(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a query column read Layer 1")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(backends, "evaluate_claim_verdict", counted)
        patch.setattr(facts, "_serving", no_layer_1)
        yield calls


def test_the_columns_cost_nothing_beyond_the_read_that_holds_them(world: SimpleNamespace) -> None:
    """A role or verdict column reads no Claim, verdict or state the value read does not.

    The verdict is the one the shared verdict path already derived to decide the
    Claim's visibility, so the column is sized by the rows it answers: the same
    verdict derivations, the same Claims read per row, with or without it.
    """

    instance = world.instance
    coordinate = instance.accepted_coordinate()
    at = _now()
    values = _spec(
        QueryProjectionField(
            name="item", value=QuerySubjectFieldRef(binding="item", field="subject_id")
        ),
        QueryProjectionField(
            name="priority", value=QueryClaimValueRef(binding="item", predicate=PRIORITY)
        ),
        QueryProjectionField(
            name="labels", value=QueryClaimValueRef(binding="item", predicate=LABELS)
        ),
    )
    with_fields = _spec(
        *values.projection.fields,  # type: ignore[union-attr]
        _field("labels_role", LABELS, "role"),
        _field("labels_verdict", LABELS, "verdict"),
        _field("priority_role", PRIORITY, "role"),
        _field("priority_verdict", PRIORITY, "verdict"),
    )
    facts_read = build_accepted_query_facts(
        instance, coordinate=coordinate, predicates=(LABELS, PRIORITY), subject_kinds=(KIND,)
    )
    results = {}
    derived = {}
    for name, spec in (("values", values), ("fields", with_fields)):
        definition = _accepted(instance, spec)
        with _verdict_evaluations() as calls:
            results[name] = evaluate_claim_query(
                definition, facts=facts_read, coordinate=coordinate, evaluation_time=at
            )
        derived[name] = sorted(calls)
    assert derived["fields"] == derived["values"]
    # One derivation per Claim of the read predicates, whatever the columns.
    assert len(derived["values"]) == len(facts_read.claims)
    assert [row.read_claims for row in results["fields"].rows] == [
        row.read_claims for row in results["values"].rows
    ]
