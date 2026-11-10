"""Typed provider operations: what one provider build serves, by Contract signature.

A provider build declares ``operations``. Each operation names its input and
output Contracts (``name@N``, pinned to the exact accepted digest when the
build is accepted), its effect, the typed refusals it may return, an optional
enumeration declaration (recorded, never verified), and the package
entrypoint that serves it.

A Procedure never names a provider. Its calling nodes name a **slot**, and the
Procedure declares each slot's signature ``{in, out, effect}``. A Line binds
every slot to one exact ``{build, operation}`` when it is accepted; runs use
that binding as given. The signature index maps ``(in, out, effect)`` to the
live ``(build, operation)`` pairs that serve it, so fitting a slot is one
lookup.

``validate_operation_value`` checks one value against an operation Contract:
the value must be exactly the Contract's shape (no undeclared keys, every
required field present, every present field valid). It raises
:class:`OperationValueRefused` naming every problem; it never coerces.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.shapes import (
    CaptureType,
    Contract,
    ContractRef,
    EnumResolver,
    FieldInvalid,
    FieldResultType,
    FieldType,
    KeyCanonicalizer,
    ListType,
    NestedType,
    NumberType,
    RecordType,
    RefusalType,
    ResultType,
    VariantType,
    canonical_number,
    check_value,
    contract_digest,
    parse_contract_ref,
)

OperationEffect = Literal["pure", "external_read", "external_mutation"]
OPERATION_EFFECTS: tuple[OperationEffect, ...] = ("pure", "external_read", "external_mutation")
OPERATION_SIGNATURE_DOMAIN = "cruxible-operation-signature-v1"

_OPERATION_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_SLOT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REFUSAL_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _contract_ref(value: object) -> object:
    """Authors write ``name@N``; the structured form is accepted as is."""

    if isinstance(value, str):
        return parse_contract_ref(value)
    return value


def _entrypoint(value: str) -> str:
    module, separator, object_name = value.partition(":")
    if not separator or not module or not object_name:
        raise ValueError("an operation entrypoint is module:object")
    return value


class OperationRefusal(_Strict):
    """One typed refusal an operation may return.

    ``absorbable`` declares that the refusal means "this input is unavailable",
    so a Line acquisition rule with ``on_unavailable: omit`` may absorb it
    instead of failing the run.
    """

    contract: ContractRef
    absorbable: bool = False

    _contract = field_validator("contract", mode="before")(_contract_ref)


class EnumeratedField(_Strict):
    at: str = Field(min_length=1, max_length=512)


class OperationEnumeration(_Strict):
    """What a provider-parsed operation enumerates. Declared and recorded, not verified."""

    capture: str
    selector: str = Field(min_length=1, max_length=512)
    fields: dict[str, EnumeratedField] = Field(min_length=1, max_length=256)

    @field_validator("capture")
    @classmethod
    def _capture(cls, value: str) -> str:
        if not _FIELD_RE.fullmatch(value):
            raise ValueError("an enumeration names one input field holding its Capture")
        return value

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: dict[str, EnumeratedField]) -> dict[str, EnumeratedField]:
        for name in value:
            if not _FIELD_RE.fullmatch(name):
                raise ValueError(f"enumerated field {name!r} is not a canonical field name")
        return value


class ProviderOperation(_Strict):
    """One operation of a provider build."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )

    name: str
    in_: ContractRef = Field(alias="in")
    out: ContractRef
    effect: OperationEffect
    refusals: dict[str, OperationRefusal] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    enumerates: OperationEnumeration | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    entrypoint: str

    _contracts = field_validator("in_", "out", mode="before")(_contract_ref)
    _entrypoint = field_validator("entrypoint")(_entrypoint)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _OPERATION_NAME_RE.fullmatch(value):
            raise ValueError("an operation name is a canonical lowercase identifier")
        return value

    @field_validator("refusals")
    @classmethod
    def _refusals(cls, value: dict[str, OperationRefusal]) -> dict[str, OperationRefusal]:
        for code in value:
            if not _REFUSAL_CODE_RE.fullmatch(code):
                raise ValueError(f"refusal code {code!r} is not a canonical identifier")
        return dict(sorted(value.items()))

    def contract_refs(self) -> tuple[ContractRef, ...]:
        """Every Contract this operation pins: in, out, then refusals by code."""

        return (self.in_, self.out, *(item.contract for item in self.refusals.values()))

    @property
    def pinned(self) -> bool:
        return all(ref.digest is not None for ref in self.contract_refs())

    def signature(self) -> OperationSignature:
        return OperationSignature.model_validate(
            {"in": self.in_, "out": self.out, "effect": self.effect}
        )


