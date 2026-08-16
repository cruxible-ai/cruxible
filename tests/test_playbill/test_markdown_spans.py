from __future__ import annotations

import hashlib

from cruxible_core.playbill.markdown_spans import parse_markdown_spans


def test_markdown_spans_are_deterministic_and_bound_exact_original_bytes() -> None:
    content = (
        b"---\r\nowner: ops\r\n---\r\n"
        b"# Release \xe2\x9c\x93\r\n\r\n"
        b"The lot is ready.\r\n\r\n"
        b"- inspect\r\n- approve\r\n"
    )
    first = parse_markdown_spans(content)
    assert first == parse_markdown_spans(content)
    assert {item.block_type for item in first}.issuperset(
        {"front_matter", "section", "paragraph", "list", "list_item"}
    )
    for span in first:
        exact = content[span.start_byte : span.end_byte]
        assert span.span_content_digest == "sha256:" + hashlib.sha256(exact).hexdigest()
        assert span.source_content_digest == "sha256:" + hashlib.sha256(content).hexdigest()


def test_span_identity_changes_with_source_bytes_not_caller_naming() -> None:
    original = parse_markdown_spans(b"# Decision\n\nUse route A.\n")
    changed = parse_markdown_spans(b"# Decision\n\nUse route B.\n")
    original_paragraph = next(item for item in original if item.block_type == "paragraph")
    changed_paragraph = next(item for item in changed if item.block_type == "paragraph")
    assert original_paragraph.span_id != changed_paragraph.span_id
    assert original_paragraph.heading_path == ("Decision",)
