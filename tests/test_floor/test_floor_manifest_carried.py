"""The carried floor manifest equals a whole rebuild, generation by generation.

One instance is floored two ways. Carried: the warm index patches the
previous render's manifest inventory, and a workspace floor is brought forward
by deltas through the shared apply, which patches the manifest it last wrote.
Rebuilt: a cold index at each generation, the manifest built entry by entry
from the files' own bytes with the reference ``build_floor_manifest`` and
``render_floor_manifest``, and the floor written out file by file. The
manifests, both digests and every byte on disk are the same.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client.authoring.floor_apply import apply_floor_delta
from cruxible_client.authoring.workspace import sync_floor_directory
from cruxible_client.contracts import floor_inventory
from cruxible_client.contracts.floor import (
    FloorManifest,
    build_floor_manifest,
    content_digest,
    floor_manifest_digest,
    render_floor_manifest,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.floor.floor import service_export_playbill_floor
from cruxible_core.service.floor.floor_delta import service_playbill_floor_delta
from cruxible_core.service.floor.floor_index import FloorRender, advance_floor_index
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_floor.test_floor_index import _cold, _set, _write, add_lead_field

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"
# Enough Subjects that the inventory spans several blocks.
BULK = 300


def _rebuilt(render: FloorRender) -> FloorManifest:
    """The manifest as it was built before: every entry anew, from the files' own bytes."""

    return build_floor_manifest(
        renderer=render.renderer,
        coordinate=render.inputs.coordinate,
        generation=render.generation,
        notes_digest=render.notes_digest,
        files={
            path: (content_digest(content), len(content), changed)
            for path, (content, changed) in render.files.items()
        },
    )


def _written_out(render: FloorRender) -> dict[str, bytes]:
    """The floor written file by file from the rebuilt manifest."""

    manifest = _rebuilt(render)
    return {
        "manifest.json": render_floor_manifest(manifest),
        **{item.path: render.files[item.path][0] for item in manifest.files},
    }


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _deliver(instance: PlaybillInstance, render: FloorRender, directory: Path) -> Any:
    head = render.inputs.coordinate
    delta, applied = sync_floor_directory(
        lambda base, renderer: service_playbill_floor_delta(
            instance, head=head, base_generation=base, base_renderer=renderer
        ),
        directory,
    )
    assert applied.status == "applied", applied
    return delta


def _changes() -> list[tuple[dict[str, Any], ...]]:
    return [
        (_set(WI1, "status", "ready"), _set(WI2, "title", "Two")),
        (_set(WI1, "lead", WI2),),
        (_set(f"{KIND}/bulk-7", "status", "ready"), _set(f"{KIND}/bulk-250", "title", "New")),
        # A long text moves to a sibling file, then back: a file enters and leaves.
        (_set(WI3, "ruling", "".join(f"Clause {i}.\n" for i in range(60))),),
        (_set(WI1, "status", "done"), _set(WI1, "lead", WI3)),
        (_set(WI3, "ruling", "Short now.\n"),),
        ({"op": "retire", "target": {"subject": WI1, "field": "status"}},),
    ]


def test_carried_manifests_digests_and_floors_equal_a_whole_rebuild(tmp_path: Path) -> None:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    _write(instance, *(_set(f"{KIND}/bulk-{index}", "title", f"T{index}") for index in range(BULK)))
    carried_dir = tmp_path / "carried"
    first = advance_floor_index(instance)
    assert first.inventory_seed is None  # nothing before it to carry from
    _deliver(instance, first, carried_dir)
    add_lead_field(instance)
    carried: list[tuple[FloorRender, dict[str, bytes]]] = []
    deltas = []
    for changes in _changes():
        _write(instance, *changes)
        render = advance_floor_index(instance)
        # Patched from the render before, never built whole.
        assert render.inventory_seed is not None, render.generation
        deltas.append(_deliver(instance, render, carried_dir))
        carried.append((render, _tree(carried_dir)))
    assert {delta.kind for delta in deltas} == {"delta"}
    # Files were written, and one left the floor.
    assert all(delta.files for delta in deltas)
    assert any(delta.tombstones for delta in deltas)
    assert len(carried[-1][0].files) > 2 * 128
    for render, on_disk in carried:
        rebuilt = _rebuilt(render)
        assert render.manifest() == rebuilt, render.generation
        assert render.manifest().floor_digest == rebuilt.floor_digest
        assert render.manifest_content() == render_floor_manifest(rebuilt)
        assert render.manifest_digest == floor_manifest_digest(rebuilt)
        assert on_disk == _written_out(render), render.generation
        # A cold index at the same generation builds the same manifest whole.
        cold = _cold(instance, render.generation)
        assert cold.inventory_seed is None
        assert cold.manifest() == rebuilt
        assert cold.manifest_content() == render.manifest_content()
        assert cold.manifest_digest == render.manifest_digest
        assert _written_out(cold) == on_disk
    head = carried[-1][0]
    exported = service_export_playbill_floor(instance, at=head.inputs.coordinate)
    assert exported == _written_out(head)


def test_a_floor_read_back_cold_reaches_the_same_head(tmp_path: Path) -> None:
    """With nothing remembered (a fresh client) the manifest on disk is parsed and patched."""

    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    _write(instance, *(_set(f"{KIND}/bulk-{index}", "title", f"T{index}") for index in range(40)))
    base = advance_floor_index(instance)
    directory = tmp_path / "floor"
    full = service_playbill_floor_delta(
        instance,
        head=base.inputs.coordinate,
        base_generation=None,
        base_renderer=None,
    )
    assert apply_floor_delta(directory, full).status == "applied"
    _write(instance, _set(f"{KIND}/bulk-3", "status", "ready"), _set(WI2, "title", "Two"))
    head = advance_floor_index(instance)
    floor_inventory._KNOWN.clear()
    assert _deliver(instance, head, directory).kind == "delta"
    assert _tree(directory) == _written_out(head)
