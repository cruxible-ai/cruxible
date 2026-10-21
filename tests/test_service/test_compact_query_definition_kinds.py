"""``query`` answers every definition kind agents ask the ontology about.

``query kind=SubjectKind`` and ``kind=CaptureContract`` list those
definitions with the same compact grammar as the other definition kinds
(``where``, ``select``, ``order_by``, ``contains``, paging): each row reads its
own artifact and what the ontology holds about it, never a scan over Claims,
Subjects or Captures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_core.indexes.query_selection import LIVE_SUBJECT_COUNT_SQL
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._write_support import KIND, REPORTS, seed_write_surface
from tests.test_service.test_write_verbs import _move_to_v7


@pytest.fixture(scope="module")
def instance(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    seeded = seed_write_surface(Path(tmp_path_factory.mktemp("definition-kinds")))[0]
    # measured admits only repo.reports and now requires a Capture under it.
    _move_to_v7(seeded, "measured", evidence_requirement="captured")
    return seeded


def _query(instance: Any, **fields: Any) -> Any:
    return service_playbill_query(instance, request=QueryRequest.model_validate(fields))


def _sql(instance: PlaybillInstance, sql: str, args: tuple[object, ...] = ()) -> list[Any]:
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        return [tuple(row) for row in projection.typed.connection.execute(sql, args)]


# -- SubjectKind ---------------------------------------------------------------------


def test_subject_kinds_list_their_identity_rule_and_live_subjects(instance: Any) -> None:
    result = _query(instance, kind="SubjectKind")

    assert result.kind == "SubjectKind"
    assert [column.name for column in result.columns] == [
        "kind",
        "rule",
        "keys",
        "uniqueness",
        "description",
        "subjects",
        "kit",
        "lifecycle",
    ]
    by_kind = {row["kind"]: row for row in result.rows}
    row = by_kind[KIND]
    assert row["rule"] == "keys" and row["keys"] == [f"{KIND}.slug"]
    assert row["uniqueness"] in {"unique", "hint"}
    # The count is the maintained counter's, which Subject writes keep.
    ((live,),) = _sql(instance, LIVE_SUBJECT_COUNT_SQL, (KIND,))
    assert row["subjects"] == live >= 3
    assert row["kit"] is None and row["lifecycle"] == "live"


def test_subject_kinds_filter_select_and_order_on_their_columns(instance: Any) -> None:
    every = _query(instance, kind="SubjectKind").rows

    populated = _query(instance, kind="SubjectKind", where=[{"field": "subjects", "gt": "0"}])
    assert {row["kind"] for row in populated.rows} == {
        row["kind"] for row in every if row["subjects"] > 0
    }
    keyed = _query(
        instance,
        kind="SubjectKind",
        where=[{"field": "kind", "eq": KIND}, {"field": "rule", "eq": "keys"}],
        select=["keys", "version"],
    )
    (row,) = keyed.rows
    assert set(row) == {"kind", "keys", "version"} and row["version"] >= 1
    assert _query(instance, kind="SubjectKind", contains="slug").rows
    ordered = _query(instance, kind="SubjectKind", order_by=["-subjects"]).rows
    assert [item["subjects"] for item in ordered] == sorted(
        (item["subjects"] for item in every), reverse=True
    )
    # Only live rows unless a lifecycle filter asks for others.
    assert all(item["lifecycle"] == "live" for item in every)
    assert (
        _query(instance, kind="SubjectKind", where=[{"field": "lifecycle", "eq": "retired"}]).rows
        == ()
    )


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"kind": "SubjectKind", "where": [{"field": "subjects", "eq": "many"}]}, "value_type"),
        ({"kind": "SubjectKind", "where": [{"field": "rule", "lt": "keys"}]}, "operator_not"),
        ({"kind": "SubjectKind", "where": [{"field": "rule", "eq": "named"}]}, "value_type"),
        ({"kind": "SubjectKind", "where": [{"field": "version", "eq": "1"}]}, "unknown_field"),
        ({"kind": "SubjectKind", "select": ["count"]}, "unknown_field"),
        ({"kind": "SubjectKind", "order_by": ["size"]}, "unknown_field"),
        (
            {"kind": "SubjectKind", "follow": [{"field": "keys", "as": "k"}]},
            "follow_not_relation",
        ),
    ],
)
def test_a_subject_kind_listing_refuses_what_its_rows_cannot_answer(
    instance: Any, fields: dict[str, Any], code: str
) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, **fields)
    assert code in refused.value.error_code


def test_a_misspelled_definition_kind_suggests_it(instance: Any) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, kind="SubjectKnd")
    assert refused.value.error_code == "cruxible.query.unknown_kind"
    assert "SubjectKind" in refused.value.candidates


# -- CaptureContract -----------------------------------------------------------------


def test_capture_contracts_list_what_they_capture_and_who_relies_on_them(instance: Any) -> None:
    result = _query(instance, kind="CaptureContract")

    assert [column.name for column in result.columns] == [
        "name",
        "version",
        "lifecycle",
        "sources",
        "source_kinds",
        "evidence_kinds",
        "grade",
        "admitted_by",
        "required_by",
    ]
    by_name = {row["name"]: row for row in result.rows}
    reports = by_name[REPORTS.identity.name]
    assert reports["sources"] == list(REPORTS.logical_source_identities)
    assert reports["source_kinds"] == list(REPORTS.allowed_source_kinds)
    assert reports["evidence_kinds"] == list(REPORTS.evidence_kinds)
    assert reports["grade"] == REPORTS.epistemic_grade
    # labels and measured admit it; only measured (v7, captured) requires a Capture.
    assert reports["admitted_by"] == 2 and reports["required_by"] == 1
    # The same counts and version orient's capture_contracts section serves.
    orient = service_playbill_orient(instance, section="capture_contracts")
    served = {
        row.contract.removeprefix("CaptureContract:"): row
        for row in orient.capture_contracts or ()
        if row.lifecycle == "live"
    }
    assert set(served) == set(by_name)
    for name, row in by_name.items():
        assert (row["admitted_by"], row["version"]) == (
            served[name].admitted_by,
            served[name].version,
        ), name


def test_capture_contracts_filter_on_grade_reliance_and_source_kinds(instance: Any) -> None:
    every = _query(instance, kind="CaptureContract").rows

    required = _query(instance, kind="CaptureContract", where=[{"field": "required_by", "gte": 1}])
    assert [row["name"] for row in required.rows] == [REPORTS.identity.name]
    grade = REPORTS.epistemic_grade
    graded = _query(instance, kind="CaptureContract", where=[{"field": "grade", "eq": grade}])
    assert {row["name"] for row in graded.rows} == {
        row["name"] for row in every if row["grade"] == grade
    }
    (kind,) = REPORTS.allowed_source_kinds[:1]
    sourced = _query(
        instance,
        kind="CaptureContract",
        where=[{"field": "source_kinds", "eq": kind}],
        select=["source_kinds"],
    )
    assert sourced.rows and all(kind in row["source_kinds"] for row in sourced.rows)
    assert set(sourced.rows[0]) == {"name", "source_kinds"}
    ordered = _query(instance, kind="CaptureContract", order_by=["-admitted_by", "name"]).rows
    assert [row["admitted_by"] for row in ordered] == sorted(
        (row["admitted_by"] for row in every), reverse=True
    )
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, kind="CaptureContract", where=[{"field": "grade", "eq": "measured"}])
    assert refused.value.error_code == "cruxible.query.value_type_mismatch"
