"""The Contract type grammar: spellings, refusals, values, casts, edges and predicates."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cruxible_client.contracts.shapes import (
    ContractFormatError,
    ContractTypeRefused,
    FieldAbsent,
    FieldInvalid,
    FieldPresent,
    InvalidRecordArm,
    Not,
    RecordArm,
    assignable,
    cast_record,
    check_record,
    contract_digest,
    contract_path,
    edge_refusal,
    evaluate_predicate,
    parse_contract,
    parse_predicate,
    parse_type,
    records,
    render_contract,
    render_type,
    validate_contract,
)

ROW = {
    "name": "security.epss-row",
    "version": 1,
    "fields": {
        "epss": "decimal{0..1}",
        "percentile": "decimal{0..1}?",
        "status": "enum[fixed, open]",
    },
}


@pytest.mark.parametrize(
    "spelling",
    [
        "string",
        'string{pattern: "^CVE-", min_len: 5, max_len: 32}',
        "int{0..9}",
        "int{0..}",
        "decimal{0..1}",
        "bool",
        "date",
        "datetime",
        "duration",
        'enum["a", "b c"]',
        "enum_of security.vulnerability.kev_ransomware_use",
        "list<int{0..}>{max_items: 10}",
        "security.declined@1",
        "variant{ok: a.b@1, bad: c@2}",
        "key<security.vulnerability>",
        "subject<security.vulnerability>",
        "capture<security.kev>",
        "field_result<date>",
        "record<security.kev-entry@1>",
        "result<security.kev-entry@1>",
        "refusal<security.declined@1>",
    ],
)
def test_every_spelling_reads_and_renders_back(spelling: str) -> None:
    assert render_type(parse_type(spelling)) == spelling


@pytest.mark.parametrize("spelling", ["json", "map", "object", "claim", "role", "revises"])
def test_untyped_and_claim_naming_types_are_refused(spelling: str) -> None:
    with pytest.raises(ContractTypeRefused) as caught:
        validate_contract({**ROW, "fields": {"x": spelling}})
    assert caught.value.code == "contract_type_refused"


@pytest.mark.parametrize(
    "field",
    [
        "list<int>",
        {"type": {"kind": "list", "item": {"kind": "int"}}},
        {"type": {"kind": "json"}},
    ],
)
def test_json_and_unbounded_lists_are_refused_in_either_form(field: object) -> None:
    with pytest.raises(ContractTypeRefused) as caught:
        validate_contract({**ROW, "fields": {"x": field}})
    assert caught.value.code == "contract_type_refused"


def test_a_malformed_spelling_is_a_format_error_not_a_refusal() -> None:
    with pytest.raises(ContractFormatError) as caught:
        parse_type("int{0..")
    assert not isinstance(caught.value, ContractTypeRefused)


def test_contract_bytes_digest_and_path_round_trip() -> None:
    contract = validate_contract(ROW)
    path = contract_path(contract.name, contract.version)
    assert path == "contracts/security.epss-row/v1.json"
    content = render_contract(contract)
    assert parse_contract(content, path=path) == contract
    assert contract.identity.qualified == "Contract:security.epss-row/v1"
    successor = validate_contract(
        {**ROW, "version": 2, "predecessor_digest": contract_digest(contract).tagged}
    )
    assert successor.predecessor_digest == contract_digest(contract).tagged
    with pytest.raises(ContractFormatError):
        validate_contract({**ROW, "version": 2})
    with pytest.raises(ContractFormatError):
        parse_contract(content, path="contracts/security.epss-row/v2.json")


def test_an_accepted_contract_pins_nested_contracts_by_digest() -> None:
    nested = validate_contract({**ROW, "fields": {"inner": "security.other@1"}})
    with pytest.raises(ContractFormatError):
        parse_contract(render_contract(nested), path="contracts/security.epss-row/v1.json")


def test_an_invalid_field_keeps_its_valid_siblings_and_never_raises() -> None:
    contract = validate_contract(ROW)
    record = check_record({"epss": "n/a", "status": "fixed"}, contract, record="/rows/0")
    assert isinstance(record.fields["epss"], FieldInvalid)
    assert record.fields["epss"].problem.code == "type_mismatch"
    assert isinstance(record.fields["percentile"], FieldAbsent)
    assert record.fields["status"] == FieldPresent(value="fixed", at="/status")
    missing = check_record({}, contract, record="/rows/1")
    assert isinstance(missing.fields["epss"], FieldInvalid)
    assert missing.fields["epss"].problem.code == "absent_required"
    assert isinstance(missing.fields["percentile"], FieldAbsent)


def test_a_failed_shape_cast_marks_only_that_field() -> None:
    raw = validate_contract(
        {
            "name": "security.epss-raw",
            "version": 1,
            "fields": {"epss": "string", "percentile": "string"},
        }
    )
    record = check_record({"epss": "n/a", "percentile": "0.3"}, raw, record="/rows/0")
    cast_to = parse_type("decimal{0..1}")
    result = cast_record(record, {"epss": cast_to, "percentile": cast_to})
    assert isinstance(result, RecordArm)
    epss = result.record.fields["epss"]
    assert isinstance(epss, FieldInvalid) and epss.problem.code == "type_mismatch"
    assert result.record.fields["percentile"] == FieldPresent(
        value=Decimal("0.3"), at="/percentile"
    )
    keyed = cast_record(record, {"epss": cast_to}, key_fields=("epss",))
    assert isinstance(keyed, InvalidRecordArm)


def test_assignable_follows_the_edge_rule() -> None:
    entry = parse_type("security.kev-entry@1")
    assert assignable(entry, parse_type("security.kev-entry@1"))
    assert not assignable(entry, parse_type("security.kev-entry@2"))
    assert not assignable(entry, parse_type("security.other@1"))
    assert assignable(parse_type("int{0..9}"), parse_type("int"))
    assert not assignable(parse_type("int"), parse_type("int{0..9}"))
    assert assignable(parse_type("enum[a]"), parse_type("enum[a, b]"))
    assert not assignable(parse_type("int"), parse_type("decimal"))


def test_a_result_flows_only_through_its_record_arm() -> None:
    results = parse_type("list<result<security.kev-entry@1>>{max_items: 100}")
    rows = parse_type("list<record<security.kev-entry@1>>{max_items: 100}")
    assert not assignable(results, rows)
    assert "record arm" in (edge_refusal(results, rows) or "")
    assert assignable(results, rows, via="records")
    assert not assignable(
        parse_type("result<security.kev-entry@1>"), parse_type("security.kev-entry@1")
    )
    assert assignable(parse_type("field_result<date>"), parse_type("date"), via="field")
    assert not assignable(parse_type("field_result<date>"), parse_type("date"))


def test_records_reads_the_record_arm_only() -> None:
    contract = validate_contract(ROW)
    row = check_record({"epss": "0.1", "status": "open"}, contract, record="/rows/0")
    bad = InvalidRecordArm(record="/rows/1", problems=({"code": "key_absent"},))  # type: ignore[arg-type]
    assert records([RecordArm(record=row), bad]) == [row]


def test_three_valued_predicates_over_an_invalid_field() -> None:
    contract = validate_contract(ROW)
    invalid = check_record({"epss": "0.5", "status": "bogus"}, contract, record="/rows/0")
    fixed = parse_predicate('eq($item.fields.status, "fixed")')
    assert evaluate_predicate(fixed, fields=invalid) == "unknown"
    assert evaluate_predicate(Not(of=fixed), fields=invalid) == "unknown"
    valid = check_record({"epss": "0.5", "status": "fixed"}, contract, record="/rows/1")
    assert evaluate_predicate(fixed, fields=valid) == "true"
    assert evaluate_predicate(
        parse_predicate("not(present($item.fields.percentile))"), fields=valid
    ) == ("true")
    assert (
        evaluate_predicate(parse_predicate('lt($item.fields.epss, "0.7")'), fields=valid) == "true"
    )
    both = parse_predicate('all_of(eq($item.fields.status, "fixed"), lt($item.fields.epss, "0.7"))')
    assert evaluate_predicate(both, fields=invalid) == "unknown"
    assert evaluate_predicate(both, fields=valid) == "true"
    falsified = parse_predicate(
        'all_of(eq($item.fields.status, "fixed"), lt($item.fields.epss, "0.1"))'
    )
    assert evaluate_predicate(falsified, fields=invalid) == "false"
    either = parse_predicate(
        'any_of(eq($item.fields.status, "fixed"), ge($item.fields.epss, "0.5"))'
    )
    assert evaluate_predicate(either, fields=invalid) == "true"
    member = parse_predicate("in($item.fields.status, $parameters.wanted)")
    assert evaluate_predicate(member, fields=valid, parameters={"wanted": ["fixed"]}) == "true"


def test_absent_required_fields_are_unknown() -> None:
    contract = validate_contract(ROW)
    missing = check_record({"epss": "0.5"}, contract, record="/rows/0")
    present = parse_predicate("present($item.fields.status)")
    assert evaluate_predicate(present, fields=missing) == "unknown"


@pytest.mark.parametrize(
    "text",
    [
        "loop($item.fields.status)",
        'concat($item.fields.status, "x")',
        "eq($item.status, 1)",
    ],
)
def test_predicates_outside_the_closed_grammar_are_refused(text: str) -> None:
    with pytest.raises((ContractFormatError, ValueError)):
        parse_predicate(text)
