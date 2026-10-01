"""Values-first ``current/`` files: the grep-first front door to accepted state.

One file per accepted Subject, ``current/<kind>/<id>.yaml``, written for an
agent that greps and reads the file it hit. A grep hit on a value shows the
field it belongs to on the same line, and the path names the Subject.

Why YAML (a strict, hand-rendered subset) rather than Markdown:

- every line is ``field: value`` (or a ``- value`` list item under its field),
  so a grep hit is self-describing without reading the file;
- the whole file still parses as data (``yaml.safe_load``) for a tool that
  wants it, with the header and the Claim/Capture handles as comments;
- multi-line text keeps its line breaks as a ``|`` block, so a ruling reads as
  prose and each of its lines greps on its own.

The rendering is deterministic: the same accepted state at the same coordinate
gives the same bytes. The file carries NO digests, addresses or repeated
coordinates; the one coordinate is in its first line. ``get`` on a ref or a
Claim handle serves the full statement and its digests.

The layout:

- line 1: ``# <kind>/<id>  kind=<kind>  at <git_oid> gen <n>``;
- then each field's current value under the shared field-naming rule's short
  name (``service.discovery.field_names``), sorted; a many-valued field, or a
  contested single-valued one, lists every value;
- each value ends with a comment naming the Claim that states it (``CLM-…``);
- a Subject-valued field shows the other Subject's ref (``kind/id``);
- then ``flags:``, the one structural flag: ``contested``, a single-valued field
  with more than one distinct live value;
- then ``incoming:``, one ``<field> <- <ref>`` line per live Subject-valued
  Claim of another Subject that points at this one.

The floor is a pure function of the accepted coordinate, so it shows every
live Claim of a slot and no verdict: a verdict also moves with time, evidence
availability and operational attestations, none of which a coordinate fixes.
``get`` on the ref answers with live verdicts.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import yaml

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ExactContentClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.exact_content import ExactContentReader
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.discovery.read_flags import answer_flags

CURRENT_PREFIX = "current/"
SUBJECT_PREFIX = "subjects/"
CURRENT_SUFFIX = ".yaml"

FloorFlag = Literal["contested"]
FLOOR_FLAG_ORDER: tuple[FloorFlag, ...] = ("contested",)
# A value inlines while it stays this small; longer text goes whole to a sibling file.
INLINE_TEXT_BYTES = 2048
INLINE_TEXT_LINES = 40
_FLAGS_NOTE = "a single-valued field with several live values; get the ref for verdicts"


# -- the stamp ------------------------------------------------------------------


def stamp_text(changed_at: int) -> str:
    """How a file names the generation it last changed: ``changed gen <n>``."""

    return f"changed gen {changed_at}"


# -- scalars --------------------------------------------------------------------

_YAML_WORDS = frozenset(
    {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~", "nan", "inf"}
)
# Starts with a letter, holds nothing YAML reads as a break or control.
_PLAIN = re.compile(r"[^\W\d_][^\x00-\x1f\x7f-\x9f\u2028\u2029\ufeff]*")
_UNPRINTABLE = re.compile(r"[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff\ud800-\udfff]")


def _quoted(text: str) -> str:
    """A JSON string, which is also a YAML double-quoted scalar."""

    rendered = json.dumps(text, ensure_ascii=False)
    return _UNPRINTABLE.sub(lambda match: f"\\u{ord(match.group()):04x}", rendered)


def yaml_scalar(text: str) -> str:
    """One string as a single-line YAML scalar: plain when that is unambiguous."""

    if (
        _PLAIN.fullmatch(text)
        and text.casefold() not in _YAML_WORDS
        and ": " not in text
        and " #" not in text
        and "\t" not in text
        and not text.endswith((" ", ":"))
    ):
        return text
    return _quoted(text)


def _block(text: str) -> tuple[str, tuple[str, ...]] | None:
    """A multi-line string as a literal block: its indicator and its lines.

    None when a block could not carry the text exactly; the caller quotes it.
    """

    if "\r" in text or not text.strip() or text[0] in " \t\n" or _UNPRINTABLE.search(text):
        return None
    body = text.rstrip("\n")
    trailing = len(text) - len(body)
    indicator = "|-" if trailing == 0 else "|" if trailing == 1 else "|+"
    lines = tuple(body.split("\n")) + (("",) * (trailing - 1) if trailing > 1 else ())
    rendered = f"v: {indicator}\n" + "".join(f"  {line}\n" if line else "\n" for line in lines)
    try:
        parsed = yaml.safe_load(rendered)
    except yaml.YAMLError:
        return None
    if parsed != {"v": text}:
        return None
    return indicator, lines


def literal_scalar(value: object) -> str:
    """A literal Claim value as one line of YAML."""

    if isinstance(value, str):
        return yaml_scalar(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _UNPRINTABLE.sub(lambda match: f"\\u{ord(match.group()):04x}", rendered)


# -- one Subject ----------------------------------------------------------------


@dataclass(frozen=True)
class _Shown:
    """One displayed value: a scalar, or a text block, plus its handles."""

    scalar: str | None
    block: tuple[str, tuple[str, ...]] | None
    note: str
    # A text too long to inline: its sibling file's name and its text.
    text_file: tuple[str, str] | None = None
    # The value as one line of plain text, for the kind's INDEX.
    plain: str = ""


INCOMING_KEY = "incoming"


@dataclass(frozen=True)
class IncomingEdge:
    """One live Subject-valued Claim of another Subject that points here.

    ``field`` is the field as the other Subject's own file names it, so one
    grep for it finds both ends of the edge.
    """

    field: str
    source: str
    claim: str


@dataclass(frozen=True)
class SubjectPart:
    """Everything the floor renders for one Subject, apart from its stamp."""

    ref: str
    kind: str
    body: str
    header_extra: str
    # Full-text sibling files: (file name beside the current/ file, header, text).
    texts: tuple[tuple[str, str, str], ...] = ()
    # The kind INDEX cells: a title-like value and the state-like fields.
    index_title: str = ""
    index_states: tuple[tuple[str, str], ...] = ()


def body_available(instance: PlaybillInstance, digest: str) -> bool:
    """Whether the body store holds ``digest`` intact, as a read would find it.

    ``verify`` re-hashes only a file whose identity changed since it was last
    verified, so an unchanged body costs one stat.
    """

    try:
        return instance.body_store().verify(digest)
    except (PlaybillError, OSError, ValueError):
        return False


def bodies_unchanged(instance: PlaybillInstance, bodies: Iterable[tuple[str, bool | None]]) -> bool:
    """Whether every recorded body still answers as it did when it was rendered."""

    return all(body_available(instance, digest) == held for digest, held in bodies)


def subject_ref(path: str) -> str:
    return path.removeprefix(SUBJECT_PREFIX).removesuffix(".json")


def current_path(ref: str) -> str:
    return f"{CURRENT_PREFIX}{ref}{CURRENT_SUFFIX}"


def claim_handle(claim: ClaimArtifactAny) -> str:
    return claim.identity.name


def _note(claim: ClaimArtifactAny) -> str:
    return claim_handle(claim)


def _value_key(claim: ClaimArtifactAny) -> str:
    obj = claim.statement.object
    if isinstance(obj, ExactContentClaimObject):
        return repr(("exact_content", obj.content_digest, obj.span))
    if isinstance(obj, SubjectClaimObject):
        return repr(("subject", obj.address.artifact_path))
    return repr(("literal", canonical_bytes(obj.value)))


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ValueRenderer:
    """Shows one Claim's object as the floor shows it.

    An exact-content value (a ruling, a method law) is shown as its text, read
    from the body store the bytes were committed to, exactly as ``get`` shows
    it. Text too long to keep a current/ file bounded goes whole into a sibling
    ``.txt`` file, never truncated; bytes that are not UTF-8 text show as a
    typed marker with their size.

    Accepted-body retention invariant: the body of an accepted exact-content
    Claim is retained for as long as the Claim is in accepted history. The text
    is then a pure function of the content digest, so a floor rendered at any
    time agrees with one rendered at any other. A lost body renders as
    ``{exact_content: unavailable, ...}`` in a fresh render; that is an integrity
    incident ``orient`` and ``next`` report, never a change of accepted state.
    """

    def __init__(self, instance: PlaybillInstance) -> None:
        self._instance = instance
        self._content = ExactContentReader(instance)

    def shown(self, claim: ClaimArtifactAny, *, text_name: str) -> _Shown:
        obj = claim.statement.object
        note = _note(claim)
        if isinstance(obj, SubjectClaimObject):
            other = subject_ref(obj.address.artifact_path)
            return _Shown(yaml_scalar(other), None, note, plain=other)
        if isinstance(obj, ExactContentClaimObject):
            value = self._content.of(obj)
            if not isinstance(value, str):
                size = "null" if value.length is None else str(value.length)
                marker = f"{{exact_content: {value.exact_content}, bytes: {size}}}"
                return _Shown(marker, None, note, plain=marker)
            return self.text(value, note=note, text_name=text_name)
        literal = obj.value
        if isinstance(literal, str):
            return self.text(literal, note=note, text_name=text_name)
        scalar = literal_scalar(literal)
        return _Shown(scalar, None, note, plain=scalar)

    def text(self, value: str, *, note: str, text_name: str) -> _Shown:
        encoded = len(value.encode("utf-8"))
        lines = value.count("\n") + 1
        if encoded > INLINE_TEXT_BYTES or lines > INLINE_TEXT_LINES:
            marker = f"{{full_text: {text_name}, bytes: {encoded}, lines: {lines}}}"
            return _Shown(marker, None, note, text_file=(text_name, value), plain=value)
        if "\n" in value:
            block = _block(value)
            if block is not None:
                return _Shown(None, block, note, plain=value)
        return _Shown(yaml_scalar(value), None, note, plain=value)


def text_file_name(ref: str, key: str, claim: ClaimArtifactAny | None) -> str:
    """The sibling file a long text is written to: ``<id>.<field>[.<CLM->].txt``."""

    stem = ref.rsplit("/", 1)[-1]
    suffix = "" if claim is None else f".{claim_handle(claim)}"
    return f"{stem}.{_SAFE_NAME.sub('_', key)}{suffix}.txt"


def _entry_lines(key: str, values: Sequence[_Shown], *, listed: bool) -> list[str]:
    lines: list[str] = []
    if not listed:
        (value,) = values
        if value.block is None:
            return [f"{key}: {value.scalar}  # {value.note}"]
        indicator, body = value.block
        return [
            f"{key}: {indicator}  # {value.note}",
            *(f"  {line}" if line else "" for line in body),
        ]
    lines.append(f"{key}:")
    for value in values:
        if value.block is None:
            lines.append(f"  - {value.scalar}  # {value.note}")
            continue
        indicator, body = value.block
        lines.append(f"  - {indicator}  # {value.note}")
        lines.extend(f"    {line}" if line else "" for line in body)
    return lines


_TITLE_FIELDS = ("title", "name", "label", "summary", "headline")
_TITLE_FIELD = re.compile(r"(^|_)(title|name)$")
_STATE_FIELD = re.compile(r"(^|_)(state|status|stage|phase)$")
INDEX_NAME = "INDEX"
_INDEX_TITLE_CHARS = 120


def _one_line(text: str, limit: int = _INDEX_TITLE_CHARS) -> str:
    line = " ".join(text.replace("\t", " ").split("\n", 1)[0].split())
    return line if len(line) <= limit else line[: limit - 3] + "..."


def _index_title(plains: Mapping[str, tuple[str, ...]]) -> str:
    """The Subject's title-like value: ``title`` first, then other names."""

    ranked = sorted(
        (key for key in plains if key in _TITLE_FIELDS or _TITLE_FIELD.search(key)),
        key=lambda key: (
            _TITLE_FIELDS.index(key) if key in _TITLE_FIELDS else len(_TITLE_FIELDS),
            key.encode(),
        ),
    )
    for key in ranked:
        values = plains[key]
        if len(values) == 1 and values[0]:
            return _one_line(values[0])
    return ""


