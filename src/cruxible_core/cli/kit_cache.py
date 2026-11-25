"""The client's verified kit file cache: what ``kit adopt`` and ``kit run`` read.

``kit add --commit`` (and ``kit pull``) verifies every inert file of the bundle
against its manifest sha256 and keeps one copy of the manifest and those files
at ``<state-root>/kit-files/<kit_id>/sha256-<hex>/`` (``CRUXIBLE_KIT_CACHE_DIR``
overrides the root), so the user's workspace is never touched. The entry is
keyed by the release's ``bundle_digest`` when it declares recipes or scripts
(what its kit receipt pins), else by its ``content_digest``: two builds with the
same definitions but other recipes or scripts never share an entry.

Before ``kit adopt`` renders a recipe or ``kit run`` executes a script, the
client checks that the kit is installed in the instance (its one receipt), that
the cached manifest is the installed release (``content_digest`` and the
receipt's ``bundle_digest``), and that every file of the entry has the sha256
that manifest records, read without following a symbolic link. A missing entry
is fetched again from the installed kit's recorded source (or ``--from PATH``)
and verified the same way; ``--from`` and ``kit pull`` replace a damaged entry.

Entries are published and replaced under a per-entry lock: a writer stages the
new copy beside the entry and swaps it in while holding the lock exclusively,
and a reader holds it shared while it reads and verifies, so no reader sees a
missing or half-replaced entry. The lock, the staging copy, the swap, a crash's
backup and every removal are made relative to directory descriptors held from
the cache root down (opened ``O_NOFOLLOW``), so no parent pathname is resolved
again once it is checked. ``kit run`` hands its script a private,
verified snapshot of the files (``CRUXIBLE_KIT_FILES``), removed on exit.

These checks catch a stale or damaged copy (another release under the same
kit id, a partial write, a stray edit). They are not a boundary against a
process running as the same user, which can rewrite any file here.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from cruxible_client._safe_files import ContainmentError, HeldRoot
from cruxible_client.contracts.canonical import Sha256Value, pretty_canonical_bytes
from cruxible_client.contracts.kits import (
    KIT_MANIFEST_FILE,
    InstalledKit,
    KitBundle,
    KitManifest,
    KitRecipeSpec,
    KitScriptSpec,
    kit_file_path_allowed,
    registry_source,
)
from cruxible_client.contracts.workspace_layout import daemon_state_root
from cruxible_client.errors import ConfigError

KIT_CACHE_ENV = "CRUXIBLE_KIT_CACHE_DIR"
_CACHE_DIRECTORY = "kit-files"
_LOCK_DIRECTORY = ".locks"
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC


class KitCacheError(ConfigError):
    """A cached kit copy that cannot be used: missing, another release, or altered."""

    error_code = "cruxible.kit.cache_invalid"

    def __init__(self, detail: str, repair: str) -> None:
        self.detail = detail
        self.repair = repair
        super().__init__(f"{self.error_code}: {detail}; next: {repair}")


class KitNotInstalled(ConfigError):
    """``kit adopt`` / ``kit run`` named a kit this instance has not installed."""

    error_code = "cruxible.kit.not_installed"

    def __init__(self, kit_id: str, installed: tuple[str, ...] | None = None) -> None:
        held = "" if installed is None else f" (installed: {', '.join(installed) or 'none'})"
        super().__init__(
            f"{self.error_code}: kit {kit_id} is not installed in this instance{held}; "
            f"next: cruxible kit add {kit_id} --yes (kit status lists the installed kits)"
        )


class KitEntryUnknown(ConfigError):
    """The installed release declares no recipe or script of that name."""

    error_code = "cruxible.kit.entry_unknown"

    def __init__(self, kit_id: str, label: str, name: str, declared: tuple[str, ...]) -> None:
        names = ", ".join(declared) if declared else "none"
        command = "adopt" if label == "recipe" else "run"
        super().__init__(
            f"{self.error_code}: kit {kit_id} declares no {label} {name} (declared: {names}); "
            f"next: cruxible kit {command} {kit_id} --list"
        )


def kit_cache_root(environ: Mapping[str, str] | None = None) -> Path:
    """``CRUXIBLE_KIT_CACHE_DIR``, else ``<state-root>/kit-files``."""

    env = os.environ if environ is None else environ
    raw = env.get(KIT_CACHE_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return daemon_state_root(env) / _CACHE_DIRECTORY


def kit_cache_directory(kit_id: str, digest: str, root: Path | None = None) -> Path:
    """``<root>/<kit_id>/sha256-<hex>``: one release's verified files, keyed by
    ``digest`` (its pinned ``bundle_digest``, else its ``content_digest``)."""

    hex_digest = Sha256Value.from_tagged(digest).value
    return (root or kit_cache_root()) / kit_id / f"sha256-{hex_digest}"


def manifest_cache_key(manifest: KitManifest) -> str:
    """The digest a release's cache entry is keyed by: its pinned bundle digest
    (it declares recipes or scripts), else its content digest."""

    return manifest.pinned_bundle_digest or manifest.content_digest


def installed_cache_key(kit: InstalledKit) -> str:
    """The digest the installed release's cache entry is keyed by (its receipt's pins)."""

    return kit.bundle_digest or kit.content_digest


def _sha256(content: bytes) -> str:
    return Sha256Value(hashlib.sha256(content).hexdigest()).tagged


def _refuse_links(directory: Path) -> None:
    """Refuse a symbolic link at any cache component below the root: the kit's
    directory, the entry, its backup, or the kit's lock directory. Checked with
    ``lstat`` before anything there is created, read or replaced, so neither a
    read nor a repair follows a link out of the cache."""

    root = directory.parent.parent
    for path in (
        directory.parent,
        directory,
        _backup_path(directory),
        root / _LOCK_DIRECTORY,
        root / _LOCK_DIRECTORY / directory.parent.name,
    ):
        if path.is_symlink():
            raise _link_refused(path)


def _backup_path(directory: Path) -> Path:
    """Where a replaced entry waits until its successor is in place."""

    return directory.with_name(f".prev-{directory.name}")


def _link_refused(path: Path) -> KitCacheError:
    return KitCacheError(
        f"the kit cache path {path} is a symbolic link (refused, never followed)",
        f"remove {path} and rerun with --from KIT",
    )


def _lstat_at(dir_fd: int, name: str) -> os.stat_result | None:
    """``name`` under the held ``dir_fd``, not followed; ``None`` when absent."""

    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _is_link_at(dir_fd: int, name: str) -> bool:
    found = _lstat_at(dir_fd, name)
    return found is not None and stat.S_ISLNK(found.st_mode)


def _open_directory_at(dir_fd: int, name: str, where: Path, *, create: bool) -> int:
    """A descriptor for the directory ``name`` under the held ``dir_fd`` (made first
    when ``create``), opened ``O_NOFOLLOW``: a symbolic link there is refused."""

    if create:
        with suppress(FileExistsError):
            os.mkdir(name, dir_fd=dir_fd)
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if _is_link_at(dir_fd, name):
            raise _link_refused(where) from exc
        raise


def _remove_at(dir_fd: int, name: str) -> None:
    """Remove ``name`` (a whole tree) under the held ``dir_fd``: each directory is
    opened ``O_NOFOLLOW`` and emptied through its descriptor, a link is unlinked
    as itself. O(the files below ``name``)."""

    found = _lstat_at(dir_fd, name)
    if found is None:
        return
    if not stat.S_ISDIR(found.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
    try:
        for child in os.listdir(descriptor):
            _remove_at(descriptor, child)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=dir_fd)


@dataclass(frozen=True)
class _HeldKit:
    """The ``<root>/<kit_id>`` directory as a descriptor held while the entry's lock
    is: the entry, its backup and its staging copies are named relative to it."""

    fd: int
    directory: Path
    entry: str

    @property
    def backup(self) -> str:
        return _backup_path(self.directory).name


def _recover_backup(kit: _HeldKit) -> None:
    """Put a backup left by an interrupted replacement back when the entry is absent.

    Called under the entry's lock (either mode): a writer that crashed between
    moving the old entry aside and publishing the new one leaves only the backup.
    """

    if _lstat_at(kit.fd, kit.entry) is not None or _lstat_at(kit.fd, kit.backup) is None:
        return
    try:
        os.rename(kit.backup, kit.entry, src_dir_fd=kit.fd, dst_dir_fd=kit.fd)
    except FileNotFoundError:
        pass  # another reader holding the shared lock restored it first


@contextmanager
def _entry_lock(directory: Path, *, exclusive: bool) -> Iterator[_HeldKit]:
    """The entry's lock (``<root>/.locks/<kit_id>/<entry>.lock``): exclusive while a
    writer publishes or replaces the entry, shared while a reader verifies it.

    Yields the kit's directory as a held descriptor. The kit's directory, the lock
    directories and the lock file are each opened ``O_NOFOLLOW`` from the one
    before, starting at the cache root, so a symbolic link swapped in anywhere
    below the root after the first check is refused rather than followed. Once
    held, restores a backup an interrupted replacement left in place of the entry.
    """

    _refuse_links(directory)
    root = directory.parent.parent
    kit_id = directory.parent.name
    root.mkdir(parents=True, exist_ok=True)
    with ExitStack() as held:
        root_fd = os.open(root.resolve(strict=True), _DIRECTORY_FLAGS)
        held.callback(os.close, root_fd)
        kit_fd = _open_directory_at(root_fd, kit_id, directory.parent, create=True)
        held.callback(os.close, kit_fd)
        locks_fd = _open_directory_at(root_fd, _LOCK_DIRECTORY, root / _LOCK_DIRECTORY, create=True)
        held.callback(os.close, locks_fd)
        kit_locks_fd = _open_directory_at(
            locks_fd, kit_id, root / _LOCK_DIRECTORY / kit_id, create=True
        )
        held.callback(os.close, kit_locks_fd)
        lock_fd = os.open(
            f"{directory.name}.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=kit_locks_fd,
        )
        held.callback(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        kit = _HeldKit(fd=kit_fd, directory=directory, entry=directory.name)
        for name in (kit.entry, kit.backup):
            if _is_link_at(kit_fd, name):
                raise _link_refused(directory.parent / name)
        _recover_backup(kit)
        yield kit


def _verified_files(bundle: KitBundle) -> dict[str, bytes]:
    """Every manifest file's bytes, each checked against its manifest sha256."""

    manifest = bundle.manifest
    contents = bundle.file_contents()
    expected = manifest.kit_files()
    missing = sorted(set(expected) - set(contents))
    if missing:
        raise KitCacheError(
            f"the kit {manifest.kit_id} bundle lacks the bytes of {', '.join(missing)}",
            "add or pull the kit by reference so its files travel with it",
        )
    for path, digest in expected.items():
        if not kit_file_path_allowed(path):
            raise KitCacheError(f"kit file path {path!r} is not allowed", "fix the kit")
        if _sha256(contents[path]) != digest:
            raise KitCacheError(
                f"kit file {path} differs from its manifest sha256",
                "fetch the release again; the kit bundle is damaged",
            )
    return {path: contents[path] for path in sorted(expected)}


