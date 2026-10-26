"""HTTP laws for the immutable capability ceiling on Cruxible surfaces."""

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
            "/api/v1/inst_ceiling/bodies",
            "cruxible_body_store",
        ),
        (
            "governed_write",
            "POST",
            "/api/v1/inst_ceiling/bodies",
            {"content_base64": ""},
            "/api/v1/inst_ceiling/proposals/missing/activate",
            "cruxible_proposal_activate",
        ),
        (
            "graph_write",
            "POST",
            "/api/v1/inst_ceiling/proposals/missing/activate",
            None,
            "/api/v1/runtime/instances",
            "cruxible_host_create",
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
        if denied_path.endswith("/bodies")
        else ({} if denied_path == "/api/v1/runtime/instances" else None),
    )

    # Uninitialized Cruxible operations reach the semantic boundary and refuse
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
        principal_id="found-admin",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    client = _client_at_ceiling(monkeypatch, "governed_write")
    headers = {"Authorization": f"Bearer {admin.token}"}

    at_ceiling = client.post(
        f"/api/v1/{host_id}/bodies",
        json={"content_base64": ""},
        headers=headers,
    )
    above_ceiling = client.post(
        f"/api/v1/{host_id}/proposals/missing/activate",
        headers=headers,
    )
    mint = client.post(
        f"/api/v1/{host_id}/runtime/credentials",
        json={"principal_id": "attempted-admin", "permission_mode": "admin"},
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
                "instance 'inst_scoped' cannot perform daemon-wide operation 'cruxible_host_create'"
            ),
        ):
            require_unscoped_operator("cruxible_host_create")


