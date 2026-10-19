"""Additive location/section fields must not change historical digest preimages."""

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import (
    AUTHORING_PAYLOAD_DIGEST_DOMAIN,
    ClaimAuthoringPayload,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    authoring_payload_digest,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    FOREIGN_SOURCE_SELECTOR_TYPE,
    build_working_selection_capture,
    foreign_source_capture_contract,
    verify_capture,
)
from cruxible_client.contracts.source_references import (
    ExternalSourceReference,
    SourceSchemaRegistry,
)
from tests.core_support._pc_c_support import NOW, body_store
from tests.test_authoring.test_authoring_preflight import _self_source_payload, _working_payload
from tests.test_client.test_doc_write_contracts import _observation
from tests.test_procedures.test_procedure_execution import _coordinate

PAYLOADS = (ClaimAuthoringPayloadV1, ClaimAuthoringPayloadV2, ClaimAuthoringPayload)


def _historical(model: type[ClaimAuthoringPayloadV1]) -> dict[str, Any]:
    raw = _working_payload(occurrence_count=1).model_dump(mode="json")
    raw.pop("located_at", None)
    raw["source"]["selector"].pop("section", None)
    if model is not ClaimAuthoringPayloadV1:
        raw["tag"] = (
            "playbill-claim-authoring-payload-v2"
            if model is ClaimAuthoringPayloadV2
            else "playbill-claim-authoring-payload-v3"
        )
        raw["dependency_drafts"] = {
            "tag": "playbill-claim-dependency-drafts-v1",
            "subject": None,
            "claim_type": None,
        }
    return raw


@pytest.mark.parametrize("model", PAYLOADS)
def test_old_payload_bytes_and_digest_are_unchanged(model: type[ClaimAuthoringPayloadV1]) -> None:
    raw = _historical(model)
    preimage = {key: value for key, value in raw.items() if key != "tag"}
    historical_digest = typed_digest(Sha256Value, AUTHORING_PAYLOAD_DIGEST_DOMAIN, preimage).tagged
    parsed = model.model_validate(raw)
    assert parsed.model_dump(mode="json") == raw
    assert authoring_payload_digest(parsed) == historical_digest
    assert model.model_validate_json(parsed.model_dump_json()).model_dump(mode="json") == raw


@pytest.mark.parametrize("model", PAYLOADS)
@pytest.mark.parametrize("location", ["absent", "null", "present"])
@pytest.mark.parametrize("section", ["absent", "null", "present"])
def test_presence_and_values_survive_round_trip(
    model: type[ClaimAuthoringPayloadV1],
    location: str,
    section: str,
) -> None:
    raw = _historical(model)
    if location != "absent":
        raw["located_at"] = None if location == "null" else _observation().model_dump(mode="json")
    if section != "absent":
        raw["source"]["selector"]["section"] = (
            None if section == "null" else {"path": ["Reason"], "extent": "section"}
        )
    parsed = model.model_validate(raw)
    assert parsed.model_dump(mode="json") == raw
    assert model.model_validate_json(parsed.model_dump_json()).model_dump(mode="json") == raw
    assert (
        authoring_payload_digest(parsed)
        == typed_digest(
            Sha256Value,
            AUTHORING_PAYLOAD_DIGEST_DOMAIN,
            {key: value for key, value in raw.items() if key != "tag"},
        ).tagged
    )


def test_missing_null_and_present_are_distinct_commitments() -> None:
    raw = _self_source_payload().model_dump(mode="json")
    wires = [
        raw,
        {**raw, "located_at": None},
        {**raw, "located_at": _observation().model_dump(mode="json")},
    ]
    assert (
        len(
            {
                authoring_payload_digest(ClaimAuthoringPayloadV1.model_validate(wire))
                for wire in wires
            }
        )
        == 3
    )
    tampered = copy.deepcopy(wires[-1])
    tampered["located_at"]["selected_bytes_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="does not reproduce"):
        ClaimAuthoringPayloadV1.model_validate(tampered)


def test_section_label_accepted_by_existing_registry_capture_and_verifier(tmp_path: Path) -> None:
    observation = _observation()
    store = body_store(tmp_path)
    result = build_working_selection_capture(
        store=store,
        actor_id="owner",
        claim_id="CLM-" + "a" * 32,
        rationale="The section explains the decision.",
        observed_at=NOW,
        accepted_coordinate=_coordinate(),
        source_id=observation.source_id,
        coordinate=observation.coordinate.model_dump(mode="json"),
        selector=observation.selector.model_dump(mode="json"),
        selected_content=observation.selected_content,
    )
    source = result.envelope.source
    assert isinstance(source, ExternalSourceReference)
    registry = SourceSchemaRegistry(
        coordinate_types=(FOREIGN_SOURCE_COORDINATE_TYPE,),
        selector_types=(FOREIGN_SOURCE_SELECTOR_TYPE,),
    )
    registry.require(source)
    assert source.selector["working_selection"]["section"] == {
        "path": ["Reason"],
        "extent": "section",
    }
    assert (
        verify_capture(
            result.capture_digest,
            store=store,
            contract=foreign_source_capture_contract(observation.source_id),
        )
        == result.envelope
    )


def test_section_label_does_not_relax_existing_locator_policy() -> None:
    # Registry accepts the added key; the independent locator rule still applies
    # to its text. This limitation is reported for the later integration slice.
    observation = _observation()
    selector = observation.selector.model_dump(mode="json")
    selector["section"]["path"] = ["https://example.com"]
    with pytest.raises(ValidationError, match="cannot carry locators"):
        ExternalSourceReference(
            source_identity="repo.notes",
            producer_binding_digest="sha256:" + "a" * 64,
            coordinate_type=FOREIGN_SOURCE_COORDINATE_TYPE,
            coordinate={},
            selector_type=FOREIGN_SOURCE_SELECTOR_TYPE,
            selector=selector,
            replayability="attested_only",
        )
