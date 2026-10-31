"""Nested intent versions retain and validate their own response fields."""

from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import (
    AuthoringIntent,
    AuthoringIntentList,
    AuthoringIntentView,
    AuthoringSubmitResult,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from tests.test_authoring.test_authoring_intents import TIMESTAMP, _coordinator, _payload
from tests.test_authoring.test_authoring_reference_expectations import _expectation


@pytest.mark.parametrize("version", [1, 2])
def test_all_response_wrappers_round_trip_the_selected_intent_version(
    tmp_path: Path, version: int
) -> None:
    coordinator, actor = _coordinator(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(coordinator.instance.accepted_coordinate())
    references = _expectation(coordinate) if version == 2 else None
    intent = coordinator.create(
        actor=actor,
        payload=_payload(),
        canonical_timestamp=TIMESTAMP,
        reference_expectations=references,
    ).intent
    wrappers = (
        AuthoringIntentView(intent=intent),
        AuthoringIntentList(intents=(intent,)),
        AuthoringSubmitResult(intent=intent, status=intent.candidate_status),
    )
    for response in wrappers:
        wire = response.model_dump(mode="json")
        nested = wire["intents"][0] if isinstance(response, AuthoringIntentList) else wire["intent"]
        assert nested == intent.model_dump(mode="json")
        restored = type(response).model_validate_json(response.model_dump_json())
        restored_intent = (
            restored.intents[0] if isinstance(restored, AuthoringIntentList) else restored.intent
        )
        assert type(restored_intent) is AuthoringIntent
        assert restored_intent == intent
        if version == 2:
            assert nested["reference_expectations"] == [
                item.model_dump(mode="json") for item in references
            ]
            # Omitted and asserted expectations stay distinct: dropping them reads
            # back as an intent that asserts none, never as this one.
            del nested["reference_expectations"]
            trimmed = type(response).model_validate(wire)
            trimmed_intent = (
                trimmed.intents[0] if isinstance(trimmed, AuthoringIntentList) else trimmed.intent
            )
            assert trimmed_intent.reference_expectations is None
            assert trimmed_intent != intent
        else:
            assert "reference_expectations" not in nested
