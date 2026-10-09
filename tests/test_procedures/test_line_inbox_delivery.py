"""A Line's ``post_inbox`` terminal: items posted as they are, with their own evidence."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.procedures.models import (
    InboxEgressNode,
    ProjectNode,
    SourceNode,
)
from cruxible_core.exhaust.line_inbox import LineInboxStore
from cruxible_core.runtime.instance import PlaybillInstance
from tests.test_procedures.test_manual_line_runs import manual_run
from tests.test_procedures.test_procedure_source_runs import (
    ADVISORY,
    NOW,
    SOURCE_ALIAS,
    _accept_more,
    _served_line,
    _world,
)

ROWS = [{"cve": "CVE-2026-0001"}, {"cve": "CVE-2026-0002"}, {"cve": "CVE-2026-0003"}]


def inbox_world(tmp_path: Path, *, max_items: int | None = None, items: Any = None):  # type: ignore[no-untyped-def]
    """The Source Procedure, ending in a post_inbox over its rows, behind an observe-only Line."""

    instance, owner, procedure, root, policy = _world(
        tmp_path,
        accept_procedure=False,
        contents=canonical_bytes({**ADVISORY, "rows": ROWS}),
    )
    source, shape = procedure.definition.nodes
    assert isinstance(source, SourceNode) and isinstance(shape, ProjectNode)
    definition = procedure.definition.model_copy(
        update={
            "nodes": (
                source,
                shape.model_copy(update={"next": "inbox"}),
                InboxEgressNode(
                    node_id="inbox",
                    input=(
                        {"items": f"$steps.{SOURCE_ALIAS}.content.json.rows"}
                        if items is None
                        else items
                    ),
                    max_items=max_items,
                ),
            ),
            "terminal_capability": 1,
        }
    )
    with_inbox = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    line = _served_line(with_inbox, policy)
    _accept_more(
        instance,
        owner,
        {
            procedure_path(with_inbox.identity.name): render_procedure(with_inbox),
            line_spec_path(line.identity.name): render_line_spec(line),
        },
        name="inbox-line",
    )
    return instance, root, line


def _entries(instance: PlaybillInstance) -> list[Any]:
    entries, _cursor = LineInboxStore(instance).page(line=None, status=None, limit=50, cursor=None)
    return list(entries)


def test_a_line_posts_each_item_as_is_with_its_capture_attached(tmp_path: Path) -> None:
    instance, root, line = inbox_world(tmp_path)
    head = instance.accepted_coordinate()

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert (egress.kind, egress.verdict, egress.required_authority) == (
        "post_inbox",
        "delivered",
        "observe",
    )
    assert egress.inbox_truncation is None
    (capture,) = {item.capture_digest for item in state.source_observations}
    entries = _entries(instance)
    assert sorted(item.payload["cve"] for item in entries) == [row["cve"] for row in ROWS]
    for item in entries:
        envelope = item.envelope
        assert envelope.status == "unread"
        assert envelope.line == line.identity.qualified
        assert envelope.node_id == "inbox"
        assert envelope.occurrences == 1
        assert envelope.first_sighting == envelope.last_sighting
        assert envelope.last_sighting.run_id == state.run_id
        assert envelope.last_sighting.source_capture_digests == (capture,)
    assert {child.egress_digest for child in egress.children} == {
        item.envelope.dedupe_key for item in entries
    }
    assert LineInboxStore(instance).unread_by_line() == ((line.identity.qualified, 3),)
    # Posting is inert: the accepted head did not move.
    assert instance.accepted_coordinate() == head


def test_a_run_posts_at_most_its_cap_and_reports_the_truncation(tmp_path: Path) -> None:
    instance, root, line = inbox_world(tmp_path, max_items=2)
    head = instance.accepted_coordinate()

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.inbox_truncation is not None
    assert egress.inbox_truncation.model_dump(exclude={"tag"}) == {
        "cap": 2,
        "cap_source": "declared",
        "selected": 3,
        "posted": 2,
    }
    assert len(egress.children) == 2
    posted = sorted((item.payload for item in _entries(instance)), key=lambda row: row["cve"])
    assert posted == ROWS[:2]
    assert instance.accepted_coordinate() == head


def test_a_single_input_is_one_item(tmp_path: Path) -> None:
    instance, root, line = inbox_world(tmp_path, items=f"$steps.{SOURCE_ALIAS}.content.json")

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    (entry,) = _entries(instance)
    assert entry.payload == {**ADVISORY, "rows": ROWS}


def test_an_hourly_reread_of_identical_rows_is_one_item_per_row_seen_each_hour(
    tmp_path: Path,
) -> None:
    """Each tick reads its own Capture; the same rows are the same items, seen again."""

    instance, root, line = inbox_world(tmp_path)
    hours = 4

    states = [
        manual_run(instance, line, at=NOW + timedelta(hours=hour), root=root)
        for hour in range(hours)
    ]

    assert all(state.status == "succeeded" for state in states)
    entries = _entries(instance)
    assert sorted(item.payload["cve"] for item in entries) == [row["cve"] for row in ROWS]
    captures = [
        next(iter({item.capture_digest for item in state.source_observations})) for state in states
    ]
    assert len(set(captures)) == hours  # new evidence every hour
    for item in entries:
        envelope = item.envelope
        assert envelope.occurrences == hours
        assert envelope.first_sighting.run_id == states[0].run_id
        assert [sighting.source_capture_digests for sighting in envelope.recent_sightings] == [
            (capture,) for capture in reversed(captures)
        ]
    assert LineInboxStore(instance).unread_by_line() == ((line.identity.qualified, len(ROWS)),)


def test_the_same_item_again_resurfaces_with_its_prior_disposition(tmp_path: Path) -> None:
    """An item no Capture backs, posted by two runs: one item, seen twice, decided once."""

    from cruxible_client.contracts.line_inbox import InboxDismissed
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from tests.test_procedures.test_procedure_source_runs import _actor

    instance, root, line = inbox_world(tmp_path, items={"items": [{"note": "check the feed"}]})
    first = manual_run(instance, line, at=NOW, root=root)
    (entry,) = _entries(instance)
    assert entry.envelope.last_sighting.source_capture_digests == ()
    store = LineInboxStore(instance)
    dismissed = InboxDismissed(actor_id="owner", decided_at=NOW, reason="seen it")
    store.record_disposition(
        entry.envelope.item_id,
        dismissed,
        actor=_actor(instance),
        now=NOW,
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )

    second = manual_run(instance, line, at=NOW + timedelta(seconds=1), root=root)

    assert first.status == second.status == "succeeded"
    (entry,) = _entries(instance)
    envelope = entry.envelope
    assert (envelope.status, envelope.occurrences, envelope.recurred_since_disposition) == (
        "unread",
        2,
        1,
    )
    assert envelope.disposition == dismissed
    assert (envelope.first_sighting.run_id, envelope.last_sighting.run_id) == (
        first.run_id,
        second.run_id,
    )
