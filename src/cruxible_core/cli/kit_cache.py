"""The client's verified kit file cache: what ``kit adopt`` and ``kit run`` read.

``kit add --commit`` (and ``kit pull``) verifies every inert file of the bundle
against its manifest sha256 and keeps one copy of the manifest and those files
at ``<state-root>/kit-files/<kit_id>/sha256-<hex>/`` (``CRUXIBLE_KIT_CACHE_DIR``
overrides the root), so the user's workspace is never touched.

Before ``kit adopt`` renders a recipe or ``kit run`` executes a script, the
client checks three things: the kit is installed in the instance (``kit
status``), the cached manifest's ``content_digest`` is the installed release's,
and the bytes of the recipe or script file on disk have the sha256 that
manifest records. A missing cache entry is fetched again from the installed
kit's recorded source (or ``--from PATH``) and verified the same way.

These checks catch a stale or damaged copy (another release under the same
kit id, a partial write, a stray edit). They are not a boundary against a
process running as the same user, which can rewrite any file here; the
release's recipes and scripts are inert metadata outside ``content_digest``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.canonical import Sha256Value, pretty_canonical_bytes
from cruxible_client.contracts.kits import (
    KIT_MANIFEST_FILE,
    InstalledKit,
    KitBundle,
    KitManifest,
    KitRecipeSpec,
    KitScriptSpec,
    kit_file_path_allowed,
)
from cruxible_client.contracts.workspace_layout import daemon_state_root
from cruxible_client.errors import ConfigError

KIT_CACHE_ENV = "CRUXIBLE_KIT_CACHE_DIR"
_CACHE_DIRECTORY = "kit-files"


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

    def __init__(self, kit_id: str, installed: tuple[str, ...]) -> None:
        held = ", ".join(installed) if installed else "none"
        super().__init__(
            f"{self.error_code}: kit {kit_id} is not installed in this instance (installed: "
            f"{held}); next: cruxible kit add {kit_id} --yes"
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


def kit_cache_directory(kit_id: str, content_digest: str, root: Path | None = None) -> Path:
    """``<root>/<kit_id>/sha256-<hex>``: one release's verified files."""

    hex_digest = Sha256Value.from_tagged(content_digest).value
    return (root or kit_cache_root()) / kit_id / f"sha256-{hex_digest}"


def _sha256(content: bytes) -> str:
    return Sha256Value(hashlib.sha256(content).hexdigest()).tagged


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
        if _sha256(contents[path]) != digest:
            raise KitCacheError(
                f"kit file {path} differs from its manifest sha256",
                "fetch the release again; the kit bundle is damaged",
            )
    return {path: contents[path] for path in sorted(expected)}


