"""Ephemeral binding reuse preserves frozen commitments and validation boundaries."""

from __future__ import annotations

import copy
from collections import Counter

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring import models
from cruxible_client.contracts.canonical import (
    CanonicalEncodingError,
    Sha256Value,
    canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.claims import LiteralClaimObject
from tests.test_playbill.test_authoring_change_set_intents import _change_set, _claim, _shell
from tests.test_playbill.test_authoring_source_presence import (
    EVENT_MODELS,
    _commit_raw_event,
    _wire_event,
)


@pytest.mark.parametrize("version", (1, 2, 3))
@pytest.mark.parametrize("presence", ("missing", "null", "content"))
def test_binding_preserves_prior_helpers_and_exact_historical_wire(
    version: int, presence: str
) -> None:
    raw = _wire_event(presence, version)
    event = EVENT_MODELS[version].model_validate(raw)
    intent = event.intent
    assert intent.payload_digest == models.authoring_payload_digest(intent.payload)
    assert intent.create_fingerprint == models.authoring_create_fingerprint(
        instance_id=intent.instance_id, actor_id=intent.actor_id, payload=intent.payload
    )
    assert canonical_bytes(event.model_dump(mode="json")) == canonical_bytes(raw)
    assert intent._binding() is intent
    assert canonical_bytes(event.model_dump(mode="json")) == canonical_bytes(raw)


def _mixed_event():
    payload = _change_set(
        _claim(subject_id="wi-42"),
        _claim(subject_id="wi-43"),
        models.SubjectAuthoringPayloadV1(subject=_shell("wi-42")),
    )
    raw = _wire_event("missing", 2)
    intent = raw["intent"]
    intent["payload"] = payload.model_dump(mode="json")
    membership = models.authoring_change_set_membership(payload.members)
    intent["semantic_identity"] = (
        "ChangeSet:"
        + typed_digest(
            Sha256Value,
            models.AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
            {"members": [{"kind": kind, "identity": identity} for kind, identity in membership]},
        ).value
    )
    intent["change_set_claim_identities"] = [
        models.ChangeSetClaimIdentityV1(
            member_identity=identity, claim_id=f"CLM-{index:032x}"
        ).model_dump(mode="json")
        for index, (kind, identity) in enumerate(membership, start=1)
        if kind == "Claim"
    ]
    _commit_raw_event(raw)
    return raw


def test_one_binding_dumps_and_normalizes_payload_once_and_reuses_member_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _mixed_event()
    calls: Counter[str] = Counter()
    original_dump = models.ChangeSetAuthoringPayloadV1.model_dump
    original_identity = models.authoring_member_identity
    original_normalize = models.normalize_canonical

    def dump(self, *args, **kwargs):
        calls["payload_dump"] += 1
        return original_dump(self, *args, **kwargs)

    def identity(member):
        calls["member_identity"] += 1
        return original_identity(member)

    def normalize(value, **kwargs):
        if isinstance(value, dict) and "members" in value:
            calls["payload_normalize"] += 1
        return original_normalize(value, **kwargs)

    monkeypatch.setattr(models.ChangeSetAuthoringPayloadV1, "model_dump", dump)
    monkeypatch.setattr(models, "authoring_member_identity", identity)
    monkeypatch.setattr(models, "normalize_canonical", normalize)
    intent = models.AuthoringIntentV2.model_validate(raw["intent"])
    assert intent.model_dump(mode="json") == raw["intent"]
    # Member uniqueness/sorting validation still runs, followed by membership
    # validation. The minted-Claim binding reuses that second identity table.
    assert calls == Counter(payload_dump=1, payload_normalize=1, member_identity=6)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("payload_digest", "sha256:" + "0" * 64, "payload digest does not reproduce"),
        ("create_fingerprint", "sha256:" + "0" * 64, "create fingerprint does not reproduce"),
        ("instance_id", "changed-instance", "create fingerprint does not reproduce"),
        ("actor_id", "changed-actor", "create fingerprint does not reproduce"),
        ("semantic_identity", "ChangeSet:changed", "identity differs from its payload"),
        ("change_set_claim_identities", [], "identities must name every Claim member once"),
    ],
)
def test_mutated_intent_bindings_still_refuse(
    field: str, replacement: object, message: str
) -> None:
    raw = _mixed_event()["intent"]
    raw[field] = replacement
    with pytest.raises(ValidationError, match=message):
        models.AuthoringIntentV2.model_validate(raw)