class _Damaged(Exception):
    """A cache entry whose bytes are not what its manifest records."""


def _read_entry(directory: Path, relative: str) -> bytes:
    """``relative`` inside the entry, read from descriptors held from the cache
    root down, without following a symbolic link anywhere below the root (the
    kit's directory and the entry included)."""

    with HeldRoot(directory.parent.parent) as held:
        return held.read(f"{directory.parent.name}/{directory.name}/{relative}")


def _entry_manifest_bytes(directory: Path) -> bytes | None:
    """The entry's manifest bytes, ``None`` when the entry is absent."""

    if not os.path.lexists(directory):
        return None
    try:
        return _read_entry(directory, KIT_MANIFEST_FILE)
    except FileNotFoundError:
        raise _Damaged(f"the cached copy {directory} has no manifest") from None
    except (ContainmentError, OSError) as exc:
        raise _Damaged(f"cannot read the cached manifest in {directory} ({exc})") from exc


def _entry_files(directory: Path, manifest: KitManifest) -> dict[str, bytes]:
    """Every file the manifest lists, read no-follow and checked against its sha256."""

    contents: dict[str, bytes] = {}
    for path, expected in sorted(manifest.kit_files().items()):
        if not kit_file_path_allowed(path):
            raise _Damaged(f"the cached manifest lists a disallowed path {path!r}")
        try:
            content = _read_entry(directory, path)
        except FileNotFoundError:
            raise _Damaged(f"the cached kit file {directory / path} is missing") from None
        except (ContainmentError, OSError) as exc:
            raise _Damaged(
                f"cannot read the cached kit file {directory / path} ({exc}; a symbolic "
                "link or another non-regular file is refused)"
            ) from exc
        actual = _sha256(content)
        if actual != expected:
            raise _Damaged(
                f"the cached kit file {directory / path} has sha256 {actual}, not the "
                f"manifest's {expected} (sha256 mismatch: altered or damaged)"
            )
        contents[path] = content
    return contents


