"""The query verb answers identically on HTTP, the client transport and MCP."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient, Playbill, contracts
from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1
from cruxible_client.contracts.errors import ReadRefusalError
from cruxible_core.cli.main import cli
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import (
    PERMISSION_REQUIREMENTS,
    PermissionMode,
    reset_permissions,
)
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from tests.core_support._knowledge_loop_support import EVALUATION_TIME, SUBJECT_KIND, seed_claims

WHERE = [{"field": "status", "ne": "blocked"}]


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[CruxibleClient, str]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    playbill_api.get_playbill_manager().clear()
    http = TestClient(create_app())
    instance, _owner = seed_claims(tmp_path)
    instance_id = instance.descriptor.instance_id
    get_registry().create_governed_instance_with_id(instance_id)
    monkeypatch.setattr(playbill_api.get_playbill_manager(), "get", lambda _: instance)
    client = CruxibleClient(base_url="http://testserver")
    client._client.close()
    client._client = http  # type: ignore[assignment]
    return client, instance_id


def _rows(result: contracts.PlaybillQueryResult) -> list[tuple[str, object]]:
    return [(row["subject_id"], row["status"]) for row in result.rows]


@pytest.mark.parametrize("surface", ["transport", "mcp-local", "mcp-remote", "cli", "sdk"])
def test_every_surface_returns_the_same_page(
    served: tuple[CruxibleClient, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    surface: str,
) -> None:
    from cruxible_core.mcp import handlers

    client, instance_id = served
    if surface == "transport":
        result = client.query_playbill(
            instance_id,
            request=PlaybillQueryRequestV1.model_validate(
                {
                    "kind": SUBJECT_KIND,
                    "where": WHERE,
                    "select": ["status"],
                    "evaluation_time": EVALUATION_TIME,
                }
            ),
        )
    elif surface == "cli":
        from cruxible_core.cli.commands import playbill as commands

        monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(client, instance_id))
        invoked = CliRunner().invoke(
            cli,
            [
                "playbill",
                "query",
                SUBJECT_KIND,
                "--where",
                "status!=blocked",
                "--select",
                "status",
                "--evaluation-time",
                EVALUATION_TIME,
                "--json",
            ],
        )
        assert invoked.exit_code == 0, invoked.output
        result = contracts.PlaybillQueryResult.model_validate(json.loads(invoked.output))
    elif surface == "sdk":
        playbill = Playbill._from_client(  # type: ignore[arg-type]
            client,
            instance_id=instance_id,
            workspace=tmp_path,
            clock=lambda: datetime.fromisoformat(EVALUATION_TIME),
        )
        result = playbill.query(SUBJECT_KIND, where=WHERE, select=["status"]).page
    else:
        monkeypatch.setattr(
            handlers, "_get_client", lambda: None if surface == "mcp-local" else client
        )
        result = handlers.handle_playbill_query(
            instance_id,
            kind=SUBJECT_KIND,
            where=WHERE,
            select=["status"],
            evaluation_time=EVALUATION_TIME,
        )
    assert _rows(result) == [("wi-42", "ready")]
    assert result.receipt.mode == "inline"


def test_wrong_names_refuse_over_http_with_code_and_nearest(
    served: tuple[CruxibleClient, str],
) -> None:
    client, instance_id = served
    response = client._client.post(
        f"/api/v1/{instance_id}/playbill/query",
        json={"kind": SUBJECT_KIND, "where": [{"field": "stauts", "eq": "ready"}]},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error_type"] == "ReadRefusalError"
    assert body["error_code"] == "playbill.query.unknown_field"
    assert "status" in body["context"]["candidates"]
    assert body["context"]["field_path"] == "where[0].field"
    assert body["context"]["repair_line"]
    assert "Traceback" not in response.text

    # The client rebuilds the same coded refusal, not a bare CoreError.
    with pytest.raises(ReadRefusalError) as refused:
        client.query_playbill(
            instance_id,
            request=PlaybillQueryRequestV1.model_validate(
                {"kind": SUBJECT_KIND, "where": [{"field": "stauts", "eq": "ready"}]}
            ),
        )
    assert refused.value.error_code == "playbill.query.unknown_field"
    assert refused.value.http_status == 400
    assert "status" in refused.value.candidates
    assert refused.value.field_path == "where[0].field"
    assert refused.value.repair_line == body["context"]["repair_line"]


def test_malformed_filters_name_their_json_path(served: tuple[CruxibleClient, str]) -> None:
    client, instance_id = served
    response = client._client.post(
        f"/api/v1/{instance_id}/playbill/query",
        json={"kind": SUBJECT_KIND, "where": [{"field": "status"}]},
    )

    assert response.status_code == 422
    assert any(
        error.startswith("body.where.0") and '"eq"' in error for error in response.json()["errors"]
    )


def test_the_mcp_tool_is_read_only_and_fully_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_core.errors import DataValidationError
    from cruxible_core.mcp import handlers

    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    schema = tools["cruxible_playbill_query"].inputSchema

    assert PERMISSION_REQUIREMENTS["cruxible_playbill_query"] is PermissionMode.READ_ONLY
    assert set(schema.get("required", ())) == set()
    assert schema["properties"]["params"]["anyOf"][0]["additionalProperties"]["anyOf"]

    def untyped(node: Any, path: str) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            if (
                node.get("type") == "object"
                and "properties" not in node
                and (node.get("additionalProperties") in (None, True, {}))
            ):
                found.append(path)
            for key, value in node.items():
                found.extend(untyped(value, f"{path}.{key}"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                found.extend(untyped(value, f"{path}[{index}]"))
        return found

    free_form = [
        path for name, prop in schema["properties"].items() for path in untyped(prop, name)
    ]
    assert free_form == []
    follow = schema["$defs"]["QueryFollowV1"]["properties"]["direction"]
    assert follow["enum"] == ["forward", "reverse"] and follow["default"] == "forward"
    # Spec mode is its own full-profile tool, so the default query tool stays small.
    assert "spec" not in schema["properties"]
    assert len(json.dumps(schema, separators=(",", ":"))) < 8_000

    spec_schema = tools["cruxible_playbill_query_spec"].inputSchema
    assert PERMISSION_REQUIREMENTS["cruxible_playbill_query_spec"] is PermissionMode.READ_ONLY
    assert set(spec_schema["required"]) == {"spec"}
    assert set(spec_schema["properties"]) == {
        "instance_id",
        "spec",
        "limit",
        "cursor",
        "at",
        "evaluation_time",
    }

    with pytest.raises(DataValidationError, match=r"where\.0"):
        handlers.handle_playbill_query("inst", kind=SUBJECT_KIND, where=[{"field": "status"}])


def test_the_spec_tool_answers_with_the_same_evaluation(
    served: tuple[CruxibleClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.query.definitions import QueryDefinitionSpecV1
    from cruxible_core.mcp import handlers
    from cruxible_core.mcp.curation import PROFILE_DEFAULT, ToolCuration, advertised_tool_names
    from tests.core_support._knowledge_loop_support import work_item_query

    client, instance_id = served
    spec = QueryDefinitionSpecV1.model_validate(
        {**work_item_query("project.adhoc").model_dump(mode="json"), "pins": []}
    )
    through_sdk = client.query_playbill(
        instance_id,
        request=PlaybillQueryRequestV1(spec=spec, evaluation_time=EVALUATION_TIME),
    )
    for remote in (False, True):
        monkeypatch.setattr(
            handlers, "_get_client", lambda remote=remote: client if remote else None
        )
        through_tool = handlers.handle_playbill_query_spec(
            instance_id, spec=spec, evaluation_time=EVALUATION_TIME
        )
        assert through_tool.receipt.mode == "spec"
        assert through_tool.rows == through_sdk.rows

    advertised = advertised_tool_names(
        mode=PermissionMode.ADMIN,
        registered_tools={"cruxible_playbill_query", "cruxible_playbill_query_spec"},
        curation=ToolCuration(profile=PROFILE_DEFAULT),
    )
    assert advertised == {"cruxible_playbill_query"}
