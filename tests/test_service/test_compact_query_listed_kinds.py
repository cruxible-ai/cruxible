"""``query kind=Trigger`` and ``query kind=Line``: Triggers and Lines are discoverable.

Before, only ClaimType and Procedure were queryable artifact kinds, so a Trigger
(even the seeded floor-refresh) could only be found by exact name. The same
compact grammar now lists both: filterable name, schedule, target_kind, target
and lifecycle on Triggers, enabled on Lines, schedule detail through select.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.test_service.test_operational_get import line_world  # noqa: F401


def _query(instance: Any, when: Any, **fields: Any) -> Any:
    return service_playbill_query(
        instance, request=QueryRequest.model_validate({**fields, "evaluation_time": when})
    )


def test_triggers_list_with_their_schedule_and_target(line_world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, line, _dispatched, when = line_world

    result = _query(instance, when, kind="Trigger")

    assert result.kind == "Trigger"
    assert [column.name for column in result.columns] == [
        "name",
        "schedule",
        "target_kind",
        "target",
        "lifecycle",
    ]
    by_name = {row["name"]: row for row in result.rows}
    # The seeded floor-refresh Trigger is found without knowing its name.
    assert by_name["floor-refresh"]["schedule"] == "generation_accepted"
    assert by_name["floor-refresh"]["target_kind"] == "action"
    assert by_name["floor-refresh"]["target"] == "floor.refresh"
    aimed = [row for row in result.rows if row["target"] == line.identity.qualified]
    assert aimed and all(row["target_kind"] == "line" for row in aimed)

    only_lines = _query(
        instance, when, kind="Trigger", where=[{"field": "target_kind", "eq": "line"}]
    )
    assert {row["name"] for row in only_lines.rows} == {row["name"] for row in aimed}
    detail = _query(
        instance,
        when,
        kind="Trigger",
        where=[{"field": "schedule", "eq": "capture_landing"}],
        select=["schedule", "capture_contract", "version"],
    )
    assert detail.rows and all(row["capture_contract"] for row in detail.rows)
    assert all(row["version"] >= 1 for row in detail.rows)
    assert {"name", "schedule", "capture_contract", "version"} == set(detail.rows[0])


def test_lines_list_whether_they_are_enabled_and_their_triggers(line_world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, line, _dispatched, when = line_world

    result = _query(instance, when, kind="Line")

    (row,) = [item for item in result.rows if item["name"] == line.identity.name]
    assert row["procedure"] == line.procedure.target.qualified
    # The fixture's arm was stopped: the Line is not enabled.
    assert row["enabled"] is False
    assert row["triggers"] and all(name.startswith("Trigger:") for name in row["triggers"])
    assert (
        _query(instance, when, kind="Line", where=[{"field": "enabled", "eq": "true"}]).rows == ()
    )
    assert _query(instance, when, kind="Line", where=[{"field": "enabled", "eq": False}]).rows
    assert _query(instance, when, kind="Line", contains=line.identity.name).rows


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"kind": "Trigger", "where": [{"field": "cron", "eq": "x"}]}, "unknown_field"),
        ({"kind": "Trigger", "where": [{"field": "schedule", "eq": "hourly"}]}, "value_type"),
        ({"kind": "Line", "where": [{"field": "enabled", "eq": "maybe"}]}, "value_type"),
        ({"kind": "Line", "select": ["arm"]}, "unknown_field"),
        ({"kind": "Line", "follow": [{"field": "procedure", "as": "p"}]}, "follow_not_relation"),
    ],
)
def test_a_listed_kind_refuses_what_its_rows_cannot_answer(line_world, fields, code) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, _line, _dispatched, when = line_world
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, when, **fields)
    assert code in refused.value.error_code


# -- F-003: typed ordering; F-004: a ceiling and bounded Trigger names ---------


def test_ordering_is_typed_with_nulls_last_and_identity_ties() -> None:
    from cruxible_core.service.discovery.listed_kinds import ordered_rows

    rows = [
        ("Trigger:a", {"cadence": 10, "version": 2}),
        ("Trigger:b", {"cadence": None, "version": 10}),
        ("Trigger:c", {"cadence": 2, "version": 2}),
        ("Trigger:d", {"cadence": None, "version": 1}),
    ]
    names = lambda result: [identity for identity, _ in result]  # noqa: E731

    assert names(ordered_rows(rows, [("cadence", False)])) == [
        "Trigger:c",
        "Trigger:a",
        "Trigger:b",
        "Trigger:d",
    ]
    assert names(ordered_rows(rows, [("cadence", True)])) == [
        "Trigger:a",
        "Trigger:c",
        "Trigger:b",
        "Trigger:d",
    ]
    assert names(ordered_rows(rows, [("version", True)])) == [
        "Trigger:b",
        "Trigger:a",
        "Trigger:c",
        "Trigger:d",
    ]


@pytest.fixture(scope="module")
def cadence_world(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """One Line with five cadence Triggers (2, 3, 10, 10, 40 seconds) aimed at it."""

    from cruxible_client.contracts.triggers import CadenceSchedule
    from tests.support.lines import line_trigger
    from tests.test_procedures.test_line_triggers import line_world as make_line_world

    seconds = {"t-a": 10, "t-b": 2, "t-c": 40, "t-d": 3, "t-e": 10}
    triggers = [
        line_trigger(name, line="trigger-test", schedule=CadenceSchedule(interval_seconds=value))
        for name, value in seconds.items()
    ]
    instance, line, _accepted = make_line_world(
        tmp_path_factory.mktemp("cadence"), None, triggers=triggers
    )
    return instance, line


def _pages(instance: Any, **fields: Any) -> list[dict[str, Any]]:
    from datetime import UTC, datetime

    when = datetime(2026, 9, 1, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = _query(instance, when, limit=2, cursor=cursor, **fields)
        rows.extend(page.rows)
        if page.next_cursor is None:
            return rows
        cursor = page.next_cursor


def _sorted_typed(rows: list[dict[str, Any]], *, descending: bool) -> bool:
    """Cadences ordered as numbers, nulls last, ties by name: the declared order."""

    valued = [row for row in rows if row["cadence"] is not None]
    nulls = [row for row in rows if row["cadence"] is None]
    expected = sorted(valued, key=lambda row: row["name"])
    expected = sorted(expected, key=lambda row: row["cadence"], reverse=descending)
    return rows == [*expected, *sorted(nulls, key=lambda row: row["name"])]


def test_trigger_cadences_order_as_numbers_across_pages(cadence_world) -> None:  # type: ignore[no-untyped-def]
    instance, _line = cadence_world

    ascending = _pages(instance, kind="Trigger", select=["cadence"], order_by=["cadence"])
    ours = [row["name"] for row in ascending if row["name"].startswith("t-")]
    assert ours == ["t-b", "t-d", "t-a", "t-e", "t-c"]
    # The seeded floor-refresh Trigger has no cadence: nulls sort last.
    assert ascending[-1] == {"name": "floor-refresh", "cadence": None}
    assert _sorted_typed(ascending, descending=False)

    descending = _pages(instance, kind="Trigger", select=["cadence"], order_by=["-cadence"])
    ours = [row["name"] for row in descending if row["name"].startswith("t-")]
    assert ours == ["t-c", "t-a", "t-e", "t-d", "t-b"]
    assert descending[-1]["cadence"] is None
    assert _sorted_typed(descending, descending=True)


def test_a_listing_past_its_ceiling_is_capped_and_says_so(  # type: ignore[no-untyped-def]
    cadence_world, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from cruxible_core.service.discovery import listed_kinds

    instance, line = cadence_world
    monkeypatch.setattr(listed_kinds, "LISTING_MAX_RESULTS", 3)
    when = datetime(2026, 9, 1, tzinfo=UTC)

    page = _query(
        instance, when, kind="Trigger", where=[{"field": "target", "eq": line.identity.qualified}]
    )

    assert [row["name"] for row in page.rows] == ["t-a", "t-b", "t-c"]
    assert page.capped == ("max_results=3",)
    assert page.truncated
    assert any("server cap max_results=3" in note for note in page.notes)
    # Under the ceiling nothing is capped.
    narrow = _query(instance, when, kind="Trigger", where=[{"field": "name", "in": ["t-a", "t-b"]}])
    assert narrow.capped == () and not narrow.truncated


def test_a_line_lists_a_bounded_page_of_its_triggers_with_their_total(  # type: ignore[no-untyped-def]
    cadence_world, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_client.contracts.operational_reads import GetLineCard
    from cruxible_core.service.discovery import listed_kinds, operational
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext

    instance, line = cadence_world
    monkeypatch.setattr(listed_kinds, "LINE_TRIGGER_NAMES_MAX", 2)
    monkeypatch.setattr(operational, "OPERATIONAL_CARD_LIST_LIMIT", 2)
    when = datetime(2026, 9, 1, tzinfo=UTC)

    (row,) = _query(instance, when, kind="Line").rows
    assert row["triggers"] == ["Trigger:t-a", "Trigger:t-b"]
    assert row["triggers_total"] == 5

    card = service_playbill_get(
        instance,
        request=GetRequest(ref=line.identity.qualified, evaluation_time=when),
        access=BodyAccessContext(principal_id="reader", can_read_body=False),
    ).card
    assert isinstance(card, GetLineCard)
    assert [item.trigger for item in card.triggers] == ["Trigger:t-a", "Trigger:t-b"]
    assert card.triggers_total == 5
    assert card.trigger == "cadence"
