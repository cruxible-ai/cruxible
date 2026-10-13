"""The floor delta: what brings a client's floor at one generation to another.

A file's ``changed_at`` is the latest generation that touched any of its
inputs, so a client at ``base`` lacks exactly the files whose ``changed_at`` is
after it, plus the paths the floor dropped since. The base floor is rendered
from the floor index by patching it back to ``base`` (the same ledger diff the
index advances by), so the delta, its tombstones and the base manifest digest
are a deterministic function of (head, base) for one renderer, whatever the
index held when it was asked. A missing, unknown, newer or foreign base gets
the whole floor.

Nothing is hashed here: every file's digest was taken once, when the index
rendered it, and each floor's manifest digest is built once per render. A base
the index rendered recently is read from memory, and the files to compare are
the union of the forward steps since that base; only a base outside them is
patched back to and compared whole.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable

from cruxible_client.contracts.errors import CruxibleError, ProjectionIntegrityError
from cruxible_client.contracts.floor import (
    FloorDelta,
    FloorHead,
    seal_floor_delta,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.floor.floor_index import (
    FloorRender,
    advance_floor_index,
    floor_render_at,
    floor_render_from,
    recent_floor_render,
)


def _file(render: FloorRender, path: str) -> dict[str, object]:
    content, changed_at = render.files[path]
    return {
        "path": path,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "sha256": render.digests[path][0],
        "changed_at": changed_at,
    }


def _byte_order(paths: Iterable[str]) -> list[str]:
    return sorted(paths, key=lambda item: item.encode("utf-8"))


def _base_render(
    instance: PlaybillInstance,
    head: FloorRender,
    base_generation: int | None,
    base_renderer: str | None,
) -> FloorRender | None:
    """The client's base floor, or None when only a full floor answers it."""

    if base_generation is None or base_renderer is None or base_renderer != head.renderer:
        return None
    if base_generation < 0 or base_generation > head.generation:
        return None
    recent = recent_floor_render(instance, head, base_generation)
    if recent is not None:
        return recent
    try:
        return floor_render_from(instance, head, base_generation)
    except CruxibleError:
        # Another renderer made the floor at that generation (a compiler
        # succession in between), or it is outside this accepted history.
        return None


def service_playbill_floor_delta(
    instance: PlaybillInstance,
    *,
    head: AcceptedCoordinate,
    base_generation: int | None,
    base_renderer: str | None,
) -> FloorDelta:
    """The delta from the client's ``base`` floor to the floor at ``head``.

    ``kind="delta"`` carries every file whose ``changed_at`` is after the base
    and every path the base held that the head does not. ``kind="full"``
    carries the whole floor; it answers a missing base, a base outside this
    accepted history or past the head, and a base of another renderer.
    """

    coordinate = instance.resolve_accepted_coordinate(
        git_oid=head.git_oid,
        semantic_root=head.semantic_root,
        generation_root=head.generation_root,
        compiler_digest=head.compiler_digest,
    )
    render = floor_render_at(instance, coordinate)
    accepted = render.inputs.coordinate
    payload: dict[str, object] = {
        "renderer": render.renderer,
        "head": FloorHead(
            git_oid=accepted.git_oid,
            generation=render.generation,
            semantic_root=accepted.semantic_root,
            generation_root=accepted.generation_root,
            compiler_digest=accepted.compiler_digest,
            notes_digest=render.notes_digest,
        ).model_dump(mode="json"),
        "head_manifest_digest": render.manifest_digest,
    }
    base = _base_render(instance, render, base_generation, base_renderer)
    if base is None:
        return seal_floor_delta(
            {
                **payload,
                "kind": "full",
                "base_generation": None,
                "base_manifest_digest": None,
                "files": [_file(render, path) for path in _byte_order(render.files)],
                "tombstones": [],
            }
        )
    # The files that can differ: those the head's forward steps since the
    # base touched; without such a chain, every file of either floor.
    candidates = render.changed_paths_since(base.generation)
    if candidates is None:
        candidates = frozenset({*render.files, *base.files})
    files = []
    tombstones = []
    for path in _byte_order(candidates):
        held = render.files.get(path)
        if held is None:
            if path in base.files:
                tombstones.append(path)
        elif held[1] > base.generation:
            files.append(_file(render, path))
        elif base.files.get(path) != held:
            # A file no input of which moved since the base must be its bytes.
            raise ProjectionIntegrityError(
                f"floor file {path} changed without its stamp moving past the base"
            )
    return seal_floor_delta(
        {
            **payload,
            "kind": "delta",
            "base_generation": base.generation,
            "base_manifest_digest": base.manifest_digest,
            "files": files,
            "tombstones": tombstones,
        }
    )


__all__ = ["advance_floor_index", "service_playbill_floor_delta"]
