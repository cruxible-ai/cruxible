"""The HTTP seam REFUSES the inputs the 0.4.0 removals retired.

Removing a field from a request model is not the same as refusing it: pydantic
ignores unknown keys, so before this every retired input was accepted, dropped,
and answered with a success. ``group_override=True`` is the case that makes it
unsafe -- a caller asking for an edge to be held for review got ``200`` and no
override. Each case below asserts the 422 names the key AND what to send now.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry

_FEEDBACK_BODY = {
    "action": "accept",
    "from_type": "Part",
    "from_id": "BP-1",
    "relationship_type": "fits",
    "to_type": "Vehicle",
    "to_id": "V-1",
}
_BATCH_ITEM = {
    "receipt_id": "RCP-1",
    "action": "accept",
    "target": {
        "from_type": "Part",
        "from_id": "BP-1",
        "relationship_type": "fits",
        "to_type": "Vehicle",
        "to_id": "V-1",
    },
}

# path, body, retired key, the guidance the refusal must carry.
_RETIRED_HTTP_INPUTS = [
    ("feedback", {**_FEEDBACK_BODY, "source": "agent"}, "source", "actor_context"),
    (
        "feedback",
        {**_FEEDBACK_BODY, "group_override": True},
        "group_override",
        "no public replacement",
    ),
    (
        "feedback/from-query",
        {"receipt_id": "RCP-1", "result_index": 0, "action": "accept", "source": "agent"},
        "source",
        "actor_context",
    ),
    (
        "feedback/from-query",
        {"receipt_id": "RCP-1", "result_index": 0, "action": "accept", "group_override": True},
        "group_override",
        "no public replacement",
    ),
    (
        "feedback/batch",
        {"items": [{**_BATCH_ITEM, "group_override": True}]},
        "group_override",
        "no public replacement",
    ),
    (
        "outcome",
        {"receipt_id": "RCP-1", "outcome": "correct", "source": "agent"},
        "source",
        "actor_context",
    ),
    (
        "groups/propose",
        {"relationship_type": "fits", "members": [], "proposed_by": "agent"},
        "proposed_by",
        "actor_context",
    ),
    (
        "groups/GRP-1/resolve",
        {"action": "approve", "expected_pending_version": 1, "resolved_by": "agent"},
        "resolved_by",
        "actor_context",
    ),
    (
        "decision-records",
        {"question": "Ship it?", "opened_by": "agent"},
        "opened_by",
        "actor_context",
    ),
]


@pytest.fixture
def app_client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()
    return TestClient(create_app())


@pytest.mark.parametrize(("path", "body", "key", "guidance"), _RETIRED_HTTP_INPUTS)
def test_retired_http_inputs_are_refused_with_422(
    app_client: TestClient,
    path: str,
    body: dict,
    key: str,
    guidance: str,
) -> None:
    # The instance id is deliberately unknown: body validation runs first, so a
    # 422 (rather than the 404 a valid body would earn) is the proof the seam
    # refused the input instead of forwarding it.
    response = app_client.post(f"/api/v1/inst-unknown/{path}", json=body)

    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["error_type"] == "RequestValidationError"
    detail = " ".join(payload["errors"])
    assert f"'{key}' was removed in 0.4.0" in detail
    assert guidance in detail


def test_an_unrelated_unknown_key_is_still_tolerated(app_client: TestClient) -> None:
    """The refusal is by NAME, not ``extra='forbid'``.

    Forbidding every extra is a separate policy change the 0.4.0 schedule never
    promised, so an unknown key that was never an accepted input still passes
    validation and reaches the (here missing) instance.
    """
    response = app_client.post(
        "/api/v1/inst-unknown/feedback",
        json={**_FEEDBACK_BODY, "not_a_retired_input": "whatever"},
    )

    assert response.status_code != 422, response.text
