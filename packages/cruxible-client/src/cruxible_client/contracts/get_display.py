"""One CLI value preview rule for rendering and evidence suggestions, and one Claim line."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, TypeAdapter, ValidationError

from cruxible_client._error_base import printable
from cruxible_client.contracts.read_values import (
    CLAIM_LINE_ID_CHARS,
    ClaimLine,
    ExactContentRef,
    ShownObject,
    ShownValue,
    TruncatedText,
    shown_plain,
)

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

    if isinstance(value, TruncatedText | ExactContentRef | ShownObject):
        return value
    wire = _wire_value(value)
    try:
        return _SHOWN.validate_python(wire)
    except ValidationError:
        return wire


def _short_claim(claim: str) -> str:
    """A Claim ID as a line prints it; ``get`` resolves the unique prefix."""

    claim = claim.removeprefix("Claim:")
    return claim[:CLAIM_LINE_ID_CHARS] if claim.startswith("CLM-") else claim


def _cut_hint(length: int, claim: str | None) -> str:
    """Where the whole value is: the Claim's evidence read when the Claim is known.

    A Subject card takes no ``--detail evidence``, so a hint that names only the
    flag is runnable only on the Claim's own card; naming the Claim keeps it
    runnable wherever the value is shown.
    """

    if claim is None:
        return f" ({length} chars; --detail evidence for all)"
    return f" ({length} chars; cruxible get {_short_claim(claim)} --detail evidence)"


def get_value_display(
    value: object,
    *,
    width: int = GET_CLI_VALUE_WIDTH,
    evidence_hint: bool = True,
    claim: str | None = None,
) -> GetValueDisplay:
    """Preview a value, reporting every cut including escaped text and JSON.

    The service uses the same result to suggest evidence before the CLI renders
    it. Exact-content markers name unavailable text and are never shortened; a
    ``TruncatedText`` preview always reads as cut. A cut names the read of the
    whole value: the preview's own ``read_whole`` Claim, else ``claim`` (the
    Claim behind the shown value), else the bare ``--detail evidence`` flag.
    """
    value = _shown(value)
    if isinstance(value, ExactContentRef):
        return GetValueDisplay(exact_content_marker_text(value))
    if isinstance(value, TruncatedText):
        text = printable(value.preview)
        preview = text[: max(1, width - 24) - 1]
        source = value.read_whole.ref if value.read_whole is not None else claim
        return GetValueDisplay(f"{preview}…{_cut_hint(value.length, source)}", True)
    if isinstance(value, list) and any(
        isinstance(item, TruncatedText | ExactContentRef) for item in value
    ):
        items = [
            get_value_display(item, width=width, evidence_hint=evidence_hint, claim=claim)
            for item in value
        ]
        return GetValueDisplay(
            "[" + ", ".join(item.text for item in items) + "]",
            any(item.truncated for item in items),
        )
    text = value if isinstance(value, str) else json.dumps(shown_plain(value), ensure_ascii=False)
    shown = printable(text)
    if len(shown) <= width:
        return GetValueDisplay(shown)
    hint = _cut_hint(len(text), claim) if evidence_hint else ""
    return GetValueDisplay(f"{shown[: width - 1]}…{hint}", True)


def render_claim_line(
    line: ClaimLine, *, lead: str | None = None, width: int = GET_CLI_VALUE_WIDTH
) -> str:
    """``[lead  ]subject  field[qualifier] = value  [CLM-<12 hex>]``: one Claim, one line.

    ``lead`` is the row's state or verb (``drifted``, ``create``, a rank). The
    value follows the CLI preview rule, and a cut names
    ``cruxible get CLM-… --detail evidence``. The CLI, the SDK's ``str()`` and
    coverage's text renderer all print Claims through this one function.
    """

    field = line.field if line.qualifier is None else f"{line.field}[{line.qualifier}]"
    value = get_value_display(line.value, width=width, claim=line.claim).text
    parts = [
        *(() if lead is None else (lead,)),
        printable(line.subject),
        f"{printable(field)} = {value}",
        f"[{_short_claim(line.claim)}]",
    ]
    return "  ".join(parts)
