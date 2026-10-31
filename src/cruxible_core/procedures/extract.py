"""The ``extract`` runtime: decode one Capture and read its records, typed.

An ``extract`` node turns a Capture's exact bytes into ``result`` values (one
per record) by its accepted CaptureContract's ``decoding`` and the node's own
declarations: the record selector, the key, the row Contract's fields and the
header. Format conversion yields located values: each present value carries
the locator it was read at, and a field that cannot be read is ``absent`` or
``invalid``, never a guess.

Work is cached in three layers, each keyed by every declaration that affects
it, so a run pays for a declaration it has not evaluated before and nothing
else:

- D, decode and record index: ``(capture digest, extractor, decoding,
  record selector, key spec)`` -> record locators, canonical keys, and
  duplicate and key problems;
- E, evaluation: ``(D key, complete_when, header spec)`` -> completeness and
  the header's field results;
- F, field: ``(D key, field spec, locator)`` -> one field result, filled lazily.

A runtime ``only_keys`` is selected after layer D through its key index, at
``O(|only_keys|)``, and is never part of a cache key.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from cruxible_client.contracts.canonical import canonical_bytes, normalize_canonical
from cruxible_client.contracts.captures import CaptureDecoding
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedures.extract import (
    EXTRACTOR_VERSIONS,
    ExtractField,
    ExtractNode,
    extract_spec_digest,
)
from cruxible_client.contracts.shapes import (
    BoolType,
    Contract,
    ContractRef,
    DateType,
    DuplicateArm,
    EnumOfType,
    EnumResolver,
    EnumType,
    FieldAbsent,
    FieldInvalid,
    FieldPresent,
    FieldType,
    IntType,
    InvalidRecordArm,
    ListType,
    NestedType,
    Problem,
    RecordArm,
    RecordValue,
    ResultValue,
    StringType,
    check_value,
)
from cruxible_client.contracts.temporal import format_datetime, parse_datetime

FieldResult = FieldPresent | FieldAbsent | FieldInvalid

#: A path that names no value in the decoded document.
_MISSING = object()


class ExtractRefused(ExecutionError):
    """The node refuses: a decode limit, a header problem, or a duplicate under ``refuse_run``."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, object] | None = None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


# -- decoding -------------------------------------------------------------------------


def _decoded_bytes(content: bytes, decoding: CaptureDecoding) -> bytes:
    limit = decoding.limits.decoded_bytes
    if decoding.encoding == "identity":
        if len(content) > limit:
            raise ExtractRefused(
                "decoded_limit_exceeded",
                f"the Capture holds {len(content)} bytes, over its {limit}-byte decode limit",
                details={"limit": limit},
            )
        return content
    stream = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    out = bytearray()
    view = memoryview(content)
    step = 64 * 1024
    try:
        for start in range(0, len(view), step):
            out += stream.decompress(view[start : start + step], limit + 1 - len(out))
            if len(out) > limit:
                break
            while stream.unconsumed_tail and len(out) <= limit:
                out += stream.decompress(stream.unconsumed_tail, limit + 1 - len(out))
            if len(out) > limit:
                break
        if len(out) <= limit:
            out += stream.flush(limit + 1 - len(out))
    except zlib.error as exc:
        raise ExtractRefused("decode_invalid", f"the gzip stream does not decode: {exc}") from exc
    if len(out) > limit:
        raise ExtractRefused(
            "decoded_limit_exceeded",
            f"the gzip stream decodes past its {limit}-byte decode limit",
            details={"limit": limit},
        )
    if not stream.eof:
        raise ExtractRefused("decode_invalid", "the gzip stream is truncated")
    return bytes(out)


def _depth(value: object, limit: int) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        item, level = pending.pop()
        if isinstance(item, dict | list):
            if level > limit:
                raise ExtractRefused(
                    "decoded_limit_exceeded",
                    f"the decoded document nests deeper than its {limit}-level limit",
                    details={"limit": limit, "dimension": "depth"},
                )
            members = item.values() if isinstance(item, dict) else item
            pending.extend((member, level + 1) for member in members)


@dataclass(frozen=True)
class _CsvTable:
    header: tuple[str, ...] | None
    rows: tuple[tuple[str, ...], ...]
    #: Data rows read to the end of the bytes, the header present when declared.
    complete: bool


