"""Provider registration schema 3: operations that serve capabilities, with carried shapes.

A provider package ships ``registration.json``. In schema 3 each operation
declares what it is (``description``), where its code is (``source``), which
capability names it serves (``serves``: names, no versions), its own ``in`` and
``out`` Contracts, its effect, whether it may truncate a listing, its typed
refusals and its entrypoint. ``enumerates`` is gone: a derived kind's layout is
the declared enumeration.

The Contracts and CaptureKinds an operation accepts and returns are not looked
up on an instance. The package carries its own copies in ``declares``: every
Contract its operations reach (full ``cruxible-contract-v1`` documents), every
CaptureKind they name with its decoding and layout, and ``built_against``, the
record of which capability and kind versions those copies came from (nothing
gates on it). The same block is embedded in the provider build artifact.

:func:`evaluate_declared_operations` is the registration law: every reference
resolves inside ``declares``, the block carries exactly what the operations
reach, carried layouts are sound, no operation serves a ``workspace.``
capability, which only core's builtin serves, and no operation's result
Contract is itself a Capture: a Capture is always a named ``capture<K>`` field.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.capabilities import (
    RESERVED_CAPABILITY_PREFIX,
    is_reserved_capability_name,
    validate_capability_name,
)
from cruxible_client.contracts.capture_layouts import (
    LAYOUT_REF_UNRESOLVED,
    LAYOUT_ROW_UNRESOLVED,
    DeclaredCaptureKind,
    capture_kinds_of,
    evaluate_layout_law,
    layout_rule_types,
)
from cruxible_client.contracts.law_problems import LawProblem
from cruxible_client.contracts.provider_operations import (
    ContractLookup,
    OperationEffect,
    OperationRefusal,
    OperationSignature,
)
from cruxible_client.contracts.shapes import (
    Contract,
    ContractRef,
    all_field_refs,
    contract_digest,
    parse_contract_ref,
    type_refs,
)

REGISTRATION_SCHEMA_VERSION = 3
#: Layout law codes the registration closure already judges inside ``declares``.
_RESOLUTION_CODES = frozenset({LAYOUT_ROW_UNRESOLVED, LAYOUT_REF_UNRESOLVED})
#: The most capability names one operation serves.
MAX_SERVES = 16
MAX_OPERATIONS = 256
MAX_DECLARED_CONTRACTS = 1024
MAX_DECLARED_CAPTURE_KINDS = 256
MAX_BUILT_AGAINST = 512
MAX_OPERATION_DESCRIPTION = 2048

_OPERATION_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_REFUSAL_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_KIND_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_REPOSITORY_RE = re.compile(r"^https://[^\s]{1,503}$")
_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

PROVIDER_CAPABILITY_RESERVED: Final = "cruxible.provider.capability_reserved"
PROVIDER_DECLARED_CONTRACT_MISSING: Final = "cruxible.provider.declared_contract_missing"
PROVIDER_DECLARED_CONTRACT_MISMATCH: Final = "cruxible.provider.declared_contract_mismatch"
PROVIDER_DECLARED_CONTRACT_UNPINNED: Final = "cruxible.provider.declared_contract_unpinned"
PROVIDER_DECLARED_CAPTURE_KIND_MISSING: Final = "cruxible.provider.declared_capture_kind_missing"
PROVIDER_DECLARED_UNREACHED: Final = "cruxible.provider.declared_unreached"
PROVIDER_RESULT_IS_CAPTURE: Final = "cruxible.provider.result_is_capture"

#: Every code the registration law returns itself (a carried layout's problems keep
#: their own ``cruxible.capture_kind.*`` codes).
RegistrationLawCode = Literal[
    "cruxible.provider.capability_reserved",
    "cruxible.provider.declared_contract_missing",
    "cruxible.provider.declared_contract_mismatch",
    "cruxible.provider.declared_contract_unpinned",
    "cruxible.provider.declared_capture_kind_missing",
    "cruxible.provider.declared_unreached",
    "cruxible.provider.result_is_capture",
]


def capability_reserved_message(capability: str) -> str:
    """``cruxible.provider.capability_reserved``: the capability is core's to serve."""

    return f"`{capability}` is served by core."


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


def _not_none(value: object) -> bool:
    return value is None


def _empty(value: object) -> bool:
    return not value


def _utf8(value: str) -> bytes:
    return value.encode("utf-8")


