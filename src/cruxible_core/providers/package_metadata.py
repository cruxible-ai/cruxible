"""Read a provider package's registration metadata.

A provider package ships a ``registration.json`` (schema 2) naming its runtime
manifest (by package-relative path and digest) and declaring its typed
operations inline: ``{name, in, out, effect, refusals, enumerates?,
entrypoint}``, Contracts named ``name@N``. The manifest's own bucket claims
are inert: core classifies no input.

Every resource reference, digest, entry point and extra is checked here, and
no package code is imported or executed. A schema-1 registration (one
ProviderInterface per implementation) refuses: rebuild the package with its
operations declared.
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
from typing import Literal
from zipfile import ZipFile

import yaml
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.provider_operations import ProviderOperation
from cruxible_client.contracts.providers import ProviderRuntimeManifest
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

REGISTRATION_FILE = "registration.json"
#: The runtime reader's unpacked-size bound on wheel inspection.
MAX_UNPACKED_WHEEL_BYTES = 128 * 1024 * 1024
_SHA256_PREFIX = "sha256:"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _callable_path(value: str) -> str:
    module, separator, member = value.partition(":")
    if (
        not separator
        or not member.isidentifier()
        or not all(part.isidentifier() for part in module.split("."))
    ):
        raise ValueError("entrypoint must be module:callable")
    return value


class _ResourceRef(_Strict):
    path: str
    digest: str

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or path.as_posix() != value
            or value == "."
        ):
            raise ValueError("resource path must be a normalized package-relative path")
        return value

    @field_validator("digest")
    @classmethod
    def _sha256(cls, value: str) -> str:
        hexadecimal = value.removeprefix(_SHA256_PREFIX)
        if (
            not value.startswith(_SHA256_PREFIX)
            or len(hexadecimal) != 64
            or any(character not in "0123456789abcdef" for character in hexadecimal)
        ):
            raise ValueError("resource digest must be sha256:<64 lowercase hex>")
        return value

    def read(self, root: Path) -> bytes:
        root = root.resolve(strict=True)
        path = root / self.path
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"resource is missing: {self.path}") from exc
        if path.is_symlink() or not resolved.is_relative_to(root):
            raise ValueError(f"resource escapes package: {self.path}")
        data = path.read_bytes()
        if _SHA256_PREFIX + hashlib.sha256(data).hexdigest() != self.digest:
            raise ValueError(f"resource digest mismatch: {self.path}")
        return data


class _RuntimeRequirement(_Strict):
    interface_id: str
    kind: Literal["python_extra", "runtime_resource"]
    name: str
    description: str
    probe_entrypoint: str | None = None

    @model_validator(mode="after")
    def _probe(self) -> _RuntimeRequirement:
        if self.kind == "runtime_resource":
            if self.probe_entrypoint is None:
                raise ValueError("runtime resources require a package readiness probe")
            _callable_path(self.probe_entrypoint)
        elif self.probe_entrypoint is not None:
            raise ValueError("Python extras do not use resource probes")
        return self


class _Descriptor(_Strict):
    schema_version: Literal[2]
    manifest: _ResourceRef
    operations: tuple[ProviderOperation, ...] = Field(min_length=1)
    runtime_requirements: tuple[_RuntimeRequirement, ...] = ()


def _yaml_document(content: bytes, *, label: str) -> object:
    try:
        return yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{label} is not readable YAML or JSON") from exc


def read_package_registration(root: Path) -> PackageRegistrationDocumentV1:
    """Read the registration a package directory ships (``registration.json``)."""

    raw = (root / REGISTRATION_FILE).read_bytes()
    try:
        version = json.loads(raw).get("schema_version")
    except (UnicodeDecodeError, ValueError, AttributeError) as exc:
        raise ValueError("registration.json is not a JSON object") from exc
    if version != 2:
        raise ValueError(
            "registration.json schema 1 exports ProviderInterfaces, which core no longer "
            "reads; rebuild the package with schema 2 and its typed operations declared"
        )
    descriptor = _Descriptor.model_validate_json(raw)
    manifest_document = _yaml_document(descriptor.manifest.read(root), label="provider manifest")
    if not isinstance(manifest_document, dict):
        raise ValueError("provider manifest must be a mapping")
    manifest = ProviderRuntimeManifest.model_validate(manifest_document)
    return PackageRegistrationDocumentV1.model_validate(
        {
            "schema_version": 2,
            "manifest": manifest.model_dump(mode="json"),
            "operations": [
                item.model_dump(mode="json", by_alias=True) for item in descriptor.operations
            ],
            "runtime_requirements": [
                item.model_dump(mode="json") for item in descriptor.runtime_requirements
            ],
        }
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
    expected = {item.interface_id: item.entrypoint for item in manifest.implementations}
    entries = [ep for ep in distribution.entry_points if ep.group == "cruxible.providers"]
    actual = {ep.name: ep.value for ep in entries}
    if len(actual) != len(entries) or actual != expected:
        raise ValueError("distribution entry points differ from manifest")
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
