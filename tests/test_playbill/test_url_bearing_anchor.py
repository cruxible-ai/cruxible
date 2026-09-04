"""Card 124: an anchor is quoted source bytes, and a URL inside it is bytes.

Every security feed interleaves reference URLs with the fields a Claim rests on,
so an anchor spanning "which CVE" and "what severity" carries a URL by
construction. The locator rule on source selectors is right -- a selector must
never name an address the daemon could fetch -- and the anchor is the one
selector field it never applied to honestly: nothing reads an anchor as an
address. The compile route used to die on it with an unhandled 500.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_MAX_BYTES,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import (
    _coordinator,
    _seed_claim_surface,
    _self_source_payload,
)

SOURCE_ID = "repo.work-items"
TIMESTAMP = "2026-08-21T12:00:00.000000Z"
ADVISORY = (
    b'{"id": "PYSEC-2026-3552",\n'
    b' "references": ["https://osv.dev/vulnerability/PYSEC-2026-3552"],\n'
    b' "severity": "HIGH"}\n'
)


def _reference(selector: object) -> ExternalSourceReferenceV1:
    return ExternalSourceReferenceV1(
        source_identity=SOURCE_ID,
        producer_binding_digest="sha256:" + "1" * 64,
        coordinate_type="foreign-source-snapshot-v1",
        coordinate={"source_byte_length": 3, "source_content_digest": "sha256:" + "2" * 64},
        selector_type="foreign-source-span-v1",
        selector=selector,
        replayability="attested_only",
    )


def test_an_anchor_may_quote_a_url_and_no_other_selector_field_may() -> None:
    quoted = _reference(
        {"claim_id": "CLM-x", "working_selection": {"anchor": "see https://osv.dev/x", "n": 1}}
    )
    assert isinstance(quoted.selector, dict)

    with pytest.raises(ValidationError, match="locator"):
        _reference({"claim_id": "CLM-x", "url": "https://osv.dev/x"})
    with pytest.raises(ValidationError, match="locator"):
        _reference({"working_selection": {"anchor": "fine", "origin": "https://osv.dev/x"}})
    # Credential material has no business in an anchor, whatever it quotes.
    with pytest.raises(ValidationError, match="secret"):
        _reference({"working_selection": {"anchor": "Bearer abc123"}})
    with pytest.raises(ValidationError, match="secret"):
        _reference({"working_selection": {"anchor": "x", "api_key": "y"}})


def _advisory_payload(anchor: bytes, *, content: bytes = ADVISORY) -> ClaimAuthoringPayloadV1:
    start = content.index(anchor)
    end = start + len(anchor)
    return ClaimAuthoringPayloadV1(
        statement=_self_source_payload().statement,
        rationale="The advisory record says so.",
        source=WorkingSelectionObservationV1(
            source_id=SOURCE_ID,
            coordinate=WorkingDigestCoordinateV1(
                source_content_digest="sha256:" + hashlib.sha256(content).hexdigest(),
                source_byte_length=len(content),
            ),
            selected_content_base64=base64.b64encode(content[start:end]).decode("ascii"),
            selected_bytes_digest="sha256:" + hashlib.sha256(content[start:end]).hexdigest(),
            selector=WorkingAnchorWindowV1(
                anchor=anchor.decode("utf-8").strip(),
                start_byte=start,
                end_byte=end,
                observed_occurrence_count=1,
            ),
        ),
        citation_role="evidence",
    )


def _world(tmp_path: Path) -> tuple[AuthoringIntentCoordinator, AuthenticatedActor]:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner, contract=foreign_source_capture_contract(SOURCE_ID))
    return _coordinator(instance), AuthenticatedActor(actor_id="owner")


def test_a_url_bearing_anchor_compiles(tmp_path: Path) -> None:
    """The whole advisory record, references and all, is one lawful anchor."""

    coordinator, actor = _world(tmp_path)
    intent = coordinator.create(
        actor=actor,
        payload=_advisory_payload(ADVISORY),
        canonical_timestamp=TIMESTAMP,
    ).intent

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert result.verdict == "passed", [item.code for item in result.frontier.diagnostics]


def test_a_selection_the_capture_contract_refuses_is_a_typed_refusal(tmp_path: Path) -> None:
    """The capture builder's own refusals reach the caller typed, never as a 500."""

    coordinator, actor = _world(tmp_path)
    oversized = b"x" * (FOREIGN_SOURCE_MAX_BYTES + 1) + b"\n"
    intent = coordinator.create(
        actor=actor,
        payload=_advisory_payload(oversized[:-1], content=oversized),
        canonical_timestamp=TIMESTAMP,
    ).intent

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert result.verdict == "refused"
    (diagnostic,) = [
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.authoring.working_selection_refused"
    ]
    assert diagnostic.offending_element == "source.selector"
    assert "byte budget" in diagnostic.message
    assert diagnostic.repairs[0].kind == "revise_selection"