def _relative_path(value: str, *, label: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
        or value == "."
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be a normalized relative POSIX path")
    return value


def _callable_path(value: str) -> str:
    module, separator, member = value.partition(":")
    if (
        not separator
        or not member.isidentifier()
        or not all(part.isidentifier() for part in module.split("."))
    ):
        raise ValueError("an entrypoint is module:callable")
    return value


def _contract_ref(value: object) -> object:
    """Authors write ``name@N``; the structured form is accepted as is."""

    if isinstance(value, str):
        return parse_contract_ref(value)
    return value


class RegistrationResource(_Strict):
    """A package file the registration names, by package-relative path and sha256."""

    path: str = Field(max_length=512)
    digest: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _relative_path(value, label="a resource path")

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError("a resource digest is sha256:<64 lowercase hex>")
        return value


class RegistrationRuntimeRequirement(_Strict):
    """One thing an entrypoint needs at run: a Python extra, or a resource with a probe."""

    entrypoint: str
    kind: Literal["python_extra", "runtime_resource"]
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=2048)
    probe_entrypoint: str | None = None

    _entrypoint = field_validator("entrypoint")(_callable_path)

    @model_validator(mode="after")
    def _probe(self) -> RegistrationRuntimeRequirement:
        if self.kind == "runtime_resource":
            if self.probe_entrypoint is None:
                raise ValueError("runtime resources require a package readiness probe")
            _callable_path(self.probe_entrypoint)
        elif self.probe_entrypoint is not None:
            raise ValueError("Python extras do not use resource probes")
        return self


class OperationSource(_Strict):
    """Where an operation's code is.

    ``path`` is relative to the root of ``repository`` (or, for a local build
    with no repository, to the provider's project directory). ``revision`` is
    the full commit id the build was made from, and needs ``repository``.
    """

    path: str = Field(max_length=512)
    repository: str | None = Field(default=None, exclude_if=_not_none)
    revision: str | None = Field(default=None, exclude_if=_not_none)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _relative_path(value, label="an operation source path")

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str | None) -> str | None:
        if value is not None and not _REPOSITORY_RE.fullmatch(value):
            raise ValueError("an operation source repository is an https URL")
        return value

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: str | None) -> str | None:
        if value is not None and not _REVISION_RE.fullmatch(value):
            raise ValueError("an operation source revision is a full lowercase commit id")
        return value

    @model_validator(mode="after")
    def _repository_for_revision(self) -> OperationSource:
        if self.revision is not None and self.repository is None:
            raise ValueError("an operation source revision names its repository")
        return self


class DeclaredOperation(_Strict):
    """One operation of a provider package (registration schema 3) and of its build.

    ``in``, ``out`` and every refusal Contract name a Contract the package
    carries in ``declares``. ``serves`` lists capability names, sorted, at most
    sixteen: fit is checked against whichever version a slot pins.
    ``may_truncate``: the operation may return a truncated listing, so it binds
    only a slot that declares ``may_truncate`` too.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        json_schema_mode_override="validation",
    )

    name: str
    description: str = Field(min_length=1, max_length=MAX_OPERATION_DESCRIPTION)
    source: OperationSource
    serves: tuple[str, ...] = Field(min_length=1, max_length=MAX_SERVES)
    in_: ContractRef = Field(alias="in")
    out: ContractRef
    effect: OperationEffect
    may_truncate: bool = Field(default=False, exclude_if=lambda value: value is False)
    refusals: dict[str, OperationRefusal] = Field(default_factory=dict, exclude_if=_empty)
    entrypoint: str

    _contracts = field_validator("in_", "out", mode="before")(_contract_ref)
    _entrypoint = field_validator("entrypoint")(_callable_path)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _OPERATION_NAME_RE.fullmatch(value):
            raise ValueError("an operation name is a canonical lowercase identifier")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value or value != value.strip():
            raise ValueError("an operation description is trimmed NFC text")
        return value

    @field_validator("serves")
    @classmethod
    def _serves(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            validate_capability_name(name)
        if value != tuple(sorted(set(value), key=_utf8)):
            raise ValueError("serves lists capability names sorted and once each")
        return value

    @field_validator("refusals")
    @classmethod
    def _refusals(cls, value: dict[str, OperationRefusal]) -> dict[str, OperationRefusal]:
        for code in value:
            if not _REFUSAL_CODE_RE.fullmatch(code):
                raise ValueError(f"refusal code {code!r} is not a canonical identifier")
        return dict(sorted(value.items(), key=lambda item: _utf8(item[0])))

    def contract_refs(self) -> tuple[ContractRef, ...]:
        """Every Contract this operation names: in, out, then refusals by code."""

        return (self.in_, self.out, *(item.contract for item in self.refusals.values()))

    def signature(self) -> OperationSignature:
        """The operation's own ``{in, out, effect}``: what its receipts sign, once pinned."""

        return OperationSignature.model_validate(
            {"in": self.in_, "out": self.out, "effect": self.effect}
        )


