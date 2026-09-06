"""Compact lookup proofs preserve exact historical-byte and retry semantics."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from cruxible_client.contracts.authoring.models import (
    authoring_create_fingerprint,
    authoring_payload_digest,
    build_insertion_expectation_v2,
    insertion_expectation_id,
)
from cruxible_core.playbill.authoring import store as store_module
from cruxible_core.playbill.authoring.store import (
    AuthoringIntentStore,
    AuthoringIntentStoreError,
    build_authoring_intent_event,
)
from tests.test_playbill.test_authoring_history_reuse import (
    _event_path,
    _history,
    _intent,
    _operation,
    _write_change_set_history,
)
from tests.test_playbill.test_authoring_insertions_v2 import _target


@pytest.fixture(autouse=True)
def clear_memos():
    store_module._reset_authoring_history_memo()
    yield
    store_module._reset_authoring_history_memo()


def _find(store, intent):
    return store._active_by_fingerprint(intent.create_fingerprint, actor_id=intent.actor_id)


def test_lookup_survives_full_history_capacity_and_checks_all_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_HISTORY_MEMO_MAX_STREAMS", 2)
    histories = [_history(tmp_path / "exhaust", token=str(i), value=str(i)) for i in range(1, 7)]
    store, events = histories[-1]
    expected = events[-1].intent
    assert _find(store, expected) == expected
    assert len(store_module._HISTORY_MEMO) == 2
    assert len(store_module._FINGERPRINT_MEMO) == 6
    reads = []
    original = Path.read_bytes

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with patch.object(
        store_module, "_decode_authoring_intent_event", side_effect=AssertionError("reparsed")
    ):
        assert _find(AuthoringIntentStore(tmp_path / "exhaust"), expected) == expected
        assert store._active_by_fingerprint(_operation(999), actor_id="owner") is None
    assert all(_event_path(store, event) in reads for _s, history in histories for event in history)
    # Returned nested payloads remain detached from both proof and parsed state.
    result = _find(store, expected)
    result.payload.statement.object.value["items"].clear()
    assert _find(store, expected) == expected


def test_corruption_in_another_actors_old_event_is_not_hidden(tmp_path):
    store, unrelated = _history(tmp_path / "exhaust", token="1", actor="other")
    _store, target = _history(tmp_path / "exhaust", token="2")
    assert _find(store, target[-1].intent) == target[-1].intent
    path = _event_path(store, unrelated[0])
    raw = path.read_bytes()
    stat = path.stat()
    altered = raw.replace(b'"value":"ready"', b'"value":"wrong"')
    assert altered != raw and len(altered) == len(raw)
    path.write_bytes(altered)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(AuthoringIntentStoreError, match="event is malformed"):
        _find(store, target[-1].intent)
    path.write_bytes(raw)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert _find(store, target[-1].intent) == target[-1].intent


def test_appends_and_valid_truncation_reproduce_current_fingerprint_and_prose(tmp_path):
    store, events = _write_change_set_history(tmp_path / "exhaust", ("first", "second"))
    assert events[0].intent.payload_digest == events[1].intent.payload_digest
    assert _find(store, events[-1].intent).payload.rationale == "second"
    assert _find(store, events[0].intent) is None
    # Restoring a valid shorter prefix must not resurrect its cached successor.
    path = _event_path(store, events[-1])
    raw = path.read_bytes()
    path.unlink()
    assert _find(store, events[0].intent).payload.rationale == "first"
    assert _find(store, events[-1].intent) is None
    path.write_bytes(raw)
    assert _find(store, events[-1].intent).payload.rationale == "second"


def test_duplicate_active_fingerprints_still_refuse_after_warming(tmp_path):
    store, events = _history(tmp_path / "exhaust", token="1")
    assert _find(store, events[-1].intent) == events[-1].intent
    _history(tmp_path / "exhaust", token="2")
    for _ in range(2):
        with pytest.raises(AuthoringIntentStoreError, match="fingerprint is not unique"):
            _find(store, events[-1].intent)


@pytest.mark.parametrize("pending_insertion", (False, True))
def test_accepted_intent_remains_pending_only_for_live_insertion(tmp_path, pending_insertion):
    exhaust = tmp_path / "exhaust"
    exhaust.mkdir()
    store = AuthoringIntentStore(exhaust)
    intent = _intent()
    intent = intent.model_copy(
        update={
            "candidate_status": intent.candidate_status.model_copy(
                update={"state": "accepted", "accepted_generation": intent.base_coordinate}
            )
        }
    )
    if pending_insertion:
        payload = intent.payload.model_copy(update={"insertion_target": _target()})
        expectation = build_insertion_expectation_v2(
            expectation_id=insertion_expectation_id(
                instance_id=intent.instance_id, intent_id=intent.intent_id, intent_revision=0
            ),
            state="awaiting_claim_acceptance",
            claim_identity=intent.semantic_identity,
            original_claim_artifact_digest=_operation(20),
            claim_statement_digest=_operation(21),
            target=payload.insertion_target,
            expires_at=datetime(2026, 8, 25, tzinfo=UTC),
        )
        intent = intent.model_copy(
            update={
                "payload": payload,
                "payload_digest": authoring_payload_digest(payload),
                "create_fingerprint": authoring_create_fingerprint(
                    instance_id=intent.instance_id, actor_id=intent.actor_id, payload=payload
                ),
                "insertion_expectation": expectation,
                "insertion_expectations": (expectation,),
            }
        )
    store.create(intent, operation_key=_operation(0))
    for _ in range(2):
        assert _find(store, intent) == (intent if pending_insertion else None)


def test_first_refusal_matches_cold_when_old_bytes_and_later_path_both_change(tmp_path):
    store, events = _history(tmp_path / "exhaust")
    assert _find(store, events[-1].intent) == events[-1].intent
    _event_path(store, events[0]).write_bytes(b"not JSON")
    final = _event_path(store, events[-1])
    final.rename(final.with_name("99999999999999999999.json"))
    for _ in range(2):
        with pytest.raises(AuthoringIntentStoreError, match="event is malformed"):
            _find(store, events[-1].intent)
        store_module._reset_authoring_history_memo()


def test_proof_eviction_is_bounded_and_only_costs_revalidation(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_FINGERPRINT_MEMO_MAX_STREAMS", 2)
    monkeypatch.setattr(store_module, "_HISTORY_MEMO_MAX_STREAMS", 1)
    histories = [_history(tmp_path / "exhaust", token=str(i), value=str(i)) for i in range(1, 4)]
    store, events = histories[-1]
    for _ in range(2):
        assert _find(store, events[-1].intent) == events[-1].intent
        assert len(store_module._FINGERPRINT_MEMO) <= 2
    store_module._reset_authoring_history_memo()
    assert _find(store, events[-1].intent) == events[-1].intent


def test_same_length_valid_replacement_changes_proof_without_stat_shortcuts(tmp_path):
    store, events = _history(tmp_path / "exhaust", count=1, value="first")
    assert _find(store, events[-1].intent) == events[-1].intent
    changed = _intent(value="other")
    event = build_authoring_intent_event(
        sequence=0,
        previous_event_digest=None,
        operation_key=_operation(0),
        intent=changed,
    )
    path = _event_path(store, event)
    stat = path.stat()
    raw = store._render_event(event)
    assert len(raw) == stat.st_size
    path.write_bytes(raw)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert _find(store, events[0].intent) is None
    assert _find(store, changed) == changed
