"""Live calls, explicit snapshots, and detached World/draft ownership."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import Playbill
from cruxible_client import contracts as api
from cruxible_client.authoring.sdk_types import ClaimRef, SlotRef
from cruxible_client.contracts.claim_reads import ClaimReadBatchResultV1
from cruxible_client.contracts.projection import AcceptedCoordinate

from .test_playbill_sdk_world import _COORDINATE, _MOVED_COORDINATE, _WorldClient


class _LiveClient(_WorldClient):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[Any] = []
        self.closed = 0
        self.corrupt_batch = False

    def close(self) -> None:
        self.closed += 1

    def read_playbill_claim_batch(self, instance: str, *, request: Any) -> ClaimReadBatchResultV1:
        self.batches.append(request)
        at = request.at or self.coordinate
        views = tuple(self.get_playbill_claim(instance, name, at=at) for name in request.claim_ids)
        if self.corrupt_batch:
            views = (views[0].model_copy(update={"coordinate": _MOVED_COORDINATE}),)
        return ClaimReadBatchResultV1(coordinate=at, claims=views)

    def activate_playbill_proposal(
        self, instance: str, proposal_id: str
    ) -> api.PlaybillActivationReceipt:
        self.coordinate = _MOVED_COORDINATE
        return api.PlaybillActivationReceipt(
            proposal_id=proposal_id,
            activated_by="owner",
            status="accepted",
            accepted_coordinate=self.coordinate,
            workspace_advertisement={"status": "not_attached", "workspace_path": None},
        )


@pytest.fixture
def connection(tmp_path: Path) -> tuple[Playbill, _LiveClient]:
    client = _LiveClient()
    pb = Playbill._from_client(
        client,  # type: ignore[arg-type]
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    return pb, client


def test_live_batch_follows_another_writer_without_head_lookup(connection):
    pb, client = connection
    before = len(client.searches)
    pb.claim_views(["CLM-first"])
    client.coordinate = _MOVED_COORDINATE
    pb.claim_views(["CLM-first"])
    assert [request.at for request in client.batches] == [None, None]
    assert pb.coordinate.git_oid == _MOVED_COORDINATE.git_oid
    assert len(client.searches) == before


def test_receipt_snapshot_reads_exact_acceptance_after_head_moves_again(connection):
    pb, client = connection
    old = pb.at(pb.coordinate)
    receipt = pb.accept("proposal-1")
    accepted = pb.at(receipt.accepted_coordinate)
    client.coordinate = _MOVED_COORDINATE.model_copy(update={"git_oid": "c" * 40})
    accepted.claim_views(["CLM-new"])
    old.claim_views(["CLM-old"])
    pb.claim_views(["CLM-new"])
    assert [r.at for r in client.batches] == [_MOVED_COORDINATE, _COORDINATE, None]
    assert accepted.coordinate.git_oid == "b" * 40
    assert old.coordinate.git_oid == "a" * 40
    assert pb.coordinate.git_oid == "c" * 40


def test_world_and_draft_do_not_share_the_live_coordinate_cell(connection):
    pb, client = connection
    world = pb.world()
    draft = pb.changes(rationale="keep observed inputs stable")
    old = world.coordinate
    client.coordinate = _MOVED_COORDINATE
    pb.refresh()
    assert world.coordinate == old
    assert world.sec.vulnerability["cve-2026-69247"].severity
    assert client.searches[-1]["at"].git_oid == old.git_oid
    assert draft._playbill.coordinate == old


def test_pinned_world_and_refresh_keep_coordinate_without_reorienting_parent(connection):
    pb, client = connection
    snapshot = pb.at(pb.coordinate)
    client.coordinate = _MOVED_COORDINATE
    pb.refresh()
    assert snapshot.world().coordinate.git_oid == "a" * 40
    snapshot.refresh()
    assert client.claim_type_coordinates[-1] == _COORDINATE
    assert client.searches[-1]["at"] == _COORDINATE
    assert pb.coordinate.git_oid == "b" * 40


def test_explicit_refs_pin_live_reads_and_refuse_mixed_batches(connection):
    pb, client = connection
    old = AcceptedCoordinate.model_validate(_COORDINATE.model_dump())
    new = AcceptedCoordinate.model_validate(_MOVED_COORDINATE.model_dump())
    client.coordinate = _MOVED_COORDINATE
    pb.claim_views([ClaimRef("CLM-old", old), "CLM-also-old"])
    assert client.batches[-1].at == _COORDINATE
    with pytest.raises(ValueError, match="share one coordinate"):
        pb.claim_views([ClaimRef("CLM-old", old), ClaimRef("CLM-new", new)])
    with pytest.raises(ValueError, match="differs from the active orientation"):
        pb.at(old).claim_views([ClaimRef("CLM-new", new)])
    assert len(client.batches) == 1


def test_mixed_response_coordinates_refuse_without_moving_live_client(connection):
    pb, client = connection
    before = pb.coordinate
    client.corrupt_batch = True
    with pytest.raises(ValueError, match="mixed accepted coordinates"):
        pb.claim_views(["CLM-first"])
    assert pb.coordinate == before


def test_closing_borrowed_context_does_not_close_shared_transport(connection):
    pb, client = connection
    with pb.at(pb.coordinate) as snapshot:
        snapshot.claim_views(["CLM-first"])
    assert client.closed == 0
    pb.claim_views(["CLM-first"])
    pb.close()
    assert client.closed == 1


def test_live_procedure_binding_rejects_mixed_coordinates_before_transport(connection):
    pb, client = connection
    old = AcceptedCoordinate.model_validate(_COORDINATE.model_dump())
    new = AcceptedCoordinate.model_validate(_MOVED_COORDINATE.model_dump())
    procedure = pb.accepted_procedure("daily-summary")
    # The spy intentionally has no bind endpoint: both refusals precede I/O.
    with pytest.raises(ValueError, match="observed coordinate"):
        procedure.bind(
            bindings={"first": ClaimRef("CLM-old", old), "second": ClaimRef("CLM-new", new)}
        )
    with pytest.raises(ValueError, match="observed coordinate"):
        procedure.bind(bindings={SlotRef("input", new): ClaimRef("CLM-old", old)})


def test_explicit_connect_skips_current_orientation(connection, monkeypatch, tmp_path):
    from cruxible_client.authoring import sdk

    _, client = connection
    client.searches.clear()
    monkeypatch.setattr(sdk, "CruxibleClient", lambda **kwargs: client)
    monkeypatch.setattr(sdk.client_compatibility, "check_daemon_compatibility", lambda c: None)
    with Playbill.connect(
        target="http://test",
        instance="inst_world",
        workspace=tmp_path,
        context=tmp_path / "no-context.json",
        at=_COORDINATE,
    ) as pinned:
        assert client.searches == []
        client.coordinate = _MOVED_COORDINATE
        pinned.claim_views(["CLM-old"])
        assert client.batches[-1].at == _COORDINATE


def test_projection_only_scan_can_bind_head_without_whole_world_orientation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from cruxible_client.authoring import workspace

    calls = []
    monkeypatch.setattr(
        workspace,
        "WorkspaceSources",
        lambda root: SimpleNamespace(procedure_projection_entries=("registered",)),
    )
    monkeypatch.setattr(
        workspace,
        "observe_playbill_projection_coverage",
        lambda root, *, coordinate: calls.append(coordinate),
    )

    def resolve():
        calls.append("metadata")
        return _MOVED_COORDINATE

    _, coordinate = workspace.observe_playbill_next_workspace_with_coverage(
        object(),
        "instance",
        tmp_path,
        observation={"source_observations": []},
        resolve_coordinate=resolve,
    )
    assert coordinate == _MOVED_COORDINATE
    assert calls == ["metadata", _MOVED_COORDINATE]
