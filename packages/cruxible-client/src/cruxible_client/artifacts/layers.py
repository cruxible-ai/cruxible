"""Deterministic tar layers: the same files always pack to the same bytes.

A layer is an uncompressed USTAR archive of regular files, sorted by path, with
every piece of host metadata (times, owners, modes) fixed. Unpacking refuses
anything but canonical relative paths to regular files.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Mapping

MAX_LAYER_FILES = 100_000
MAX_LAYER_BYTES = 512 * 1024 * 1024


def _canonical_path(path: str) -> str:
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} or not part.isprintable() for part in parts)
    ):
        raise ValueError(f"layer path {path!r} is not a canonical relative path")
    return path


def pack_files(files: Mapping[str, bytes]) -> bytes:
    """One uncompressed tar of ``files``; identical input gives identical bytes."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(files):
            content = files[path]
            info = tarfile.TarInfo(_canonical_path(path))
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def unpack_files(
    data: bytes,
    *,
    max_files: int = MAX_LAYER_FILES,
    max_bytes: int = MAX_LAYER_BYTES,
) -> dict[str, bytes]:
    """The files in one layer; links, devices, directories and traversal refuse."""

    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            if not member.isreg():
                raise ValueError(f"layer entry {member.name!r} is not a regular file")
            path = _canonical_path(member.name)
            if path in files:
                raise ValueError(f"layer entry {path!r} appears twice")
            total += member.size
            if len(files) >= max_files or total > max_bytes:
                raise ValueError("layer exceeds its file or byte limit")
            extracted = archive.extractfile(member)
            assert extracted is not None
            files[path] = extracted.read()
    return files
