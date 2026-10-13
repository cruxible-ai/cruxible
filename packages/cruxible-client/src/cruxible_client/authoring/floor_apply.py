"""The one floor apply: bring a floor directory to a delta's head, or refuse.

Every surface that writes a floor (the CLI, the SDK, MCP, and the daemon's
workspace-output delivery) writes it through ``apply_floor_delta``. It proves
everything before it writes anything:

1. the delta itself: every file decodes to its digest, and its paths, the
   tombstones included, are safe, unreserved and distinct on a case- and
   normalization-insensitive filesystem (the contract refuses the rest);
2. the floor the delta builds -- the directory's manifest minus the tombstones
   plus the delta's files, or the full floor's files -- has exactly the head
   manifest digest the delta names (coordinate, generation, renderer, notes and
   every file). A delta whose base the directory does not hold, and is not
   already at the head of, is ``base_mismatch``: nothing is written and the
   caller asks for a full floor;
3. the bytes actually installed: every file the delta does not touch still
   holds its manifest bytes, and nothing else is there. Each untouched file
   is checked by its stat (a regular file of its manifest size), and read
   and hashed again only when its change time is no older than the
   installed manifest (or the floor directory's last entry change, if
   earlier): a file changed since the floor was last verified. A directory
   whose entries changed since then is listed, so a stray entry or another
   spelling of a floor path is found. Every file the delta writes or
   removes is checked under its exact spelling, as a regular file (a removed
   path may also be a link, which is unlinked, never followed) or absent. A
   delta that finds a corrupted, missing, stray or aliased entry is
   ``base_mismatch`` too; a full floor reads the whole directory and repairs
   it, removing stale files but never the client's own
   (``projections/INDEX`` and ``sources/INDEX``).

Then each touched file is written atomically (a staged file renamed over it),
the removed paths are unlinked, and ``manifest.json`` is written last: it is
the commit point. A crash after any single write leaves the old manifest in
place, so applying the same delta again finishes the job; a floor already at
the head is written to only where its bytes differ.

Every read and mutation goes through directory descriptors opened one
component at a time without following links, so a directory swapped for a
symlink mid-apply is refused rather than written through.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cruxible_client._safe_files import SafeFileReadError, read_regular_file
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.floor import (
    FLOOR_LOCAL_PATHS,
    FLOOR_MANIFEST_PATH,
    FLOOR_STAGING_NAME,
    FloorApplyResult,
    FloorDelta,
    FloorManifest,
    build_floor_manifest,
    floor_manifest_digest,
    floor_path_key,
    render_floor_manifest,
)

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC
_LOCAL_KEYS = frozenset(floor_path_key(path) for path in FLOOR_LOCAL_PATHS)
_MANIFEST_KEY = floor_path_key(FLOOR_MANIFEST_PATH)


class FloorApplyError(CruxibleError, ValueError):
    """A floor delta failed verification, or the floor changed under it."""

    error_code = "cruxible.floor.apply_refused"


# -- descriptor-anchored access ----------------------------------------------------


def _split(path: str) -> tuple[tuple[str, ...], str]:
    parts = path.split("/")
    return tuple(parts[:-1]), parts[-1]


class _PathEscape(FloorApplyError):
    """A link or a file stands where a floor directory belongs."""


def _escape(parts: tuple[str, ...], exc: OSError) -> FloorApplyError:
    where = "/".join(parts) or "."
    if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}:
        return _PathEscape(
            f"floor path escapes the floor directory through a link or a file: {where}"
        )
    return FloorApplyError(f"floor directory {where} is unusable: {exc}")


@contextmanager
def _directory(root: int, parts: tuple[str, ...], *, create: bool) -> Iterator[int | None]:
    """A descriptor for ``parts`` under ``root``, opened one component at a time.

    No component is followed through a link: a symlink, or a file, where a
    directory belongs refuses the apply. Yields None when a component is
    missing and ``create`` is false.
    """

    current = os.dup(root)
    try:
        for index, name in enumerate(parts):
            try:
                child = os.open(name, _DIRECTORY, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(name, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                try:
                    child = os.open(name, _DIRECTORY, dir_fd=current)
                except OSError as exc:
                    raise _escape(parts[: index + 1], exc) from exc
            except OSError as exc:
                raise _escape(parts[: index + 1], exc) from exc
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


def _read_at(directory: int, name: str, *, max_bytes: int | None = None) -> bytes:
    """Translate the shared reader's special-file refusal into a floor refusal."""

    try:
        return read_regular_file(name, dir_fd=directory, max_bytes=max_bytes)
    except SafeFileReadError as exc:
        raise FloorApplyError(str(exc)) from exc