def _intact(directory: Path, manifest_bytes: bytes) -> bool:
    """Whether the entry holds exactly ``manifest_bytes`` and every file it lists."""

    try:
        if _entry_manifest_bytes(directory) != manifest_bytes:
            return False
        _entry_files(directory, KitManifest.model_validate_json(manifest_bytes))
    except (_Damaged, ValueError):
        return False
    return True


def _make_staging(kit: _HeldKit) -> tuple[str, int]:
    """A new private directory beside the entry, and its held descriptor."""

    for _attempt in range(100):
        name = f".staging-{kit.entry}-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, 0o700, dir_fd=kit.fd)
        except FileExistsError:
            continue
        return name, _open_directory_at(kit.fd, name, kit.directory.parent / name, create=False)
    raise FileExistsError(f"no free staging name beside {kit.directory}")


def _write_files_at(dir_fd: int, where: Path, files: Mapping[str, bytes]) -> None:
    """Each file created read-only under the held staging ``dir_fd`` (its parent
    directories made and opened ``O_NOFOLLOW``, the file ``O_EXCL``)."""

    for relative, content in files.items():
        *parents, leaf = relative.split("/")
        with ExitStack() as opened:
            current = dir_fd
            for index, part in enumerate(parents):
                current = _open_directory_at(
                    current, part, where.joinpath(*parents[: index + 1]), create=True
                )
                opened.callback(os.close, current)
            descriptor = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=current,
            )
            try:
                view = memoryview(content)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fchmod(descriptor, 0o444)
            finally:
                os.close(descriptor)