def test_mutated_nested_payload_is_not_remembered_by_binding_or_public_helpers() -> None:
    # Construct this deliberately mutated model without minting new commitments.
    intent = models.AuthoringIntentV2.model_validate(_mixed_event()["intent"])
    member = intent.payload.members[0]
    changed_member = member.model_copy(
        update={
            "statement": member.statement.model_copy(
                update={"object": LiteralClaimObject(value={"status": ["ready"]})}
            )
        }
    )
    payload = intent.payload.model_copy(
        update={"members": (changed_member, *intent.payload.members[1:])}
    )
    changed = intent.model_copy(update={"payload": payload})
    before_digest = models.authoring_payload_digest(payload)
    before_fingerprint = models.authoring_create_fingerprint(
        instance_id=intent.instance_id, actor_id=intent.actor_id, payload=payload
    )
    payload.members[0].statement.object.value["status"].append("changed")
    assert models.authoring_payload_digest(payload) != before_digest
    assert (
        models.authoring_create_fingerprint(
            instance_id=intent.instance_id, actor_id=intent.actor_id, payload=payload
        )
        != before_fingerprint
    )
    with pytest.raises(ValueError, match="payload digest does not reproduce"):
        changed._binding()


@pytest.mark.parametrize("field", ("instance_id", "actor_id"))
def test_malformed_model_values_keep_fingerprint_locations_and_payload_first_order(
    field: str,
) -> None:
    intent = models.AuthoringIntentV2.model_validate(_mixed_event()["intent"])
    malformed = intent.model_copy(update={field: 1.5})
    with pytest.raises(CanonicalEncodingError) as previous:
        models.authoring_create_fingerprint(
            instance_id=malformed.instance_id,
            actor_id=malformed.actor_id,
            payload=malformed.payload,
        )
    with pytest.raises(CanonicalEncodingError) as current:
        malformed._binding()
    assert str(current.value) == str(previous.value)
    both = malformed.model_copy(update={"payload_digest": "sha256:" + "0" * 64})
    with pytest.raises(ValueError, match="payload digest does not reproduce"):
        both._binding()


def test_non_ascii_payload_and_actor_commitments_match_public_helpers() -> None:
    raw = copy.deepcopy(_wire_event("missing", 2))
    raw["intent"]["actor_id"] = "auteur-e\u0301"
    raw["intent"]["instance_id"] = "world-\U0001f30d"
    raw["intent"]["payload"]["members"][0]["rationale"] = (
        'Observe\u0301: \u03b1, \U0001f30d, "quoted"\n'
    )
    _commit_raw_event(raw)
    intent = models.AuthoringIntentV2.model_validate(raw["intent"])
    assert intent.payload_digest == models.authoring_payload_digest(intent.payload)
    assert intent.create_fingerprint == models.authoring_create_fingerprint(
        instance_id=intent.instance_id, actor_id=intent.actor_id, payload=intent.payload
    )


def _stamp_event_digest(raw: dict[str, object]) -> None:
    """Restamp only the envelope, leaving the intent's own commitments alone."""

    preimage = dict(raw)
    preimage.pop("tag")
    preimage.pop("event_digest")
    raw["event_digest"] = typed_digest(Sha256Value, str(raw["tag"]), preimage).tagged


def _change_set_identity(payload: models.ChangeSetAuthoringPayloadV1) -> str:
    membership = models.authoring_change_set_membership(payload.members)
    return (
        "ChangeSet:"
        + typed_digest(
            Sha256Value,
            models.AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
            {"members": [{"kind": kind, "identity": identity} for kind, identity in membership]},
        ).value
    )


