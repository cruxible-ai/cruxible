"""CaptureKind layouts: the header fields and record sets one kind's documents carry.

A layout sits beside a CaptureKind's ``decoding``. ``header`` names
document-level fields; ``records`` names record sets, each with its path
(``at``: a JSON Pointer with at most one ``*``, a JSON Pointer naming one
record, or ``/*`` for every CSV data row), its key parts, its row Contract, its
field rules (the extract ``ExtractField``: ``at``, ``type``, ``parse``,
``project``, ``canonicalize``, ``presence``) and an optional ``count_at``. An
``extract`` names one record set and some header fields; core resolves it
against the layout into the full extract spec the engine reads.

A layout is a declaration read by authoring, by the binding check and by
extract. Capture bytes are never checked against it when a Capture is
admitted. A successor may only grow a layout (:func:`layout_successor_break`);
:func:`layout_covers` decides whether a producer's declared layout serves what
a reader's layout declares.

:class:`DeclaredCaptureKind` is a kind's name, decoding and layout together: the
copy a provider carries of each kind it accepts or returns, and the part of an
instance's kind a coverage check reads.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.capability_fit import FitResult, Unfit
from cruxible_client.contracts.captures import (
    DERIVED_CAPTURE_DECODINGS,
    CaptureCsvDecoding,
    CaptureDecoding,
)
from cruxible_client.contracts.law_problems import LawProblem
from cruxible_client.contracts.procedures.extract import ExtractField
from cruxible_client.contracts.provider_operations import ContractLookup
from cruxible_client.contracts.shapes import (
    ANY_CAPTURE_KIND,
    CaptureType,
    Contract,
    ContractField,
    ContractRef,
    FieldResultType,
    FieldType,
    ListType,
    all_field_refs,
    parse_contract_ref,
    type_refs,
)

MAX_LAYOUT_HEADER_FIELDS = 64
MAX_LAYOUT_RECORD_SETS = 64
MAX_LAYOUT_KEY_PARTS = 16
MAX_LAYOUT_FIELDS = 256
DECLARED_CAPTURE_KIND_DIGEST_DOMAIN = "cruxible-declared-capture-kind-v1"
#: How a coverage gap renders the side that lacks an entry.
ABSENT = "absent"

_LAYOUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_KIND_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")

LAYOUT_DECODING_UNSUPPORTED: Final = "cruxible.capture_kind.layout_decoding_unsupported"
LAYOUT_ROW_UNRESOLVED: Final = "cruxible.capture_kind.layout_row_unresolved"
LAYOUT_FIELD_UNKNOWN: Final = "cruxible.capture_kind.layout_field_unknown"
LAYOUT_FIELD_MISSING: Final = "cruxible.capture_kind.layout_field_missing"
LAYOUT_REF_UNRESOLVED: Final = "cruxible.capture_kind.layout_ref_unresolved"
LAYOUT_SUCCESSION_BREAK: Final = "cruxible.capture_kind.layout_succession_break"

#: Every code the layout law and the succession check return.
LayoutLawCode = Literal[
    "cruxible.capture_kind.layout_decoding_unsupported",
    "cruxible.capture_kind.layout_row_unresolved",
    "cruxible.capture_kind.layout_field_unknown",
    "cruxible.capture_kind.layout_field_missing",
    "cruxible.capture_kind.layout_ref_unresolved",
    "cruxible.capture_kind.layout_succession_break",
]


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


def _not_none(value: object) -> bool:
    return value is None


def _empty(value: object) -> bool:
    return not value


def _canonical(value: str, pattern: re.Pattern[str], *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value or not pattern.fullmatch(value):
        raise ValueError(f"{label} {value!r} is not a canonical lowercase identifier")
    return value


def validate_record_set_name(value: str) -> str:
    """A record set name: a canonical lowercase identifier of at most 64 characters."""

    return _canonical(value, _LAYOUT_NAME_RE, label="layout record set")


def validate_layout_field_name(value: str) -> str:
    """A header field, key part or row field name, as an extract field name."""

    return _canonical(value, _FIELD_NAME_RE, label="layout field")


def _sorted_rules(value: dict[str, ExtractField], *, label: str) -> dict[str, ExtractField]:
    for name in value:
        _canonical(name, _FIELD_NAME_RE, label=label)
    return dict(sorted(value.items(), key=lambda item: item[0].encode("utf-8")))


class LayoutRecordSet(_Shape):
    """One named record set: where its records are, their key, row Contract and field rules.

    ``count_at`` names a document field that states how many records the set
    holds; a read is complete only when it equals the records read.
    """

    at: str = Field(min_length=1, max_length=512)
    key: dict[str, ExtractField] = Field(
        default_factory=dict, max_length=MAX_LAYOUT_KEY_PARTS, exclude_if=_empty
    )
    row: ContractRef
    fields: dict[str, ExtractField] = Field(min_length=1, max_length=MAX_LAYOUT_FIELDS)
    count_at: str | None = Field(default=None, min_length=1, max_length=512, exclude_if=_not_none)

    @field_validator("row", mode="before")
    @classmethod
    def _row(cls, value: object) -> object:
        return parse_contract_ref(value) if isinstance(value, str) else value

    @field_validator("key")
    @classmethod
    def _key(cls, value: dict[str, ExtractField]) -> dict[str, ExtractField]:
        return _sorted_rules(value, label="layout key part")

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: dict[str, ExtractField]) -> dict[str, ExtractField]:
        return _sorted_rules(value, label="layout field")

    @model_validator(mode="after")
    def _shape(self) -> LayoutRecordSet:
        if self.at.count("*") > 1:
            raise ValueError("a record set's path has at most one '*'")
        overlap = set(self.key) & set(self.fields)
        if overlap:
            raise ValueError(f"a key part is not also a row field: {sorted(overlap)}")
        return self


class CaptureLayout(_Shape):
    """A CaptureKind's declared document shape: header fields and named record sets."""

    header: dict[str, ExtractField] = Field(
        default_factory=dict, max_length=MAX_LAYOUT_HEADER_FIELDS, exclude_if=_empty
    )
    records: dict[str, LayoutRecordSet] = Field(min_length=1, max_length=MAX_LAYOUT_RECORD_SETS)

    @field_validator("header")
    @classmethod
    def _header(cls, value: dict[str, ExtractField]) -> dict[str, ExtractField]:
        return _sorted_rules(value, label="layout header field")

    @field_validator("records")
    @classmethod
    def _records(cls, value: dict[str, LayoutRecordSet]) -> dict[str, LayoutRecordSet]:
        for name in value:
            validate_record_set_name(name)
        return dict(sorted(value.items(), key=lambda item: item[0].encode("utf-8")))

    def row_refs(self) -> tuple[ContractRef, ...]:
        """Every record set's row Contract, in record set order."""

        return tuple(item.row for item in self.records.values())


