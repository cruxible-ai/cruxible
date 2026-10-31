"""One canonical authoring request model serves both client and daemon."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from cruxible_client.contracts.authoring.models import AuthoringIntentCompileRequest
from cruxible_core.server.routes import playbill as server_routes


def _request_shape(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(ref_template="#/$defs/{model}")
    properties = schema["properties"]
    return {
        "fields": tuple(model.model_fields),
        "required": tuple(sorted(schema.get("required", ()))),
        "properties": properties,
        "tag": properties["tag"].get("const"),
    }


@pytest.mark.parametrize(
    ("client_model", "server_model"),
    [
        (AuthoringIntentCompileRequest, server_routes.AuthoringIntentCompileRequest),
    ],
    ids=("compile",),
)
def test_client_and_server_share_each_authoring_request_model(
    client_model: type[BaseModel],
    server_model: type[BaseModel],
) -> None:
    assert server_model is client_model
    assert _request_shape(server_model) == _request_shape(client_model)