def _wire_intent(payload, *, semantic_identity: str, claim_identities: tuple[str, ...] = ()):
    """One event whose intent digests come from the PUBLIC standalone helpers.

    `_commit_raw_event` mints the payload digest from a raw preimage that pops
    only `tag`, so it cannot express the change-set rationale law. These digests
    are the ones a caller of the shipped helpers gets, which is exactly what
    `_binding` has to reproduce.
    """

    raw = copy.deepcopy(_wire_event("missing", 2))
    intent = raw["intent"]
    intent["payload"] = payload.model_dump(mode="json")
    intent["semantic_identity"] = semantic_identity
    intent["change_set_claim_identities"] = [
        models.ChangeSetClaimIdentityV1(
            member_identity=identity, claim_id=f"CLM-{index:032x}"
        ).model_dump(mode="json")
        for index, identity in enumerate(claim_identities, start=1)
    ]
    intent["payload_digest"] = models.authoring_payload_digest(payload)
    intent["create_fingerprint"] = models.authoring_create_fingerprint(
        instance_id=intent["instance_id"], actor_id=intent["actor_id"], payload=payload
    )
    _stamp_event_digest(raw)
    return raw


def _rationale_set(rationale: str | None) -> models.ChangeSetAuthoringPayloadV1:
    members = _change_set(
        _claim(subject_id="wi-42"),
        models.SubjectAuthoringPayloadV1(subject=_shell("wi-42")),
    ).members
    if rationale is None:
        return models.ChangeSetAuthoringPayloadV1(members=members)
    return models.ChangeSetAuthoringPayloadV1(members=members, rationale=rationale)


def _claim_members(payload: models.ChangeSetAuthoringPayloadV1) -> tuple[str, ...]:
    return tuple(
        identity
        for kind, identity in models.authoring_change_set_membership(payload.members)
        if kind == "Claim"
    )


@pytest.mark.parametrize("rationale", (None, "Why this set exists, in the author's own words."))
def test_binding_reproduces_the_standalone_digests_for_a_change_set(
    rationale: str | None,
) -> None:
    payload = _rationale_set(rationale)
    raw = _wire_intent(
        payload,
        semantic_identity=_change_set_identity(payload),
        claim_identities=_claim_members(payload),
    )

    # Validation IS the assertion: `_binding` recomputes both commitments.
    event = EVENT_MODELS[2].model_validate(raw)

    intent = event.intent
    assert intent.payload.rationale == rationale
    assert intent.payload_digest == models.authoring_payload_digest(payload)
    assert intent.create_fingerprint == models.authoring_create_fingerprint(
        instance_id=intent.instance_id, actor_id=intent.actor_id, payload=payload
    )
    assert intent._binding() is intent


def test_a_change_sets_prose_moves_its_fingerprint_and_never_its_payload_digest() -> None:
    unset = _rationale_set(None)
    first = _rationale_set("The first prose.")
    second = _rationale_set("The corrected prose.")
    fingerprints = {
        models.authoring_create_fingerprint(
            instance_id="binding-fixture", actor_id="fixture-author", payload=payload
        )
        for payload in (unset, first, second)
    }

    assert (
        models.authoring_payload_digest(unset)
        == models.authoring_payload_digest(first)
        == models.authoring_payload_digest(second)
    )
    assert len(fingerprints) == 3


def test_a_change_set_rationale_edited_after_the_fact_fails_the_fingerprint() -> None:
    payload = _rationale_set("The first prose.")
    raw = _wire_intent(
        payload,
        semantic_identity=_change_set_identity(payload),
        claim_identities=_claim_members(payload),
    )
    raw["intent"]["payload"]["rationale"] = "Edited after the fact."
    _stamp_event_digest(raw)

    with pytest.raises(ValidationError) as refusal:
        EVENT_MODELS[2].model_validate(raw)

    assert "create fingerprint does not reproduce" in str(refusal.value)
    assert "payload digest does not reproduce" not in str(refusal.value)


def test_binding_keeps_a_singular_claims_rationale_in_both_commitments() -> None:
    payload = _claim(subject_id="wi-42", rationale="The writer observed the current status.")
    other = _claim(subject_id="wi-42", rationale="A different reason entirely.")
    raw = _wire_intent(payload, semantic_identity="CLM-" + "2" * 32)

    event = EVENT_MODELS[2].model_validate(raw)

    intent = event.intent
    assert isinstance(intent.payload, models.ClaimAuthoringPayloadV1)
    assert intent.payload_digest == models.authoring_payload_digest(payload)
    assert intent.create_fingerprint == models.authoring_create_fingerprint(
        instance_id=intent.instance_id, actor_id=intent.actor_id, payload=payload
    )
    # A Claim's rationale is part of what its author asserted, so it moves the
    # payload digest -- the half of the law a change set inverts.
    assert models.authoring_payload_digest(payload) != models.authoring_payload_digest(other)
