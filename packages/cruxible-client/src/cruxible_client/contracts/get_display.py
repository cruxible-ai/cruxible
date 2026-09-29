"""One CLI value preview rule for rendering and evidence suggestions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from cruxible_client._error_base import printable

GET_CLI_VALUE_WIDTH = 120
GET_CLI_HISTORY_VALUE_WIDTH = 80


def exact_content_marker_text(marker: Mapping[str, object]) -> str:
    """An exact-content value shown by digest (binary or unavailable), in one line."""

    digest = str(marker.get("content_digest", ""))
    algorithm, _, hexdigest = digest.partition(":")
    short = f"{algorithm}:{hexdigest[:12]}" if hexdigest else digest
    length = marker.get("length")
    size = f" {length} bytes" if isinstance(length, int) else ""
    reason = {
        "binary": "binary",
        "unavailable": "unavailable",
    }.get(str(marker.get("exact_content")), str(marker.get("exact_content")))
    return f"<{reason}{size} {short}>"


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


def get_value_display(
    value: object, *, width: int = GET_CLI_VALUE_WIDTH, evidence_hint: bool = True
) -> GetValueDisplay:
    """Preview a value, reporting every cut including escaped text and JSON.

    The service uses the same result to suggest evidence before the CLI renders
    it. Exact-content markers name unavailable text and are never shortened.
    """
    value = _wire_value(value)
    if isinstance(value, dict) and "exact_content" in value and "content_digest" in value:
        return GetValueDisplay(exact_content_marker_text(value))
    if isinstance(value, dict) and value.get("truncated") is True and "length" in value:
        text = printable(str(value.get("value", "")))
        preview = text[: max(1, width - 24) - 1]
        return GetValueDisplay(
            f"{preview}… ({value['length']} chars; --detail evidence for all)", True
        )
    if isinstance(value, list) and any(
        isinstance(item, dict) and ("truncated" in item or "exact_content" in item)
        for item in value
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
