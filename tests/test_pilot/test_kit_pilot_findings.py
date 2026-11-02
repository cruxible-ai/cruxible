"""Kit-pilot core findings on the pilot's own KEV slice.

F1 (D-R29): a date-valued source field reaches a Claim's datetime
``effective_from`` only through the declared extract projection
``utc_start_of_day``. F2: a Claims typing refusal names the authored step, the
template and the slot it was typed against, and the Procedure that owns it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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


def test_kev_listed_effective_from_an_unprojected_date_is_refused_naming_the_slot(
    tmp_path: Path,
) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    spec, rows = _kev_listed_from("$item.fields.date_added")
    with pytest.raises(AuthoringLoweringError) as refused:
        pilot_world(tmp_path, "kev.json", spec=spec, rows=rows)
    assert refused.value.code == "cruxible.authoring.claims_type_invalid"
    assert refused.value.offending_element == "$.steps[3].claims[0].effective_from"
    assert "Procedure security.kev-triage" in refused.value.message
    assert "the Claim's effective_from (datetime)" in refused.value.message
    assert "ClaimType.value" not in refused.value.message
    assert "project: utc_start_of_day" in refused.value.message


def _renamed(spec: Any, name: str) -> Any:
    data = spec.model_dump(mode="python", by_alias=True)
    data["procedure"] = name
    return procedure_spec(base=data, reference_text=True)


def _change_set(*specs: Any) -> Any:
    from cruxible_client.contracts.authoring.inputs import ChangeSetInput

    return ChangeSetInput.model_validate(
        {
            "kind": "change_set",
            "members": [{"kind": "procedure", "spec": spec} for spec in specs],
            "rationale": "Two KEV Procedures in one set.",
        }
    )


def test_a_change_set_claims_refusal_names_its_procedure_step_and_slot(tmp_path: Path) -> None:
    """The refused member is named; its index is the set's canonical (identity) order."""

    from cruxible_core.authoring.lowering import AuthoringLoweringError
    from tests.support.operation_world import author

    bad, rows = _kev_listed_from("$item.fields.date_added")
    good, _rows = _kev_listed_from("$item.fields.listed_from")
    instance, _root = pilot_world(tmp_path, "kev.json", spec=good, rows=rows)
    # Authored first, the bad member sorts second by identity (kev-triage > kev-a).
    payload = _change_set(bad, _renamed(good, "security.kev-a"))
    with pytest.raises(AuthoringLoweringError) as refused:
        author(instance, payload, name="two-procedures")
    assert refused.value.code == "cruxible.authoring.claims_type_invalid"
    assert refused.value.offending_element == "members[1].steps[3].claims[0].effective_from"
    assert refused.value.message.startswith("Procedure security.kev-triage: claims[0] ")
    assert "the Claim's effective_from (datetime)" in refused.value.message
