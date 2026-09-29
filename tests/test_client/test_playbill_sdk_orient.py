"""SDK orient: the new verb, rendered for SDK callers, bound to the context's coordinate."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cruxible_client import Playbill
from cruxible_client import contracts as api
from cruxible_client.contracts.projection import AcceptedCoordinate
from tests.test_client.test_playbill_sdk import _COORDINATE, _Client, _workspace


class _OrientClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.orient_calls: list[dict[str, Any]] = []

    def orient_playbill(self, _instance_id: str, **values: Any) -> api.PlaybillOrientResultV1:
        self.orient_calls.append(values)
        return api.PlaybillOrientResultV1(
            instance="inst_test",
            coordinate=AcceptedCoordinate.model_validate(_COORDINATE.model_dump(mode="json")),
            generation=4,
            accepted_at=datetime(2026, 8, 24, tzinfo=UTC),
            evaluation_time=datetime(2026, 8, 24, 12, tzinfo=UTC),
            kinds=(),
            next=('pb.orient(kind="dev.roadmap_item")',),
        )


def test_sdk_orient_reads_the_map_for_the_sdk_surface(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _OrientClient()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    result = pb.orient(kind="dev.roadmap_item")

    assert result.generation == 4 and result.next == ('pb.orient(kind="dev.roadmap_item")',)
    (call,) = client.orient_calls
    assert call["surface"] == "sdk" and call["kind"] == "dev.roadmap_item"
    assert call["section"] is None and call["cursor"] is None
    assert pb.coordinate == result.coordinate

    pinned = pb.at(pb.coordinate)
    pinned.orient(section="queries", limit=5)
    assert client.orient_calls[-1]["at"] == _COORDINATE
    assert client.orient_calls[-1]["section"] == "queries"
