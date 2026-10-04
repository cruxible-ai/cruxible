"""A named query's replay through the served ``query`` verb, as the old run_query gave it.

The SDK's ``run_query`` and a declared block's query backing read a named
query's full receipt through ``query``. These run against a real daemon (an
in-process HTTP app over a seeded instance), never a fake, because the
properties under test live in the service: which parameter bindings it admits
and which budgets it runs.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cruxible_client import Cruxible, CruxibleClient
from cruxible_client.authoring.blocks import _query_backing
from cruxible_client.contracts.compact_query import QueryRequest, QueryResultRecord
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.definitions import QueryDefinition, QueryDefinitionSpec
from cruxible_client.contracts.query.grammar import (
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryParameterDeclaration,
    QueryParameterRef,
)
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service.discovery import compact_query as compact_module
from tests.core_support._candidate_support import submit_query_definition_candidate
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)

BY_STATUS = "project.work_items_by_status"
BARE = "project.work_items_bare"
WHEN = datetime.fromisoformat(EVALUATION_TIME)


def _by_status_query() -> QueryDefinition:
    """The work-item read filtered by an optional status parameter (default ``ready``)."""

    declared = work_item_query(BY_STATUS)
    return declared.model_validate(
        {
            **declared.model_dump(mode="json"),
            "parameters": (
                QueryParameterDeclaration(
                    name="optional_status", value_type="string", required=False, default="ready"
                ).model_dump(mode="json"),
            ),
            "where": QueryComparisonFilter(
                left=QueryClaimValueRef(binding="item", predicate=PREDICATE),
                operator="eq",
                right=QueryParameterRef(parameter="optional_status"),
                value_type="string",
            ).model_dump(mode="json"),
        }
    )


def _bare_query() -> QueryDefinition:
    """The work-item read with no projection: rows render the Subjects' own cells."""

    declared = work_item_query(BARE)
    return declared.model_validate(
        {**declared.model_dump(mode="json"), "projection": None, "pins": []}
    )


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[CruxibleClient, str, Any]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    playbill_api.get_playbill_manager().clear()
    http = TestClient(create_app())
    instance, owner = seed_claims(tmp_path)
    for query in (work_item_query(), _by_status_query(), _bare_query()):
        accept_proposal(
            instance,
            owner,
            submit_query_definition_candidate(
                instance,
                query=query,
                actor_id="owner",
                proposal_name=query.identity.name.replace(".", "-"),
                timestamp=TIMESTAMP,
            ),
        )
    instance_id = instance.descriptor.instance_id
    get_registry().create_governed_instance_with_id(instance_id)
    monkeypatch.setattr(playbill_api.get_playbill_manager(), "get", lambda _: instance)
    client = CruxibleClient(base_url="http://testserver")
    client._client.close()
    client._client = http  # type: ignore[assignment]
    return client, instance_id, instance


def _sdk(client: CruxibleClient, instance_id: str, tmp_path: Path) -> Cruxible:
    return Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id=instance_id,
        workspace=tmp_path,
        clock=lambda: WHEN,
    )


def _coordinate(instance: Any) -> AcceptedCoordinate:
    return AcceptedCoordinate.from_internal(instance.accepted_coordinate())


# -- F-006: an explicit null binds an optional parameter -----------------------


def test_the_sdk_binds_an_explicit_null_distinct_from_the_default(
    served: tuple[CruxibleClient, str, Any], tmp_path: Path
) -> None:
    client, instance_id, _instance = served
    pb = _sdk(client, instance_id, tmp_path)
    binding = pb.query_binding(BY_STATUS)

    explicit = pb.run_query(binding, parameters=binding.parameters(optional_status=None))
    defaulted = pb.run_query(binding)

    assert explicit.result.verdict == "completed"
    assert [(item.name, item.value) for item in explicit.result.parameters] == [
        ("optional_status", None)
    ]
    assert [(item.name, item.value) for item in defaulted.result.parameters] == [
        ("optional_status", "ready")
    ]
    assert explicit.receipt.parameter_digest != defaulted.receipt.parameter_digest


def test_http_admits_a_null_parameter_and_the_definition_decides(
    served: tuple[CruxibleClient, str, Any],
) -> None:
    client, instance_id, _instance = served
    response = client._client.post(
        f"/api/v1/{instance_id}/playbill/query",
        json={
            "name": BY_STATUS,
            "params": {"optional_status": None},
            "receipt": "full",
            "evaluation_time": EVALUATION_TIME,
        },
    )
    assert response.status_code == 200, response.text
    page = QueryResultRecord.model_validate(response.json())
    assert page.receipt.replay is not None
    assert page.receipt.replay.result.parameters[0].value is None

    # A null for a parameter the definition does not declare still refuses there.
    refused = client._client.post(
        f"/api/v1/{instance_id}/playbill/query",
        json={"name": BY_STATUS, "params": {"undeclared": None}},
    )
    assert refused.status_code == 400
    assert refused.json()["error_code"] == "playbill.query.parameter_undeclared"


