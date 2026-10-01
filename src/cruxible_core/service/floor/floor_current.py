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
coordinates. The one coordinate is in its first line, and the digests and full
statements behind every value are under ``provenance/subjects/``.

The layout:

- line 1: ``# <kind>/<id>  kind=<kind>  at <git_oid> gen <n>``;
- then each field's current value under the shared field-naming rule's short
  name (``service.discovery.field_names``), sorted; a many-valued field, or a
  contested single-valued one, lists every value;
- each value ends with a comment naming the Claim that states it (``CLM-…``)
  and the Captures it cites (``CAP-<12 hex>``);
- a Subject-valued field shows the other Subject's ref (``kind/id``);
- then ``flags:``, the verdict problems of each flagged field as of this file's
  coordinate (``stale``, ``contested``, ``contradicted``, ``uncovered``,
  ``unsure_hold``). ``get`` on the ref answers with live verdicts.

The values are the slot's answer exactly as ``get`` shows it: what resolution
selected, or every live contender while the slot is contested.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal

import yaml

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import ClaimType, parse_claim_type
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ExactContentClaimObject,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
)
from cruxible_client.contracts.errors import PlaybillError, ProjectionIntegrityError
from cruxible_client.contracts.operational_reads import capture_handle
from cruxible_client.contracts.primitives import pretty_json
from cruxible_client.contracts.subjects import SubjectShell, parse_subject
from cruxible_core.indexes.history.history_index import HistoryReader
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.discovery.exact_content import ExactContentReader
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.discovery.read_flags import answer_flags, verdict_flags

if TYPE_CHECKING:
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

CURRENT_PREFIX = "current/"
SUBJECT_PREFIX = "subjects/"
PROVENANCE_SUBJECTS_PREFIX = "provenance/subjects/"
CURRENT_SUFFIX = ".yaml"

FloorFlag = Literal["stale", "contested", "contradicted", "uncovered", "unsure_hold"]
FLOOR_FLAG_ORDER: tuple[FloorFlag, ...] = (
    "stale",
    "contested",
    "contradicted",
    "uncovered",
    "unsure_hold",
)
# A value inlines while it stays this small; longer text goes whole to a sibling file.
INLINE_TEXT_BYTES = 2048
INLINE_TEXT_LINES = 40
_FLAGS_NOTE = "verdicts as of this file's coordinate; get the ref for live verdicts"


# -- the stamp ------------------------------------------------------------------


@dataclass(frozen=True)
class FloorStamp:
    """The one accepted coordinate a floor file is exported at."""

    git_oid: str
    generation: int
    accepted_at: datetime

    @property
    def at(self) -> str:
        return f"at {self.git_oid} gen {self.generation}"


def floor_stamp(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    history: HistoryReader,
) -> FloorStamp:
    location = history.generation_for_oid(coordinate.git_oid)
    if location is None:
        raise ProjectionIntegrityError("floor coordinate is outside accepted history")
    return FloorStamp(
        git_oid=coordinate.git_oid,
        generation=location.sequence,
        accepted_at=instance.accepted_evaluation_time(coordinate.git_oid),
    )


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
class ClaimVerdict:
    """What the floor shows of one live Claim's verdict at its coordinate."""

    verdict: str | None
    status: str
    held: bool


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
    # An exact-content value's body digest, and whether the store held it intact.
    body: tuple[str, bool | None] | None = None


@dataclass(frozen=True)
class SubjectPart:
    """Everything the floor renders for one Subject, apart from its stamp."""

    ref: str
    kind: str
    body: str
    header_extra: str
    provenance: bytes
    sequences: tuple[int, ...]
    # Full-text sibling files: (file name beside the current/ file, header, text).
    texts: tuple[tuple[str, str, str], ...] = ()
    # The kind INDEX cells: a title-like value and the state-like fields.
    index_title: str = ""
    index_states: tuple[tuple[str, str], ...] = ()
    # Every body-store object this render read, and whether it was held intact.
    # A render is reused only while each one still answers the same.
    bodies: tuple[tuple[str, bool | None], ...] = ()


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
    return " ".join(
        (claim_handle(claim), *(capture_handle(item) for item in claim.backing.capture_digests))
    )