class BuiltAgainstCapability(_Strict):
    """One capability version the package's copies were made from."""

    kind: Literal["Capability"] = "Capability"
    name: str
    version: int = Field(ge=1, le=999_999_999)
    digest: str

    _name = field_validator("name")(validate_capability_name)

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError("a built_against digest is a sha256 digest")
        return value


class BuiltAgainstCaptureKind(_Strict):
    """One CaptureKind a carried copy was made from, by the digest of its declared copy
    (``declared_capture_kind_digest``: name, decoding, layout)."""

    kind: Literal["CaptureKind"] = "CaptureKind"
    name: str
    digest: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _KIND_NAME_RE.fullmatch(value):
            raise ValueError("a CaptureKind name is a canonical identifier")
        return value

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return BuiltAgainstCapability._digest(value)


BuiltAgainst: TypeAlias = Annotated[
    BuiltAgainstCapability | BuiltAgainstCaptureKind, Field(discriminator="kind")
]


def _built_against_key(item: BuiltAgainstCapability | BuiltAgainstCaptureKind) -> tuple[bytes, ...]:
    version = item.version if isinstance(item, BuiltAgainstCapability) else 0
    return (_utf8(item.kind), _utf8(item.name), version.to_bytes(4, "big"))


class ProviderDeclares(_Strict):
    """The shapes a package carries for its operations: never installed as instance state.

    ``contracts`` are sorted by name then version; ``capture_kinds`` by name;
    ``built_against`` by kind, name, then version. Each is listed once.
    """

    contracts: tuple[Contract, ...] = Field(min_length=1, max_length=MAX_DECLARED_CONTRACTS)
    capture_kinds: tuple[DeclaredCaptureKind, ...] = Field(
        default=(), max_length=MAX_DECLARED_CAPTURE_KINDS, exclude_if=_empty
    )
    built_against: tuple[BuiltAgainst, ...] = Field(
        default=(), max_length=MAX_BUILT_AGAINST, exclude_if=_empty
    )

    @field_validator("contracts")
    @classmethod
    def _contracts(cls, value: tuple[Contract, ...]) -> tuple[Contract, ...]:
        keys = [(_utf8(item.name), item.version) for item in value]
        if keys != sorted(set(keys)):
            raise ValueError("declared Contracts are sorted by name and version, once each")
        return value

    @field_validator("capture_kinds")
    @classmethod
    def _capture_kinds(
        cls, value: tuple[DeclaredCaptureKind, ...]
    ) -> tuple[DeclaredCaptureKind, ...]:
        names = [_utf8(item.name) for item in value]
        if names != sorted(set(names)):
            raise ValueError("declared CaptureKinds are sorted by name, once each")
        return value

    @field_validator("built_against")
    @classmethod
    def _built_against(
        cls, value: tuple[BuiltAgainstCapability | BuiltAgainstCaptureKind, ...]
    ) -> tuple[BuiltAgainstCapability | BuiltAgainstCaptureKind, ...]:
        keys = [_built_against_key(item) for item in value]
        if keys != sorted(set(keys)):
            raise ValueError("built_against is sorted by kind, name and version, once each")
        return value

    def lookup(self) -> ContractLookup:
        """A resolver over the carried Contracts: a pin with a digest resolves only to it."""

        by_key = {(item.name, item.version): item for item in self.contracts}

        def resolve(ref: ContractRef) -> Contract | None:
            found = by_key.get((ref.name, ref.version))
            if found is None:
                return None
            if ref.digest is not None and contract_digest(found).tagged != ref.digest:
                return None
            return found

        return resolve

    def capture_kind(self, name: str) -> DeclaredCaptureKind | None:
        return next((item for item in self.capture_kinds if item.name == name), None)


