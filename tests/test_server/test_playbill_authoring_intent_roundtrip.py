"""Public intent responses preserve V2 reference assertions without changing V1."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import contracts
from cruxible_client.contracts.authoring.models import AuthoringIntent, AuthoringIntentV1
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.test_authoring.test_authoring_preflight import _self_source_payload
from tests.test_authoring.test_authoring_program_stamp import _stamp
from tests.test_authoring.test_authoring_reference_expectations import _expectation


@pytest.mark.parametrize("version", [1, 2])
def test_public_compile_get_pending_and_submit_preserve_intent_version(
    playbill_http: tuple[TestClient, str, Path], version: int
) -> None:
    client, instance_id, _key = playbill_http
    instance = get_playbill_manager().get(instance_id)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    references = [item.model_dump(mode="json") for item in _expectation(coordinate)]
    # Compile with no intent ID creates the intent: the V1 request makes a V1
    # intent, the V3 request (references plus program stamp) a V2 intent.
    request: dict[str, object] = {
        "tag": f"playbill-authoring-intent-compile-request-v{1 if version == 1 else 3}",
        "payload": _self_source_payload().model_dump(mode="json"),
    }
    if version == 2:
        request["reference_expectations"] = references
        request["program_stamp"] = _stamp().model_dump(mode="json")
    authoring = f"/api/v1/{instance_id}/authoring"
    base = f"{authoring}/intents"
    compiled = client.post(f"{authoring}/compile", json=request)
    assert compiled.status_code == 200, compiled.text
    intent_id = compiled.json()["certificate"]["intent_id"]

    got = client.get(f"{base}/{intent_id}")
    pending = client.get(base)
    submitted = client.post(
        f"{base}/{intent_id}/submit", json={"tag": "playbill-authoring-intent-submit-request-v1"}
    )
    for response in (got, pending, submitted):
        assert response.status_code == 200, response.text
    expected = contracts.AuthoringIntentViewRecord.model_validate(got.json()).intent
    assert expected["intent_id"] == intent_id
    model = AuthoringIntent if version == 2 else AuthoringIntentV1
    assert model.model_validate(expected).model_dump(mode="json") == expected
    if version == 2:
        assert expected["reference_expectations"] == references
    else:
        assert "reference_expectations" not in expected
    assert pending.json()["intents"] == [expected]
    # Missing Subject/ClaimType refuses this unseeded fixture; its response must
    # still retain the assertions rather than silently returning a V1 shape.
    submitted_intent = submitted.json()["intent"]
    restored = model.model_validate(submitted_intent)
    assert restored.intent_id == intent_id
    if version == 2:
        assert submitted_intent["reference_expectations"] == references