def _write_file(
    root: int,
    path: str,
    content: bytes,
    *,
    mode: int = 0o644,
    durable: bool = False,
    preserve_mode: bool = False,
) -> None:
    """Write ``path`` atomically: a staged file in its own directory, renamed over it."""

    parts, name = _split(path)
    with _directory(root, parts, create=True) as directory:
        assert directory is not None
        staged = f".floor-{secrets.token_hex(8)}.tmp"
        handle = os.open(staged, _CREATE, mode, dir_fd=directory)
        try:
            with os.fdopen(handle, "wb") as stream:
                if preserve_mode:
                    os.fchmod(stream.fileno(), mode)
                stream.write(content)
                if durable:
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(staged, name, src_dir_fd=directory, dst_dir_fd=directory)
            if durable:
                os.fsync(directory)
        except BaseException:
            try:
                os.unlink(staged, dir_fd=directory)
            except FileNotFoundError:
                pass
            raise


def _unlink_file(root: int, path: str) -> bool:
    """Unlink ``path`` itself (a link is removed, never followed); missing is fine."""

    parts, name = _split(path)
    with _directory(root, parts, create=False) as directory:
        if directory is None:
            return False
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            return False
        return True


def _parents(paths: Iterable[str]) -> set[str]:
    """Every directory the given paths live under, by their exact spelling."""

    return {
        "/".join(parts[:end])
        for path in paths
        for parts in (path.split("/"),)
        for end in range(1, len(parts))
    }


def _remove_directories(root: int, directories: set[str]) -> None:
    """Remove directories (emptied first by the removals), deepest first, via descriptors."""

    for path in sorted(directories, key=lambda item: item.count("/"), reverse=True):
        parts, name = _split(path)
        with _directory(root, parts, create=False) as directory:
            if directory is None:
                continue
            try:
                os.rmdir(name, dir_fd=directory)
            except FileNotFoundError:
                continue


def _prune(root: int, removed: set[str]) -> None:
    """Remove the directories the removals emptied, deepest first, never the floor itself."""

    parents = {
        tuple(path.split("/")[:end]) for path in removed for end in range(1, path.count("/") + 1)
    }
    for parts in sorted(parents, key=len, reverse=True):
        with _directory(root, parts[:-1], create=False) as directory:
            if directory is None:
                continue
            try:
                os.rmdir(parts[-1], dir_fd=directory)
            except OSError:
                continue


@dataclass
class _Installed:
    """What the floor directory actually holds, read without following links."""

    files: dict[str, str] = field(default_factory=dict)
    # Anything that is neither a regular file nor a directory: links, fifos.
    others: set[str] = field(default_factory=set)
    # Debris a crash left: staged files no rename consumed.
    staged: set[str] = field(default_factory=set)
    directories: set[str] = field(default_factory=set)
    # The client's own files, by the spelling they were found under.
    local: set[str] = field(default_factory=set)
    manifest: bytes | None = None


def _walk(root: int) -> _Installed:
    installed = _Installed()

    def visit(directory: int, prefix: str) -> None:
        for name in sorted(os.listdir(directory)):
            path = f"{prefix}{name}"
            mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
            if FLOOR_STAGING_NAME.fullmatch(name):
                installed.staged.add(path)
            elif stat.S_ISDIR(mode):
                installed.directories.add(path)
                child = os.open(name, _DIRECTORY, dir_fd=directory)
                try:
                    visit(child, f"{path}/")
                finally:
                    os.close(child)
            elif floor_path_key(path) in _LOCAL_KEYS:
                # The client's own file, never the floor's to read or remove.
                installed.local.add(path)
            elif not stat.S_ISREG(mode):
                installed.others.add(path)
            elif path == FLOOR_MANIFEST_PATH:
                installed.manifest = _read_at(directory, name)
            elif floor_path_key(path) == _MANIFEST_KEY:
                # The manifest under another spelling is not the manifest.
                installed.others.add(path)
            else:
                content = _read_at(directory, name)
                installed.files[path] = "sha256:" + hashlib.sha256(content).hexdigest()

    visit(root, "")
    return installed


