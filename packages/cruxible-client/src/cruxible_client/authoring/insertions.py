"""Pure client-side application of daemon-minted Playbill insertion patches."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Mapping

from cruxible_client.contracts.authoring.models import (
    InsertionExpectationV2,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionMarkerError,
    assert_projection_block_frame,
    frame_projection_block,
)


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
        raise PlaybillInsertionApplyError(
            "postimage does not contain the body at its committed span"
        )
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


def apply_playbill_publication(
    content: bytes,
    *,
    intent_id: str,
    expectation: Mapping[str, Any],
    retained_body: bytes,
) -> PlaybillInsertionApplication:
    """Apply one durable v2 preparation or recognize its exact stamped postimage."""

    typed_expectation = InsertionExpectationV2.model_validate(expectation)
    preparation = typed_expectation.preparation
    if preparation is None:
        raise PlaybillInsertionApplyError("publication has no durable preparation")
    if (
        _digest(retained_body) != preparation.body_digest
        or len(retained_body) != preparation.body_byte_length
    ):
        raise PlaybillInsertionApplyError(
            "retained accepted body does not reproduce the publication preparation"
        )
    framed = frame_projection_block(stamp=preparation.stamp, body=retained_body)
    if (
        _digest(framed) != preparation.inserted_block_digest
        or len(framed) != preparation.inserted_block_byte_length
    ):
        raise PlaybillInsertionApplyError(
            "retained accepted body does not reproduce the publication preparation"
        )

    current_digest = _digest(content)
    if (
        current_digest == preparation.final_postimage_digest
        and len(content) == preparation.final_postimage_byte_length
    ):
        updated = content
        outcome: Literal["applied", "already_applied"] = "already_applied"
    elif (
        current_digest == preparation.preimage_digest
        and len(content) == preparation.preimage_byte_length
    ):
        selector = preparation.rebased_selector
        anchor = selector.content
        empty_append = (
            preparation.operation == "append"
            and not anchor
            and selector.start_byte == selector.end_byte == selector.insertion_offset
        )
        if content[selector.start_byte : selector.end_byte] != anchor or (
            not empty_append and content.count(anchor) != 1
        ):
            raise PlaybillInsertionApplyError("publication anchor is stale or ambiguous")
        if preparation.operation == "replace_window":
            updated = content[: selector.start_byte] + framed + content[selector.end_byte :]
        else:
            offset = selector.insertion_offset
            updated = content[:offset] + framed + content[offset:]
        outcome = "applied"
    else:
        raise PlaybillInsertionApplyError(
            "local source is neither prepared preimage nor exact final postimage"
        )
    if (
        len(updated) != preparation.final_postimage_byte_length
        or _digest(updated) != preparation.final_postimage_digest
    ):
        raise PlaybillInsertionApplyError(
            "applied publication bytes do not reproduce the committed final postimage"
        )
    try:
        match = assert_projection_block_frame(
            updated,
            source_id=preparation.source_id,
            block_id=preparation.block_id,
            stamp=preparation.stamp,
            body_digest=preparation.body_digest,
            start_byte=preparation.block_start_byte,
            end_byte=preparation.block_end_byte,
        )
    except ProjectionMarkerError as exc:
        raise PlaybillInsertionApplyError(
            "final publication does not reproduce its exact declared block"
        ) from exc
    observation = {
        "tag": "playbill-insertion-confirmation-observation-v2",
        "intent_id": intent_id,
        "expectation_id": typed_expectation.expectation_id,
        "preparation_digest": preparation.preparation_digest,
        "source_id": preparation.source_id,
        "final_postimage_digest": preparation.final_postimage_digest,
        "final_postimage_byte_length": preparation.final_postimage_byte_length,
        "marker_summary": match.summary().model_dump(mode="json"),
        "observed_occurrence_count": 1,
    }
    return PlaybillInsertionApplication(
        outcome=outcome,
        content=updated,
        observation=observation,
    )


def replace_publication_file(
    path: Path,
    *,
    expected: bytes,
    replacement: bytes,
) -> None:
    """Durably replace one exact preimage without overwriting a concurrent edit."""

    temporary: Path | None = None
    try:
        original_mode = path.stat().st_mode
        with NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(replacement)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(original_mode)
        if path.read_bytes() != expected:
            raise PlaybillInsertionApplyError(
                "source bytes changed before the whole-file compare-and-swap"
            )
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise PlaybillInsertionApplyError(
            f"source could not be replaced atomically: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = [
    "PlaybillInsertionApplication",
    "PlaybillInsertionApplyError",
    "apply_playbill_insertion",
    "apply_playbill_publication",
    "replace_publication_file",
]
