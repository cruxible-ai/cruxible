"""HTTP laws for the immutable capability ceiling on Playbill surfaces."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cruxible_core.errors import ConfigError, DaemonOperationScopeError
from cruxible_core.runtime.permissions import (
    init_permissions,
    request_instance_scope,
    require_unscoped_operator,
    reset_permissions,
)
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry


@pytest.fixture
def host_id(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    record = get_registry().create_governed_instance_with_id("inst_ceiling")
    try:
        yield record.record.instance_id
    finally:
        reset_runtime_credential_store()
        reset_registry()
        reset_permissions()


def _client_at_ceiling(monkeypatch: pytest.MonkeyPatch, ceiling: str) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_MODE", ceiling)
    init_permissions()
    return TestClient(create_app())


@pytest.mark.parametrize(
    ("ceiling", "allowed_method", "allowed_path", "allowed_json", "denied_path", "operation"),
    [
        (
            "read_only",
            "GET",
            "/api/v1/server/info",
            None,
            "/api/v1/inst_ceiling/playbill/bodies",
            "cruxible_playbill_store_body",
        ),
        (
            "governed_write",
            "POST",
            "/api/v1/inst_ceiling/playbill/bodies",
            {"content_base64": ""},
            "/api/v1/inst_ceiling/playbill/proposals/missing/activate",
            "cruxible_playbill_activate",
        ),
        (
            "graph_write",
            "POST",
            "/api/v1/inst_ceiling/playbill/proposals/missing/activate",
            None,
            "/api/v1/runtime/instances",
            "cruxible_playbill_host_create",
        ),
    ],
)
def test_each_playbill_tier_allows_at_ceiling_and_refuses_above_it(
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
    ceiling: str,
    allowed_method: str,
    allowed_path: str,
    allowed_json: dict[str, str] | None,
    denied_path: str,
    operation: str,
) -> None:
    client = _client_at_ceiling(monkeypatch, ceiling)

    allowed = client.request(allowed_method, allowed_path, json=allowed_json)
    denied = client.post(
        denied_path,
        json={"content_base64": ""}
        if denied_path.endswith("/playbill/bodies")
        else ({} if denied_path == "/api/v1/runtime/instances" else None),
    )

    # Uninitialized Playbill operations reach the semantic boundary and refuse
    # with a state conflict; the read-only server-info request succeeds.
    assert allowed.status_code in {200, 409}, allowed.text
    assert denied.status_code == 403, denied.text
    payload = denied.json()
    assert payload["context"]["tool_name"] == operation
    assert payload["context"]["ceiling_mode"] == ceiling.upper()
    assert "capability ceiling" in payload["message"]


def test_admin_ceiling_allows_host_allocation(
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_at_ceiling(monkeypatch, "admin")

    response = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_admin_allowed"},
    )

    assert response.status_code == 200, response.text


def test_admin_credential_is_clamped_and_cannot_mint_above_ceiling(
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_runtime_credential_store()
    admin = store.create_credential(
        instance_id=host_id,
        label="found-admin",
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    client = _client_at_ceiling(monkeypatch, "governed_write")
    headers = {"Authorization": f"Bearer {admin.token}"}

    at_ceiling = client.post(
        f"/api/v1/{host_id}/playbill/bodies",
        json={"content_base64": ""},
        headers=headers,
    )
    above_ceiling = client.post(
        f"/api/v1/{host_id}/playbill/proposals/missing/activate",
        headers=headers,
    )
    mint = client.post(
        f"/api/v1/{host_id}/runtime/credentials",
        json={"label": "attempted-admin", "permission_mode": "admin"},
        headers=headers,
    )

    assert at_ceiling.status_code == 409, at_ceiling.text
    assert above_ceiling.status_code == 403
    assert above_ceiling.json()["context"]["ceiling_mode"] == "GOVERNED_WRITE"
    assert mint.status_code == 403
    assert mint.json()["context"]["tool_name"] == "cruxible_runtime_credentials"
    assert [record.label for record in store.list_for_instance(host_id)] == ["found-admin"]


def test_bootstrap_claim_cannot_mint_admin_above_ceiling(
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")
    client = _client_at_ceiling(monkeypatch, "graph_write")

    response = client.post(
        f"/api/v1/{host_id}/runtime/bootstrap/claim",
        json={"bootstrap_secret": "bootstrap-secret"},
    )

    assert response.status_code == 403
    assert response.json()["context"]["tool_name"] == "cruxible_runtime_credentials"
    assert get_runtime_credential_store().list_for_instance(host_id) == []


def test_environment_changes_cannot_alter_frozen_ceiling(
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_at_ceiling(monkeypatch, "graph_write")
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")

    with pytest.raises(ConfigError, match="immutable after permission initialization"):
        init_permissions()

    denied = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_still_denied"},
    )
    assert denied.status_code == 403
    assert denied.json()["context"]["ceiling_mode"] == "GRAPH_WRITE"


def test_health_does_not_disclose_capability_ceiling(
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_at_ceiling(monkeypatch, "read_only")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_daemon_scope_refusal_names_operation_instead_of_a_fake_instance() -> None:
    with request_instance_scope("inst_scoped"):
        with pytest.raises(
            DaemonOperationScopeError,
            match=(
                "instance 'inst_scoped' cannot perform daemon-wide operation "
                "'cruxible_playbill_host_create'"
            ),
        ):
            require_unscoped_operator("cruxible_playbill_host_create")


@pytest.mark.parametrize(
    ("method", "path", "body", "operation", "command"),
    [
        (
            "POST",
            "/api/v1/runtime/instances",
            {"instance_id": "inst_ceiling"},
            "cruxible_playbill_host_create",
            "playbill host create",
        ),
        (
            "GET",
            "/api/v1/server/info",
            None,
            "cruxible_server_info",
            "server status",
        ),
        (
            "POST",
            "/api/v1/server/restart",
            None,
            "cruxible_server_restart",
            "server restart",
        ),
    ],
)
def test_instance_token_gets_typed_403_for_every_daemon_scope_route(
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, str] | None,
    operation: str,
    command: str,
) -> None:
    credential = get_runtime_credential_store().create_credential(
        instance_id=host_id,
        label="instance-admin",
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    headers = {"Authorization": f"Bearer {credential.token}"}

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.request(method, path, json=body, headers=headers)

    assert response.status_code == 403, response.text
    payload = response.json()
    assert payload["error_type"] == "DaemonOperationScopeError"
    assert payload["context"] == {
        "operation": operation,
        "credential_scope": host_id,
    }
    assert payload["message"] == (
        f"The bearer token is instance-scoped; `{command}` is a daemon-scope operation. "
        "Use the operator credential (the bootstrap secret or a daemon-scope token) in "
        "CRUXIBLE_SERVER_BEARER_TOKEN. The daemon's runtime bootstrap secret keeps "
        "authorizing daemon-scope operations after `credential claim-bootstrap` has "
        "consumed its one-time claim."
    )
    assert payload["message"] != "internal server error"
    # The repair names the served command that mints the operator credential,
    # not the refused operation: a repair must be runnable as written.
    assert payload["repair"] == {
        "operation": "credential.mint",
        "arguments": {
            "refused_operation": operation,
            "credential_env": "CRUXIBLE_SERVER_BEARER_TOKEN",
            "accepted_credentials": ["bootstrap secret", "daemon-scope token"],
        },
    }


def test_line_run_is_an_instance_operation_a_foreign_token_cannot_reach(
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token scoped to another instance never reaches Line authority."""

    other = get_registry().create_governed_instance_with_id("inst_other")
    foreign = get_runtime_credential_store().create_credential(
        instance_id=other.record.instance_id,
        label="other-instance-admin",
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    digest = "sha256:" + "b" * 64
    body = {
        "tag": "playbill-line-run-request-v1",
        "line_identity_digest": digest,
        "occurrence_id": None,
        "evaluation_time": "2026-08-21T12:00:00Z",
    }

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/v1/{host_id}/playbill/lines/{digest}/runs",
            json=body,
            headers={"Authorization": f"Bearer {foreign.token}"},
        )

    assert response.status_code == 403, response.text
    payload = response.json()
    assert payload["error_type"] == "InstanceScopeError"
    # Refused on scope alone: the Line was never read, so no Line refusal code
    # and no accepted-closure detail can leak to a caller from another instance.
    assert "line_" not in response.text