def layout_decoding_problem(layout: CaptureLayout, decoding: CaptureDecoding | None) -> str | None:
    """Why ``layout`` cannot sit on a kind with ``decoding``, or ``None``.

    A layout is read by extract, so the kind decodes as JSON or CSV. A CSV
    record set is every data row (``/*``), and a CSV is complete when its header
    row is present and every row reads: it declares no header fields and no
    count.
    """

    if decoding is None:
        return "a layout needs the kind's decoding (json or csv)"
    if decoding.format not in DERIVED_CAPTURE_DECODINGS:
        return f"core reads a layout over json or csv, not {decoding.format}"
    if decoding.format == "csv":
        if layout.header:
            return "a CSV layout declares no header fields"
        for name, item in layout.records.items():
            if item.at != "/*":
                return f"CSV record set {name!r} is at /*: every data row"
            if item.count_at is not None:
                return f"CSV record set {name!r} declares no count_at"
    return None


class DeclaredCaptureKind(_Shape):
    """A CaptureKind's name, decoding and layout: what a provider copies and promises.

    A raw kind may declare neither: a provider then names it without promising
    a document shape. A layout needs a JSON or CSV decoding.
    """

    name: str
    decoding: CaptureDecoding | None = Field(default=None, exclude_if=_not_none)
    layout: CaptureLayout | None = Field(default=None, exclude_if=_not_none)

    _name = field_validator("name")(
        lambda value: _canonical(value, _KIND_NAME_RE, label="CaptureKind name")
    )

    @model_validator(mode="after")
    def _layout(self) -> DeclaredCaptureKind:
        if self.layout is not None:
            problem = layout_decoding_problem(self.layout, self.decoding)
            if problem is not None:
                raise ValueError(problem)
        return self


