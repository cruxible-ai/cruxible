"""Kits: frozen sets of definition artifacts imported through one governed change set.

A kit carries definitions only -- what can be known and how -- never authority,
bindings, or state. A release is self-contained: every artifact is a snapshot
with no predecessor, pinning the release's own digests, so the release's content
digest names the same definitions wherever it is installed. The consumer, not
the release, owns history: installing diffs the release against the consumer's
accepted state and proposes that diff, giving each changed definition the
consumer's own current digest as its predecessor.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from .canonical import Sha256Value, canonical_digest
from .change_control import DryRun, PreviewAt
from .get_reads import GetCoordinate
from .projection import AcceptedCoordinate
from .provider_installation import (
    AutomationCarryV1,
    ProviderRepinV1,
    ProviderWheelObject,
    StoppedAutomationV1,
)
from .providers import provider_build_key_of

KIT_MANIFEST_FILE = "cruxible-kit.json"
KIT_ARTIFACT_DIRECTORY = "artifacts"
#: Where a kit directory (and a kit artifact's layer) holds its bundled provider
#: packages: built wheels and uv locks, never source.
KIT_PROVIDER_DIRECTORY = "providers"
KIT_RECEIPT_DOCUMENT_KIND = "kit_receipt"
#: Where a kit artifact's layer holds the kit's inert files (README, reference,
#: examples, skills). A kit directory holds them at their own paths.
KIT_FILE_DIRECTORY = "files"

# The definition families a kit may carry. Authority (governance, principals,
# mandates), local binding (providers, lines) and state (subjects, claims,
# attestations) are never kit content. A Procedure moves its pins through its
# typed graph (they feed its definition digest). A SubjectKind (how a kind's
# Subjects are named) names its key ClaimTypes by predicate, so it travels with
# them. A Contract (one immutable data shape) travels with the Procedures and
# provider operations whose slots and signatures pin it.
KIT_ARTIFACT_PREFIXES: tuple[str, ...] = (
    "capture-contracts/",
    "claim-types/",
    "contracts/",
    "procedures/",
    "query-definitions/",
    "subject-kinds/",
)

_KIT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_OWNS_RE = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*\.$")


class _Strict(BaseModel):
    # Kit models cross both directions (a bundle is built and sent back), so they
    # publish one schema rather than a request and a response spelling.
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


def kit_artifact_path_allowed(path: str) -> bool:
    """A canonical relative artifact path inside one kit family, and nothing else."""

    parts = path.split("/")
    return (
        path.endswith(".json")
        and path.startswith(KIT_ARTIFACT_PREFIXES)
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in parts)
        and all(part.isprintable() for part in parts)
    )


def kit_receipt_document_id(kit_id: str) -> str:
    return f"kit-{kit_id}"


def _kit_id(value: str) -> str:
    if not _KIT_ID_RE.fullmatch(value):
        raise ValueError("kit id must be lowercase letters, digits and hyphens")
    return value


def _version(value: str) -> str:
    if not _VERSION_RE.fullmatch(value):
        raise ValueError("kit version must be MAJOR.MINOR.PATCH")
    return value


def _digest(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


def _owns(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        raise ValueError("a kit owns at least one identity prefix")
    for prefix in value:
        if not _OWNS_RE.fullmatch(prefix):
            raise ValueError(f"owned prefix {prefix!r} must be dotted lowercase ending in '.'")
    return _sorted_unique(value, label="owned prefixes")


def _sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be sorted and unique")
    return values


class KitArtifact(_Strict):
    """One artifact a kit carries, named by its accepted path and exact digest."""

    path: str
    artifact_digest: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not kit_artifact_path_allowed(value):
            raise ValueError(f"a kit may not carry {value}")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str) -> str:
        return _digest(value)


def _plain_filename(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or not value.isprintable()
    ):
        raise ValueError(f"{value!r} is not a plain file name")
    return value


class KitProviderFile(_Strict):
    """One file of a bundled provider package, by name and exact sha256.

    The sha256 is also its body-store digest: ``kit add`` reads the file from the
    daemon's body store under it, where the CLI and MCP adapters stage it.
    """

    filename: str
    sha256: str

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        return _plain_filename(value)

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        return _digest(value)


#: How ``kit add`` obtains a provider package: ``bundled`` (the kit carries its
#: wheel, lock and path-dependency wheels) or ``index`` (a default provider: the
#: kit names its published package and exact version, installed by name from the
#: daemon's provider index when no Provider here serves its operations).
KitProviderDelivery = Literal["bundled", "index"]


class KitProvider(_Strict):
    """One provider package a kit names: its built wheel and the uv lock it was
    built with, plus the wheels of the first-party dependencies that lock names by
    path. Registry dependencies are never bundled: the daemon resolves them by
    name from its provider index (PyPI unless the operator configured one),
    pinned by the lock's hashes.

    A bundled package (the default delivery) is carried as those files; ``kit
    add`` installs it before the kit's definitions, through the ordinary
    transfer install, so the operations it declares are there for the kit's Lines
    to bind. An ``index`` package is the kit's default provider of its
    operations: the kit carries no file of it, ``kit add`` installs ``package``
    at exactly ``version`` by name (its lock is the one the wheel embeds, its path
    dependencies come from the same index) unless another live Provider here
    already serves every one of its operations' signatures, and ``wheel`` and
    ``lock`` name the published build it verifies.
    """

    provider_id: str
    package: str
    version: str
    wheel: KitProviderFile
    lock: KitProviderFile
    dependencies: tuple[KitProviderFile, ...] = ()
    #: The operations the package declares, by name.
    operations: tuple[str, ...] = ()
    delivery: KitProviderDelivery = Field(
        default="bundled", exclude_if=lambda value: value == "bundled"
    )

    @field_validator("wheel")
    @classmethod
    def _wheel(cls, value: KitProviderFile) -> KitProviderFile:
        if not value.filename.endswith(".whl"):
            raise ValueError("a bundled provider is a built wheel")
        return value

    @field_validator("dependencies")
    @classmethod
    def _dependencies(cls, value: tuple[KitProviderFile, ...]) -> tuple[KitProviderFile, ...]:
        if any(not item.filename.endswith(".whl") for item in value):
            raise ValueError("a bundled provider dependency is a built wheel")
        _sorted_unique(tuple(item.filename for item in value), label="provider dependencies")
        return value

    @field_validator("operations")
    @classmethod
    def _operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="provider operations")

    @model_validator(mode="after")
    def _index_entry(self) -> KitProvider:
        if self.delivery == "index" and (self.dependencies or not self.operations):
            raise ValueError(
                "a default provider names the operations it serves and no dependency "
                "files: its path dependencies come from the index that serves it"
            )
        return self

    @property
    def build(self) -> str:
        """The exact build this package registers: ``<provider_id>/<build key>``.

        The key covers the wheel and its lock, never how a host materializes
        them, so the kit names the same build on every host.
        """

        return (
            f"{self.provider_id}/"
            + provider_build_key_of(
                provider_id=self.provider_id,
                distribution_name=self.package,
                distribution_version=self.version,
                distribution_sha256=self.wheel.sha256,
                lock_sha256=self.lock.sha256,
            ).value
        )

    def files(self) -> tuple[KitProviderFile, ...]:
        """The files the kit carries for this package (none for an index default)."""

        if self.delivery == "index":
            return ()
        return (self.wheel, self.lock, *self.dependencies)


def _provider_files(providers: tuple[KitProvider, ...]) -> dict[str, str]:
    """Every bundled file name -> its sha256; one name never holds two contents.

    An index default carries no file, so it names none here.
    """

    files: dict[str, str] = {}
    for provider in providers:
        for item in provider.files():
            if files.setdefault(item.filename, item.sha256) != item.sha256:
                raise ValueError(f"bundled provider file {item.filename} names two contents")
    return files


class KitProviderFileBytes(_Strict):
    filename: str
    content_base64: str

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        return _plain_filename(value)

    @field_validator("content_base64")
    @classmethod
    def _content(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("bundled provider file content must be canonical base64") from exc
        return value

    @property
    def content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)

    @classmethod
    def of(cls, filename: str, content: bytes) -> KitProviderFileBytes:
        return cls(filename=filename, content_base64=base64.b64encode(content).decode("ascii"))


#: What an inert kit file is for: ``readme`` (a top-level README), ``reference``
#: (other documentation), ``example`` (under ``examples/``), ``skill`` (a file of
#: one agent skill, under ``skills/<name>/``).
KitFileRole = Literal["readme", "reference", "example", "skill"]

#: The names a kit directory gives its definition and provider content: an
#: inert file never sits under them.
_KIT_RESERVED_ROOTS = frozenset({KIT_MANIFEST_FILE, KIT_ARTIFACT_DIRECTORY, KIT_PROVIDER_DIRECTORY})
_KIT_FILE_PATH_MAX = 240


def kit_file_path_allowed(path: str) -> bool:
    """A canonical relative path for an inert kit file, outside the definition paths."""

    parts = path.split("/")
    return (
        0 < len(path) <= _KIT_FILE_PATH_MAX
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in parts)
        and all(part.isprintable() and not part.startswith(".") for part in parts)
        and parts[0].casefold() not in {name.casefold() for name in _KIT_RESERVED_ROOTS}
    )


def kit_file_role_of(path: str) -> KitFileRole:
    """The role a kit file's path implies; a README elsewhere is ``reference``."""

    parts = path.split("/")
    if parts[0] == "skills" and len(parts) >= 3:
        return "skill"
    if parts[0] == "examples" and len(parts) >= 2:
        return "example"
    if len(parts) == 1 and parts[0].casefold().startswith("readme"):
        return "readme"
    return "reference"


