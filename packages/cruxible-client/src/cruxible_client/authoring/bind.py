"""Client-side Flow-A selection derivation; no locator crosses the wire."""

from __future__ import annotations

import base64
import hashlib

from cruxible_client.authoring.blocks import assert_independent_projection_evidence
from cruxible_client.authoring.inputs import ClaimInput, lower_bound_claim_input
from cruxible_client.authoring.selectors import source_content_for_observation
from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillError


class AuthoringBindError(PlaybillError):
    """A local Flow-A bind input could not produce one mechanical observation."""


class AuthoringBindAnchorNotFoundError(AuthoringBindError):
    """The requested anchor was absent from the selected file."""

    code = "playbill.authoring.anchor_not_found"

    def __init__(self) -> None:
        self.observed_occurrence_count = 0
        self.candidate_byte_offsets: tuple[int, ...] = ()
        super().__init__(
            self.code
            + ": "
            + canonical_bytes(
                {
                    "message": "anchor not found in file",
                    "observed_occurrence_count": 0,
                    "candidate_byte_offsets": [],
                }
            ).decode("utf-8")
        )


class AuthoringBindAmbiguityError(AuthoringBindError):
    """The requested anchor identified multiple byte occurrences."""

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
                    "repair": "rerun with --occurrence N",
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


def bind_working_selection_input(
    input: ClaimInput,
    *,
    content: bytes,
    anchor: str,
    window_lines: int | None = None,
    occurrence: int | None = None,
) -> ClaimAuthoringPayloadV1:
    """Observe local bytes for a decision-only working_selection Claim input."""

    if window_lines is not None and window_lines < 0:
        raise AuthoringBindError("--window-lines must be nonnegative")
    source = input.source
    if source.kind != "working_selection":
        raise AuthoringBindError("Flow-A bind input source must be working_selection")
    anchor_bytes = anchor.encode("utf-8")
    offsets = _anchor_offsets(content, anchor_bytes)
    if not offsets and occurrence is None:
        raise AuthoringBindAnchorNotFoundError
    if occurrence is None and len(offsets) > 1:
        raise AuthoringBindAmbiguityError(offsets)
    if occurrence is not None and not 1 <= occurrence <= len(offsets):
        raise AuthoringBindError(
            "invalid --occurrence: must select a 1-based anchor occurrence; "
            f"observed {len(offsets)}, requested {occurrence}"
        )
    start = offsets[0 if occurrence is None else occurrence - 1]
    end = start + len(anchor_bytes)
    if window_lines is not None:
        start, end = _line_window(
            content,
            start=start,
            end=end,
            surrounding_lines=window_lines,
        )
    # Any role: a copy of projection bytes is the same attestation into
    # concrete that evidence from them would be.
    assert_independent_projection_evidence(
        source_id=source.source_id,
        content=content,
        start_byte=start,
        end_byte=end,
    )
    selected = content[start:end]
    observation = WorkingSelectionObservationV1(
        source_id=source.source_id,
        coordinate=WorkingDigestCoordinateV1(
            source_content_digest=_sha256(content),
            source_byte_length=len(content),
        ),
        source_content_base64=source_content_for_observation(content),
        selected_content_base64=base64.b64encode(selected).decode("ascii"),
        selected_bytes_digest=_sha256(selected),
        selector=WorkingAnchorWindowV1(
            anchor=anchor,
            start_byte=start,
            end_byte=end,
            observed_occurrence_count=len(offsets),
            selected_occurrence=occurrence,
        ),
    )
    try:
        return lower_bound_claim_input(input, observation=observation)
    except ValueError as exc:
        raise AuthoringBindError(str(exc)) from exc


__all__ = [
    "AuthoringBindAnchorNotFoundError",
    "AuthoringBindAmbiguityError",
    "AuthoringBindError",
    "bind_working_selection_input",
]
