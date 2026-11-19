"""Query parameter constructors reuse the evaluator's type rules."""

import pytest

from cruxible_client.contracts.query.grammar import QueryParameterDeclaration
from cruxible_client.contracts.query.parameters import QueryParameters
from tests.support.scoped_query_oracle import _scoped_facts_answer_as_whole_facts  # noqa: F401
from tests.test_query.test_query_definitions import active_work_query


def test_parameters_use_query_types_and_reject_unknown_or_missing_fields():
    query = active_work_query(
        parameters=(QueryParameterDeclaration(name="status", value_type="string"),)
    )
    constructor = QueryParameters(query)
    assert constructor(status="ready").status == "ready"
    for fields in ({}, {"status": False}, {"status": "ready", "typo": True}):
        with pytest.raises(ValueError):
            constructor(**fields)


@pytest.mark.parametrize(
    "kind,valid,invalid",
    [
        ("integer", 3, True),
        ("boolean", False, 0),
        ("decimal", "12.50", []),
        ("timestamp", "2026-09-21T12:00:00Z", "2026-09-21"),
        ("subject_reference", "Subject:security.asset/app", 1),
    ],
)
def test_parameters_reuse_the_evaluator_type_rules(kind, valid, invalid):
    query = active_work_query(
        parameters=(QueryParameterDeclaration(name="status", value_type=kind),)
    )
    constructor = QueryParameters(query)
    assert constructor(status=valid)["status"] == valid
    with pytest.raises(ValueError):
        constructor(status=invalid)
