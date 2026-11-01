"""Query Contracts are enforced where a Procedure reads: parameters before, rows after.

A QueryDefinition may pin a ``parameter_contract`` and a ``row_contract``
(``name@N`` with the exact digest lowering adds). Acceptance proves only that
each Contract exists and names declared parameters (projected fields); the
values are checked at runtime, for a Procedure ``read``. An interactive query
over the same definition keeps its behavior.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.procedures.authored import ProcedureSpec
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.shapes import (
    ContractRef,
    contract_digest,
    contract_path,
    render_contract,
    validate_contract,
)
from cruxible_core.service.discovery.query import service_run_playbill_query
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRunRequest,
    service_run_playbill_procedure,
)
from tests.core_support._identity_support import accept_tree
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    seed_claims,
    work_item_query,
)
from tests.support.operation_world import actor, author_procedure

NOW = datetime.fromisoformat(EVALUATION_TIME)
ROW = "test.work-row"


def _world(tmp_path: Path, row_fields: dict[str, str]) -> tuple[Any, str]:
    instance, _owner = seed_claims(tmp_path)
    row = validate_contract({"name": ROW, "version": 1, "fields": row_fields})
    query = work_item_query().model_copy(
        update={
            "row_contract": ContractRef(name=ROW, version=1, digest=contract_digest(row).tagged)
        }
    )
    accept_tree(
        instance,
        {
            contract_path(ROW, 1): render_contract(row),
            query_definition_path(query.identity.name): render_query_definition(query),
        },
        name="row-contract-query",
    )
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.read-work",
                "input": {},
                "output": {"count": "int"},
                "steps": [
                    {"read": "work", "query": query.identity.name},
                    {"project": "out", "fields": {"count": 2}},
                ],
                "returns": "out",
            }
        ),
    )
    return instance, query.identity.name


def _run(instance: Any) -> Any:
    return service_run_playbill_procedure(
        instance,
        name="test.read-work",
        request=ProcedureRunRequest(input={}, evaluation_time=NOW),
        actor_context=actor().model_copy(update={"timestamp": NOW}),
    )


def _terminal(state: Any) -> dict[str, Any]:
    return {} if state.terminal is None else state.terminal.model_dump(mode="json")


def test_a_read_whose_rows_satisfy_the_row_contract_runs(tmp_path: Path) -> None:
    instance, _query = _world(tmp_path, {"item_id": "string", "status": "string"})

    state = _run(instance)

    assert state.status == "succeeded", _terminal(state)


def test_a_read_whose_row_breaks_the_row_contract_refuses_naming_the_field(
    tmp_path: Path,
) -> None:
    instance, query = _world(tmp_path, {"item_id": "int", "status": "string"})

    state = _run(instance)

    assert state.status != "succeeded"
    terminal = str(_terminal(state))
    assert "query_contract_violated" in terminal
    assert "item_id" in terminal and f"{ROW}@1" in terminal
    # An interactive query over the same definition keeps its behavior.
    interactive = service_run_playbill_query(
        instance, name=query, evaluation_time=NOW, parameters={}
    )
    assert interactive.result.verdict == "completed"
    assert len(interactive.result.rows) == 2


def test_parameters_that_break_the_parameter_contract_refuse_before_the_query_runs() -> None:
    from cruxible_core.procedures.execution import QueryContractViolated
    from cruxible_core.service.procedures.procedures import enforce_query_parameter_contract

    contract = validate_contract({"name": "test.params", "version": 1, "fields": {"limit": "int"}})
    enforce_query_parameter_contract({"limit": 3}, contract, lambda _ref: None)
    with pytest.raises(QueryContractViolated) as refused:
        enforce_query_parameter_contract({"limit": "three"}, contract, lambda _ref: None)
    assert refused.value.direction == "parameters"
    assert refused.value.details["field"] == "limit"


# -- the effective parameter record, and enum_of members (review fixes) --------------

STATUS = "project.work_item.status"


def _status_query(contract_fields: dict[str, str]) -> tuple[Any, Any]:
    """The work-item Query filtered by an optional ``status`` parameter (default ``ready``)."""

    from cruxible_client.contracts.query.grammar import (
        QueryClaimValueRef,
        QueryComparisonFilter,
        QueryParameterDeclaration,
        QueryParameterRef,
    )

    params = validate_contract(
        {"name": "test.status-params", "version": 1, "fields": contract_fields}
    )
    query = work_item_query().model_copy(
        update={
            "parameters": (
                QueryParameterDeclaration(
                    name="status", value_type="string", required=False, default="ready"
                ),
            ),
            "where": QueryComparisonFilter(
                left=QueryClaimValueRef(binding="item", predicate=STATUS),
                operator="eq",
                right=QueryParameterRef(parameter="status"),
                value_type="string",
            ),
            "parameter_contract": ContractRef(
                name=params.name, version=1, digest=contract_digest(params).tagged
            ),
        }
    )
    return params, type(query).model_validate(query.model_dump(mode="python", by_alias=True))


def _param_world(tmp_path: Path, contract_fields: dict[str, str]) -> Any:
    instance, _owner = seed_claims(tmp_path)
    params, query = _status_query(contract_fields)
    accept_tree(
        instance,
        {
            contract_path(params.name, 1): render_contract(params),
            query_definition_path(query.identity.name): render_query_definition(query),
        },
        name="parameter-contract-query",
    )
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.read-work",
                "input": {},
                "output": {"count": "int"},
                "steps": [
                    {"read": "work", "query": query.identity.name},
                    {"project": "out", "fields": {"count": 1}},
                ],
                "returns": "out",
            }
        ),
    )
    return instance


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        # A required Contract field is satisfied by the Query's default.
        ({"status": "string"}, "succeeded"),
        # The default is a member of the ClaimType's enum: enum_of resolves.
        ({"status": f"enum_of:{STATUS}"}, "succeeded"),
        # The default breaks the refinement: checked although the caller omitted it.
        ({"status": "string{min_len: 6}?"}, "admission_refused"),
    ],
    ids=["default-satisfies-required", "default-in-enum", "default-breaks-refinement"],
)
def test_a_read_checks_the_effective_parameters_with_the_querys_defaults(
    tmp_path: Path, fields: dict[str, str], status: str
) -> None:
    instance = _param_world(tmp_path, fields)

    state = _run(instance)

    assert state.status == status, _terminal(state)
    if status != "succeeded":
        terminal = str(_terminal(state))
        assert "query_contract_violated" in terminal and "status" in terminal


def test_a_row_contract_resolves_enum_of_claim_type_members(tmp_path: Path) -> None:
    instance, _query = _world(tmp_path, {"item_id": "string", "status": f"enum_of:{STATUS}"})

    state = _run(instance)

    assert state.status == "succeeded", _terminal(state)


def test_reference_parameters_are_checked_in_their_normalized_spelling() -> None:
    from cruxible_client.contracts.query.grammar import QueryParameterDeclaration
    from cruxible_core.service.procedures.procedures import (
        effective_query_parameters,
        enforce_query_parameter_contract,
    )

    query = work_item_query().model_copy(
        update={
            "parameters": (QueryParameterDeclaration(name="owner", value_type="subject_reference"),)
        }
    )
    effective = effective_query_parameters(query, {"owner": "project.work_item/wi-42"})
    assert effective == {"owner": "Subject:project.work_item/wi-42"}
    contract = validate_contract(
        {"name": "test.owner", "version": 1, "fields": {"owner": 'string{pattern: "Subject:.+"}'}}
    )
    enforce_query_parameter_contract(effective, contract, lambda _ref: None)


def _enum(members: dict[str, tuple[str, ...]]) -> Any:
    return lambda claim_type: members.get(claim_type)


def test_enum_of_members_are_checked_in_parameters_and_nested_contracts() -> None:
    from cruxible_core.procedures.execution import QueryContractViolated
    from cruxible_core.service.procedures.procedures import enforce_query_parameter_contract

    inner = validate_contract(
        {"name": "test.inner", "version": 1, "fields": {"s": f"enum_of:{STATUS}"}}
    )
    outer = validate_contract(
        {
            "name": "test.outer",
            "version": 1,
            "fields": {"s": f"enum_of:{STATUS}", "n": "test.inner@1"},
        }
    )
    resolve = lambda ref: inner if ref.name == "test.inner" else None  # noqa: E731
    members = _enum({STATUS: ("blocked", "done", "ready")})
    enforce_query_parameter_contract({"s": "ready", "n": {"s": "done"}}, outer, resolve, members)
    with pytest.raises(QueryContractViolated) as top:
        enforce_query_parameter_contract({"s": "lost", "n": {"s": "done"}}, outer, resolve, members)
    assert top.value.details["field"] == "s"
    with pytest.raises(QueryContractViolated) as nested:
        enforce_query_parameter_contract(
            {"s": "ready", "n": {"s": "lost"}}, outer, resolve, members
        )
    assert nested.value.details["field"] == "n"


def test_enum_of_members_are_checked_in_rows_and_nested_contracts() -> None:
    from types import SimpleNamespace

    from cruxible_core.procedures.execution import QueryContractViolated
    from cruxible_core.service.procedures.procedures import enforce_query_row_contract

    inner = validate_contract(
        {"name": "test.inner", "version": 1, "fields": {"s": f"enum_of:{STATUS}"}}
    )
    row = validate_contract(
        {
            "name": "test.row",
            "version": 1,
            "fields": {"s": f"enum_of:{STATUS}", "n": "test.inner@1"},
        }
    )
    resolve = lambda ref: inner if ref.name == "test.inner" else None  # noqa: E731
    members = _enum({STATUS: ("blocked", "done", "ready")})

    def result(s: str, nested: str) -> Any:
        fields = (
            SimpleNamespace(name="s", state="present", value=s),
            SimpleNamespace(name="n", state="present", value={"s": nested}),
        )
        return SimpleNamespace(rows=(SimpleNamespace(fields=fields),))

    enforce_query_row_contract(result("ready", "done"), row, resolve, members)
    for s, nested, field in (("lost", "done", "s"), ("ready", "lost", "n")):
        with pytest.raises(QueryContractViolated) as refused:
            enforce_query_row_contract(result(s, nested), row, resolve, members)
        assert refused.value.details["field"] == field and refused.value.details["row"] == 0
