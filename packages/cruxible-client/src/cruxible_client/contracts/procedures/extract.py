"""The ``extract`` node: one Capture's bytes read as typed records.

``capture`` names a ``capture<K>`` field of an earlier acquiring call
(``$steps.<alias>.<field>``); the field's accepted CaptureContract declares how
its bytes decode. ``records`` selects the records (a JSON Pointer with one
``*``, a JSON Pointer naming one record, or ``/*`` for every CSV data
row), ``key`` reads each record's key,
``row`` is the Contract whose fields the node reads, and ``header`` reads
document-level fields. Each record becomes one ``result``: a typed
``record`` with its locator, an ``invalid_record`` (key or enumeration
problems), or a ``duplicate``.

Key fields are plain scalars here: ``key<K>`` and ``key_part<K>`` stay refused
until SubjectKind identity rules exist.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from cruxible_client.contracts.canonical import ArtifactDigest, normalize_canonical, typed_digest
from cruxible_client.contracts.procedures.model_base import (
    _ALIAS_RE,
    _canonical_identifier,
    _StrictProcedureModel,
)
from cruxible_client.contracts.shapes import (
    BoolType,
    ContractRef,
    DateTimeType,
    DateType,
    DecimalType,
    FieldType,
    IntType,
    KeyType,
    StringType,
    parse_type,
)

#: The immutable extractor version that reads each decoded format.
EXTRACTOR_VERSIONS: dict[str, str] = {
    "json": "json-pointer-v1",
    "csv": "csv-cell-v1",
    "text": "unsupported",
    "bytes": "unsupported",
}

Parse = Literal["date", "datetime", "int", "decimal", "bool"]
_PARSE_TYPES: dict[str, FieldType] = {
    "date": DateType(),
    "datetime": DateTimeType(),
    "int": IntType(),
    "decimal": DecimalType(),
    "bool": BoolType(),
}


class ExtractField(_StrictProcedureModel):
    """Where one field is read, and how its lexeme becomes a typed value.

    ``at`` is a JSON Pointer relative to the record (or to the document, for a
    header field), or a CSV column name. ``parse`` converts a text lexeme
    (``date``, ``datetime``, ``int``, ``decimal``, ``bool``); ``type`` states
    the field's type where no row Contract does (keys and header fields);
    ``canonicalize: claim_type`` matches an enum member case-insensitively.
    ``presence`` overrides the row Contract's optionality.
    """

    at: str = Field(min_length=1, max_length=512)
    type: str | None = Field(default=None, exclude_if=lambda value: value is None)
    parse: Parse | None = Field(default=None, exclude_if=lambda value: value is None)
    canonicalize: Literal["claim_type"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    presence: Literal["required", "optional"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("type")
    @classmethod
    def _type(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = parse_type(value)
            if isinstance(parsed, KeyType) or value.startswith("key_part"):
                raise ValueError(
                    "key<K> and key_part<K> extract keys wait for SubjectKind identity rules; "
                    "type a key with a plain scalar type"
                )
        return value

    def field_type(self) -> FieldType:
        """The declared type; else the parse's type; else a string."""

        if self.type is not None:
            return parse_type(self.type)
        if self.parse is not None:
            return _PARSE_TYPES[self.parse]
        return StringType()


class CompleteWhen(_StrictProcedureModel):
    """The extract is complete only when the count stated at ``count_at`` is the records read."""

    count_at: str = Field(min_length=1, max_length=512)


class ExtractNode(_StrictProcedureModel):
    """Read one Capture's records, typed, with per-field results and a completeness verdict."""

    kind: Literal["extract"] = "extract"
    node_id: str
    capture: str
    records: str = Field(min_length=1, max_length=512)
    complete_when: CompleteWhen | None = Field(default=None, exclude_if=lambda value: value is None)
    key: dict[str, ExtractField] = Field(default_factory=dict, exclude_if=lambda value: not value)
    duplicates: Literal["refuse_run", "refuse_key", "first"] = "refuse_key"
    only_keys: object = Field(default=None, exclude_if=lambda value: value is None)
    row: ContractRef
    fields: dict[str, ExtractField] = Field(min_length=1, max_length=256)
    header: dict[str, ExtractField] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    as_: str = Field(alias="as")
    next: str | None = None

    _only_keys = field_validator("only_keys", mode="before")(normalize_canonical)

    @field_validator("capture")
    @classmethod
    def _capture(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != "$steps":
            raise ValueError("an extract reads one capture field: $steps.<alias>.<field>")
        for part in parts[1:]:
            _canonical_identifier(part, _ALIAS_RE, label="extract capture reference")
        return value

    @field_validator("key", "fields", "header")
    @classmethod
    def _names(cls, value: dict[str, ExtractField]) -> dict[str, ExtractField]:
        for name in value:
            _canonical_identifier(name, _ALIAS_RE, label="extract field")
        return dict(sorted(value.items(), key=lambda item: item[0].encode("utf-8")))

    @model_validator(mode="after")
    def _shape(self) -> ExtractNode:
        if self.records.count("*") > 1:
            raise ValueError("an extract's records selector has at most one '*'")
        if self.only_keys is not None and not self.key:
            raise ValueError("only_keys selects by key; declare the extract's key")
        overlap = set(self.key) & set(self.fields)
        if overlap:
            raise ValueError(f"a key field is not also a row field: {sorted(overlap)}")
        return self

    @property
    def capture_alias(self) -> str:
        return self.capture.split(".")[1]

    @property
    def capture_field(self) -> str:
        return self.capture.split(".")[2]


def extract_spec_digest(value: object) -> str:
    """A cache key for one declaration: the canonical digest of its JSON form."""

    from pydantic import BaseModel

    def dumped(item: object) -> object:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json", by_alias=True)
        if isinstance(item, dict):
            return {str(key): dumped(member) for key, member in item.items()}
        if isinstance(item, list | tuple):
            return [dumped(member) for member in item]
        return item

    return typed_digest(
        ArtifactDigest, "cruxible-extract-spec-v1", {"declaration": dumped(value)}
    ).tagged


__all__ = [
    "EXTRACTOR_VERSIONS",
    "CompleteWhen",
    "ExtractField",
    "ExtractNode",
    "Parse",
    "extract_spec_digest",
]
