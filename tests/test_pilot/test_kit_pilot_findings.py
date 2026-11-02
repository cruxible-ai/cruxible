"""Kit-pilot core findings on the pilot's own KEV slice.

F1 (D-R29): a date-valued source field reaches a Claim's datetime
``effective_from`` only through the declared extract projection
``utc_start_of_day``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.support.procedures import procedure_spec
from tests.test_pilot.support import KEV, kev_spec, pilot_world
from tests.test_procedures.test_claims_terminal import candidate_claims, run


def _kev_listed_from(effective_from: str) -> tuple[Any, Any]:
    """The KEV slice with ``listed`` effective from its record's ``dateAdded`` (D-R29)."""

    from tests.support.procedures import boundary_contract
    from tests.test_pilot.support import KEV_FIELDS

    rows = boundary_contract("security.kev-entry", {**KEV_FIELDS, "listed_from": "datetime?"})
    data = kev_spec().model_dump(mode="python", by_alias=True)
    extract, claims = data["steps"][2], data["steps"][3]
    extract["row"] = rows.ref.spelling
    extract["fields"]["listed_from"] = {
        "at": "dateAdded",
        "parse": "date",
        "project": "utc_start_of_day",
    }
    (listed,) = (c for c in claims["claims"] if c["type"] == KEV + "listed")
    listed["effective_from"] = effective_from
    return procedure_spec(base=data, reference_text=True), rows


def test_kev_listed_is_effective_from_date_added_projected_to_utc_start_of_day(
    tmp_path: Path,
) -> None:
    spec, rows = _kev_listed_from("$item.fields.listed_from")
    instance, root = pilot_world(tmp_path, "kev.json", spec=spec, rows=rows)
    state = run(instance, root)
    listed = {
        c.statement.subject.artifact_path: c.statement
        for c in candidate_claims(instance, state)
        if c.statement.predicate == KEV + "listed"
    }
    assert len(listed) == 2
    by_day = sorted((s.effective_from.isoformat(), s.object.value) for s in listed.values())
    assert by_day == [("2026-10-01T00:00:00+00:00", True), ("2026-10-02T00:00:00+00:00", True)]
