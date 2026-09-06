"""Acceptance leaves read snapshots alone; floor maintenance pins its target."""

from pathlib import Path
from typing import Any, Literal

import pytest

from cruxible_client import Playbill, contracts
from cruxible_client.authoring.workspace import (
    activate_with_workspace_refresh,
    inspect_workspace_floor,
    materialize_playbill_floor,
    refresh_workspace_floor,
)
from cruxible_client.contracts.projection import AcceptedCoordinate

from .test_playbill_workspace import _coordinate, _export, _workspace


class _Client:
    def __init__(self, status: Literal["accepted", "lost_cas"] = "accepted") -> None:
        self.events: list[object] = []
        self.status = status

    def activate_playbill_proposal(
        self, instance_id: str, proposal_id: str
    ) -> contracts.PlaybillActivationReceipt:
        self.events.append(("accept", instance_id, proposal_id))
        return contracts.PlaybillActivationReceipt(
            proposal_id=proposal_id,
            activated_by="owner",
            status=self.status,
            accepted_coordinate=_coordinate() if self.status == "accepted" else None,
            workspace_advertisement={"status": "not_attached", "workspace_path": None},
        )

    def export_playbill_floor(
        self, instance_id: str, *, at: contracts.PlaybillAcceptedCoordinate | None = None
    ) -> contracts.PlaybillFloorExport:
        self.events.append(("floor", instance_id, at))
        return _export()


def _sdk(client: Any, workspace: Path) -> Playbill:
    # Avoid orientation I/O: this test isolates the mutation/maintenance boundary.
    pb = Playbill(
        client=client,
        instance_id="inst_test",
        workspace=workspace,
        access_profile=None,  # type: ignore[arg-type]
        clock=None,
    )
    pb._coordinate = AcceptedCoordinate.model_validate(_coordinate("9").model_dump())
    return pb


@pytest.mark.parametrize("status", ["accepted", "lost_cas"])
def test_accept_only_calls_daemon_and_retains_read_coordinate(
    tmp_path: Path, status: Literal["accepted", "lost_cas"]
) -> None:
    client = _Client(status)
    pb = _sdk(client, tmp_path)
    before = pb.coordinate

    receipt = pb.accept("proposal-1")

    assert receipt.status == status
    assert client.events == [("accept", "inst_test", "proposal-1")]
    assert pb.coordinate == before
    assert list(tmp_path.iterdir()) == []


def test_explicit_refresh_pins_target_reports_floor_and_retains_snapshot(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    client = _Client()
    pb = _sdk(client, workspace)
    before = pb.coordinate

    result = pb.refresh_workspace(at=_coordinate())

    assert client.events == [("floor", "inst_test", _coordinate())]
    assert result.status == "refreshed"
    assert result.coordinate == _coordinate()
    assert pb.coordinate == before
    installed = inspect_workspace_floor(workspace, current_coordinate=_coordinate("9"))
    assert installed.installed_coordinate == _coordinate()
    assert installed.status == "stale"


def test_refresh_refuses_different_coordinate_before_replacing_floor(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    previous = materialize_playbill_floor(workspace, export=_export(content=b"previous"))
    before = (Path(previous.destination) / "cards/fresh.json").read_bytes()
    client = _Client()

    result = refresh_workspace_floor(client, "inst_test", workspace=workspace, at=_coordinate("9"))

    assert result.status == "failed"
    assert result.coordinate is None
    assert "requested coordinate" in (result.message or "")
    assert (Path(previous.destination) / "cards/fresh.json").read_bytes() == before


def test_unconfigured_refresh_makes_no_request(tmp_path: Path) -> None:
    client = _Client()
    result = _sdk(client, tmp_path).refresh_workspace(at=_coordinate())
    assert result.status == "not_configured"
    assert result.coordinate is None
    assert client.events == []


def test_convenience_activation_pins_floor_to_receipt(tmp_path: Path) -> None:
    client = _Client()
    result = activate_with_workspace_refresh(
        client, "inst_test", "proposal-1", workspace=_workspace(tmp_path), sync=False
    )
    assert result.status == "accepted"
    assert client.events == [
        ("accept", "inst_test", "proposal-1"),
        ("floor", "inst_test", result.accepted_coordinate),
    ]
    assert result.floor_refresh.coordinate == result.accepted_coordinate
    assert result.block_sync is None
