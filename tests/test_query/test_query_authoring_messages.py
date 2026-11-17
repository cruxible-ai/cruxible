"""Authored-query refusals a first-time kit author meets (coldkit-04, -08, -09)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from cruxible_client.authoring.compact_query import parse_where
from cruxible_client.contracts.authoring.inputs import AuthoringInput
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.query.authored import (
    QuerySpecError,
    QuerySpecInput,
    lower_query_spec,
    parse_filter,
    query_spec_from_definition,
)
from cruxible_core.service.discovery.authored_query import (
    AuthoredQueryResolver,
    candidate_query_vocabulary,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/security_authored_queries.json").read_text()
)


@pytest.fixture(scope="module")
def resolver():
    tree = {
        claim_type_path(q["predicate"]): render_claim_type(ClaimType.model_validate(q))
        for q in FIXTURE["claim_types"]
    }
    return AuthoredQueryResolver(candidate_query_vocabulary(tree))


def spec(**changes):
    return QuerySpecInput.model_validate(
        {
            "query": "test.authored",
            "from": {"incident": "security.incident"},
            "fields": ["incident", "incident.name"],
            **changes,
        }
    )


def test_an_unknown_param_type_is_one_refusal_listing_the_types():
    """coldkit-04: one error naming the allowed types, not a dump of every union arm."""

    payload = {
        "kind": "query_definition",
        "query_definition": {
            "query": "test.since",
            "params": {"since": "datetime"},
            "from": {"incident": "security.incident"},
            "where": "incident.window_start > $since",
            "fields": ["incident.name"],
        },
    }
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(AuthoringInput).validate_python(payload)
    errors = caught.value.errors(include_url=False)
    assert len(errors) == 1
    assert errors[0]["type"] == "query_param_type_unknown"
    message = errors[0]["msg"]
    for allowed in ("string", "integer", "boolean", "decimal", "timestamp", "subject_reference"):
        assert allowed in message
    assert "(use timestamp)" in message


def test_a_typed_definition_still_validates_as_the_typed_form():
    """The authored/typed choice keys on `query`; a typed definition is unaffected."""

    from cruxible_client.authoring.examples import query_claims_by_type_example

    typed = query_claims_by_type_example().model_dump(mode="json")
    parsed = TypeAdapter(AuthoringInput).validate_python(typed)
    assert not isinstance(parsed.query_definition, QuerySpecInput)  # type: ignore[union-attr]


def test_a_string_param_against_a_link_field_names_subject_reference(resolver):
    """coldkit-08: the refusal says which declaration would work."""

    with pytest.raises(QuerySpecError) as caught:
        lower_query_spec(
            spec(where="incident.vulnerability == $vuln", params={"vuln": "string"}), resolver
        )
    assert caught.value.error_code == "cruxible.query.value_type_mismatch"
    assert "declare it in params as vuln: subject_reference" in str(caught.value)
    assert "link field" in str(caught.value)


@pytest.mark.parametrize(
    "single,double",
    [
        ("incident.status in ('open', 'closed')", 'incident.status in ("open", "closed")'),
        ("incident.name == 'it''s'", None),
        ("incident.name == 'say \"hi\"'", 'incident.name == "say \\"hi\\""'),
        ("incident.name == 'it\\'s'", 'incident.name == "it\'s"'),
    ],
)
def test_single_quoted_text_lowers_like_double_quoted(single, double, resolver):
    """coldkit-09: both quote styles name the same value and render back double-quoted."""

    if double is None:
        # Doubling the quote is not an escape; the tokens after it do not parse.
        with pytest.raises(QuerySpecError):
            lower_query_spec(spec(where=single), resolver)
        return
    from_single = lower_query_spec(spec(where=single), resolver)
    from_double = lower_query_spec(spec(where=double), resolver)
    assert from_single == from_double
    rendered = query_spec_from_definition(from_single).where
    assert rendered is not None and "'" not in rendered.replace("it's", "")


def test_single_quotes_are_not_a_reference():
    with pytest.raises(QuerySpecError) as caught:
        parse_filter("'open' == incident.status")
    assert caught.value.error_code == "cruxible.query.filter_invalid"


def test_compact_where_accepts_single_quotes():
    assert parse_where("status='open'") == parse_where('status="open"')
    assert parse_where("status in ('open', 'closed')") == parse_where(
        'status in ("open", "closed")'
    )


@pytest.mark.parametrize(
    "where",
    [
        "incident.name < now - 3d",  # a string field (a date reads as one too)
        "incident.status > now - 3d",  # an enum field: refused before its operators
        "now - 3d > incident.name",
    ],
)
def test_now_on_a_field_that_is_not_datetime_says_to_declare_it_datetime(where, resolver):
    """sec N9 / psw F18: the refusal said `cannot compare string with timestamp`."""

    with pytest.raises(QuerySpecError) as caught:
        lower_query_spec(spec(where=where), resolver)
    assert caught.value.error_code == "cruxible.query.value_type_mismatch"
    assert "now compares only with a datetime field" in str(caught.value)
    assert "timestamp" not in str(caught.value)


def test_a_timestamp_field_type_names_datetime():
    """sec N9: `value: timestamp` refused with no word of `datetime`."""

    from cruxible_client.contracts.shapes import ContractFormatError, parse_type

    with pytest.raises(ContractFormatError, match="did you mean 'datetime'"):
        parse_type("timestamp")