class PackageRegistration(_Strict):
    """``registration.json`` schema 3, exactly as a package ships it.

    ``manifest`` names the package's runtime manifest (schema 2) by path and
    digest; ``operations`` are sorted by name, once each; ``declares`` carries
    every shape they reach.
    """

    schema_version: Literal[3]
    manifest: RegistrationResource
    operations: tuple[DeclaredOperation, ...] = Field(min_length=1, max_length=MAX_OPERATIONS)
    declares: ProviderDeclares
    runtime_requirements: tuple[RegistrationRuntimeRequirement, ...] = ()

    @field_validator("operations")
    @classmethod
    def _operations(cls, value: tuple[DeclaredOperation, ...]) -> tuple[DeclaredOperation, ...]:
        names = [_utf8(item.name) for item in value]
        if names != sorted(set(names)):
            raise ValueError("operations are sorted by name, once each")
        return value


# -- the registration law ------------------------------------------------------------------


def spells_capture_wire(contract: Contract) -> bool:
    """Whether ``contract`` spells core's Capture wire as its own fields.

    That is a result Contract that is itself a Capture: it declares every
    required field of the wire a provider returns a Capture in
    (``ProviderResultToExternalCapture``). A Capture is a named ``capture<K>``
    field of a result Contract instead, and its wire travels in that field.
    """

    from cruxible_client.contracts.captures import ProviderResultToExternalCapture

    required = {
        name
        for name, info in ProviderResultToExternalCapture.model_fields.items()
        if info.is_required()
    }
    return required <= set(contract.fields)


def evaluate_declared_operations(
    operations: Sequence[DeclaredOperation], declares: ProviderDeclares
) -> tuple[LawProblem, ...]:
    """Judge a package's operations against the shapes it carries.

    No operation serves a ``workspace.`` capability. Every ``in``, ``out`` and
    refusal Contract resolves inside ``declares`` (to the carried digest when
    the reference pins one); every carried Contract pins its nested Contracts by
    digest and they resolve inside ``declares``; every ``capture<K>`` the reached
    Contracts name is a carried CaptureKind, whose layout's rows and field-rule
    types are reached in turn (their Contracts and named kinds, transitively);
    carried layouts pass the layout law; no operation's ``out`` Contract spells
    the Capture wire (:func:`spells_capture_wire`); and ``declares`` carries
    nothing the operations do not reach. ``built_against`` is a record and is
    not judged.
    """

    problems: list[LawProblem] = []
    resolve = declares.lookup()
    carried = {(item.name, item.version): item for item in declares.contracts}
    digests = {key: contract_digest(item).tagged for key, item in carried.items()}

    for operation in operations:
        for name in operation.serves:
            if is_reserved_capability_name(name):
                problems.append(
                    LawProblem(
                        PROVIDER_CAPABILITY_RESERVED,
                        f"operation `{operation.name}` serves `{name}`: "
                        + capability_reserved_message(name)
                        + f" Capabilities under {RESERVED_CAPABILITY_PREFIX} are core's.",
                    )
                )

    def check(ref: ContractRef, *, owner: str) -> Contract | None:
        found = carried.get((ref.name, ref.version))
        if found is None:
            problems.append(
                LawProblem(
                    PROVIDER_DECLARED_CONTRACT_MISSING,
                    f"{owner} names {ref.spelling}, which declares does not carry.",
                )
            )
            return None
        digest = digests[(ref.name, ref.version)]
        if ref.digest is not None and ref.digest != digest:
            problems.append(
                LawProblem(
                    PROVIDER_DECLARED_CONTRACT_MISMATCH,
                    f"{owner} pins {ref.spelling} at {ref.digest}; the carried copy is {digest}.",
                )
            )
        return found

    # Reach from the operations; a named, carried kind's layout (its rows and
    # the types of its field rules) is reached too, and may name further
    # Contracts and kinds, until nothing new is reached.
    carried_kinds = {item.name: item for item in declares.capture_kinds}
    reached: dict[tuple[str, int], Contract] = {}
    named_kinds: dict[str, str] = {}
    pending: deque[tuple[ContractRef, str]] = deque()

    def reach_kind(kind_name: str, where: str) -> None:
        if kind_name in named_kinds:
            return
        named_kinds[kind_name] = where
        kind = carried_kinds.get(kind_name)
        if kind is None or kind.layout is None:
            return
        owner = f"CaptureKind `{kind_name}`"
        pending.extend((row, owner) for row in kind.layout.row_refs())
        for entry, field_type in layout_rule_types(kind.layout):
            pending.extend((ref, f"{owner} {entry}") for ref in type_refs(field_type))
            for nested_kind in capture_kinds_of(field_type):
                reach_kind(nested_kind, f"{owner} {entry}")

    for operation in operations:
        pending.extend((ref, f"operation `{operation.name}`") for ref in operation.contract_refs())
        out = carried.get((operation.out.name, operation.out.version))
        if out is not None and spells_capture_wire(out):
            problems.append(
                LawProblem(
                    PROVIDER_RESULT_IS_CAPTURE,
                    f"operation `{operation.name}` returns {operation.out.spelling}, which "
                    "spells a Capture's wire as its own fields; a Capture is a named field of "
                    "the result Contract: declare it as `capture: capture<*>` (or "
                    "capture<K>) and return the Capture in that field.",
                )
            )
    while pending:
        ref, owner = pending.popleft()
        found = check(ref, owner=owner)
        if found is None or (found.name, found.version) in reached:
            continue
        reached[(found.name, found.version)] = found
        nested_owner = f"carried Contract {found.ref.spelling}"
        for nested in all_field_refs(found):
            if nested.digest is None:
                problems.append(
                    LawProblem(
                        PROVIDER_DECLARED_CONTRACT_UNPINNED,
                        f"{nested_owner} names {nested.spelling} without its digest; a carried "
                        "copy pins every nested Contract by digest.",
                    )
                )
            pending.append((nested, nested_owner))
        for field_name, declared in found.fields.items():
            for kind_name in capture_kinds_of(declared.type):
                reach_kind(kind_name, f"{found.ref.spelling}.{field_name}")

    for kind_name, where in sorted(named_kinds.items()):
        if kind_name not in carried_kinds:
            problems.append(
                LawProblem(
                    PROVIDER_DECLARED_CAPTURE_KIND_MISSING,
                    f"{where} is capture<{kind_name}>; declares carries no copy of "
                    f"CaptureKind `{kind_name}` (name it capture<*> to promise no layout).",
                )
            )
    for kind in declares.capture_kinds:
        if kind.name not in named_kinds:
            problems.append(
                LawProblem(
                    PROVIDER_DECLARED_UNREACHED,
                    f"declares carries CaptureKind `{kind.name}`, which no operation names.",
                )
            )
        if kind.layout is not None:
            # Resolution inside declares is judged by the closure above, with its
            # owner; the layout law adds what it alone knows.
            problems.extend(
                LawProblem(problem.code, f"carried CaptureKind `{kind.name}`: {problem.message}")
                for problem in evaluate_layout_law(
                    kind.layout, decoding=kind.decoding, resolve=resolve
                )
                if problem.code not in _RESOLUTION_CODES
            )
    for key, contract in carried.items():
        if key not in reached:
            problems.append(
                LawProblem(
                    PROVIDER_DECLARED_UNREACHED,
                    f"declares carries Contract {contract.ref.spelling}, which no operation, "
                    "and no layout of a kind they name, reaches.",
                )
            )
    return tuple(dict.fromkeys(problems))


