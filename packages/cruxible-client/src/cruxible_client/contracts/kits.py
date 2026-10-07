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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from .canonical import Sha256Value, canonical_digest
from .change_control import DryRun, PreviewAt
from .get_reads import GetCoordinate
from .projection import AcceptedCoordinate
from .provider_installation import ProviderWheelObject

KIT_MANIFEST_FILE = "cruxible-kit.json"
KIT_ARTIFACT_DIRECTORY = "artifacts"
#: Where a kit directory (and a kit artifact's layer) holds its bundled provider
#: packages: built wheels and uv locks, never source.
KIT_PROVIDER_DIRECTORY = "providers"
KIT_RECEIPT_DOCUMENT_KIND = "kit_receipt"

# The definition families a kit may carry. Authority (governance, principals,
# mandates), local binding (providers, lines) and state (subjects, claims,
# attestations) are never kit content. A Procedure or Blueprint moves its pins
# through its typed graph (they feed its definition digest); a
# SourceAcquisitionPolicy moves only its pins, never the digests its rules hold
# as values. A ProviderInterface is carried only as the exact bytes its
# provider package registers, and never owned: the kit bundles that package.
KIT_ARTIFACT_PREFIXES: tuple[str, ...] = (
    "blueprints/",
    "capture-contracts/",
    "claim-types/",
    "procedures/",
    "provider-interfaces/",
    "query-definitions/",
    "source-acquisition-policies/",
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


class KitProvider(_Strict):
    """One provider package a kit bundles: its built wheel and the uv lock it was
    built with, plus the wheels of the first-party dependencies that lock names by
    path. Registry dependencies are never bundled: the daemon resolves them by
    name from its provider index (PyPI unless the operator configured one),
    pinned by the lock's hashes.

    ``kit add`` installs it before the kit's definitions, through the ordinary
    transfer install, so the Provider and ProviderInterfaces it registers are the
    ones the kit's Procedures and Blueprints pin.
    """

    provider_id: str
    package: str
    version: str
    wheel: KitProviderFile
    lock: KitProviderFile
    dependencies: tuple[KitProviderFile, ...] = ()
    #: The ProviderInterfaces the package registers.
    interfaces: tuple[str, ...] = ()

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

    @field_validator("interfaces")
    @classmethod
    def _interfaces(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="provider interfaces")

    def files(self) -> tuple[KitProviderFile, ...]:
        return (self.wheel, self.lock, *self.dependencies)


def _provider_files(providers: tuple[KitProvider, ...]) -> dict[str, str]:
    """Every bundled file name -> its sha256; one name never holds two contents."""

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
    #: Provider packages the kit bundles (wheel and lock), sorted by provider id.
    providers: tuple[KitProvider, ...] = Field(default=(), exclude_if=lambda value: not value)
    provenance: KitProvenance | None = None

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

    def provider_files(self) -> dict[str, str]:
        """Every bundled provider file name -> its sha256."""

        return _provider_files(self.providers)

    @property
    def content_digest(self) -> str:
        """The release identity: every manifest field, including each artifact digest."""

        return Sha256Value(
            canonical_digest(
                "playbill-kit-content-v1",
                self.model_dump(mode="json", exclude={"tag", "provenance"}),
            )
        ).tagged

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

    def contents(self) -> dict[str, bytes]:
        return {item.path: item.content for item in self.artifacts}

    def provider_contents(self) -> dict[str, bytes]:
        return {item.filename: item.content for item in self.provider_files}


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
    """A provider package to bundle, staged in the body store: the built wheel, the
    uv lock it was built with, and the wheels of the dependencies that lock names
    by path (``provider install WHEEL --lock FILE`` takes the same three)."""

    wheel: ProviderWheelObject
    lock_digest: str
    dependencies: tuple[ProviderWheelObject, ...] = ()

    @field_validator("lock_digest")
    @classmethod
    def _lock_digest(cls, value: str) -> str:
        return _digest(value)


class KitBuildRequest(_Strict):
    """Export this instance's definitions under ``owns`` as one kit release.

    ``providers`` bundles provider packages with the release. Every Provider a
    carried Procedure pins must be bundled (the same wheel this instance
    installed), and every ProviderInterface the kit carries must be exactly what
    a bundled wheel registers.
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
        return value

    @field_validator("keep", "retire_dependents")
    @classmethod
    def _identities(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        for identity in value:
            kind, separator, name = identity.partition(":")
            if not separator or not kind or not name:
                raise ValueError(f"{info.field_name} names a definition as KIND:NAME")
        return _sorted_unique(value, label=str(info.field_name))


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

#: What a kit change does with one bundled provider: ``unchanged`` (this build
#: is installed), ``install`` (installed and registered now), ``would_install``
#: (a preview), ``awaiting_approval`` (its install proposal needs approval),
#: ``blocked`` (a different build of it is installed, or its install refused).
KitProviderAction = Literal["unchanged", "install", "would_install", "awaiting_approval", "blocked"]


class KitProviderStep(_Strict):
    provider_id: str
    package: str
    version: str
    action: KitProviderAction
    #: The install proposal, when it awaits approval or was refused.
    proposal_id: str | None = None
    detail: str | None = None


class KitChangeResult(_Strict):
    """What a kit add or remove changed, or why it changed nothing."""

    tag: Literal["playbill-kit-change-result-v1"] = "playbill-kit-change-result-v1"
    kit_id: str
    version: str | None
    # A kit change lands at once (``accepted``) when the instance's approval
    # policy requires no approval, like provider install and value writes;
    # otherwise it stops at ``proposed`` for the ordinary review and activation.
    # A kit that bundles providers installs them first: when an install awaits
    # approval the change stops at ``awaiting_providers`` and proposes the
    # definitions once they land (run kit add again). A preview (the default)
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
    detail: str | None = None
    #: The accepted coordinate this change was evaluated at.
    coordinate: GetCoordinate | None = None


#: Whether the adapter looked for a newer release of a registry-sourced kit:
#: ``checked`` (latest_available answers), ``offline`` (skipped), ``local_source``
#: (installed from a directory or layout: nothing to check), ``unavailable``.
KitUpdateCheck = Literal["not_checked", "checked", "offline", "local_source", "unavailable"]


#: A bundled provider here: ``installed`` (this build is the live Provider),
#: ``differs`` (another build of it is), ``missing`` (none is live).
KitProviderInstallState = Literal["installed", "differs", "missing"]


class KitProviderStatus(_Strict):
    provider_id: str
    package: str
    version: str
    wheel_sha256: str
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


class KitStatus(_Strict):
    tag: Literal["playbill-kit-status-v1"] = "playbill-kit-status-v1"
    kits: tuple[InstalledKit, ...] = ()