def _decode(content: bytes, decoding: CaptureDecoding) -> object:
    raw = _decoded_bytes(content, decoding)
    if decoding.format == "bytes":
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractRefused("decode_invalid", "the Capture is not UTF-8 text") from exc
    if decoding.format == "text":
        return text
    if decoding.format == "json":
        try:
            document = json.loads(text, parse_float=Decimal, parse_constant=_no_constant)
        except ValueError as exc:
            raise ExtractRefused("decode_invalid", f"the Capture is not JSON: {exc}") from exc
        _depth(document, decoding.limits.depth)
        return document
    dialect = decoding.csv
    assert dialect is not None  # the decoding law pairs csv with its dialect
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[tuple[str, ...]] = []
    header: tuple[str, ...] | None = None
    try:
        for line in reader:
            if (
                dialect.comment_prefix is not None
                and line
                and line[0].startswith(dialect.comment_prefix)
            ):
                continue
            if dialect.header and header is None:
                header = tuple(line)
                continue
            rows.append(tuple(line))
            if len(rows) > decoding.limits.records:
                raise ExtractRefused(
                    "decoded_limit_exceeded",
                    f"the CSV holds more than its {decoding.limits.records}-record limit",
                    details={"limit": decoding.limits.records, "dimension": "records"},
                )
    except csv.Error as exc:
        raise ExtractRefused("decode_invalid", f"the Capture is not CSV: {exc}") from exc
    return _CsvTable(
        header=header,
        rows=tuple(rows),
        complete=not dialect.header or header is not None,
    )


def _no_constant(name: str) -> object:
    raise ValueError(f"JSON constant {name} is not a value")


# -- locating values ------------------------------------------------------------------


def _pointer(path: str) -> tuple[str, ...]:
    """A JSON Pointer as its unescaped segments; a relative path reads the same way."""

    text = path[1:] if path.startswith("/") else path
    if not text:
        return ()
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in text.split("/"))


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _at(document: object, segments: Sequence[str]) -> object:
    current = document
    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list):
            if not segment.isdigit() or (len(segment) > 1 and segment[0] == "0"):
                return _MISSING
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _enumerate_json(document: object, selector: str) -> list[tuple[str, object]] | None:
    """``(locator, record)`` for every record the selector matches; ``None`` when none can."""

    segments = _pointer(selector)
    if "*" not in segments:
        record = _at(document, segments)
        if record is _MISSING:
            return None
        return [("".join("/" + _escape(part) for part in segments) or "/", record)]
    if segments.count("*") != 1:
        raise ExecutionError("a json-pointer-v1 record selector has at most one '*'")
    star = segments.index("*")
    container = _at(document, segments[:star])
    rest = segments[star + 1 :]
    prefix = "".join("/" + _escape(part) for part in segments[:star])
    if isinstance(container, list):
        members: Iterable[tuple[str, object]] = (
            (str(index), item) for index, item in enumerate(container)
        )
    elif isinstance(container, dict):
        members = ((str(key), item) for key, item in container.items())
    else:
        return None
    found: list[tuple[str, object]] = []
    for name, item in members:
        record = _at(item, rest)
        if record is _MISSING:
            continue
        locator = prefix + "/" + _escape(name) + "".join("/" + _escape(part) for part in rest)
        found.append((locator, record))
    return found


def _drop_nulls(value: object) -> object:
    """``json-pointer-v1`` reads a JSON null as absence, at every depth."""

    if isinstance(value, dict):
        return {key: _drop_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_nulls(item) for item in value]
    return value


# -- typing one lexeme ----------------------------------------------------------------


def _parsed(lexeme: object, parse: str | None) -> object:
    if parse is None or not isinstance(lexeme, str):
        return lexeme
    text = lexeme.strip()
    if parse == "date":
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return _UNPARSEABLE
    if parse == "datetime":
        try:
            return format_datetime(parse_datetime(text))
        except ValueError:
            return _UNPARSEABLE
    if parse == "int":
        try:
            return int(text, 10)
        except ValueError:
            return _UNPARSEABLE
    if parse == "decimal":
        try:
            number = Decimal(text)
        except InvalidOperation:
            return _UNPARSEABLE
        return number if number.is_finite() else _UNPARSEABLE
    if parse == "bool":
        return {"true": True, "false": False}.get(text, _UNPARSEABLE)
    raise ExecutionError(f"unknown parse {parse!r}")  # pragma: no cover - the model is closed


