"""Build verifiable floor v5 exports for client-side tests that stub the daemon."""

from __future__ import annotations

import base64
from collections.abc import Mapping

from cruxible_client import contracts
from cruxible_client.contracts.floor import (
    FloorDelta,
    build_floor_manifest,
    content_digest,
    floor_manifest_digest,
    render_floor_manifest,
    seal_floor_delta,
)

TEST_RENDERER = "sha256:" + "5" * 64
TEST_NOTES = "sha256:" + "6" * 64


def floor_v5_export(
    files: Mapping[str, bytes],
    *,
    coordinate: contracts.AcceptedCoordinate,
    generation: int = 1,
    changed_at: Mapping[str, int] | None = None,
    notes_digest: str = TEST_NOTES,
) -> contracts.FloorExport:
    """A full v5 floor export of ``files`` at ``coordinate``, as the daemon serves it."""

    manifest = build_floor_manifest(
        renderer=TEST_RENDERER,
        coordinate=coordinate,
        generation=generation,
        notes_digest=notes_digest,
        files={
            path: (
                content_digest(content),
                len(content),
                (changed_at or {}).get(path, generation),
            )
            for path, content in files.items()
        },
    )
    rendered = render_floor_manifest(manifest)
    return contracts.FloorExport(
        tag="playbill-floor-export-v6",
        coordinate=coordinate,
        manifest=manifest.model_dump(mode="json"),
        files=[
            contracts.FloorFile(path=path, content_base64=base64.b64encode(content).decode("ascii"))
            for path, content in {"manifest.json": rendered, **files}.items()
        ],
    )


def floor_v5_delta(
    head: Mapping[str, tuple[bytes, int]],
    *,
    coordinate: contracts.AcceptedCoordinate,
    generation: int,
    base: tuple[int, Mapping[str, tuple[bytes, int]]] | None = None,
    renderer: str = TEST_RENDERER,
    notes_digest: str = TEST_NOTES,
    base_notes_digest: str | None = None,
) -> FloorDelta:
    """The delta the daemon would serve from ``base`` (or a full floor) to ``head``.

    Each map is ``path -> (bytes, changed_at)``.
    """

    def manifest(files: Mapping[str, tuple[bytes, int]], at: int, notes: str) -> str:
        return floor_manifest_digest(
            build_floor_manifest(
                renderer=renderer,
                coordinate=coordinate,
                generation=at,
                notes_digest=notes,
                files={
                    path: (content_digest(content), len(content), changed)
                    for path, (content, changed) in files.items()
                },
            )
        )

    def item(path: str, content: bytes, changed: int) -> dict[str, object]:
        return {
            "path": path,
            "content_b64": base64.b64encode(content).decode("ascii"),
            "sha256": content_digest(content),
            "changed_at": changed,
        }

    head_coordinate = coordinate.model_dump(mode="json")
    head_coordinate.pop("tag", None)
    payload: dict[str, object] = {
        "renderer": renderer,
        "head": {**head_coordinate, "generation": generation, "notes_digest": notes_digest},
        "head_manifest_digest": manifest(head, generation, notes_digest),
    }
    ordered = sorted(head, key=lambda path: path.encode("utf-8"))
    if base is None:
        return seal_floor_delta(
            {
                **payload,
                "kind": "full",
                "files": [item(path, *head[path]) for path in ordered],
            }
        )
    base_generation, base_files = base
    return seal_floor_delta(
        {
            **payload,
            "kind": "delta",
            "base_generation": base_generation,
            "base_manifest_digest": manifest(
                base_files,
                base_generation,
                notes_digest if base_notes_digest is None else base_notes_digest,
            ),
            "files": [
                item(path, *head[path]) for path in ordered if head[path][1] > base_generation
            ],
            "tombstones": sorted(
                (path for path in base_files if path not in head),
                key=lambda path: path.encode("utf-8"),
            ),
        }
    )


def delta_from_export(export: contracts.FloorExport, *, corrupt: str | None = None) -> FloorDelta:
    """The full delta carrying exactly a v5 export's floor.

    ``corrupt`` names one file whose bytes are then swapped after sealing, as a
    damaged transport would deliver them.
    """

    manifest = export.manifest
    changed = {item["path"]: item["changed_at"] for item in manifest["files"]}
    files = {
        item.path: (base64.b64decode(item.content_base64), changed[item.path])
        for item in export.files
        if item.path != "manifest.json"
    }
    delta = floor_v5_delta(
        files,
        coordinate=export.coordinate,
        generation=manifest["generation"],
        renderer=manifest["renderer"],
        notes_digest=manifest["notes_digest"],
    )
    if corrupt is None:
        return delta
    damaged = tuple(
        item.model_copy(update={"content_b64": base64.b64encode(b"corrupt").decode("ascii")})
        if item.path == corrupt
        else item
        for item in delta.files
    )
    return delta.model_copy(update={"files": damaged})