def claim_flags(claim: ClaimArtifactAny, verdicts: Mapping[str, ClaimVerdict]) -> set[FloorFlag]:
    known = verdicts.get(claim.identity.name)
    if known is None:
        return set()
    flags: set[FloorFlag] = set(verdict_flags(known.verdict, known.status, held=known.held))
    if known.verdict == "uncovered":
        flags.add("uncovered")
    return flags


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
    once per digest from the body store the bytes were committed to, exactly as
    ``get`` shows it. Text too long to keep a current/ file bounded goes whole
    into a sibling ``<id>.<field>.txt`` file, never truncated; bytes that are not
    UTF-8 text show as a typed marker with their size.
    """

    def __init__(self, instance: PlaybillInstance) -> None:
        self._instance = instance
        self._content = ExactContentReader(instance)
        self._held: dict[str, bool | None] = {}

    def shown(self, claim: ClaimArtifactAny, *, text_name: str) -> _Shown:
        obj = claim.statement.object
        note = _note(claim)
        if isinstance(obj, SubjectClaimObject):
            other = subject_ref(obj.address.artifact_path)
            return _Shown(yaml_scalar(other), None, note, plain=other)
        if isinstance(obj, ExactContentClaimObject):
            digest = obj.content_digest
            if digest not in self._held:
                # Bracket the one read of these bytes: a body that moved while it
                # was read has no answer to record, so its render is never reused.
                before = body_available(self._instance, digest)
                value = self._content.of(obj)
                after = body_available(self._instance, digest)
                self._held[digest] = before if before == after else None
            else:
                value = self._content.of(obj)
            body = (digest, self._held[digest])
            if not isinstance(value, str):
                size = "null" if value.length is None else str(value.length)
                marker = f"{{exact_content: {value.exact_content}, bytes: {size}}}"
                return _Shown(marker, None, note, plain=marker, body=body)
            shown = self.text(value, note=note, text_name=text_name)
            return replace(shown, body=body)
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


def render_index(kind: str, parts: Sequence[SubjectPart], stamp: FloorStamp) -> bytes:
    """``current/<kind>/INDEX``: one line per Subject, ref, title and states.

    Columns are tab-separated: the ref, a title-like field's value (or ``-``),
    and ``field=value`` for each state-like field, ``; ``-separated.
    """

    rows = sorted(parts, key=lambda part: part.ref.encode())
    lines = [
        f"# {kind} INDEX  {len(rows)} subjects  columns: ref, title, states  {stamp.at}",
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


def index_files(parts: Iterable[SubjectPart], stamp: FloorStamp) -> dict[str, bytes]:
    by_kind: dict[str, list[SubjectPart]] = defaultdict(list)
    for part in parts:
        by_kind[part.kind].append(part)
    return {
        f"{CURRENT_PREFIX}{kind}/{INDEX_NAME}": render_index(kind, members, stamp)
        for kind, members in by_kind.items()
    }


def render_subject(
    *,
    path: str,
    shell: SubjectShell,
    claims: Sequence[ClaimArtifactAny],
    claim_types: Mapping[str, ClaimType],
    accepted_predicates: frozenset[str],
    verdicts: Mapping[str, ClaimVerdict],
    values: ValueRenderer,
    history: HistoryReader,
) -> SubjectPart:
    """Render one Subject's values-first body and its provenance row."""

    ref = subject_ref(path)
    kind = shell.subject_kind
    slots: dict[tuple[str, str | None], list[ClaimArtifactAny]] = defaultdict(list)
    for claim in claims:
        slots[(claim.statement.predicate, claim.statement.qualifier)].append(claim)
    entries: list[tuple[str, str, list[str]]] = []
    texts: list[tuple[str, str, str]] = []
    plains: dict[str, tuple[str, ...]] = {}
    bodies: set[tuple[str, bool | None]] = set()
    flagged: list[tuple[str, list[FloorFlag]]] = []
    for (predicate, qualifier), members in slots.items():
        members.sort(key=lambda item: item.identity.name.encode())
        display = short_field_name(predicate, kind, accepted_predicates)
        key = display if qualifier is None else f"{display}[{qualifier}]"
        declared = claim_types.get(predicate)
        many = declared is not None and declared.cardinality == "many"
        # The slot's answer as get shows it: what resolution selected, or every
        # live contender while contested. Overturned and refused are not values.
        shown = [
            item
            for item in members
            if (known := verdicts.get(item.identity.name)) is None
            or known.status in {"accepted", "conflicted"}
        ] or members
        marks: set[FloorFlag] = set(
            answer_flags("many" if many else "one", len({_value_key(item) for item in shown}))
        )
        for item in shown:
            marks |= claim_flags(item, verdicts)
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
        bodies.update(value.body for value in rendered if value.body is not None)
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
    body = "".join(f"{line}\n" for line in lines)
    rows: list[dict[str, object]] = []
    sequences: set[int] = set()
    for claim in sorted(claims, key=lambda item: item.identity.qualified.encode()):
        location = history.latest_member(claim_path(claim.identity.name))
        sequence = None if location is None else location.sequence
        if sequence is not None:
            sequences.add(sequence)
        rows.append(
            {
                "claim": claim.identity.qualified,
                "artifact_digest": claim_artifact_digest(claim).tagged,
                "statement": claim.statement.model_dump(mode="json"),
                "latest_change_sequence": sequence,
            }
        )
    provenance = _render_json(
        {
            "subject": path,
            "current": current_path(ref),
            "scope": (
                "all live accepted Claims; contenders are preserved; no current verdict implied"
            ),
            "claims": rows,
        }
    )
    return SubjectPart(
        ref=ref,
        kind=kind,
        body=body,
        header_extra="" if shell.lifecycle.state == "live" else "  lifecycle=retired",
        provenance=provenance,
        sequences=tuple(sorted(sequences)),
        texts=tuple(sorted(texts)),
        index_title=_index_title(plains),
        index_states=tuple(
            (key, "|".join(_one_line(value) for value in plains[key]))
            for key in sorted(plains, key=lambda item: item.encode())
            if _STATE_FIELD.search(key)
        )
        + ((("lifecycle", "retired"),) if shell.lifecycle.state != "live" else ()),
        bodies=tuple(sorted(bodies)),
    )