class _Mismatch(Exception):
    """A touched entry is not what a delta may write over or remove."""


# A file is re-read when its change time is within this of the floor's last
# verification: older ones are proven by the stat alone. One second covers
# clock slew; filesystem timestamps are at least that fine.
_RECENT_NS = 1_000_000_000


def _changed(metadata: os.stat_result) -> int:
    return max(metadata.st_mtime_ns, metadata.st_ctime_ns)


def _inspect(
    root: int,
    delta_files: set[str],
    tombstones: set[str],
) -> _Installed:
    """The installed floor a delta builds on, verified at the cost of a stat per file.

    Reads the manifest first. Its write time (or, if earlier, the floor
    root's last entry change) is when this directory was last verified: an
    untouched file whose change time is older holds its manifest bytes unless
    its stat says otherwise, and a directory whose entries are older holds
    exactly what it held then. Anything newer is read again: files are
    hashed against the manifest, directories listed for strays and aliases.
    The delta's own files are hashed and its tombstones classified, as a
    write needs. Raises ``_Mismatch`` naming the first damaged entry.
    """

    installed = _Installed()
    if FLOOR_MANIFEST_PATH not in os.listdir(root):
        # No manifest under its exact spelling: there is no base to verify.
        return installed
    manifest_stat = os.stat(FLOOR_MANIFEST_PATH, dir_fd=root, follow_symlinks=False)
    if not stat.S_ISREG(manifest_stat.st_mode):
        raise _Mismatch(FLOOR_MANIFEST_PATH)
    installed.manifest = _read_at(root, FLOOR_MANIFEST_PATH)
    local = _manifest(installed.manifest)
    if local is None:
        return installed
    verified = min(manifest_stat.st_mtime_ns, os.fstat(root).st_mtime_ns) - _RECENT_NS
    expected = {item.path: (item.content_digest, item.byte_length) for item in local.files}
    by_parent: dict[tuple[str, ...], set[str]] = {}
    for path in (*expected, *delta_files, *tombstones):
        parts, name = _split(path)
        by_parent.setdefault(parts, set()).add(name)
    for path in FLOOR_LOCAL_PATHS:
        parts, name = _split(path)
        by_parent.setdefault(parts, set())
    for parts in list(by_parent):
        for end in range(len(parts)):
            by_parent.setdefault(parts[:end], set()).add(parts[end])
    local_names: dict[tuple[str, ...], set[str]] = {}
    for path in FLOOR_LOCAL_PATHS:
        parts, name = _split(path)
        local_names.setdefault(parts, set()).add(name)
    for parts in sorted(by_parent, key=len):
        try:
            with _directory(root, parts, create=False) as directory:
                if directory is None:
                    # Absent: every untouched file under it is missing.
                    missing = next(
                        (
                            path
                            for path in expected
                            if path.startswith("/".join(parts) + "/")
                            and path not in delta_files
                            and path not in tombstones
                        ),
                        None,
                    )
                    if missing is not None:
                        raise _Mismatch(missing)
                    continue
                if parts == () or _changed(os.fstat(directory)) >= verified:
                    allowed = by_parent[parts] | local_names.get(parts, set())
                    if parts == ():
                        allowed |= {FLOOR_MANIFEST_PATH}
                    _check_spellings(parts, os.listdir(directory), allowed, installed)
                for name in sorted(by_parent[parts]):
                    path = "/".join((*parts, name))
                    if path not in expected and path not in delta_files and path not in tombstones:
                        continue  # a directory on the way, checked as its own parent
                    _check_entry(
                        directory,
                        path,
                        name,
                        expected,
                        delta_files,
                        tombstones,
                        verified,
                        installed,
                    )
        except _PathEscape as exc:
            raise _Mismatch("/".join(parts)) from exc
    return installed


