"""Publication readers consume narrow current state, not whole intents or events."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_core.playbill.authoring.registrations import (
    bound_publication_registrations,
    reset_bound_publication_registration_memo,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.service.playbill_publications import (
    _released_publication_expectation,
    service_depublish_playbill_block,
)
from tests.test_playbill.test_authoring_insertions_v2 import (
    _registered_publication,
    _submitted_publication,
)


def test_publication_fold_and_release_use_current_intents_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    bound, _landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    preparation = bound.preparation
    assert preparation is not None
    assert len(coordinator.store.events()) > 1
    reset_bound_publication_registration_memo()
    current_reads = 0
    original = AuthoringIntentStore.publication_states

    def latest(store: AuthoringIntentStore) -> Any:
        nonlocal current_reads
        assert store._read_only
        current_reads += 1
        return original(store)

    def historical(_store: AuthoringIntentStore) -> Any:
        pytest.fail("publication state must not request whole intents or historical events")

    monkeypatch.setattr(AuthoringIntentStore, "publication_states", latest)
    monkeypatch.setattr(AuthoringIntentStore, "latest_intents", historical)
    monkeypatch.setattr(AuthoringIntentStore, "events", historical)
    lock_path = coordinator.store.root / ".lock"
    lock_path.unlink(missing_ok=True)

    registrations = bound_publication_registrations(instance)
    assert registrations is not None
    assert len(registrations) == 1
    registration = registrations[0]
    assert registration.intent_id == intent_id
    assert registration.claim_identity == bound.claim_identity
    assert registration.claim_statement_digest == bound.claim_statement_digest
    assert registration.preparation == preparation
    assert bound_publication_registrations(instance) == registrations
    assert current_reads == 1
    assert not lock_path.exists()
    assert (
        _released_publication_expectation(instance, preparation.source_id, preparation.block_id)
        is None
    )

    released = service_depublish_playbill_block(
        instance,
        coordinator=coordinator,
        actor=actor,
        source_id=preparation.source_id,
        block_id=preparation.block_id,
    )
    assert released.outcome == "depublished"
    # The append invalidates the registration memo; an earlier bound snapshot
    # must not keep the released block registered.
    assert bound_publication_registrations(instance) == ()
    assert _released_publication_expectation(
        instance, preparation.source_id, preparation.block_id
    ) == (intent_id, bound.expectation_id, bound.claim_identity)

    repeated = service_depublish_playbill_block(
        instance,
        coordinator=coordinator,
        actor=actor,
        source_id=preparation.source_id,
        block_id=preparation.block_id,
    )
    assert repeated.outcome == "already_depublished"
    assert repeated.intent_id == intent_id
    assert repeated.expectation_id == bound.expectation_id


def test_current_publication_reads_refuse_corrupt_historical_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner, coordinator, actor, intent_id, preimage, _clock = _submitted_publication(
        tmp_path
    )
    bound, _landed = _registered_publication(instance, coordinator, actor, intent_id, preimage)
    preparation = bound.preparation
    assert preparation is not None
    reset_bound_publication_registration_memo()
    assert bound_publication_registrations(instance)

    def historical(_store: AuthoringIntentStore) -> Any:
        pytest.fail("publication state must use the validated narrow publication-state API")

    monkeypatch.setattr(AuthoringIntentStore, "events", historical)
    monkeypatch.setattr(AuthoringIntentStore, "latest_intents", historical)
    events = sorted((coordinator.store.root / intent_id / "events").glob("*.json"))
    assert len(events) > 1
    events[0].write_bytes(b"{}\n")

    # A valid latest snapshot cannot hide a corrupted predecessor. Both
    # consumers retain their unavailable result rather than claiming emptiness.
    assert bound_publication_registrations(instance) is None
    assert (
        _released_publication_expectation(instance, preparation.source_id, preparation.block_id)
        is None
    )
