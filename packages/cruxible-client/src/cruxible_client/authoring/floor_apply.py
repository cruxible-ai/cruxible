"""The one floor apply: bring a floor directory to a delta's head, or refuse.

Every surface that writes a floor (the CLI, the SDK, MCP, and the daemon's
workspace-output delivery) writes it through ``apply_floor_delta``. It proves
everything before it writes anything:

1. every file in the delta decodes to its digest, and every path stays inside
   the floor;
2. for a delta, the directory's own ``manifest.json`` is the delta's base
   (generation, renderer and manifest digest), else nothing is written and the
   result is ``base_mismatch``, and the caller asks for a full floor;
3. the manifest the delta brings it to -- the base manifest minus the
   tombstones plus the delta's files, or the full floor's files -- has the
   head manifest digest the delta names.

Then each touched file is written atomically (a temporary file renamed over
it), the tombstoned and stale paths are removed, and ``manifest.json`` is
written last: it is the commit point. A crash after any single write leaves
the base manifest in place, so applying the same delta again finishes the job;
applying it to a floor already at its head writes nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.floor import (
    PLAYBILL_FLOOR_LOCAL_PATHS,
    PLAYBILL_FLOOR_MANIFEST_PATH,
    PlaybillFloorApplyResultV1,
    PlaybillFloorDeltaV1,
    PlaybillFloorManifestV5,
    build_floor_manifest,
    content_digest,
    floor_manifest_digest,
    render_floor_manifest,
    safe_floor_path,
)


class PlaybillFloorApplyError(PlaybillError, ValueError):
    """A floor delta failed verification; nothing was written."""

    error_code = "playbill.floor.apply_refused"


def read_floor_manifest(floor_dir: Path) -> PlaybillFloorManifestV5 | None:
    """The directory's v5 manifest, or None when it holds no valid one."""

    try:
        payload = json.loads((floor_dir / PLAYBILL_FLOOR_MANIFEST_PATH).read_bytes())
        return PlaybillFloorManifestV5.model_validate(payload)
    except (OSError, ValueError):
        return None


def _target(root: Path, path: str) -> Path:
    safe_floor_path(path)
    target = root / path
    parent = target.parent.resolve()
    if not parent.is_relative_to(root) or target.is_symlink():
        raise PlaybillFloorApplyError(f"floor path escapes the floor directory: {path}")
    return target


def _holds(target: Path, digest: str) -> bool:
    try:
        return not target.is_symlink() and content_digest(target.read_bytes()) == digest
    except OSError:
        return False


def _write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(prefix=".floor-", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        os.replace(staged, target)
    finally:
        Path(staged).unlink(missing_ok=True)


def _on_disk(root: Path) -> set[str]:
    found: set[str] = set()
    for parent, _directories, names in os.walk(root, followlinks=False):
        for name in names:
            relative = (Path(parent) / name).relative_to(root).as_posix()
            if relative != PLAYBILL_FLOOR_MANIFEST_PATH and relative not in (
                PLAYBILL_FLOOR_LOCAL_PATHS
            ):
                found.add(relative)
    return found


def _prune(root: Path, removed: set[str]) -> None:
    """Remove directories the removals emptied, deepest first, never the floor itself."""

    parents = {
        parent
        for path in removed
        for parent in (root / path).parents
        if parent != root and parent.is_relative_to(root)
    }
    for directory in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            continue


def apply_floor_delta(floor_dir: Path, delta: PlaybillFloorDeltaV1) -> PlaybillFloorApplyResultV1:
    """Bring ``floor_dir`` to ``delta.head``; see the module docstring for the proof order."""

    floor_dir = Path(floor_dir)
    if floor_dir.is_symlink():
        raise PlaybillFloorApplyError("the floor directory may not be a symlink")
    floor_dir.mkdir(parents=True, exist_ok=True)
    root = floor_dir.resolve()
    try:
        decoded = {item.path: item.content() for item in delta.files}
    except ValueError as exc:
        raise PlaybillFloorApplyError(str(exc)) from exc
    targets = {path: _target(root, path) for path in (*decoded, *delta.tombstones)}
    local = read_floor_manifest(root)
    head_files: dict[str, tuple[str, int, int]]
    if delta.kind == "full":
        head_files = {}
    else:
        if (
            local is None
            or local.generation != delta.base_generation
            or local.renderer != delta.renderer
            or floor_manifest_digest(local) != delta.base_manifest_digest
        ):
            if local is not None and floor_manifest_digest(local) == delta.head_manifest_digest:
                return _unchanged(delta, local, decoded, targets)
            return PlaybillFloorApplyResultV1(
                status="base_mismatch",
                kind=delta.kind,
                generation=delta.head.generation,
                message=(
                    "the floor directory does not hold the delta's base "
                    f"(generation {delta.base_generation}); ask for a full floor"
                ),
            )
        tombstoned = set(delta.tombstones)
        head_files = {
            item.path: (item.content_digest, item.byte_length, item.changed_at)
            for item in local.files
            if item.path not in tombstoned
        }
    for item in delta.files:
        head_files[item.path] = (item.sha256, len(decoded[item.path]), item.changed_at)
    head = build_floor_manifest(
        renderer=delta.renderer,
        coordinate=delta.head.coordinate(),
        generation=delta.head.generation,
        files=head_files,
    )
    if floor_manifest_digest(head) != delta.head_manifest_digest:
        raise PlaybillFloorApplyError(
            "the floor this delta builds differs from its head manifest digest"
        )
    if local is not None and floor_manifest_digest(local) == delta.head_manifest_digest:
        return _unchanged(delta, local, decoded, targets)
    # Everything is proven; now write. The manifest is last: it is the commit point.
    written = 0
    for path, content in decoded.items():
        if not _holds(targets[path], content_digest(content)):
            _write(targets[path], content)
            written += 1
    if delta.kind == "full":
        stale = _on_disk(root) - set(head_files)
        for path in stale:
            targets[path] = _target(root, path)
    else:
        stale = set(delta.tombstones)
    removed = 0
    for path in stale:
        try:
            targets[path].unlink()
            removed += 1
        except FileNotFoundError:
            continue
    _prune(root, stale)
    _write(root / PLAYBILL_FLOOR_MANIFEST_PATH, render_floor_manifest(head))
    return PlaybillFloorApplyResultV1(
        status="applied",
        kind=delta.kind,
        generation=head.generation,
        manifest_digest=delta.head_manifest_digest,
        floor_digest=head.floor_digest,
        written=written,
        removed=removed,
        file_count=len(head.files),
    )


def _unchanged(
    delta: PlaybillFloorDeltaV1,
    local: PlaybillFloorManifestV5,
    decoded: Mapping[str, bytes],
    targets: Mapping[str, Path],
) -> PlaybillFloorApplyResultV1:
    """Already at the head: repair any delta file a hand edit changed, else write nothing."""

    written = 0
    for path, content in decoded.items():
        if not _holds(targets[path], content_digest(content)):
            _write(targets[path], content)
            written += 1
    return PlaybillFloorApplyResultV1(
        status="applied" if written else "unchanged",
        kind=delta.kind,
        generation=local.generation,
        manifest_digest=delta.head_manifest_digest,
        floor_digest=local.floor_digest,
        written=written,
        file_count=len(local.files),
    )


__all__ = ["PlaybillFloorApplyError", "apply_floor_delta", "read_floor_manifest"]
