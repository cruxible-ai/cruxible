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


def test_a_run_count_splits_new_items_from_re_sightings(tmp_path: Path) -> None:
    """What applying each posting learned (insert or update) is the run's split."""

    _instance, store, write = _world(tmp_path)
    store.post([_posting({"a": 1}, run_id="RUN-1")], **write)  # type: ignore[arg-type]

    store.post(  # type: ignore[arg-type]
        [
            _posting({"a": 1}, run_id="RUN-2", item_key="item-0"),
            _posting({"b": 2}, run_id="RUN-2", item_key="item-1"),
        ],
        **write,
    )

    assert store.posted_by_run("RUN-1") == (1, 1)
    assert store.posted_by_run("RUN-2") == (2, 1)
    assert store.posted_by_run("RUN-none") == (0, 0)


def test_a_layout_5_projection_gains_the_split_in_place_with_earlier_postings_unknown(
    tmp_path: Path,
) -> None:
    """Additive: the column is added without a replay; rows it predates read unknown."""

    import sqlite3

    _instance, store, write = _world(tmp_path)
    _post_inline(store, [_posting({"a": 1}, run_id="RUN-1")], write)
    _layout_5(store)

    store.post([_posting({"a": 1}, run_id="RUN-2")], **write)  # type: ignore[arg-type]

    assert store.posted_by_run("RUN-1") == (1, None)
    assert store.posted_by_run("RUN-2") == (1, 0)
    connection = sqlite3.connect(store.path)
    # Layout 5 moves in place to 9 (the columns, then the batch and group tables).
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    connection.close()


def _post_inline(store: LineInboxStore, postings: list[InboxPosting], write: dict) -> None:  # type: ignore[type-arg]
    """Post as builds before posting batches did: every item inline, one row each."""

    original = LineInboxStore._posted_data
    LineInboxStore._posted_data = lambda self, fresh: {"items": fresh}  # type: ignore[method-assign]
    try:
        store.post(postings, **write)
    finally:
        LineInboxStore._posted_data = original  # type: ignore[method-assign]


def _layout_5(store: LineInboxStore) -> None:
    """Rewind a projection to what layout 5 kept: postings without the created column."""

    import sqlite3

    connection = sqlite3.connect(store.path)
    connection.execute("ALTER TABLE postings DROP COLUMN created")
    connection.execute("PRAGMA user_version=5")
    connection.commit()
    connection.close()


