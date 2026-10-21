"""``query`` answers every definition kind agents ask the ontology about.

``query kind=SubjectKind``, ``kind=CaptureContract``, ``kind=ProcedureMandate``
and ``kind=QueryDefinition`` list those definitions with the same compact
grammar as the other definition kinds (``where``, ``select``, ``order_by``,
``contains``, paging): each row reads its own artifact and what the ontology
holds about it, never a scan over Claims, Subjects or Captures. ``kind=ClaimType``
rows gain the role a write takes, freshness, contention, whether a Capture is
required and the owning kit under ``select``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.claim_types import ClaimEvidenceFreshness, ClaimFreshnessDuration
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_core.indexes.query_selection import LIVE_SUBJECT_COUNT_SQL
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._write_support import KIND, REPORTS, seed_write_surface
from tests.test_proposals.test_delegated_settlement import STATUS
from tests.test_proposals.test_delegated_settlement import _world as settle_world
from tests.test_service.test_write_verbs import _move_to_v7


@pytest.fixture(scope="module")
def instance(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    seeded = seed_write_surface(Path(tmp_path_factory.mktemp("definition-kinds")))[0]
    # measured admits only repo.reports and now requires a Capture under it.
    _move_to_v7(seeded, "measured", evidence_requirement="captured")
    # priority permits two roles; v7 names normative as the default and a horizon.
    _move_to_v7(
        seeded,
        "priority",
        default_role="normative",
        evidence_freshness=ClaimEvidenceFreshness(
            stale_after=ClaimFreshnessDuration(microseconds=30 * 86_400_000_000)
        ),
    )
    return seeded


@pytest.fixture(scope="module")
def settling(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    """A settle mandate (triage) scoped to the status ClaimType under a condition query."""

    return settle_world(Path(tmp_path_factory.mktemp("definition-kinds-settle")))[0]


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


# -- ProcedureMandate ----------------------------------------------------------------

_WHEN = datetime(2026, 6, 1, tzinfo=UTC)


def test_mandates_list_their_grant_scope_condition_and_expiry(settling: Any) -> None:
    result = _query(settling, kind="ProcedureMandate", evaluation_time=_WHEN)

    assert [column.name for column in result.columns] == [
        "name",
        "procedure",
        "grant",
        "state",
        "scope",
        "change_kinds",
        "condition",
        "suspended",
        "lifecycle",
        "expires_at",
    ]
    (row,) = (item for item in result.rows if item["name"] == "triage")
    assert row["procedure"].startswith("Procedure:")
    assert (row["grant"], row["state"]) == ("settle", "active")
    assert row["scope"] == [f"ClaimType:{STATUS}"]
    assert row["change_kinds"] == ["create", "revise"]
    assert row["condition"] == "QueryDefinition:project.settle-condition"
    assert row["suspended"] is False and row["lifecycle"] == "live"
    assert row["expires_at"] == "2027-01-01T00:00:00Z"


def test_mandates_filter_on_grant_scope_and_expiry(settling: Any) -> None:
    def names(**fields: Any) -> list[str]:
        rows = _query(settling, kind="ProcedureMandate", evaluation_time=_WHEN, **fields).rows
        return [row["name"] for row in rows]

    assert "triage" in names(where=[{"field": "grant", "eq": "settle"}])
    assert "triage" not in names(where=[{"field": "grant", "eq": "propose"}])
    assert "triage" in names(where=[{"field": "scope", "eq": f"ClaimType:{STATUS}"}])
    assert "triage" in names(where=[{"field": "change_kinds", "eq": "revise"}])
    assert "triage" not in names(where=[{"field": "change_kinds", "eq": "retire"}])
    assert "triage" in names(where=[{"field": "suspended", "eq": "false"}])
    assert "triage" in names(where=[{"field": "expires_at", "lt": "2027-01-01T00:00:01Z"}])
    assert "triage" not in names(where=[{"field": "expires_at", "gte": "2027-06-01T00:00:00Z"}])
    # The same instant in another offset is the same expiry.
    assert "triage" in names(where=[{"field": "expires_at", "eq": "2027-01-01T01:00:00+01:00"}])
    assert "triage" in names(where=[{"field": "state", "eq": "active"}])
    late = _query(
        settling,
        kind="ProcedureMandate",
        evaluation_time=datetime(2026, 12, 28, tzinfo=UTC),
        select=["state", "version"],
    )
    (row,) = (item for item in late.rows if item["name"] == "triage")
    assert row == {"name": "triage", "state": "expiring", "version": 1}
    with pytest.raises(ReadRefusalError) as refused:
        names(where=[{"field": "expires_at", "lt": "next year"}])
    assert refused.value.error_code == "cruxible.query.value_type_mismatch"
    with pytest.raises(ReadRefusalError) as wrong:
        names(where=[{"field": "expires_at", "contains": "2027"}])
    assert wrong.value.error_code == "cruxible.query.operator_not_applicable"


# -- QueryDefinition -----------------------------------------------------------------


def test_query_definitions_list_their_parameters_and_result_shape(settling: Any) -> None:
    result = _query(settling, kind="QueryDefinition")

    assert [column.name for column in result.columns] == [
        "name",
        "parameters",
        "description",
        "result_shape",
        "lifecycle",
    ]
    (row,) = (item for item in result.rows if item["name"] == "project.settle-condition")
    assert row == {
        "name": "project.settle-condition",
        "parameters": ["item_id: string"],
        "description": None,
        "result_shape": "subject",
        "lifecycle": "live",
    }
    named = _query(
        settling,
        kind="QueryDefinition",
        where=[{"field": "name", "in": ["project.settle-condition"]}],
        select=["parameters", "version"],
    )
    assert named.rows == (
        {"name": "project.settle-condition", "parameters": ["item_id: string"], "version": 1},
    )
    assert _query(
        settling, kind="QueryDefinition", where=[{"field": "parameters", "contains": "item_id"}]
    ).rows
    assert not _query(
        settling, kind="QueryDefinition", where=[{"field": "result_shape", "eq": "path"}]
    ).rows
    with pytest.raises(ReadRefusalError) as refused:
        _query(settling, kind="QueryDefinition", where=[{"field": "result_shape", "eq": "rows"}])
    assert refused.value.error_code == "cruxible.query.value_type_mismatch"


# -- ClaimType columns ---------------------------------------------------------------

_DETAIL = ["role", "roles", "stale_after", "selector", "on_conflict", "captured_required", "kit"]


def test_claim_type_rows_select_role_freshness_contention_capture_and_kit(instance: Any) -> None:
    plain = _query(instance, kind="ClaimType", where=[{"field": "namespace", "eq": KIND}])
    # The default columns are unchanged; the new ones come by select.
    assert [column.name for column in plain.columns] == [
        "predicate",
        "subject_kinds",
        "object",
        "cardinality",
        "members",
        "description",
        "evidence",
    ]
    assert not set(_DETAIL) & {name for row in plain.rows for name in row}

    result = _query(
        instance,
        kind="ClaimType",
        where=[{"field": "namespace", "eq": KIND}],
        select=["predicate", *_DETAIL],
    )
    assert [column.name for column in result.columns] == ["predicate", *_DETAIL]
    rows = {row["predicate"]: row for row in result.rows}
    priority = rows[f"{KIND}.priority"]
    assert priority["role"] == "normative"
    assert priority["roles"] == ["normative", "observation"]
    assert priority["stale_after"] == "30d"
    assert (priority["selector"], priority["on_conflict"]) == ("only_contender", "unresolved")
    assert priority["captured_required"] is False
    # A sole permitted role is the implied one; no kit owns the seeded vocabulary.
    assert rows[f"{KIND}.title"]["role"] == "observation"
    assert "stale_after" not in rows[f"{KIND}.title"] and "kit" not in priority
    assert rows[f"{KIND}.measured"]["captured_required"] is True
    assert rows[f"{KIND}.labels"]["selector"] == "all"


def test_claim_types_filter_on_the_new_columns(instance: Any) -> None:
    def predicates(*where: dict[str, Any]) -> set[str]:
        rows = _query(
            instance,
            kind="ClaimType",
            where=[{"field": "namespace", "eq": KIND}, *where],
            select=["predicate"],
        ).rows
        return {row["predicate"] for row in rows}

    assert predicates({"field": "captured_required", "eq": "true"}) == {f"{KIND}.measured"}
    assert f"{KIND}.measured" not in predicates({"field": "captured_required", "eq": False})
    assert f"{KIND}.priority" in predicates({"field": "role", "eq": "normative"})
    assert f"{KIND}.labels" in predicates({"field": "selector", "eq": "all"})
    assert predicates({"field": "kit", "eq": "acme"}) == set()
    with pytest.raises(ReadRefusalError) as refused:
        predicates({"field": "captured_required", "eq": "yes"})
    assert refused.value.error_code == "cruxible.query.value_type_mismatch"
    with pytest.raises(ReadRefusalError) as wrong:
        predicates({"field": "captured_required", "in": ["true"]})
    assert wrong.value.error_code == "cruxible.query.operator_not_applicable"


def test_a_selected_definition_row_keeps_its_key_and_takes_the_where_names(
    instance: Any,
) -> None:
    from cruxible_client.authoring.compact_query import render_query_table

    selected = _query(
        instance,
        kind="ClaimType",
        where=[{"field": "name", "eq": f"{KIND}.priority"}],
        select=["name", "roles"],
        order_by=["name"],
    )
    # `name` filters ClaimTypes, so it selects and orders their predicate column,
    # and the predicate leads even when select leaves it out.
    assert [column.name for column in selected.columns] == ["predicate", "roles"]
    assert [row["predicate"] for row in selected.rows] == [f"{KIND}.priority"]
    unnamed = _query(instance, kind="ClaimType", select=["roles"])
    assert [column.name for column in unnamed.columns] == ["predicate", "roles"]
    contracts = _query(instance, kind="CaptureContract", select=["grade"])
    assert [column.name for column in contracts.columns] == ["name", "grade"]
    # A table names reserved contracts by their public names; typed rows keep them.
    stored = REPORTS.identity.name
    assert stored.startswith("playbill.foreign-source.")
    assert stored in {row["name"] for row in contracts.rows}
    table = render_query_table(contracts)
    assert "cruxible.foreign-source.repo.reports" in table
    assert stored not in table