def write_kit_cache(bundle: KitBundle, root: Path | None = None) -> Path:
    """Keep a verified copy of ``bundle``'s manifest and inert files; returns its directory.

    An existing entry is kept only when its manifest is these bytes and every
    file it lists still has its sha256 (read no-follow); any other entry --
    another build, a missing, altered or symlinked file -- is replaced whole,
    never mixed. The new copy is staged beside the entry and swapped in under
    the entry's exclusive lock, so a reader (holding it shared) never sees the
    entry absent or half-replaced. The old entry is moved to a backup first and
    removed only after the new one is in place; a failed swap restores it before
    the lock is released, and a backup a crash left behind is restored by the
    next holder of the lock. A symbolic link anywhere below the cache root is
    refused. O(the kit's inert files).
    """

    manifest = bundle.manifest
    files = _verified_files(bundle)
    target = kit_cache_directory(manifest.kit_id, manifest_cache_key(manifest), root)
    manifest_bytes = pretty_canonical_bytes(manifest.model_dump(mode="json"))
    with _entry_lock(target, exclusive=True) as kit:
        if _intact(target, manifest_bytes):
            return target
        staging, staging_fd = _make_staging(kit)
        try:
            try:
                _write_files_at(
                    staging_fd,
                    target.parent / staging,
                    {**files, KIT_MANIFEST_FILE: manifest_bytes},
                )
            finally:
                os.close(staging_fd)
            if _lstat_at(kit.fd, kit.entry) is not None:
                # The entry is present, so a leftover backup is stale.
                with suppress(OSError):
                    _remove_at(kit.fd, kit.backup)
                os.rename(kit.entry, kit.backup, src_dir_fd=kit.fd, dst_dir_fd=kit.fd)
            try:
                os.rename(staging, kit.entry, src_dir_fd=kit.fd, dst_dir_fd=kit.fd)
            except BaseException:
                # Put the old entry back before the lock is released; the backup
                # is only ever removed once its successor is in place.
                if (
                    _lstat_at(kit.fd, kit.backup) is not None
                    and _lstat_at(kit.fd, kit.entry) is None
                ):
                    os.rename(kit.backup, kit.entry, src_dir_fd=kit.fd, dst_dir_fd=kit.fd)
                raise
            with suppress(OSError):
                _remove_at(kit.fd, kit.backup)
        finally:
            with suppress(OSError):
                _remove_at(kit.fd, staging)
    return target


