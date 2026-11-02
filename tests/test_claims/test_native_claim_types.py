"""D-R12/D-R13: typed Subject unions, bounded facts and Documents for prose."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_path,
    evaluate_claim_type_law,
)
from cruxible_client.contracts.procedures.claims import ClaimTemplate
from cruxible_client.contracts.shapes import (
    Contract,
    FieldInvalid,
    StringType,
    assignable,
    check_value,
    parse_type,
    render_type,
)
from tests.support.claim_types import claim_type


def test_subject_union_is_canonical_and_accepts_only_named_kinds():
    union = parse_type("subject<catalog.b | catalog.a>")
    assert render_type(union) == "subject<catalog.a | catalog.b>"
    assert union.subject_kinds == ("catalog.a", "catalog.b")
    for value in ("catalog.a/one", "subjects/catalog.b/two.json"):
        assert not isinstance(check_value(value, union, at="value"), FieldInvalid)
    for value in ("catalog.c/three", "one", "", {"kind": "catalog.a"}):
        assert isinstance(check_value(value, union, at="value"), FieldInvalid)
    assert assignable(parse_type("subject<catalog.a>"), union)
    assert not assignable(union, parse_type("subject<catalog.a>"))
    with pytest.raises(ValueError, match="unique"):
        parse_type("subject<catalog.a | catalog.a>")


@pytest.mark.parametrize("maximum", [None, 257])
def test_string_claim_types_refuse_unbounded_or_prose_size(maximum):
    value = claim_type().model_copy(update={"value": StringType(max_len=maximum)})
    law = evaluate_claim_type_law(value, path=claim_type_path(value.predicate), predecessor=None)
    assert law.verdict == "refused"
    assert law.diagnostics[0].code == "cruxible.claim_type.string_unbounded"
    assert "cite a Document section" in law.diagnostics[0].message


@pytest.mark.parametrize("value", ["exact_content", {"kind": "exact_content"}])
def test_native_exact_content_value_has_a_document_repair(value):
    with pytest.raises(ValueError, match="exact_content_retired.*Documents"):
        ClaimType.model_validate({**claim_type().model_dump(mode="json"), "value": value})


def test_exact_content_claim_type_has_a_document_repair():
    raw = claim_type().model_dump(mode="json")
    raw["object_kind"] = "exact_content"
    with pytest.raises(ValidationError, match="exact_content_retired.*Documents"):
        ClaimType.model_validate(raw)


@pytest.mark.parametrize(
    "rationale", ["line\nbreak", "line\u2028break", "line\x85break", "x" * 281]
)
def test_claim_template_rationale_is_one_short_line(rationale):
    with pytest.raises(ValidationError, match="rationale_invalid"):
        ClaimTemplate(
            type=ArtifactNameRef(
                target=ArtifactIdentity(kind="ClaimType", name="test.item.status")
            ),
            value="open",
            rationale=rationale,
        )


@pytest.mark.parametrize("separator", ["\n", "\r", "\u2028", "\x85"])
def test_multiline_extraction_is_inadmissible(tmp_path: Path, separator: str):
    from tests.test_procedures.test_claims_terminal import (
        claim_type as terminal_type,
    )
    from tests.test_procedures.test_claims_terminal import (
        claims_spec,
        claims_world,
        install,
        run,
    )

    rows = Contract.model_validate(
        {
            "name": "test.claim-row",
            "version": 1,
            "fields": {"status": "string{max_len: 256}", "note": "string{max_len: 256}?"},
        }
    )
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": "CVE-2026-0001", "status": f"one{separator}two"}]},
        types=(terminal_type(value="string{max_len: 256}"),),
        rows=rows,
    )
    install(instance, claims_spec())
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert state.terminal_egress[0].proposal_id is None
    assert state.findings[0].problem == "multiline_string"


def test_multiline_literal_template_refuses_before_execution(tmp_path: Path):
    from cruxible_client.contracts.authoring.inputs import AuthoringInputError
    from tests.test_procedures.test_claims_terminal import (
        PREDICATE,
        claims_spec,
        claims_world,
        install,
    )
    from tests.test_procedures.test_claims_terminal import (
        claim_type as terminal_type,
    )

    instance, _ = claims_world(
        tmp_path,
        {"entries": [{"id": "CVE-2026-0001", "status": "open"}]},
        types=(terminal_type(value="string{max_len: 256}"),),
    )
    with pytest.raises((AuthoringInputError, ValueError), match="multiline_string"):
        install(instance, claims_spec(mappings=[{"type": PREDICATE, "value": "one\ntwo"}]))


def test_expanded_rationale_cannot_turn_a_short_template_into_prose(tmp_path):
    from tests.test_procedures.test_claims_terminal import claims_spec, claims_world, install, run

    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open", "note": "x" * 255}]}
    )
    install(
        instance,
        claims_spec(
            mappings=[
                {
                    "type": "security.vulnerability.status",
                    "value": "$item.fields.status",
                    "rationale": "Observation from the declared source: {$item.fields.note}",
                }
            ]
        ),
    )
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    egress = state.terminal_egress[0]
    assert egress.proposal_id is None and not egress.children
    assert egress.claims_diagnostics[0].problem == "rationale_invalid"


@pytest.mark.parametrize(
    "spelling", ["list<string>{max_items: 10}", "list<string{max_len: 100000}>{max_items: 10}"]
)
def test_prose_guard_checks_list_elements(spelling):
    value = claim_type().model_copy(update={"value": parse_type(spelling)})
    law = evaluate_claim_type_law(value, path=claim_type_path(value.predicate), predecessor=None)
    assert law.verdict == "refused"
    assert law.diagnostics[0].code == "cruxible.claim_type.string_unbounded"


@pytest.mark.parametrize("optional", [False, True])
@pytest.mark.parametrize("variant", [False, True])
def test_prose_guard_follows_nested_contracts_and_variant_arms(optional, variant):
    from cruxible_client.contracts.shapes import ContractField, NestedType, VariantType

    row = Contract(
        name="test.prose",
        version=1,
        fields={"text": ContractField(type=StringType(), optional=optional)},
    )
    field = VariantType(arms={"note": row.ref}) if variant else NestedType(contract=row.ref)
    value = claim_type().model_copy(update={"value": field})

    def resolve(ref):
        return row if ref == row.ref else None

    law = evaluate_claim_type_law(
        value, path=claim_type_path(value.predicate), predecessor=None, resolve_contract=resolve
    )
    assert law.verdict == "refused"
    assert law.diagnostics[0].code == "cruxible.claim_type.string_unbounded"
    bounded = row.model_copy(
        update={"fields": {"text": ContractField(type=StringType(max_len=256), optional=optional)}}
    )
    law = evaluate_claim_type_law(
        value,
        path=claim_type_path(value.predicate),
        predecessor=None,
        resolve_contract=lambda ref: bounded,
    )
    assert law.verdict == "accepted"


@pytest.mark.parametrize("separator", list("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"))
def test_nested_literal_strings_use_the_same_line_break_rule(separator):
    from cruxible_client.contracts.shapes import claim_literal_problem

    value = {"nested": ["one" + separator + "two"]}
    assert (
        claim_literal_problem(value, parse_type("string{max_len: 256}"))
        == "multiline_string: use a Document section for prose"
    )


def test_contract_subject_unions_do_not_treat_semantic_subject_as_a_wildcard():
    assert isinstance(
        check_value("catalog.a/one", parse_type("subject<semantic.subject>"), at="value"),
        FieldInvalid,
    )