_UNPARSEABLE = object()


def _canonicalized(
    value: object, field_type: FieldType, canonicalize: str | None, enums: EnumResolver | None
) -> object:
    if canonicalize != "claim_type" or not isinstance(value, str):
        return value
    members: tuple[str, ...] | None = None
    if isinstance(field_type, EnumType):
        members = field_type.members
    elif isinstance(field_type, EnumOfType) and enums is not None:
        members = enums(field_type.claim_type)
    if not members:
        return value
    folded = value.strip().casefold()
    matches = [member for member in members if member.casefold() == folded]
    return matches[0] if len(matches) == 1 else value


#: A field result as the extractor holds it: ``("present", value, at)``,
#: ``("absent",)`` or ``("invalid", code, field, lexeme)``. Models are built
#: only for a caller that asks for them (``ExtractOutcome.records``), so a
#: record costs no model construction on the run path.
Held = tuple[Any, ...]
_ABSENT: Held = ("absent",)


def _lexeme(value: object) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:256]


def _bad(code: str, field: str | None, lexeme: object = None) -> Held:
    return ("invalid", code, field, _lexeme(lexeme))


def _in_range(value: Any, low: Any, high: Any) -> bool:
    return (low is None or value >= low) and (high is None or value <= high)


def _check(
    value: object,
    field_type: FieldType,
    *,
    at: str,
    field: str | None,
    resolve: Callable[[ContractRef], Contract | None] | None,
    enums: EnumResolver | None,
) -> Held:
    """``check_value`` without model construction, for the types records carry.

    It agrees with :func:`check_value` type for type; any other type is checked
    by ``check_value`` itself.
    """

    if isinstance(field_type, StringType):
        if not isinstance(value, str):
            return _bad("type_mismatch", field, value)
        if unicodedata.normalize("NFC", value) != value:
            return _bad("canonical_form_failed", field, value)
        if not _in_range(len(value), field_type.min_len, field_type.max_len):
            return _bad("out_of_range", field, value)
        if field_type.pattern is not None and re.fullmatch(field_type.pattern, value) is None:
            return _bad("pattern_mismatch", field, value)
        return ("present", value, at)
    if isinstance(field_type, IntType):
        if isinstance(value, bool) or not isinstance(value, int):
            return _bad("type_mismatch", field, value)
        if not _in_range(value, field_type.min, field_type.max):
            return _bad("out_of_range", field, value)
        return ("present", value, at)
    if isinstance(field_type, BoolType):
        return (
            ("present", value, at)
            if isinstance(value, bool)
            else _bad("type_mismatch", field, value)
        )
    if isinstance(field_type, EnumType | EnumOfType):
        if not isinstance(value, str):
            return _bad("type_mismatch", field, value)
        members = (
            field_type.members
            if isinstance(field_type, EnumType)
            else None
            if enums is None
            else enums(field_type.claim_type)
        )
        if members is None or value not in members:
            return _bad("not_in_enum", field, value)
        return ("present", value, at)
    if isinstance(field_type, DateType):
        if isinstance(value, date) and not isinstance(value, datetime):
            return ("present", value, at)
        if not isinstance(value, str):
            return _bad("type_mismatch", field, value)
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return _bad("type_mismatch", field, value)
        if parsed.isoformat() != value:
            return _bad("canonical_form_failed", field, value)
        return ("present", parsed, at)
    if isinstance(field_type, ListType):
        if not isinstance(value, list | tuple):
            return _bad("type_mismatch", field, value)
        if len(value) > field_type.max_items:
            return _bad("out_of_range", field, len(value))
        items = []
        for index, member in enumerate(value):
            checked = _check(
                member,
                field_type.item,
                at=f"{at}/{index}",
                field=f"{field or ''}[{index}]",
                resolve=resolve,
                enums=enums,
            )
            if checked[0] == "invalid":
                return checked
            items.append(checked[1])
        return ("present", items, at)
    if isinstance(field_type, NestedType):
        if not isinstance(value, Mapping):
            return _bad("type_mismatch", field, value)
        target = None if resolve is None else resolve(field_type.contract)
        if target is None:
            return _bad("type_mismatch", field, value)
        for name, declared in target.fields.items():
            if name not in value:
                if not declared.optional:
                    return _bad("absent_required", f"{field or ''}.{name}")
                continue
            checked = _check(
                value[name],
                declared.type,
                at=f"{at}/{name}",
                field=name,
                resolve=resolve,
                enums=enums,
            )
            if checked[0] == "invalid":
                return ("invalid", checked[1], f"{field or ''}.{checked[2] or name}", checked[3])
        return ("present", dict(value), at)
    result = check_value(value, field_type, at=at, field=field, resolve=resolve, enum_members=enums)
    if isinstance(result, FieldPresent):
        return ("present", result.value, result.at)
    problem = result.problem
    return ("invalid", problem.code, problem.field, problem.lexeme)


