"""The reads that replace the cut surfaces: head, orient sections, get refs, query dimensions."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.write import PlaybillWriteRequestV1
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.orient import service_playbill_head
from tests.core_support._write_support import caller, seed_write_surface


def _write(instance: PlaybillInstance, *changes: dict[str, object]) -> list[str]:
    request = PlaybillWriteRequestV1.model_validate({"because": "test", "changes": list(changes)})
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return [str(change.claim) for change in outcome.changes]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    instance, _owner = seed_write_surface(tmp_path_factory.mktemp("read-cut"))
    return instance


def test_head_names_the_coordinate_and_generation_and_resolves_at(
    world: PlaybillInstance,
) -> None:
    head = service_playbill_head(world)
    history = world.accepted_history()

    assert head.instance == world.descriptor.instance_id
    assert head.coordinate.git_oid == world.accepted_coordinate().git_oid
    assert head.generation == history[-1].sequence
    earlier = service_playbill_head(world, at=history[0].oid[:12])
    assert earlier.generation == history[0].sequence
    assert earlier.coordinate.git_oid == history[0].oid
