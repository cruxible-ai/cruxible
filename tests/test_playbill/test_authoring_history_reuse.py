"""Parsed history reuse must retain durable-byte checks and caller isolation."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import (
    AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
    AuthoringIntentV1,
    CandidateStatusV1,
    ChangeSetAuthoringPayloadV1,
    SubjectAuthoringPayloadV1,
    authoring_change_set_membership,
    authoring_create_fingerprint,
    authoring_payload_digest,
    build_insertion_expectation_v2,
    insertion_expectation_id,
)
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_core.playbill.authoring import store as store_module
from cruxible_core.playbill.authoring.store import (
    AuthoringIntentEventAny,
    AuthoringIntentStore,
    AuthoringIntentStoreError,
    build_authoring_intent_event,
)
from tests.test_playbill.test_authoring_change_set_intents import _shell
from tests.test_playbill.test_authoring_insertions_v2 import _target
from tests.test_playbill.test_authoring_intents import TIMESTAMP, _payload
from tests.test_playbill.test_authoring_source_presence import _commit_raw_event, _wire_event
from tests.test_playbill.test_procedure_execution import _coordinate


@pytest.fixture(autouse=True)
def _clear_history_memo():
    store_module._reset_authoring_history_memo()
    yield
    store_module._reset_authoring_history_memo()


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    calls: list[bytes] = []
    original = store_module._decode_authoring_intent_event

    def count(raw: bytes) -> tuple[AuthoringIntentEventAny, bytes]:
        calls.append(raw)
        return original(raw)

    monkeypatch.setattr(store_module, "_decode_authoring_intent_event", count)
    return calls


def _operation(index: int) -> str:
    return "sha256:" + hashlib.sha256(f"history-operation-{index}".encode()).hexdigest()


def _intent(*, token: str = "1", actor: str = "owner", value: str = "ready") -> AuthoringIntentV1:
    payload = _payload(value=value)
    payload = payload.model_copy(
        update={
            "statement": payload.statement.model_copy(
                update={"object": LiteralClaimObject(value={"items": [{"value": value}]})}
            )
        }
    )
    return AuthoringIntentV1(
        intent_id="AIT-" + token * 32,
        instance_id="history-reuse-fixture",
        actor_id=actor,
        canonical_timestamp=TIMESTAMP,
        base_coordinate=_coordinate(),
        semantic_identity="CLM-" + "2" * 32,
        payload=payload,
        payload_digest=authoring_payload_digest(payload),
        create_fingerprint=authoring_create_fingerprint(
            instance_id="history-reuse-fixture", actor_id=actor, payload=payload
        ),
        candidate_status=CandidateStatusV1(
            state="draft", current_accepted_coordinate=_coordinate()
        ),
    )


def _event_path(store: AuthoringIntentStore, event: AuthoringIntentEventAny) -> Path:
    return store.root / event.intent.intent_id / "events" / f"{event.sequence:020d}.json"


def _history(
    exhaust: Path, *, count: int = 3, token: str = "1", actor: str = "owner", value: str = "ready"
) -> tuple[AuthoringIntentStore, tuple[AuthoringIntentEventAny, ...]]:
    exhaust.mkdir(parents=True, exist_ok=True)
    store = AuthoringIntentStore(exhaust)
    intent = _intent(token=token, actor=actor, value=value)
    events: list[AuthoringIntentEventAny] = []
    for sequence in range(count):
        event = build_authoring_intent_event(
            sequence=sequence,
            previous_event_digest=events[-1].event_digest if events else None,
            operation_key=_operation(sequence),
            intent=intent.model_copy(update={"intent_revision": sequence}, deep=True),
        )
        path = _event_path(store, event)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(store._render_event(event))
        events.append(event)
    return store, tuple(events)


def _get(store: AuthoringIntentStore, event: AuthoringIntentEventAny) -> AuthoringIntentV1:
    return store.get(event.intent.intent_id, actor_id=event.intent.actor_id)


def test_unchanged_history_is_parsed_once_across_consumers_and_store_instances(
    tmp_path: Path, parsed: list[bytes]
) -> None:
    store, events = _history(tmp_path / "exhaust")
    latest = events[-1]
    assert _get(store, latest) == latest.intent
    assert len(parsed) == 3
    reopened = AuthoringIntentStore(tmp_path / "exhaust")
    readonly = AuthoringIntentStore(tmp_path / "exhaust", read_only=True)
    for reader in (store, reopened):
        assert _get(reader, latest) == latest.intent
        assert reader.latest_intents() == (latest.intent,)
        assert reader.list_pending(actor_id="owner") == (latest.intent,)
        assert reader.events() == events
        assert reader.latest_transition(latest.intent.intent_id, actor_id="owner") == (
            events[-2].intent,
            latest,
        )
        assert (
            reader.operation_result(
                latest.intent.intent_id, actor_id="owner", operation_key=events[0].operation_key
            )
            == events[0].intent
        )
    assert readonly.latest_intents() == (latest.intent,)
    assert readonly.events() == events
    assert store.create(_intent(token="3"), operation_key=_operation(99)) == latest.intent
    assert len(tuple(store.root.glob("AIT-*"))) == 1
    assert len(parsed) == 3


def test_real_transition_only_parses_new_durable_event(tmp_path: Path, parsed: list[bytes]) -> None:
    store, events = _history(tmp_path / "exhaust")
    assert _get(store, events[-1]) == events[-1].intent
    updated = store.transition(
        events[-1].intent.intent_id,
        actor_id="owner",
        operation_key=_operation(3),
        transform=lambda intent: intent.model_copy(update={"intent_revision": 3}),
    )
    assert updated.intent_revision == 3
    assert store.latest_intents() == (updated,)
    assert len(parsed) == 4
    assert _get(AuthoringIntentStore(tmp_path / "exhaust"), events[-1]) == updated
    assert len(store.events()) == 4
    assert len(parsed) == 4


def test_same_size_tampering_with_restored_mtime_is_rejected_and_recovery_is_clean(
    tmp_path: Path, parsed: list[bytes]
) -> None:
    store, events = _history(tmp_path / "exhaust")
    _get(store, events[-1])
    path = _event_path(store, events[0])
    raw = path.read_bytes()
    stat = path.stat()
    altered = raw.replace(b'"value":"ready"', b'"value":"wrong"')
    assert altered != raw and len(altered) == len(raw)
    path.write_bytes(altered)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    with pytest.raises(AuthoringIntentStoreError, match="event is malformed"):
        _get(store, events[-1])
    assert len(parsed) == 4
    path.write_bytes(raw)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert _get(store, events[-1]) == events[-1].intent


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("malformed", "event is malformed"),
        ("noncanonical", "event is not canonical"),
        ("previous", "event chain is broken"),
        ("sequence", "event chain is broken"),
        ("operation", "operation key was replayed"),
        ("identity", "event names another stream"),
        ("missing_middle", "sequence is not contiguous"),
        ("empty", "event stream is empty"),
    ],
)
def test_corrupt_history_has_identical_cold_and_warm_refusals(
    tmp_path: Path, corruption: str, message: str
) -> None:
    store, events = _history(tmp_path / "exhaust")
    _get(store, events[-1])
    event = events[1]
    path = _event_path(store, event)
    if corruption == "missing_middle":
        path.unlink()
    elif corruption == "empty":
        for item in events:
            _event_path(store, item).unlink()
    elif corruption == "malformed":
        path.write_bytes(b'{"tag":')
    elif corruption == "noncanonical":
        path.write_text(json.dumps(event.model_dump(mode="json"), indent=2))
    else:
        rewritten = build_authoring_intent_event(
            sequence=9 if corruption == "sequence" else event.sequence,
            previous_event_digest=(
                _operation(99) if corruption == "previous" else event.previous_event_digest
            ),
            operation_key=events[0].operation_key
            if corruption == "operation"
            else event.operation_key,
            intent=(
                event.intent.model_copy(update={"intent_id": "AIT-" + "4" * 32})
                if corruption == "identity"
                else event.intent
            ),
        )
        path.write_bytes(store._render_event(rewritten))
    for cold in (False, True):
        if cold:
            store_module._reset_authoring_history_memo()
        with pytest.raises(AuthoringIntentStoreError, match=message):
            _get(store, events[-1])


def test_valid_shortened_prefix_retains_cold_reader_semantics(tmp_path: Path) -> None:
    store, events = _history(tmp_path / "exhaust")
    assert _get(store, events[-1]) == events[-1].intent
    _event_path(store, events[-1]).unlink()
    assert store.latest_intents() == (events[-2].intent,)
    assert store.events() == events[:-1]
    store_module._reset_authoring_history_memo()
    assert store.latest_intents() == (events[-2].intent,)
    assert store.events() == events[:-1]


def test_changed_valid_predecessor_invalidates_unchanged_cached_successor(
    tmp_path: Path, parsed: list[bytes]
) -> None:
    store, events = _history(tmp_path / "exhaust")
    _get(store, events[-1])
    first = build_authoring_intent_event(
        sequence=0,
        previous_event_digest=None,
        operation_key=_operation(99),
        intent=events[0].intent,
    )
    _event_path(store, events[0]).write_bytes(store._render_event(first))
    with pytest.raises(AuthoringIntentStoreError, match="event chain is broken"):
        _get(store, events[-1])
    # The successor's bytes were reusable, but its link still needs verification.
    assert len(parsed) == 4


@pytest.mark.parametrize("target", ["event", "events_directory"])
def test_warm_cache_does_not_bypass_symlink_refusal(tmp_path: Path, target: str) -> None:
    store, events = _history(tmp_path / "exhaust")
    _get(store, events[-1])
    path = _event_path(store, events[0])
    if target == "events_directory":
        path = path.parent
    moved = tmp_path / "moved"
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=target == "events_directory")
    message = "event directory is invalid" if target == "events_directory" else "not contiguous"
    for cold in (False, True):
        if cold:
            store_module._reset_authoring_history_memo()
        with pytest.raises(AuthoringIntentStoreError, match=message):
            _get(store, events[-1])


def test_warm_retry_discovers_transition_durable_before_response_loss(
    tmp_path: Path, parsed: list[bytes]
) -> None:
    store, events = _history(tmp_path / "exhaust")
    latest = events[-1]
    _get(store, latest)

    def crash(boundary: str) -> None:
        if boundary == "after_transition_event_sync":
            raise RuntimeError("synthetic response loss")

    failing = AuthoringIntentStore(tmp_path / "exhaust", crash_hook=crash)
    with pytest.raises(RuntimeError, match="synthetic response loss"):
        failing.transition(
            latest.intent.intent_id,
            actor_id="owner",
            operation_key=_operation(3),
            transform=lambda intent: intent.model_copy(update={"intent_revision": 3}),
        )

    def unexpected_transform(intent: AuthoringIntentV1) -> AuthoringIntentV1:
        pytest.fail("the durable transition must be discovered before retrying its transform")

    resumed = store.transition(
        latest.intent.intent_id,
        actor_id="owner",
        operation_key=_operation(3),
        transform=unexpected_transform,
    )
    assert resumed.intent_revision == 3
    assert len(store.events()) == 4
    assert len(parsed) == 4


def test_cache_identity_includes_exhaust_root(tmp_path: Path, parsed: list[bytes]) -> None:
    left, left_events = _history(tmp_path / "left", value="ready")
    right, right_events = _history(tmp_path / "right", value="other")
    assert left_events[-1].intent.intent_id == right_events[-1].intent.intent_id
    for _ in range(2):
        assert _get(left, left_events[-1]) == left_events[-1].intent
        assert _get(right, right_events[-1]) == right_events[-1].intent
    assert len(parsed) == 6


def test_warm_actor_filters_still_apply(tmp_path: Path, parsed: list[bytes]) -> None:
    store, owner_events = _history(tmp_path / "exhaust", token="1", actor="owner")
    _, reviewer_events = _history(tmp_path / "exhaust", token="2", actor="reviewer")
    assert store.latest_intents() == (owner_events[-1].intent, reviewer_events[-1].intent)
    assert store.list_pending(actor_id="owner") == (owner_events[-1].intent,)
    assert store.list_pending(actor_id="reviewer") == (reviewer_events[-1].intent,)
    assert store.list_pending(actor_id="stranger") == ()
    with pytest.raises(AuthoringIntentStoreError, match="another actor"):
        store.get(owner_events[-1].intent.intent_id, actor_id="reviewer")
    assert len(parsed) == 6


@pytest.mark.parametrize(
    "route",
    [
        "get",
        "latest",
        "pending",
        "events",
        "load",
        "predecessor",
        "transition_event",
        "operation",
        "dedup",
        "retry",
    ],
)
def test_returned_nested_containers_cannot_mutate_cached_history(
    tmp_path: Path, parsed: list[bytes], route: str
) -> None:
    store, events = _history(tmp_path / "exhaust")
    latest = events[-1]
    _get(store, latest)
    if route == "get":
        returned = _get(store, latest)
    elif route == "latest":
        returned = store.latest_intents()[0]
    elif route == "pending":
        returned = store.list_pending(actor_id="owner")[0]
    elif route == "events":
        returned = store.events()[-1].intent
    elif route == "load":
        returned = store._load_events(store.root / latest.intent.intent_id)[-1].intent
    elif route in {"predecessor", "transition_event"}:
        predecessor, last = store.latest_transition(latest.intent.intent_id, actor_id="owner")
        returned = predecessor if route == "predecessor" else last.intent
    elif route == "operation":
        returned = store.operation_result(
            latest.intent.intent_id, actor_id="owner", operation_key=latest.operation_key
        )
    elif route == "dedup":
        returned = store.create(_intent(token="3"), operation_key=_operation(99))
    else:

        def unexpected_transform(intent: AuthoringIntentV1) -> AuthoringIntentV1:
            pytest.fail("retry must return the recorded operation")

        returned = store.transition(
            latest.intent.intent_id,
            actor_id="owner",
            operation_key=latest.operation_key,
            transform=unexpected_transform,
        )
    assert returned is not None
    returned.payload.statement.object.value["items"][0]["value"] = "wrong"
    returned.payload.statement.object.value["items"].append({"value": "injected"})
    assert _get(store, latest) == latest.intent
    assert store.events() == events
    assert len(parsed) == 3


def test_failed_transform_cannot_mutate_memoized_predecessor(
    tmp_path: Path, parsed: list[bytes]
) -> None:
    store, events = _history(tmp_path / "exhaust")
    latest = events[-1]
    _get(store, latest)

    def fail_after_mutation(intent: AuthoringIntentV1) -> AuthoringIntentV1:
        intent.payload.statement.object.value["items"].clear()
        raise RuntimeError("synthetic transform failure")

    with pytest.raises(RuntimeError, match="synthetic transform failure"):
        store.transition(
            latest.intent.intent_id,
            actor_id="owner",
            operation_key=_operation(99),
            transform=fail_after_mutation,
        )
    assert _get(store, latest) == latest.intent
    assert store.events() == events
    assert len(parsed) == 3


def test_stream_limit_evicts_least_recently_used_history(
    tmp_path: Path, parsed: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "_HISTORY_MEMO_MAX_STREAMS", 2)
    histories = [_history(tmp_path / str(index), count=1) for index in range(3)]
    for index, expected in [(0, 1), (1, 2), (0, 2), (2, 3), (0, 3), (1, 4)]:
        store, events = histories[index]
        assert _get(store, events[-1]) == events[-1].intent
        assert len(parsed) == expected


def test_stream_larger_than_byte_limit_is_not_retained(
    tmp_path: Path, parsed: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, events = _history(tmp_path / "exhaust")
    weight = sum(_event_path(store, event).stat().st_size for event in events)
    monkeypatch.setattr(store_module, "_HISTORY_MEMO_MAX_BYTES", weight - 1)
    for expected in (3, 6):
        assert _get(store, events[-1]) == events[-1].intent
        assert len(parsed) == expected


def test_publication_states_return_detached_nested_expectations(
    tmp_path: Path, parsed: list[bytes]
) -> None:
    exhaust = tmp_path / "exhaust"
    exhaust.mkdir()
    store = AuthoringIntentStore(exhaust)
    intent = _intent()
    payload = intent.payload.model_copy(update={"insertion_target": _target()})
    expectation = build_insertion_expectation_v2(
        expectation_id=insertion_expectation_id(
            instance_id=intent.instance_id,
            intent_id=intent.intent_id,
            intent_revision=intent.intent_revision,
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
    first = store.publication_states()[0]
    assert first.intent_id == intent.intent_id
    assert first.insertion_expectations == (expectation,)
    # Even deliberately bypassing frozen assignment must not expose private models.
    first.insertion_expectations[0].target.__dict__["source_id"] = "caller.changed"
    readonly = AuthoringIntentStore(exhaust, read_only=True)
    assert readonly.publication_states()[0].insertion_expectations == (expectation,)
    assert store.get(intent.intent_id, actor_id="owner") == intent
    assert len(parsed) == 1


def test_private_history_shares_only_validated_equal_payloads_across_revisions(
    tmp_path: Path, parsed: list[bytes]
) -> None:
    store, events = _history(tmp_path / "exhaust")
    directory = store.root / events[0].intent.intent_id
    private = store._validated_events(directory)
    shared = private[0].intent.payload
    assert all(event.intent.payload is shared for event in private)
    assert tuple(store._render_event(event) for event in private) == tuple(
        _event_path(store, event).read_bytes() for event in events
    )

    changed = _intent(value="other").model_copy(update={"intent_revision": 3})
    next_event = build_authoring_intent_event(
        sequence=3,
        previous_event_digest=events[-1].event_digest,
        operation_key=_operation(3),
        intent=changed,
    )
    _event_path(store, next_event).write_bytes(store._render_event(next_event))
    after = store._validated_events(directory)
    assert all(event.intent.payload is shared for event in after[:3])
    assert after[3].intent.payload is not shared
    assert after[3].intent.payload == changed.payload
    assert len(parsed) == 4

    detached = store.events()
    detached[0].intent.payload.statement.object.value["items"].clear()
    assert store.events() == (*events, next_event)
    assert store.latest_intents() == (changed,)
    assert len(parsed) == 4


@pytest.mark.parametrize("version", (1, 2, 3))
def test_private_payload_sharing_preserves_mixed_historical_source_presence(
    tmp_path: Path, parsed: list[bytes], version: int
) -> None:
    exhaust = tmp_path / "exhaust"
    exhaust.mkdir()
    store = AuthoringIntentStore(exhaust)
    wires: list[bytes] = []
    previous = None
    directory = store.root / ("AIT-" + "1" * 32)
    (directory / "events").mkdir(parents=True)
    for sequence, presence in enumerate(("missing", "null", "content") * 2):
        raw = _wire_event(presence, version)
        raw["sequence"] = sequence
        raw["previous_event_digest"] = previous
        raw["operation_key"] = _operation(sequence)
        raw["intent"]["intent_revision"] = sequence
        _commit_raw_event(raw)
        previous = raw["event_digest"]
        wire = canonical_bytes(raw) + b"\n"
        (directory / "events" / f"{sequence:020d}.json").write_bytes(wire)
        wires.append(wire)

    private = store._validated_events(directory)
    assert len({id(event.intent.payload) for event in private}) == 3
    for index in range(3):
        assert private[index].intent.payload is private[index + 3].intent.payload
    for _ in range(2):
        assert [store._render_event(event) for event in store.events()] == wires
    assert len(parsed) == 6


def _change_set(rationale: str | None) -> ChangeSetAuthoringPayloadV1:
    members = (SubjectAuthoringPayloadV1(subject=_shell("wi-1")),)
    if rationale is None:
        return ChangeSetAuthoringPayloadV1(members=members)
    return ChangeSetAuthoringPayloadV1(members=members, rationale=rationale)


def _change_set_intent(rationale: str | None, revision: int) -> AuthoringIntentV1:
    payload = _change_set(rationale)
    membership = authoring_change_set_membership(payload.members)
    return AuthoringIntentV1(
        intent_id="AIT-" + "4" * 32,
        instance_id="history-reuse-fixture",
        actor_id="owner",
        canonical_timestamp=TIMESTAMP,
        base_coordinate=_coordinate(),
        semantic_identity="ChangeSet:"
        + typed_digest(
            Sha256Value,
            AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
            {"members": [{"kind": kind, "identity": identity} for kind, identity in membership]},
        ).value,
        payload=payload,
        payload_digest=authoring_payload_digest(payload),
        create_fingerprint=authoring_create_fingerprint(
            instance_id="history-reuse-fixture", actor_id="owner", payload=payload
        ),
        intent_revision=revision,
        candidate_status=CandidateStatusV1(
            state="draft", current_accepted_coordinate=_coordinate()
        ),
    )


def _write_change_set_history(
    exhaust: Path, rationales: tuple[str | None, ...]
) -> tuple[AuthoringIntentStore, tuple[AuthoringIntentEventAny, ...]]:
    exhaust.mkdir(parents=True, exist_ok=True)
    store = AuthoringIntentStore(exhaust)
    events: list[AuthoringIntentEventAny] = []
    for sequence, rationale in enumerate(rationales):
        event = build_authoring_intent_event(
            sequence=sequence,
            previous_event_digest=events[-1].event_digest if events else None,
            operation_key=_operation(sequence),
            intent=_change_set_intent(rationale, sequence),
        )
        path = _event_path(store, event)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(store._render_event(event))
        events.append(event)
    return store, tuple(events)


def test_a_rationale_only_revision_is_never_shared_with_the_one_it_replaced(
    tmp_path: Path, parsed: list[bytes]
) -> None:
    """Two revisions of one set can share a payload digest and not a payload.

    A change set's payload digest deliberately drops its rationale, so prose
    alone does not restate the set's identity. Sharing the decoded payload under
    that digest would hand the reader the prose it replaced -- silently, because
    the bytes on disk still carry the edit.
    """

    store, events = _write_change_set_history(
        tmp_path / "exhaust", ("The first prose.", "The corrected prose.")
    )
    assert events[0].intent.payload_digest == events[1].intent.payload_digest
    assert events[0].intent.create_fingerprint != events[1].intent.create_fingerprint

    directory = store.root / events[0].intent.intent_id
    private = store._validated_events(directory)

    assert private[0].intent.payload is not private[1].intent.payload
    assert [event.intent.payload.rationale for event in store.events()] == [
        "The first prose.",
        "The corrected prose.",
    ]
    assert store.get(events[0].intent.intent_id, actor_id="owner").payload.rationale == (
        "The corrected prose."
    )
    assert tuple(store._render_event(event) for event in private) == tuple(
        _event_path(store, event).read_bytes() for event in events
    )
    assert len(parsed) == 2


def test_an_unchanged_change_set_payload_is_still_shared_across_revisions(
    tmp_path: Path, parsed: list[bytes]
) -> None:
    store, events = _write_change_set_history(
        tmp_path / "exhaust", ("The same prose.", "The same prose.", None)
    )
    private = store._validated_events(store.root / events[0].intent.intent_id)

    assert private[0].intent.payload is private[1].intent.payload
    assert private[2].intent.payload is not private[0].intent.payload
    assert private[2].intent.payload.rationale is None
    assert len(parsed) == 3
