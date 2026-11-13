"""Read a provider package's registration metadata.

A provider package ships a ``registration.json`` (schema 3,
:class:`~cruxible_client.contracts.provider_registration.PackageRegistration`)
naming its runtime manifest (schema 2, one row per entrypoint, by
package-relative path and digest), declaring its typed operations inline
(``{name, description, source, serves, in, out, effect, may_truncate?,
refusals, entrypoint}``, Contracts named ``name@N``) and carrying every shape
they reach in ``declares``. Runtime requirements are keyed by the entrypoint
they belong to, and the wheel's ``cruxible.providers`` entry points are exactly
``{operation: entrypoint}``.

Every resource reference, digest, entry point and extra is checked here, and
no package code is imported or executed. Only schema 3 is read: rebuild an
older package with the provider runtime's ``write_registration``.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PathDistribution
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import yaml
from packaging.utils import canonicalize_name

from cruxible_client.contracts.provider_registration import (
    REGISTRATION_SCHEMA_VERSION,
    PackageRegistration,
    RegistrationResource,
)
from cruxible_client.contracts.providers import ProviderRuntimeManifest
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

REGISTRATION_FILE = "registration.json"
#: The runtime reader's unpacked-size bound on wheel inspection.
MAX_UNPACKED_WHEEL_BYTES = 128 * 1024 * 1024
_SHA256_PREFIX = "sha256:"


def _read_resource(resource: RegistrationResource, root: Path) -> bytes:
    """One package file the registration names, refusing an escape or a digest mismatch."""

    root = root.resolve(strict=True)
    path = root / resource.path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"resource is missing: {resource.path}") from exc
    if path.is_symlink() or not resolved.is_relative_to(root):
        raise ValueError(f"resource escapes package: {resource.path}")
    data = path.read_bytes()
    if _SHA256_PREFIX + hashlib.sha256(data).hexdigest() != resource.digest:
        raise ValueError(f"resource digest mismatch: {resource.path}")
    return data


def _yaml_document(content: bytes, *, label: str) -> object:
    try:
        return yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{label} is not readable YAML or JSON") from exc


def read_package_registration(root: Path) -> PackageRegistrationDocumentV1:
    """Read the registration a package directory ships (``registration.json``, schema 3)."""

    raw = (root / REGISTRATION_FILE).read_bytes()
    try:
        version = json.loads(raw).get("schema_version")
    except (UnicodeDecodeError, ValueError, AttributeError) as exc:
        raise ValueError("registration.json is not a JSON object") from exc
    if version != REGISTRATION_SCHEMA_VERSION:
        raise ValueError(
            f"registration.json schema {version!r} is not read; schema 3 declares the "
            "capabilities each operation serves and carries the shapes it reaches (rebuild "
            "the package with the provider runtime's write_registration)"
        )
    registration = PackageRegistration.model_validate_json(raw)
    manifest_document = _yaml_document(
        _read_resource(registration.manifest, root), label="provider manifest"
    )
    if not isinstance(manifest_document, dict):
        raise ValueError("provider manifest must be a mapping")
    if manifest_document.get("schema_version") != 2:
        raise ValueError(
            f"provider manifest schema {manifest_document.get('schema_version')!r} is not "
            "read; a schema-2 manifest keys implementations by entrypoint"
        )
    return PackageRegistrationDocumentV1(
        schema_version=3,
        manifest=ProviderRuntimeManifest.model_validate(manifest_document),
        operations=registration.operations,
        declares=registration.declares,
        runtime_requirements=registration.runtime_requirements,
    )


def _distribution_registration(distribution: PathDistribution) -> PackageRegistrationDocumentV1:
    """Locate the descriptor through the wheel's own metadata; import nothing."""

    files = distribution.files or ()
    matches = [path for path in files if path.name == REGISTRATION_FILE]
    if len(matches) != 1:
        raise ValueError("provider distribution must contain exactly one registration.json")
    document = read_package_registration(Path(str(distribution.locate_file(matches[0]))).parent)
    manifest = document.manifest
    if canonicalize_name(distribution.metadata["Name"]) != canonicalize_name(
        manifest.distribution.name
    ):
        raise ValueError("distribution name differs from manifest")
    if distribution.version != manifest.distribution.version:
        raise ValueError("distribution version differs from manifest")
    # One entry point per operation, named by it and valued by its entrypoint.
    expected = {item.name: item.entrypoint for item in document.operations}
    entries = [ep for ep in distribution.entry_points if ep.group == "cruxible.providers"]
    actual = {ep.name: ep.value for ep in entries}
    if len(actual) != len(entries) or actual != expected:
        raise ValueError("distribution entry points differ from the registered operations")
    extras = set(distribution.metadata.get_all("Provides-Extra") or ())
    for implementation in manifest.implementations:
        if not set(implementation.requires_extras) <= extras:
            raise ValueError("manifest requires an extra absent from distribution")
    return document


@contextmanager
def _unpacked(path: Path) -> Iterator[Path]:
    """The wheel's members in disposable storage, refusing unsafe archives."""

    with TemporaryDirectory(prefix="cruxible-wheel-") as temporary, ZipFile(path) as archive:
        root = Path(temporary)
        seen: set[str] = set()
        if sum(item.file_size for item in archive.infolist()) > MAX_UNPACKED_WHEEL_BYTES:
            raise ValueError("provider wheel metadata inspection exceeds its unpacked size limit")
        for item in archive.infolist():
            member = PurePosixPath(item.filename)
            if (
                member.is_absolute()
                or ".." in member.parts
                or "\\" in item.filename
                or str(member) in seen
                or stat.S_ISLNK(item.external_attr >> 16)
            ):
                raise ValueError("wheel contains an unsafe or duplicate member")
            seen.add(str(member))
            destination = root.joinpath(*member.parts)
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(item))
        yield root


def read_wheel_registration(path: Path) -> PackageRegistrationDocumentV1:
    """Read one wheel's registration; execute no entry point and judge no bucket."""

    with _unpacked(path) as root:
        metadata = tuple(root.glob("*.dist-info"))
        if len(metadata) != 1:
            raise ValueError("wheel must carry exactly one top-level distribution")
        return _distribution_registration(PathDistribution(metadata[0]))


__all__ = [
    "MAX_UNPACKED_WHEEL_BYTES",
    "read_package_registration",
    "read_wheel_registration",
]