def stamped(part: SubjectPart, stamp: FloorStamp) -> dict[str, bytes]:
    """One Subject's current/ file, its one-line header first, and its full texts."""

    header = f"# {part.ref}  kind={part.kind}  {stamp.at}{part.header_extra}\n"
    directory = CURRENT_PREFIX + part.ref.rsplit("/", 1)[0]
    files = {current_path(part.ref): (header + part.body).encode("utf-8")}
    for name, label, text in part.texts:
        files[f"{directory}/{name}"] = f"# {part.ref}  {label}  {stamp.at}\n{text}".encode()
    return files


def _render_json(value: object) -> bytes:
    return pretty_json(json.loads(canonical_bytes(value))).encode("utf-8") + b"\n"


# -- the accepted inputs ----------------------------------------------------------


def accepted_subjects(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    paths: Iterable[str] | None = None,
) -> dict[str, SubjectShell]:
    """Accepted Subject shells by path: every one, or just ``paths``."""

    with instance.bind_accepted_projection(coordinate) as projection:
        wanted = (
            tuple(row.path for row in projection.typed.envelopes(kind="subject"))
            if paths is None
            else tuple(paths)
        )
        projection.typed.prefetch_members(wanted)
        raw = {path: projection.typed.member_bytes(path) for path in wanted}
    return {
        path: parse_subject(content, path=path)
        for path, content in sorted(raw.items(), key=lambda item: item[0].encode())
        if content is not None
    }