def declared_capture_kind_digest(kind: DeclaredCaptureKind) -> ArtifactDigest:
    """The digest of one declared copy (name, decoding, layout), for ``built_against``."""

    return typed_digest(
        ArtifactDigest, DECLARED_CAPTURE_KIND_DIGEST_DOMAIN, kind.model_dump(mode="json")
    )


# -- the layout law ------------------------------------------------------------------------


def capture_kinds_of(field_type: FieldType) -> Iterator[str]:
    """Every named CaptureKind (``capture<K>``, K not ``*``) one type names directly.

    Through lists and field results; a nested Contract's own fields are read
    where that Contract is reached.
    """

    if isinstance(field_type, CaptureType):
        if field_type.capture_kind != ANY_CAPTURE_KIND:
            yield field_type.capture_kind
    elif isinstance(field_type, ListType):
        yield from capture_kinds_of(field_type.item)
    elif isinstance(field_type, FieldResultType):
        yield from capture_kinds_of(field_type.value)


def layout_rule_types(layout: CaptureLayout) -> Iterator[tuple[str, FieldType]]:
    """Each field rule's effective type with its entry path: header fields, then each
    record set's key parts and field rules."""

    for name, rule in layout.header.items():
        yield f"header.{name}", rule.field_type()
    for set_name, item in layout.records.items():
        for part, rule in item.key.items():
            yield f"records.{set_name}.key.{part}", rule.field_type()
        for field, rule in item.fields.items():
            yield f"records.{set_name}.fields.{field}", rule.field_type()


def evaluate_layout_law(
    layout: CaptureLayout,
    *,
    decoding: CaptureDecoding | None,
    resolve: ContractLookup,
    capture_kind_exists: Callable[[str], bool] | None = None,
) -> tuple[LawProblem, ...]:
    """Judge one layout against its kind's decoding and the definitions it names.

    The kind decodes as JSON or CSV (and a CSV layout has the CSV shape); every
    record set's row resolves (by exact digest when pinned); its field rules
    are exactly its row Contract's fields, so each record it yields is a whole
    ``record<row>`` (an optional row field still needs a rule; key parts are a
    separate map and are not row fields). Every Contract a rule's type names
    resolves, and so does every Contract those and the rows name, transitively
    (``layout_ref_unresolved``). With ``capture_kind_exists``, every
    ``capture<K>`` a rule's type or a reached Contract names is a CaptureKind
    here. Key types are judged where the kind is accepted, against the
    instance's SubjectKinds.
    """

    problems: list[LawProblem] = []
    decoding_problem = layout_decoding_problem(layout, decoding)
    if decoding_problem is not None:
        problems.append(LawProblem(LAYOUT_DECODING_UNSUPPORTED, decoding_problem))
    pending: deque[tuple[ContractRef, str]] = deque()
    kinds: dict[str, str] = {}

    def reached(contract: Contract, where: str) -> None:
        through = f"{where} via {contract.ref.spelling}"
        pending.extend((nested, through) for nested in all_field_refs(contract))
        for field_name, declared in contract.fields.items():
            for kind in capture_kinds_of(declared.type):
                kinds.setdefault(kind, f"{through}.{field_name}")

    for name, item in layout.records.items():
        row = resolve(item.row)
        if row is None:
            problems.append(
                LawProblem(
                    LAYOUT_ROW_UNRESOLVED,
                    f"record set {name!r}: row {item.row.spelling} is not an accepted Contract"
                    + (" at its pinned digest." if item.row.digest is not None else "."),
                )
            )
            continue
        reached(row, f"records.{name}.row")
        unknown = sorted(set(item.fields) - set(row.fields))
        if unknown:
            problems.append(
                LawProblem(
                    LAYOUT_FIELD_UNKNOWN,
                    f"record set {name!r} reads fields its row Contract {item.row.spelling} "
                    f"does not declare: {unknown}",
                )
            )
        for field in sorted(set(row.fields) - set(item.fields)):
            problems.append(
                LawProblem(
                    LAYOUT_FIELD_MISSING,
                    f"records.{name}.fields.{field}: row Contract {item.row.spelling} declares "
                    f"{field!r}, which the record set gives no field rule; every row field needs "
                    "a rule, so each record is a whole record of its row",
                )
            )
    for entry, field_type in layout_rule_types(layout):
        pending.extend((ref, entry) for ref in type_refs(field_type))
        for kind in capture_kinds_of(field_type):
            kinds.setdefault(kind, entry)
    seen: set[tuple[str, int, str | None]] = set()
    while pending:
        ref, where = pending.popleft()
        key = (ref.name, ref.version, ref.digest)
        if key in seen:
            continue
        seen.add(key)
        found = resolve(ref)
        if found is None:
            problems.append(
                LawProblem(
                    LAYOUT_REF_UNRESOLVED,
                    f"{where}: {ref.spelling} is not an accepted Contract"
                    + (" at its pinned digest." if ref.digest is not None else "."),
                )
            )
            continue
        reached(found, where)
    if capture_kind_exists is not None:
        for kind, where in kinds.items():
            if not capture_kind_exists(kind):
                problems.append(
                    LawProblem(
                        LAYOUT_REF_UNRESOLVED,
                        f"{where}: capture<{kind}> names no CaptureKind here.",
                    )
                )
    return tuple(dict.fromkeys(problems))


