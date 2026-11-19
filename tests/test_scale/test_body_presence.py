"""Presence-only work is zero at N and 10N retained bodies, on every read route."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.storage.read_positions import PositionedBodies, reading_at
from tests.test_service.test_indexed_body_presence import PATHS, _presence_probes, _read


@pytest.mark.parametrize("path", PATHS)
def test_presence_probes_stay_zero_at_ten_times_as_many_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    from tests.test_authoring.test_authoring_existing_capture import shared_capture_world

    instance, _owner, _actor, claim, *_rest = shared_capture_world(tmp_path)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        capture = projection.typed.source(f"Claim:{claim}").backing.capture_digests[0]
    world = (instance, claim, capture)
    store = instance.body_store()
    counts = []
    for start, stop in ((0, 20), (20, 200)):
        # Fixture growth is outside measurement. The read has the same requested
        # result and the same cited dependencies at either population size.
        for index in range(start, stop):
            store.store(f"unrelated body {index}".encode())
        _read(world, path)
        with monkeypatch.context() as scoped:
            probes = _presence_probes(scoped)
            _read(world, path)
            counts.append(len(probes))
    assert counts == [0, 0]


def test_positioned_presence_never_hashes_or_stats_even_on_a_cold_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.storage.cas import ContentAddressedBodyStore

    tmp_path.mkdir(exist_ok=True)
    store = ContentAddressedBodyStore(tmp_path)
    sizes: list[int] = []
    for start, stop in ((0, 20), (20, 200)):
        digest = ""
        for index in range(start, stop):
            digest = store.store(str(index).encode()).digest
        head = store.landings.head()
        from cruxible_core.storage.read_positions import ReadPositions

        with reading_at(ReadPositions(head.identity, head.sequence, 0)):
            with monkeypatch.context() as scoped:
                probes = _presence_probes(scoped)
                assert PositionedBodies(store).verify(digest)
                assert not PositionedBodies(store).verify("sha256:" + "f" * 64)
                sizes.append(len(probes))
    assert sizes == [0, 0]
