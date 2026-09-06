"""Public intent responses preserve V2 reference assertions without changing V1."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import contracts
from cruxible_client.contracts.authoring.models import AuthoringIntentV1, AuthoringIntentV2
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.test_playbill.test_authoring_preflight import _self_source_payload
from tests.test_playbill.test_authoring_reference_expectations import _expectation


@pytest.mark.parametrize("version", [1, 2])
def test_public_create_get_resume_pending_and_submit_preserve_intent_version(
    playbill_http: tuple[TestClient, str, Path], version: int
) -> None:
    client, instance_id, _key = playbill_http
    instance = get_playbill_manager().get(instance_id)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    references = [item.model_dump(mode="json") for item in _expectation(coordinate)]
    request = {
        "tag": f"playbill-authoring-intent-create-request-v{version}",
        "payload": _self_source_payload().model_dump(mode="json"),
    }
    if version == 2:
        request["reference_expectations"] = references
    base = f"/api/v1/{instance_id}/playbill/authoring/intents"
    created = client.post(base, json=request)
    assert created.status_code == 200, created.text
    expected = created.json()["intent"]
    intent_id = expected["intent_id"]
    model = AuthoringIntentV2 if version == 2 else AuthoringIntentV1
    assert model.model_validate(expected).model_dump(mode="json") == expected
    if version == 2:
        assert expected["reference_expectations"] == references
    else:
        assert "reference_expectations" not in expected
    got = client.get(f"{base}/{intent_id}")
    resumed = client.get(f"{base}/{intent_id}/resume")
    pending = client.get(base)
    submitted = client.post(
        f"{base}/{intent_id}/submit", json={"tag": "playbill-authoring-intent-submit-request-v1"}
    )
    for response in (got, resumed, pending, submitted):
        assert response.status_code == 200, response.text
    assert contracts.PlaybillAuthoringIntentView.model_validate(got.json()).intent == expected
    assert resumed.json()["intent"] == expected
    assert pending.json()["intents"] == [expected]
    # Missing Subject/ClaimType refuses this unseeded fixture; its response must
    # still retain the assertions rather than silently returning a V1 shape.
    submitted_intent = submitted.json()["intent"]
    restored = model.model_validate(submitted_intent)
    assert restored.intent_id == intent_id
    if version == 2:
        assert submitted_intent["reference_expectations"] == references
