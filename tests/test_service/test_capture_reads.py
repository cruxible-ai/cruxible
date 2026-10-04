"""Retained Capture reads enforce access, verification and bounded material."""

from pathlib import Path

import pytest

from cruxible_client.authoring.sdk_types import CaptureView
from cruxible_client.contracts.capture_reads import CaptureReadRequest
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import ReadRefusalError
from cruxible_core.errors import PermissionDeniedError
from cruxible_core.service.evidence.capture_reads import (
    service_read_playbill_capture,
)
from tests.core_support._exact_content_support import seed_exact_content
from tests.test_authoring.test_authoring_existing_capture import shared_capture_world


def test_capture_read_is_verified_bounded_and_does_not_refetch(tmp_path: Path) -> None:
    instance, _owner, _actor, _first, _second, _coordinator, payload = shared_capture_world(
        tmp_path
    )
    access = BodyAccessContext(principal_id="reader", can_read_body=True)
    request = CaptureReadRequest(capture_digest=payload.source.capture_digest)
    result = service_read_playbill_capture(instance, request=request, access=access)
    assert result.status == "verified"
    view = CaptureView(result=result)
    assert view.content
    assert view.ref.capture_digest == request.capture_digest
    limited = service_read_playbill_capture(
        instance, request=request.model_copy(update={"max_bytes": 0}), access=access
    )
    assert limited.material.status == "unavailable"
    assert limited.material.coverage.truncated_facets == ("source_material",)
    with pytest.raises(ValueError, match="unavailable"):
        _ = CaptureView(result=limited).content
    # A CAS body is not a Capture; this surface cannot bypass envelope verification.
    with pytest.raises(ReadRefusalError) as not_a_capture:
        service_read_playbill_capture(
            instance,
            request=CaptureReadRequest(capture_digest=result.envelope.commitment.digest),
            access=access,
        )
    assert not_a_capture.value.error_code == "playbill.capture.not_a_capture"
    assert not_a_capture.value.http_status == 404
    # Missing content is explicit and never reconstructed by rereading the source.
    instance.body_store().erase(result.envelope.commitment.digest)
    missing = service_read_playbill_capture(instance, request=request, access=access)
    assert missing.status == "unavailable"
    assert missing.reason == "body_unavailable"


def test_capture_read_denies_before_accessing_instance() -> None:
    with pytest.raises(PermissionDeniedError, match="requires"):
        service_read_playbill_capture(
            None,  # type: ignore[arg-type]
            request=CaptureReadRequest(capture_digest="sha256:" + "0" * 64),
            access=BodyAccessContext(principal_id="reader", can_read_body=False),
        )


def test_an_exact_content_digest_refuses_naming_the_claim_whose_value_it_is(
    tmp_path: Path,
) -> None:
    """Regression: the digest of an exact-content value is not a Capture.

    It names the stored bytes the Claim IS, so reading it as a Capture envelope
    failed "strict versioned validation". The read now says what the bytes are
    and points at get on the Claim, which shows them as text.
    """

    instance, seeded = seed_exact_content(tmp_path, {"wi-42": b"The ruling.\n"})
    ruling = seeded["wi-42"]

    with pytest.raises(ReadRefusalError) as refused:
        service_read_playbill_capture(
            instance,
            request=CaptureReadRequest(capture_digest=ruling.digest),
            access=BodyAccessContext(principal_id="reader", can_read_body=True),
        )

    error = refused.value
    assert error.error_code == "playbill.capture.not_a_capture"
    assert error.http_status == 404
    assert error.candidates == (ruling.claim_id,)
    assert error.repair is not None and error.repair.operation == "playbill.get"
    assert error.repair.arguments == {"ref": ruling.claim_id}
    assert "strict versioned validation" not in str(error)
    assert f"the exact content of Claim {ruling.claim_id}" in str(error)