def render_index(kind: str, parts: Sequence[SubjectPart], changed_at: int) -> bytes:
    """``current/<kind>/INDEX``: one line per Subject, ref, title and states.

    Columns are tab-separated: the ref, a title-like field's value (or ``-``),
    and ``field=value`` for each state-like field, ``; ``-separated.
    """

    rows = sorted(parts, key=lambda part: part.ref.encode())
    lines = [
        f"# {kind} INDEX  {len(rows)} subjects  columns: ref, title, states  "
        f"{stamp_text(changed_at)}",
        *(
            "\t".join(
                (
                    part.ref,
                    part.index_title or "-",
                    "; ".join(f"{key}={value}" for key, value in part.index_states) or "-",
                )
            )
            for part in rows
        ),
    ]
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def index_path(kind: str) -> str:
    return f"{CURRENT_PREFIX}{kind}/{INDEX_NAME}"


def render_subject(
    *,
    path: str,
    shell: SubjectShell,
    claims: Sequence[ClaimArtifactAny],
    claim_types: Mapping[str, ClaimType],
    accepted_predicates: frozenset[str],
    values: ValueRenderer,
    incoming: Sequence[IncomingEdge] = (),
) -> SubjectPart:
    """Render one Subject's values-first body, then the live edges that point at it."""

    ref = subject_ref(path)
    kind = shell.subject_kind
    slots: dict[tuple[str, str | None], list[ClaimArtifactAny]] = defaultdict(list)
    for claim in claims:
        slots[(claim.statement.predicate, claim.statement.qualifier)].append(claim)
    entries: list[tuple[str, str, list[str]]] = []
    texts: list[tuple[str, str, str]] = []
    plains: dict[str, tuple[str, ...]] = {}
    flagged: list[tuple[str, list[FloorFlag]]] = []
    for (predicate, qualifier), members in slots.items():
        members.sort(key=lambda item: item.identity.name.encode())
        display = short_field_name(predicate, kind, accepted_predicates)
        key = display if qualifier is None else f"{display}[{qualifier}]"
        declared = claim_types.get(predicate)
        many = declared is not None and declared.cardinality == "many"
        # Every live Claim of the slot: which one resolution selects is a
        # verdict, and verdicts are get's to serve.
        shown = members
        marks: set[FloorFlag] = {
            flag
            for flag in answer_flags(
                "many" if many else "one", len({_value_key(item) for item in shown})
            )
            if flag == "contested"
        }
        listed = many or len(shown) > 1
        rendered = sorted(
            (
                values.shown(item, text_name=text_file_name(ref, key, item if listed else None))
                for item in shown
            ),
            key=lambda value: (
                (value.scalar or "\n".join(value.block[1] if value.block else ())).encode(),
                value.note.encode(),
            ),
        )
        entries.append(
            (display, qualifier or "", _entry_lines(yaml_scalar(key), rendered, listed=listed))
        )
        plains[key] = tuple(value.plain for value in rendered)
        texts.extend(
            (value.text_file[0], f"field={key}  {value.note.split()[0]}", value.text_file[1])
            for value in rendered
            if value.text_file is not None
        )
        if marks:
            flagged.append((yaml_scalar(key), [flag for flag in FLOOR_FLAG_ORDER if flag in marks]))
    lines: list[str] = []
    for _display, _qualifier, entry in sorted(
        entries, key=lambda item: (item[0].encode(), item[1].encode())
    ):
        lines.extend(entry)
    if flagged:
        lines.append(f"flags:  # {_FLAGS_NOTE}")
        lines.extend(
            f"  {key}: [{', '.join(flags)}]"
            for key, flags in sorted(flagged, key=lambda item: item[0].encode())
        )
    if incoming:
        lines.append(f"{INCOMING_KEY}:")
        lines.extend(
            f"  - {yaml_scalar(f'{edge.field} <- {edge.source}')}  # {edge.claim}"
            for edge in sorted(
                incoming,
                key=lambda edge: (edge.field.encode(), edge.source.encode(), edge.claim.encode()),
            )
        )
    body = "".join(f"{line}\n" for line in lines)
    return SubjectPart(
        ref=ref,
        kind=kind,
        body=body,
        header_extra="" if shell.lifecycle.state == "live" else "  lifecycle=retired",
        texts=tuple(sorted(texts)),
        index_title=_index_title(plains),
        index_states=tuple(
            (key, "|".join(_one_line(value) for value in plains[key]))
            for key in sorted(plains, key=lambda item: item.encode())
            if _STATE_FIELD.search(key)
        )
        + ((("lifecycle", "retired"),) if shell.lifecycle.state != "live" else ()),
    )


def stamped(part: SubjectPart, changed_at: int) -> dict[str, bytes]:
    """One Subject's current/ file, its one-line header first, and its full texts."""

    at = stamp_text(changed_at)
    header = f"# {part.ref}  kind={part.kind}  {at}{part.header_extra}\n"
    directory = CURRENT_PREFIX + part.ref.rsplit("/", 1)[0]
    files = {current_path(part.ref): (header + part.body).encode("utf-8")}
    for name, label, text in part.texts:
        files[f"{directory}/{name}"] = f"# {part.ref}  {label}  {at}\n{text}".encode()
    return files


__all__ = [
    "INCOMING_KEY",
    "IncomingEdge",
    "CURRENT_PREFIX",
    "CURRENT_SUFFIX",
    "FLOOR_FLAG_ORDER",
    "FloorFlag",
    "SubjectPart",
    "ValueRenderer",
    "bodies_unchanged",
    "body_available",
    "current_path",
    "index_path",
    "literal_scalar",
    "render_index",
    "render_subject",
    "stamp_text",
    "stamped",
    "subject_ref",
    "text_file_name",
    "yaml_scalar",
]
