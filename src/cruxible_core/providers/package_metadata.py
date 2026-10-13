"""Read a provider package's registration metadata, with its bucket data inert.

Provider packages built on cruxible-provider-runtime 0.2.x ship a
``registration.json`` that exports, per interface, the operation definition and
also a bucket vocabulary, a classifier and conformance fixtures, and a manifest
that declares input-bucket selectors. The runtime's own reader
(``wheels.wheel_registration`` -> ``registration.load_registration``) validates
that bucket data: it parses every measured bucket id and selector against the
vocabulary and refuses a package whose selectors are not each covered by a
matching fixture.

Core classifies no input, so it reads the same metadata here instead: every
resource reference, digest, interface definition, effect declaration, entry
point and extra is checked exactly as the runtime checks it, and the bucket
data is only read -- its bytes verified against their digests, parsed as data,
and carried into the registration as inert fields. Nothing here parses a
selector, measures or matches a bucket, or requires a fixture for a selector.
No package code is imported or executed.
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
from typing import Any, Literal
from zipfile import ZipFile

import yaml
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.provider_interfaces import (
    provider_external_interface_definition_digest,
    provider_vocabulary_document,
)
from cruxible_client.contracts.providers import ProviderRuntimeManifest
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

REGISTRATION_FILE = "registration.json"
INTERFACE_DOMAIN = "cruxible.interface.stub.v1"
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


class _ClassifierExport(_Strict):
    """The package's classifier: carried by identity and source digest, never run."""

    identity: str
    version: int = Field(ge=1)
    entrypoint: str
    source: _ResourceRef

    _entrypoint = field_validator("entrypoint")(_callable_path)


class _InterfaceHistory(_Strict):
    definition: _ResourceRef
    interface_digest: str


class _InterfaceExport(_Strict):
    interface_id: str
    interface_digest: str
    definition: _ResourceRef
    vocabulary: _ResourceRef
    classifier: _ClassifierExport
    fixtures: _ResourceRef
    predecessors: tuple[_InterfaceHistory, ...] = ()


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
    schema_version: Literal[1] = 1
    manifest: _ResourceRef
    interfaces: tuple[_InterfaceExport, ...] = Field(min_length=1)
    runtime_requirements: tuple[_RuntimeRequirement, ...] = ()


class _Fixture(_Strict):
    """One conformance fixture row, as data: its bucket id is never parsed."""

    fixture_id: str
    canonical_input: dict[str, Any]
    measured_bucket_id: str


def _yaml_document(content: bytes, *, label: str) -> object:
    try:
        return yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{label} is not readable YAML or JSON") from exc


def _definition(root: Path, reference: _ResourceRef, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(reference.read(root))
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _interface_digest(definition: dict[str, Any]) -> str:
    return provider_external_interface_definition_digest(
        canonical_bytes(definition).hex(), domain=INTERFACE_DOMAIN
    )


def _export(
    root: Path, item: _InterfaceExport, manifest: ProviderRuntimeManifest
) -> dict[str, Any]:
    """One interface export: its contract checked, its bucket data carried inert."""

    identity = item.interface_id
    definition = _definition(root, item.definition, label="interface definition")
    if definition.get("interface_id") != identity:
        raise ValueError("interface identity differs from export")
    if _interface_digest(definition) != item.interface_digest:
        raise ValueError("interface digest differs from definition")
    implementations = [row for row in manifest.implementations if row.interface_id == identity]
    if len(implementations) != 1:
        raise ValueError("a package implements each exported interface exactly once")
    implementation = implementations[0]
    if implementation.interface_digest != item.interface_digest:
        raise ValueError("manifest pins a different interface digest")
    if definition.get("effect_class") not in {"pure", "external_read", "external_mutation"}:
        raise ValueError("interface must declare its effect class")
    if implementation.side_effects != (definition["effect_class"] == "external_mutation"):
        raise ValueError("manifest and interface effects disagree")
    contracts = definition.get("contracts")
    if (
        not isinstance(contracts, dict)
        or not {"input", "output"}.issubset(contracts)
        or set(contracts) - {"input", "output", "material"}
    ):
        raise ValueError("interface must export shared input/output contracts")
    if "material" in contracts and (
        contracts["output"] != "playbill-provider-result-to-external-capture-v1"
        or not isinstance(contracts["material"], dict)
    ):
        raise ValueError("captured material schema requires an acquisition interface")
    revision = definition.get("version")
    if type(revision) is not int or revision < 1:
        raise ValueError("interface version must be a positive integer")
    seen: set[str] = set()
    for previous in item.predecessors:
        old = _definition(root, previous.definition, label="interface predecessor")
        if (
            old.get("interface_id") != identity
            or type(old.get("version")) is not int
            or not 0 < old["version"] < revision
            or _interface_digest(old) != previous.interface_digest
            or previous.interface_digest in seen
        ):
            raise ValueError("invalid interface predecessor")
        seen.add(previous.interface_digest)
    vocabulary = provider_vocabulary_document(
        _yaml_document(item.vocabulary.read(root), label="bucket vocabulary")
    )
    # The classifier's source is pinned by digest and carried, never imported.
    item.classifier.source.read(root)
    rows = json.loads(item.fixtures.read(root))
    if not isinstance(rows, list):
        raise ValueError("conformance fixtures must be a JSON list")
    fixtures = tuple(_Fixture.model_validate(row) for row in rows)
    if len({row.fixture_id for row in fixtures}) != len(fixtures):
        raise ValueError("duplicate conformance fixture id")
    return {
        "interface_id": identity,
        "interface_digest": item.interface_digest,
        "definition": definition,
        "vocabulary": vocabulary,
        "classifier_identity": item.classifier.identity,
        "classifier_version": item.classifier.version,
        "classifier_code": {
            "entrypoint": item.classifier.entrypoint,
            "source_digest": item.classifier.source.digest,
        },
        "fixtures": [row.model_dump(mode="json") for row in fixtures],
    }


def read_package_registration(root: Path) -> PackageRegistrationDocumentV1:
    """Read the registration a package directory ships (``registration.json``)."""

    descriptor = _Descriptor.model_validate_json((root / REGISTRATION_FILE).read_bytes())
    manifest_document = _yaml_document(descriptor.manifest.read(root), label="provider manifest")
    if not isinstance(manifest_document, dict):
        raise ValueError("provider manifest must be a mapping")
    manifest = ProviderRuntimeManifest.model_validate(manifest_document)
    exports = {item.interface_id: item for item in descriptor.interfaces}
    if len(exports) != len(descriptor.interfaces):
        raise ValueError("duplicate interface exports")
    if set(exports) != {item.interface_id for item in manifest.implementations}:
        raise ValueError("registration must export every and only implemented interface")
    interfaces = [_export(root, exports[identity], manifest) for identity in sorted(exports)]
    for requirement in descriptor.runtime_requirements:
        implementation = next(
            (
                row
                for row in manifest.implementations
                if row.interface_id == requirement.interface_id
            ),
            None,
        )
        if implementation is None:
            raise ValueError("runtime requirement names an undeclared interface")
        if (
            requirement.kind == "python_extra"
            and requirement.name not in implementation.requires_extras
        ):
            raise ValueError("runtime requirement names an undeclared extra")
    return PackageRegistrationDocumentV1.model_validate(
        {
            "schema_version": 1,
            "manifest": manifest.model_dump(mode="json"),
            "interfaces": interfaces,
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
