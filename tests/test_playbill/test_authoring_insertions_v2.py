"""Flow-B v2 publication wire and reducer laws."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import (
    InsertionAnchorWindowV1,
    InsertionTargetV2,
    PublicationSourceObservationV2,
    WorkingDigestCoordinateV1,
    insertion_target_v2_digest,
    publication_block_id,
    publication_source_observation_v2_digest,
)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _target(content: bytes = b"status: ") -> InsertionTargetV2:
    return InsertionTargetV2(
        source_id="repo.work-items",
        coordinate=WorkingDigestCoordinateV1(
            source_content_digest=_digest(content),
            source_byte_length=len(content),
        ),
        initial_preimage_digest=_digest(content),
        initial_preimage_byte_length=len(content),
        selector=InsertionAnchorWindowV1(
            anchor_content_base64=base64.b64encode(content).decode("ascii"),
            anchor_bytes_digest=_digest(content),
            start_byte=0,
            end_byte=len(content),
            insertion_offset=len(content),
            observed_occurrence_count=1,
        ),
        operation="insert_after",
    )


def test_v2_target_and_source_observation_digest_exact_bytes() -> None:
    target = _target()
    source = PublicationSourceObservationV2(
        source_id=target.source_id,
        content_base64=base64.b64encode(b"status: ").decode("ascii"),
        content_digest=_digest(b"status: "),
        byte_length=8,
    )

    assert insertion_target_v2_digest(target).startswith("sha256:")
    assert publication_source_observation_v2_digest(source).startswith("sha256:")
    assert source.content == b"status: "


def test_v2_source_observation_refuses_noncanonical_base64_and_wrong_digest() -> None:
    with pytest.raises(ValidationError, match="canonical base64"):
        PublicationSourceObservationV2(
            source_id="repo.work-items",
            content_base64="c3RhdHVzOiA",
            content_digest=_digest(b"status: "),
            byte_length=8,
        )
    with pytest.raises(ValidationError, match="digest does not reproduce"):
        PublicationSourceObservationV2(
            source_id="repo.work-items",
            content_base64=base64.b64encode(b"status: ").decode("ascii"),
            content_digest=_digest(b"different"),
            byte_length=8,
        )


def test_publication_block_id_is_deterministic_and_parser_safe() -> None:
    expectation_id = _digest(b"expectation")
    first = publication_block_id(expectation_id)

    assert first == publication_block_id(expectation_id)
    assert first.startswith("pub-")
    assert len(first) == 36


# Kept here so the frozen test module owns one absolute instant used by the
# reducer cases added below without allowing wall-clock construction.
EVALUATION_TIME = datetime(2026, 8, 26, 12, tzinfo=UTC)
