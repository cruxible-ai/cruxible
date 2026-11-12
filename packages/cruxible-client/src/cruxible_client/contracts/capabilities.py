"""Capabilities: one named, versioned kind of call that a Procedure slot asks for.

A Capability is a small immutable artifact (``capabilities/<name>/v<N>.json``,
format ``cruxible-capability-v1``): an authored description, an effect, and the
request (``in``) and result (``out``) Contracts, each pinned by digest once
accepted. A Procedure slot names one capability version and nothing else but
``may_truncate``; a provider operation states the capability names it serves.
Whether an operation fits a slot is decided elsewhere, by shape; this module
holds the artifact, its path, digest and canonical bytes, its acceptance law,
the slot shape, and the slot's derived signature.

The law is the Contract law's: an accepted version is immutable, version N names
version N-1's digest, and both Contract pins resolve to the exact accepted
Contracts they name. One more: every version of one name has the same effect.
Names under ``workspace.`` belong to core: a capability may be declared there
only under a name core's builtin serves (any version of it).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Collection, Mapping
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.law_problems import LawProblem
from cruxible_client.contracts.provider_operations import OperationEffect, OperationSignature
from cruxible_client.contracts.shapes import (
    ContractRef,
    contract_digest,
    contract_path,
    parse_contract,
    parse_contract_ref,
)

CAPABILITY_ARTIFACT_FORMAT = "cruxible-capability-v1"
CAPABILITY_DIGEST_DOMAIN = "cruxible-capability-v1"
CAPABILITY_PREFIX = "capabilities/"
CAPABILITY_IDENTITY_KIND = "Capability"
CAPABILITY_PIN_ROLE = "capability"
#: Names under this prefix are core's: only a name core's builtin serves may be declared.
RESERVED_CAPABILITY_PREFIX = "workspace."
MAX_CAPABILITY_DESCRIPTION = 2048

_CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CAPABILITY_PATH_RE = re.compile(
    r"^capabilities/([a-z][a-z0-9_.-]{0,127})/v([1-9][0-9]{0,8})\.json$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

CAPABILITY_IMMUTABLE: Final = "cruxible.capability.immutable"
CAPABILITY_PREDECESSOR_MISSING: Final = "cruxible.capability.predecessor_missing"
CAPABILITY_PREDECESSOR_MISMATCH: Final = "cruxible.capability.predecessor_mismatch"
CAPABILITY_EFFECT_CHANGED: Final = "cruxible.capability.effect_changed"
CAPABILITY_PIN_UNRESOLVED: Final = "cruxible.capability.pin_unresolved"
CAPABILITY_PIN_DIGEST_MISMATCH: Final = "cruxible.capability.pin_digest_mismatch"
CAPABILITY_RESERVED: Final = "cruxible.capability.reserved"

#: Every code the capability acceptance law returns.
CapabilityLawCode = Literal[
    "cruxible.capability.immutable",
    "cruxible.capability.predecessor_missing",
    "cruxible.capability.predecessor_mismatch",
    "cruxible.capability.effect_changed",
    "cruxible.capability.pin_unresolved",
    "cruxible.capability.pin_digest_mismatch",
    "cruxible.capability.reserved",
]


class CapabilityFormatError(FormatError, ValueError):
    """A Capability artifact, path or pin spelling is invalid."""

    code: str = "cruxible.capability.invalid"


class _Shape(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        json_schema_mode_override="validation",
    )


def _not_none(value: object) -> bool:
    return value is None


def validate_capability_name(value: str) -> str:
    """A capability name: a canonical lowercase dotted identifier, as a Contract name."""

    if unicodedata.normalize("NFC", value) != value or not _CAPABILITY_NAME_RE.fullmatch(value):
        raise ValueError(f"capability name {value!r} is not a canonical identifier")
    return value


def is_reserved_capability_name(name: str) -> bool:
    """Whether ``name`` is under the prefix core reserves for its builtin."""

    return name.startswith(RESERVED_CAPABILITY_PREFIX)


def _contract_ref(value: object) -> object:
    """Authors write ``name@N``; the structured form is accepted as is."""

    if isinstance(value, str):
        return parse_contract_ref(value)
    return value


class CapabilityRef(_Shape):
    """A pin to one capability version. Authors write ``name@N``; acceptance adds the digest."""

    name: str
    version: int = Field(ge=1, le=999_999_999)
    digest: str | None = Field(default=None, exclude_if=_not_none)

    _name = field_validator("name")(validate_capability_name)

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST_RE.fullmatch(value):
            raise ValueError("a capability pin digest is a sha256 digest")
        return value

    @property
    def identity(self) -> ArtifactIdentity:
        return capability_identity(self.name, self.version)

    @property
    def spelling(self) -> str:
        return f"{self.name}@{self.version}"


def parse_capability_ref(spelling: str) -> CapabilityRef:
    """``name@N`` as a capability pin (the digest is added where the pin resolves)."""

    name, at, version = spelling.strip().partition("@")
    if not at or not version.isdigit():
        raise CapabilityFormatError(f"a capability pin is written name@N, not {spelling!r}")
    return CapabilityRef(name=name, version=int(version))


def _capability_ref(value: object) -> object:
    if isinstance(value, str):
        return parse_capability_ref(value)
    return value


class Capability(_Shape):
    """One immutable capability version. A successor version names its predecessor's digest.

    ``description`` leads with what the capability does; it is covered by the
    digest, so a wording fix is a new version. ``in`` is what a slot sends and
    ``out`` is what it receives back.
    """

    artifact_format: Literal["cruxible-capability-v1"] = "cruxible-capability-v1"
    name: str
    version: int = Field(ge=1, le=999_999_999)
    description: str = Field(min_length=1, max_length=MAX_CAPABILITY_DESCRIPTION)
    effect: OperationEffect
    in_: ContractRef = Field(alias="in")
    out: ContractRef
    predecessor_digest: str | None = Field(default=None, exclude_if=_not_none)

    _name = field_validator("name")(validate_capability_name)
    _contracts = field_validator("in_", "out", mode="before")(_contract_ref)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value or value != value.strip():
            raise ValueError("a capability description is trimmed NFC text")
        return value

    @field_validator("predecessor_digest")
    @classmethod
    def _predecessor(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _succession(self) -> Capability:
        if (self.version == 1) != (self.predecessor_digest is None):
            raise ValueError(
                "version 1 of a capability has no predecessor; every later version names "
                "its predecessor's digest"
            )
        return self

    @property
    def identity(self) -> ArtifactIdentity:
        return capability_identity(self.name, self.version)

    @property
    def lifecycle(self) -> ArtifactLifecycle:
        """Capabilities are immutable and never retire; succession is a new version."""

        return ArtifactLifecycle()

    @property
    def ref(self) -> CapabilityRef:
        return CapabilityRef(name=self.name, version=self.version)

    def contract_refs(self) -> tuple[ContractRef, ContractRef]:
        """The two Contract versions this capability pins: in, then out."""

        return (self.in_, self.out)

    @property
    def pinned(self) -> bool:
        return all(ref.digest is not None for ref in self.contract_refs())


#: Resolve a capability pin to the accepted version it names (by exact digest when pinned).
CapabilityLookup = Callable[[CapabilityRef], Capability | None]


def capability_identity(name: str, version: int) -> ArtifactIdentity:
    return ArtifactIdentity(kind=CAPABILITY_IDENTITY_KIND, name=f"{name}/v{version}")


def capability_ref_for_identity(identity: ArtifactIdentity) -> CapabilityRef:
    if identity.kind != CAPABILITY_IDENTITY_KIND:
        raise CapabilityFormatError("a capability pin targets a Capability identity")
    name, slash, version = identity.name.rpartition("/v")
    if not slash or not version.isdigit():
        raise CapabilityFormatError("a Capability identity is <name>/v<N>")
    return CapabilityRef(name=name, version=int(version))


def capability_path(name: str, version: int) -> str:
    if not _CAPABILITY_NAME_RE.fullmatch(name) or version < 1:
        raise CapabilityFormatError("capability is not path-addressable")
    return f"{CAPABILITY_PREFIX}{name}/v{version}.json"


def capability_path_for_identity(identity_name: str) -> str:
    ref = capability_ref_for_identity(
        ArtifactIdentity(kind=CAPABILITY_IDENTITY_KIND, name=identity_name)
    )
    return capability_path(ref.name, ref.version)


def capability_path_parts(path: str) -> tuple[str, int] | None:
    match = _CAPABILITY_PATH_RE.fullmatch(path)
    return None if match is None else (match.group(1), int(match.group(2)))


def render_capability(capability: Capability) -> bytes:
    return pretty_canonical_bytes(capability.model_dump(mode="json"))


def capability_digest(capability: Capability) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest, CAPABILITY_DIGEST_DOMAIN, capability.model_dump(mode="json")
    )


def capability_pin(capability: Capability) -> CapabilityRef:
    """The exact pin a slot stores for this version."""

    return CapabilityRef(
        name=capability.name,
        version=capability.version,
        digest=capability_digest(capability).tagged,
    )


def validate_capability(data: object) -> Capability:
    try:
        return Capability.model_validate(data)
    except ValidationError as exc:
        raise CapabilityFormatError(f"Capability failed strict validation: {exc}") from exc


def parse_capability(content: bytes, *, path: str) -> Capability:
    """Strict canonical parse of one accepted capability; both Contract pins carry digests."""

    try:
        data = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CapabilityFormatError("Capability is not canonical JSON") from exc
    capability = validate_capability(data)
    if not capability.pinned:
        raise CapabilityFormatError("an accepted capability pins both Contracts by digest")
    if capability_path(capability.name, capability.version) != path:
        raise CapabilityFormatError("Capability identity/path disagreement")
    if render_capability(capability) != content:
        raise CapabilityFormatError("Capability is not in canonical wire form")
    return capability


def tree_capability_resolver(tree: Mapping[str, bytes]) -> CapabilityLookup:
    """Read capability versions from a (candidate or accepted) tree.

    A reference that pins a digest resolves only to the version with that digest.
    """

    found: dict[tuple[str, int], Capability | None] = {}

    def resolve(ref: CapabilityRef) -> Capability | None:
        key = (ref.name, ref.version)
        if key not in found:
            path = capability_path(ref.name, ref.version)
            content = tree.get(path)
            found[key] = None if content is None else parse_capability(content, path=path)
        capability = found[key]
        if capability is None or (
            ref.digest is not None and capability_digest(capability).tagged != ref.digest
        ):
            return None
        return capability

    return resolve


def capability_signature(capability: Capability) -> OperationSignature:
    """The capability's ``{in, out, effect}``, where code still needs a signature."""

    return OperationSignature.model_validate(
        {"in": capability.in_, "out": capability.out, "effect": capability.effect}
    )


