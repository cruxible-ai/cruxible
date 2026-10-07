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