class OperationSignature(_Strict):
    """``{in, out, effect}``: what a slot asks for and an operation serves."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )

    in_: ContractRef = Field(alias="in")
    out: ContractRef
    effect: OperationEffect

    _contracts = field_validator("in_", "out", mode="before")(_contract_ref)

    def key(self) -> tuple[str, str, str]:
        """The index key: exact in and out digests and the effect."""

        if self.in_.digest is None or self.out.digest is None:
            raise ValueError("an operation signature is keyed by exact Contract digests")
        return (self.in_.digest, self.out.digest, self.effect)

    def same_as(self, other: OperationSignature) -> bool:
        return self.key() == other.key()

    def spelling(self) -> str:
        return f"{self.in_.spelling} -> {self.out.spelling} ({self.effect})"


def operation_signature_digest(signature: OperationSignature) -> str:
    in_digest, out_digest, effect = signature.key()
    return typed_digest(
        ArtifactDigest,
        OPERATION_SIGNATURE_DOMAIN,
        {"in": in_digest, "out": out_digest, "effect": effect},
    ).tagged


def validate_operations(operations: tuple[ProviderOperation, ...]) -> tuple[ProviderOperation, ...]:
    """Operations are sorted by name and unique."""

    names = tuple(item.name for item in operations)
    if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
        raise ValueError("provider operations must be sorted by name and unique")
    return operations


class ProcedureSlot(OperationSignature):
    """One binding point a Procedure declares: the signature its operation must serve."""

    may_truncate: bool = False


def validate_slot_name(value: str) -> str:
    if not _SLOT_NAME_RE.fullmatch(value):
        raise ValueError(f"slot name {value!r} is not a canonical identifier")
    return value


class LineBinding(_Strict):
    """One slot's binding in a Line: an exact provider build and one of its operations."""

    build: ArtifactPin
    operation: str

    @field_validator("operation")
    @classmethod
    def _operation(cls, value: str) -> str:
        if not _OPERATION_NAME_RE.fullmatch(value):
            raise ValueError("a binding names one canonical operation")
        return value

    @model_validator(mode="after")
    def _build(self) -> LineBinding:
        if self.build.role != "provider" or self.build.target.kind != "Provider":
            raise ValueError("a Line binding pins one Provider build with role provider")
        return self

    @property
    def spelling(self) -> str:
        return f"{self.build.target.name}#{self.operation}"


def binding_pin(build: ArtifactIdentity, digest: str) -> ArtifactPin:
    return ArtifactPin(role="provider", target=build, artifact_digest=digest)


# -- values ------------------------------------------------------------------------------