def installed_kit(status_kits: tuple[InstalledKit, ...], kit_id: str) -> InstalledKit:
    """The installed kit ``kit_id`` from a ``kit status`` answer, or a typed refusal."""

    for kit in status_kits:
        if kit.kit_id == kit_id:
            return kit
    raise KitNotInstalled(kit_id)


@dataclass(frozen=True)
class CachedKit:
    """A cached release whose manifest is the installed one's, with every file's
    verified bytes (read once, under the entry's lock)."""

    directory: Path
    manifest: KitManifest
    contents: Mapping[str, bytes] = field(default_factory=dict)

    def recipe(self, name: str) -> KitRecipeSpec:
        spec = self.manifest.recipe(name)
        if spec is None:
            raise KitEntryUnknown(
                self.manifest.kit_id,
                "recipe",
                name,
                tuple(item.name for item in self.manifest.recipes),
            )
        return spec

    def script(self, name: str) -> KitScriptSpec:
        spec = self.manifest.script(name)
        if spec is None:
            raise KitEntryUnknown(
                self.manifest.kit_id,
                "script",
                name,
                tuple(item.name for item in self.manifest.scripts),
            )
        return spec

    def verified_bytes(self, path: str) -> bytes:
        """The file's verified bytes, as read and checked when the copy was loaded."""

        if path not in self.manifest.kit_files():
            raise KitCacheError(
                f"the cached manifest lists no file {path}",
                f"remove {self.directory} and rerun with --from KIT",
            )
        if path not in self.contents:
            raise KitCacheError(
                f"the kit file {path} was not verified (the copy was loaded for counts only)",
                "rerun the command",
            )
        return self.contents[path]

    def stage(self, destination: Path) -> Path:
        """A private snapshot of the verified files (and manifest) at ``destination``."""

        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for path in sorted(self.manifest.kit_files()):
            target = destination / path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(self.verified_bytes(path))
            target.chmod(0o444)
        manifest = destination / KIT_MANIFEST_FILE
        manifest.write_bytes(pretty_canonical_bytes(self.manifest.model_dump(mode="json")))
        manifest.chmod(0o444)
        return destination


def _check_release(manifest: KitManifest, kit: InstalledKit, where: str) -> None:
    """Refuse a manifest that is not the installed release (id, content and the
    receipt's pinned bundle digest)."""

    if (manifest.kit_id, manifest.content_digest) != (kit.kit_id, kit.content_digest):
        raise KitCacheError(
            f"{where} holds kit {manifest.kit_id} {manifest.version} content "
            f"{manifest.content_digest}, not the installed {kit.kit_id} {kit.version} "
            f"content {kit.content_digest}",
            f"rerun with --from naming the installed release {kit.kit_id} {kit.version}",
        )
    pinned = manifest.pinned_bundle_digest
    if kit.bundle_digest is None and pinned is not None:
        raise KitCacheError(
            f"{where} declares recipes or scripts, but the installed {kit.kit_id} "
            f"{kit.version} pins none (its receipt records no bundle digest)",
            f"cruxible kit add the release again to pin its recipes and scripts "
            f"({kit.kit_id} {kit.version})",
        )
    if kit.bundle_digest is not None and pinned != kit.bundle_digest:
        raise KitCacheError(
            f"{where} is bundle {pinned or manifest.bundle_digest}, not the installed "
            f"{kit.kit_id} {kit.version} bundle {kit.bundle_digest} (the same definitions "
            "built with other recipes, scripts or files)",
            f"rerun with --from naming the exact build installed as {kit.kit_id} {kit.version}",
        )