def test_mcp_admits_a_null_parameter(
    served: tuple[CruxibleClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.mcp import handlers

    client, instance_id, _instance = served
    monkeypatch.setattr(handlers, "_get_client", lambda: client)
    page = handlers.handle_playbill_query(
        instance_id,
        name=BY_STATUS,
        params={"optional_status": None},
        receipt="full",
        evaluation_time=EVALUATION_TIME,
    )
    assert page.receipt.replay is not None
    assert page.receipt.replay.result.parameters[0].value is None


def test_the_cli_reads_json_null_as_an_explicit_null() -> None:
    from cruxible_core.cli.commands.playbill import _query_param_value

    assert _query_param_value("null") is None
    assert _query_param_value('"null"') == "null"
    assert _query_param_value("ready") == "ready"


def test_a_block_backing_pins_an_explicit_null_binding(
    served: tuple[CruxibleClient, str, Any],
) -> None:
    client, instance_id, instance = served
    backing = _query_backing(
        client,
        instance_id,
        name=f"QueryDefinition:{BY_STATUS}",
        parameters={"optional_status": None},
        coordinate=_coordinate(instance),
        evaluation_time=WHEN,
    )
    defaulted = _query_backing(
        client,
        instance_id,
        name=f"QueryDefinition:{BY_STATUS}",
        parameters={},
        coordinate=_coordinate(instance),
        evaluation_time=WHEN,
    )
    assert [(item.name, item.value) for item in backing.resolved_parameter_bindings] == [
        ("optional_status", None)
    ]
    assert backing.canonical_param_digest != defaulted.canonical_param_digest


def test_the_request_model_admits_null_params_only() -> None:
    request = QueryRequest.model_validate({"name": BY_STATUS, "params": {"optional_status": None}})
    assert request.params == {"optional_status": None}
    with pytest.raises(ValueError):
        QueryRequest.model_validate({"name": BY_STATUS, "params": {"x": [1]}})


# -- F-004: a replay runs the definition's declared budgets ---------------------


def test_a_replay_runs_declared_budgets_past_the_surface_ceiling(
    served: tuple[CruxibleClient, str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_query's result and digest do not move with the compact surface's ceiling."""

    client, instance_id, _instance = served
    pb = _sdk(client, instance_id, tmp_path)
    uncapped = pb.run_query(QUERY_NAME)
    assert len(uncapped.result.rows) == 2

    monkeypatch.setattr(compact_module, "COMPACT_QUERY_MAX_RESULTS", 1)
    replayed = pb.run_query(QUERY_NAME)

    assert replayed.result.verdict == "completed"
    assert replayed.result.truncation.clipped_budgets == ()
    assert replayed.result.budgets.max_results == work_item_query().default_budgets.max_results
    assert replayed.result == uncapped.result
    assert replayed.receipt == uncapped.receipt
    # The compact page is still held under the ceiling.
    compact = pb.query(name=QUERY_NAME).page
    assert compact.capped == ("max_results=1",)


def test_a_block_backing_repins_a_query_past_the_surface_ceiling(
    served: tuple[CruxibleClient, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, instance_id, instance = served

    def backing() -> Any:
        return _query_backing(
            client,
            instance_id,
            name=f"QueryDefinition:{QUERY_NAME}",
            parameters={},
            coordinate=_coordinate(instance),
            evaluation_time=WHEN,
        )

    uncapped = backing()
    monkeypatch.setattr(compact_module, "COMPACT_QUERY_MAX_RESULTS", 1)
    assert backing() == uncapped


# -- F-007: a page without a projection still records the Claims it served -----


@pytest.mark.parametrize("mode", ["named", "spec"])
def test_a_query_without_a_projection_records_the_claims_its_cells_served(
    served: tuple[CruxibleClient, str, Any], monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    client, instance_id, _instance = served
    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "on")
    recorded: list[tuple[str, tuple[str, ...]]] = []

    def spy(
        _instance: Any, *, context: Any, operation: str, coordinate: Any, artifacts: Any
    ) -> tuple[()]:
        recorded.append((operation, tuple(identity.qualified for identity, _ in artifacts)))
        return ()

    monkeypatch.setattr(playbill_api, "record_consumption", spy)
    body: dict[str, Any] = {"claims": False, "evaluation_time": EVALUATION_TIME}
    if mode == "named":
        body["name"] = BARE
    else:
        body["spec"] = QueryDefinitionSpec.model_validate(
            {**_bare_query().model_dump(mode="json"), "pins": []}
        ).model_dump(mode="json")
    response = client._client.post(f"/api/v1/{instance_id}/playbill/query", json=body)
    assert response.status_code == 200, response.text
    page = QueryResultRecord.model_validate(response.json())
    assert [row["status"] for row in page.rows] == ["ready", "blocked"]
    assert all("claims" not in row for row in page.rows)

    claims = [names for operation, names in recorded if operation == "playbill.claim.get"]
    assert len(claims) == 1 and len(claims[0]) == 2
    assert all(name.startswith("Claim:CLM-") for name in claims[0])