# -- succession: a layout may only grow ----------------------------------------------------


def layout_succession_message(entry: str) -> str:
    """The refusal text for a successor that changes ``entry`` instead of growing."""

    return (
        f"layout entry {entry} changes on succession; a layout may only add header fields, "
        "record sets, and optional row fields: declare a new kind"
    )


def _same_ref(left: ContractRef, right: ContractRef) -> bool:
    return (left.name, left.version, left.digest) == (right.name, right.version, right.digest)


def _field_shape(field: ContractField) -> dict[str, object]:
    return field.model_dump(mode="json", exclude={"description"})


def layout_successor_break(
    previous: CaptureLayout | None,
    successor: CaptureLayout | None,
    *,
    resolve: ContractLookup,
) -> str | None:
    """Name the first layout entry a kind's successor changes instead of growing, or ``None``.

    A successor may add header fields and record sets, and may move a record
    set's row to a later version of the same Contract that only adds optional
    fields, with field rules for them. Every path, key, field rule, type and
    count already declared stays as it is. A kind that had no layout may gain
    one; a layout cannot be removed. ``resolve`` reads both row versions when a
    row moves.
    """

    if previous is None:
        return None
    if successor is None:
        return "layout"
    for name, rule in previous.header.items():
        if successor.header.get(name) != rule:
            return f"header.{name}"
    for name, old in previous.records.items():
        entry = f"records.{name}"
        new = successor.records.get(name)
        if new is None:
            return entry
        if new.at != old.at:
            return f"{entry}.at"
        if new.key != old.key:
            return f"{entry}.key"
        if new.count_at != old.count_at:
            return f"{entry}.count_at"
        for field, rule in old.fields.items():
            if new.fields.get(field) != rule:
                return f"{entry}.fields.{field}"
        added_rules = set(new.fields) - set(old.fields)
        if _same_ref(old.row, new.row):
            if added_rules:
                return f"{entry}.fields.{sorted(added_rules)[0]}"
            continue
        if new.row.name != old.row.name or new.row.version <= old.row.version:
            return f"{entry}.row"
        old_row, new_row = resolve(old.row), resolve(new.row)
        if old_row is None or new_row is None:
            return f"{entry}.row"
        for field, declared in old_row.fields.items():
            moved = new_row.fields.get(field)
            if moved is None or _field_shape(moved) != _field_shape(declared):
                return f"{entry}.row.{field}"
        added_fields = set(new_row.fields) - set(old_row.fields)
        for field in sorted(added_fields):
            if not new_row.fields[field].optional:
                return f"{entry}.row.{field}"
        if added_rules != added_fields:
            missing = sorted(added_rules ^ added_fields)
            return f"{entry}.fields.{missing[0]}"
    return None


# -- coverage: does a producer's layout serve a reader's? ----------------------------------


class LayoutGap(_Shape):
    """The first layout entry a producer's declaration does not cover, with both sides.

    ``entry`` is a dotted path inside the layout (``records.affected.count_at``);
    ``producer`` and ``reader`` render each side's declaration, or ``absent``.
    """

    entry: str = Field(min_length=1)
    producer: str
    reader: str


#: The fit rule for one field type (producer's type as sender, reader's as receiver).
TypeFit = Callable[[FieldType, FieldType], FitResult]
#: The fit rule for two row Contracts (producer's row as sender, reader's as receiver).
RowFit = Callable[[ContractRef, ContractRef], FitResult]


