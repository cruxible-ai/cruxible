"""Transfer locally built provider wheels to any selected daemon through its CAS.

An operator job, so it lives with the CLI rather than the SDK: `provider install
WHEEL --lock FILE` and `kit build --provider DIR` read the paths here and the
daemon receives only CAS references.
"""

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename

from cruxible_client.contracts.kits import KitBuildProvider
from cruxible_client.contracts.provider_installation import (
    ProviderInstallRequest,
    ProviderInstallResult,
    ProviderWheelObject,
)
from cruxible_core.errors import ConfigError

if TYPE_CHECKING:
    from cruxible_client.transport.http import CruxibleClient


def install_provider_wheel(
    client: "CruxibleClient",
    instance_id: str,
    *,
    wheel: Path,
    lock: Path,
    dependency_wheels: tuple[Path, ...] = (),
    extras: tuple[str, ...] = (),
    control_domain: str = "operator",
    reverify: bool = False,
    at: str | None = None,
) -> ProviderInstallResult:
    """Paths are consumed here on the client; the daemon receives only CAS references."""

    def transfer(path: Path) -> ProviderWheelObject:
        # Validate the filename before uploading any bytes.
        ProviderWheelObject(filename=path.name, digest="sha256:" + "0" * 64)
        stored = client.store_body(instance_id, path.read_bytes())
        return ProviderWheelObject(filename=path.name, digest=stored.digest)

    root = transfer(wheel)
    dependencies = tuple(transfer(path) for path in dependency_wheels)
    retained_lock = client.store_body(instance_id, lock.read_bytes())
    return client.install_provider(
        instance_id,
        ProviderInstallRequest(
            wheel=root,
            lock_digest=retained_lock.digest,
            dependencies=dependencies,
            extras=tuple(sorted(set(extras))),
            control_domain=control_domain,
            reverify=reverify,
            at=at,
        ),
    )


def _transfer(client: "CruxibleClient", instance_id: str, path: Path) -> ProviderWheelObject:
    ProviderWheelObject(filename=path.name, digest="sha256:" + "0" * 64)
    stored = client.store_body(instance_id, path.read_bytes())
    return ProviderWheelObject(filename=path.name, digest=stored.digest)


def stage_kit_provider_directory(
    client: "CruxibleClient", instance_id: str, directory: Path
) -> KitBuildProvider:
    """One provider package of a kit's source tree, staged for ``kit build``.

    The directory is the package's project: its ``pyproject.toml`` names it, its
    ``uv.lock`` is the lock it was built with, and ``dist/`` holds its built wheel
    plus the wheel of each dependency the lock names by path (``uv build --wheel
    --out-dir dist`` for each). The kit bundles those bytes, never the source.
    """

    try:
        name = tomllib.loads((directory / "pyproject.toml").read_text())["project"]["name"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{directory} is not a provider package (no project name)") from exc
    lock = directory / "uv.lock"
    if not lock.is_file():
        raise ConfigError(f"{directory} has no uv.lock; run uv lock there first")
    wheels: dict[str, list[Path]] = {}
    for path in sorted((directory / "dist").glob("*.whl")):
        try:
            distribution = canonicalize_name(parse_wheel_filename(path.name)[0])
        except InvalidWheelFilename as exc:
            raise ConfigError(f"{path} is not a wheel file name") from exc
        wheels.setdefault(distribution, []).append(path)
    if any(len(paths) > 1 for paths in wheels.values()):
        raise ConfigError(f"{directory / 'dist'} holds several builds of one package; keep one")
    root = wheels.pop(canonicalize_name(name), [])
    if not root:
        raise ConfigError(f"{directory / 'dist'} holds no wheel of {name}; run uv build --wheel")
    return KitBuildProvider(
        wheel=_transfer(client, instance_id, root[0]),
        lock_digest=client.store_body(instance_id, lock.read_bytes()).digest,
        dependencies=tuple(
            _transfer(client, instance_id, paths[0]) for _name, paths in sorted(wheels.items())
        ),
    )
