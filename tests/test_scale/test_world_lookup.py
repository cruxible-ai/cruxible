"""The first typed lookup pays for one Subject at either instance size."""

from pathlib import Path

from cruxible_client import Cruxible
from tests.test_client.test_playbill_sdk_world import _WorldClient


def test_first_world_lookup_does_the_same_request_work_at_20_and_2000_subjects(
    tmp_path: Path,
) -> None:
    measured = []
    for size in (20, 2000):
        client = _WorldClient()
        kinds = ("dev.batch", "sec.package", "sec.vulnerability")
        client.subjects = {
            f"{kinds[index % len(kinds)]}/item-{index:04d}": "live" for index in range(size)
        }
        workspace = tmp_path / str(size)
        workspace.mkdir()
        cx = Cruxible._from_client(client, instance_id="inst_world", workspace=workspace)
        world = cx.world()
        assert client.subject_get_requests == [] and client.subject_queries == []

        # The first lookup, membership and repeats must never enumerate even
        # their own kind. Count requests and cache payload, not elapsed time.
        subject = world.sec.package["item-0001"]
        assert subject.address == "sec.package/item-0001"
        assert "item-0001" in world.sec.package
        assert world.sec.package["item-0001"] is subject
        measured.append(
            (
                len(client.subject_get_requests),
                len(client.subject_queries),
                client.subject_list_calls,
                sum(len(items) for items in world._subject_cache.values()),
            )
        )
        assert client.subject_get_requests[0].at == world.coordinate
        cx.close()
    assert measured == [(1, 0, 0, 1), (1, 0, 0, 1)]
