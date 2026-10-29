"""Contracts: one data shape, its field-type grammar, typed values and edges.

A Contract is a named, versioned, immutable data shape (``contracts/<name>/v<N>.json``).
Every field has a declared type from one closed grammar. There is no ``json``
type and no untyped map: an opaque blob enters as a Capture. A list always
states ``max_items``. No type names a Claim, a statement, a role, a predicate,
``revises`` or ``retires``: those are decided by core, never carried as data.

This module is a library. It holds the type grammar (structured models and the
author spelling, ``decimal{0..1}``, ``list<int>{max_items: 10}``,
``security.declined@1``), the Contract artifact with its path, digest and
canonical bytes, the runtime value shapes (``field_result``, ``record``,
``result``, ``refusal``), a value validator that never raises on a field
problem, the edge rule :func:`assignable`, explicit casts, and the closed
three-valued predicate evaluator.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    artifact_bytes_for_path,
    artifact_path_matches,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.temporal import format_datetime, parse_datetime

CONTRACT_ARTIFACT_FORMAT = "cruxible-contract-v1"
CONTRACT_DIGEST_DOMAIN = "cruxible-contract-v1"
CONTRACT_PREFIX = "contracts/"
CONTRACT_PIN_ROLE = "contract"
#: Refusal codes a Contract definition can earn.
CONTRACT_TYPE_REFUSED = "contract_type_refused"
CONTRACT_TYPE_INVALID = "contract_type_invalid"

MAX_CONTRACT_FIELDS = 256
MAX_LIST_ITEMS = 1_000_000
MAX_TYPE_DEPTH = 16
MAX_ENUM_MEMBERS = 1024
MAX_VARIANT_ARMS = 64

_CONTRACT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_KIND_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*$")
_ARTIFACT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})+$")
_CONTRACT_PATH_RE = re.compile(r"^contracts/([a-z][a-z0-9_.-]{0,127})/v([1-9][0-9]{0,8})\.json$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Type spellings refused at definition time. ``json`` and untyped maps carry no
#: shape; the others name what only core decides about a Claim.
REFUSED_TYPE_NAMES = frozenset(
    {
        "json",
        "object",
        "map",
        "dict",
        "any",
        "claim",
        "claims",
        "claim_ref",
        "statement",
        "role",
        "predicate",
        "revises",
        "retires",
    }
)


class ContractFormatError(FormatError, ValueError):
    """A Contract artifact, path, or type spelling is invalid.

    Also a ``ValueError``, so a bad spelling inside a model validates as an
    ordinary validation failure that :func:`contract_type_refusal` can read.
    """

    code: str = CONTRACT_TYPE_INVALID


class ContractTypeRefused(ContractFormatError):
    """A field type the grammar refuses at definition time (``contract_type_refused``)."""

    code = CONTRACT_TYPE_REFUSED


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


def _nfc_identifier(value: str, pattern: re.Pattern[str], *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value or not pattern.fullmatch(value):
        raise ValueError(f"{label} {value!r} is not a canonical identifier")
    return value


def _not_none(value: object) -> bool:
    return value is None


# -- the field-type grammar ---------------------------------------------------------


class ContractRef(_Shape):
    """A pin to one Contract version. Authors write ``name@N``; acceptance adds the digest."""

    name: str
    version: int = Field(ge=1, le=999_999_999)
    digest: str | None = Field(default=None, exclude_if=_not_none)

    _name = field_validator("name")(
        lambda value: _nfc_identifier(value, _CONTRACT_NAME_RE, label="Contract name")
    )

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST_RE.fullmatch(value):
            raise ValueError("a Contract pin digest is a sha256 digest")
        return value

    @property
    def identity(self) -> ArtifactIdentity:
        return contract_identity(self.name, self.version)

    @property
    def spelling(self) -> str:
        return f"{self.name}@{self.version}"


class StringType(_Shape):
    kind: Literal["string"] = "string"
    pattern: str | None = Field(default=None, exclude_if=_not_none)
    min_len: int | None = Field(default=None, ge=0, exclude_if=_not_none)
    max_len: int | None = Field(default=None, ge=0, exclude_if=_not_none)

    @model_validator(mode="after")
    def _bounds(self) -> StringType:
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"string pattern does not compile: {exc}") from exc
        if self.min_len is not None and self.max_len is not None and self.min_len > self.max_len:
            raise ValueError("string min_len exceeds max_len")
        return self


class IntType(_Shape):
    kind: Literal["int"] = "int"
    min: int | None = Field(default=None, exclude_if=_not_none)
    max: int | None = Field(default=None, exclude_if=_not_none)

    @model_validator(mode="after")
    def _bounds(self) -> IntType:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("int min exceeds max")
        return self


class DecimalType(_Shape):
    """A decimal; bounds are canonical decimal strings, never floats."""

    kind: Literal["decimal"] = "decimal"
    min: str | None = Field(default=None, exclude_if=_not_none)
    max: str | None = Field(default=None, exclude_if=_not_none)

    @field_validator("min", "max", mode="before")
    @classmethod
    def _decimal(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError("decimal bounds are written as decimal strings or integers")
        try:
            parsed = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"decimal bound {value!r} is not a decimal") from exc
        if not parsed.is_finite():
            raise ValueError("decimal bounds are finite")
        return _decimal_text(parsed)

    @model_validator(mode="after")
    def _bounds(self) -> DecimalType:
        if self.min is not None and self.max is not None and Decimal(self.min) > Decimal(self.max):
            raise ValueError("decimal min exceeds max")
        return self


class BoolType(_Shape):
    kind: Literal["bool"] = "bool"


class DateType(_Shape):
    kind: Literal["date"] = "date"


class DateTimeType(_Shape):
    kind: Literal["datetime"] = "datetime"


class DurationType(_Shape):
    kind: Literal["duration"] = "duration"


class EnumType(_Shape):
    kind: Literal["enum"] = "enum"
    members: tuple[str, ...] = Field(min_length=1, max_length=MAX_ENUM_MEMBERS)

    @field_validator("members")
    @classmethod
    def _members(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("enum members are unique")
        for member in value:
            if not member or unicodedata.normalize("NFC", member) != member:
                raise ValueError("enum members are non-empty NFC strings")
        return value


class EnumOfType(_Shape):
    """The members a ClaimType's enum value admits, read from that ClaimType."""

    kind: Literal["enum_of"] = "enum_of"
    claim_type: str

    _claim_type = field_validator("claim_type")(
        lambda value: _nfc_identifier(value, _PREDICATE_RE, label="enum_of ClaimType")
    )


