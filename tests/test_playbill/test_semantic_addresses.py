"""PB-C semantic-address and exact-content-span protocol tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.semantic import (
    ContentSpan,
    SemanticAddress,
    SemanticSelector,
    SourceMapping,
    registered_selector_schemes,
    whole_body_mapping,
)
from cruxible_core.playbill.cas import ContentAddressedBodyStore


def test_whole_document_address_is_stable_while_exact_body_span_changes() -> None:
    path = "documents/playbill-design.yaml"
    subject_before = SemanticAddress.whole_artifact(path)
    subject_after = SemanticAddress.whole_artifact(path)
    body_before = b"Heading\nBody\n"
    body_after = b"\n\nHeading\nBody\n"
    digest_before = ContentAddressedBodyStore.digest_bytes(body_before).tagged
    digest_after = ContentAddressedBodyStore.digest_bytes(body_after).tagged

    mapping_before = whole_body_mapping(path, digest_before, len(body_before))
    mapping_after = whole_body_mapping(path, digest_after, len(body_after))
    assert subject_before == subject_after
    assert mapping_before.subject == mapping_after.subject
    assert mapping_before.spans != mapping_after.spans
    assert mapping_before.spans[0].content_digest != mapping_after.spans[0].content_digest


def test_content_spans_count_utf8_bytes_not_characters_or_utf16_units() -> None:
    body = "A café 🍞\n".encode("utf-8")
    digest = ContentAddressedBodyStore.digest_bytes(body).tagged
    mapping = whole_body_mapping("documents/unicode.yaml", digest, len(body))
    span = mapping.spans[0]
    assert span.start_byte == 0
    assert span.end_byte == len(body)
    assert span.end_byte != len(body.decode("utf-8"))


def test_unknown_line_number_and_malformed_selectors_refuse() -> None:
    assert registered_selector_schemes() == (
        "artifact-v1",
        "claim-statement-v1",
        "line-v1",
        "procedure-arm-v1",
        "procedure-node-v1",
        "procedure-unit-v1",
    )
    with pytest.raises(ValidationError, match="unknown semantic selector"):
        SemanticSelector(scheme="line-number-v1", value="12")
    with pytest.raises(ValidationError, match="must be empty"):
        SemanticSelector(scheme="artifact-v1", value="/headings/intro")
    with pytest.raises(ValidationError, match="must be empty"):
        SemanticSelector(scheme="claim-statement-v1", value="line:12")
    with pytest.raises(ValidationError, match="ledger path"):
        SemanticAddress.whole_artifact("/Users/example/document.md")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SemanticAddress.model_validate(
            {
                "artifact_path": "documents/example.yaml",
                "selector": {"scheme": "artifact-v1", "value": ""},
                "editor_uri": "file:///tmp/example.md",
            }
        )


def test_invalid_ranges_and_unsorted_duplicate_source_mappings_refuse() -> None:
    digest = "sha256:" + "11" * 32
    with pytest.raises(ValidationError, match="byte range"):
        ContentSpan(content_digest=digest, start_byte=3, end_byte=2)

    subject = SemanticAddress.whole_artifact("documents/example.yaml")
    first = ContentSpan(content_digest=digest, start_byte=0, end_byte=1)
    second = ContentSpan(content_digest=digest, start_byte=1, end_byte=2)
    with pytest.raises(ValidationError, match="sorted and unique"):
        SourceMapping(subject=subject, spans=(second, first))
    with pytest.raises(ValidationError, match="sorted and unique"):
        SourceMapping(subject=subject, spans=(first, first))