def _type_value(
    lexeme: object,
    declared: ExtractField,
    field_type: FieldType,
    *,
    name: str,
    at: str,
    required: bool,
    enums: EnumResolver | None,
    resolve: Callable[[ContractRef], Contract | None] | None,
) -> Held:
    if lexeme is _MISSING or lexeme is None or lexeme == "" and declared.parse is not None:
        return _bad("absent_required", name) if required else _ABSENT
    value = _parsed(lexeme, declared.parse)
    if value is _UNPARSEABLE:
        return _bad("type_mismatch", name, lexeme)
    value = _canonicalized(_drop_nulls(value), field_type, declared.canonicalize, enums)
    return _check(value, field_type, at=at, field=name, resolve=resolve, enums=enums)


def held_result(held: Held) -> FieldResult:
    """The model form of one held field result."""

    if held[0] == "present":
        return FieldPresent(value=held[1], at=held[2])
    if held[0] == "absent":
        return FieldAbsent()
    return FieldInvalid(problem=Problem(code=held[1], field=held[2], lexeme=held[3]))


def wire_value(value: object) -> object:
    """A typed value as canonical JSON: dates and moments as text, decimals as text."""

    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
        return "0" if text in {"-0", ""} else text
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [wire_value(item) for item in value]
    return value


def held_wire(held: Held) -> dict[str, object]:
    """One held field result as canonical JSON, the shape ``field_result`` values take."""

    if held[0] == "present":
        return {"tag": "present", "value": wire_value(held[1]), "at": held[2]}
    if held[0] == "absent":
        return {"tag": "absent"}
    problem: dict[str, object] = {"code": held[1]}
    if held[2] is not None:
        problem["field"] = held[2]
    if held[3] is not None:
        problem["lexeme"] = held[3]
    return {"tag": "invalid", "problem": problem}


def field_result_wire(result: FieldResult) -> dict[str, object]:
    if isinstance(result, FieldPresent):
        return {"tag": "present", "value": wire_value(result.value), "at": result.at}
    return result.model_dump(mode="json")


# -- the cache ------------------------------------------------------------------------


@dataclass
class ExtractCacheStats:
    """What the cache did, for the tests that hold it to its cost."""

    decodes: int = 0
    d_hits: int = 0
    evaluations: int = 0
    e_hits: int = 0
    field_reads: int = 0
    f_hits: int = 0

    def reset(self) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, 0)


STATS = ExtractCacheStats()


@dataclass
class _Index:
    """Layer D: one decoded Capture's records under one selector and key spec."""

    records: list[tuple[str, object]]
    keys: dict[str, dict[str, object]]
    key_problems: dict[str, tuple[Problem, ...]]
    duplicate_of: dict[str, str]
    by_key: dict[bytes, list[str]]
    header_source: object
    csv_complete: bool
    selector_matched: bool


@dataclass
class _Evaluation:
    """Layer E: completeness and header results."""

    complete_when_held: bool
    header: dict[str, Held]


class _Lru(OrderedDict[Any, Any]):
    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.capacity = capacity

    def lookup(self, key: Any) -> Any:
        value = self.get(key, _MISSING)
        if value is not _MISSING:
            self.move_to_end(key)
        return value

    def remember(self, key: Any, value: Any) -> Any:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.capacity:
            self.popitem(last=False)
        return value


_D: _Lru = _Lru(64)
_E: _Lru = _Lru(256)
_F: _Lru = _Lru(1 << 18)


def clear_extract_cache() -> None:
    _D.clear()
    _E.clear()
    _F.clear()


def _key_of(values: Mapping[str, object]) -> bytes:
    return canonical_bytes(normalize_canonical({k: wire_value(v) for k, v in values.items()}))