def test_an_upgrade_stopped_before_its_version_moves_leaves_layout_5_and_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The column and the version move in one transaction: a stop between them undoes both."""

    import sqlite3

    _instance, store, write = _world(tmp_path)
    _post_inline(store, [_posting({"a": 1}, run_id="RUN-1")], write)
    _layout_5(store)
    open_projection = store._open_projection  # noqa: SLF001

    def stopped_before_the_version(*_args: object) -> int:
        action, first, second = _args[:3]
        if action == sqlite3.SQLITE_PRAGMA and first == "user_version" and second == "9":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def interrupted() -> sqlite3.Connection:
        conn = open_projection()
        conn.set_authorizer(stopped_before_the_version)
        return conn

    monkeypatch.setattr(store, "_open_projection", interrupted)
    with pytest.raises(sqlite3.DatabaseError):
        store.posted_by_run("RUN-1")
    monkeypatch.undo()
    with sqlite3.connect(f"file:{store.path}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = {row[1] for row in connection.execute("PRAGMA table_info(postings)")}
    assert "created" not in columns

    assert store.posted_by_run("RUN-1") == (1, None)
    assert store.posted_by_run("RUN-1") == (1, None)


def test_a_column_an_earlier_upgrade_left_at_layout_5_is_kept_not_added_twice(
    tmp_path: Path,
) -> None:
    import sqlite3

    _instance, store, write = _world(tmp_path)
    _post_inline(store, [_posting({"a": 1}, run_id="RUN-1")], write)
    _layout_5(store)
    connection = sqlite3.connect(store.path)
    connection.execute("ALTER TABLE postings ADD COLUMN created INTEGER")
    connection.commit()
    connection.close()

    assert store.posted_by_run("RUN-1") == (1, None)
    store.post([_posting({"a": 1}, run_id="RUN-2")], **write)  # type: ignore[arg-type]
    assert store.posted_by_run("RUN-2") == (1, 0)


def test_a_terminal_reposting_the_same_items_journals_one_batch_and_no_item_row(
    tmp_path: Path,
) -> None:
    """Codex 3.5: a re-sighting batch cites the body the first posting stored."""

    import sqlite3

    instance, store, write = _world(tmp_path)
    items = [{"cve": f"CVE-{index}"} for index in range(5)]

    def post(run_id: str, at: datetime) -> None:
        store.post(
            [
                _posting(item, run_id=run_id, item_key=f"item-{index}", at=at)
                for index, item in enumerate(items)
            ],
            **write,  # type: ignore[arg-type]
        )

    post("RUN-1", NOW)
    cas = instance.root / "cas"
    before = {path for path in cas.rglob("*") if path.is_file()}
    post("RUN-2", NOW + timedelta(hours=1))
    added = {path for path in cas.rglob("*") if path.is_file()} - before
    # The run's journal record payload alone: the items body is the first run's.
    assert len(added) == 1
    assert store.posted_by_run("RUN-1") == (5, 5)
    assert store.posted_by_run("RUN-2") == (5, 0)
    # A recovered delivery of the same terminal counts once.
    post("RUN-2", NOW + timedelta(hours=1))
    assert store.posted_by_run("RUN-2") == (5, 0)
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM posting_batches").fetchone()[0] == 2
    finally:
        conn.close()
    entries, _cursor = store.page(line=None, status=None, limit=10, cursor=None)
    assert {entry.envelope.occurrences for entry in entries} == {2}


def test_a_resighted_batch_writes_one_group_row_and_no_item_row_at_two_sizes(
    tmp_path: Path,
) -> None:
    """Open question 3: a re-sighting is one row for its group; items are not rewritten.

    Each item's occurrences, window and last sighting as read come from its
    group's sightings since it joined.
    """

    import sqlite3

    for size in (3, 30):
        (tmp_path / str(size)).mkdir()
        _instance, store, write = _world(tmp_path / str(size))
        items = [{"cve": f"CVE-{index}"} for index in range(size)]

        def post(run_id: str, at: datetime) -> None:
            store.post(
                [
                    _posting(item, run_id=run_id, item_key=f"item-{index}", at=at)
                    for index, item in enumerate(items)
                ],
                **write,  # type: ignore[arg-type]
            )

        post("RUN-1", NOW)

        def stored() -> list[tuple[object, ...]]:
            conn = sqlite3.connect(store.path)
            try:
                return conn.execute(
                    "SELECT item_id,last_seen,envelope,group_base FROM items ORDER BY item_id"
                ).fetchall()
            finally:
                conn.close()

        before = stored()
        post("RUN-2", NOW + timedelta(hours=1))
        post("RUN-3", NOW + timedelta(hours=2))
        assert stored() == before  # no item row rewritten
        conn = sqlite3.connect(store.path)
        try:
            assert conn.execute("SELECT occurrences FROM inbox_groups").fetchall() == [(3,)]
        finally:
            conn.close()
        entries, _cursor = store.page(line=None, status=None, limit=size, cursor=None)
        assert len(entries) == size
        for entry in entries:
            envelope = entry.envelope
            assert envelope.occurrences == 3
            assert envelope.last_sighting.run_id == "RUN-3"
            assert [item.run_id for item in envelope.recent_sightings][:3] == [
                "RUN-3",
                "RUN-2",
                "RUN-1",
            ]
            assert store.get(envelope.item_id).envelope == envelope


def test_grouped_sightings_keep_their_true_first_and_latest_after_out_of_order_recovery(
    tmp_path: Path,
) -> None:
    """F-006: recovered batches posted earlier than the group's latest sighting.

    More recovered batches than the recent window arrive after a newer
    posting; separately a backdated batch predates the first. Each item still
    reads the true latest and first postings, its count and its newest window.
    """

    items = [{"cve": f"CVE-{index}"} for index in range(3)]
    for name, minutes in (
        ("latest", (10, 100, 20, 21, 22, 23, 24)),
        ("first", (100, 0, 200, 201, 202, 203, 204)),
    ):
        (tmp_path / name).mkdir()
        _instance, store, write = _world(tmp_path / name)
        for index, minute in enumerate(minutes):
            store.post(
                [
                    _posting(
                        item,
                        run_id=f"RUN-{index}",
                        item_key=f"item-{position}",
                        at=NOW + timedelta(minutes=minute),
                    )
                    for position, item in enumerate(items)
                ],
                **write,  # type: ignore[arg-type]
            )
        entries, _cursor = store.page(line=None, status=None, limit=10, cursor=None)
        assert len(entries) == len(items)
        for entry in entries:
            envelope = entry.envelope
            assert envelope.occurrences == len(minutes), name
            assert envelope.last_sighting.posted_at == NOW + timedelta(minutes=max(minutes)), name
            assert envelope.first_sighting.posted_at == NOW + timedelta(minutes=min(minutes)), name
            newest = sorted(minutes, reverse=True)[: len(envelope.recent_sightings)]
            assert [item.posted_at for item in envelope.recent_sightings] == [
                NOW + timedelta(minutes=minute) for minute in newest
            ], name
            assert store.get(envelope.item_id).envelope == envelope


def test_a_layout_8_projection_is_replayed_from_the_journal(tmp_path: Path) -> None:
    """Review 2c #9: a layout-8 projection (unreleased builds only) may hold a
    group window that lost its first or latest sighting; it is replayed from
    the journal, which restores both."""

    import sqlite3

    _instance, store, write = _world(tmp_path)
    items = [{"cve": "CVE-0"}]
    for index, minute in enumerate((10, 100)):
        store.post(
            [
                _posting(item, run_id=f"RUN-{index}", at=NOW + timedelta(minutes=minute))
                for item in items
            ],
            **write,  # type: ignore[arg-type]
        )
    connection = sqlite3.connect(store.path)
    try:
        # A lost latest, as the earlier algorithm could leave it, at layout 8.
        connection.execute("UPDATE inbox_groups SET recent='[]', last_seen=?", ("x",))
        connection.execute("PRAGMA user_version=8")
        connection.commit()
    finally:
        connection.close()
    entries, _cursor = store.page(line=None, status=None, limit=10, cursor=None)
    (entry,) = entries
    assert entry.envelope.last_sighting.posted_at == NOW + timedelta(minutes=100)
    assert entry.envelope.occurrences == 2
