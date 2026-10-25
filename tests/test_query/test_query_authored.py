"""Authored query coverage against all 50 frozen security-kit definitions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput, lower_authoring_input
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.query.authored import (
    QuerySpecError,
    QuerySpecInput,
    lower_query_spec,
    parse_filter,
    query_spec_from_definition,
)
from cruxible_client.contracts.query.definitions import (
    parse_query_definition,
    query_definition_digest,
    render_query_definition,
)
from cruxible_core.authoring.lowering import _render_non_procedure_member
from cruxible_core.service.discovery.authored_query import (
    AuthoredQueryResolver,
    candidate_query_vocabulary,
)
from cruxible_core.service.read_refusals import ReadRefusalError

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/security_authored_queries.json").read_text()
)


@pytest.fixture(scope="module")
def tree():
    return {
        claim_type_path(q["predicate"]): render_claim_type(ClaimType.model_validate(q))
        for q in FIXTURE["claim_types"]
    }


@pytest.fixture(scope="module")
def resolver(tree):
    return AuthoredQueryResolver(candidate_query_vocabulary(tree))


@pytest.mark.parametrize("case", FIXTURE["queries"], ids=lambda case: case["authored"]["query"])
def test_all_kit_queries_lower_to_exact_typed_tree(case, resolver, tree):
    spec = QuerySpecInput.model_validate(case["authored"])
    query = lower_query_spec(spec, resolver)
    body = query.model_dump(mode="json")
    body.pop("pins")
    assert hashlib.sha256(canonical_bytes(body)).hexdigest() == case["tree_sha256"]
    payload = lower_authoring_input(
        QueryDefinitionInput(kind="query_definition", query_definition=spec)
    )
    path, wire, digest = _render_non_procedure_member(payload, tree=tree, light_query=True)
    stored = parse_query_definition(wire, path=path)
    assert stored.artifact_format == "cruxible-query-definition-v3"
    assert query_definition_digest(stored).tagged == digest
    assert b'"tag"' not in wire
    assert b'"result_binding"' not in wire
    assert b'"result_cardinality"' not in wire
    assert render_query_definition(stored) == wire
    assert lower_query_spec(query_spec_from_definition(stored), resolver) == query


def simple(**changes):
    return QuerySpecInput.model_validate(
        {
            "query": "test.authored",
            "from": {"incident": "security.incident"},
            "fields": ["incident", "incident.name"],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "expression",
    [
        'incident.name == "x" or incident.name == "y" and not exists incident.status',
        '(incident.name == "x" or incident.name == "y") and exists incident.status',
        'not (incident.name in ("x", "y"))',
        "incident.name == $name",
    ],
)
def test_filters_have_stable_roundtrip(expression, resolver):
    query = lower_query_spec(simple(where=expression, params={"name": "string"}), resolver)
    assert lower_query_spec(query_spec_from_definition(query), resolver) == query


@pytest.mark.parametrize(
    "expression,column",
    [
        ("incident.name >> 0", 16),
        ("incident.name ==", 17),
        ('(incident.name == "x"', 22),
        ("incident.name in ()", 19),
    ],
)
def test_syntax_refusals_have_authored_path_and_column(expression, column):
    with pytest.raises(QuerySpecError) as caught:
        parse_filter(expression, path="$.hops[1]")
    assert caught.value.error_code == "cruxible.query.filter_invalid"
    assert caught.value.field_path == "$.hops[1]"
    assert caught.value.column == column
    assert "column" in str(caught.value)


@pytest.mark.parametrize(
    "changes,code,path",
    [
        ({"fields": ["incident.nmae"]}, "unknown_field", "$.fields[0]"),
        ({"where": "incident.name == $missing"}, "parameter_undeclared", "$.where"),
        ({"where": "incident.name == 3"}, "value_type_mismatch", "$.where"),
        ({"where": 'incident.status == "not-a-status"'}, "unknown_member", "$.where"),
        ({"where": 'incident.status > "active"'}, "operator_not_applicable", "$.where"),
        ({"hops": [{"other": "incident.name"}]}, "follow_not_relation", "$.hops[0]"),
        (
            {"hops": [{"other": "<incident.security.incident.vulnerability"}]},
            "follow_not_incoming",
            "$.hops[0]",
        ),
    ],
)
def test_refusal_table(changes, code, path, resolver):
    with pytest.raises((QuerySpecError, ReadRefusalError)) as caught:
        lower_query_spec(simple(**changes), resolver)
    assert caught.value.error_code == "cruxible.query." + code
    assert caught.value.field_path == path


def test_model_is_closed():
    with pytest.raises(ValidationError):
        simple(unknown=True)
    with pytest.raises(ValidationError):
        simple(hops=[{"a": "incident.name", "b": "incident.name"}])


def test_v3_never_changes_frozen_wire_or_digests(tree, resolver):
    query = lower_query_spec(simple(), resolver)
    payload = lower_authoring_input(
        QueryDefinitionInput(kind="query_definition", query_definition=query)
    )
    path, wire, digest = _render_non_procedure_member(payload, tree=tree)
    old = parse_query_definition(wire, path=path)
    assert old.artifact_format == "playbill-query-definition-v1"
    assert render_query_definition(old) == wire
    assert query_definition_digest(old).tagged == digest
    new = old.model_copy(update={"artifact_format": "cruxible-query-definition-v3"})
    assert query_definition_digest(new).tagged != digest
    assert len(render_query_definition(new)) < len(wire)


def test_reverse_shorthand_and_multiple_target_kinds(resolver):
    explicit = simple(
        hops=[
            {"vulnerability": "incident.vulnerability!"},
            {"assessment": "<vulnerability.security.assessment.vulnerability!"},
        ]
    )
    shorthand = simple(
        hops=[
            {"vulnerability": "incident.vulnerability!"},
            {"assessment": "<assessment.vulnerability!"},
        ]
    )
    assert lower_query_spec(explicit, resolver) == lower_query_spec(shorthand, resolver)


def test_authored_errors_are_mapped_through_authoring_refusals(tree):
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    payload = lower_authoring_input(
        QueryDefinitionInput(
            kind="query_definition", query_definition=simple(where="incident.name >> 0")
        )
    )
    with pytest.raises(AuthoringLoweringError) as caught:
        _render_non_procedure_member(payload, tree=tree, light_query=True)
    assert caught.value.code == "cruxible.query.filter_invalid"
    assert caught.value.offending_element == "$.where"
    assert "$.where" in str(caught.value)
    assert "column 16" in str(caught.value)


@pytest.mark.parametrize("size", [10, 1000])
def test_indexed_vocabulary_only_reads_definition_paths(tree, size):
    class CountedTree(dict):
        def __iter__(self):
            raise AssertionError("indexed authoring must not enumerate the accepted tree")

        def get(self, key, default=None):
            assert not key.startswith("claims/")
            return super().get(key, default)

    population = CountedTree(tree)
    population.update({f"claims/{i}.json": b"unread" for i in range(size)})
    paths = tuple(tree)
    assert candidate_query_vocabulary(population, paths=paths) == candidate_query_vocabulary(tree)


@pytest.mark.parametrize(
    "light,format",
    [(True, "playbill-query-definition-v1"), (False, "cruxible-query-definition-v3")],
)
def test_new_query_formats_are_gated_by_compiler_revision(light, format, tree, resolver):
    from types import SimpleNamespace

    from cruxible_core.compiler.compiler import CLAIM_REBIND_COMPILER, LIGHT_ARTIFACTS_COMPILER
    from cruxible_core.proposals.proposals import _query_definition_member

    payload = lower_authoring_input(
        QueryDefinitionInput(kind="query_definition", query_definition=simple())
    )
    path, wire, _ = _render_non_procedure_member(
        payload, tree=tree, light_query=format == "cruxible-query-definition-v3"
    )
    verdict = _query_definition_member(
        SimpleNamespace(
            content=wire,
            path=path,
            historical_law_coordinate=None,
            current=SimpleNamespace(
                compiler=LIGHT_ARTIFACTS_COMPILER if light else CLAIM_REBIND_COMPILER
            ),
        )
    )
    assert [item.code for item in verdict.diagnostics] == [
        "cruxible.proposal.member_format_invalid"
    ]
    assert "upgrade the compiler" in verdict.diagnostics[0].message
    assert parse_query_definition(wire, path=path).artifact_format == format


def test_decimal_filter_preserves_its_exact_spelling(resolver):
    from dataclasses import replace

    from cruxible_core.service.discovery.query_vocabulary import QueryVocabulary

    info = resolver.vocabulary.predicates["security.incident.name"]
    numeric = replace(
        info,
        value_type="decimal",
        claim_type=info.claim_type.model_copy(
            update={"literal_schema": {"type": "number"}, "canonical_form": ()}
        ),
    )
    vocabulary = QueryVocabulary(
        predicates={numeric.predicate: numeric}, kinds=("security.incident",)
    )
    query = lower_query_spec(
        simple(where="incident.name > 0.100"), AuthoredQueryResolver(vocabulary)
    )
    assert query.where.value_type == "decimal"
    assert query.where.right.value == "0.100"
    assert parse_filter("incident.name > 0.100").operands[1].kind == "decimal"


def test_v3_preserves_opaque_literal_tags_and_kinds(tree, resolver):
    from cruxible_client.contracts.query.grammar import (
        QueryLiteralRef,
        QueryProjection,
        QueryProjectionField,
    )

    query = lower_query_spec(simple(), resolver).model_copy(
        update={
            "artifact_format": "cruxible-query-definition-v3",
            "projection": QueryProjection(
                fields=(
                    QueryProjectionField(
                        name="data",
                        value=QueryLiteralRef(
                            value={"tag": "user", "nested": [{"tag": "also-user", "kind": "data"}]}
                        ),
                    ),
                )
            ),
        }
    )
    wire = render_query_definition(query)
    restored = type(query).model_validate_json(wire)
    assert restored.projection == query.projection
    assert restored.projection.fields[0].value.value["tag"] == "user"
    assert restored.projection.fields[0].value.value["nested"][0]["tag"] == "also-user"


def test_compact_v3_input_restores_equal_budgets_before_spec_defaults(resolver):
    from cruxible_client.contracts.authoring.models import QueryDefinitionAuthoringPayload

    query = lower_query_spec(simple(limit=2000), resolver).model_copy(
        update={"artifact_format": "cruxible-query-definition-v3"}
    )
    wire = json.loads(render_query_definition(query))
    assert "default_budgets" not in wire
    payload = QueryDefinitionAuthoringPayload.model_validate({"query_definition": wire})
    assert payload.query_definition.default_budgets == query.maximum_budgets
    assert payload.query_definition.maximum_budgets == query.maximum_budgets


def test_v3_artifact_entry_restores_its_default_binding(tree):
    from cruxible_client.authoring.examples import query_ontology_example

    payload = lower_authoring_input(query_ontology_example())
    path, wire, digest = _render_non_procedure_member(payload, tree=tree, light_query=True)
    body = json.loads(wire)
    assert "binding" not in body["entry"]
    assert "result_binding" not in body
    query = parse_query_definition(wire, path=path)
    assert query.result_binding == query.entry.binding == "definition"
    assert query_definition_digest(query).tagged == digest
