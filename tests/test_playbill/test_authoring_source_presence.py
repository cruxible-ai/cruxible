"""Historical source-field presence remains part of the committed wire bytes.

Fixtures are synthetic: explicit-null public models supply the ordinary fields,
then raw dictionaries reproduce the three existing source representations. Their
commitments are computed before parsing, independently of the model serializer.
No daemon history or source material is copied into these tests.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import (
    AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
    AUTHORING_CREATE_FINGERPRINT_DOMAIN,
    AUTHORING_PAYLOAD_DIGEST_DOMAIN,
    AuthoringIntentV1,
    AuthoringIntentV2,
    AuthoringProgramStampV1,
    CandidateStatusV1,
    ChangeSetAuthoringPayloadV1,
    ChangeSetClaimIdentityV1,
    WorkingSelectionObservationV1,
    authoring_change_set_membership,
    authoring_create_fingerprint,
    authoring_member_identity,
    authoring_payload_digest,
)
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.authoring.store import (
    AuthoringIntentEventV1,
    AuthoringIntentEventV2,
    AuthoringIntentEventV3,
    AuthoringIntentStore,
    AuthoringIntentStoreError,
    build_authoring_intent_event,
)
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _working_payload
from tests.test_playbill.test_procedure_execution import _coordinate

Presence = Literal["missing", "null", "content"]
FIELD = "source_content_base64"
CONTENT = base64.b64encode(b"status: ready").decode("ascii")
EVENT_MODELS = {1: AuthoringIntentEventV1, 2: AuthoringIntentEventV2, 3: AuthoringIntentEventV3}


def _commit_raw_event(raw: dict[str, Any]) -> None:
    """Commit the supplied wire shape, never a reserialized Pydantic object."""
    intent = raw["intent"]
    payload = intent["payload"]
    preimage = dict(payload)
    preimage.pop("tag")
    intent["payload_digest"] = typed_digest(
        Sha256Value, AUTHORING_PAYLOAD_DIGEST_DOMAIN, preimage
    ).tagged
    intent["create_fingerprint"] = typed_digest(
        Sha256Value,
        AUTHORING_CREATE_FINGERPRINT_DOMAIN,
        {"instance_id": intent["instance_id"], "actor_id": intent["actor_id"], "payload": payload},
    ).tagged
    event_preimage = dict(raw)
    event_preimage.pop("tag")
    event_preimage.pop("event_digest")
    raw["event_digest"] = typed_digest(Sha256Value, raw["tag"], event_preimage).tagged


def _wire_event(presence: Presence, version: int = 2) -> dict[str, Any]:
    claim = _working_payload(occurrence_count=1)
    assert isinstance(claim.source, WorkingSelectionObservationV1)
    # Explicit null is a valid stored representation both before and after the fix.
    claim = claim.model_copy(update={"source": claim.source.model_copy(update={FIELD: None})})
    payload = ChangeSetAuthoringPayloadV1(members=(claim,))
    membership = authoring_change_set_membership(payload.members)
    semantic_identity = "ChangeSet:" + typed_digest(
        Sha256Value,
        AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
        {"members": [{"kind": kind, "identity": identity} for kind, identity in membership]},
    ).tagged.removeprefix("sha256:")
    fields = {
        "intent_id": "AIT-" + "1" * 32,
        "instance_id": "source-presence-fixture",
        "actor_id": "fixture-author",
        "canonical_timestamp": TIMESTAMP,
        "base_coordinate": _coordinate(),
        "semantic_identity": semantic_identity,
        "payload": payload,
        "payload_digest": authoring_payload_digest(payload),
        "create_fingerprint": authoring_create_fingerprint(
            instance_id="source-presence-fixture", actor_id="fixture-author", payload=payload
        ),
        "candidate_status": CandidateStatusV1(
            state="draft", current_accepted_coordinate=_coordinate()
        ),
        "change_set_claim_identities": (
            ChangeSetClaimIdentityV1(
                member_identity=authoring_member_identity(claim), claim_id="CLM-" + "2" * 32
            ),
        ),
    }
    intent = (
        AuthoringIntentV1.model_validate(fields)
        if version == 1
        else AuthoringIntentV2.model_validate({**fields, "reference_expectations": ()})
    )
    event = build_authoring_intent_event(
        sequence=0,
        previous_event_digest=None,
        operation_key="sha256:" + "3" * 64,
        intent=intent,
        program_stamp=(
            AuthoringProgramStampV1(
                program_digest="sha256:" + "4" * 64,
                sdk_version="0.4.0",
                sdk_contract_snapshot_digest="sha256:" + "5" * 64,
            )
            if version == 3
            else None
        ),
    )
    raw = event.model_dump(mode="json")
    source = raw["intent"]["payload"]["members"][0]["source"]
    if presence == "missing":
        source.pop(FIELD)
    else:
        source[FIELD] = None if presence == "null" else CONTENT
    _commit_raw_event(raw)
    return raw


@pytest.mark.parametrize("version", (1, 2, 3))
@pytest.mark.parametrize("presence", ("missing", "null", "content"))
def test_historical_nested_wire_commitments_round_trip(presence: Presence, version: int) -> None:
    raw = _wire_event(presence, version)
    wire = canonical_bytes(raw) + b"\n"
    event = EVENT_MODELS[version].model_validate_json(wire)

    assert canonical_bytes(event.model_dump(mode="json")) + b"\n" == wire
    assert json.loads(event.model_dump_json()) == raw
    assert canonical_bytes(event.intent.payload.model_dump(mode="json")) == canonical_bytes(
        raw["intent"]["payload"]
    )
    assert authoring_payload_digest(event.intent.payload) == raw["intent"]["payload_digest"]
    assert (
        authoring_create_fingerprint(
            instance_id=event.intent.instance_id,
            actor_id=event.intent.actor_id,
            payload=event.intent.payload,
        )
        == raw["intent"]["create_fingerprint"]
    )
    source = event.intent.payload.members[0].source
    assert (FIELD in source.model_fields_set) == (presence != "missing")
    for mode in ("json", "python"):
        dumped = source.model_dump(mode=mode)
        assert (FIELD in dumped) == (presence != "missing")
        assert FIELD not in source.model_dump(mode=mode, exclude={FIELD})
    assert event.model_copy(deep=True).model_dump(mode="json") == raw


def test_missing_null_and_content_keep_distinct_idempotency_domains() -> None:
    events = [_wire_event(presence) for presence in ("missing", "null", "content")]
    assert len({event["intent"]["payload_digest"] for event in events}) == 3
    assert len({event["intent"]["create_fingerprint"] for event in events}) == 3
    assert len({event["event_digest"] for event in events}) == 3
    sources = [
        WorkingSelectionObservationV1.model_validate(
            event["intent"]["payload"]["members"][0]["source"]
        )
        for event in events
    ]
    assert sources[0].model_copy(update={FIELD: None}).model_dump(mode="json") == sources[
        1
    ].model_dump(mode="json")
    assert sources[0].model_copy(update={FIELD: CONTENT}).model_dump(mode="json") == sources[
        2
    ].model_dump(mode="json")


@pytest.mark.parametrize("version", (1, 2, 3))
def test_historical_reload_allows_new_create_without_rewriting_history(
    tmp_path: Path, version: int
) -> None:
    historical = _wire_event("missing", version)
    exhaust = tmp_path / "exhaust"
    directory = exhaust / "authoring-intents" / historical["intent"]["intent_id"] / "events"
    directory.mkdir(parents=True)
    path = directory / "00000000000000000000.json"
    original = canonical_bytes(historical) + b"\n"
    path.write_bytes(original)
    store = AuthoringIntentStore(exhaust)
    loaded = store.get(historical["intent"]["intent_id"], actor_id="fixture-author")
    assert loaded.payload_digest == historical["intent"]["payload_digest"]

    incoming = _wire_event("null", version)
    incoming["intent"]["intent_id"] = "AIT-" + "6" * 32
    incoming["operation_key"] = "sha256:" + "7" * 64
    _commit_raw_event(incoming)
    intent = EVENT_MODELS[version].model_validate(incoming).intent
    created = store.create(intent, operation_key=incoming["operation_key"])
    assert created.intent_id == intent.intent_id != loaded.intent_id
    assert store.create(intent, operation_key=incoming["operation_key"]) == created
    reopened = AuthoringIntentStore(exhaust, read_only=True)
    assert len(reopened.events()) == 2
    assert path.read_bytes() == original
    assert len(tuple(directory.glob("*.json"))) == 1


@pytest.mark.parametrize("tamper", ("presence", "payload_digest", "event_digest", "unknown_field"))
def test_presence_compatibility_does_not_admit_tampered_history(
    tmp_path: Path, tamper: str
) -> None:
    raw = _wire_event("missing")
    if tamper == "presence":
        # Null is valid source data, but adding it without new commitments is tampering.
        raw["intent"]["payload"]["members"][0]["source"][FIELD] = None
    elif tamper == "payload_digest":
        raw["intent"]["payload_digest"] = "sha256:" + "0" * 64
    elif tamper == "event_digest":
        raw["event_digest"] = "sha256:" + "0" * 64
    else:
        raw["intent"]["payload"]["members"][0]["source"]["future_source_field"] = None
    with pytest.raises(ValidationError):
        AuthoringIntentEventV2.model_validate(raw)

    exhaust = tmp_path / "exhaust"
    directory = exhaust / "authoring-intents" / raw["intent"]["intent_id"] / "events"
    directory.mkdir(parents=True)
    path = directory / "00000000000000000000.json"
    wire = canonical_bytes(raw) + b"\n"
    path.write_bytes(wire)
    with pytest.raises(AuthoringIntentStoreError, match="event is malformed"):
        AuthoringIntentStore(exhaust, read_only=True).events()
    assert path.read_bytes() == wire


def test_valid_payload_digest_cannot_hide_changed_event_content() -> None:
    raw = _wire_event("content")
    changed = copy.deepcopy(raw)
    changed["intent"]["payload"]["members"][0]["rationale"] = "A different synthetic rationale."
    _commit_raw_event(changed)
    changed["event_digest"] = raw["event_digest"]
    with pytest.raises(ValidationError, match="event digest does not reproduce"):
        AuthoringIntentEventV2.model_validate(changed)