# -- the acceptance law ------------------------------------------------------------------


def evaluate_capability_law(
    capability: Capability,
    *,
    path: str,
    lookup: Callable[[str], bytes | None],
    parent_content: bytes | None,
    content: bytes,
    builtin_capabilities: Collection[str],
) -> tuple[LawProblem, ...]:
    """Judge one capability version against its candidate tree.

    A version is immutable; its predecessor is the accepted previous version
    and has the same effect; both Contract pins resolve to the exact accepted
    Contracts they name. ``builtin_capabilities`` are the names core's builtin
    serves: a name under ``workspace.`` must be one of them.
    """

    problems: list[LawProblem] = []
    spelling = capability.ref.spelling
    if parent_content is not None and parent_content != content:
        problems.append(
            LawProblem(
                CAPABILITY_IMMUTABLE,
                f"{capability.identity.qualified} is accepted and immutable; "
                "author the next version instead.",
            )
        )
    if is_reserved_capability_name(capability.name) and (
        capability.name not in builtin_capabilities
    ):
        served = ", ".join(sorted(builtin_capabilities)) or "none"
        problems.append(
            LawProblem(
                CAPABILITY_RESERVED,
                f"{capability.name} is under {RESERVED_CAPABILITY_PREFIX}, which core reserves "
                f"for the capabilities its builtin serves ({served}); declare it under "
                "your own prefix.",
            )
        )
    if capability.version > 1:
        previous_path = capability_path(capability.name, capability.version - 1)
        previous_content = lookup(previous_path)
        if previous_content is None:
            problems.append(
                LawProblem(
                    CAPABILITY_PREDECESSOR_MISSING,
                    f"{spelling} succeeds version {capability.version - 1}, which is not accepted.",
                )
            )
        else:
            previous = parse_capability(previous_content, path=previous_path)
            expected = capability_digest(previous).tagged
            if capability.predecessor_digest != expected:
                problems.append(
                    LawProblem(
                        CAPABILITY_PREDECESSOR_MISMATCH,
                        f"{spelling} must name {expected} as its predecessor.",
                    )
                )
            if previous.effect != capability.effect:
                problems.append(
                    LawProblem(
                        CAPABILITY_EFFECT_CHANGED,
                        f"{spelling} is {capability.effect}, but version "
                        f"{previous.version} is {previous.effect}; a different effect is a "
                        "new capability name.",
                    )
                )
    for role, ref in (("in", capability.in_), ("out", capability.out)):
        target_path = contract_path(ref.name, ref.version)
        target = lookup(target_path)
        if target is None:
            problems.append(
                LawProblem(
                    CAPABILITY_PIN_UNRESOLVED,
                    f"{spelling} {role}: {ref.spelling} is not an accepted Contract.",
                )
            )
            continue
        digest = contract_digest(parse_contract(target, path=target_path)).tagged
        if ref.digest != digest:
            problems.append(
                LawProblem(
                    CAPABILITY_PIN_DIGEST_MISMATCH,
                    f"{spelling} {role}: {ref.spelling} pins {ref.digest}, but the accepted "
                    f"version is {digest}.",
                )
            )
    return tuple(problems)


