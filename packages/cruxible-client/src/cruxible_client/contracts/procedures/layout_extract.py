"""The ``extract`` node that reads through its CaptureKind's layout.

The kind declares the document's shape (``capture_layouts``); the node names
what it reads and its own read policy: ``records`` (one record set of the
layout), ``header`` (some of the layout's header fields), ``duplicates`` and
``only_keys``. A node reads every field of its record set: there are no field
subsets. One override is allowed: a key part the layout declares as a plain
string may be given a Subject key type (``key<K>`` or ``key_part<K:p>``) by the
node, since a shared kind cannot name an instance's SubjectKinds.

:func:`resolve_layout_extract` turns the node and the layout into the full
extract spec (:class:`~cruxible_client.contracts.procedures.extract.ExtractNode`)
the extract engine runs unchanged. :class:`LayoutExtractStep` is the authored
spelling of the same node.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, field_validator

from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.capture_layouts import (
    CaptureLayout,
    validate_layout_field_name,
    validate_record_set_name,
)
from cruxible_client.contracts.procedures.authored_base import (
    StepId,
    StepOptions,
    Template,
)
from cruxible_client.contracts.procedures.extract import CompleteWhen, ExtractField, ExtractNode
from cruxible_client.contracts.procedures.model_base import (
    _ALIAS_RE,
    _canonical_identifier,
    _StrictProcedureModel,
)
from cruxible_client.contracts.refs import Ref
from cruxible_client.contracts.shapes import (
    Contract,
    ContractRef,
    KeyPartType,
    KeyType,
    StringType,
    parse_contract_ref,
    parse_type,
)

#: The most header fields one node selects.
MAX_EXTRACT_HEADER = 64

EXTRACT_LAYOUT_UNDECLARED: Final = "cruxible.procedure.extract_layout_undeclared"
EXTRACT_RECORDS_UNKNOWN: Final = "cruxible.procedure.extract_records_unknown"
EXTRACT_HEADER_UNKNOWN: Final = "cruxible.procedure.extract_header_unknown"
EXTRACT_KEY_OVERRIDE_INVALID: Final = "cruxible.procedure.extract_key_override_invalid"
EXTRACT_RECORDS_UNKEYED: Final = "cruxible.procedure.extract_records_unkeyed"
#: The kind's layout no longer offers the row an accepted extract pinned (ruling L1).
EXTRACT_ROW_UNAVAILABLE: Final = "cruxible.procedure.extract_row_unavailable"
#: The existing code for an extract over a Capture list that selects a header or a count (a5).
EXTRACT_FORMAT_UNSUPPORTED: Final = "cruxible.procedure.extract_format_unsupported"

#: Every code resolving a node against its kind's layout can refuse with.
LayoutExtractRefusalCode = Literal[
    "cruxible.procedure.extract_layout_undeclared",
    "cruxible.procedure.extract_records_unknown",
    "cruxible.procedure.extract_header_unknown",
    "cruxible.procedure.extract_key_override_invalid",
    "cruxible.procedure.extract_records_unkeyed",
    "cruxible.procedure.extract_format_unsupported",
    "cruxible.procedure.extract_row_unavailable",
]


class LayoutExtractRefused(ValueError):
    """The node does not resolve against its kind's layout."""

    def __init__(self, code: LayoutExtractRefusalCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class KeyTypeOverride(_StrictProcedureModel):
    """The one override a node may make: a Subject key type for a plain-string key part."""

    type: str = Field(min_length=1, max_length=512)

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        if not isinstance(parse_type(value), KeyType | KeyPartType):
            raise ValueError("a key override names key<K> or key_part<K:p>")
        return value


def _header_names(value: object) -> object:
    if isinstance(value, list | tuple):
        names = tuple(value)
        for name in names:
            if not isinstance(name, str):
                raise ValueError("a header selection names header fields")
            validate_layout_field_name(name)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("header fields are listed sorted and once each")
        return names
    return value


def _key_overrides(value: dict[str, KeyTypeOverride]) -> dict[str, KeyTypeOverride]:
    for name in value:
        validate_layout_field_name(name)
    return dict(sorted(value.items(), key=lambda item: item[0].encode("utf-8")))


class LayoutExtractNode(_StrictProcedureModel):
    """Read one record set of a Capture through its kind's layout (stored node).

    ``capture`` is ``$steps.<alias>.<field>`` as on today's node; ``records``
    names a record set; ``header`` lists header fields, sorted. ``row`` is the
    record set's row Contract pin ``{name, version, digest}``, added when the
    Procedure is accepted (ruling L1; never authored): every later read takes that
    row and only its fields, so a kind whose row grew does not change what the
    accepted Procedure extracts.
    """

    kind: Literal["extract"] = "extract"
    node_id: str
    capture: str
    records: str
    header: tuple[str, ...] = Field(
        default=(), max_length=MAX_EXTRACT_HEADER, exclude_if=lambda value: not value
    )
    key: dict[str, KeyTypeOverride] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    duplicates: Literal["refuse_run", "refuse_key", "first"] = "refuse_key"
    only_keys: object = Field(default=None, exclude_if=lambda value: value is None)
    row: ContractRef | None = Field(default=None, exclude_if=lambda value: value is None)
    as_: str = Field(alias="as")
    next: str | None = None

    _only_keys = field_validator("only_keys", mode="before")(normalize_canonical)
    _records = field_validator("records")(validate_record_set_name)

    @field_validator("row", mode="before")
    @classmethod
    def _row(cls, value: object) -> object:
        return parse_contract_ref(value) if isinstance(value, str) else value

    _header = field_validator("header", mode="before")(_header_names)
    _key = field_validator("key")(_key_overrides)

    @field_validator("capture")
    @classmethod
    def _capture(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != "$steps":
            raise ValueError("an extract reads one capture field: $steps.<alias>.<field>")
        for part in parts[1:]:
            _canonical_identifier(part, _ALIAS_RE, label="extract capture reference")
        return value

    @property
    def capture_alias(self) -> str:
        return self.capture.split(".")[1]

    @property
    def capture_field(self) -> str:
        return self.capture.split(".")[2]


class LayoutExtractStep(StepOptions):
    """The authored spelling: ``extract: <id>``, ``capture`` references ``<call>.<field>``."""

    extract: StepId
    capture: Ref
    records: str
    header: tuple[str, ...] = Field(default=(), max_length=MAX_EXTRACT_HEADER)
    key: dict[str, KeyTypeOverride] = Field(default_factory=dict)
    duplicates: Literal["refuse_run", "refuse_key", "first"] = "refuse_key"
    only_keys: Template = None

    _records = field_validator("records")(validate_record_set_name)
    _header = field_validator("header", mode="before")(_header_names)
    _key = field_validator("key")(_key_overrides)

    @field_validator("capture")
    @classmethod
    def _capture(cls, value: Ref) -> Ref:
        if value.root != "step" or len(value.path) != 1:
            raise ValueError("an extract reads one capture field: {$ref: step, step, path: [f]}")
        return value


def _plain_string(rule: ExtractField) -> bool:
    return (
        isinstance(rule.field_type(), StringType)
        and rule.parse is None
        and rule.project is None
        and rule.canonicalize is None
    )


def _pinned_fields(
    node: LayoutExtractNode,
    offered: ContractRef,
    rules: dict[str, ExtractField],
    pinned_row: Contract | None,
) -> dict[str, ExtractField]:
    """The layout's rules for exactly the pinned row's fields, or the refusal."""

    pinned = node.row
    assert pinned is not None

    def refuse(reason: str) -> LayoutExtractRefused:
        return LayoutExtractRefused(
            EXTRACT_ROW_UNAVAILABLE,
            f"extract {node.node_id!r} pins row {pinned.spelling}; {reason}; re-accept the "
            "Procedure against the kind's current layout",
        )

    if pinned_row is None or (pinned_row.name, pinned_row.version) != (
        pinned.name,
        pinned.version,
    ):
        raise refuse("that row Contract is not available")
    if offered.name != pinned.name or offered.version < pinned.version:
        raise refuse(f"record set {node.records!r} now reads row {offered.spelling}")
    if (
        offered.version == pinned.version
        and offered.digest is not None
        and pinned.digest is not None
        and offered.digest != pinned.digest
    ):
        raise refuse(f"record set {node.records!r} reads that row at another digest")
    missing = sorted(set(pinned_row.fields) - set(rules))
    if missing:
        raise refuse(f"record set {node.records!r} no longer reads fields {missing}")
    return {name: rules[name] for name in sorted(pinned_row.fields)}


def resolve_layout_extract(
    node: LayoutExtractNode,
    layout: CaptureLayout | None,
    *,
    reads_list: bool = False,
    pinned_row: Contract | None = None,
) -> ExtractNode:
    """The full extract spec ``node`` reads through ``layout``, or :class:`LayoutExtractRefused`.

    The record set supplies the path, key, row, field rules and count; the
    layout supplies the selected header fields; the node keeps its read policy
    and may give a plain-string key part a Subject key type. ``reads_list``: the
    capture field is a ``list<capture<K>>``, whose members' records merge, so
    the node selects no header field and a record set with no count (a5).

    A node that pins its row (``node.row``, ``pinned_row`` the Contract it pins)
    reads that row and only its fields: the record set's row must be the same
    Contract at the same or a later version (a layout only grows), with a rule
    for every pinned field; otherwise it refuses ``extract_row_unavailable``.
    """

    if layout is None:
        raise LayoutExtractRefused(
            EXTRACT_LAYOUT_UNDECLARED,
            f"extract {node.node_id!r} reads {node.capture!r}, whose CaptureKind declares no "
            "layout; declare the kind's layout",
        )
    record_set = layout.records.get(node.records)
    if record_set is None:
        raise LayoutExtractRefused(
            EXTRACT_RECORDS_UNKNOWN,
            f"extract {node.node_id!r} names record set {node.records!r}; the layout declares "
            + ", ".join(repr(name) for name in layout.records),
        )
    unknown = [name for name in node.header if name not in layout.header]
    if unknown:
        declared = ", ".join(repr(name) for name in layout.header) or "none"
        raise LayoutExtractRefused(
            EXTRACT_HEADER_UNKNOWN,
            f"extract {node.node_id!r} names header fields {unknown}; the layout declares "
            f"{declared}",
        )
    if reads_list and (node.header or record_set.count_at is not None):
        raise LayoutExtractRefused(
            EXTRACT_FORMAT_UNSUPPORTED,
            f"extract {node.node_id!r} reads the Capture list {node.capture!r}: an extract over "
            "a list merges its members' records and selects no header field and no record "
            "set with a count",
        )
    key = dict(record_set.key)
    for part, override in node.key.items():
        rule = key.get(part)
        if rule is None:
            raise LayoutExtractRefused(
                EXTRACT_KEY_OVERRIDE_INVALID,
                f"extract {node.node_id!r} overrides key part {part!r}, which record set "
                f"{node.records!r} does not declare",
            )
        if not _plain_string(rule):
            raise LayoutExtractRefused(
                EXTRACT_KEY_OVERRIDE_INVALID,
                f"extract {node.node_id!r} overrides key part {part!r}; only a key part the "
                "layout declares as a plain string may take a Subject key type",
            )
        key[part] = rule.model_copy(update={"type": override.type})
    if node.only_keys is not None and not key:
        raise LayoutExtractRefused(
            EXTRACT_RECORDS_UNKEYED,
            f"extract {node.node_id!r} selects only_keys from record set {node.records!r}, "
            "which declares no key",
        )
    row: ContractRef = record_set.row
    fields = dict(record_set.fields)
    if node.row is not None:
        row, fields = node.row, _pinned_fields(node, record_set.row, fields, pinned_row)
    return ExtractNode.model_validate(
        {
            "kind": "extract",
            "node_id": node.node_id,
            "capture": node.capture,
            "records": record_set.at,
            "complete_when": (
                None if record_set.count_at is None else CompleteWhen(count_at=record_set.count_at)
            ),
            "key": key,
            "duplicates": node.duplicates,
            "only_keys": node.only_keys,
            "row": row,
            "fields": fields,
            "header": {name: layout.header[name] for name in node.header},
            "as": node.as_,
            "next": node.next,
        }
    )


__all__ = [
    "EXTRACT_FORMAT_UNSUPPORTED",
    "EXTRACT_HEADER_UNKNOWN",
    "EXTRACT_KEY_OVERRIDE_INVALID",
    "EXTRACT_LAYOUT_UNDECLARED",
    "EXTRACT_RECORDS_UNKEYED",
    "EXTRACT_RECORDS_UNKNOWN",
    "EXTRACT_ROW_UNAVAILABLE",
    "MAX_EXTRACT_HEADER",
    "KeyTypeOverride",
    "LayoutExtractNode",
    "LayoutExtractRefusalCode",
    "LayoutExtractRefused",
    "LayoutExtractStep",
    "resolve_layout_extract",
]
