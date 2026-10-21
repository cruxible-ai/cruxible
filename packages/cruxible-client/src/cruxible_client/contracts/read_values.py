"""How a read shows a Claim value: whole, as a typed preview, or as a marker.

Summary reads (``get`` cards, ``query`` rows and cells, ``World.values``, write
outcomes) bound each value: a string over ``GET_SUMMARY_TEXT_MAX_CHARS``
characters is cut to a :class:`TruncatedText`, a declared preview that can never
be mistaken for the value, and an exact-content value with no text to show is an
:class:`ExactContentRef`. An object value is a :class:`ShownObject`, shown whole.
Every other value is itself. ``ShownValue`` is that typed union. A preview names
the exact read of its whole value (:class:`WholeValueRead`) whenever one Claim
at one accepted generation backs it.

No JSON object in a shown value is ever a bare value: it is a marker, which
carries its required discriminator (``truncated``, ``exact_content``), or an
object value wrapped as ``{"object": {...}}``. A Claim whose value is an object
shaped like a marker therefore reads back as that object, never as a marker.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypeAliasType

#: A summary read shows at most this many characters of one string value.
GET_SUMMARY_TEXT_MAX_CHARS = 500


class _StrictReadValueModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _omit_none(value: object) -> bool:
    return value is None


class WholeValueRead(_StrictReadValueModel):
    """The exact read that returns a preview's whole value.

    ``get(ref, detail="evidence", at=at)``: the Claim the preview came from, at
    the accepted generation (``at``, its git oid) it was read at, so an older
    revision or a value since replaced reads back as exactly what was previewed.
    Its ``evidence.value`` is the whole value; the SDK's ``cx.read_whole(preview)``
    makes this read.
    """

    ref: str
    detail: Literal["evidence"] = "evidence"
    at: str = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class TruncatedText(_StrictReadValueModel):
    """A preview of a string value too long to show whole -- never the value itself.

    ``preview`` is the value's first ``GET_SUMMARY_TEXT_MAX_CHARS`` characters
    and ``length`` its whole length. ``read_whole`` is the exact read of the
    whole value when one Claim at one generation backs the preview (get cards,
    history, query Claim cells and ``World.values``, a write's ``before``). A
    plain query row cell has none: ask the query with ``claims=True`` and use
    each Claim's preview; a write's ``after`` is the value the write sent.
    """

    truncated: Literal[True]
    preview: str
    length: int = Field(gt=GET_SUMMARY_TEXT_MAX_CHARS)
    read_whole: WholeValueRead | None = Field(default=None, exclude_if=_omit_none)


class ExactContentRef(_StrictReadValueModel):
    """An exact-content Claim value shown by digest, because it cannot be shown as text.

    An exact-content value reads as its UTF-8 text wherever a value is shown.
    This marker stands in for the text when there is none to show: the bytes are
    not UTF-8 text (``binary``), or the store no longer holds them
    (``unavailable``). It never raises. Every caller who may read a Claim reads
    its exact-content value; nothing is withheld by permission.
    """

    exact_content: Literal["binary", "unavailable"]
    content_digest: str
    # The value's length in bytes, when it is known without reading the bytes
    # (the accepted span) or they were read.
    length: int | None = Field(default=None, ge=0, exclude_if=_omit_none)


class ShownObject(_StrictReadValueModel):
    """An object value, shown whole: ``object`` is the value itself, never cut.

    The wrapper is what keeps an object value from reading as a marker: an
    object Claim holding ``{"preview": "cover", "length": 600}`` is shown as
    ``{"object": {"preview": "cover", "length": 600}}`` and reads back as that
    object. Nothing inside ``object`` is interpreted.
    """

    object: dict[str, Any]


#: A value as a summary read shows it: a TruncatedText preview of a long string,
#: an ExactContentRef marker, a ShownObject for an object value, or the value
#: itself (a list element-wise). The value is a string so the alias can name itself.
ShownValue = TypeAliasType(
    "ShownValue",
    "TruncatedText | ExactContentRef | ShownObject | str | bool | int | float | None"
    " | list[ShownValue]",
)


#: A Claim ID as a text line shows it: ``CLM-`` and the first 12 hex digits.
CLAIM_LINE_ID_CHARS = len("CLM-") + 12


class ClaimLine(_StrictReadValueModel):
    """One Claim as a text line shows it: the Subject, the field, the value, the Claim.

    Every surface that prints a line about a Claim (``since``, ``coverage
    resolve``, ``audit``, refusals) carries this and renders it through
    ``get_display.render_claim_line``, so the line reads the same everywhere:
    ``subject  field[qualifier] = value  [CLM-<12 hex>]``. ``subject`` is
    ``kind/id``; ``field`` is the short field name ``get`` and ``orient`` show;
    ``value`` is the summary value (a long string is a ``TruncatedText``
    preview). Digests and artifact paths stay in the typed record beside it.
    """

    claim: str = Field(pattern=r"^CLM-[0-9a-f]{32}$")
    subject: str
    field: str
    qualifier: str | None = Field(default=None, exclude_if=_omit_none)
    value: ShownValue

    def __str__(self) -> str:
        from cruxible_client.contracts.get_display import render_claim_line

        return render_claim_line(self)


def summary_value(value: Any, *, read_whole: WholeValueRead | None = None) -> ShownValue:
    """A value as a summary read shows it: long strings cut, objects wrapped, lists element-wise.

    ``read_whole`` is the exact read of the value a cut preview names, when one
    Claim at one generation backs it. A marker already shown stays itself.
    """

    if isinstance(value, str) and len(value) > GET_SUMMARY_TEXT_MAX_CHARS:
        return TruncatedText(
            truncated=True,
            preview=value[:GET_SUMMARY_TEXT_MAX_CHARS],
            length=len(value),
            read_whole=read_whole,
        )
    if isinstance(value, list | tuple):
        return [summary_value(item, read_whole=read_whole) for item in value]
    if isinstance(value, Mapping):
        return ShownObject(object=dict(value))
    return value  # type: ignore[no-any-return]


def shown_plain(value: object) -> object:
    """A shown value as plain JSON data: an object value unwrapped, a marker as its wire form.

    For quoting a shown value in text; never parse the result back as a
    ``ShownValue``.
    """

    if isinstance(value, ShownObject):
        return value.object
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [shown_plain(item) for item in value]
    return value


def whole_value_read(claim: str, git_oid: str) -> WholeValueRead:
    """The exact read of a Claim's value at one accepted generation."""

    return WholeValueRead(ref=claim.removeprefix("Claim:"), at=git_oid)


__all__ = [
    "CLAIM_LINE_ID_CHARS",
    "GET_SUMMARY_TEXT_MAX_CHARS",
    "ClaimLine",
    "ExactContentRef",
    "ShownObject",
    "ShownValue",
    "TruncatedText",
    "WholeValueRead",
    "shown_plain",
    "summary_value",
    "whole_value_read",
]