class KitFile(_Strict):
    """One inert file a kit release carries for its reader: never a definition.

    Kit files are not part of the release identity (``content_digest``), so a
    docs fix leaves every definition digest and KitReceipt as it was; ``kit add``
    never sends them to the daemon.
    """

    path: str
    sha256: str
    role: KitFileRole

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not kit_file_path_allowed(value):
            raise ValueError(f"{value!r} is not a kit file path")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        return _digest(value)

    @model_validator(mode="after")
    def _role_matches_path(self) -> KitFile:
        if self.role != kit_file_role_of(self.path):
            raise ValueError(
                f"kit file {self.path} is a {kit_file_role_of(self.path)}, not a {self.role}"
            )
        return self

    @property
    def skill(self) -> str | None:
        """The skill this file belongs to (``skills/<name>/...``), if any."""

        return self.path.split("/")[1] if self.role == "skill" else None


def _kit_files(value: tuple[KitFile, ...]) -> tuple[KitFile, ...]:
    paths = tuple(item.path for item in value)
    _sorted_unique(paths, label="kit file paths")
    # Case-insensitive file systems would fold two names onto one file, and a
    # file cannot also be a directory of another.
    folded = [path.casefold() for path in paths]
    if len(set(folded)) != len(folded):
        raise ValueError("kit file paths must differ ignoring case")
    directories = {
        "/".join(parts[:end])
        for parts in (p.split("/") for p in folded)
        for end in range(1, len(parts))
    }
    clash = sorted(set(folded) & directories)
    if clash:
        raise ValueError(f"kit file {clash[0]} is also a directory of another kit file")
    return value


