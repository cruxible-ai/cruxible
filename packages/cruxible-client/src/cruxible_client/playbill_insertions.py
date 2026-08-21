"""Pure client-side application of daemon-minted Playbill insertion patches."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Mapping


class PlaybillInsertionApplyError(ValueError):
    """A local source cannot be reconciled with its insertion expectation."""


@dataclass(frozen=True)
class PlaybillInsertionApplication:
    outcome: Literal["applied", "already_applied"]
    content: bytes
    observation: dict[str, Any]


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlaybillInsertionApplyError(f"{label} is not an object")
    return value


def apply_playbill_insertion(
    content: bytes,
    *,
    expectation: Mapping[str, Any],
    retained_body: bytes,
) -> PlaybillInsertionApplication:
    """Apply once, or recognize the exact postimage after response loss.

    This adapter never guesses a source path. The caller resolves ``source_id``
    to local bytes, while the adapter owns digest checks, anchor matching, patch
    arithmetic, and the exact confirmation observation sent back to the daemon.
    """

    patch = _mapping(expectation.get("patch"), label="insertion patch")
    selector = _mapping(patch.get("selector"), label="insertion selector")
    expected_body_digest = patch.get("body_digest")
    if _digest(retained_body) != expected_body_digest:
        raise PlaybillInsertionApplyError("retained body differs from the patch commitment")
    current_digest = _digest(content)
    preimage_digest = patch.get("preimage_digest")
    postimage_digest = patch.get("postimage_digest")
    outcome: Literal["applied", "already_applied"]
    if current_digest == postimage_digest:
        updated = content
        outcome = "already_applied"
    elif current_digest == preimage_digest:
        encoded_anchor = selector.get("anchor_content_base64")
        if not isinstance(encoded_anchor, str):
            raise PlaybillInsertionApplyError("insertion anchor bytes are missing")
        try:
            anchor = base64.b64decode(encoded_anchor, validate=True)
        except ValueError as exc:
            raise PlaybillInsertionApplyError("insertion anchor is not canonical base64") from exc
        start = selector.get("start_byte")
        end = selector.get("end_byte")
        offset = selector.get("insertion_offset")
        if not all(isinstance(value, int) for value in (start, end, offset)):
            raise PlaybillInsertionApplyError("insertion selector offsets are malformed")
        assert isinstance(start, int) and isinstance(end, int) and isinstance(offset, int)
        empty_append = (
            patch.get("operation") == "append"
            and not content
            and not anchor
            and start == end == offset == 0
        )
        if content[start:end] != anchor or (not empty_append and content.count(anchor) != 1):
            raise PlaybillInsertionApplyError("insertion anchor is stale or ambiguous")
        operation = patch.get("operation")
        if operation == "replace_window":
            updated = content[:start] + retained_body + content[end:]
        elif operation in {"insert_before", "insert_after", "append"}:
            updated = content[:offset] + retained_body + content[offset:]
        else:
            raise PlaybillInsertionApplyError("insertion operation is unsupported")
        outcome = "applied"
    else:
        raise PlaybillInsertionApplyError("local source is neither patch preimage nor postimage")

    if len(updated) != patch.get("postimage_byte_length") or _digest(updated) != postimage_digest:
        raise PlaybillInsertionApplyError("applied bytes do not reproduce the committed postimage")
    operation = patch.get("operation")
    selector_offset = selector.get("insertion_offset")
    selector_start = selector.get("start_byte")
    if not isinstance(selector_offset, int) or not isinstance(selector_start, int):
        raise PlaybillInsertionApplyError("insertion selector offsets are malformed")
    selected_start = selector_start if operation == "replace_window" else selector_offset
    selected_end = selected_start + len(retained_body)
    if updated[selected_start:selected_end] != retained_body:
        raise PlaybillInsertionApplyError("postimage does not contain the body at its committed span")
    expectation_id = expectation.get("expectation_id")
    source_id = patch.get("source_id")
    if not isinstance(expectation_id, str) or not isinstance(source_id, str):
        raise PlaybillInsertionApplyError("insertion expectation identity is malformed")
    observation = {
        "tag": "playbill-insertion-confirmation-observation-v1",
        "expectation_id": expectation_id,
        "source_id": source_id,
        "coordinate": {
            "kind": "observed_digest",
            "source_content_digest": postimage_digest,
            "source_byte_length": len(updated),
        },
        "observed_content_digest": postimage_digest,
        "selected_start_byte": selected_start,
        "selected_end_byte": selected_end,
        "selected_bytes_digest": expected_body_digest,
        "observed_occurrence_count": 1,
    }
    return PlaybillInsertionApplication(
        outcome=outcome,
        content=updated,
        observation=observation,
    )


__all__ = [
    "PlaybillInsertionApplication",
    "PlaybillInsertionApplyError",
    "apply_playbill_insertion",
]