def accepted_claim_types(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> tuple[dict[str, ClaimType], frozenset[str]]:
    """Accepted ClaimTypes by predicate, and the live predicates short names avoid.

    The live set is the one ``get`` and ``orient`` shorten field names against.
    """

    with instance.bind_accepted_projection(coordinate) as projection:
        rows = tuple(projection.typed.envelopes(kind="claim-type"))
        projection.typed.prefetch_members(tuple(row.path for row in rows))
        raw = {row.path: projection.typed.member_bytes(row.path) for row in rows}
        live = frozenset(
            str(identity).removeprefix("ClaimType:")
            for (identity,) in projection.typed.connection.execute(
                "SELECT identity FROM claim_types WHERE lifecycle='live'"
            )
        )
    parsed = (
        parse_claim_type(content, path=path) for path, content in raw.items() if content is not None
    )
    return {item.predicate: item for item in parsed}, live


def live_claims(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> tuple[tuple[ClaimArtifactAny, ...], ClaimVerdictReadContext]:
    """Every live accepted Claim, read through the verdict context the flags reuse."""

    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext as Context

    with instance.bind_accepted_projection(coordinate) as projection:
        identities = tuple(
            str(row[0])
            for row in projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE lifecycle='live' ORDER BY identity"
            )
        )
    context = Context(instance, coordinate)
    context.prefetch(tuple(claim_path(item.removeprefix("Claim:")) for item in identities))
    claims = tuple(context.claim(item) for item in identities)
    return tuple(sorted(claims, key=lambda item: claim_path(item.identity.name))), context


def claim_verdicts(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    claims: tuple[ClaimArtifactAny, ...],
    *,
    evaluation_time: datetime,
    read_context: ClaimVerdictReadContext | None = None,
) -> dict[str, ClaimVerdict]:
    """Each live Claim's verdict, slot status and hold, from the shared machinery.

    Nothing is re-adjudicated: this is the per-slot derivation ``get`` and
    ``query`` read, at the floor coordinate's own acceptance instant, so it is
    a function of accepted state rather than of the moment of export.
    """

    from cruxible_core.service.discovery.claim_status import claim_resolution_statuses
    from cruxible_core.service.discovery.read_flags import unsure_holds
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext as Context

    if not claims:
        return {}
    verdicts: dict[str, object] = {}
    statuses = claim_resolution_statuses(
        instance,
        claims=claims,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        evaluation_time=evaluation_time,
        verdicts_by_identity=verdicts,  # type: ignore[arg-type]
        read_context=read_context or Context(instance, coordinate),
    )
    held = unsure_holds(
        instance, coordinate, claims, statuses=statuses, evaluation_time=evaluation_time
    )
    return {
        claim.identity.name: ClaimVerdict(
            verdict=getattr(verdicts.get(claim.identity.qualified), "verdict", None),
            status=statuses.get(claim.identity.name, "accepted"),
            held=claim.identity.qualified in held,
        )
        for claim in claims
    }


def claims_by_subject(
    claims: Iterable[ClaimArtifactAny],
) -> dict[str, list[ClaimArtifactAny]]:
    grouped: dict[str, list[ClaimArtifactAny]] = defaultdict(list)
    for claim in claims:
        if claim.lifecycle.state == "live":
            grouped[claim.statement.subject.artifact_path].append(claim)
    return grouped


__all__ = [
    "CURRENT_PREFIX",
    "CURRENT_SUFFIX",
    "FLOOR_FLAG_ORDER",
    "PROVENANCE_SUBJECTS_PREFIX",
    "ClaimVerdict",
    "FloorFlag",
    "FloorStamp",
    "SubjectPart",
    "ValueRenderer",
    "accepted_claim_types",
    "accepted_subjects",
    "capture_handle",
    "claim_verdicts",
    "claims_by_subject",
    "current_path",
    "index_files",
    "render_index",
    "body_available",
    "bodies_unchanged",
    "floor_stamp",
    "live_claims",
    "literal_scalar",
    "render_subject",
    "stamped",
    "text_file_name",
    "subject_ref",
    "yaml_scalar",
]