def render_field_rule(rule: ExtractField) -> str:
    """One field rule as plain text: ``at /count, parse int``."""

    parts = [f"at {rule.at}"]
    for name in ("type", "parse", "project", "canonicalize", "presence"):
        value = getattr(rule, name)
        if value is not None:
            parts.append(f"{name} {value}")
    return ", ".join(parts)


def _policy(rule: ExtractField) -> tuple[object, ...]:
    return (rule.at, rule.parse, rule.project, rule.canonicalize, rule.presence)


def _rule_gap(
    entry: str, producer: ExtractField | None, reader: ExtractField, type_fits: TypeFit
) -> LayoutGap | None:
    if producer is None:
        return LayoutGap(entry=entry, producer=ABSENT, reader=render_field_rule(reader))
    if _policy(producer) != _policy(reader):
        return LayoutGap(
            entry=entry, producer=render_field_rule(producer), reader=render_field_rule(reader)
        )
    fit = type_fits(producer.field_type(), reader.field_type())
    if isinstance(fit, Unfit):
        return LayoutGap(entry=f"{entry}.type", producer=fit.sender, reader=fit.receiver)
    return None


def _render_decoding_value(value: object) -> str:
    if value is None:
        return ABSENT
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _decoding_gap(
    producer: CaptureDecoding | None, reader: CaptureDecoding | None
) -> LayoutGap | None:
    """The first decoding entry that differs: format, then encoding, then each CSV dialect field.

    Bytes are read with exactly the kind's declarations, so a producer that
    encodes or splits rows differently yields a different read (ruling O1).
    Resource limits are not compared.
    """

    produced_format = ABSENT if producer is None else producer.format
    read_format = ABSENT if reader is None else reader.format
    if produced_format != read_format:
        return LayoutGap(entry="decoding.format", producer=produced_format, reader=read_format)
    if producer is None or reader is None:
        return None
    if producer.encoding != reader.encoding:
        return LayoutGap(
            entry="decoding.encoding", producer=producer.encoding, reader=reader.encoding
        )
    if producer.csv is None or reader.csv is None:
        return None
    for field in CaptureCsvDecoding.model_fields:
        offered, wanted = getattr(producer.csv, field), getattr(reader.csv, field)
        if offered != wanted:
            return LayoutGap(
                entry=f"decoding.dialect.{field}",
                producer=_render_decoding_value(offered),
                reader=_render_decoding_value(wanted),
            )
    return None


def _may_reach_capture(field_type: FieldType) -> bool:
    """Whether a rule type names a ``capture<J>`` or a Contract (whose fields may)."""

    return next(capture_kinds_of(field_type), None) is not None or bool(type_refs(field_type))


def _shared_layout_gap(
    layout: CaptureLayout, *, type_fits: TypeFit, row_fits: RowFit
) -> LayoutGap | None:
    """Equal declarations of one kind: ask only what can reach another kind's layout.

    Header rules whose type may reach a Capture are fitted to themselves; a
    record set with such a rule fits its row Contract to itself, then those
    rules. Everything else is the same declaration and is not compared.
    """

    for name, rule in layout.header.items():
        field_type = rule.field_type()
        if _may_reach_capture(field_type):
            fit = type_fits(field_type, field_type)
            if isinstance(fit, Unfit):
                return LayoutGap(
                    entry=f"header.{name}.type", producer=fit.sender, reader=fit.receiver
                )
    for set_name, item in layout.records.items():
        entry = f"records.{set_name}"
        reaching = [
            (f"{entry}.{group}.{name}", field_type)
            for group, rules in (("key", item.key), ("fields", item.fields))
            for name, rule in rules.items()
            if _may_reach_capture(field_type := rule.field_type())
        ]
        if not reaching:
            continue
        row = row_fits(item.row, item.row)
        if isinstance(row, Unfit):
            return LayoutGap(
                entry=f"{entry}.row.{row.path}", producer=row.sender, reader=row.receiver
            )
        for rule_entry, field_type in reaching:
            fit = type_fits(field_type, field_type)
            if isinstance(fit, Unfit):
                return LayoutGap(
                    entry=f"{rule_entry}.type", producer=fit.sender, reader=fit.receiver
                )
    return None


