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
_BLOCK = 512
_ZERO_BLOCK = bytes(_BLOCK)


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


def _scan_headers(data: bytes, *, max_files: int, max_bytes: int) -> None:
    """Walk the raw USTAR headers before ``tarfile`` interprets any of them.

    Only plain regular-file headers are admitted: PAX and GNU extension headers
    (whose payloads ``tarfile`` would otherwise parse before yielding a member),
    links and devices refuse here, and the file count and payload bytes are
    bounded before anything is read.
    """

    offset = files = total = 0
    while offset + _BLOCK <= len(data):
        header = data[offset : offset + _BLOCK]
        if header == _ZERO_BLOCK:
            return
        if header[257:263] not in {b"ustar\x00", b"ustar "}:
            raise ValueError("layer entry is not a USTAR header")
        if header[156:157] not in {b"0", b"\x00"}:
            raise ValueError("layer entry is not a regular file")
        try:
            size = int(header[124:136].rstrip(b"\x00 ").decode("ascii") or "0", 8)
        except ValueError as exc:
            raise ValueError("layer entry has a malformed size") from exc
        files += 1
        total += size
        if files > max_files or total > max_bytes:
            raise ValueError("layer exceeds its file or byte limit")
        offset += _BLOCK + (size + _BLOCK - 1) // _BLOCK * _BLOCK
    raise ValueError("layer is truncated")


def unpack_files(
    data: bytes,
    *,
    max_files: int = MAX_LAYER_FILES,
    max_bytes: int = MAX_LAYER_BYTES,
) -> dict[str, bytes]:
    """The files in one layer; links, devices, directories and traversal refuse."""

    _scan_headers(data, max_files=max_files, max_bytes=max_bytes)
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
