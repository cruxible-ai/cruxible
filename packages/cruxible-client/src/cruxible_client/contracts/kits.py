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
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from .canonical import Sha256Value, canonical_digest
from .change_control import DryRun, PreviewAt
from .get_reads import GetCoordinate
from .projection import AcceptedCoordinate

KIT_MANIFEST_FILE = "cruxible-kit.json"
KIT_ARTIFACT_DIRECTORY = "artifacts"
KIT_RECEIPT_DOCUMENT_KIND = "kit_receipt"

# The definition families a kit may carry. Authority (governance, principals,
# mandates), local binding (providers, lines) and state (subjects, claims,
# attestations) are never kit content. Procedures, ProviderInterfaces and
# SourceAcquisitionPolicies are definitions too, but join only once their
# references can be moved field by field: a Procedure's pins feed its derived
# definition digest, and a policy's literal defaults can hold digests that are
# values, not pins.
KIT_ARTIFACT_PREFIXES: tuple[str, ...] = (
    "capture-contracts/",
    "claim-types/",
    "query-definitions/",
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
    """A manifest and the exact bytes of every artifact it names."""

    tag: Literal["playbill-kit-bundle-v1"] = "playbill-kit-bundle-v1"
    manifest: KitManifest
    artifacts: tuple[KitArtifactBytes, ...]

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

    def contents(self) -> dict[str, bytes]:
        return {item.path: item.content for item in self.artifacts}


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


class KitBuildRequest(_Strict):
    """Export this instance's definitions under ``owns`` as one kit release."""

    kit_id: str
    version: str
    owns: tuple[str, ...]

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


class KitChangeResult(_Strict):
    """What a kit add or remove changed, or why it changed nothing."""

    tag: Literal["playbill-kit-change-result-v1"] = "playbill-kit-change-result-v1"
    kit_id: str
    version: str | None
    # A kit change lands at once (``accepted``) when the instance's approval
    # policy requires no approval, like provider install and value writes;
    # otherwise it stops at ``proposed`` for the ordinary review and activation.
    # A preview (the default) answers would_propose or would_block and proposes
    # nothing; commit with dry_run=false and at=<coordinate>.
    status: Literal["unchanged", "proposed", "accepted", "blocked", "would_propose", "would_block"]
    proposal_id: str | None = None
    approval_required: bool = False
    #: From the installed version to this release's (None for a removal).
    transition: KitTransition | None = None
    installed_version: str | None = None
    provenance: KitProvenance | None = None
    plan: tuple[KitPathPlan, ...] = ()
    detail: str | None = None
    #: The accepted coordinate this change was evaluated at.
    coordinate: GetCoordinate | None = None


#: Whether the adapter looked for a newer release of a registry-sourced kit:
#: ``checked`` (latest_available answers), ``offline`` (skipped), ``local_source``
#: (installed from a directory or layout: nothing to check), ``unavailable``.
KitUpdateCheck = Literal["not_checked", "checked", "offline", "local_source", "unavailable"]


class InstalledKit(_Strict):
    kit_id: str
    version: str
    content_digest: str
    source: str | None = None
    # Kit paths whose accepted content no longer matches the receipt: local edits.
    drifted: tuple[str, ...] = ()
    provenance: KitProvenance | None = None
    kept: tuple[KitKeptDivergence, ...] = ()
    #: Filled by the CLI and MCP adapters from the registry's tags.
    latest_available: str | None = None
    update_check: KitUpdateCheck = "not_checked"


class KitStatus(_Strict):
    tag: Literal["playbill-kit-status-v1"] = "playbill-kit-status-v1"
    kits: tuple[InstalledKit, ...] = ()
