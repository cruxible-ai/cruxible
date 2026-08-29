"""Pure client-side application of daemon-minted Playbill insertion patches."""

from __future__ import annotations

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

    try:
        assert_projection_block_frame(
            content,
            source_id=preparation.source_id,
            block_id=preparation.block_id,
            stamp=preparation.stamp,
            body_digest=preparation.body_digest,
        )
        updated = content
        outcome: Literal["applied", "already_applied"] = "already_applied"
    except ProjectionMarkerError:
        if f"playbill:block:{preparation.block_id}".encode("ascii") in content:
            raise PlaybillInsertionApplyError(
                "local source contains a conflicting publication block"
            )
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
    try:
        match = assert_projection_block_frame(
            updated,
            source_id=preparation.source_id,
            block_id=preparation.block_id,
            stamp=preparation.stamp,
            body_digest=preparation.body_digest,
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
    "apply_playbill_publication",
    "replace_publication_file",
]