def evaluate_registration_law(registration: PackageRegistration) -> tuple[LawProblem, ...]:
    """The registration law over one ``registration.json`` (schema 3)."""

    return evaluate_declared_operations(registration.operations, registration.declares)


__all__ = [
    "MAX_BUILT_AGAINST",
    "MAX_DECLARED_CAPTURE_KINDS",
    "MAX_DECLARED_CONTRACTS",
    "MAX_OPERATIONS",
    "MAX_OPERATION_DESCRIPTION",
    "MAX_SERVES",
    "PROVIDER_CAPABILITY_RESERVED",
    "PROVIDER_DECLARED_CAPTURE_KIND_MISSING",
    "PROVIDER_DECLARED_CONTRACT_MISMATCH",
    "PROVIDER_DECLARED_CONTRACT_MISSING",
    "PROVIDER_DECLARED_CONTRACT_UNPINNED",
    "PROVIDER_DECLARED_UNREACHED",
    "PROVIDER_RESULT_IS_CAPTURE",
    "REGISTRATION_SCHEMA_VERSION",
    "BuiltAgainst",
    "BuiltAgainstCapability",
    "BuiltAgainstCaptureKind",
    "DeclaredOperation",
    "OperationSource",
    "PackageRegistration",
    "ProviderDeclares",
    "RegistrationLawCode",
    "RegistrationResource",
    "RegistrationRuntimeRequirement",
    "capability_reserved_message",
    "evaluate_declared_operations",
    "evaluate_registration_law",
    "spells_capture_wire",
]
