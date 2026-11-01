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
                "output": {"count": "integer"},
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
