"""Read a provider package a kit bundles: its identity and what installing it registers.

Metadata only, through the first-party toolchain's wheel and lock readers: no
provider code runs. ``kit build`` uses it to record the package in the kit
manifest and to check that every ProviderInterface the kit carries is exactly
what installing the bundled wheel registers.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cruxible_client.contracts.kits import KitProvider, KitProviderFile
from cruxible_client.contracts.provider_interfaces import (
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.errors import ConfigError, RequestRefusedError
from cruxible_core.providers.package_index import embedded_lock
from cruxible_core.providers.package_materialization import (
    package_preparation_errors,
    resolve_provider_package,
    toolchain,
)
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

_REPAIR = RepairOperation(operation="cruxible.kit.build")


@dataclass(frozen=True)
class InspectedProviderPackage:
    """One bundled package: its manifest entry and the interface bytes it registers."""

    provider: KitProvider
    #: ProviderInterface path -> the exact artifact bytes installing the wheel writes.
    interfaces: dict[str, bytes]
    #: This host's environment pin key and the materialization digest installing
    #: these exact bytes records there: the whole dependency closure, path
    #: dependency wheels included, which the lock pins only by name and version.
    pin_key: str
    materialization: str


def _refuse(code: str, message: str) -> RequestRefusedError:
    return RequestRefusedError(code, message, repair=_REPAIR)


def _write(root: Path, filename: str, content: bytes) -> Path:
    path = root / filename
    path.write_bytes(content)
    return path


def inspect_provider_package(
    *,
    wheel: tuple[str, bytes],
    lock: bytes,
    dependencies: tuple[tuple[str, bytes], ...] = (),
    embedded_lock_required: bool = False,
) -> InspectedProviderPackage:
    """The manifest entry for one wheel, its lock and its path-sourced dependency wheels.

    The lock's root must be this wheel's distribution at its version. Every other
    package the lock names by path (a first-party sibling such as the provider
    runtime, until it resolves from an index) must come as a wheel; registry
    packages resolve by name at install time and are never bundled. A package a
    consumer installs by name (``embedded_lock_required``) materializes from the
    lock its wheel embeds, so the lock given must be exactly that one.
    """

    with tempfile.TemporaryDirectory(prefix="cruxible-kit-provider-") as temporary:
        root = Path(temporary)
        wheel_path = _write(root, wheel[0], wheel[1])
        lock_path = _write(root, "uv.lock", lock)
        dependency_root = root / "dependencies"
        dependency_root.mkdir()
        dependency_paths = tuple(
            _write(dependency_root, name, content) for name, content in dependencies
        )
        with package_preparation_errors():
            wheels = toolchain("wheels")
            pin = wheels.wheel_pin(wheel_path)
            with wheels.wheel_registration(wheel_path) as bundle:
                document = PackageRegistrationDocumentV1.model_validate(bundle.export_document())
            locked = toolchain("resolution").load_uv_lock(lock_path)
            dependency_pins = tuple(wheels.wheel_pin(path) for path in dependency_paths)
        if embedded_lock_required:
            try:
                embedded = embedded_lock(wheel_path)
            except ConfigError as exc:
                raise _refuse("cruxible.kit.provider_lock_not_embedded", str(exc)) from exc
            if embedded != lock:
                raise _refuse(
                    "cruxible.kit.provider_lock_not_embedded",
                    f"the lock given for {pin.filename} is not the one the wheel embeds; an "
                    "index default installs by name from that embedded lock, so name the "
                    "package directory whose uv.lock the published wheel was built with",
                )
    if document.governed_definitions:
        raise _refuse(
            "cruxible.kit.provider_carries_definitions",
            f"{pin.filename} carries governed definitions; a provider package registers "
            "only its Provider and interfaces, and the kit carries the definitions",
        )
    rows: tuple[dict[str, Any], ...] = locked.packages
    roots = [row for row in rows if row["name"] == pin.name]
    if len(roots) != 1 or str(roots[0]["version"]) != pin.version:
        raise _refuse(
            "cruxible.kit.provider_lock_mismatch",
            f"the lock bundled with {pin.filename} does not lock {pin.name} {pin.version}; "
            "bundle the lock the wheel was built with",
        )
    by_path = {
        row["name"]: str(row["version"])
        for row in rows
        if row["name"] != pin.name and "registry" not in row.get("source", {})
    }
    supplied = {item.name: item for item in dependency_pins}
    missing = sorted(set(by_path) - set(supplied))
    if missing:
        raise _refuse(
            "cruxible.kit.provider_dependency_missing",
            f"the lock of {pin.name} names {', '.join(missing)} by path; bundle each one's "
            "built wheel beside the provider's (in its dist/ directory)",
        )
    extra = sorted(name for name in supplied if name not in by_path)
    mismatched = sorted(
        name for name, item in supplied.items() if name in by_path and item.version != by_path[name]
    )
    if extra or mismatched:
        raise _refuse(
            "cruxible.kit.provider_dependency_mismatch",
            f"the dependency wheels bundled with {pin.name} are not the path packages its "
            f"lock names (unexpected: {extra or 'none'}; other version: {mismatched or 'none'})",
        )
    registrations = document.interface_registrations()
    with tempfile.TemporaryDirectory(prefix="cruxible-kit-provider-") as temporary:
        root = Path(temporary)
        dependency_root = root / "dependencies"
        dependency_root.mkdir()
        with package_preparation_errors():
            resolved = resolve_provider_package(
                wheel=_write(root, wheel[0], wheel[1]),
                lock_path=_write(root, "uv.lock", lock),
                dependency_wheels=tuple(
                    _write(dependency_root, name, content) for name, content in dependencies
                ),
            )
    provider = KitProvider(
        provider_id=document.manifest.provider_id,
        package=pin.name,
        version=pin.version,
        wheel=KitProviderFile(filename=pin.filename, sha256=pin.artifact_id),
        lock=KitProviderFile(
            filename=f"{pin.name}-{pin.version}.uv.lock", sha256=locked.lock_sha256
        ),
        dependencies=tuple(
            KitProviderFile(filename=item.filename, sha256=item.artifact_id)
            for item in sorted(dependency_pins, key=lambda item: item.filename)
        ),
        interfaces=tuple(sorted(item.interface_id for item in registrations)),
    )
    return InspectedProviderPackage(
        provider=provider,
        interfaces={
            provider_interface_path(item.interface_id): render_provider_interface(item)
            for item in registrations
        },
        pin_key=resolved.pin_key,
        materialization=resolved.materialization,
    )


__all__ = ["InspectedProviderPackage", "inspect_provider_package"]