def _index(
    node: ExtractNode,
    *,
    capture_digest: str,
    content: bytes,
    decoding: CaptureDecoding,
    key_types: Mapping[str, FieldType],
    enums: EnumResolver | None,
) -> tuple[tuple[object, ...], _Index]:
    extractor = EXTRACTOR_VERSIONS[decoding.format]
    d_key = (
        capture_digest,
        extractor,
        canonical_bytes(decoding.model_dump(mode="json")),
        node.records,
        extract_spec_digest({"key": node.key, "key_types": key_types}),
    )
    cached = _D.lookup(d_key)
    if cached is not _MISSING:
        STATS.d_hits += 1
        return d_key, cached
    STATS.decodes += 1
    document = _decode(content, decoding)
    csv_complete = True
    header_source: object = document
    columns: dict[str, int] = {}
    selector_matched = True
    if isinstance(document, _CsvTable):
        csv_complete = document.complete
        header_source = {}
        if document.header is not None:
            columns = {name: index for index, name in enumerate(document.header)}
        records: list[tuple[str, object]] = []
        width = None if document.header is None else len(document.header)
        for index, row in enumerate(document.rows):
            locator = f"/rows/{index}"
            if width is not None and len(row) != width:
                records.append((locator, _RaggedRow(len(row), width)))
                continue
            records.append(
                (locator, {name: row[i] for name, i in columns.items()} if columns else list(row))
            )
    elif isinstance(document, dict | list):
        found = _enumerate_json(document, node.records)
        selector_matched = found is not None
        records = [] if found is None else found
        if len(records) > decoding.limits.records:
            raise ExtractRefused(
                "decoded_limit_exceeded",
                f"the selector matches more than its {decoding.limits.records}-record limit",
                details={"limit": decoding.limits.records, "dimension": "records"},
            )
    else:
        raise ExtractRefused(
            "extract_format_unsupported",
            f"core extracts records from json and csv, not {decoding.format}",
        )
    keys: dict[str, dict[str, object]] = {}
    problems: dict[str, tuple[Problem, ...]] = {}
    for locator, record in records:
        if isinstance(record, _RaggedRow):
            problems[locator] = (
                Problem(
                    code="type_mismatch",
                    field=None,
                    lexeme=f"{record.cells} cells in a {record.width}-column row",
                ),
            )
            continue
        values: dict[str, object] = {}
        found_problems: list[Problem] = []
        for name, declared in node.key.items():
            lexeme = _read(record, declared.at)
            result = _type_value(
                lexeme,
                declared,
                key_types[name],
                name=name,
                at=locator + "/" + declared.at.lstrip("/"),
                required=True,
                enums=enums,
                resolve=None,
            )
            if result[0] == "present":
                values[name] = result[1]
            else:
                absent = lexeme is _MISSING or lexeme is None
                found_problems.append(
                    Problem(
                        code="key_absent" if absent else "key_unparseable",
                        field=name,
                        lexeme=result[3] if result[0] == "invalid" else None,
                    )
                )
        if found_problems:
            problems[locator] = tuple(found_problems)
        else:
            keys[locator] = values
    by_key: dict[bytes, list[str]] = {}
    if node.key:
        # Without a declared key, records have no identity to share.
        for locator, values in keys.items():
            by_key.setdefault(_key_of(values), []).append(locator)
    duplicate_of = {
        locator: locators[0]
        for locators in by_key.values()
        if len(locators) > 1
        for locator in locators
    }
    built = _Index(
        records=records,
        keys=keys,
        key_problems=problems,
        duplicate_of=duplicate_of,
        by_key=by_key,
        header_source=header_source,
        csv_complete=csv_complete,
        selector_matched=selector_matched,
    )
    return d_key, _D.remember(d_key, built)


@dataclass(frozen=True)
class _RaggedRow:
    cells: int
    width: int


def _read(record: object, at: str) -> object:
    if isinstance(record, dict) and at in record and "/" not in at:
        return record[at]
    if isinstance(record, list):
        return record[int(at)] if at.isdigit() and int(at) < len(record) else _MISSING
    return _at(record, _pointer(at))