class KitProvenance(_Strict):
    """Where a release was built: claimed by the builder, not proven.

    Shown by kit status and the install preview; it is not part of the release
    identity, so the same definitions built twice are the same release.
    """

    instance_id: str
    coordinate: AcceptedCoordinate
    principal_id: str | None = None


def kit_version_key(version: str) -> tuple[int, int, int]:
    """MAJOR.MINOR.PATCH as a sortable tuple."""

    major, minor, patch = _version(version).split(".")
    return int(major), int(minor), int(patch)


class KitManifest(_Strict):
    tag: Literal["playbill-kit-manifest-v1"] = "playbill-kit-manifest-v1"
    kit_id: str
    version: str
    # Identity prefixes this kit defines, each ending in a dot (``dev.``).
    owns: tuple[str, ...]
    artifacts: tuple[KitArtifact, ...]
    #: Provider packages the kit names, sorted by provider id: bundled ones (wheel
    #: and lock carried) and index defaults (installed by name and version).
    providers: tuple[KitProvider, ...] = Field(default=(), exclude_if=lambda value: not value)
    provenance: KitProvenance | None = None
    #: Inert files for the kit's reader, sorted by path: not part of
    #: ``content_digest``, never sent to ``kit add``.
    files: tuple[KitFile, ...] = Field(default=(), exclude_if=lambda value: not value)

    @field_validator("kit_id")
    @classmethod
    def _kit_id(cls, value: str) -> str:
        return _kit_id(value)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return _version(value)

    @field_validator("owns")
    @classmethod
    def _owns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _owns(value)

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[KitArtifact, ...]) -> tuple[KitArtifact, ...]:
        _sorted_unique(tuple(item.path for item in value), label="kit artifact paths")
        return value

    @field_validator("providers")
    @classmethod
    def _providers(cls, value: tuple[KitProvider, ...]) -> tuple[KitProvider, ...]:
        _sorted_unique(tuple(item.provider_id for item in value), label="kit providers")
        _provider_files(value)
        return value

    @field_validator("files")
    @classmethod
    def _files(cls, value: tuple[KitFile, ...]) -> tuple[KitFile, ...]:
        return _kit_files(value)

    def provider_files(self) -> dict[str, str]:
        """Every bundled provider file name -> its sha256."""

        return _provider_files(self.providers)

    @property
    def content_digest(self) -> str:
        """The release identity: every manifest field, including each artifact digest,
        apart from where it was built and its inert files."""

        return Sha256Value(
            canonical_digest(
                "playbill-kit-content-v1",
                self.model_dump(mode="json", exclude={"tag", "provenance", "files"}),
            )
        ).tagged

    def kit_files(self) -> dict[str, str]:
        """Every inert kit file path -> its sha256."""

        return {item.path: item.sha256 for item in self.files}

    def digests(self) -> dict[str, str]:
        return {item.path: item.artifact_digest for item in self.artifacts}