def _load_entry(directory: Path, kit: InstalledKit, *, verify_files: bool) -> CachedKit | None:
    """The entry at ``directory`` checked against ``kit``; ``None`` when absent."""

    repair = f"rerun with --from KIT, or kit pull the release again (replaces {directory})"
    with _entry_lock(directory, exclusive=False):
        try:
            raw = _entry_manifest_bytes(directory)
            if raw is None:
                return None
            try:
                manifest = KitManifest.model_validate_json(raw)
            except ValueError as exc:
                raise _Damaged(f"the cached manifest in {directory} is not a kit manifest") from exc
            _check_release(manifest, kit, f"the cached copy {directory}")
            contents = _entry_files(directory, manifest) if verify_files else {}
        except _Damaged as exc:
            raise KitCacheError(str(exc), repair) from exc
    return CachedKit(directory=directory, manifest=manifest, contents=contents)


def load_installed_kit(
    kit: InstalledKit,
    *,
    from_path: str | None = None,
    root: Path | None = None,
    offline: bool = False,
    verify_files: bool = True,
) -> CachedKit:
    """The cached copy of the installed release ``kit``, fetched and cached when absent.

    ``from_path`` (``--from``) is read first when given (replacing a damaged
    entry); otherwise the cached entry is used, and only an absent one is
    fetched from the installed kit's recorded source (never when ``offline``; a
    recorded local directory that no longer exists asks for ``--from`` instead
    of trying a registry). Either way the copy must be the installed release --
    its ``content_digest`` and the receipt's ``bundle_digest`` -- and every file
    is verified. ``verify_files=False`` checks the manifest only (a status line).
    """

    from cruxible_client.kits import resolve_kit
    from cruxible_core.kits.base_kit import bundled_kit_reference

    directory = kit_cache_directory(kit.kit_id, installed_cache_key(kit), root)
    if from_path is None:
        cached = _load_entry(directory, kit, verify_files=verify_files)
        if cached is not None:
            return cached
    reference = from_path or kit.source
    if offline and from_path is None:
        raise KitCacheError(
            f"no cached copy of kit {kit.kit_id} {kit.version}", "rerun with --from KIT"
        )
    if reference is None:
        raise KitCacheError(
            f"no cached copy of kit {kit.kit_id} {kit.version} and no recorded source",
            f"rerun with --from KIT (the kit directory, layout or reference of {kit.version})",
        )
    bundled = bundled_kit_reference(reference)
    if (
        from_path is None
        and bundled is None
        and not registry_source(reference)
        and not Path(reference).expanduser().exists()
    ):
        raise KitCacheError(
            f"no cached copy of kit {kit.kit_id} {kit.version}, and its recorded source "
            f"{reference} (a local kit directory or layout) is not on disk here",
            f"rerun with --from KIT (the kit directory, layout or reference of {kit.version})",
        )
    try:
        bundle, _origin = bundled or resolve_kit(reference)
    except ConfigError as exc:
        raise KitCacheError(
            f"no cached copy of kit {kit.kit_id} {kit.version}, and fetching "
            f"{reference} failed ({exc})",
            f"rerun with --from KIT (the kit directory, layout or reference of {kit.version})",
        ) from exc
    _check_release(bundle.manifest, kit, reference)
    written = write_kit_cache(bundle, root)
    cached = _load_entry(written, kit, verify_files=True)
    if cached is None:
        raise KitCacheError(
            f"the cached copy {written} vanished after it was written",
            "rerun the command",
        )
    return cached


__all__ = [
    "KIT_CACHE_ENV",
    "CachedKit",
    "KitCacheError",
    "KitEntryUnknown",
    "KitNotInstalled",
    "installed_cache_key",
    "installed_kit",
    "kit_cache_directory",
    "kit_cache_root",
    "load_installed_kit",
    "manifest_cache_key",
    "write_kit_cache",
]