def _evaluate(
    node: ExtractNode,
    d_key: tuple[object, ...],
    index: _Index,
    *,
    header_types: Mapping[str, FieldType],
    enums: EnumResolver | None,
) -> _Evaluation:
    e_key = (
        d_key,
        extract_spec_digest(node.complete_when),
        extract_spec_digest({"header": node.header, "types": header_types}),
    )
    cached = _E.lookup(e_key)
    if cached is not _MISSING:
        STATS.e_hits += 1
        return cached  # type: ignore[no-any-return]
    STATS.evaluations += 1
    header: dict[str, Held] = {}
    for name, declared in node.header.items():
        header[name] = _type_value(
            _read(index.header_source, declared.at),
            declared,
            header_types[name],
            name=name,
            at="/" + declared.at.lstrip("/"),
            required=declared.presence == "required",
            enums=enums,
            resolve=None,
        )
    held = index.csv_complete and index.selector_matched
    if node.complete_when is not None:
        stated = _read(index.header_source, node.complete_when.count_at)
        held = (
            held
            and isinstance(stated, int | Decimal)
            and not isinstance(stated, bool)
            and stated == len(index.records)
        )
    return _E.remember(e_key, _Evaluation(complete_when_held=held, header=header))  # type: ignore[no-any-return]


@dataclass(frozen=True)
class _Record:
    locator: str
    key: dict[str, object]
    fields: dict[str, Held]


@dataclass
class ExtractOutcome:
    """One extract node's results and what its completeness rests on.

    ``held`` keeps each result as the extractor read it: a ``_Record``, an
    ``InvalidRecordArm`` or a ``DuplicateArm``. ``results`` and ``records``
    build the ``result`` and ``record`` models on demand.
    """

    held: list[_Record | InvalidRecordArm | DuplicateArm]
    held_header: dict[str, Held]
    complete_when_held: bool
    invalid_records: int
    duplicates: int
    invalid_fields: dict[str, int] = field(default_factory=dict)

    @property
    def results(self) -> list[ResultValue]:
        return [_result_model(item) for item in self.held]

    @property
    def records(self) -> list[RecordValue]:
        return [_record_model(item) for item in self.held if isinstance(item, _Record)]

    @property
    def header(self) -> dict[str, FieldResult]:
        return {name: held_result(value) for name, value in self.held_header.items()}

    @property
    def complete(self) -> bool:
        """``complete_when`` held, and no record was invalid or a refused duplicate."""

        return self.complete_when_held and self.invalid_records == 0 and self.duplicates == 0

    def value(self) -> dict[str, object]:
        """The node's value: results, their record arms, rows, header and completeness."""

        records = [item for item in self.held if isinstance(item, _Record)]
        return {
            "results": [_result_wire(item) for item in self.held],
            "records": [_record_wire(item) for item in records],
            "rows": [
                {
                    name: wire_value(result[1])
                    for name, result in item.fields.items()
                    if result[0] == "present"
                }
                for item in records
            ],
            "header": {name: held_wire(value) for name, value in self.held_header.items()},
            "complete": self.complete,
            "count": len(records),
        }


def _record_model(item: _Record) -> RecordValue:
    return RecordValue(
        record=item.locator,
        key=item.key,
        fields={name: held_result(value) for name, value in item.fields.items()},
    )


def _result_model(item: _Record | InvalidRecordArm | DuplicateArm) -> ResultValue:
    return RecordArm(record=_record_model(item)) if isinstance(item, _Record) else item


def _record_wire(item: _Record) -> dict[str, object]:
    return {
        "record": item.locator,
        "key": {name: wire_value(value) for name, value in item.key.items()},
        "fields": {name: held_wire(value) for name, value in item.fields.items()},
    }


def _result_wire(item: _Record | InvalidRecordArm | DuplicateArm) -> dict[str, object]:
    if isinstance(item, _Record):
        return {"tag": "record", "record": _record_wire(item)}
    if isinstance(item, DuplicateArm):
        return {
            "tag": "duplicate",
            "record": item.record,
            "key": {name: wire_value(value) for name, value in item.key.items()},
            "first": item.first,
        }
    return item.model_dump(mode="json")