def _check_spellings(
    parts: tuple[str, ...], entries: Iterable[str], allowed: set[str], installed: _Installed
) -> None:
    """Every entry of one listed directory is an expected name, spelled exactly.

    Anything else -- a stray, or another spelling of a floor path -- is a
    mismatch; the client's own files are accepted under any spelling, as a
    full floor keeps them.
    """

    for entry in entries:
        path = "/".join((*parts, entry))
        if FLOOR_STAGING_NAME.fullmatch(entry):
            installed.staged.add(path)
        elif entry not in allowed and floor_path_key(path) not in _LOCAL_KEYS:
            raise _Mismatch(path)


def _check_entry(
    directory: int,
    path: str,
    name: str,
    expected: dict[str, tuple[str, int]],
    delta_files: set[str],
    tombstones: set[str],
    verified: int,
    installed: _Installed,
) -> None:
    """Classify one entry; an untouched file must still hold its manifest bytes."""

    untouched = path in expected and path not in delta_files and path not in tombstones
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        if untouched:
            raise _Mismatch(path) from None
        return  # a file the delta writes, or a removed path already gone
    if stat.S_ISDIR(metadata.st_mode):
        installed.directories.add(path)
        if untouched:
            raise _Mismatch(path)
        return
    if not stat.S_ISREG(metadata.st_mode):
        installed.others.add(path)
        if untouched:
            raise _Mismatch(path)
        return
    if path in delta_files:
        content = _read_at(directory, name)
        installed.files[path] = "sha256:" + hashlib.sha256(content).hexdigest()
        return
    if not untouched:
        installed.files[path] = ""  # a removed path still present
        return
    digest, size = expected[path]
    if metadata.st_size != size:
        raise _Mismatch(path)
    if _changed(metadata) >= verified:
        # Changed since the floor was last verified: read it again.
        if "sha256:" + hashlib.sha256(_read_at(directory, name)).hexdigest() != digest:
            raise _Mismatch(path)
    installed.files[path] = digest


def _manifest(content: bytes | None) -> FloorManifest | None:
    if content is None:
        return None
    try:
        return FloorManifest.model_validate(json.loads(content))
    except ValueError:
        return None


def read_floor_manifest(floor_dir: Path) -> FloorManifest | None:
    """The directory's v5 manifest, or None when it holds no valid one."""

    try:
        root = os.open(floor_dir, _DIRECTORY)
    except OSError:
        return None
    try:
        return _manifest(_read_at(root, FLOOR_MANIFEST_PATH))
    except (OSError, FloorApplyError):
        return None
    finally:
        os.close(root)


@contextmanager
def _floor_root(floor_dir: Path) -> Iterator[int]:
    floor_dir = Path(floor_dir)
    if floor_dir.is_symlink():
        raise FloorApplyError("the floor directory may not be a symlink")
    floor_dir.mkdir(parents=True, exist_ok=True)
    try:
        root = os.open(floor_dir, _DIRECTORY)
    except OSError as exc:
        raise FloorApplyError(f"the floor directory cannot be opened: {exc}") from exc
    try:
        yield root
    finally:
        os.close(root)


# -- the apply ----------------------------------------------------------------------


def _mismatch(delta: FloorDelta, message: str) -> FloorApplyResult:
    return FloorApplyResult(
        status="base_mismatch",
        kind=delta.kind,
        generation=delta.head.generation,
        message=f"{message}; ask for a full floor",
    )


def _result(
    delta: FloorDelta,
    head: FloorManifest,
    status: Literal["applied", "unchanged"],
    written: int,
    removed: int,
) -> FloorApplyResult:
    return FloorApplyResult(
        status=status,
        kind=delta.kind,
        generation=head.generation,
        manifest_digest=delta.head_manifest_digest,
        floor_digest=head.floor_digest,
        written=written,
        removed=removed,
        file_count=len(head.files),
    )


def _head_files(
    delta: FloorDelta, local: FloorManifest | None
) -> dict[str, tuple[str, int, int]] | None:
    """The head inventory the delta builds from this directory, or None for no base."""

    if delta.kind == "full":
        return {}
    if local is None:
        return None
    local_digest = floor_manifest_digest(local)
    holds_base = (
        local.generation == delta.base_generation
        and local.renderer == delta.renderer
        and local_digest == delta.base_manifest_digest
    )
    if not holds_base and local_digest != delta.head_manifest_digest:
        return None
    removed = set(delta.tombstones)
    return {
        item.path: (item.content_digest, item.byte_length, item.changed_at)
        for item in local.files
        if item.path not in removed
    }


