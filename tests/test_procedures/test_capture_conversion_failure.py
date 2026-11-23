"""A provider capture field that fails Capture conversion says why (Slice A2).

The run's internal failure journals the node, the field and the conversion
error, bounded and without input values (a Capture wire's input is its
base64 bytes); the served terminal message carries the same cause.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.captures import CaptureFormatError, ProviderResultToExternalCapture
from cruxible_core.procedures.execution import (
    _CONVERSION_DETAIL_MAX_CHARS,
    _capture_conversion_detail,
    _capture_conversion_failure,
)
from cruxible_core.service.procedures.procedure_runs import _internal_failure_detail

SECRET_BYTES = "c2VjcmV0LXBheWxvYWQ="


def test_a_capture_format_error_keeps_its_reason() -> None:
    failure = _capture_conversion_failure(
        "view", "capture", CaptureFormatError("CaptureContract lacks replay-policy")
    )
    assert failure.code == "provider_protocol_violation"
    assert failure.details == {
        "node_id": "view",
        "field": "capture",
        "reason": "Capture conversion failed",
        "error": "CaptureFormatError",
        "detail": "CaptureContract lacks replay-policy",
    }


def test_a_validation_error_names_locations_and_messages_never_inputs() -> None:
    with pytest.raises(ValidationError) as raised:
        ProviderResultToExternalCapture.model_validate(
            {
                "source_identity": "test.view",
                "coordinate_type": "x",
                "coordinate": {},
                "selector_type": "y",
                "selector": {},
                "replayability": "sometimes",
                "content_base64": SECRET_BYTES,
                "byte_length": -1,
                "bytes_digest": "sha256:" + "0" * 64,
                "observed_at": "2026-01-01T00:00:00Z",
            }
        )
    detail = _capture_conversion_detail(raised.value)
    assert "replayability: " in detail and "byte_length: " in detail
    assert SECRET_BYTES not in detail and "sometimes" not in detail
    assert "\n" not in detail


def test_a_long_message_is_bounded_on_one_line() -> None:
    detail = _capture_conversion_detail(ValueError("line\n" * 400))
    assert len(detail) == _CONVERSION_DETAIL_MAX_CHARS
    assert "\n" not in detail and detail.endswith("…")
    assert _capture_conversion_detail(ValueError("")) == "ValueError"


def test_the_terminal_message_appends_a_journaled_detail_only() -> None:
    assert _internal_failure_detail(
        {
            "node_id": "view",
            "field": "capture",
            "reason": "Capture conversion failed",
            "detail": "CaptureContract lacks replay-policy",
        }
    ) == (" Capture conversion failed at view.capture: CaptureContract lacks replay-policy")
    # A failure journaled before the detail existed keeps its message's bytes.
    for legacy in (
        {"node_id": "view", "reason": "Capture conversion failed"},
        {},
        None,
        [],
    ):
        assert _internal_failure_detail(legacy) == ""