def run_extract(
    node: ExtractNode,
    *,
    capture_digest: str,
    content: bytes,
    decoding: CaptureDecoding | None,
    row: Contract,
    resolve: Callable[[ContractRef], Contract | None] | None = None,
    enums: EnumResolver | None = None,
    only_keys: Sequence[Mapping[str, object]] | None = None,
    header_types: Mapping[str, FieldType] | None = None,
    key_types: Mapping[str, FieldType] | None = None,
) -> ExtractOutcome:
    """Decode (or reuse) one Capture's record index and read the node's fields."""

    if decoding is None:
        raise ExtractRefused(
            "extract_decoding_undeclared",
            "the Capture's CaptureContract declares no decoding",
        )
    types = {name: row.fields[name].type for name in node.fields}
    keys = dict(key_types or {name: declared.field_type() for name, declared in node.key.items()})
    headers = dict(
        header_types or {name: declared.field_type() for name, declared in node.header.items()}
    )
    d_key, index = _index(
        node,
        capture_digest=capture_digest,
        content=content,
        decoding=decoding,
        key_types=keys,
        enums=enums,
    )
    evaluation = _evaluate(node, d_key, index, header_types=headers, enums=enums)
    for name, declared in node.header.items():
        result = evaluation.header[name]
        if declared.presence == "required" and result[0] != "present":
            code = "absent_required" if result[0] == "absent" else result[1]
            lexeme = result[3] if result[0] == "invalid" else None
            raise ExtractRefused(
                "extract.header_invalid@1",
                f"required header field {name!r} is {code}",
                details={
                    "field": name,
                    "code": code,
                    **({"lexeme": lexeme} if lexeme is not None else {}),
                },
            )
    if only_keys is None:
        selected: Iterable[tuple[str, object]] = index.records
    else:
        wanted: list[str] = []
        for key in only_keys:
            wanted.extend(index.by_key.get(_key_of(key), ()))
        positions = {locator: position for position, (locator, _r) in enumerate(index.records)}
        selected = sorted(
            ((locator, None) for locator in dict.fromkeys(wanted)),
            key=lambda item: positions[item[0]],
        )
    record_data = None
    held: list[_Record | InvalidRecordArm | DuplicateArm] = []
    invalid_records = 0
    duplicates = 0
    invalid_fields: dict[str, int] = {}
    field_digests = {name: extract_spec_digest(declared) for name, declared in node.fields.items()}
    required = {
        name: declared.presence == "required"
        or (declared.presence is None and not row.fields[name].optional)
        for name, declared in node.fields.items()
    }
    for locator, record in selected:
        problems = index.key_problems.get(locator)
        if problems is not None:
            invalid_records += 1
            held.append(InvalidRecordArm(record=locator, problems=problems))
            continue
        key = index.keys[locator]
        first = index.duplicate_of.get(locator)
        if first is not None and (node.duplicates != "first" or first != locator):
            if node.duplicates == "refuse_run":
                raise ExtractRefused(
                    "extract_duplicate_key",
                    f"records {first} and {locator} share one key",
                    details={"first": first, "record": locator},
                )
            if node.duplicates == "refuse_key":
                duplicates += 1
            held.append(DuplicateArm(record=locator, key=key, first=first))
            continue
        if record is None:
            if record_data is None:
                record_data = dict(index.records)
            record = record_data[locator]
        fields: dict[str, Held] = {}
        for name, declared in node.fields.items():
            f_key = (d_key, field_digests[name], locator)
            cached = _F.lookup(f_key)
            if cached is _MISSING:
                STATS.field_reads += 1
                cached = _F.remember(
                    f_key,
                    _type_value(
                        _read(record, declared.at),
                        declared,
                        types[name],
                        name=name,
                        at=locator + "/" + declared.at.lstrip("/"),
                        required=required[name],
                        enums=enums,
                        resolve=resolve,
                    ),
                )
            else:
                STATS.f_hits += 1
            if cached[0] == "invalid":
                invalid_fields[name] = invalid_fields.get(name, 0) + 1
            fields[name] = cached
        held.append(_Record(locator=locator, key=key, fields=fields))
    return ExtractOutcome(
        held=held,
        held_header=evaluation.header,
        complete_when_held=evaluation.complete_when_held,
        invalid_records=invalid_records,
        duplicates=duplicates,
        invalid_fields=invalid_fields,
    )


Extractor = Literal["json-pointer-v1", "csv-cell-v1"]

__all__ = [
    "STATS",
    "ExtractCacheStats",
    "ExtractOutcome",
    "ExtractRefused",
    "Extractor",
    "clear_extract_cache",
    "field_result_wire",
    "held_result",
    "held_wire",
    "run_extract",
    "wire_value",
]
