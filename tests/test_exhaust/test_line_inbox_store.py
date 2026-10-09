"""The Line inbox store: postings, dedupe, dispositions, paging, and its rebuildable index."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.line_inbox import (
    InboxAcknowledged,
    InboxDismissed,
    InboxLinked,
    InboxLinkProposal,
    InboxLinkSubject,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.write import SUBJECT_REF_PATTERN
from cruxible_core.exhaust.line_inbox import (
    InboxItemNotFound,
    InboxPosting,
    LineInboxStore,
    inbox_dedupe_key,
    inbox_payload_digest,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._support import initialize_local

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
CAPTURE = "sha256:" + "c" * 64
DIGEST = "sha256:" + "d" * 64


def _actor(instance: PlaybillInstance) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="agent",
        org_id=instance.descriptor.instance_id,
        operation_id="line-inbox-store-test",
        timestamp=NOW,
    )


def _posting(
    payload: object,
    *,
    run_id: str = "RUN-1",
    item_key: str = "item-0",
    line: str = "Line:watch",
    captures: tuple[str, ...] = (CAPTURE,),
    at: datetime = NOW,
) -> InboxPosting:
    return InboxPosting(
        line=line,
        procedure="Procedure:watcher",
        procedure_artifact_digest=DIGEST,
        run_id=run_id,
        node_id="inbox",
        item_key=item_key,
        payload=payload,  # type: ignore[arg-type]
        source_capture_digests=captures,
        posted_at=at,
    )


def _world(tmp_path: Path) -> tuple[PlaybillInstance, LineInboxStore, dict[str, object]]:
    instance, _owner = initialize_local(tmp_path)
    store = LineInboxStore(instance)
    write = {
        "actor": _actor(instance),
        "now": NOW,
        "coordinate": AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    }
    return instance, store, write


def test_the_subject_grammar_is_the_write_surfaces_own() -> None:
    from cruxible_client.contracts.line_inbox import INBOX_SUBJECT_REF_PATTERN

    assert INBOX_SUBJECT_REF_PATTERN == SUBJECT_REF_PATTERN


def test_a_posting_lands_unread_with_its_envelope(tmp_path: Path) -> None:
    _instance, store, write = _world(tmp_path)

    (key,) = store.post([_posting({"cve": "CVE-1"})], **write)  # type: ignore[arg-type]

    assert key == inbox_dedupe_key(
        line="Line:watch",
        payload_digest=inbox_payload_digest({"cve": "CVE-1"}),
        source_capture_digests=(CAPTURE,),
    )
    item_id = store.resolve(key)
    stored = store.get(item_id)
    assert stored.payload == {"cve": "CVE-1"}
    envelope = stored.envelope
    assert envelope.status == "unread"
    assert (envelope.line, envelope.procedure, envelope.run_id, envelope.node_id) == (
        "Line:watch",
        "Procedure:watcher",
        "RUN-1",
        "inbox",
    )
    assert envelope.source_capture_digests == (CAPTURE,)
    assert envelope.occurrences == 1 and envelope.disposition is None
    assert store.unread_by_line() == (("Line:watch", 1),)


def test_the_same_item_on_other_evidence_or_another_line_is_another_item(tmp_path: Path) -> None:
    _instance, store, write = _world(tmp_path)

    keys = {
        store.post([_posting({"a": 1}, run_id="RUN-1")], **write)[0],  # type: ignore[arg-type]
        store.post(  # type: ignore[arg-type]
            [_posting({"a": 1}, run_id="RUN-2", captures=("sha256:" + "e" * 64,))], **write
        )[0],
        store.post([_posting({"a": 1}, run_id="RUN-3", line="Line:other")], **write)[0],  # type: ignore[arg-type]
    }

    assert len(keys) == 3
    assert store.unread_by_line() == (("Line:other", 1), ("Line:watch", 2))


def test_a_recurring_item_counts_and_resurfaces_with_its_prior_disposition(
    tmp_path: Path,
) -> None:
    _instance, store, write = _world(tmp_path)
    (key,) = store.post([_posting({"a": 1})], **write)  # type: ignore[arg-type]
    item_id = store.resolve(key)
    dismissed = InboxDismissed(actor_id="agent", decided_at=NOW, reason="known noise")
    envelope, recorded = store.record_disposition(item_id, dismissed, **write)  # type: ignore[arg-type]
    assert recorded and envelope.status == "dismissed"
    assert store.unread_by_line() == ()

    later = NOW + timedelta(hours=1)
    store.post([_posting({"a": 1}, run_id="RUN-2", at=later)], **write)  # type: ignore[arg-type]

    envelope = store.get(item_id).envelope
    assert envelope.status == "unread"
    assert envelope.occurrences == 2
    assert envelope.disposition == dismissed
    assert envelope.recurred_since_disposition == 1
    assert (envelope.run_id, envelope.last_run_id) == ("RUN-1", "RUN-2")
    assert envelope.last_posted_at == later
    assert store.unread_by_line() == (("Line:watch", 1),)


def test_the_same_run_delivering_twice_counts_once(tmp_path: Path) -> None:
    _instance, store, write = _world(tmp_path)
    store.post([_posting({"a": 1})], **write)  # type: ignore[arg-type]
    journal = store._journal(create=False)
    assert journal is not None
    records_before = len(journal.select_records(store.stream, partition_id="inbox"))

    store.post([_posting({"a": 1})], **write)  # type: ignore[arg-type]

    assert len(journal.select_records(store.stream, partition_id="inbox")) == records_before
    (entry,), _cursor = store.page(line=None, status=None, limit=10, cursor=None)
    assert entry.envelope.occurrences == 1


def test_dispositions_are_journaled_and_a_repeat_records_nothing(tmp_path: Path) -> None:
    _instance, store, write = _world(tmp_path)
    (key,) = store.post([_posting({"a": 1})], **write)  # type: ignore[arg-type]
    item_id = store.resolve(key)

    ack = InboxAcknowledged(actor_id="agent", decided_at=NOW)
    assert store.record_disposition(item_id, ack, **write)[1]  # type: ignore[arg-type]
    assert not store.record_disposition(item_id, ack, **write)[1]  # type: ignore[arg-type]
    linked = InboxLinked(
        actor_id="agent", decided_at=NOW, target=InboxLinkProposal(proposal_id="prop-1")
    )
    envelope, recorded = store.record_disposition(item_id, linked, **write)  # type: ignore[arg-type]
    assert recorded and envelope.status == "linked"
    note = InboxLinked(
        actor_id="agent", decided_at=NOW, target=InboxLinkSubject(subject="dev.note/triage")
    )
    store.record_disposition(item_id, note, **write)  # type: ignore[arg-type]

    assert [item.disposition for item in store.get(item_id).dispositions] == [
        "acknowledged",
        "linked",
        "linked",
    ]


def test_the_index_rebuilds_from_the_journal(tmp_path: Path) -> None:
    instance, store, write = _world(tmp_path)
    (key,) = store.post([_posting({"a": 1})], **write)  # type: ignore[arg-type]
    item_id = store.resolve(key)
    store.record_disposition(  # type: ignore[arg-type]
        item_id, InboxDismissed(actor_id="agent", decided_at=NOW, reason="noise"), **write
    )
    store.post([_posting({"b": 2}, run_id="RUN-2")], **write)  # type: ignore[arg-type]
    before = store.page(line=None, status=None, limit=10, cursor=None)

    store.path.unlink()
    rebuilt = LineInboxStore(instance)

    assert rebuilt.page(line=None, status=None, limit=10, cursor=None) == before
    assert rebuilt.get(item_id).dispositions == store.get(item_id).dispositions
    assert rebuilt.unread_by_line() == (("Line:watch", 1),)


def test_a_page_is_most_recent_first_and_filters_by_line_and_status(tmp_path: Path) -> None:
    _instance, store, write = _world(tmp_path)
    for index in range(5):
        store.post(  # type: ignore[arg-type]
            [
                _posting(
                    {"n": index},
                    run_id=f"RUN-{index}",
                    line="Line:watch" if index % 2 == 0 else "Line:other",
                    at=NOW + timedelta(minutes=index),
                )
            ],
            **write,
        )

    first, cursor = store.page(line=None, status=None, limit=2, cursor=None)
    second, cursor2 = store.page(line=None, status=None, limit=2, cursor=cursor)
    third, cursor3 = store.page(line=None, status=None, limit=2, cursor=cursor2)
    assert [item.payload for item in (*first, *second, *third)] == [
        {"n": 4},
        {"n": 3},
        {"n": 2},
        {"n": 1},
        {"n": 0},
    ]
    assert cursor3 is None
    watched, _ = store.page(line="Line:watch", status="unread", limit=10, cursor=None)
    assert [item.payload for item in watched] == [{"n": 4}, {"n": 2}, {"n": 0}]
    assert store.page(line=None, status="dismissed", limit=10, cursor=None) == ((), None)


def test_a_selector_must_name_exactly_one_item(tmp_path: Path) -> None:
    _instance, store, write = _world(tmp_path)
    (key,) = store.post([_posting({"a": 1})], **write)  # type: ignore[arg-type]
    item_id = store.resolve(key)

    assert store.resolve(item_id[:16]) == item_id
    with pytest.raises(InboxItemNotFound):
        store.resolve("INB-" + "0" * 12)


def test_an_empty_inbox_reads_without_creating_anything(tmp_path: Path) -> None:
    _instance, store, _write = _world(tmp_path)

    assert store.unread_by_line() == ()
    assert store.page(line=None, status=None, limit=5, cursor=None) == ((), None)
    assert not store.root.exists()
