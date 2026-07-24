"""Procedure provider-call and wall-clock budget enforcement."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import ProviderSchema
from cruxible_core.errors import ProcedureBudgetExceededError
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.provider.registry import resolve_provider
from cruxible_core.provider.types import ProviderContext
from cruxible_core.service import (
    service_accept_procedure,
    service_propose_procedure,
    service_run_procedure,
)
from tests.test_procedures.conftest import actor


def _accept(instance: CruxibleInstance, definition: ProcedureDefinition) -> str:
    proposed = service_propose_procedure(
        instance,
        definition,
        actor_context=actor("proposer"),
    )
    accepted = service_accept_procedure(
        instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer"),
    )
    return accepted.procedure.procedure_id


def _repeat_definition(name: str, *, wall_clock_s: float = 30) -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            "name": name,
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 3,
                        "until": {
                            "left": "$steps.result.value",
                            "op": "eq",
                            "right": 3,
                            "message": "done",
                        },
                        "steps": [
                            {
                                "id": "invoke",
                                "provider": "exported_action",
                                "input": {"value": "$input.value"},
                                "as": "result",
                            }
                        ],
                    },
                    "as": "attempt",
                }
            ],
            "returns": "attempt",
            "precondition": {},
            "budget": {
                "wall_clock_s": wall_clock_s,
                "max_provider_calls": 3,
            },
            "declared_tier": "graph_write",
        }
    )


def test_provider_call_budget_counts_every_repeat_invocation_exactly(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procedure_id = _accept(
        procedure_instance,
        _repeat_definition("exact_provider_call_accounting"),
    )
    values = iter([1, 2, 3])

    def resolve_stub(*args: Any, **kwargs: Any) -> Any:
        def invoke(payload: dict[str, Any], context: Any) -> dict[str, Any]:
            return {"value": next(values)}

        return invoke

    monkeypatch.setattr("cruxible_core.workflow.io.resolve_provider", resolve_stub)

    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        {"value": 0},
        actor("runner"),
    )

    assert result.run.budget_spent.provider_calls == 3
    assert result.receipt.nodes[0].detail["budget"]["spent"]["provider_calls"] == 3
    assert result.output["attempt_count"] == 3


def test_wall_clock_budget_is_threaded_as_minimum_transport_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, int]:
            return {"value": 1}

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("cruxible_core.provider.registry.httpx.Client", FakeClient)
    provider = ProviderSchema(
        contract_in="cruxible.JsonObject",
        contract_out="cruxible.JsonObject",
        ref="https://example.test/action",
        version="1",
        runtime="http_json",
        config={"timeout_s": 9},
    )
    context = ProviderContext(
        workflow_name="procedure",
        step_id="invoke",
        provider_name="action",
        provider_version="1",
        provider_config={},
        deterministic=True,
    )

    output = resolve_provider(
        "action",
        provider,
        timeout_ceiling_s=2.5,
    )({"value": 1}, context)

    assert output == {"value": 1}
    assert captured["timeout"] == 2.5


def test_timeout_ceiling_is_applied_to_command_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(
            args=["action"],
            returncode=0,
            stdout=json.dumps({"value": 1}),
            stderr="",
        )

    monkeypatch.setattr("cruxible_core.provider.registry.subprocess.run", fake_run)
    provider = ProviderSchema(
        contract_in="cruxible.JsonObject",
        contract_out="cruxible.JsonObject",
        ref="/usr/bin/action",
        version="1",
        runtime="command",
        config={"timeout_s": 12},
    )
    context = ProviderContext(
        workflow_name="procedure",
        step_id="invoke",
        provider_name="action",
        provider_version="1",
        provider_config={},
        deterministic=True,
    )

    output = resolve_provider(
        "action",
        provider,
        timeout_ceiling_s=3,
    )({"value": 1}, context)

    assert output == {"value": 1}
    assert captured["timeout"] == 3


def test_wall_clock_breach_finalizes_budget_exceeded_with_annotation(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procedure_id = _accept(
        procedure_instance,
        _repeat_definition("wall_clock_exceeded", wall_clock_s=0.1),
    )

    def resolve_slow(*args: Any, **kwargs: Any) -> Any:
        assert 0 < kwargs["timeout_ceiling_s"] <= 0.1

        def invoke(payload: dict[str, Any], context: Any) -> dict[str, Any]:
            time.sleep(0.12)
            return {"value": 1}

        return invoke

    monkeypatch.setattr("cruxible_core.workflow.io.resolve_provider", resolve_slow)

    with pytest.raises(ProcedureBudgetExceededError) as exc_info:
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": 0},
            actor("runner"),
        )

    store = procedure_instance.get_procedure_store()
    try:
        run = store.get_run(getattr(exc_info.value, "procedure_run_id"))
    finally:
        store.close()
    assert run is not None
    assert run.status == "finalized"
    assert run.verdict == "budget_exceeded"
    assert run.budget_spent.provider_calls == 1
    assert run.receipt_id is not None

    receipt_store = procedure_instance.get_receipt_store()
    try:
        receipt = receipt_store.get_receipt(run.receipt_id)
    finally:
        receipt_store.close()
    assert receipt is not None
    assert receipt.nodes[0].detail["budget_exceeded"] is True
    assert receipt.nodes[0].detail["verdict"] == "budget_exceeded"
