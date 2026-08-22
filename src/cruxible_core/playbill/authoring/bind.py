"""Client-side Flow-A selection derivation; no locator crosses the wire."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from cruxible_core.playbill.authoring.models import (
    AuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.errors import PlaybillError


class AuthoringBindError(PlaybillError):
    """A local Flow-A bind input could not produce one mechanical observation."""


class AuthoringBindAmbiguityError(AuthoringBindError):
    """The requested anchor did not identify exactly one byte occurrence."""

    code = "playbill.authoring.anchor_ambiguous"

    def __init__(self, offsets: tuple[int, ...]) -> None:
        self.observed_occurrence_count = len(offsets)
        self.candidate_byte_offsets = offsets
        super().__init__(
            self.code
            + ": "
            + canonical_bytes(
                {
                    "observed_occurrence_count": len(offsets),
                    "candidate_byte_offsets": list(offsets),
                }
            ).decode("utf-8")
        )


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _anchor_offsets(content: bytes, anchor: bytes) -> tuple[int, ...]:
    if not anchor:
        raise AuthoringBindError("Flow-A anchor must not be empty")
    found: list[int] = []
    cursor = 0
    while cursor <= len(content) - len(anchor):
        offset = content.find(anchor, cursor)
        if offset < 0:
            break
        found.append(offset)
        cursor = offset + 1
    return tuple(found)


def _line_window(
    content: bytes,
    *,
    start: int,
    end: int,
    surrounding_lines: int,
) -> tuple[int, int]:
    window_start = content.rfind(b"\n", 0, start) + 1
    last_occupied = end - 1
    closing_newline = content.find(b"\n", last_occupied)
    window_end = len(content) if closing_newline < 0 else closing_newline + 1

    for _ in range(surrounding_lines):
        if window_start == 0:
            break
        window_start = content.rfind(b"\n", 0, window_start - 1) + 1
    for _ in range(surrounding_lines):
        if window_end >= len(content):
            break
        closing_newline = content.find(b"\n", window_end)
        window_end = len(content) if closing_newline < 0 else closing_newline + 1
    return window_start, window_end


def bind_working_selection(
    stub: Mapping[str, Any],
    *,
    content: bytes,
    anchor: str,
    window_lines: int | None = None,
) -> ClaimAuthoringPayloadV1:
    """Merge one exact local selection into a Claim authoring stub."""

    if window_lines is not None and window_lines < 0:
        raise AuthoringBindError("--window-lines must be nonnegative")
    source = stub.get("source")
    if not isinstance(source, Mapping):
        raise AuthoringBindError(
            "Flow-A bind stub source must name the working observation tag and source_id"
        )
    expected_keys = {"tag", "source_id"}
    if set(source) != expected_keys:
        unexpected = sorted(set(source) - expected_keys)
        missing = sorted(expected_keys - set(source))
        raise AuthoringBindError(
            "Flow-A bind source must contain exactly tag and source_id; "
            f"unexpected={unexpected!r}; missing={missing!r}"
        )
    if source.get("tag") != "playbill-working-selection-observation-v1":
        raise AuthoringBindError(
            "Flow-A bind source tag must be playbill-working-selection-observation-v1"
        )
    source_id = source.get("source_id")
    if not isinstance(source_id, str):
        raise AuthoringBindError("Flow-A bind source_id must be a string")
    if stub.get("citation_role") not in {"evidence", "copy"}:
        raise AuthoringBindError("Flow-A bind stub requires citation_role=evidence|copy")

    anchor_bytes = anchor.encode("utf-8")
    offsets = _anchor_offsets(content, anchor_bytes)
    if len(offsets) != 1:
        raise AuthoringBindAmbiguityError(offsets)
    start = offsets[0]
    end = start + len(anchor_bytes)
    if window_lines is not None:
        start, end = _line_window(
            content,
            start=start,
            end=end,
            surrounding_lines=window_lines,
        )
    selected = content[start:end]
    observation = WorkingSelectionObservationV1(
        source_id=source_id,
        coordinate=WorkingDigestCoordinateV1(
            source_content_digest=_sha256(content),
            source_byte_length=len(content),
        ),
        selected_content_base64=base64.b64encode(selected).decode("ascii"),
        selected_bytes_digest=_sha256(selected),
        selector=WorkingAnchorWindowV1(
            anchor=anchor,
            start_byte=start,
            end_byte=end,
            observed_occurrence_count=1,
        ),
    )
    payload = dict(stub)
    payload["source"] = observation.model_dump(mode="json")
    try:
        parsed: AuthoringPayloadV1 = TypeAdapter(AuthoringPayloadV1).validate_python(payload)
    except ValidationError as exc:
        raise AuthoringBindError(str(exc)) from exc
    if not isinstance(parsed, ClaimAuthoringPayloadV1):
        raise AuthoringBindError("Flow-A bind accepts only Claim authoring payloads")
    return parsed


__all__ = [
    "AuthoringBindAmbiguityError",
    "AuthoringBindError",
    "bind_working_selection",
]