class OperationValueRefused(ValueError):
    """A value is not exactly the shape of an operation Contract."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems) if problems else "value does not match its Contract")


ContractLookup = Callable[[ContractRef], Contract | None]


def _typed_members(
    value: object, field_type: FieldType, path: str, resolve: ContractLookup
) -> Iterable[tuple[object, FieldType, str]]:
    """Visit this value and its declared closure, including typed envelopes."""
    yield value, field_type, path
    if isinstance(field_type, ListType) and isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _typed_members(item, field_type.item, f"{path}[{index}]", resolve)
        return
    if not isinstance(value, Mapping):
        return
    if isinstance(field_type, FieldResultType):
        if value.get("tag") == "present":
            yield from _typed_members(
                value.get("value"), field_type.value, f"{path}.value", resolve
            )
        return
    if isinstance(field_type, ResultType):
        if value.get("tag") == "record":
            yield from _typed_members(
                value.get("record"),
                RecordType(contract=field_type.contract),
                f"{path}.record",
                resolve,
            )
        return
    if isinstance(field_type, NestedType | RecordType | RefusalType | VariantType):
        if isinstance(field_type, VariantType):
            tag = value.get("tag")
            ref = field_type.arms.get(tag) if isinstance(tag, str) else None
        else:
            ref = field_type.contract
        target = None if ref is None else resolve(ref)
        if target is None:
            return
        if isinstance(field_type, RecordType):
            fields = value.get("fields")
            if isinstance(fields, Mapping):
                for name, declared in target.fields.items():
                    if name in fields:
                        yield from _typed_members(
                            fields[name],
                            FieldResultType(value=declared.type),
                            f"{path}.fields.{name}",
                            resolve,
                        )
            return
        if isinstance(field_type, RefusalType):
            value = value.get("body")
            path = f"{path}.body"
            if not isinstance(value, Mapping):
                return
        for name, declared in target.fields.items():
            if name in value:
                yield from _typed_members(value[name], declared.type, f"{path}.{name}", resolve)


def _number_encoding_problem(value: object, field_type: FieldType, path: str) -> str | None:
    if isinstance(field_type, NumberType):
        try:
            canonical = canonical_number(value)
        except ValueError:
            return None  # The ordinary type checker supplies the mismatch.
        if type(value) is not type(canonical) or value != canonical:
            return f"{path}: noncanonical number; use {canonical!r}"
    return None


def validate_typed_number_encoding(
    value: object, field_type: FieldType, *, resolve: ContractLookup, path: str
) -> None:
    """Refuse noncanonical number wire forms before a typed value is stored or hashed.

    The scalar checker can normalize numbers for callers that request conversion;
    wire boundaries require the canonical int-or-decimal-text representation.
    """
    problems = [
        problem
        for member, declared, member_path in _typed_members(value, field_type, path, resolve)
        if (problem := _number_encoding_problem(member, declared, member_path)) is not None
    ]
    if problems:
        raise OperationValueRefused(tuple(problems))


def validate_operation_value(
    value: object,
    contract: Contract,
    *,
    resolve: ContractLookup,
    enum_members: EnumResolver | None = None,
    max_items: int | None = None,
    observe_items: Callable[[int, str], None] | None = None,
    allow_captures: bool = True,
    keys: KeyCanonicalizer | None = None,
) -> dict[str, object]:
    """``value`` exactly as ``contract`` declares it, or :class:`OperationValueRefused`.

    Undeclared keys refuse; a missing required field refuses; every present
    field is checked by the Contract type grammar (a ``key<K>`` field through
    ``keys``, core's SubjectKind identity rules: it states its canonical key).
    Nested Contracts are closed the same way. List lengths are reported to
    ``observe_items`` and bounded by ``max_items`` when given. The value is
    returned unchanged (never coerced).
    """

    if not isinstance(value, Mapping):
        raise OperationValueRefused(("the value is not a record",))
    problems: list[str] = []
    _closed(value, contract, resolve=resolve, path="", problems=problems)
    for name, declared in contract.fields.items():
        if name not in value:
            if not declared.optional:
                problems.append(f"{name}: absent_required")
            continue
        if not allow_captures and isinstance(declared.type, CaptureType):
            problems.append(f"{name}: a Capture is produced by core, not returned")
            continue
        checked = check_value(
            value[name],
            declared.type,
            at=f"/{name}",
            field=name,
            resolve=resolve,
            enum_members=enum_members,
            keys=keys,
        )
        if isinstance(checked, FieldInvalid):
            problems.append(f"{checked.problem.field or name}: {checked.problem.code}")
            continue
        for member, member_type, path in _typed_members(value[name], declared.type, name, resolve):
            problem = _number_encoding_problem(member, member_type, path)
            if problem is not None:
                problems.append(problem)
            if not isinstance(member_type, ListType) or not isinstance(member, list | tuple):
                continue
            count = len(member)
            if observe_items is not None:
                observe_items(count, path)
            if max_items is not None and count > max_items:
                raise OperationItemBudgetExceeded(limit=max_items, observed=count, field_path=path)
    if problems:
        raise OperationValueRefused(tuple(problems))
    return dict(value)


def _closed(
    value: Mapping[str, object],
    contract: Contract,
    *,
    resolve: ContractLookup,
    path: str,
    problems: list[str],
) -> None:
    for key in value:
        if key not in contract.fields:
            problems.append(f"{path}{key}: undeclared field")
    for name, declared in contract.fields.items():
        _close_member(
            value.get(name), declared.type, resolve=resolve, path=f"{path}{name}", problems=problems
        )


def _close_member(
    value: object,
    field_type: FieldType,
    *,
    resolve: ContractLookup,
    path: str,
    problems: list[str],
) -> None:
    """Close every record ``field_type`` reaches, at any depth of wrapping.

    A list wraps to any depth (``list<list<Contract>>``), and a variant's arm
    is one Contract chosen by its tag; closure traverses both, so a record
    nested inside either can never carry an undeclared key past this check.
    """

    if isinstance(field_type, ListType):
        if isinstance(value, list | tuple):
            for index, item in enumerate(value):
                _close_member(
                    item,
                    field_type.item,
                    resolve=resolve,
                    path=f"{path}[{index}]",
                    problems=problems,
                )
        return
    if isinstance(field_type, NestedType):
        if isinstance(value, Mapping):
            target = resolve(field_type.contract)
            if target is not None:
                _closed(value, target, resolve=resolve, path=f"{path}.", problems=problems)
        return
    if isinstance(field_type, VariantType):
        if isinstance(value, Mapping):
            tag = value.get("tag")
            ref = field_type.arms.get(tag) if isinstance(tag, str) else None
            if ref is not None:
                target = resolve(ref)
                if target is not None:
                    # "tag" selects the arm; it is not one of the arm
                    # Contract's own fields (matches check_value's variant
                    # body, which strips it the same way).
                    body = {key: item for key, item in value.items() if key != "tag"}
                    _closed(body, target, resolve=resolve, path=f"{path}.", problems=problems)
        return
    if isinstance(field_type, FieldResultType):
        # field_result<T> = variant{present: {value: T, at}, absent: {}, invalid: {problem}}.
        # The envelope's own keys close by its tag's arm (F06); only
        # "present" also carries a value to close further, at ``field_type.value``.
        if isinstance(value, Mapping):
            # F06b: the tag is an unchecked discriminator here, before
            # ``check_value`` ever validates it -- canonical JSON permits a
            # list or object value, neither hashable, so the lookup key is
            # checked first and anything else is simply never an arm (no
            # closure performed; ``check_value`` refuses it as usual).
            tag = value.get("tag")
            allowed = _FIELD_RESULT_ENVELOPE_KEYS.get(tag) if isinstance(tag, str) else None
            if allowed is not None:
                _close_envelope_keys(value, allowed, path=path, problems=problems)
            if value.get("tag") == "present" and "value" in value:
                _close_member(
                    value["value"],
                    field_type.value,
                    resolve=resolve,
                    path=f"{path}.value",
                    problems=problems,
                )
        return
    if isinstance(field_type, RecordType):
        # record<C> = {record, key, fields: {f: field_result<C.f>}}.
        if isinstance(value, Mapping):
            target = resolve(field_type.contract)
            if target is not None:
                _close_record_envelope(value, target, resolve=resolve, path=path, problems=problems)
        return
    if isinstance(field_type, ResultType):
        # result<C> = variant{record: record<C>, invalid_record, duplicate};
        # only the "record" arm carries a record<C> envelope to close (the
        # other two arms are closed strict pydantic models, extra="forbid").
        if isinstance(value, Mapping) and value.get("tag") == "record":
            _close_envelope_keys(value, _RESULT_RECORD_ARM_KEYS, path=path, problems=problems)
            target = resolve(field_type.contract)
            record_value = value.get("record")
            if target is not None and isinstance(record_value, Mapping):
                _close_record_envelope(
                    record_value,
                    target,
                    resolve=resolve,
                    path=f"{path}.record",
                    problems=problems,
                )
        return
    if isinstance(field_type, RefusalType):
        # refusal<C> = {code, contract, body}; the body is a plain record of C
        # (not field_result-wrapped), exactly like a nested Contract's fields.
        if isinstance(value, Mapping):
            _close_envelope_keys(value, _REFUSAL_ENVELOPE_KEYS, path=path, problems=problems)
            # F06a: the typed Contract reference (name, version, digest) is
            # itself a fixed-shape wrapper member, closed the same way --
            # shapes._check_refusal reads these three keys individually and
            # never validates the mapping through the strict ContractRef model.
            contract_ref_value = value.get("contract")
            if isinstance(contract_ref_value, Mapping):
                _close_envelope_keys(
                    contract_ref_value,
                    _REFUSAL_CONTRACT_REF_KEYS,
                    path=f"{path}.contract",
                    problems=problems,
                )
            target = resolve(field_type.contract)
            refusal_body = value.get("body")
            if target is not None and isinstance(refusal_body, Mapping):
                _closed(
                    refusal_body, target, resolve=resolve, path=f"{path}.body.", problems=problems
                )
        return


#: field_result<T>'s own wire keys, per arm (F06: the envelope closes too).
_FIELD_RESULT_ENVELOPE_KEYS: dict[str | None, frozenset[str]] = {
    "present": frozenset({"tag", "value", "at"}),
    "absent": frozenset({"tag"}),
    "invalid": frozenset({"tag", "problem"}),
}
#: result<C>'s "record" arm's own wire keys (the record<C> envelope nested at
#: "record" closes separately, via ``_close_record_envelope``).
_RESULT_RECORD_ARM_KEYS: frozenset[str] = frozenset({"tag", "record"})
#: refusal<C>'s own wire keys.
_REFUSAL_ENVELOPE_KEYS: frozenset[str] = frozenset({"code", "contract", "body"})
#: refusal<C>'s own typed Contract reference's wire keys (F06a).
_REFUSAL_CONTRACT_REF_KEYS: frozenset[str] = frozenset({"name", "version", "digest"})
#: record<C>'s own wire keys.
_RECORD_ENVELOPE_KEYS: frozenset[str] = frozenset({"record", "key", "fields"})


def _close_envelope_keys(
    value: Mapping[str, object],
    allowed: frozenset[str],
    *,
    path: str,
    problems: list[str],
) -> None:
    """Refuse any key of a fixed-shape wrapper envelope outside ``allowed`` (F06)."""

    for key in value:
        if key not in allowed:
            problems.append(f"{path}.{key}: undeclared field")


def _close_record_envelope(
    value: Mapping[str, object],
    contract: Contract,
    *,
    resolve: ContractLookup,
    path: str,
    problems: list[str],
) -> None:
    """Close a ``record<C>`` envelope: its own keys, and each ``fields`` entry.

    The envelope's own keys (``record``, ``key``, ``fields``) are closed
    first (F06); a ``fields`` entry outside ``contract``'s own fields
    refuses too, and each declared field's present value is closed at its
    own type, same as any other field_result.
    """

    _close_envelope_keys(value, _RECORD_ENVELOPE_KEYS, path=path, problems=problems)
    fields_data = value.get("fields")
    if not isinstance(fields_data, Mapping):
        return
    for key in fields_data:
        if key not in contract.fields:
            problems.append(f"{path}.fields.{key}: undeclared field")
    for name, declared in contract.fields.items():
        if name in fields_data:
            _close_member(
                fields_data[name],
                FieldResultType(value=declared.type),
                resolve=resolve,
                path=f"{path}.fields.{name}",
                problems=problems,
            )


class OperationItemBudgetExceeded(ValueError):
    def __init__(self, *, limit: int, observed: int, field_path: str) -> None:
        self.limit = limit
        self.observed = observed
        self.field_path = field_path
        super().__init__(f"{field_path} holds {observed} items; the run allows {limit}")


def contract_closure(
    roots: Iterable[ContractRef], lookup: ContractLookup
) -> tuple[Contract, ...] | None:
    """Every Contract ``roots`` reach, each once, by exact digest; ``None`` if one is missing."""

    found: dict[str, Contract] = {}
    pending = list(roots)
    while pending:
        ref = pending.pop()
        contract = lookup(ref)
        if contract is None:
            return None
        digest = contract_digest(contract).tagged
        if ref.digest is not None and ref.digest != digest:
            return None
        if digest in found:
            continue
        found[digest] = contract
        pending.extend(contract.refs())
    return tuple(found[key] for key in sorted(found))


def closure_lookup(contracts: Iterable[Contract]) -> ContractLookup:
    """A resolver over one exact closure: a pin resolves only to its own digest."""

    by_name: dict[tuple[str, int], Contract] = {
        (item.name, item.version): item for item in contracts
    }

    def resolve(ref: ContractRef) -> Contract | None:
        contract = by_name.get((ref.name, ref.version))
        if contract is None:
            return None
        if ref.digest is not None and contract_digest(contract).tagged != ref.digest:
            return None
        return contract

    return resolve


def capture_fields(contract: Contract) -> tuple[tuple[str, str], ...]:
    """``(field, CaptureKind)`` for each ``capture<K>`` field of one Contract."""

    return tuple(
        (name, item.type.capture_kind)
        for name, item in contract.fields.items()
        if isinstance(item.type, CaptureType)
    )


def capture_list_fields(contract: Contract) -> tuple[tuple[str, str, int], ...]:
    """``(field, CaptureKind, max_items)`` for each ``list<capture<K>>`` field (D-R37 a1).

    Each member of such a field is its own Capture; the call's ``captures:``
    map binds the kind (``*`` here).
    """

    return tuple(
        (name, item.type.item.capture_kind, item.type.max_items)
        for name, item in contract.fields.items()
        if isinstance(item.type, ListType) and isinstance(item.type.item, CaptureType)
    )


# -- compiler typing views -------------------------------------------------------------


__all__ = [
    "OPERATION_EFFECTS",
    "OPERATION_SIGNATURE_DOMAIN",
    "ContractLookup",
    "EnumeratedField",
    "LineBinding",
    "OperationEffect",
    "OperationEnumeration",
    "OperationItemBudgetExceeded",
    "OperationRefusal",
    "OperationSignature",
    "OperationValueRefused",
    "ProcedureSlot",
    "ProviderOperation",
    "binding_pin",
    "capture_fields",
    "capture_list_fields",
    "closure_lookup",
    "contract_closure",
    "operation_signature_digest",
    "validate_operation_value",
    "validate_typed_number_encoding",
    "validate_operations",
    "validate_slot_name",
]
