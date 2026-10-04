"""The floor delta: deterministic for (head, base), and applying it equals the full floor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.authoring.floor_apply import apply_floor_delta, read_floor_manifest
from cruxible_client.contracts.floor import PlaybillFloorDelta
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.floor.floor_delta import (
    advance_floor_index,
    service_playbill_floor_delta,
)
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_floor.test_floor_index import _set, _write

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"


def _head(instance: PlaybillInstance, generation: int | None = None) -> AcceptedCoordinate:
    if generation is None:
        return AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    with instance.accepted_history_reader() as history:
        oid = history.generation(generation).git_oid
    return AcceptedCoordinate.from_internal(instance.coordinate_for_oid(oid))


def _delta(
    instance: PlaybillInstance,
    *,
    head: int | None = None,
    base: int | None = None,
    renderer: str | None = None,
) -> PlaybillFloorDelta:
    full = service_playbill_floor_delta(
        instance, head=_head(instance, head), base_generation=None, base_renderer=None
    )
    return service_playbill_floor_delta(
        instance,
        head=_head(instance, head),
        base_generation=base,
        base_renderer=full.renderer if renderer is None else renderer,
    )


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _full_dir(instance: PlaybillInstance, directory: Path, generation: int) -> dict[str, bytes]:
    full = service_playbill_floor_delta(
        instance,
        head=_head(instance, generation),
        base_generation=None,
        base_renderer=None,
    )
    assert full.kind == "full"
    assert apply_floor_delta(directory, full).status == "applied"
    return _tree(directory)


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    from tests.test_floor.test_floor_index import add_lead_field

    instance, _owner = seed_write_surface(tmp_path_factory.mktemp("floor-delta"))
    _write(instance, _set(WI1, "status", "ready"), _set(WI2, "title", "Two"))
    add_lead_field(instance)
    _write(instance, _set(WI1, "lead", WI2))
    _write(instance, _set(WI3, "ruling", "".join(f"Clause {i}.\n" for i in range(60))))
    _write(instance, _set(WI1, "status", "done"), _set(WI1, "lead", WI3))
    _write(instance, _set(WI3, "ruling", "Short now.\n"))
    _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "status"}})
    return {"instance": instance, "head": len(instance.accepted_history()) - 1}


def test_applying_the_delta_at_any_base_equals_the_full_floor_at_head(
    history: dict[str, Any], tmp_path: Path
) -> None:
    instance: PlaybillInstance = history["instance"]
    head = history["head"]
    for head_generation in (head - 2, head):
        expected = _full_dir(instance, tmp_path / f"full-{head_generation}", head_generation)
        for base in range(head_generation + 1):
            directory = tmp_path / f"at-{head_generation}-{base}"
            _full_dir(instance, directory, base)
            delta = _delta(instance, head=head_generation, base=base)
            assert delta.kind == "delta" and delta.base_generation == base
            assert all(item.changed_at > base for item in delta.files)
            assert apply_floor_delta(directory, delta).status in {"applied", "unchanged"}
            assert _tree(directory) == expected, (head_generation, base)


def test_the_delta_is_the_same_cold_warm_and_coalesced(history: dict[str, Any]) -> None:
    instance: PlaybillInstance = history["instance"]
    head = history["head"]
    warm = [_delta(instance, base=base) for base in range(head + 1)]
    instance.floor_current_memo.clear()
    cold = [_delta(instance, base=base) for base in range(head + 1)]
    assert warm == cold
    # An index advanced from an old generation in one coalesced step agrees too.
    instance.floor_current_memo.clear()
    advance_floor_index(instance, _head(instance, 1))
    coalesced = [_delta(instance, base=base) for base in range(head + 1)]
    assert coalesced == warm
    assert len({delta.delta_digest for delta in warm}) == head + 1


def test_a_missing_newer_or_foreign_base_gets_the_whole_floor(history: dict[str, Any]) -> None:
    instance: PlaybillInstance = history["instance"]
    head = history["head"]
    whole = service_playbill_floor_delta(
        instance, head=_head(instance), base_generation=None, base_renderer=None
    )
    assert whole.kind == "full" and whole.tombstones == ()
    assert _delta(instance, base=0, renderer="sha256:" + "0" * 64) == whole
    assert _delta(instance, head=head - 1, base=head).kind == "full"
    at_head = _delta(instance, base=head)
    assert at_head.kind == "delta" and at_head.files == () and at_head.tombstones == ()


def test_a_delta_names_the_paths_the_floor_dropped(history: dict[str, Any], tmp_path: Path) -> None:
    instance: PlaybillInstance = history["instance"]
    head = history["head"]
    # The long ruling's sibling file exists until the ruling is shortened.
    long_at = next(
        generation
        for generation in range(head + 1)
        if f"current/{KIND}/wi-3.ruling.txt"
        in _full_dir(instance, tmp_path / f"g{generation}", generation)
    )
    delta = _delta(instance, base=long_at)
    assert f"current/{KIND}/wi-3.ruling.txt" in delta.tombstones
    manifest = read_floor_manifest(tmp_path / f"g{long_at}")
    assert manifest is not None and manifest.generation == long_at
