"""Kits: frozen sets of definition artifacts imported through one governed change set.

A kit carries definitions only -- what can be known and how -- never authority,
bindings, or state. Its artifacts are the exact accepted bytes an instance holds
after import, so a kit's digests are the digests every unmodified consumer
holds. The lineage inside those bytes is the kit's own release lineage: a
first release names no predecessors, and a later release names the previous
release's digest for each artifact it changes.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from .canonical import Sha256Value, canonical_digest

KIT_MANIFEST_FILE = "cruxible-kit.json"
KIT_ARTIFACT_DIRECTORY = "artifacts"
KIT_RECEIPT_DOCUMENT_KIND = "kit_receipt"

# The definition families a kit may carry. Authority (governance, principals,
# mandates), local binding (providers, lines) and state (subjects, claims,
# attestations) are never kit content.
KIT_ARTIFACT_PREFIXES: tuple[str, ...] = (
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


class KitArtifactV1(_Strict):
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


class KitReleaseRefV1(_Strict):
    """The release a later release descends from."""

    version: str
    content_digest: str

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return _version(value)

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        return _digest(value)


class KitManifestV1(_Strict):
    tag: Literal["playbill-kit-manifest-v1"] = "playbill-kit-manifest-v1"
    kit_id: str
    version: str
    # Identity prefixes this kit defines, each ending in a dot (``dev.``).
    owns: tuple[str, ...]
    previous: KitReleaseRefV1 | None = None
    artifacts: tuple[KitArtifactV1, ...]

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
    def _artifacts(cls, value: tuple[KitArtifactV1, ...]) -> tuple[KitArtifactV1, ...]:
        _sorted_unique(tuple(item.path for item in value), label="kit artifact paths")
        return value

    @property
    def content_digest(self) -> str:
        """The release identity: every manifest field, including each artifact digest."""

        return Sha256Value(
            canonical_digest(
                "playbill-kit-content-v1", self.model_dump(mode="json", exclude={"tag"})
            )
        ).tagged

    def digests(self) -> dict[str, str]:
        return {item.path: item.artifact_digest for item in self.artifacts}


class KitArtifactBytesV1(_Strict):
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
    def of(cls, path: str, content: bytes) -> KitArtifactBytesV1:
        return cls(path=path, content_base64=base64.b64encode(content).decode("ascii"))


class KitBundleV1(_Strict):
    """A manifest and the exact bytes of every artifact it names."""

    tag: Literal["playbill-kit-bundle-v1"] = "playbill-kit-bundle-v1"
    manifest: KitManifestV1
    artifacts: tuple[KitArtifactBytesV1, ...]

    @field_validator("artifacts")
    @classmethod
    def _same_paths(
        cls, value: tuple[KitArtifactBytesV1, ...], info: ValidationInfo
    ) -> tuple[KitArtifactBytesV1, ...]:
        carried = tuple(item.path for item in value)
        _sorted_unique(carried, label="kit bundle paths")
        manifest = info.data.get("manifest")
        if manifest is not None and carried != tuple(item.path for item in manifest.artifacts):
            raise ValueError("kit bundle bytes must cover exactly the manifest's artifacts")
        return value

    def contents(self) -> dict[str, bytes]:
        return {item.path: item.content for item in self.artifacts}


class KitReceiptV1(_Strict):
    """The accepted record of one installed kit, carried as a Document body."""

    tag: Literal["playbill-kit-receipt-v1"] = "playbill-kit-receipt-v1"
    kit_id: str
    version: str
    content_digest: str
    owns: tuple[str, ...]
    # Definitions the kit owns: it may replace and retire these.
    artifacts: tuple[KitArtifactV1, ...]
    # Definitions it pins but does not own: never replaced or retired through it.
    carried: tuple[KitArtifactV1, ...] = ()
    source: str | None = None

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
        return {item.path: item.artifact_digest for item in self.artifacts}


class PlaybillKitBuildRequestV1(_Strict):
    """Export this instance's definitions under ``owns`` as one kit release."""

    kit_id: str
    version: str
    owns: tuple[str, ...]
    previous: KitBundleV1 | None = None

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


class PlaybillKitBuildResultV1(_Strict):
    tag: Literal["playbill-kit-build-result-v1"] = "playbill-kit-build-result-v1"
    bundle: KitBundleV1


class PlaybillKitAddRequestV1(_Strict):
    bundle: KitBundleV1
    # Where the bundle came from, recorded in the receipt (a registry reference
    # or a directory name); never interpreted.
    source: str | None = None


class PlaybillKitRemoveRequestV1(_Strict):
    kit_id: str

    @field_validator("kit_id")
    @classmethod
    def _kit_id(cls, value: str) -> str:
        return _kit_id(value)


KitPathAction = Literal["add", "unchanged", "replace", "retire", "conflict"]


class KitPathPlanV1(_Strict):
    path: str
    action: KitPathAction
    detail: str | None = None


class PlaybillKitChangeResultV1(_Strict):
    """What a kit add or remove changed, or why it changed nothing."""

    tag: Literal["playbill-kit-change-result-v1"] = "playbill-kit-change-result-v1"
    kit_id: str
    version: str | None
    # A kit change is proposed, never activated here: activation stays the
    # ordinary tier-gated step, after any approval the instance's policy requires.
    status: Literal["unchanged", "proposed", "blocked"]
    proposal_id: str | None = None
    approval_required: bool = False
    plan: tuple[KitPathPlanV1, ...] = ()
    missing_interfaces: tuple[str, ...] = ()
    detail: str | None = None


class InstalledKitV1(_Strict):
    kit_id: str
    version: str
    content_digest: str
    source: str | None = None
    # Kit paths whose accepted bytes no longer match the receipt: local edits.
    drifted: tuple[str, ...] = ()


class PlaybillKitStatusV1(_Strict):
    tag: Literal["playbill-kit-status-v1"] = "playbill-kit-status-v1"
    kits: tuple[InstalledKitV1, ...] = ()
