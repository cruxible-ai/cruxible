"""A floor has one writer: a delivering daemon, or else the client."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client import contracts
from cruxible_client.authoring import workspace as authoring
from cruxible_client.authoring.workspace import daemon_floor_delivery, write_workspace_floor_delta
from tests.test_client.test_playbill_workspace import _delta


def _registration(workspace, *, enabled, registered=None):
    registered = workspace if registered is None else registered

    def answer(_instance_id, workspace_root=None):
        return contracts.HostWorkspaceRegistration(
            instance_id="inst_floor",
            status="registered",
            workspace_path=str(registered),
            floor_delivery=enabled,
            delivers_here=None
            if workspace_root is None
            else enabled and Path(workspace_root) == Path(registered).resolve(),
        )

    return answer


@pytest.mark.parametrize("enabled,local", [(True, True), (False, True), (False, False)])
def test_floor_write_delegates_only_to_an_opted_in_local_daemon(
    tmp_path, monkeypatch, enabled, local
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    delivered = []
    delta = _delta()
    receipt = contracts.WorkspaceFloorWriteResult(
        status="written",
        path=".cruxible/floor",
        destination=str(workspace / ".cruxible/floor"),
        floor_digest="sha256:" + "a" * 64,
        coordinate=contracts.AcceptedCoordinate.model_validate(
            delta.head.coordinate().model_dump(mode="json")
        ),
        file_count=1,
    )
    result = contracts.FloorDeliveryResult(delta=delta, written=receipt)

    def deliver(_):
        delivered.append(True)
        return result

    client = SimpleNamespace(
        socket_path=str(tmp_path / "socket") if local else None,
        host_workspace_registration=_registration(workspace, enabled=enabled),
        deliver_floor_now=deliver,
    )
    fetches = []

    def fetch(*_):
        fetches.append(True)
        return delta

    if enabled and local:
        monkeypatch.setattr(
            authoring, "apply_floor_delta", lambda *_: pytest.fail("client wrote the floor")
        )
    _, written = write_workspace_floor_delta(
        fetch,
        instance_id="inst_floor",
        workspace=workspace,
        server_socket=str(tmp_path / "socket"),
        delivery=lambda: daemon_floor_delivery(client, "inst_floor", workspace),
    )
    assert written.status == "written"
    assert bool(delivered) == (enabled and local)
    assert bool(fetches) != (enabled and local)
    assert (workspace / ".cruxible/floor").exists() != (enabled and local)
    assert authoring.configured_floor_output(workspace) is not None


def test_delivery_refuses_a_different_registered_workspace(tmp_path):
    client = SimpleNamespace(
        socket_path=str(tmp_path / "socket"),
        host_workspace_registration=_registration(
            tmp_path, enabled=True, registered=tmp_path / "other"
        ),
    )
    with pytest.raises(authoring.WorkspaceError, match="another workspace"):
        daemon_floor_delivery(client, "inst_floor", tmp_path)


def test_a_tcp_export_refuses_when_the_daemon_delivers_that_floor(tmp_path):
    """Single writer: over TCP the daemon cannot deliver now, so the client must not write."""

    client = SimpleNamespace(
        socket_path=None, host_workspace_registration=_registration(tmp_path, enabled=True)
    )
    with pytest.raises(authoring.WorkspaceError, match="floor delivery off"):
        daemon_floor_delivery(client, "inst_floor", tmp_path)


def test_a_tcp_export_writes_a_floor_the_daemon_delivers_elsewhere(tmp_path):
    client = SimpleNamespace(
        socket_path=None,
        host_workspace_registration=_registration(
            tmp_path, enabled=True, registered=tmp_path / "other"
        ),
    )
    assert daemon_floor_delivery(client, "inst_floor", tmp_path) is None


def test_activation_refresh_uses_the_same_daemon_writer(tmp_path, monkeypatch):
    from cruxible_client.authoring.workspace import (
        record_floor_output,
        refresh_workspace_floor,
    )

    record_floor_output(tmp_path, instance_id="inst_floor", server_socket=str(tmp_path / "socket"))
    delta = _delta()
    written = contracts.WorkspaceFloorWriteResult(
        path=".cruxible/floor",
        destination=str(tmp_path / ".cruxible/floor"),
        floor_digest="sha256:" + "a" * 64,
        coordinate=contracts.AcceptedCoordinate.model_validate(
            delta.head.coordinate().model_dump(mode="json")
        ),
        file_count=1,
    )
    client = SimpleNamespace(
        socket_path=str(tmp_path / "socket"),
        host_workspace_registration=_registration(tmp_path, enabled=True),
        deliver_floor_now=lambda *_a, **_k: contracts.FloorDeliveryResult(
            delta=delta, written=written
        ),
    )
    monkeypatch.setattr(
        authoring, "apply_floor_delta", lambda *_: pytest.fail("client wrote the floor")
    )
    assert refresh_workspace_floor(client, "inst_floor", workspace=tmp_path).status == "refreshed"
    assert not (tmp_path / ".cruxible/floor").exists()


def test_client_transport_sends_typed_delivery_requests(tmp_path):
    import httpx

    from cruxible_client import CruxibleClient

    calls = []
    delta = _delta()
    delivered = contracts.FloorDeliveryResult(
        delta=delta,
        written=contracts.WorkspaceFloorWriteResult(
            path=".cruxible/floor",
            destination=str(tmp_path / ".cruxible/floor"),
            floor_digest="sha256:" + "a" * 64,
            coordinate=contracts.AcceptedCoordinate.model_validate(
                delta.head.coordinate().model_dump(mode="json")
            ),
            file_count=1,
        ),
    )

    def respond(request):
        import json

        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("deliver-now"):
            return httpx.Response(200, json=delivered.model_dump(mode="json"))
        return httpx.Response(
            200,
            json=contracts.HostWorkspaceRegistration(
                instance_id="inst_floor", status="registered", floor_delivery=True
            ).model_dump(mode="json"),
        )

    client = CruxibleClient(socket_path=str(tmp_path / "socket"))
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(respond), base_url="http://test")
    try:
        assert client.set_floor_delivery("inst_floor", enabled=True).floor_delivery
        assert client.deliver_floor_now("inst_floor") == delivered
        assert calls == [
            ("/api/v1/inst_floor/workspace/floor-delivery", {"enabled": True}),
            ("/api/v1/inst_floor/floor/deliver-now", {"include": [], "at": None}),
        ]
    finally:
        client.close()