class ListType(_Shape):
    kind: Literal["list"] = "list"
    item: FieldType
    max_items: int = Field(ge=1, le=MAX_LIST_ITEMS)

    @model_validator(mode="before")
    @classmethod
    def _required_bound(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and data.get("max_items") is None:
            raise ContractTypeRefused("a list type requires max_items")
        return data


class NestedType(_Shape):
    """A field holding one record of another Contract version."""

    kind: Literal["contract"] = "contract"
    contract: ContractRef


class VariantType(_Shape):
    """A tagged choice; each arm is one Contract version."""

    kind: Literal["variant"] = "variant"
    arms: dict[str, ContractRef] = Field(min_length=1, max_length=MAX_VARIANT_ARMS)

    @field_validator("arms")
    @classmethod
    def _arms(cls, value: dict[str, ContractRef]) -> dict[str, ContractRef]:
        for tag in value:
            _nfc_identifier(tag, _FIELD_NAME_RE, label="variant tag")
        return value


class KeyType(_Shape):
    """A Subject key of one kind; core canonicalizes it with the kind's identity rule."""

    kind: Literal["key"] = "key"
    subject_kind: str

    _subject_kind = field_validator("subject_kind")(
        lambda value: _nfc_identifier(value, _KIND_NAME_RE, label="SubjectKind")
    )


class SubjectType(_Shape):
    """A Subject of one kind. Reads produce it; an operation may only echo its input's."""

    kind: Literal["subject"] = "subject"
    subject_kind: str

    _subject_kind = field_validator("subject_kind")(
        lambda value: _nfc_identifier(value, _KIND_NAME_RE, label="SubjectKind")
    )


class CaptureType(_Shape):
    """Retained bytes of one CaptureKind, by Capture digest."""

    kind: Literal["capture"] = "capture"
    capture_kind: str

    _capture_kind = field_validator("capture_kind")(
        lambda value: _nfc_identifier(value, _ARTIFACT_NAME_RE, label="CaptureKind")
    )


class FieldResultType(_Shape):
    """``variant{present: {value: T, at}, absent: {}, invalid: {problem}}``."""

    kind: Literal["field_result"] = "field_result"
    value: FieldType


class RecordType(_Shape):
    """``{record: <locator>, key, fields: {f: field_result<C.f>}}``."""

    kind: Literal["record"] = "record"
    contract: ContractRef


class ResultType(_Shape):
    """``variant{record: record<C>, invalid_record, duplicate}``; consumed via ``.records``."""

    kind: Literal["result"] = "result"
    contract: ContractRef


class RefusalType(_Shape):
    """One operation refusal code's typed body."""

    kind: Literal["refusal"] = "refusal"
    contract: ContractRef


FieldType: TypeAlias = Annotated[
    StringType
    | IntType
    | DecimalType
    | BoolType
    | DateType
    | DateTimeType
    | DurationType
    | EnumType
    | EnumOfType
    | ListType
    | NestedType
    | VariantType
    | KeyType
    | SubjectType
    | CaptureType
    | FieldResultType
    | RecordType
    | ResultType
    | RefusalType,
    Field(discriminator="kind"),
]

ListType.model_rebuild()
FieldResultType.model_rebuild()
_FIELD_TYPE: Any = TypeAdapter(FieldType)

_SCALAR_KINDS = frozenset({"string", "int", "decimal", "bool", "date", "datetime", "duration"})
_SIMPLE_TYPES: dict[str, type[_Shape]] = {
    "bool": BoolType,
    "date": DateType,
    "datetime": DateTimeType,
    "duration": DurationType,
}


def _refuse_structured(value: object, *, depth: int = 0) -> None:
    """Refuse the definition-time exclusions in a structured (wire) type."""

    if depth > MAX_TYPE_DEPTH:
        raise ContractFormatError("a field type nests too deeply")
    if isinstance(value, BaseModel):
        return
    if isinstance(value, str):
        return
    if not isinstance(value, Mapping):
        return
    kind = value.get("kind")
    if isinstance(kind, str) and kind in REFUSED_TYPE_NAMES:
        raise ContractTypeRefused(f"the field type {kind!r} is refused; declare a typed shape")
    if kind == "list" and value.get("max_items") is None:
        raise ContractTypeRefused("a list type requires max_items")
    for key in ("item", "value"):
        if key in value:
            _refuse_structured(value[key], depth=depth + 1)


# -- the author spelling --------------------------------------------------------------


class _Spelling:
    """A small recursive-descent reader for one type spelling."""

    _TOKEN = re.compile(
        r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>-?[0-9]+(?:\.[0-9]+)?)'
        r"|(?P<range>\.\.)|(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)|(?P<sym>[<>{}\[\],:@?|=]))"
    )

    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(text):
            match = self._TOKEN.match(text, position)
            if match is None or match.end() == position:
                if text[position:].strip() == "":
                    break
                raise ContractFormatError(f"cannot read the type {text!r} at {position}")
            group = match.lastgroup
            assert group is not None
            self.tokens.append((group, match.group(group)))
            position = match.end()
        self.index = 0

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        at = self.index + offset
        return self.tokens[at] if at < len(self.tokens) else None

    def take(self, kind: str | None = None, value: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if (
            token is None
            or (kind is not None and token[0] != kind)
            or (value is not None and token[1] != value)
        ):
            expected = value or kind or "a token"
            raise ContractFormatError(f"the type {self.text!r} expected {expected}")
        self.index += 1
        return token

    def at_symbol(self, value: str) -> bool:
        token = self.peek()
        return token is not None and token == ("sym", value)

    def done(self) -> bool:
        return self.index >= len(self.tokens)


def _read_type(reader: _Spelling, depth: int = 0) -> dict[str, Any]:
    if depth > MAX_TYPE_DEPTH:
        raise ContractFormatError("a field type nests too deeply")
    _kind, name = reader.take("name")
    lowered = name.lower()
    if lowered in REFUSED_TYPE_NAMES:
        raise ContractTypeRefused(f"the field type {name!r} is refused; declare a typed shape")
    if reader.at_symbol("@"):
        reader.take("sym", "@")
        _n, version = reader.take("num")
        return {"kind": "contract", "contract": _ref(name, version)}
    if name == "string":
        return {"kind": "string", **_read_refinement(reader, ("pattern", "min_len", "max_len"))}
    if name in ("int", "decimal"):
        return {"kind": name, **_read_refinement(reader, ("min", "max"), numeric=name)}
    if name in _SIMPLE_TYPES:
        return {"kind": name}
    if name == "enum":
        reader.take("sym", "[")
        members = [_read_literal_text(reader)]
        while reader.at_symbol(","):
            reader.take("sym", ",")
            members.append(_read_literal_text(reader))
        reader.take("sym", "]")
        return {"kind": "enum", "members": members}
    if name == "enum_of":
        if reader.at_symbol(":"):
            reader.take("sym", ":")
        _k, claim_type = reader.take("name")
        return {"kind": "enum_of", "claim_type": claim_type}
    if name == "list":
        reader.take("sym", "<")
        item = _read_type(reader, depth + 1)
        reader.take("sym", ">")
        if not reader.at_symbol("{"):
            raise ContractTypeRefused("a list type requires max_items: list<T>{max_items: N}")
        bound = _read_refinement(reader, ("max_items",), numeric="int", bare="max_items")
        if "max_items" not in bound:
            raise ContractTypeRefused("a list type requires max_items")
        return {"kind": "list", "item": item, "max_items": bound["max_items"]}
    if name == "variant":
        reader.take("sym", "{")
        arms: dict[str, Any] = {}
        while True:
            _t, tag = reader.take("name")
            reader.take("sym", ":")
            arms[tag] = _read_ref(reader)
            if reader.at_symbol(","):
                reader.take("sym", ",")
                continue
            break
        reader.take("sym", "}")
        return {"kind": "variant", "arms": arms}
    if name in ("key", "subject", "capture"):
        reader.take("sym", "<")
        _k, target = reader.take("name")
        reader.take("sym", ">")
        field = {"key": "subject_kind", "subject": "subject_kind", "capture": "capture_kind"}[name]
        return {"kind": name, field: target}
    if name == "field_result":
        reader.take("sym", "<")
        value = _read_type(reader, depth + 1)
        reader.take("sym", ">")
        return {"kind": "field_result", "value": value}
    if name in ("record", "result", "refusal"):
        reader.take("sym", "<")
        ref = _read_ref(reader)
        reader.take("sym", ">")
        return {"kind": name, "contract": ref}
    raise ContractFormatError(f"unknown field type {name!r}")


def _ref(name: str, version: str) -> dict[str, Any]:
    if not version.isdigit():
        raise ContractFormatError(f"a Contract version is a positive integer, not {version!r}")
    return {"name": name, "version": int(version)}


def _read_ref(reader: _Spelling) -> dict[str, Any]:
    _k, name = reader.take("name")
    reader.take("sym", "@")
    _n, version = reader.take("num")
    return _ref(name, version)


def _read_literal_text(reader: _Spelling) -> str:
    kind, value = reader.take()
    if kind == "str":
        return cast(str, json.loads(value))
    if kind in ("name", "num"):
        return value
    raise ContractFormatError(f"the type {reader.text!r} has an invalid enum member")


def _read_number(reader: _Spelling, numeric: str) -> int | str:
    _k, value = reader.take("num")
    if numeric == "int":
        if "." in value:
            raise ContractFormatError(f"an int bound is an integer, not {value!r}")
        return int(value)
    return value


def _read_refinement(
    reader: _Spelling,
    keys: tuple[str, ...],
    *,
    numeric: str | None = None,
    bare: str | None = None,
) -> dict[str, Any]:
    if not reader.at_symbol("{"):
        return {}
    reader.take("sym", "{")
    found: dict[str, Any] = {}
    token = reader.peek()
    if numeric is not None and token is not None and token[0] in ("num", "range"):
        if bare is not None:
            found[bare] = _read_number(reader, numeric)
        else:
            if token[0] == "num":
                found["min"] = _read_number(reader, numeric)
            reader.take("range")
            following = reader.peek()
            if following is not None and following[0] == "num":
                found["max"] = _read_number(reader, numeric)
        reader.take("sym", "}")
        return found
    while not reader.at_symbol("}"):
        _k, key = reader.take("name")
        if key not in keys:
            raise ContractFormatError(f"the refinement {key!r} does not apply here")
        if reader.at_symbol(":"):
            reader.take("sym", ":")
        else:
            reader.take("sym", "=")
        if key == "pattern":
            found[key] = _read_literal_text(reader)
        else:
            found[key] = _read_number(reader, numeric or "int")
        if reader.at_symbol(","):
            reader.take("sym", ",")
    reader.take("sym", "}")
    return found


def parse_type(spelling: str) -> FieldType:
    """Read one author type spelling (without the ``?`` optional marker)."""

    reader = _Spelling(spelling)
    data = _read_type(reader)
    if not reader.done():
        raise ContractFormatError(f"the type {spelling!r} has trailing text")
    return cast(FieldType, _FIELD_TYPE.validate_python(data))


def parse_field_spelling(spelling: str) -> tuple[FieldType, bool]:
    """Read a field spelling: a type, optionally followed by ``?`` (key absent, no null)."""

    text = spelling.strip()
    optional = text.endswith("?")
    return parse_type(text[:-1] if optional else text), optional


def render_type(field_type: FieldType) -> str:
    """The author spelling of one type: the inverse of :func:`parse_type`."""

    kind = field_type.kind
    if isinstance(field_type, StringType):
        parts = []
        if field_type.pattern is not None:
            parts.append(f"pattern: {json.dumps(field_type.pattern)}")
        if field_type.min_len is not None:
            parts.append(f"min_len: {field_type.min_len}")
        if field_type.max_len is not None:
            parts.append(f"max_len: {field_type.max_len}")
        return "string" + ("{" + ", ".join(parts) + "}" if parts else "")
    if isinstance(field_type, IntType | DecimalType):
        if field_type.min is None and field_type.max is None:
            return kind
        low = "" if field_type.min is None else str(field_type.min)
        high = "" if field_type.max is None else str(field_type.max)
        return f"{kind}{{{low}..{high}}}"
    if isinstance(field_type, EnumType):
        return "enum[" + ", ".join(json.dumps(member) for member in field_type.members) + "]"
    if isinstance(field_type, EnumOfType):
        return f"enum_of {field_type.claim_type}"
    if isinstance(field_type, ListType):
        return f"list<{render_type(field_type.item)}>{{max_items: {field_type.max_items}}}"
    if isinstance(field_type, NestedType):
        return field_type.contract.spelling
    if isinstance(field_type, VariantType):
        arms = ", ".join(f"{tag}: {ref.spelling}" for tag, ref in field_type.arms.items())
        return "variant{" + arms + "}"
    if isinstance(field_type, KeyType | SubjectType):
        return f"{kind}<{field_type.subject_kind}>"
    if isinstance(field_type, CaptureType):
        return f"capture<{field_type.capture_kind}>"
    if isinstance(field_type, FieldResultType):
        return f"field_result<{render_type(field_type.value)}>"
    if isinstance(field_type, RecordType | ResultType | RefusalType):
        return f"{kind}<{field_type.contract.spelling}>"
    return kind


# -- the Contract artifact --------------------------------------------------------------


class ContractField(_Shape):
    """One field: its type, whether its key may be absent, and what it means."""

    type: FieldType
    optional: bool = Field(default=False, exclude_if=lambda value: value is False)
    description: str | None = Field(default=None, exclude_if=_not_none)

    @model_validator(mode="before")
    @classmethod
    def _spelling(cls, data: Any) -> Any:
        if isinstance(data, str):
            field_type, optional = parse_field_spelling(data)
            return {"type": field_type, "optional": optional}
        if isinstance(data, Mapping) and "type" in data:
            stated = dict(data)
            raw = stated["type"]
            if isinstance(raw, str):
                field_type, optional = parse_field_spelling(raw)
                if optional and stated.get("optional") is False:
                    raise ValueError("a field spelled optional cannot also state optional: false")
                stated["type"] = field_type
                if optional:
                    stated["optional"] = True
            else:
                _refuse_structured(raw)
            return stated
        return data

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is not None and (not value or unicodedata.normalize("NFC", value) != value):
            raise ValueError("a field description is non-empty NFC text")
        return value

    @property
    def spelling(self) -> str:
        return render_type(self.type) + ("?" if self.optional else "")


class Contract(_Shape):
    """One immutable data shape. A successor version names its predecessor's digest."""

    artifact_format: Literal["cruxible-contract-v1"] = "cruxible-contract-v1"
    name: str
    version: int = Field(ge=1, le=999_999_999)
    description: str | None = Field(default=None, exclude_if=_not_none)
    fields: dict[str, ContractField] = Field(min_length=1, max_length=MAX_CONTRACT_FIELDS)
    predecessor_digest: str | None = Field(default=None, exclude_if=_not_none)

    _name = field_validator("name")(
        lambda value: _nfc_identifier(value, _CONTRACT_NAME_RE, label="Contract name")
    )

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: dict[str, ContractField]) -> dict[str, ContractField]:
        for name in value:
            _nfc_identifier(name, _FIELD_NAME_RE, label="Contract field name")
        return value

    @field_validator("predecessor_digest")
    @classmethod
    def _predecessor(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _succession(self) -> Contract:
        if (self.version == 1) != (self.predecessor_digest is None):
            raise ValueError(
                "version 1 of a Contract has no predecessor; every later version names "
                "its predecessor's digest"
            )
        return self

    @property
    def identity(self) -> ArtifactIdentity:
        return contract_identity(self.name, self.version)

    @property
    def lifecycle(self) -> ArtifactLifecycle:
        """Contracts are immutable and never retire; succession is a new version."""

        return ArtifactLifecycle()

    @property
    def ref(self) -> ContractRef:
        return ContractRef(name=self.name, version=self.version)

    def refs(self) -> tuple[ContractRef, ...]:
        """Every Contract version this shape pins, in field order, without repeats.

        For dependency listing only (``get``, ``orient``, closure): use
        :func:`all_field_refs` to judge the acceptance law, which must see
        every occurrence, not just the first.
        """

        found: dict[tuple[str, int], ContractRef] = {}
        for ref in all_field_refs(self):
            found.setdefault((ref.name, ref.version), ref)
        return tuple(found.values())


def coerce_field_type(value: FieldType | str | Mapping[str, object]) -> FieldType:
    """A type from its spelling or its structured form; refusals carry their code."""

    if isinstance(value, str):
        return parse_type(value)
    _refuse_structured(value)
    return cast(FieldType, _FIELD_TYPE.validate_python(value))


def all_field_refs(contract: Contract) -> tuple[ContractRef, ...]:
    """Every Contract pin this shape names, in field order, including repeats.

    Two fields may pin the same ``name@N`` with different (or missing) digest
    annotations; the acceptance law and :func:`parse_contract` must judge each
    occurrence on its own, not just the first one found.
    """

    found: list[ContractRef] = []
    for item in contract.fields.values():
        found.extend(type_refs(item.type))
    return tuple(found)


def type_refs(field_type: FieldType) -> tuple[ContractRef, ...]:
    """The Contract pins one type names, depth first."""

    if isinstance(field_type, NestedType | RecordType | ResultType | RefusalType):
        return (field_type.contract,)
    if isinstance(field_type, VariantType):
        return tuple(field_type.arms.values())
    if isinstance(field_type, ListType):
        return type_refs(field_type.item)
    if isinstance(field_type, FieldResultType):
        return type_refs(field_type.value)
    return ()


def map_type_refs(field_type: FieldType, pin: Callable[[ContractRef], ContractRef]) -> FieldType:
    """The same type with every Contract pin replaced by ``pin(ref)``."""

    if isinstance(field_type, NestedType | RecordType | ResultType | RefusalType):
        return field_type.model_copy(update={"contract": pin(field_type.contract)})
    if isinstance(field_type, VariantType):
        return field_type.model_copy(
            update={"arms": {tag: pin(ref) for tag, ref in field_type.arms.items()}}
        )
    if isinstance(field_type, ListType):
        return field_type.model_copy(update={"item": map_type_refs(field_type.item, pin)})
    if isinstance(field_type, FieldResultType):
        return field_type.model_copy(update={"value": map_type_refs(field_type.value, pin)})
    return field_type


def pin_contract_refs(contract: Contract, pin: Callable[[ContractRef], ContractRef]) -> Contract:
    """The same Contract with every nested pin resolved by ``pin``."""

    return contract.model_copy(
        update={
            "fields": {
                name: item.model_copy(update={"type": map_type_refs(item.type, pin)})
                for name, item in contract.fields.items()
            }
        }
    )


def parse_contract_ref(spelling: str) -> ContractRef:
    """``name@N`` as a Contract pin (the digest is added where the pin resolves)."""

    name, at, version = spelling.strip().partition("@")
    if not at or not version.isdigit():
        raise ContractFormatError(f"a Contract pin is written name@N, not {spelling!r}")
    return ContractRef(name=name, version=int(version))


def contract_identity(name: str, version: int) -> ArtifactIdentity:
    return ArtifactIdentity(kind="Contract", name=f"{name}/v{version}")


def contract_ref_for_identity(identity: ArtifactIdentity) -> ContractRef:
    if identity.kind != "Contract":
        raise ContractFormatError("a Contract pin targets a Contract identity")
    name, slash, version = identity.name.rpartition("/v")
    if not slash or not version.isdigit():
        raise ContractFormatError("a Contract identity is <name>/v<N>")
    return ContractRef(name=name, version=int(version))


def contract_path(name: str, version: int) -> str:
    if not _CONTRACT_NAME_RE.fullmatch(name) or version < 1:
        raise ContractFormatError("Contract is not path-addressable")
    return f"{CONTRACT_PREFIX}{name}/v{version}.json"


def contract_path_for_identity(identity_name: str) -> str:
    ref = contract_ref_for_identity(ArtifactIdentity(kind="Contract", name=identity_name))
    return contract_path(ref.name, ref.version)


def contract_path_parts(path: str) -> tuple[str, int] | None:
    match = _CONTRACT_PATH_RE.fullmatch(path)
    return None if match is None else (match.group(1), int(match.group(2)))


def render_contract(contract: Contract) -> bytes:
    return pretty_canonical_bytes(contract.model_dump(mode="json"))


def contract_digest(contract: Contract) -> ArtifactDigest:
    return typed_digest(ArtifactDigest, CONTRACT_DIGEST_DOMAIN, contract.model_dump(mode="json"))


def contract_type_refusal(exc: BaseException) -> ContractTypeRefused | None:
    """The definition-time type refusal inside a validation failure, if that is what it is."""

    if isinstance(exc, ContractTypeRefused):
        return exc
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        found = contract_type_refusal(cause)
        if found is not None:
            return found
    if isinstance(exc, ValidationError):
        for error in exc.errors():
            inner = (error.get("ctx") or {}).get("error")
            if isinstance(inner, ContractTypeRefused):
                return inner
    return None


def validate_contract(data: object) -> Contract:
    """A Contract from data; a refused type raises :class:`ContractTypeRefused`."""

    try:
        return Contract.model_validate(data)
    except ValidationError as exc:
        refused = contract_type_refusal(exc)
        if refused is not None:
            raise ContractTypeRefused(str(refused)) from exc
        raise ContractFormatError(f"Contract failed strict validation: {exc}") from exc


def parse_contract(
    content: bytes, *, path: str, codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC
) -> Contract:
    """Strict canonical parse of one accepted Contract; every nested pin carries its digest."""

    try:
        data = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractFormatError("Contract is not canonical JSON") from exc
    contract = validate_contract(data)
    if any(ref.digest is None for ref in all_field_refs(contract)):
        raise ContractFormatError("an accepted Contract pins every nested Contract by digest")
    if not artifact_path_matches(contract_path(contract.name, contract.version), path, codec=codec):
        raise ContractFormatError("Contract identity/path disagreement")
    if artifact_bytes_for_path(render_contract(contract), path, codec=codec) != content:
        raise ContractFormatError("Contract is not in canonical wire form")
    return contract


# -- the acceptance law ------------------------------------------------------------------


@dataclass(frozen=True)
class ContractLawProblem:
    code: str
    message: str


def evaluate_contract_law(
    contract: Contract,
    *,
    path: str,
    lookup: Callable[[str], bytes | None],
    parent_content: bytes | None,
    content: bytes,
) -> tuple[ContractLawProblem, ...]:
    """Judge one Contract version against its candidate tree.

    A version is immutable, its predecessor is the accepted previous version,
    and every nested pin resolves to the exact accepted Contract it names.
    """

    problems: list[ContractLawProblem] = []
    if parent_content is not None and parent_content != content:
        problems.append(
            ContractLawProblem(
                "cruxible.contract.immutable",
                f"{contract.identity.qualified} is accepted and immutable; "
                "author the next version instead.",
            )
        )
    if contract.version > 1:
        previous_path = contract_path(contract.name, contract.version - 1)
        previous = lookup(previous_path)
        if previous is None:
            problems.append(
                ContractLawProblem(
                    "cruxible.contract.predecessor_missing",
                    f"{contract.ref.spelling} succeeds version {contract.version - 1}, "
                    "which is not accepted.",
                )
            )
        else:
            expected = contract_digest(parse_contract(previous, path=previous_path)).tagged
            if contract.predecessor_digest != expected:
                problems.append(
                    ContractLawProblem(
                        "cruxible.contract.predecessor_mismatch",
                        f"{contract.ref.spelling} must name {expected} as its predecessor.",
                    )
                )
    for ref in all_field_refs(contract):
        if (ref.name, ref.version) == (contract.name, contract.version):
            problems.append(
                ContractLawProblem(
                    "cruxible.contract.self_reference", "A Contract cannot contain itself."
                )
            )
            continue
        target_path = contract_path(ref.name, ref.version)
        target = lookup(target_path)
        if target is None:
            problems.append(
                ContractLawProblem(
                    "cruxible.contract.pin_unresolved",
                    f"{ref.spelling} is not an accepted Contract.",
                )
            )
            continue
        digest = contract_digest(parse_contract(target, path=target_path)).tagged
        if ref.digest != digest:
            problems.append(
                ContractLawProblem(
                    "cruxible.contract.pin_digest_mismatch",
                    f"{ref.spelling} pins {ref.digest}, but the accepted version is {digest}.",
                )
            )
    # Several occurrences of the same pin can earn the identical problem; keep
    # one of each so a repeated reference does not multiply the diagnostics.
    return tuple(dict.fromkeys(problems))


# -- runtime values ------------------------------------------------------------------------

ProblemCode = Literal[
    "absent_required",
    "type_mismatch",
    "out_of_range",
    "not_in_enum",
    "canonical_form_failed",
    "pattern_mismatch",
    "key_unparseable",
    "key_absent",
    "header_invalid",
]


class Problem(_Shape):
    """``problem@1``: why one value is invalid, with the offending lexeme if any."""

    code: ProblemCode
    field: str | None = Field(default=None, exclude_if=_not_none)
    lexeme: str | None = Field(default=None, exclude_if=_not_none)


class FieldPresent(_Shape):
    tag: Literal["present"] = "present"
    value: Any
    at: str


class FieldAbsent(_Shape):
    tag: Literal["absent"] = "absent"


class FieldInvalid(_Shape):
    tag: Literal["invalid"] = "invalid"
    problem: Problem


FieldResult: TypeAlias = Annotated[
    FieldPresent | FieldAbsent | FieldInvalid, Field(discriminator="tag")
]


class RecordValue(_Shape):
    """``record<C>``: one record's locator, key and per-field results."""

    record: str
    key: dict[str, Any] = Field(default_factory=dict)
    fields: dict[str, FieldResult]


class RecordArm(_Shape):
    tag: Literal["record"] = "record"
    record: RecordValue


class InvalidRecordArm(_Shape):
    """Key or enumeration failures only; a field problem stays inside its record."""

    tag: Literal["invalid_record"] = "invalid_record"
    record: str
    problems: tuple[Problem, ...] = Field(min_length=1)


class DuplicateArm(_Shape):
    tag: Literal["duplicate"] = "duplicate"
    record: str
    key: dict[str, Any]
    first: str


ResultValue: TypeAlias = Annotated[
    RecordArm | InvalidRecordArm | DuplicateArm, Field(discriminator="tag")
]


class RefusalValue(_Shape):
    """``refusal<C>``: one operation refusal code with its typed body."""

    code: str
    contract: ContractRef
    body: dict[str, Any]


ContractResolver = Callable[[ContractRef], "Contract | None"]
EnumResolver = Callable[[str], "tuple[str, ...] | None"]


def records(results: Iterable[RecordArm | InvalidRecordArm | DuplicateArm]) -> list[RecordValue]:
    """``$steps.x.records``: the record arm of each result, and only that arm."""

    return [item.record for item in results if isinstance(item, RecordArm)]


def _invalid(code: ProblemCode, field: str | None, lexeme: object = None) -> FieldInvalid:
    text = None
    if lexeme is not None:
        text = lexeme if isinstance(lexeme, str) else json.dumps(lexeme, default=str)
        text = text[:256]
    return FieldInvalid(problem=Problem(code=code, field=field, lexeme=text))


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _in_range(value: Any, low: Any, high: Any) -> bool:
    return (low is None or value >= low) and (high is None or value <= high)


def check_value(
    value: object,
    field_type: FieldType,
    *,
    at: str,
    field: str | None = None,
    resolve: ContractResolver | None = None,
    enum_members: EnumResolver | None = None,
) -> FieldPresent | FieldInvalid:
    """Check one present value against one type; a problem is a result, never an exception."""

    if isinstance(field_type, StringType):
        if not isinstance(value, str):
            return _invalid("type_mismatch", field, value)
        if unicodedata.normalize("NFC", value) != value:
            return _invalid("canonical_form_failed", field, value)
        if not _in_range(len(value), field_type.min_len, field_type.max_len):
            return _invalid("out_of_range", field, value)
        if field_type.pattern is not None and re.fullmatch(field_type.pattern, value) is None:
            return _invalid("pattern_mismatch", field, value)
        return FieldPresent(value=value, at=at)
    if isinstance(field_type, IntType):
        if isinstance(value, bool) or not isinstance(value, int):
            return _invalid("type_mismatch", field, value)
        if not _in_range(value, field_type.min, field_type.max):
            return _invalid("out_of_range", field, value)
        return FieldPresent(value=value, at=at)
    if isinstance(field_type, DecimalType):
        if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
            return _invalid("type_mismatch", field, value)
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return _invalid("type_mismatch", field, value)
        if not number.is_finite():
            return _invalid("type_mismatch", field, value)
        low = None if field_type.min is None else Decimal(field_type.min)
        high = None if field_type.max is None else Decimal(field_type.max)
        if not _in_range(number, low, high):
            return _invalid("out_of_range", field, value)
        return FieldPresent(value=number, at=at)
    if isinstance(field_type, BoolType):
        if not isinstance(value, bool):
            return _invalid("type_mismatch", field, value)
        return FieldPresent(value=value, at=at)
    if isinstance(field_type, DateType):
        if isinstance(value, date) and not isinstance(value, datetime):
            return FieldPresent(value=value, at=at)
        if not isinstance(value, str):
            return _invalid("type_mismatch", field, value)
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            return _invalid("type_mismatch", field, value)
        if parsed_date.isoformat() != value:
            return _invalid("canonical_form_failed", field, value)
        return FieldPresent(value=parsed_date, at=at)
    if isinstance(field_type, DateTimeType):
        if isinstance(value, datetime):
            return FieldPresent(value=parse_datetime(value), at=at)
        if not isinstance(value, str):
            return _invalid("type_mismatch", field, value)
        try:
            moment = parse_datetime(value)
        except ValueError:
            return _invalid("type_mismatch", field, value)
        if format_datetime(moment) != value.replace("Z", "+00:00"):
            return _invalid("canonical_form_failed", field, value)
        return FieldPresent(value=moment, at=at)
    if isinstance(field_type, DurationType):
        from cruxible_client.contracts.captures import CanonicalDuration

        try:
            return FieldPresent(value=CanonicalDuration.model_validate(value), at=at)
        except ValidationError:
            return _invalid("type_mismatch", field, value)
    if isinstance(field_type, EnumType | EnumOfType):
        if not isinstance(value, str):
            return _invalid("type_mismatch", field, value)
        members: tuple[str, ...] | None
        if isinstance(field_type, EnumType):
            members = field_type.members
        else:
            members = None if enum_members is None else enum_members(field_type.claim_type)
        if members is not None and value not in members:
            return _invalid("not_in_enum", field, value)
        return FieldPresent(value=value, at=at)
    if isinstance(field_type, ListType):
        if not isinstance(value, (list, tuple)):
            return _invalid("type_mismatch", field, value)
        if len(value) > field_type.max_items:
            return _invalid("out_of_range", field, len(value))
        items = []
        for index, member in enumerate(value):
            checked = check_value(
                member,
                field_type.item,
                at=f"{at}/{index}",
                field=f"{field or ''}[{index}]",
                resolve=resolve,
                enum_members=enum_members,
            )
            if isinstance(checked, FieldInvalid):
                return checked
            items.append(checked.value)
        return FieldPresent(value=items, at=at)
    if isinstance(field_type, NestedType | VariantType):
        if isinstance(field_type, VariantType):
            if not isinstance(value, Mapping) or value.get("tag") not in field_type.arms:
                return _invalid("type_mismatch", field, value)
            ref = field_type.arms[cast(str, value["tag"])]
            body = {key: item for key, item in value.items() if key != "tag"}
        else:
            if not isinstance(value, Mapping):
                return _invalid("type_mismatch", field, value)
            ref, body = field_type.contract, dict(value)
        target = None if resolve is None else resolve(ref)
        if target is None:
            return _invalid("type_mismatch", field, value)
        nested = check_record(
            body, target, record=at, resolve=resolve, enum_members=enum_members, base=at
        )
        for name, result in nested.fields.items():
            if isinstance(result, FieldInvalid):
                inner = result.problem
                return FieldInvalid(
                    problem=inner.model_copy(
                        update={"field": f"{field or ''}.{inner.field or name}"}
                    )
                )
        return FieldPresent(value=dict(value), at=at)
    if isinstance(field_type, KeyType | SubjectType):
        # Core canonicalizes a key with its SubjectKind's identity rule; that
        # hook is not built yet (deferred past B1, no identity rule invented
        # here). A plain non-empty string -- the key's written spelling -- is
        # accepted unchanged until then. Untyped nested data (a mapping) names
        # no identity rule at all, so it fails closed rather than passing
        # through as an opaque blob.
        if isinstance(value, str) and value:
            return FieldPresent(value=value, at=at)
        return _invalid(
            "key_unparseable" if isinstance(field_type, KeyType) else "type_mismatch", field, value
        )
    if isinstance(field_type, CaptureType):
        if isinstance(value, str) and _DIGEST_RE.fullmatch(value):
            return FieldPresent(value=value, at=at)
        return _invalid("type_mismatch", field, value)
    model: Any = {
        "field_result": _FIELD_RESULT,
        "record": _RECORD,
        "result": _RESULT,
        "refusal": _REFUSAL,
    }[field_type.kind]
    try:
        return FieldPresent(value=model.validate_python(value), at=at)
    except ValidationError:
        return _invalid("type_mismatch", field, value)


def check_record(
    data: Mapping[str, object],
    contract: Contract,
    *,
    record: str,
    key: Mapping[str, object] | None = None,
    resolve: ContractResolver | None = None,
    enum_members: EnumResolver | None = None,
    base: str = "",
) -> RecordValue:
    """``record<C>`` from raw data: every field gets its own result; siblings never taint.

    A missing required field is ``invalid{absent_required}``, a missing optional
    one is ``absent``. Keys the Contract does not declare are ignored here.
    """

    fields: dict[str, FieldPresent | FieldAbsent | FieldInvalid] = {}
    for name, declared in contract.fields.items():
        if name not in data:
            fields[name] = FieldAbsent() if declared.optional else _invalid("absent_required", name)
            continue
        fields[name] = check_value(
            data[name],
            declared.type,
            at=f"{base}/{name}",
            field=name,
            resolve=resolve,
            enum_members=enum_members,
        )
    return RecordValue(record=record, key=dict(key or {}), fields=fields)


_FIELD_RESULT: Any = TypeAdapter(FieldResult)
_RECORD: Any = TypeAdapter(RecordValue)
_RESULT: Any = TypeAdapter(ResultValue)
_REFUSAL: Any = TypeAdapter(RefusalValue)


# -- casts ----------------------------------------------------------------------------------


def cast_value(
    result: FieldPresent | FieldAbsent | FieldInvalid,
    to: FieldType,
    *,
    field: str | None = None,
    enum_members: EnumResolver | None = None,
) -> FieldPresent | FieldAbsent | FieldInvalid:
    """``shape`` ``as: <type>``: an explicit conversion of one field result.

    Absent and invalid inputs pass through. A value that cannot convert becomes
    ``invalid{type_mismatch}``; one that converts but breaks a refinement is
    ``invalid{out_of_range}`` (or the refinement's own code).
    """

    if not isinstance(result, FieldPresent):
        return result
    value = result.value
    converted: object = value
    if isinstance(to, IntType) and isinstance(value, str):
        try:
            converted = int(value.strip(), 10)
        except ValueError:
            return _invalid("type_mismatch", field, value)
    elif isinstance(to, DecimalType) and isinstance(value, str):
        try:
            converted = Decimal(value.strip())
        except InvalidOperation:
            return _invalid("type_mismatch", field, value)
    elif isinstance(to, BoolType) and isinstance(value, str):
        if value not in ("true", "false"):
            return _invalid("type_mismatch", field, value)
        converted = value == "true"
    elif isinstance(to, DateType) and isinstance(value, datetime):
        return _invalid("type_mismatch", field, format_datetime(value))
    elif isinstance(to, StringType) and isinstance(value, Decimal):
        return _invalid("type_mismatch", field, str(value))
    elif isinstance(to, DecimalType) and isinstance(value, int) and not isinstance(value, bool):
        converted = Decimal(value)
    checked = check_value(converted, to, at=result.at, field=field, enum_members=enum_members)
    if isinstance(checked, FieldInvalid) and checked.problem.code in (
        "canonical_form_failed",
        "key_unparseable",
    ):
        return _invalid("type_mismatch", field, value)
    return checked


def cast_record(
    record: RecordValue,
    casts: Mapping[str, FieldType],
    *,
    key_fields: Iterable[str] = (),
    enum_members: EnumResolver | None = None,
) -> RecordArm | InvalidRecordArm:
    """Cast named fields. Only a failed cast of a key field invalidates the record."""

    keys = frozenset(key_fields)
    fields = dict(record.fields)
    key_problems: list[Problem] = []
    for name, to in casts.items():
        source = fields.get(name, FieldAbsent())
        cast_result = cast_value(source, to, field=name, enum_members=enum_members)
        fields[name] = cast_result
        if name in keys and isinstance(cast_result, FieldInvalid):
            key_problems.append(cast_result.problem.model_copy(update={"code": "key_unparseable"}))
    if key_problems:
        return InvalidRecordArm(record=record.record, problems=tuple(key_problems))
    return RecordArm(record=record.model_copy(update={"fields": fields}))


# -- the edge rule ----------------------------------------------------------------------------

EdgeVia = Literal["records", "field"]


def _same_ref(left: ContractRef, right: ContractRef) -> bool:
    if (left.name, left.version) != (right.name, right.version):
        return False
    return left.digest is None or right.digest is None or left.digest == right.digest


def _within(low: Any, high: Any, outer_low: Any, outer_high: Any) -> bool:
    if outer_low is not None and (low is None or low < outer_low):
        return False
    return not (outer_high is not None and (high is None or high > outer_high))


def edge_refusal(src: FieldType, dst: FieldType, *, via: EdgeVia | None = None) -> str | None:
    """Why ``src`` may not flow into ``dst``; ``None`` when the edge is admitted.

    Admitted: identical types; a narrower refinement into a wider one; a
    ``result<C>`` list read through its record arm (``via="records"``) as
    ``record<C>``; a ``field_result<T>`` read through ``$item.fields.f``
    (``via="field"``) as ``T``. Every other conversion is an explicit cast.
    """

    if via == "records":
        if isinstance(src, ListType) and isinstance(src.item, ResultType):
            return edge_refusal(
                ListType(item=RecordType(contract=src.item.contract), max_items=src.max_items),
                dst,
            )
        if isinstance(src, ResultType):
            return edge_refusal(RecordType(contract=src.contract), dst)
        return "only a result<C> is read through .records"
    if via == "field":
        if isinstance(src, FieldResultType):
            return edge_refusal(src.value, dst)
        return "only a field_result<T> is read through $item.fields"
    if src == dst:
        return None
    if src.kind != dst.kind:
        if isinstance(src, ResultType) or (
            isinstance(src, ListType) and isinstance(src.item, ResultType)
        ):
            return "a result<C> is consumed only through its record arm: for_each $steps.x.records"
        if isinstance(src, FieldResultType):
            return "a field_result<T> is read through $item.fields.f, never as a value"
        return f"{render_type(src)} does not flow into {render_type(dst)}; cast it with as:"
    if isinstance(src, StringType) and isinstance(dst, StringType):
        if dst.pattern is not None and src.pattern != dst.pattern:
            return "the destination string pattern is not implied by the source"
        if not _within(src.min_len, src.max_len, dst.min_len, dst.max_len):
            return "the source string length is not within the destination's"
        return None
    if isinstance(src, IntType) and isinstance(dst, IntType):
        return (
            None
            if _within(src.min, src.max, dst.min, dst.max)
            else ("the source int range is wider than the destination's")
        )
    if isinstance(src, DecimalType) and isinstance(dst, DecimalType):
        bounds = [None if item is None else Decimal(item) for item in (src.min, src.max)]
        outer = [None if item is None else Decimal(item) for item in (dst.min, dst.max)]
        return (
            None
            if _within(*bounds, *outer)
            else ("the source decimal range is wider than the destination's")
        )
    if isinstance(src, EnumType) and isinstance(dst, EnumType):
        return (
            None
            if set(src.members) <= set(dst.members)
            else ("the source enum has members the destination does not")
        )
    if isinstance(src, ListType) and isinstance(dst, ListType):
        if src.max_items > dst.max_items:
            return "the source list may hold more items than the destination admits"
        inner = edge_refusal(src.item, dst.item)
        return None if inner is None else f"list item: {inner}"
    if isinstance(src, NestedType | RecordType | ResultType | RefusalType) and isinstance(
        dst, NestedType | RecordType | ResultType | RefusalType
    ):
        return (
            None
            if _same_ref(src.contract, dst.contract)
            else (f"{src.contract.spelling} is not {dst.contract.spelling}")
        )
    if isinstance(src, VariantType) and isinstance(dst, VariantType):
        if not set(src.arms) <= set(dst.arms) or any(
            not _same_ref(ref, dst.arms[tag]) for tag, ref in src.arms.items()
        ):
            return "the source variant has arms the destination does not"
        return None
    if isinstance(src, FieldResultType) and isinstance(dst, FieldResultType):
        inner = edge_refusal(src.value, dst.value)
        return None if inner is None else f"field_result value: {inner}"
    return f"{render_type(src)} is not {render_type(dst)}"


def assignable(src: FieldType, dst: FieldType, *, via: EdgeVia | None = None) -> bool:
    """Whether a value of ``src`` may flow into ``dst`` without an explicit cast."""

    return edge_refusal(src, dst, via=via) is None


# -- the closed three-valued predicate -------------------------------------------------------

Truth = Literal["true", "false", "unknown"]
Scalar: TypeAlias = str | int | bool | Decimal | date | datetime


class FieldOperand(_Shape):
    """``$item.fields.<name>``: evaluates only when the field is present."""

    field: str

    _field = field_validator("field")(
        lambda value: _nfc_identifier(value, _FIELD_NAME_RE, label="predicate field")
    )


class ParameterOperand(_Shape):
    """``$parameters.<name>``."""

    parameter: str

    _parameter = field_validator("parameter")(
        lambda value: _nfc_identifier(value, _FIELD_NAME_RE, label="predicate parameter")
    )


class LiteralOperand(_Shape):
    literal: str | int | bool

    @field_validator("literal", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("predicate literals are strings, integers or booleans")
        return value


Operand: TypeAlias = FieldOperand | ParameterOperand | LiteralOperand
ComparisonOp = Literal["eq", "ne", "lt", "le", "gt", "ge"]


class Comparison(_Shape):
    op: ComparisonOp
    left: Operand
    right: Operand


class Membership(_Shape):
    op: Literal["in", "not_in"]
    left: Operand
    right: tuple[str | int | bool, ...] | ParameterOperand


class Presence(_Shape):
    op: Literal["present", "absent"]
    field: str

    _field = field_validator("field")(
        lambda value: _nfc_identifier(value, _FIELD_NAME_RE, label="predicate field")
    )


class AllOf(_Shape):
    op: Literal["all_of"] = "all_of"
    of: tuple[Predicate, ...] = Field(min_length=1)


class AnyOf(_Shape):
    op: Literal["any_of"] = "any_of"
    of: tuple[Predicate, ...] = Field(min_length=1)


class Not(_Shape):
    op: Literal["not"] = "not"
    of: Predicate


Predicate: TypeAlias = Annotated[
    Comparison | Membership | Presence | AllOf | AnyOf | Not, Field(discriminator="op")
]
for _model in (AllOf, AnyOf, Not):
    _model.model_rebuild()
_PREDICATE: Any = TypeAdapter(Predicate)

_UNKNOWN = object()
_ABSENT = object()


def _not(value: Truth) -> Truth:
    return {"true": "false", "false": "true", "unknown": "unknown"}[value]  # type: ignore[return-value]


def _operand(
    operand: Operand,
    fields: Mapping[str, FieldPresent | FieldAbsent | FieldInvalid],
    parameters: Mapping[str, object],
    required: frozenset[str],
) -> object:
    if isinstance(operand, LiteralOperand):
        return operand.literal
    if isinstance(operand, ParameterOperand):
        return parameters.get(operand.parameter, _UNKNOWN)
    result = fields.get(operand.field)
    if isinstance(result, FieldPresent):
        return result.value
    if isinstance(result, FieldAbsent) and operand.field not in required:
        return _ABSENT
    return _UNKNOWN


def _comparable(left: object, right: object) -> tuple[Any, Any] | None:
    numbers = (int, Decimal)
    if isinstance(left, bool) or isinstance(right, bool):
        return (left, right) if isinstance(left, bool) and isinstance(right, bool) else None
    if isinstance(left, numbers) and isinstance(right, numbers):
        return left, right
    if isinstance(left, Decimal) and isinstance(right, str):
        try:
            return left, Decimal(right)
        except InvalidOperation:
            return None
    if isinstance(left, str) and isinstance(right, Decimal):
        flipped = _comparable(right, left)
        return None if flipped is None else (flipped[1], flipped[0])
    for kind, reader in ((datetime, parse_datetime), (date, date.fromisoformat)):
        if isinstance(left, kind) and isinstance(right, str):
            try:
                return left, reader(right)
            except ValueError:
                return None
        if isinstance(right, kind) and isinstance(left, str):
            try:
                return reader(left), right
            except ValueError:
                return None
    if type(left) is type(right) or (isinstance(left, str) and isinstance(right, str)):
        return left, right
    return None


def evaluate_predicate(
    predicate: Predicate,
    *,
    fields: Mapping[str, FieldPresent | FieldAbsent | FieldInvalid] | RecordValue,
    parameters: Mapping[str, object] | None = None,
    required: Iterable[str] = (),
) -> Truth:
    """Kleene evaluation: an invalid field, or an absent required one, is ``unknown``.

    An absent optional field has no value: a comparison or membership over it is
    ``false``; ``present``/``absent`` test it directly. ``not unknown`` is
    ``unknown``; ``all_of`` is false if any part is false, else unknown if any is;
    ``any_of`` is true if any part is true, else unknown if any is.
    """

    values = fields.fields if isinstance(fields, RecordValue) else fields
    stated = parameters or {}
    needed = frozenset(required)
    if isinstance(predicate, Presence):
        result = values.get(predicate.field)
        if isinstance(result, FieldInvalid) or (
            isinstance(result, FieldAbsent | type(None)) and predicate.field in needed
        ):
            return "unknown"
        present = isinstance(result, FieldPresent)
        return "true" if present == (predicate.op == "present") else "false"
    if isinstance(predicate, Not):
        return _not(
            evaluate_predicate(predicate.of, fields=values, parameters=stated, required=needed)
        )
    if isinstance(predicate, AllOf | AnyOf):
        parts = [
            evaluate_predicate(part, fields=values, parameters=stated, required=needed)
            for part in predicate.of
        ]
        decisive, other = ("false", "true") if isinstance(predicate, AllOf) else ("true", "false")
        if decisive in parts:
            return decisive  # type: ignore[return-value]
        return "unknown" if "unknown" in parts else other  # type: ignore[return-value]
    left = _operand(predicate.left, values, stated, needed)
    if left is _UNKNOWN:
        return "unknown"
    if isinstance(predicate, Membership):
        if isinstance(predicate.right, ParameterOperand):
            members = stated.get(predicate.right.parameter, _UNKNOWN)
            if members is _UNKNOWN or not isinstance(members, (list, tuple, frozenset, set)):
                return "unknown"
        else:
            members = predicate.right
        if left is _ABSENT:
            return "false"
        inside = any(
            (pair := _comparable(left, member)) is not None and pair[0] == pair[1]
            for member in cast(Iterable[object], members)
        )
        return "true" if inside == (predicate.op == "in") else "false"
    right = _operand(predicate.right, values, stated, needed)
    if right is _UNKNOWN:
        return "unknown"
    if left is _ABSENT or right is _ABSENT:
        return "false"
    pair = _comparable(left, right)
    if pair is None:
        return "unknown"
    a, b = pair
    try:
        outcome = {
            "eq": a == b,
            "ne": a != b,
            "lt": a < b,
            "le": a <= b,
            "gt": a > b,
            "ge": a >= b,
        }[predicate.op]
    except TypeError:
        return "unknown"
    return "true" if outcome else "false"


_CALL_RE = re.compile(r"\s*([a-z_]+)\s*\(")


def parse_predicate(text: str) -> Predicate:
    """Read the closed call form: ``eq($item.fields.status, "fixed")``, ``all_of(a, b)``.

    Operands are ``$item.fields.<f>``, ``$parameters.<p>``, or a string,
    integer or boolean literal; ``in``/``not_in`` take a ``[...]`` literal list
    or a parameter. Anything else is refused.
    """

    reader = _PredicateReader(text)
    predicate = reader.predicate()
    reader.skip()
    if reader.position != len(text):
        raise ContractFormatError(f"the predicate {text!r} has trailing text")
    return predicate


class _PredicateReader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.position = 0

    def skip(self) -> None:
        while self.position < len(self.text) and self.text[self.position].isspace():
            self.position += 1

    def expect(self, symbol: str) -> None:
        self.skip()
        if not self.text.startswith(symbol, self.position):
            raise ContractFormatError(f"the predicate {self.text!r} expected {symbol!r}")
        self.position += len(symbol)

    def peek(self, symbol: str) -> bool:
        self.skip()
        return self.text.startswith(symbol, self.position)

    def predicate(self) -> Predicate:
        match = _CALL_RE.match(self.text, self.position)
        if match is None:
            raise ContractFormatError(f"the predicate {self.text!r} is not a closed form")
        op = match.group(1)
        self.position = match.end()
        data: dict[str, Any]
        if op in ("all_of", "any_of"):
            parts = [self.predicate()]
            while self.peek(","):
                self.expect(",")
                parts.append(self.predicate())
            data = {"op": op, "of": parts}
        elif op == "not":
            data = {"op": "not", "of": self.predicate()}
        elif op in ("present", "absent"):
            operand = self.operand()
            if not isinstance(operand, dict) or "field" not in operand:
                raise ContractFormatError(f"{op} takes $item.fields.<f>")
            data = {"op": op, "field": operand["field"]}
        elif op in ("eq", "ne", "lt", "le", "gt", "ge"):
            left = self.operand()
            self.expect(",")
            data = {"op": op, "left": left, "right": self.operand()}
        elif op in ("in", "not_in"):
            left = self.operand()
            self.expect(",")
            if self.peek("["):
                self.expect("[")
                members = [self.literal()]
                while self.peek(","):
                    self.expect(",")
                    members.append(self.literal())
                self.expect("]")
                right: object = members
            else:
                right = self.operand()
            data = {"op": op, "left": left, "right": right}
        else:
            raise ContractFormatError(f"{op!r} is outside the closed predicate grammar")
        self.expect(")")
        if isinstance(data.get("of"), list):
            data["of"] = [item for item in data["of"]]
        return cast(Predicate, _PREDICATE.validate_python(data))

    def literal(self) -> str | int | bool:
        self.skip()
        rest = self.text[self.position :]
        string = re.match(r'"(?:[^"\\]|\\.)*"', rest)
        if string is not None:
            self.position += string.end()
            return cast(str, json.loads(string.group(0)))
        number = re.match(r"-?[0-9]+(?![0-9.])", rest)
        if number is not None:
            self.position += number.end()
            return int(number.group(0))
        for word, value in (("true", True), ("false", False)):
            if rest.startswith(word):
                self.position += len(word)
                return value
        raise ContractFormatError(f"the predicate {self.text!r} has an invalid literal")

    def operand(self) -> dict[str, object]:
        self.skip()
        rest = self.text[self.position :]
        reference = re.match(r"\$(item\.fields|parameters)\.([a-z][a-z0-9_]*)", rest)
        if reference is not None:
            self.position += reference.end()
            if reference.group(1) == "item.fields":
                return {"field": reference.group(2)}
            return {"parameter": reference.group(2)}
        return {"literal": self.literal()}


def as_predicate(value: Predicate | str | Mapping[str, object]) -> Predicate:
    """A predicate from its call form or its structured form."""

    if isinstance(value, str):
        return parse_predicate(value)
    if isinstance(value, BaseModel):
        return value
    return cast(Predicate, _PREDICATE.validate_python(value))


__all__ = [
    "CONTRACT_ARTIFACT_FORMAT",
    "CONTRACT_PIN_ROLE",
    "CONTRACT_TYPE_INVALID",
    "CONTRACT_TYPE_REFUSED",
    "REFUSED_TYPE_NAMES",
    "AllOf",
    "AnyOf",
    "BoolType",
    "CaptureType",
    "Comparison",
    "Contract",
    "ContractField",
    "ContractFormatError",
    "ContractLawProblem",
    "ContractRef",
    "ContractTypeRefused",
    "DateTimeType",
    "DateType",
    "DecimalType",
    "DuplicateArm",
    "DurationType",
    "EnumOfType",
    "EnumType",
    "FieldAbsent",
    "FieldInvalid",
    "FieldOperand",
    "FieldPresent",
    "FieldResult",
    "FieldResultType",
    "FieldType",
    "IntType",
    "InvalidRecordArm",
    "KeyType",
    "ListType",
    "LiteralOperand",
    "Membership",
    "NestedType",
    "Not",
    "ParameterOperand",
    "Predicate",
    "Presence",
    "Problem",
    "ProblemCode",
    "RecordArm",
    "RecordType",
    "RecordValue",
    "RefusalType",
    "RefusalValue",
    "ResultType",
    "ResultValue",
    "StringType",
    "SubjectType",
    "Truth",
    "VariantType",
    "as_predicate",
    "assignable",
    "cast_record",
    "cast_value",
    "check_record",
    "check_value",
    "coerce_field_type",
    "contract_digest",
    "contract_identity",
    "contract_path",
    "contract_path_for_identity",
    "contract_path_parts",
    "contract_ref_for_identity",
    "contract_type_refusal",
    "edge_refusal",
    "evaluate_contract_law",
    "evaluate_predicate",
    "map_type_refs",
    "parse_contract",
    "parse_contract_ref",
    "parse_field_spelling",
    "parse_predicate",
    "parse_type",
    "pin_contract_refs",
    "records",
    "render_contract",
    "render_type",
    "type_refs",
    "validate_contract",
]
