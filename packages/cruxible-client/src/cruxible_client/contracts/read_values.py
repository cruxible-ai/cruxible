"""How a read shows a Claim value: whole, as a typed preview, or as a marker.

Summary reads (``get`` cards, ``query`` rows and cells, ``World.values``, write
outcomes) bound each value: a string over ``GET_SUMMARY_TEXT_MAX_CHARS``
characters is cut to a :class:`TruncatedText`, a declared preview that can never
be mistaken for the value, and an exact-content value with no text to show is an
:class:`ExactContentRef`. Every other value is itself. ``ShownValue`` is that
typed union; ``get(claim, detail="evidence")`` reads a value whole.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypeAliasType

#: A summary read shows at most this many characters of one string value.
GET_SUMMARY_TEXT_MAX_CHARS = 500

#: The read that returns a cut value whole: ``get`` on the Claim behind it.
TRUNCATED_TEXT_READ_WHOLE: Final = 'get(claim, detail="evidence")'


class _StrictReadValueModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _omit_none(value: object) -> bool:
    return value is None


class TruncatedText(_StrictReadValueModel):
    """A preview of a string value too long to show whole -- never the value itself.

    ``preview`` is the value's first ``GET_SUMMARY_TEXT_MAX_CHARS`` characters
    and ``length`` its whole length. ``read_whole`` names the read that returns
    the whole value: ``get`` on the Claim this value belongs to (the ``claim``
    beside it) with ``detail="evidence"``, whose ``evidence.value`` is the value.
    """

    truncated: Literal[True] = True
    preview: str
    length: int = Field(gt=GET_SUMMARY_TEXT_MAX_CHARS)
    read_whole: Literal['get(claim, detail="evidence")'] = 'get(claim, detail="evidence")'


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


#: A value as a summary read shows it: a TruncatedText preview of a long string,
#: an ExactContentRef marker, or the value itself (a list element-wise). The
#: value is a string so the alias can name itself.
ShownValue = TypeAliasType(
    "ShownValue",
    "TruncatedText | ExactContentRef | str | bool | int | float | None"
    " | list[ShownValue] | dict[str, Any]",
)


def summary_value(value: Any) -> ShownValue:
    """A value as a summary read shows it: long strings cut, lists element-wise."""

    if isinstance(value, str) and len(value) > GET_SUMMARY_TEXT_MAX_CHARS:
        return TruncatedText(preview=value[:GET_SUMMARY_TEXT_MAX_CHARS], length=len(value))
    if isinstance(value, list | tuple):
        return [summary_value(item) for item in value]
    return value  # type: ignore[no-any-return]


__all__ = [
    "GET_SUMMARY_TEXT_MAX_CHARS",
    "TRUNCATED_TEXT_READ_WHOLE",
    "ExactContentRef",
    "ShownValue",
    "TruncatedText",
    "summary_value",
]
