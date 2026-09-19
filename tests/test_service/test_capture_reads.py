"""Retained Capture reads enforce access, verification and bounded material."""

from pathlib import Path

import pytest

from cruxible_client.authoring.sdk_types import CaptureView
from cruxible_client.contracts.capture_reads import CaptureReadRequestV1
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_core.errors import PermissionDeniedError
from cruxible_core.service.evidence.capture_reads import (
    CaptureReadInvalid,
    service_read_playbill_capture,
)
from tests.test_authoring.test_authoring_existing_capture import shared_capture_world


def test_capture_read_is_verified_bounded_and_does_not_refetch(tmp_path: Path) -> None:
    instance, _owner, _actor, _first, _second, _coordinator, payload = shared_capture_world(
        tmp_path
    )
    access = BodyAccessContext(principal_id="reader", can_read_body=True)
    request = CaptureReadRequestV1(capture_digest=payload.source.capture_digest)
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
    with pytest.raises(CaptureReadInvalid, match="verification failed"):
        service_read_playbill_capture(
            instance,
            request=CaptureReadRequestV1(capture_digest=result.envelope.commitment.digest),
            access=access,
        )
    # Missing content is explicit and never reconstructed by rereading the source.
    instance.body_store().erase(result.envelope.commitment.digest)
    missing = service_read_playbill_capture(instance, request=request, access=access)
    assert missing.status == "unavailable"
    assert missing.reason == "body_unavailable"


def test_capture_read_denies_before_accessing_instance() -> None:
    with pytest.raises(PermissionDeniedError, match="requires"):
        service_read_playbill_capture(
            None,  # type: ignore[arg-type]
            request=CaptureReadRequestV1(capture_digest="sha256:" + "0" * 64),
            access=BodyAccessContext(principal_id="reader", can_read_body=False),
        )