class KitArtifactBytes(_Strict):
    path: str
    content_base64: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not kit_artifact_path_allowed(value):
            raise ValueError(f"a kit may not carry {value}")
        return value

    @field_validator("content_base64")
    @classmethod
    def _content(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("kit artifact content must be canonical base64") from exc
        return value

    @property
    def content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)

    @classmethod
    def of(cls, path: str, content: bytes) -> KitArtifactBytes:
        return cls(path=path, content_base64=base64.b64encode(content).decode("ascii"))


class KitFileBytes(_Strict):
    path: str
    content_base64: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not kit_file_path_allowed(value):
            raise ValueError(f"{value!r} is not a kit file path")
        return value

    @field_validator("content_base64")
    @classmethod
    def _content(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("kit file content must be canonical base64") from exc
        return value

    @property
    def content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)

    @classmethod
    def of(cls, path: str, content: bytes) -> KitFileBytes:
        return cls(path=path, content_base64=base64.b64encode(content).decode("ascii"))


def kit_file_entry(path: str, content: bytes) -> KitFile:
    """The manifest entry for one inert file: its sha256 and the role its path implies."""

    return KitFile(
        path=path,
        sha256=Sha256Value(hashlib.sha256(content).hexdigest()).tagged,
        role=kit_file_role_of(path),
    )