def layout_covers(
    producer: DeclaredCaptureKind,
    reader: DeclaredCaptureKind,
    *,
    type_fits: TypeFit,
    row_fits: RowFit,
) -> LayoutGap | None:
    """``None`` when the producer's declared layout covers the reader's, else the first gap.

    Same kind; when the reader declares a layout, the producer declares one too,
    with the same decoding format, the same encoding and, for CSV, the same
    dialect (every field of it); every header field of the reader's is in the
    producer's with the same path and rule and a fitting type; every record set
    of the reader's is in the producer's with the same path, the same key
    parts, a row Contract that fits, and every field rule of the reader's
    present (the same rule, with a fitting type); a count the reader declares,
    the producer declares at the same path. Two equal declarations cover each
    other in everything the kind itself declares, but a rule type that names a
    ``capture<J>`` or a Contract (which may reach one) still asks its fit, since
    the two sides' layouts of ``J`` are not part of this kind
    (:func:`_shared_layout_gap`); a layout with no such type covers at once.
    """

    if producer.name != reader.name:
        return LayoutGap(entry="name", producer=producer.name, reader=reader.name)
    wanted = reader.layout
    if wanted is None:
        return None
    if producer == reader:
        return _shared_layout_gap(wanted, type_fits=type_fits, row_fits=row_fits)
    offered = producer.layout
    if offered is None:
        return LayoutGap(
            entry="layout", producer=ABSENT, reader="record sets " + ", ".join(wanted.records)
        )
    gap = _decoding_gap(producer.decoding, reader.decoding)
    if gap is not None:
        return gap
    for name, rule in wanted.header.items():
        gap = _rule_gap(f"header.{name}", offered.header.get(name), rule, type_fits)
        if gap is not None:
            return gap
    for name, wanted_set in wanted.records.items():
        entry = f"records.{name}"
        offered_set = offered.records.get(name)
        if offered_set is None:
            return LayoutGap(entry=entry, producer=ABSENT, reader=f"at {wanted_set.at}")
        if offered_set.at != wanted_set.at:
            return LayoutGap(entry=f"{entry}.at", producer=offered_set.at, reader=wanted_set.at)
        for part in sorted(set(wanted_set.key) | set(offered_set.key)):
            offered_part, wanted_part = offered_set.key.get(part), wanted_set.key.get(part)
            if offered_part != wanted_part:
                return LayoutGap(
                    entry=f"{entry}.key.{part}",
                    producer=ABSENT if offered_part is None else render_field_rule(offered_part),
                    reader=ABSENT if wanted_part is None else render_field_rule(wanted_part),
                )
        if wanted_set.count_at is not None and offered_set.count_at != wanted_set.count_at:
            return LayoutGap(
                entry=f"{entry}.count_at",
                producer=offered_set.count_at or ABSENT,
                reader=wanted_set.count_at,
            )
        row = row_fits(offered_set.row, wanted_set.row)
        if isinstance(row, Unfit):
            return LayoutGap(
                entry=f"{entry}.row.{row.path}", producer=row.sender, reader=row.receiver
            )
        for field, rule in wanted_set.fields.items():
            gap = _rule_gap(
                f"{entry}.fields.{field}", offered_set.fields.get(field), rule, type_fits
            )
            if gap is not None:
                return gap
    return None


__all__ = [
    "ABSENT",
    "DECLARED_CAPTURE_KIND_DIGEST_DOMAIN",
    "LAYOUT_DECODING_UNSUPPORTED",
    "LAYOUT_FIELD_MISSING",
    "LAYOUT_FIELD_UNKNOWN",
    "LAYOUT_REF_UNRESOLVED",
    "LAYOUT_ROW_UNRESOLVED",
    "LAYOUT_SUCCESSION_BREAK",
    "MAX_LAYOUT_FIELDS",
    "MAX_LAYOUT_HEADER_FIELDS",
    "MAX_LAYOUT_KEY_PARTS",
    "MAX_LAYOUT_RECORD_SETS",
    "CaptureLayout",
    "DeclaredCaptureKind",
    "LayoutGap",
    "LayoutLawCode",
    "LayoutRecordSet",
    "RowFit",
    "TypeFit",
    "capture_kinds_of",
    "declared_capture_kind_digest",
    "evaluate_layout_law",
    "layout_covers",
    "layout_decoding_problem",
    "layout_rule_types",
    "layout_succession_message",
    "layout_successor_break",
    "render_field_rule",
    "validate_layout_field_name",
    "validate_record_set_name",
]
