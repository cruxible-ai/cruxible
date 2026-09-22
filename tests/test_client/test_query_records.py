"""Named projection access preserves the retained query wire and ambiguity."""

import pytest

from cruxible_client.contracts.query.results import QueryResultRowV1


def test_named_projection_preserves_absence_conflict_and_wire():
    raw = {
        "bindings": [],
        "fields": [
            {"name": "count", "state": "present", "value": 2},
            {"name": "priority", "state": "present", "value": "urgent"},
            {"name": "owner", "state": "absent"},
            {"name": "status", "state": "conflict"},
        ],
    }
    row = QueryResultRowV1.model_validate(raw)
    assert row.fields.count.value == 2
    assert row.fields.priority.value == "urgent"
    assert row.fields.owner.state == "absent"
    assert row.fields.status.state == "conflict"
    with pytest.raises(AttributeError, match="no projected field"):
        _ = row.fields.typographical_error
    wire = row.model_dump(mode="json")
    assert isinstance(wire["fields"], list)
    assert QueryResultRowV1.model_validate(wire) == row