# -- the slot a Procedure declares ------------------------------------------------------


class CapabilitySlot(_Shape):
    """One binding point a Procedure declares: the capability version its operation serves.

    ``may_truncate`` declares that the slot treats its result as possibly
    incomplete; an operation that may truncate binds only such a slot.
    """

    capability: CapabilityRef
    may_truncate: bool = False

    _capability = field_validator("capability", mode="before")(_capability_ref)


def capability_slot_signature(
    slot: CapabilitySlot, resolve: CapabilityLookup
) -> OperationSignature | None:
    """The slot's derived signature (its capability's in, out and effect), or ``None``
    when the pinned capability does not resolve."""

    capability = resolve(slot.capability)
    return None if capability is None else capability_signature(capability)


__all__ = [
    "CAPABILITY_ARTIFACT_FORMAT",
    "CAPABILITY_DIGEST_DOMAIN",
    "CAPABILITY_EFFECT_CHANGED",
    "CAPABILITY_IDENTITY_KIND",
    "CAPABILITY_IMMUTABLE",
    "CAPABILITY_PIN_DIGEST_MISMATCH",
    "CAPABILITY_PIN_ROLE",
    "CAPABILITY_PIN_UNRESOLVED",
    "CAPABILITY_PREDECESSOR_MISMATCH",
    "CAPABILITY_PREDECESSOR_MISSING",
    "CAPABILITY_PREFIX",
    "CAPABILITY_RESERVED",
    "MAX_CAPABILITY_DESCRIPTION",
    "RESERVED_CAPABILITY_PREFIX",
    "Capability",
    "CapabilityFormatError",
    "CapabilityLawCode",
    "CapabilityLookup",
    "CapabilityRef",
    "CapabilitySlot",
    "capability_digest",
    "capability_identity",
    "capability_path",
    "capability_path_for_identity",
    "capability_path_parts",
    "capability_pin",
    "capability_ref_for_identity",
    "capability_signature",
    "capability_slot_signature",
    "evaluate_capability_law",
    "is_reserved_capability_name",
    "parse_capability",
    "parse_capability_ref",
    "render_capability",
    "tree_capability_resolver",
    "validate_capability",
    "validate_capability_name",
]
