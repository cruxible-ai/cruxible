"""The Line inbox store: postings, dedupe, dispositions, paging, and its rebuildable index."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.line_inbox import (
    INBOX_RECENT_SIGHTINGS,
    InboxAcknowledged,
    InboxDismissed,
    InboxLinked,
    InboxLinkProposal,
    InboxLinkSubject,
    InboxSighting,
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
    node_id: str = "inbox",
) -> InboxPosting:
    return InboxPosting(
        line=line,
        procedure="Procedure:watcher",
        procedure_artifact_digest=DIGEST,
        run_id=run_id,
        node_id=node_id,
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
        node_id="inbox",
        payload_digest=inbox_payload_digest({"cve": "CVE-1"}),
    )
    item_id = store.resolve(key)
    stored = store.get(item_id)
    assert stored.payload == {"cve": "CVE-1"}
    envelope = stored.envelope
    assert envelope.status == "unread"
    assert (envelope.line, envelope.procedure, envelope.node_id) == (
        "Line:watch",
        "Procedure:watcher",
        "inbox",
    )
    sighting = InboxSighting(run_id="RUN-1", posted_at=NOW, source_capture_digests=(CAPTURE,))
    assert envelope.first_sighting == sighting
    assert envelope.recent_sightings == (sighting,)
    assert envelope.occurrences == 1 and envelope.disposition is None
    assert store.unread_by_line() == (("Line:watch", 1),)


def test_the_same_payload_on_new_evidence_is_the_same_item_seen_again(tmp_path: Path) -> None:
    _instance, store, write = _world(tmp_path)
    other = "sha256:" + "e" * 64

    first = store.post([_posting({"a": 1}, run_id="RUN-1")], **write)[0]  # type: ignore[arg-type]
    again = store.post(  # type: ignore[arg-type]
        [_posting({"a": 1}, run_id="RUN-2", captures=(other,), at=NOW + timedelta(hours=1))],
        **write,
    )[0]

    assert first == again
    envelope = store.get(store.resolve(first)).envelope
    assert envelope.occurrences == 2
    assert envelope.first_sighting.source_capture_digests == (CAPTURE,)
    assert [item.source_capture_digests for item in envelope.recent_sightings] == [
        (other,),
        (CAPTURE,),
    ]
    assert store.unread_by_line() == (("Line:watch", 1),)


def test_another_line_or_another_terminal_is_another_item(tmp_path: Path) -> None:
    _instance, store, write = _world(tmp_path)

    keys = {
        store.post([_posting({"a": 1}, run_id="RUN-1")], **write)[0],  # type: ignore[arg-type]
        store.post([_posting({"a": 1}, run_id="RUN-2", line="Line:other")], **write)[0],  # type: ignore[arg-type]
        store.post([_posting({"a": 1}, run_id="RUN-3", node_id="escalate")], **write)[0],  # type: ignore[arg-type]
    }

    assert len(keys) == 3
    assert store.unread_by_line() == (("Line:other", 1), ("Line:watch", 2))


def test_an_item_keeps_its_first_and_a_bounded_window_of_recent_sightings(
    tmp_path: Path,
) -> None:
    _instance, store, write = _world(tmp_path)
    runs = 8
    for index in range(runs):
        (key,) = store.post(  # type: ignore[arg-type]
            [
                _posting(
                    {"a": 1},
                    run_id=f"RUN-{index}",
                    captures=(f"sha256:{index:064x}",),
                    at=NOW + timedelta(hours=index),
                )
            ],
            **write,
        )
    # A recovered run lands late: it is counted, and the window stays by time.
    store.post(  # type: ignore[arg-type]
        [_posting({"a": 1}, run_id="RUN-late", at=NOW - timedelta(hours=1))], **write
    )

    envelope = store.get(store.resolve(key)).envelope
    assert envelope.occurrences == runs + 1
    assert envelope.first_sighting.run_id == "RUN-late"
    assert [item.run_id for item in envelope.recent_sightings] == [
        f"RUN-{index}" for index in range(runs - 1, runs - 1 - INBOX_RECENT_SIGHTINGS, -1)
    ]
    assert envelope.last_sighting.posted_at == NOW + timedelta(hours=runs - 1)


def test_an_identical_re_sighting_counts_and_keeps_its_decision(
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
    # The same dedupe key is the same payload: no new work, the decision stands.
    assert envelope.status == "dismissed"
    assert envelope.occurrences == 2
    assert envelope.disposition == dismissed
    assert envelope.recurred_since_disposition == 1
    assert (envelope.first_sighting.run_id, envelope.last_sighting.run_id) == ("RUN-1", "RUN-2")
    assert envelope.last_sighting.posted_at == later
    assert store.unread_by_line() == ()
    # A changed payload is a new key, and that is new work.
    store.post([_posting({"a": 2}, run_id="RUN-3", at=later)], **write)  # type: ignore[arg-type]
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

    stored = store.get(item_id)
    assert [item.disposition for item in stored.recent_dispositions] == [
        "linked",
        "linked",
        "acknowledged",
    ]
    assert stored.envelope.disposition_count == 3


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
    assert rebuilt.get(item_id).recent_dispositions == store.get(item_id).recent_dispositions
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
    assert [item.preview.object for item in (*first, *second, *third)] == [  # type: ignore[union-attr]
        {"n": 4},
        {"n": 3},
        {"n": 2},
        {"n": 1},
        {"n": 0},
    ]
    assert cursor3 is None
    watched, _ = store.page(line="Line:watch", status="unread", limit=10, cursor=None)
    assert [item.preview.object for item in watched] == [{"n": 4}, {"n": 2}, {"n": 0}]  # type: ignore[union-attr]
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


def test_a_whole_item_read_returns_its_latest_decisions_only(tmp_path: Path) -> None:
    """Review F-006: an item decided many times keeps a bounded recent window."""

    from cruxible_client.contracts.line_inbox import INBOX_RECENT_DISPOSITIONS

    _instance, store, write = _world(tmp_path)
    (key,) = store.post([_posting({"a": 1})], **write)  # type: ignore[arg-type]
    item_id = store.resolve(key)
    decisions = INBOX_RECENT_DISPOSITIONS + 5
    for index in range(decisions):
        store.record_disposition(  # type: ignore[arg-type]
            item_id,
            InboxDismissed(actor_id="agent", decided_at=NOW, reason=f"reason {index}"),
            **write,
        )

    stored = store.get(item_id)
    assert stored.envelope.disposition_count == decisions
    assert [item.reason for item in stored.recent_dispositions] == [  # type: ignore[union-attr]
        f"reason {index}"
        for index in range(decisions - 1, decisions - 1 - INBOX_RECENT_DISPOSITIONS, -1)
    ]


def test_the_store_refuses_an_item_over_the_byte_bound(tmp_path: Path) -> None:
    """Review F-003: the bound holds at the store too, before anything is journaled."""

    from cruxible_client.contracts.line_inbox import INBOX_ITEM_MAX_BYTES

    _instance, store, write = _world(tmp_path)

    with pytest.raises(ValueError, match="at most"):
        store.post([_posting("x" * INBOX_ITEM_MAX_BYTES)], **write)  # type: ignore[arg-type]
    assert not store.root.exists() or store.page(line=None, status=None, limit=5, cursor=None) == (
        (),
        None,
    )


def test_an_older_projection_layout_is_rebuilt_from_the_journal(tmp_path: Path) -> None:
    import sqlite3

    _instance, store, write = _world(tmp_path)
    (key,) = store.post([_posting({"a": 1})], **write)  # type: ignore[arg-type]
    connection = sqlite3.connect(store.path)
    connection.execute("PRAGMA user_version=1")
    connection.execute("DELETE FROM items")
    connection.commit()
    connection.close()

    assert store.get(store.resolve(key)).payload == {"a": 1}


def test_a_projection_under_the_reopening_rule_replays_once_under_the_new_one(
    tmp_path: Path,
) -> None:
    """Layout 4: the journal is unchanged; an older projection is rebuilt from it once."""

    import sqlite3

    _instance, store, write = _world(tmp_path)
    (key,) = store.post([_posting({"a": 1})], **write)  # type: ignore[arg-type]
    item_id = store.resolve(key)
    dismissed = InboxDismissed(actor_id="agent", decided_at=NOW, reason="known noise")
    store.record_disposition(item_id, dismissed, **write)  # type: ignore[arg-type]
    later = NOW + timedelta(hours=1)
    store.post([_posting({"a": 1}, run_id="RUN-2", at=later)], **write)  # type: ignore[arg-type]
    # What layout 3 wrote: the identical re-sighting re-opened the item.
    connection = sqlite3.connect(store.path)
    connection.execute("PRAGMA user_version=3")
    connection.execute("UPDATE items SET status='unread'")
    connection.commit()
    connection.close()

    envelope = store.get(item_id).envelope
    assert (envelope.status, envelope.occurrences) == ("dismissed", 2)
    assert store.unread_by_line() == ()
