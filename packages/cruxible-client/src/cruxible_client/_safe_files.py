"""One nonblocking regular-file reader for workspace-controlled inputs.

Check the opened descriptor, not a pathname pre-check: replacing a regular file
with a FIFO must refuse promptly. Callers may anchor a basename to a held parent
and bound reads used for comparisons or processing budgets.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class SafeFileReadError(OSError):
    """The opened input is not a regular file."""


def read_regular_file(
    path: str | Path, *, dir_fd: int | None = None, max_bytes: int | None = None
) -> bytes:
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("read limit must not be negative")
    handle = os.open(path, _READ, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise SafeFileReadError(f"{path} is not a regular file")
        chunks = []
        remaining = max_bytes
        while remaining is None or remaining > 0:
            chunk = os.read(handle, (1 << 20) if remaining is None else min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(handle)


class ContainmentError(ValueError):
    """A path beneath a held root crosses a symbolic link (or leaves the root)."""


_DIRECTORY = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)


def _relative_parts(relative: str) -> list[str]:
    parts = relative.split("/")
    if not relative or relative.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ContainmentError(f"{relative!r} is not a relative path beneath the root")
    return parts


def _symlink_refusal(relative: str) -> ContainmentError:
    return ContainmentError(f"{relative} is (or sits under) a symbolic link")


class HeldRoot:
    """A directory held open by descriptor; every read walks beneath it, atomically.

    The named root is opened once. Each later path is opened component by
    component from held directory descriptors with ``O_NOFOLLOW`` (and
    ``O_DIRECTORY`` for each parent), and its bytes come from the final held
    descriptor. Swapping a parent for a symbolic link between a check and the
    open is therefore refused, never followed; no pathname is resolved twice.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._fd = os.open(
            root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> HeldRoot:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _open_directory(self, parts: list[str], relative: str) -> int:
        """A new descriptor for the directory ``parts`` names beneath the root."""

        current = os.dup(self._fd)
        for part in parts:
            try:
                following = os.open(part, _DIRECTORY, dir_fd=current)
            except OSError as exc:
                symlink = exc.errno == errno.ELOOP or (
                    exc.errno == errno.ENOTDIR
                    and stat.S_ISLNK(os.lstat(part, dir_fd=current).st_mode)
                )
                os.close(current)
                if symlink:
                    raise _symlink_refusal(relative) from exc
                raise
            os.close(current)
            current = following
        return current

    def read(self, relative: str, *, max_bytes: int | None = None) -> bytes:
        """The regular file at ``relative`` beneath the root, read from its held descriptor."""

        parts = _relative_parts(relative)
        directory = self._open_directory(parts[:-1], relative)
        try:
            return read_regular_file(parts[-1], dir_fd=directory, max_bytes=max_bytes)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _symlink_refusal(relative) from exc
            raise
        finally:
            os.close(directory)

    def files(self, relative: str, *, missing_ok: bool = False) -> list[str]:
        """Every file beneath directory ``relative``, as paths relative to it, sorted.

        Listed through held descriptors (``os.scandir`` on each); a symbolic link
        anywhere beneath it is refused.
        """

        try:
            directory = self._open_directory(_relative_parts(relative), relative)
        except FileNotFoundError:
            if missing_ok:
                return []
            raise
        found: list[str] = []

        def walk(descriptor: int, prefix: str) -> None:
            with os.scandir(descriptor) as entries:
                listed = sorted(entries, key=lambda entry: entry.name)
            for entry in listed:
                name = f"{prefix}{entry.name}"
                if entry.is_symlink():
                    raise _symlink_refusal(f"{relative}/{name}")
                if entry.is_dir(follow_symlinks=False):
                    try:
                        child = os.open(entry.name, _DIRECTORY, dir_fd=descriptor)
                    except OSError as exc:
                        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                            raise _symlink_refusal(f"{relative}/{name}") from exc
                        raise
                    try:
                        walk(child, f"{name}/")
                    finally:
                        os.close(child)
                else:
                    found.append(name)

        try:
            walk(directory, "")
        finally:
            os.close(directory)
        return sorted(found)