def write_kit_cache(bundle: KitBundle, root: Path | None = None) -> Path:
    """Keep a verified copy of ``bundle``'s manifest and inert files; returns its directory.

    An existing copy with the same manifest bytes is kept as it is; one whose
    manifest differs (the same release identity rebuilt with other inert files)
    is replaced whole, never mixed. Written beside the target and renamed into
    place, so a reader never sees a partial copy. O(the kit's inert files).
    """

    manifest = bundle.manifest
    files = _verified_files(bundle)
    target = kit_cache_directory(manifest.kit_id, manifest.content_digest, root)
    manifest_bytes = pretty_canonical_bytes(manifest.model_dump(mode="json"))
    existing = target / KIT_MANIFEST_FILE
    if existing.is_file() and existing.read_bytes() == manifest_bytes:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target.parent))
    try:
        for path, content in files.items():
            if not kit_file_path_allowed(path):
                raise KitCacheError(f"kit file path {path!r} is not allowed", "fix the kit")
            destination = staging / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            destination.chmod(0o444)
        (staging / KIT_MANIFEST_FILE).write_bytes(manifest_bytes)
        (staging / KIT_MANIFEST_FILE).chmod(0o444)
        retired = None
        if target.exists():
            retired = target.with_name(f".retired-{target.name}-{os.getpid()}")
            os.replace(target, retired)
        os.replace(staging, target)
        if retired is not None:
            shutil.rmtree(retired, ignore_errors=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return target


def installed_kit(status_kits: tuple[InstalledKit, ...], kit_id: str) -> InstalledKit:
    """The installed kit ``kit_id`` from a ``kit status`` answer, or a typed refusal."""

    for kit in status_kits:
        if kit.kit_id == kit_id:
            return kit
    raise KitNotInstalled(kit_id, tuple(sorted(kit.kit_id for kit in status_kits)))


@dataclass(frozen=True)
class CachedKit:
    """A cached release whose manifest is the installed one's."""

    directory: Path
    manifest: KitManifest

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

    def verified_bytes(self, path: str) -> tuple[Path, bytes]:
        """The file's path on disk and its bytes, refused unless they have the
        manifest's sha256."""

        expected = self.manifest.kit_files().get(path)
        on_disk = self.directory / path
        if expected is None:
            raise KitCacheError(
                f"the cached manifest lists no file {path}",
                f"remove {self.directory} and rerun with --from KIT",
            )
        try:
            content = on_disk.read_bytes()
        except OSError as exc:
            raise KitCacheError(
                f"cannot read the cached kit file {on_disk} ({exc.strerror or exc})",
                f"remove {self.directory} and rerun with --from KIT",
            ) from exc
        if _sha256(content) != expected:
            raise KitCacheError(
                f"the cached kit file {on_disk} has sha256 {_sha256(content)}, not the "
                f"manifest's {expected} (sha256 mismatch: altered or damaged)",
                f"remove {self.directory} and rerun with --from KIT, or kit pull it again",
            )
        return on_disk, content


def _read_cached_manifest(directory: Path) -> KitManifest | None:
    path = directory / KIT_MANIFEST_FILE
    if not path.is_file():
        return None
    try:
        return KitManifest.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise KitCacheError(
            f"the cached manifest {path} is not a kit manifest ({exc})",
            f"remove {directory} and rerun with --from KIT",
        ) from exc


def load_installed_kit(
    kit: InstalledKit,
    *,
    from_path: str | None = None,
    root: Path | None = None,
    offline: bool = False,
) -> CachedKit:
    """The cached copy of the installed release ``kit``, fetched and cached when absent.

    ``from_path`` (``--from``) is read first when given; otherwise the installed
    kit's recorded source is tried (never when ``offline``). Either way the
    fetched bundle must carry the installed ``content_digest``, and its files
    are verified before caching.
    """

    from cruxible_client.kits import resolve_kit
    from cruxible_core.kits.base_kit import bundled_kit_reference

    directory = kit_cache_directory(kit.kit_id, kit.content_digest, root)
    manifest = None if from_path is not None else _read_cached_manifest(directory)
    if manifest is None:
        reference = from_path or kit.source
        if offline:
            raise KitCacheError(
                f"no cached copy of kit {kit.kit_id} {kit.version}",
                "rerun with --from KIT",
            )
        if reference is None:
            raise KitCacheError(
                f"no cached copy of kit {kit.kit_id} {kit.version} and no recorded source",
                f"rerun with --from KIT (the kit directory, layout or reference of {kit.version})",
            )
        try:
            bundle, _origin = bundled_kit_reference(reference) or resolve_kit(reference)
        except ConfigError as exc:
            raise KitCacheError(
                f"no cached copy of kit {kit.kit_id} {kit.version}, and fetching "
                f"{reference} failed ({exc})",
                f"rerun with --from KIT (the kit directory, layout or reference of {kit.version})",
            ) from exc
        if (bundle.manifest.kit_id, bundle.manifest.content_digest) != (
            kit.kit_id,
            kit.content_digest,
        ):
            raise KitCacheError(
                f"{reference} holds kit {bundle.manifest.kit_id} {bundle.manifest.version} "
                f"content {bundle.manifest.content_digest}, not the installed {kit.kit_id} "
                f"{kit.version} content {kit.content_digest}",
                f"rerun with --from naming the installed release {kit.kit_id} {kit.version}",
            )
        directory = write_kit_cache(bundle, root)
        manifest = _read_cached_manifest(directory)
        assert manifest is not None
    if (manifest.kit_id, manifest.content_digest) != (kit.kit_id, kit.content_digest):
        raise KitCacheError(
            f"the cached manifest in {directory} is kit {manifest.kit_id} content "
            f"{manifest.content_digest}, not the installed content {kit.content_digest}",
            f"remove {directory} and rerun with --from KIT",
        )
    return CachedKit(directory=directory, manifest=manifest)


__all__ = [
    "KIT_CACHE_ENV",
    "CachedKit",
    "KitCacheError",
    "KitEntryUnknown",
    "KitNotInstalled",
    "installed_kit",
    "kit_cache_directory",
    "kit_cache_root",
    "load_installed_kit",
    "write_kit_cache",
]