@pytest.mark.parametrize(
    ("method", "path", "body", "operation", "command"),
    [
        (
            "POST",
            "/api/v1/runtime/instances",
            {"instance_id": "inst_ceiling"},
            "cruxible_host_create",
            "cruxible host create",
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
    body = {
        "tag": "playbill-line-run-request-v1",
        "line": "Line:intake",
        "occurrence_id": None,
        "evaluation_time": "2026-08-21T12:00:00Z",
    }

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/v1/{host_id}/lines/intake/runs",
            json=body,
            headers={"Authorization": f"Bearer {foreign.token}"},
        )

    assert response.status_code == 403, response.text
    payload = response.json()
    assert payload["error_type"] == "InstanceScopeError"
    # Refused on scope alone: the Line was never read, so no Line refusal code
    # and no accepted-closure detail can leak to a caller from another instance.
    assert "line_" not in response.text


@pytest.mark.parametrize(
    ("path", "body", "operation"),
    [
        (
            "/api/v1/inst_ceiling/lines/intake/runs",
            {
                "tag": "playbill-line-run-request-v1",
                "line": "Line:intake",
                "occurrence_id": None,
                "evaluation_time": "2026-08-21T12:00:00Z",
            },
            "cruxible_line_run",
        ),
        ("/api/v1/inst_ceiling/lines/intake/dispatch", {}, "cruxible_line_dispatch"),
        ("/api/v1/inst_ceiling/procedures/intake/runs", {"input": {}}, "cruxible_procedure_run"),
    ],
)
def test_a_read_only_daemon_refuses_every_run_before_reading_its_target(
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, object],
    operation: str,
) -> None:
    """A run retains its journal and Captures, so a read-only ceiling refuses it.

    An observe-only Line or Procedure runs at read-only under a writable
    ceiling; a read-only daemon persists nothing, whatever the target can do.
    """

    client = _client_at_ceiling(monkeypatch, "read_only")

    response = client.post(path, json=body)

    assert response.status_code == 403, response.text
    payload = response.json()
    assert payload["context"]["tool_name"] == operation
    assert payload["context"]["ceiling_mode"] == "READ_ONLY"


#: Non-GET routes a read-only daemon serves, each with why it changes no state.
#: Every other non-GET route must refuse under a read-only ceiling.
_READ_ONLY_POSTS: dict[str, str] = {
    "/api/v1/{instance_id}/claims/backings": "reads the Claims backing a projection",
    "/api/v1/{instance_id}/claims/read-batch": "reads a batch of Claims",
    "/api/v1/{instance_id}/coverage/resolve": "resolves observations against accepted coverage",
    "/api/v1/{instance_id}/curation/duplicates": "lists duplicate candidates",
    "/api/v1/{instance_id}/curation/list": "lists curation findings",
    "/api/v1/{instance_id}/audit": "reads the audit view",
    "/api/v1/{instance_id}/files/check": "checks a file bundle without proposing it",
    "/api/v1/{instance_id}/floor/delta": "returns floor bytes between two generations",
    "/api/v1/{instance_id}/floor/deliver-now": (
        "renders accepted state into the attached workspace floor; no instance state changes"
    ),
    "/api/v1/{instance_id}/floor/export": "returns the floor bytes at a coordinate",
    "/api/v1/{instance_id}/get": "reads one ref",
    "/api/v1/{instance_id}/get-batch": "reads a batch of refs",
    "/api/v1/{instance_id}/kits/build": "returns a kit bundle exported from accepted state",
    "/api/v1/{instance_id}/lines/{line}/evaluate": (
        "a dry run reads; enqueueing needs governed write, which the ceiling refuses"
    ),
    "/api/v1/{instance_id}/next": "lists the next work for the caller",
    "/api/v1/{instance_id}/predictions/query": "lists predictions",
    "/api/v1/{instance_id}/procedures/source/preview": "previews Procedure source",
    "/api/v1/{instance_id}/procedures/{name}/readings": "reads measurement standing",
    "/api/v1/{instance_id}/projections/check": "checks projection blocks",
    "/api/v1/{instance_id}/projections/sync-backing": "reads a block's sync backing",
    "/api/v1/{instance_id}/proposals/{proposal_id}/approval-challenge": (
        "returns the statement a signer would approve; nothing is recorded"
    ),
    "/api/v1/{instance_id}/proposals/{proposal_id}/review": "reads a proposal's review view",
    "/api/v1/{instance_id}/query": "runs a named Query",
    "/api/v1/{instance_id}/since": "reads what changed since a coordinate",
}


class _Placeholder:
    """Any request field, so a handler reaches its permission gate unvalidated."""

    def __getattr__(self, name: str) -> _Placeholder:
        return _Placeholder()

    def __call__(self, *args: object, **kwargs: object) -> _Placeholder:
        return _Placeholder()

    def __iter__(self) -> Iterator[object]:
        return iter(())


def test_every_mutating_route_refuses_under_a_read_only_ceiling(
    host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sweep the served route table: each non-GET route refuses or is a listed read.

    Handlers are called directly with placeholder inputs, so each reaches its
    own permission gate without a hand-built body per route. A route that is
    not refused fails the sweep unless it is listed above with a reason; a
    listed route that refuses is stale in the list.
    """

    import asyncio
    import inspect

    from cruxible_core.errors import PermissionDeniedError
    from cruxible_core.server.app import iter_api_routes

    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    init_permissions()
    app = create_app()
    unrefused: dict[str, str] = {}
    refused_but_listed: list[str] = []
    swept = 0
    for route in iter_api_routes(app.routes):
        if not set(route.methods or ()) - {"GET", "HEAD"}:
            continue
        swept += 1
        arguments: dict[str, object] = {}
        for name, parameter in inspect.signature(route.endpoint).parameters.items():
            if name == "instance_id":
                arguments[name] = host_id
            elif parameter.annotation in (str, "str"):
                arguments[name] = "placeholder"
            elif parameter.annotation in (int, "int"):
                arguments[name] = 1
            elif parameter.default is None:
                arguments[name] = None
            else:
                arguments[name] = _Placeholder()
        try:
            result = route.endpoint(**arguments)
            if inspect.iscoroutine(result):
                asyncio.run(result)
        except PermissionDeniedError as exc:
            assert exc.ceiling_mode == "READ_ONLY", (route.path, exc.ceiling_mode)
            if route.path in _READ_ONLY_POSTS:
                refused_but_listed.append(route.path)
            continue
        except Exception as exc:  # noqa: BLE001 - past the gate: any refusal will do
            unrefused[route.path] = f"{type(exc).__name__}: {exc}"[:160]
            continue
        unrefused[route.path] = "returned"
    assert swept > 80
    assert {path: why for path, why in unrefused.items() if path not in _READ_ONLY_POSTS} == {}
    assert refused_but_listed == []
    assert set(_READ_ONLY_POSTS) <= set(unrefused)


@pytest.mark.parametrize(
    ("ceiling", "refused"), [("read_only", True), ("governed_write", False), ("admin", False)]
)
def test_the_shared_run_gate_refuses_an_observe_only_run_only_under_a_read_only_ceiling(
    monkeypatch: pytest.MonkeyPatch, ceiling: str, refused: bool
) -> None:
    """Every door (served, MCP, Trigger dispatch) passes this gate."""

    from cruxible_core.errors import PermissionDeniedError
    from cruxible_core.service.procedures.procedure_runs import require_run_permission

    monkeypatch.setenv("CRUXIBLE_MODE", ceiling)
    reset_permissions()
    init_permissions()
    try:
        if refused:
            with pytest.raises(PermissionDeniedError):
                require_run_permission("cruxible_line_run", target_rung=1, caller_rung=0)
        else:
            require_run_permission("cruxible_line_run", target_rung=1, caller_rung=0)
    finally:
        reset_permissions()
