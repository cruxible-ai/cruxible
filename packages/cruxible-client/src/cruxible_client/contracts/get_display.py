"""One CLI value preview rule for rendering and evidence suggestions."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, TypeAdapter, ValidationError

from cruxible_client._error_base import printable
from cruxible_client.contracts.read_values import ExactContentRef, ShownValue, TruncatedText

GET_CLI_VALUE_WIDTH = 120
GET_CLI_HISTORY_VALUE_WIDTH = 80

_SHOWN: TypeAdapter[ShownValue] = TypeAdapter(ShownValue)


def exact_content_marker_text(marker: ExactContentRef) -> str:
    """An exact-content value shown by digest (binary or unavailable), in one line."""

    algorithm, _, hexdigest = marker.content_digest.partition(":")
    short = f"{algorithm}:{hexdigest[:12]}" if hexdigest else marker.content_digest
    size = f" {marker.length} bytes" if marker.length is not None else ""
    return f"<{marker.exact_content}{size} {short}>"


@dataclass(frozen=True)
class GetValueDisplay:
    text: str
    truncated: bool = False


def _wire_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_wire_value(item) for item in value]
    return value


def _shown(value: object) -> object:
    """The value re-typed as a read shows it: markers and previews are models again.

    A CLI renders a card from its JSON dump, so a ``TruncatedText`` or an
    ``ExactContentRef`` may arrive as its wire object; parsing it back as a
    ``ShownValue`` keeps one typed rule for both.
    """

    if isinstance(value, TruncatedText | ExactContentRef):
        return value
    wire = _wire_value(value)
    try:
        return _SHOWN.validate_python(wire)
    except ValidationError:
        return wire


def get_value_display(
    value: object, *, width: int = GET_CLI_VALUE_WIDTH, evidence_hint: bool = True
) -> GetValueDisplay:
    """Preview a value, reporting every cut including escaped text and JSON.

    The service uses the same result to suggest evidence before the CLI renders
    it. Exact-content markers name unavailable text and are never shortened; a
    ``TruncatedText`` preview always reads as cut.
    """
    value = _shown(value)
    if isinstance(value, ExactContentRef):
        return GetValueDisplay(exact_content_marker_text(value))
    if isinstance(value, TruncatedText):
        text = printable(value.preview)
        preview = text[: max(1, width - 24) - 1]
        return GetValueDisplay(
            f"{preview}… ({value.length} chars; --detail evidence for all)", True
        )
    if isinstance(value, list) and any(
        isinstance(item, TruncatedText | ExactContentRef) for item in value
    ):
        items = [
            get_value_display(item, width=width, evidence_hint=evidence_hint) for item in value
        ]
        return GetValueDisplay(
            "[" + ", ".join(item.text for item in items) + "]",
            any(item.truncated for item in items),
        )
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    shown = printable(text)
    if len(shown) <= width:
        return GetValueDisplay(shown)
    hint = f" ({len(text)} chars; --detail evidence for all)" if evidence_hint else ""
    return GetValueDisplay(f"{shown[: width - 1]}…{hint}", True)
