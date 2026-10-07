"""One canonical authoring request model serves both client and daemon."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from cruxible_client.contracts.authoring.models import (
    AuthoringIntentCompileRequest,
    AuthoringIntentCompileRequestV1,
)
from cruxible_core.server import playbill_request_models as server_models


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
        (AuthoringIntentCompileRequestV1, server_models.PlaybillAuthoringCompileRequest),
        (AuthoringIntentCompileRequest, server_models.PlaybillAuthoringCompileRequestV3),
    ],
    ids=("compile-v1", "compile-v3"),
)
def test_client_and_server_share_each_authoring_request_model(
    client_model: type[BaseModel],
    server_model: type[BaseModel],
) -> None:
    assert server_model is client_model
    assert _request_shape(server_model) == _request_shape(client_model)