def apply_floor_delta(floor_dir: Path, delta: FloorDelta) -> FloorApplyResult:
    """Bring ``floor_dir`` to ``delta.head``; see the module docstring for the proof order."""

    try:
        decoded = {item.path: item.content() for item in delta.files}
    except ValueError as exc:
        raise FloorApplyError(str(exc)) from exc
    with _floor_root(floor_dir) as root:
        if delta.kind == "full":
            installed = _walk(root)
        else:
            try:
                installed = _inspect(root, set(decoded), set(delta.tombstones))
            except _Mismatch as exc:
                return _mismatch(delta, f"the installed floor differs from its manifest at {exc}")
        head_files = _head_files(delta, _manifest(installed.manifest))
        if head_files is None:
            return _mismatch(
                delta,
                "the floor directory holds neither the delta's base "
                f"(generation {delta.base_generation}) nor its head",
            )
        for item in delta.files:
            head_files[item.path] = (item.sha256, len(decoded[item.path]), item.changed_at)
        try:
            head = build_floor_manifest(
                renderer=delta.renderer,
                coordinate=delta.head.coordinate(),
                generation=delta.head.generation,
                notes_digest=delta.head.notes_digest,
                files=head_files,
            )
        except ValueError as exc:
            raise FloorApplyError(f"the floor this delta builds is invalid: {exc}") from exc
        # The floor the delta builds from this directory is exactly the head it
        # names. At an installed head this proves every replayed file, every
        # tombstone and the whole coordinate against the installed manifest.
        if floor_manifest_digest(head) != delta.head_manifest_digest:
            raise FloorApplyError(
                "the floor this delta builds differs from its head manifest digest"
            )
        wanted = {item.path: item.content_digest for item in head.files}
        stale_directories: set[str] = set()
        if delta.kind == "delta":
            # Only the delta's own entries were read. Each file it writes may
            # hold base or head bytes (a crash between them), or be absent;
            # each path it removes may be a file or a link. A directory where
            # either goes refuses the delta: a full floor repairs it.
            blocked = sorted(
                path
                for path in (*decoded, *delta.tombstones)
                if path in installed.directories or (path in decoded and path in installed.others)
            )
            if blocked:
                return _mismatch(
                    delta, f"the installed floor differs from its manifest at {blocked[0]}"
                )
            removals = {
                path
                for path in delta.tombstones
                if path in installed.files or path in installed.others
            }
        else:
            # Installed entries are matched to the floor by exact spelling only.
            # Anything spelled otherwise -- a case or normalization alias of a
            # floor file, the manifest or a directory -- is never taken for it:
            # a full floor removes it and writes the one spelling.
            removals = {path for path in installed.files if path not in wanted} | installed.others
            # The directories a floor needs: those its files (and the client's
            # own) live under, by exact spelling. Any other is stale or an alias;
            # every one goes, with all it holds (its files are strays already),
            # deepest first.
            needed = _parents(wanted) | _parents(installed.local) | _parents(FLOOR_LOCAL_PATHS)
            stale_directories = installed.directories - needed
        writes = {
            path: content
            for path, content in decoded.items()
            if installed.files.get(path) != wanted[path]
        }
        manifest_bytes = render_floor_manifest(head)
        if (
            not writes
            and not removals
            and not stale_directories
            and not installed.staged
            and installed.manifest == manifest_bytes
        ):
            return _result(delta, head, "unchanged", 0, 0)
        # Everything is proven; now write. The manifest is last: the commit point.
        removed = 0
        for path in sorted(removals | installed.staged):
            if _unlink_file(root, path) and path not in installed.staged:
                removed += 1
        # A stale directory may stand where a head file goes: empty it first.
        _prune(root, removals | installed.staged)
        _remove_directories(root, stale_directories)
        for path in sorted(writes):
            _write_file(root, path, writes[path])
        if installed.manifest != manifest_bytes:
            _write_file(root, FLOOR_MANIFEST_PATH, manifest_bytes)
        return _result(delta, head, "applied", len(writes), removed)


__all__ = ["FloorApplyError", "apply_floor_delta", "read_floor_manifest"]
