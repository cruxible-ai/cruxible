"""Nested intent versions retain and validate their own response fields."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import (
    AuthoringIntentListV1,
    AuthoringIntentV1,
    AuthoringIntentV2,
    AuthoringIntentViewV1,
    AuthoringSubmitResultV1,
    InsertionAbandonResultV1,
    InsertionConfirmResultV2,
    InsertionExpectationV2,
    InsertionPrepareResultV2,
    insertion_expectation_v2_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from tests.test_playbill.test_authoring_insertions_v2 import _target
from tests.test_playbill.test_authoring_intents import TIMESTAMP, _coordinator, _payload
from tests.test_playbill.test_authoring_reference_expectations import _expectation


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
    # A valid retained publication expectation exercises the legacy wrappers too.
    draft = InsertionExpectationV2.model_construct(
        expectation_id="sha256:" + "1" * 64,
        state="awaiting_claim_acceptance",
        claim_identity=intent.semantic_identity,
        original_claim_artifact_digest="sha256:" + "2" * 64,
        claim_statement_digest="sha256:" + "3" * 64,
        target=_target(),
        expires_at=datetime(2026, 9, 1, tzinfo=UTC),
        expectation_digest="sha256:" + "0" * 64,
    )
    expectation = InsertionExpectationV2.model_validate(
        draft.model_dump(mode="json")
        | {"expectation_digest": insertion_expectation_v2_digest(draft)}
    )
    wrappers = (
        AuthoringIntentViewV1(intent=intent),
        AuthoringIntentListV1(intents=(intent,)),
        AuthoringSubmitResultV1(intent=intent, status=intent.candidate_status),
        InsertionPrepareResultV2(intent=intent, expectation=expectation, outcome="expired"),
        InsertionConfirmResultV2(intent=intent, expectation=expectation, outcome="expired"),
        InsertionAbandonResultV1(intent=intent, expectation=expectation),
    )
    for response in wrappers:
        wire = response.model_dump(mode="json")
        nested = (
            wire["intents"][0] if isinstance(response, AuthoringIntentListV1) else wire["intent"]
        )
        assert nested == intent.model_dump(mode="json")
        restored = type(response).model_validate_json(response.model_dump_json())
        restored_intent = (
            restored.intents[0] if isinstance(restored, AuthoringIntentListV1) else restored.intent
        )
        assert type(restored_intent) is (AuthoringIntentV2 if version == 2 else AuthoringIntentV1)
        assert restored_intent == intent
        if version == 2:
            assert nested["reference_expectations"] == [
                item.model_dump(mode="json") for item in references
            ]
            del nested["reference_expectations"]
            with pytest.raises(ValidationError, match="reference_expectations"):
                type(response).model_validate(wire)
        else:
            assert "reference_expectations" not in nested
