"""Ordered enums: ``ordered: true`` ranks an EnumType's members, lowest first.

Values stay stored as their member strings; queries compare (``lt``, ``lte``,
``gt``, ``gte``) and order them by declared rank. Ordering an unordered enum,
comparing two different enums, or naming a non-member refuses. Unordered enums
keep their canonical bytes, so no existing digest moves.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_digest
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionSpec,
    enum_comparison_problem,
)
from cruxible_client.contracts.query.grammar import (
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryLiteralRef,
    QueryParameterRef,
    QuerySubjectFieldRef,
)
from cruxible_client.contracts.shapes import (
    Contract,
    EnumType,
    StringType,
    contract_digest,
    declares_ordered_enum,
    edge_refusal,
    ordered_enum,
    parse_type,
    render_type,
)
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    SUBJECT_KIND,
    seed_claims,
)
from tests.support.claim_types import claim_type

WHEN = datetime.fromisoformat(EVALUATION_TIME)
PRIORITY = ("low", "medium", "high", "critical")


# -- the type ------------------------------------------------------------------------


def test_an_unordered_enum_keeps_its_canonical_bytes_and_every_digest() -> None:
    plain = EnumType(members=("a", "b"))
    assert plain.ordered is False
    assert canonical_bytes(plain.model_dump(mode="json")) == b'{"kind":"enum","members":["a","b"]}'
    assert EnumType.model_validate({"kind": "enum", "members": ["a", "b"], "ordered": False}) == (
        plain
    )
    # Pinned from the release before ordered enums existed (45fa94dd7).
    unordered = claim_type(
        "test.ticket.priority", value_type={"kind": "enum", "members": list(PRIORITY)}
    )
    assert claim_type_digest(unordered).tagged == (
        "sha256:9f9bf90b90a233cc8d2b7ecdc18edf868812de212f030b0a07f912182e494180"
    )
    row = Contract.model_validate(
        {"name": "test.row", "version": 1, "fields": {"p": "enum[low, high]"}}
    )
    assert contract_digest(row).tagged == (
        "sha256:4e23a6b6f99c00709cd6bcbfae2acce69e293c7348fa62c3a77afe6ea5155e4f"
    )
    ordered = claim_type(
        "test.ticket.priority",
        value_type={"kind": "enum", "members": list(PRIORITY), "ordered": True},
    )
    assert claim_type_digest(ordered) != claim_type_digest(unordered)
    assert ordered.value.model_dump(mode="json")["ordered"] is True


def test_rank_is_the_declared_index_lowest_first() -> None:
    priority = EnumType(members=PRIORITY, ordered=True)
    assert [priority.rank(member) for member in PRIORITY] == [0, 1, 2, 3]
    assert priority.rank("urgent") is None and priority.rank(3) is None
    assert ordered_enum(priority) is priority
    assert ordered_enum(EnumType(members=PRIORITY)) is None


def test_the_spelling_reads_and_renders_both_forms() -> None:
    assert parse_type("enum[low, high]") == EnumType(members=("low", "high"))
    assert render_type(EnumType(members=("low", "high"))) == 'enum["low", "high"]'
    ranked = parse_type("ordered_enum[low, medium, high]")
    assert ranked == EnumType(members=("low", "medium", "high"), ordered=True)
    assert render_type(ranked) == 'ordered_enum["low", "medium", "high"]'
    assert parse_type(render_type(ranked)) == ranked
    with pytest.raises(ValueError, match="quote a member"):
        parse_type("ordered_enum[0.5.0, next]")


def test_declares_ordered_enum_reads_lists_but_not_unordered_values() -> None:
    assert declares_ordered_enum(parse_type("list<ordered_enum[a, b]>{max_items: 3}"))
    assert not declares_ordered_enum(parse_type("list<enum[a, b]>{max_items: 3}"))
    assert not declares_ordered_enum(StringType())


def test_an_edge_between_ordered_enums_keeps_the_rank_order() -> None:
    low_high = EnumType(members=("low", "high"), ordered=True)
    widened = EnumType(members=("low", "medium", "high"), ordered=True)
    reversed_ = EnumType(members=("high", "low"), ordered=True)
    assert edge_refusal(low_high, widened) is None
    assert edge_refusal(low_high, reversed_) == (
        "the destination enum ranks the source's members in another order"
    )
    # Into or out of an unordered enum, membership alone decides.
    assert edge_refusal(EnumType(members=("high", "low")), low_high) is None
    assert edge_refusal(reversed_, EnumType(members=("low", "high"))) is None
    assert edge_refusal(widened, low_high) == "the source enum has members the destination does not"


def test_a_procedure_predicate_refuses_to_order_an_ordered_enum() -> None:
    from cruxible_client.contracts.procedures.authored import (
        ProcedureSpecError,
        _check_predicate,
    )
    from cruxible_client.contracts.shapes import ContractField, as_predicate

    fields = {
        "ranked": ContractField(type=EnumType(members=PRIORITY, ordered=True)),
        "plain": ContractField(type=EnumType(members=PRIORITY)),
    }

    def lookup(ref: Any, _path: str) -> ContractField:
        return fields[ref.path[-1]]

    def predicate(op: str, field: str) -> Any:
        return as_predicate(
            {
                "op": op,
                "left": {"$ref": "item", "path": ["fields", field]},
                "right": {"literal": "high"},
            }
        )

    with pytest.raises(ProcedureSpecError, match="compares by rank only in a query"):
        _check_predicate(predicate("gt", "ranked"), "steps[0]", lookup)
    _check_predicate(predicate("eq", "ranked"), "steps[0]", lookup)
    _check_predicate(predicate("gt", "plain"), "steps[0]", lookup)


def test_a_structured_ordered_flag_must_be_a_boolean() -> None:
    with pytest.raises(ValueError):
        EnumType.model_validate({"kind": "enum", "members": ["a"], "ordered": "yes"})


# -- the shared comparison rule ------------------------------------------------------


_ENUMS = {
    "t.item.priority": EnumType(members=PRIORITY, ordered=True),
    "t.item.urgency": EnumType(members=PRIORITY, ordered=True),
    "t.item.status": EnumType(members=("open", "closed")),
}


def _compare(left: Any, operator: str, right: Any, value_type: str = "string") -> str | None:
    return enum_comparison_problem(
        QueryComparisonFilter(left=left, operator=operator, right=right, value_type=value_type),  # type: ignore[arg-type]
        _ENUMS.get,
    )


def _value(predicate: str, binding: str = "item") -> QueryClaimValueRef:
    return QueryClaimValueRef(binding=binding, predicate=predicate)


@pytest.mark.parametrize("operator", ["lt", "lte", "gt", "gte"])
def test_an_ordered_enum_orders_against_a_member_a_parameter_or_itself(operator: str) -> None:
    priority = _value("t.item.priority")
    assert _compare(priority, operator, QueryLiteralRef(value="high")) is None
    assert _compare(QueryLiteralRef(value="low"), operator, priority) is None
    assert _compare(priority, operator, QueryParameterRef(parameter="floor")) is None
    assert _compare(priority, operator, _value("t.item.priority", "other")) is None


def test_ordering_an_unordered_enum_refuses_and_equality_never_does() -> None:
    status = _value("t.item.status")
    problem = _compare(status, "gt", QueryLiteralRef(value="open"))
    assert problem is not None and "unordered enum" in problem
    assert _compare(status, "eq", QueryLiteralRef(value="open")) is None
    assert _compare(status, "ne", QueryLiteralRef(value="nope")) is None


def test_two_different_enums_never_order_against_each_other() -> None:
    problem = _compare(_value("t.item.priority"), "lt", _value("t.item.urgency"))
    assert problem is not None and "different enums" in problem
    # Another predicate's value that is not an enum is not a member either.
    problem = _compare(_value("t.item.priority"), "lt", _value("t.item.title"))
    assert problem is not None and "only with a member" in problem


def test_a_non_member_literal_or_another_operand_refuses() -> None:
    priority = _value("t.item.priority")
    problem = _compare(priority, "gte", QueryLiteralRef(value="urgent"))
    assert problem is not None and "not a member of t.item.priority" in problem
    assert "low, medium, high, critical" in problem
    subject_id = QuerySubjectFieldRef(binding="item", field="subject_id")
    assert _compare(priority, "gt", subject_id) is not None
    problem = _compare(priority, "gt", QueryLiteralRef(value="high"), value_type="integer")
    assert problem is not None and "value_type string" in problem


# -- served queries --------------------------------------------------------------------


#: Ranked so text order disagrees: ``ready`` (0) < ``blocked`` (1) < ``done`` (2),
#: while text sorts blocked < done < ready. wi-42 is ready, wi-43 blocked.
RANKED = EnumType(members=("ready", "blocked", "done"), ordered=True)


def _status_type(value: EnumType) -> Any:
    from tests.test_claims.test_claims import _claim_type

    return _claim_type().model_copy(update={"value": value})


@pytest.fixture(scope="module")
def ranked(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from tests.core_support._candidate_support import submit_query_definition_candidate
    from tests.core_support._knowledge_loop_support import TIMESTAMP, accept_proposal

    instance, owner = seed_claims(
        tmp_path_factory.mktemp("ordered-enum"), claim_type_override=_status_type(RANKED)
    )
    # A named query comparing with a caller parameter: params bind named queries only.
    parameters = [{"name": "floor", "value_type": "string", "required": True}]
    query = QueryDefinitionSpec.model_validate(
        _definition("gte", {"kind": "parameter", "parameter": "floor"}, parameters=parameters)
    )
    inspection = submit_query_definition_candidate(
        instance,
        query=query,
        actor_id="owner",
        proposal_name="ordered-floor",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)
    return instance


@pytest.fixture(scope="module")
def unranked(tmp_path_factory: pytest.TempPathFactory) -> Any:
    instance, _owner = seed_claims(tmp_path_factory.mktemp("unordered-enum"))
    return instance


def _query(instance: Any, **fields: Any) -> Any:
    fields.setdefault("evaluation_time", WHEN)
    return service_playbill_query(instance, request=QueryRequest.model_validate(fields))


def _ids(result: Any) -> list[str]:
    return [row["subject_id"] for row in result.rows]


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("gt", "ready", ["wi-43"]),
        ("gte", "ready", ["wi-42", "wi-43"]),
        ("lt", "blocked", ["wi-42"]),
        ("lte", "blocked", ["wi-42", "wi-43"]),
        ("gte", "done", []),
        ("lt", "ready", []),
    ],
)
def test_a_compact_comparison_compares_by_declared_rank(
    ranked: Any, operator: str, value: str, expected: list[str]
) -> None:
    result = _query(ranked, kind=SUBJECT_KIND, where=[{"field": "status", operator: value}])
    assert _ids(result) == expected


def test_order_by_an_ordered_enum_sorts_by_rank_in_both_directions(ranked: Any) -> None:
    assert _ids(_query(ranked, kind=SUBJECT_KIND, order_by=["status"])) == ["wi-42", "wi-43"]
    assert _ids(_query(ranked, kind=SUBJECT_KIND, order_by=["-status"])) == ["wi-43", "wi-42"]


def test_order_by_an_unordered_enum_keeps_text_order(unranked: Any) -> None:
    assert _ids(_query(unranked, kind=SUBJECT_KIND, order_by=["status"])) == ["wi-43", "wi-42"]


def test_the_column_and_catalog_say_ordered(ranked: Any, unranked: Any) -> None:
    result = _query(ranked, kind=SUBJECT_KIND, select=["status"])
    assert result.columns[0].type == "ordered_enum"
    assert result.columns[0].members == RANKED.members
    plain = _query(unranked, kind=SUBJECT_KIND, select=["status"])
    assert plain.columns[0].type == "enum"
    (row,) = [r for r in _query(ranked, kind="ClaimType").rows if r["predicate"] == PREDICATE]
    assert row["object"] == "ordered_enum" and row["members"] == list(RANKED.members)
    (row,) = [r for r in _query(unranked, kind="ClaimType").rows if r["predicate"] == PREDICATE]
    assert row["object"] == "enum"


def test_the_orient_type_spells_the_ordering(ranked: Any) -> None:
    from cruxible_core.service.discovery.orient import _value_type

    assert _value_type(_status_type(RANKED)) == (
        'ordered_enum["ready", "blocked", "done"]',
        RANKED.members,
    )


@pytest.mark.parametrize(
    ("instance_name", "where", "code"),
    [
        ("unranked", {"field": "status", "lt": "ready"}, "cruxible.query.operator_not_applicable"),
        ("ranked", {"field": "status", "gt": "urgent"}, "cruxible.query.unknown_member"),
    ],
)
def test_compact_refusals_name_the_repair(
    request: pytest.FixtureRequest, instance_name: str, where: dict[str, Any], code: str
) -> None:
    instance = request.getfixturevalue(instance_name)
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, kind=SUBJECT_KIND, where=[where])
    assert refused.value.error_code == code


def _definition(operator: str, right: dict[str, Any], *, parameters: Any = ()) -> dict[str, Any]:
    from tests.core_support._knowledge_loop_support import work_item_query

    declared = work_item_query("project.ordered").model_dump(mode="json")
    declared["where"] = {
        "kind": "comparison",
        "left": {"kind": "claim_value", "binding": "item", "predicate": PREDICATE},
        "operator": operator,
        "right": right,
        "value_type": "string",
    }
    declared["parameters"] = list(parameters)
    return declared


def _spec(operator: str, right: dict[str, Any], *, parameters: Any = ()) -> QueryDefinitionSpec:
    return QueryDefinitionSpec.model_validate(_definition(operator, right, parameters=parameters))


def test_a_spec_ordering_an_unordered_enum_refuses_before_any_row(unranked: Any) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(unranked, spec=_spec("gt", {"kind": "literal", "value": "blocked"}))
    assert "unordered enum" in str(refused.value)


def test_a_parameter_compares_by_rank_and_a_non_member_refuses(ranked: Any) -> None:
    def rows(floor: str) -> list[str]:
        result = _query(ranked, name="project.ordered", params={"floor": floor})
        return sorted(row["item_id"] for row in result.rows)

    assert rows("blocked") == ["wi-43"]
    assert rows("ready") == ["wi-42", "wi-43"]
    with pytest.raises(ReadRefusalError) as refused:
        rows("urgent")
    assert "not one of ready, blocked, done" in str(refused.value)


# -- authoring ------------------------------------------------------------------------


def _authored_query(tree: dict[str, bytes], where: str) -> Any:
    from cruxible_client.contracts.authoring.inputs import (
        QueryDefinitionInput,
        lower_authoring_input,
    )
    from cruxible_client.contracts.query.authored import QuerySpecInput
    from cruxible_core.authoring.lowering import _render_non_procedure_member

    spec = QuerySpecInput.model_validate(
        {
            "query": "test.ordered",
            "from": {"item": SUBJECT_KIND},
            "fields": ["item", "item.status"],
            "where": where,
        }
    )
    payload = lower_authoring_input(
        QueryDefinitionInput(kind="query_definition", query_definition=spec)
    )
    return _render_non_procedure_member(payload, tree=tree)


def _tree(value: EnumType) -> dict[str, bytes]:
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type

    return {claim_type_path(PREDICATE): render_claim_type(_status_type(value))}


def test_an_authored_query_orders_an_ordered_enum_and_refuses_an_unordered_one() -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError
    from cruxible_core.service.read_refusals import ReadRefusalError as Refusal

    _authored_query(_tree(RANKED), 'item.status > "ready"')
    with pytest.raises((AuthoringLoweringError, Refusal)) as refused:
        _authored_query(_tree(EnumType(members=RANKED.members)), 'item.status > "ready"')
    assert getattr(refused.value, "code", None) in {
        "cruxible.query_definition.enum_comparison_invalid",
        "cruxible.query.operator_not_applicable",
    }


def test_the_definition_check_refuses_a_query_built_without_the_authored_resolver() -> None:
    from cruxible_client.contracts.authoring.inputs import (
        QueryDefinitionInput,
        lower_authoring_input,
    )
    from cruxible_core.authoring.lowering import (
        AuthoringLoweringError,
        _render_non_procedure_member,
    )

    payload = lower_authoring_input(
        QueryDefinitionInput.model_validate(
            {
                "kind": "query_definition",
                "query_definition": _definition("lt", {"kind": "literal", "value": "done"}),
            }
        )
    )
    _render_non_procedure_member(payload, tree=_tree(RANKED))
    with pytest.raises(AuthoringLoweringError) as refused:
        _render_non_procedure_member(payload, tree=_tree(EnumType(members=RANKED.members)))
    assert refused.value.code == "cruxible.query_definition.enum_comparison_invalid"


# -- compiler revision 74 ------------------------------------------------------------


@pytest.fixture
def revision_73(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from cruxible_core.compiler.compiler import COMPILER_REVISION_73
    from tests.support.operation_world import world

    with monkeypatch.context() as patch:
        patch.setattr(
            "cruxible_core.runtime.instance.current_compiler_coordinate",
            lambda: COMPILER_REVISION_73,
        )
        instance = world(tmp_path)
    assert instance.accepted_coordinate().compiler == COMPILER_REVISION_73
    return instance


def _set(*members: Any) -> Any:
    from cruxible_client.contracts.authoring.inputs import ChangeSetInput

    return ChangeSetInput(kind="change_set", members=members)


def _priority(ordered: bool, name: str = "test.ticket.priority") -> Any:
    return claim_type(
        name,
        value_type={"kind": "enum", "members": list(PRIORITY), "ordered": ordered},
    )


def test_revision_73_refuses_an_ordered_enum_and_keeps_an_unordered_one(revision_73: Any) -> None:
    from cruxible_client.contracts.authoring.inputs import ClaimTypeInput, ContractInput
    from cruxible_core.authoring.lowering import AuthoringLoweringError
    from tests.support.operation_world import author

    with pytest.raises(AuthoringLoweringError) as refused:
        author(
            revision_73,
            _set(ClaimTypeInput(kind="claim_type", claim_type=_priority(True))),
            name="o",
        )
    assert refused.value.code == "cruxible.authoring.ordered_enum_requires_compiler_upgrade"
    assert "cruxible compiler upgrade" in str(refused.value.repairs)
    with pytest.raises(AuthoringLoweringError) as contract:
        author(
            revision_73,
            ContractInput.model_validate(
                {
                    "kind": "contract",
                    "name": "test.ranked-row",
                    "version": 1,
                    "fields": {"p": "list<ordered_enum[low, high]>{max_items: 2}"},
                }
            ),
            name="c",
        )
    assert contract.value.code == "cruxible.authoring.ordered_enum_requires_compiler_upgrade"
    changed = author(
        revision_73,
        _set(ClaimTypeInput(kind="claim_type", claim_type=_priority(False))),
        name="u",
    )
    from cruxible_client.contracts.claim_types import claim_type_path

    assert claim_type_path("test.ticket.priority") in changed


def test_revision_73_acceptance_law_refuses_ordered_enum_bytes(revision_73: Any) -> None:
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from tests.core_support._identity_support import accept_tree

    path = claim_type_path("test.ticket.rank")
    with pytest.raises(Exception, match="ordered_enum_requires_compiler_upgrade"):
        accept_tree(
            revision_73,
            {path: render_claim_type(_priority(True, "test.ticket.rank"))},
            name="raw-ordered",
        )


def test_revision_74_accepts_an_ordered_enum(tmp_path: Path) -> None:
    from cruxible_client.contracts.authoring.inputs import ClaimTypeInput
    from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
    from tests.support.operation_world import author, world

    instance = world(tmp_path)
    changed = author(
        instance, _set(ClaimTypeInput(kind="claim_type", claim_type=_priority(True))), name="o"
    )
    path = claim_type_path("test.ticket.priority")
    assert parse_claim_type(changed[path], path=path).value == EnumType(
        members=PRIORITY, ordered=True
    )