class KitBundle(_Strict):
    """A manifest and the exact bytes of every artifact it names.

    ``provider_files`` holds the bundled provider packages' bytes, or nothing: a
    bundle sent to ``kit add`` carries none, because the adapter stages each file
    in the daemon's body store first.
    """

    tag: Literal["playbill-kit-bundle-v1"] = "playbill-kit-bundle-v1"
    manifest: KitManifest
    artifacts: tuple[KitArtifactBytes, ...]
    provider_files: tuple[KitProviderFileBytes, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    #: The inert files' bytes, or nothing (a bundle on its way to ``kit add``).
    files: tuple[KitFileBytes, ...] = Field(default=(), exclude_if=lambda value: not value)

    @field_validator("artifacts")
    @classmethod
    def _same_paths(
        cls, value: tuple[KitArtifactBytes, ...], info: ValidationInfo
    ) -> tuple[KitArtifactBytes, ...]:
        carried = tuple(item.path for item in value)
        _sorted_unique(carried, label="kit bundle paths")
        manifest = info.data.get("manifest")
        if manifest is not None and carried != tuple(item.path for item in manifest.artifacts):
            raise ValueError("kit bundle bytes must cover exactly the manifest's artifacts")
        return value

    @model_validator(mode="after")
    def _provider_bytes(self) -> KitBundle:
        if not self.provider_files:
            return self
        names = tuple(item.filename for item in self.provider_files)
        _sorted_unique(names, label="bundled provider files")
        expected = self.manifest.provider_files()
        if set(names) != set(expected):
            raise ValueError("bundled provider bytes must cover exactly the manifest's files")
        for item in self.provider_files:
            actual = Sha256Value(hashlib.sha256(item.content).hexdigest()).tagged
            if actual != expected[item.filename]:
                raise ValueError(f"bundled provider file {item.filename} differs from its sha256")
        return self

    @model_validator(mode="after")
    def _file_bytes(self) -> KitBundle:
        if not self.files:
            return self
        expected = self.manifest.kit_files()
        paths = tuple(item.path for item in self.files)
        _sorted_unique(paths, label="kit bundle files")
        if set(paths) != set(expected):
            raise ValueError("kit file bytes must cover exactly the manifest's files")
        for item in self.files:
            actual = Sha256Value(hashlib.sha256(item.content).hexdigest()).tagged
            if actual != expected[item.path]:
                raise ValueError(f"kit file {item.path} differs from its sha256")
        return self

    def contents(self) -> dict[str, bytes]:
        return {item.path: item.content for item in self.artifacts}

    def provider_contents(self) -> dict[str, bytes]:
        return {item.filename: item.content for item in self.provider_files}

    def file_contents(self) -> dict[str, bytes]:
        return {item.path: item.content for item in self.files}


class KitInstalledArtifact(_Strict):
    """One kit path: the release's snapshot digest and the digest this instance holds.

    They differ exactly when the instance already had history for the path, so
    the accepted artifact names its own predecessor.
    """

    path: str
    release_digest: str
    installed_digest: str
    #: The installed artifact's content apart from its lineage and lifecycle
    #: (``kit_content_digest``): an edit since install is a content change, so
    #: reverting one is no longer an edit.
    content_digest: str | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not kit_artifact_path_allowed(value):
            raise ValueError(f"a kit may not carry {value}")
        return value

    @field_validator("release_digest", "installed_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _digest(value)

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str | None) -> str | None:
        return None if value is None else _digest(value)


def kit_content_digest(payload: dict[str, object]) -> str:
    """One artifact's content apart from its lifecycle (lineage and live/retired)."""

    return Sha256Value(
        canonical_digest(
            "playbill-kit-artifact-content-v1",
            {key: value for key, value in payload.items() if key != "lifecycle"},
        )
    ).tagged


#: What taking a release's version of a conflicting definition does here.
KitConsequence = Literal[
    "overwrites_your_edit",
    "re_adds_retired",
    "takes_over_outside_definition",
    "replaces_carried_definition",
    "release_dropped",
]


class KitKeptDivergence(_Strict):
    """A definition the consumer kept its own way on purpose, so upgrades do not re-ask.

    ``release_digest`` is the release's version it declined (None for a definition
    the release dropped); a later release that changes that definition again asks
    once more.
    """

    path: str
    identity: str
    consequence: KitConsequence
    release_digest: str | None = None


class KitReceipt(_Strict):
    """The accepted record of one installed kit, carried as a Document body."""

    tag: Literal["playbill-kit-receipt-v1"] = "playbill-kit-receipt-v1"
    kit_id: str
    version: str
    content_digest: str
    owns: tuple[str, ...]
    # Definitions the kit owns: it may replace and retire these.
    artifacts: tuple[KitInstalledArtifact, ...]
    # Definitions it pins but does not own: never replaced or retired through it.
    carried: tuple[KitInstalledArtifact, ...] = ()
    source: str | None = None
    kept: tuple[KitKeptDivergence, ...] = ()
    provenance: KitProvenance | None = None
    #: The provider packages the installed release bundled.
    providers: tuple[KitProvider, ...] = Field(default=(), exclude_if=lambda value: not value)

    @field_validator("kit_id")
    @classmethod
    def _kit_id(cls, value: str) -> str:
        return _kit_id(value)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return _version(value)

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        return _digest(value)

    def digests(self) -> dict[str, str]:
        """The digest this instance accepted for each owned path."""

        return {item.path: item.installed_digest for item in self.artifacts}


class KitBuildProvider(_Strict):
    """A provider package to name in the kit, staged in the body store: the built
    wheel, the uv lock it was built with, and the wheels of the dependencies that
    lock names by path (``provider install WHEEL --lock FILE`` takes the same three).

    ``delivery`` ``index`` makes it the kit's default provider: the kit records
    the package, version, wheel and lock digests and carries none of the files,
    and ``kit add`` installs it by name from the consumer's provider index when no
    Provider there serves its operations. The staged wheel must be the
    published build and the lock the one it embeds.
    """

    wheel: ProviderWheelObject
    lock_digest: str
    dependencies: tuple[ProviderWheelObject, ...] = ()
    delivery: KitProviderDelivery = "bundled"

    @field_validator("lock_digest")
    @classmethod
    def _lock_digest(cls, value: str) -> str:
        return _digest(value)


class KitBuildRequest(_Strict):
    """Export this instance's definitions under ``owns`` as one kit release.

    ``providers`` bundles provider packages with the release (the same wheels
    this instance installed), so the kit's Lines can bind their operations.
    """

    kit_id: str
    version: str
    owns: tuple[str, ...]
    providers: tuple[KitBuildProvider, ...] = ()

    @field_validator("kit_id")
    @classmethod
    def _kit_id(cls, value: str) -> str:
        return _kit_id(value)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return _version(value)

    @field_validator("owns")
    @classmethod
    def _owns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _owns(value)


class KitBuildResult(_Strict):
    tag: Literal["playbill-kit-build-result-v1"] = "playbill-kit-build-result-v1"
    bundle: KitBundle


class KitAddRequest(_Strict):
    """Install or upgrade a kit. It always proposes: a conflicting definition takes
    the release's version unless kept, and a replaced definition's dependents are
    carried to it."""

    bundle: KitBundle
    # Where the bundle came from, recorded in the receipt (a registry reference
    # or a directory name); a registry reference lets kit status look for updates.
    source: str | None = None
    #: Definitions (``ClaimType:acme.account.seats``) to keep as this instance has
    #: them instead of taking the release's version; for a definition the release
    #: dropped, keep it live.
    keep: tuple[str, ...] = ()
    #: Keep every definition edited here since the kit installed it.
    keep_local_edits: bool = False
    #: Definitions the release dropped to retire along with their live dependents.
    #: A dropped definition with no dependents retires without being named; one
    #: with dependents is kept unless named here.
    retire_dependents: tuple[str, ...] = ()
    #: Install a release older than the installed one.
    allow_downgrade: bool = False
    #: Procedures (``Procedure:acme.triage``) to keep on the provider build they
    #: pin. Every other live Procedure bound to a provider the kit ships moves to
    #: the build the kit ships, in the same change set.
    keep_build: tuple[str, ...] = ()
    #: A kit install is derived across many artifacts, so it previews by default.
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("bundle")
    @classmethod
    def _staged(cls, value: KitBundle) -> KitBundle:
        if value.provider_files:
            raise ValueError(
                "bundled provider files travel through the body store: stage each one "
                "(the CLI and MCP adapters do) and send the bundle without them"
            )
        # Inert files are the reader's copy: the daemon installs definitions and
        # providers only, so their bytes never travel with an install.
        return value.model_copy(update={"files": ()}) if value.files else value

    @field_validator("keep", "retire_dependents")
    @classmethod
    def _identities(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        for identity in value:
            kind, separator, name = identity.partition(":")
            if not separator or not kind or not name:
                raise ValueError(f"{info.field_name} names a definition as KIND:NAME")
        return _sorted_unique(value, label=str(info.field_name))

    @field_validator("keep_build")
    @classmethod
    def _keep_build(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for identity in value:
            kind, separator, name = identity.partition(":")
            if not separator or kind != "Procedure" or not name:
                raise ValueError("keep_build names a Procedure as Procedure:NAME")
        return _sorted_unique(value, label="keep_build")


class KitRemoveRequest(_Strict):
    kit_id: str
    #: A kit removal is derived across many artifacts, so it previews by default.
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("kit_id")
    @classmethod
    def _kit_id(cls, value: str) -> str:
        return _kit_id(value)


KitPathAction = Literal["add", "unchanged", "replace", "retire", "keep"]


class KitPathPlan(_Strict):
    """One kit definition in a change: what happens to it and what that does here."""

    path: str
    action: KitPathAction
    #: The definition (``ClaimType:acme.account.seats``), for grouping by kind.
    identity: str | None = None
    #: What taking the release's version does here, or (``keep``) what was declined.
    consequence: KitConsequence | None = None
    #: Live artifacts here that pin this definition: carried to a replacement,
    #: retired with a retirement.
    dependent_count: int = 0
    detail: str | None = None


KitTransition = Literal["install", "upgrade", "downgrade", "reinstall"]

#: What a kit change does with one provider package: ``unchanged`` (this build
#: is installed), ``install`` (installed and registered now), ``would_install``
#: (a preview), ``awaiting_approval`` (its install proposal needs approval),
#: ``satisfied`` (an index default not installed: another live Provider here
#: serves its operations), ``blocked`` (this build is retired here, or its
#: install refused). Another installed build of the provider never blocks: the
#: kit's build installs beside it.
KitProviderAction = Literal[
    "unchanged", "install", "would_install", "awaiting_approval", "satisfied", "blocked"
]


class KitProviderStep(_Strict):
    provider_id: str
    package: str
    version: str
    action: KitProviderAction
    #: The exact build the kit ships (``<provider_id>/<build key>``). Installing
    #: it adds a build: other builds of the provider stay installed.
    build: str | None = None
    #: The install proposal, when it awaits approval or was refused.
    proposal_id: str | None = None
    detail: str | None = None


class KitOwnershipConflictRefusal(_Strict):
    """Another installed kit owns an overlapping prefix or holds a definition."""

    code: Literal["cruxible.kit.ownership_conflict"] = "cruxible.kit.ownership_conflict"
    our_prefixes: tuple[str, ...]
    owning_kit_id: str
    owning_prefix: str | None = None
    identity: str | None = None
    detail: str
    repair: str

    @model_validator(mode="after")
    def _collision_target(self) -> KitOwnershipConflictRefusal:
        if self.owning_prefix is None and self.identity is None:
            raise ValueError("an ownership conflict must name a prefix or identity")
        return self


class KitIdReusedRefusal(_Strict):
    """An installed kit id cannot silently become an unrelated set of prefixes."""

    code: Literal["cruxible.kit.id_reused"] = "cruxible.kit.id_reused"
    owning_kit_id: str
    our_prefixes: tuple[str, ...]
    installed_prefixes: tuple[str, ...]
    detail: str
    repair: str


KitAddRefusal = Annotated[
    KitOwnershipConflictRefusal | KitIdReusedRefusal, Field(discriminator="code")
]


class KitChangeResult(_Strict):
    """What a kit add or remove changed, or why it changed nothing."""

    tag: Literal["playbill-kit-change-result-v1"] = "playbill-kit-change-result-v1"
    kit_id: str
    version: str | None
    # A kit change lands at once (``accepted``) when the instance's approval
    # policy requires no approval, like provider install and value writes;
    # otherwise it stops at ``proposed`` for the ordinary review and activation.
    # A kit that bundles providers installs them first: when an install awaits
    # approval the change stops at ``awaiting_providers``, and the definitions
    # are proposed on their own, as this caller, once those installs activate
    # (``kit status`` shows the pending resume). A preview (the default)
    # answers would_propose or would_block and proposes nothing; commit with
    # dry_run=false and at=<coordinate>.
    status: Literal[
        "unchanged",
        "proposed",
        "accepted",
        "blocked",
        "awaiting_providers",
        "would_propose",
        "would_block",
    ]
    proposal_id: str | None = None
    approval_required: bool = False
    #: From the installed version to this release's (None for a removal).
    transition: KitTransition | None = None
    installed_version: str | None = None
    provenance: KitProvenance | None = None
    #: The bundled provider packages, installed before the definitions.
    providers: tuple[KitProviderStep, ...] = ()
    plan: tuple[KitPathPlan, ...] = ()
    #: Each live Procedure bound to another build of a provider the kit ships,
    #: moved to the kit's build in this change set (or kept, with keep_build).
    repins: tuple[ProviderRepinV1, ...] = ()
    #: Automation retired with definitions, rather than carried to live successors.
    stops: tuple[StoppedAutomationV1, ...] = ()
    automation_carries: tuple[AutomationCarryV1, ...] = ()
    #: Hard ownership/id blocks; the same structured refusals on every surface.
    refusals: tuple[KitAddRefusal, ...] = ()
    detail: str | None = None
    #: The accepted coordinate this change was evaluated at.
    coordinate: GetCoordinate | None = None


#: Whether the adapter looked for a newer release of a registry-sourced kit:
#: ``checked`` (latest_available answers), ``offline`` (skipped), ``local_source``
#: (installed from a directory or layout: nothing to check), ``unavailable``.
KitUpdateCheck = Literal["not_checked", "checked", "offline", "local_source", "unavailable"]


#: A kit's provider package here: ``installed`` (this build is the live
#: Provider), ``differs`` (another build of it is), ``satisfied`` (an index
#: default that is not: another live Provider serves its operations),
#: ``missing`` (none is live).
KitProviderInstallState = Literal["installed", "differs", "satisfied", "missing"]


class KitProviderStatus(_Strict):
    provider_id: str
    package: str
    version: str
    wheel_sha256: str
    delivery: KitProviderDelivery = "bundled"
    state: KitProviderInstallState
    #: The live Provider's package version, when another build of it is installed.
    installed_version: str | None = None


class InstalledKit(_Strict):
    kit_id: str
    version: str
    content_digest: str
    source: str | None = None
    # Kit paths whose accepted content no longer matches the receipt: local edits.
    drifted: tuple[str, ...] = ()
    provenance: KitProvenance | None = None
    kept: tuple[KitKeptDivergence, ...] = ()
    #: The provider packages the release bundled, and whether each is installed.
    providers: tuple[KitProviderStatus, ...] = ()
    #: Filled by the CLI and MCP adapters from the registry's tags.
    latest_available: str | None = None
    update_check: KitUpdateCheck = "not_checked"


#: A recorded kit add waiting on its provider installs: ``waiting`` (an install
#: has not activated yet), ``proposed`` (the definitions were proposed, or
#: landed, once they did), ``blocked`` (proposing them refused; ``detail`` says why).
KitResumeState = Literal["waiting", "proposed", "blocked"]


class KitResumeStatus(_Strict):
    """A ``kit add`` that stopped at awaiting_providers and resumes on activation."""

    kit_id: str
    version: str
    state: KitResumeState
    #: The principal whose kit add this is; the definitions are proposed as it.
    actor_id: str
    recorded_at: str
    #: Bundled providers whose install has not activated here yet.
    waiting_on: tuple[str, ...] = ()
    #: The definitions' proposal, once proposed.
    proposal_id: str | None = None
    detail: str | None = None
    #: Where the bundle came from, as the kit add recorded it.
    source: str | None = None


class KitStatus(_Strict):
    tag: Literal["playbill-kit-status-v1"] = "playbill-kit-status-v1"
    kits: tuple[InstalledKit, ...] = ()
    #: Kit adds waiting on provider installs, or whose resume proposed or refused.
    resumes: tuple[KitResumeStatus, ...] = ()
