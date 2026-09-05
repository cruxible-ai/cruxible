"""One event snapshot preserves the independent frozen digest and byte checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import (
    DiagnosticFrontierV1,
    PreflightResultV1,
    build_preflight_certificate,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.proposal_models import AuthenticatedActor, ProposalReceiveLimits
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.playbill.authoring import store as store_module
from tests.test_playbill.test_authoring_source_presence import (
    EVENT_MODELS,
    FIELD,
    Presence,
    _commit_raw_event,
    _wire_event,
)


@pytest.mark.parametrize("version", (1, 2, 3))
@pytest.mark.parametrize("presence", ("missing", "null", "content"))
def test_decoded_snapshot_matches_independent_wire_and_public_helpers(
    version: int, presence: Presence
) -> None:
    raw = _wire_event(presence, version)
    wire = canonical_bytes(raw) + b"\n"
    old = store_module._parse_authoring_intent_event(wire)
    event, rendered = store_module._decode_authoring_intent_event(wire)

    assert event == old
    assert rendered == wire == store_module.AuthoringIntentStore._render_event(old)
    assert event.event_digest == store_module.authoring_intent_event_digest(event)
    assert event.model_dump(mode="json") == raw
    source = event.intent.payload.members[0].source
    assert (FIELD in source.model_fields_set) == (presence != "missing")
    assert not any(
        isinstance(value, store_module._EventDecodeContext) for value in vars(event).values()
    )


def test_decoder_normalizes_one_snapshot_without_calling_public_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = canonical_bytes(_wire_event("missing")) + b"\n"
    original = store_module.normalize_canonical
    calls: list[object] = []

    def normalize(value: object):
        calls.append(value)
        return original(value)

    def unexpected_digest(event: object) -> str:
        pytest.fail("internal decoder repeated the public event digest pass")

    monkeypatch.setattr(store_module, "normalize_canonical", normalize)
    monkeypatch.setattr(store_module, "authoring_intent_event_digest", unexpected_digest)
    _, rendered = store_module._decode_authoring_intent_event(wire)
    assert rendered == wire
    assert len(calls) == 1


@pytest.mark.parametrize("version", (1, 2, 3))
def test_ordinary_validation_context_keeps_public_digest_path(
    version: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _wire_event("null", version)
    original = store_module.authoring_intent_event_digest
    calls: list[object] = []

    def digest(event):
        calls.append(event)
        return original(event)

    context = {"rendered": b"untrusted"}
    monkeypatch.setattr(store_module, "authoring_intent_event_digest", digest)
    EVENT_MODELS[version].model_validate(raw, context=context)
    assert len(calls) == 1
    assert context == {"rendered": b"untrusted"}


@pytest.mark.parametrize("version", (1, 2, 3))
@pytest.mark.parametrize("corruption", ("event_digest", "payload_digest", "unknown", "source"))
def test_failed_validation_never_publishes_snapshot_and_matches_default_errors(
    version: int, corruption: str
) -> None:
    raw = _wire_event("content", version)
    if corruption == "event_digest":
        raw["event_digest"] = "sha256:" + "0" * 64
    elif corruption == "payload_digest":
        raw["intent"]["payload_digest"] = "sha256:" + "0" * 64
    elif corruption == "unknown":
        raw["context"] = {"rendered": "forged"}
    else:
        raw["intent"]["payload"]["members"][0]["source"][FIELD] = "not base64!"
    context = store_module._EventDecodeContext()
    with pytest.raises(ValidationError) as old:
        EVENT_MODELS[version].model_validate(raw)
    with pytest.raises(ValidationError) as new:
        EVENT_MODELS[version].model_validate(raw, context=context)
    assert new.value.errors(include_context=False) == old.value.errors(include_context=False)
    assert context.rendered is None


@pytest.mark.parametrize("representation", ("pretty", "no_newline", "duplicate_key"))
def test_store_still_refuses_noncanonical_bytes_after_successful_decode(
    tmp_path: Path, representation: str
) -> None:
    raw = _wire_event("missing")
    canonical = canonical_bytes(raw) + b"\n"
    if representation == "pretty":
        wire = json.dumps(raw, indent=2).encode() + b"\n"
    elif representation == "no_newline":
        wire = canonical[:-1]
    else:
        wire = b'{"sequence":0,' + canonical[1:]
    _, rendered = store_module._decode_authoring_intent_event(wire)
    assert rendered == canonical != wire
    exhaust = tmp_path / "exhaust"
    directory = exhaust / "authoring-intents" / raw["intent"]["intent_id"] / "events"
    directory.mkdir(parents=True)
    (directory / "00000000000000000000.json").write_bytes(wire)
    store = store_module.AuthoringIntentStore(exhaust, read_only=True)
    with pytest.raises(store_module.AuthoringIntentStoreError, match="event is not canonical"):
        store.events()


@pytest.mark.parametrize("version", (1, 2, 3))
def test_nested_preflight_uses_frozen_receive_subset_and_verifies_its_digest(version: int) -> None:
    raw = _wire_event("missing", version)
    event = store_module._parse_authoring_intent_event(canonical_bytes(raw))
    frontier = DiagnosticFrontierV1()
    limits = ProposalReceiveLimits()
    certificate = build_preflight_certificate(
        instance_id=event.intent.instance_id,
        intent_id=event.intent.intent_id,
        intent_revision=event.intent.intent_revision,
        actor=AuthenticatedActor(actor_id=event.intent.actor_id),
        payload_digest=event.intent.payload_digest,
        resolved_authoring_digest="sha256:" + "6" * 64,
        accepted_coordinate=event.intent.base_coordinate,
        compiler_coordinate=CompilerCoordinate(rule_digest="sha256:" + "7" * 64),
        instance_descriptor_digest="sha256:" + "8" * 64,
        receive_limits=limits,
        canonical_timestamp=event.intent.canonical_timestamp,
        proposal_ref="refs/proposals/fixture/preflight",
        proposal_ref_oid=None,
        candidate_tree_digest="sha256:" + "9" * 64,
        frontier_digest=frontier.digest,
    )
    raw["intent"]["last_preflight"] = PreflightResultV1(
        verdict="passed", certificate=certificate, frontier=frontier
    ).model_dump(mode="json")
    _commit_raw_event(raw)
    wire = canonical_bytes(raw) + b"\n"
    decoded, rendered = store_module._decode_authoring_intent_event(wire)
    assert rendered == wire == store_module.AuthoringIntentStore._render_event(decoded)
    received = json.loads(rendered)["intent"]["last_preflight"]["certificate"]["receive_limits"]
    assert received == limits.receive_bound_payload()
    assert received != limits.model_dump(mode="json")

    raw["intent"]["last_preflight"]["certificate"]["certificate_digest"] = "sha256:" + "0" * 64
    _commit_raw_event(raw)
    with pytest.raises(ValidationError, match="preflight certificate digest does not reproduce"):
        store_module._decode_authoring_intent_event(canonical_bytes(raw))
