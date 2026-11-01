"""The Contract type grammar: spellings, refusals, values, casts, edges and predicates."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cruxible_client.contracts.shapes import (
    ContractFormatError,
    ContractRef,
    ContractTypeRefused,
    FieldAbsent,
    FieldInvalid,
    FieldPresent,
    InvalidRecordArm,
    KeyValueRefused,
    Not,
    RecordArm,
    RefusalType,
    as_predicate,
    assignable,
    cast_record,
    cast_value,
    check_record,
    check_value,
    contract_digest,
    contract_path,
    edge_refusal,
    evaluate_contract_law,
    evaluate_predicate,
    parse_contract,
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
        "number",
        "number{0..1.5}",
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
        "key_part<security.vulnerability:cve_id>",
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


def test_a_json_refusal_names_the_typed_replacement() -> None:
    with pytest.raises(ContractTypeRefused, match="nested Contract") as caught:
        validate_contract({**ROW, "fields": {"x": "json"}})
    assert "capture<K>" in str(caught.value)


@pytest.mark.parametrize(
    ("value", "canonical"),
    [(1, 1), ("1", 1), ("1.0", 1), (Decimal("1.00"), 1), ("0.50", "0.5"), (-2, -2), ("1e2", 100)],
)
def test_a_number_encodes_as_an_integer_when_integral_else_canonical_decimal_text(
    value: object, canonical: object
) -> None:
    checked = check_value(value, parse_type("number"), at="/x", field="x")
    assert checked == FieldPresent(value=canonical, at="/x")


def test_equal_numbers_have_one_canonical_encoding_and_digest() -> None:
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.shapes import canonical_number

    assert canonical_number(1) == canonical_number("1.0") == canonical_number(Decimal("1.000"))
    one = {"n": canonical_number("1.0")}
    assert canonical_bytes(one) == canonical_bytes({"n": 1})
    assert canonical_bytes({"n": canonical_number("0.250")}) == canonical_bytes({"n": "0.25"})


@pytest.mark.parametrize("value", [1.5, True, "n/a", "NaN", "Infinity", None])
def test_a_number_refuses_floats_booleans_and_non_numbers(value: object) -> None:
    checked = check_value(value, parse_type("number"), at="/x", field="x")
    assert isinstance(checked, FieldInvalid) and checked.problem.code == "type_mismatch"


def test_a_number_refinement_bounds_the_value() -> None:
    bounded = parse_type("number{0..1}")
    assert isinstance(check_value("0.5", bounded, at="/x"), FieldPresent)
    refused = check_value(2, bounded, at="/x")
    assert isinstance(refused, FieldInvalid) and refused.problem.code == "out_of_range"
    assert edge_refusal(parse_type("number{0..1}"), parse_type("number{0..2}")) is None
    assert edge_refusal(parse_type("number{0..3}"), parse_type("number{0..2}")) is not None


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


def _f(name: str) -> dict[str, object]:
    """The current record's field ``name``: a typed item reference."""

    return {"$ref": "item", "path": ["fields", name]}


def _cmp(op: str, left: object, right: object) -> object:
    def operand(value: object) -> object:
        return value if isinstance(value, dict) else {"literal": value}

    return as_predicate({"op": op, "left": operand(left), "right": operand(right)})


def test_three_valued_predicates_over_an_invalid_field() -> None:
    contract = validate_contract(ROW)
    invalid = check_record({"epss": "0.5", "status": "bogus"}, contract, record="/rows/0")
    fixed = _cmp("eq", _f("status"), "fixed")
    assert evaluate_predicate(fixed, fields=invalid) == "unknown"
    assert evaluate_predicate(Not(of=fixed), fields=invalid) == "unknown"
    valid = check_record({"epss": "0.5", "status": "fixed"}, contract, record="/rows/1")
    assert evaluate_predicate(fixed, fields=valid) == "true"
    assert evaluate_predicate(
        as_predicate({"op": "not", "of": {"op": "present", "of": _f("percentile")}}), fields=valid
    ) == ("true")
    assert evaluate_predicate(_cmp("lt", _f("epss"), "0.7"), fields=valid) == "true"
    both = as_predicate({"op": "all_of", "of": [fixed, _cmp("lt", _f("epss"), "0.7")]})
    assert evaluate_predicate(both, fields=invalid) == "unknown"
    assert evaluate_predicate(both, fields=valid) == "true"
    falsified = as_predicate({"op": "all_of", "of": [fixed, _cmp("lt", _f("epss"), "0.1")]})
    assert evaluate_predicate(falsified, fields=invalid) == "false"
    either = as_predicate({"op": "any_of", "of": [fixed, _cmp("ge", _f("epss"), "0.5")]})
    assert evaluate_predicate(either, fields=invalid) == "true"
    member = as_predicate(
        {"op": "in", "left": _f("status"), "right": {"$ref": "param", "path": ["wanted"]}}
    )
    assert evaluate_predicate(member, fields=valid, parameters={"wanted": ["fixed"]}) == "true"


def test_absent_required_fields_are_unknown() -> None:
    contract = validate_contract(ROW)
    missing = check_record({"epss": "0.5"}, contract, record="/rows/0")
    present = as_predicate({"op": "present", "of": _f("status")})
    assert evaluate_predicate(present, fields=missing) == "unknown"


@pytest.mark.parametrize(
    "value",
    [
        'eq($item.fields.status, "fixed")',  # expression strings are not a predicate form
        {"op": "loop", "of": {"$ref": "item", "path": ["fields", "status"]}},
        {"op": "eq", "left": "$item.fields.status", "right": {"literal": 1}},
        {"op": "eq", "left": {"literal": 1.5}, "right": {"literal": 1}},
    ],
)
def test_predicates_outside_the_closed_grammar_are_refused(value: object) -> None:
    with pytest.raises((ContractFormatError, ValueError)):
        as_predicate(value)  # type: ignore[arg-type]


def test_a_record_predicate_reads_only_its_record_fields_and_parameters() -> None:
    from cruxible_client.contracts.shapes import predicate_context_problem

    record = as_predicate({"op": "present", "of": _f("status")})
    assert predicate_context_problem(record, "record") is None
    run = _cmp("gt", {"$ref": "step", "step": "rows", "path": ["count"]}, 0)
    assert "record's fields" in str(predicate_context_problem(run, "record"))
    assert predicate_context_problem(run, "run") is None
    assert "current item" in str(predicate_context_problem(record, "run"))


# -- F1: the acceptance law validates every pinned occurrence, not only the first --------


def test_the_contract_law_checks_every_pinned_occurrence_not_only_the_first() -> None:
    target = validate_contract({"name": "security.target", "version": 1, "fields": {"x": "string"}})
    target_path = contract_path("security.target", 1)
    target_bytes = render_contract(target)
    correct_digest = contract_digest(target).tagged
    wrong_digest = "sha256:" + "0" * 64
    outer = validate_contract(
        {
            "name": "security.outer",
            "version": 1,
            "fields": {
                # Two fields pin the same Contract version. The first carries the
                # right digest; the second -- which a naive dedupe-by-(name,version)
                # would drop -- carries a wrong one.
                "a": {
                    "type": {
                        "kind": "record",
                        "contract": {
                            "name": "security.target",
                            "version": 1,
                            "digest": correct_digest,
                        },
                    }
                },
                "b": {
                    "type": {
                        "kind": "record",
                        "contract": {
                            "name": "security.target",
                            "version": 1,
                            "digest": wrong_digest,
                        },
                    }
                },
            },
        }
    )
    # refs() still dedupes for dependency listing (get/orient/closure consumers).
    assert len(outer.refs()) == 1
    outer_path = contract_path("security.outer", 1)
    problems = evaluate_contract_law(
        outer,
        path=outer_path,
        lookup=lambda path: target_bytes if path == target_path else None,
        parent_content=None,
        content=render_contract(outer),
    )
    assert any(p.code == "cruxible.contract.pin_digest_mismatch" for p in problems)


def test_parse_contract_requires_a_digest_on_every_occurrence() -> None:
    correct_digest = "sha256:" + "1" * 64
    outer = validate_contract(
        {
            "name": "security.outer2",
            "version": 1,
            "fields": {
                "a": {
                    "type": {
                        "kind": "record",
                        "contract": {
                            "name": "security.target",
                            "version": 1,
                            "digest": correct_digest,
                        },
                    }
                },
                # The second occurrence of the same pin carries no digest at all.
                "b": {
                    "type": {
                        "kind": "record",
                        "contract": {"name": "security.target", "version": 1},
                    }
                },
            },
        }
    )
    with pytest.raises(ContractFormatError):
        parse_contract(render_contract(outer), path=contract_path("security.outer2", 1))


# -- F2: the generic envelopes enforce their type parameter ------------------------------


def test_check_value_enforces_the_generic_type_parameter() -> None:
    demo = validate_contract({"name": "security.demo", "version": 1, "fields": {"n": "int"}})

    def resolve(ref: object) -> object:
        return demo if (ref.name, ref.version) == ("security.demo", 1) else None  # type: ignore[attr-defined]

    bad_field_result = {"tag": "present", "value": "not-an-int", "at": "/x"}
    checked = check_value(
        bad_field_result, parse_type("field_result<int>"), at="/x", resolve=resolve
    )
    assert isinstance(checked, FieldInvalid)

    ok_field_result = {"tag": "present", "value": 3, "at": "/x"}
    checked = check_value(
        ok_field_result, parse_type("field_result<int>"), at="/x", resolve=resolve
    )
    assert isinstance(checked, FieldPresent)

    empty_record = {"record": "/r/0", "key": {}, "fields": {}}
    checked = check_value(
        empty_record, parse_type("record<security.demo@1>"), at="/r", resolve=resolve
    )
    assert isinstance(checked, FieldInvalid)

    demo_digest = contract_digest(demo).tagged
    pinned_refusal_type = RefusalType(
        contract=ContractRef(name="security.demo", version=1, digest=demo_digest)
    )

    other_contract_refusal = {
        "code": "any_code",
        "contract": {"name": "security.other", "version": 9, "digest": demo_digest},
        "body": {"whatever": True},
    }
    checked = check_value(other_contract_refusal, pinned_refusal_type, at="/f", resolve=resolve)
    assert isinstance(checked, FieldInvalid)

    valid_refusal = {
        "code": "any_code",
        "contract": {"name": "security.demo", "version": 1, "digest": demo_digest},
        "body": {"n": 3},
    }
    checked = check_value(valid_refusal, pinned_refusal_type, at="/f", resolve=resolve)
    assert isinstance(checked, FieldPresent)


def test_refusal_values_claimed_digest_must_match_the_pinned_contract() -> None:
    """A refusal<C> value's own ``contract.digest`` is checked, not only name/version."""

    demo = validate_contract({"name": "security.demo", "version": 1, "fields": {"n": "int"}})
    real_digest = contract_digest(demo).tagged
    wrong_digest = "sha256:" + "0" * 64

    def resolve(ref: object) -> object:
        return demo if (ref.name, ref.version) == ("security.demo", 1) else None  # type: ignore[attr-defined]

    pinned_refusal_type = RefusalType(
        contract=ContractRef(name="security.demo", version=1, digest=real_digest)
    )

    wrong_digest_refusal = {
        "code": "any_code",
        "contract": {"name": "security.demo", "version": 1, "digest": wrong_digest},
        "body": {"n": 3},
    }
    checked = check_value(wrong_digest_refusal, pinned_refusal_type, at="/f", resolve=resolve)
    assert isinstance(checked, FieldInvalid)

    missing_digest_refusal = {
        "code": "any_code",
        "contract": {"name": "security.demo", "version": 1},
        "body": {"n": 3},
    }
    checked = check_value(missing_digest_refusal, pinned_refusal_type, at="/f", resolve=resolve)
    assert isinstance(checked, FieldInvalid)

    correct_digest_refusal = {
        "code": "any_code",
        "contract": {"name": "security.demo", "version": 1, "digest": real_digest},
        "body": {"n": 3},
    }
    checked = check_value(correct_digest_refusal, pinned_refusal_type, at="/f", resolve=resolve)
    assert isinstance(checked, FieldPresent)


# -- key<K> / key_part<K:p>: SubjectKind identity rules (R12, D4) -------------------------


class _Rules:
    """A stand-in for core's identity rules: ``pkg`` keys fold to lowercase."""

    def canonical(self, subject_kind: str, part: str | None, value: object) -> object:
        if subject_kind != "pkg.package" or not isinstance(value, str) or not value:
            raise KeyValueRefused(f"{value!r} is not a key of {subject_kind}")
        if part not in (None, "name", "pkg.package.name"):
            raise KeyValueRefused(f"{part!r} is not a key of {subject_kind}")
        return value.lower()

    def rule_token(self, subject_kind: str) -> object:
        return {"lower": subject_kind}


def test_key_type_without_identity_rules_fails_closed_on_every_value() -> None:
    """Without core's identity rules no value can be confirmed as a key."""

    for value in ("CVE-2024-1234", "   ", {"undeclared": [{"opaque": True}]}):
        checked = check_value(value, parse_type("key<security.vulnerability>"), at="/k")
        assert isinstance(checked, FieldInvalid)
        assert checked.problem.code == "key_unparseable"


def test_a_checked_key_must_already_be_canonical() -> None:
    """A key crossing a Contract boundary states its canonical key, as a string states NFC."""

    rules = _Rules()
    for spelling in ("key<pkg.package>", "key_part<pkg.package:name>"):
        field_type = parse_type(spelling)
        present = check_value("django", field_type, at="/k", keys=rules)
        assert isinstance(present, FieldPresent) and present.value == "django"
        respelled = check_value("Django", field_type, at="/k", keys=rules)
        assert isinstance(respelled, FieldInvalid)
        assert respelled.problem.code == "canonical_form_failed"
        unparseable = check_value("", field_type, at="/k", keys=rules)
        assert isinstance(unparseable, FieldInvalid)
        assert unparseable.problem.code == "key_unparseable"
    wrong_part = check_value("x", parse_type("key_part<pkg.package:version>"), at="/k", keys=rules)
    assert isinstance(wrong_part, FieldInvalid) and wrong_part.problem.code == "key_unparseable"


def test_a_shape_cast_to_a_key_canonicalizes_it() -> None:
    """``as: key<K>`` is an explicit conversion: two raw keys become one canonical key."""

    rules = _Rules()
    to = parse_type("key<pkg.package>")
    for raw in ("Django", "DJANGO", "django"):
        cast = cast_value(FieldPresent(value=raw, at="/k"), to, field="k", keys=rules)
        assert isinstance(cast, FieldPresent) and cast.value == "django"
    failed = cast_value(FieldPresent(value="", at="/k"), to, field="k", keys=rules)
    assert isinstance(failed, FieldInvalid) and failed.problem.code == "type_mismatch"
    closed = cast_value(FieldPresent(value="Django", at="/k"), to, field="k")
    assert isinstance(closed, FieldInvalid) and closed.problem.code == "type_mismatch"


def test_key_types_are_edges_only_to_the_same_key() -> None:
    whole, part = parse_type("key<pkg.package>"), parse_type("key_part<pkg.package:name>")
    assert assignable(whole, whole) and assignable(part, part)
    assert not assignable(part, whole)
    assert not assignable(part, parse_type("key_part<pkg.package:version>"))
    assert not assignable(parse_type("string"), whole)


def _subject_kind_bytes(*, minted: bool = False, live: bool = True) -> bytes:
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
    from cruxible_client.contracts.identity_normalizers import Pep503Normalizer
    from cruxible_client.contracts.subject_kinds import (
        SubjectKeyRule,
        SubjectKind,
        SubjectKindKeys,
        SubjectKindMinted,
        render_subject_kind,
    )

    rule = (
        SubjectKindMinted()
        if minted
        else SubjectKindKeys(
            parts=(
                SubjectKeyRule(
                    claim_type="pkg.package.name", normalizer=Pep503Normalizer(version=1)
                ),
            ),
            uniqueness="unique",
        )
    )
    return render_subject_kind(
        SubjectKind(
            identity=ArtifactIdentity(kind="SubjectKind", name="pkg.package"),
            identity_rule=rule,
            lifecycle=ArtifactLifecycle(state="live" if live else "retired"),
        )
    )


@pytest.mark.parametrize(
    ("spelling", "kind_bytes", "accepted"),
    [
        ("key<pkg.package>", "keyed", True),
        ("key_part<pkg.package:name>", "keyed", True),
        ("key_part<pkg.package:pkg.package.name>", "keyed", True),
        ("list<key_part<pkg.package:name>>{max_items: 10}", "keyed", True),
        ("key_part<pkg.package:version>", "keyed", False),
        ("key<pkg.package>", None, False),
        ("key<pkg.package>", "retired", False),
        ("key<pkg.package>", "minted", False),
    ],
)
def test_a_contract_key_field_names_a_live_keyed_subject_kind(
    spelling: str, kind_bytes: str | None, accepted: bool
) -> None:
    """A Contract may declare key<K> / key_part<K:p>; its law checks K's identity rules exist."""

    contract = validate_contract({**ROW, "fields": {"k": spelling}})
    content = {
        None: None,
        "keyed": _subject_kind_bytes(),
        "retired": _subject_kind_bytes(live=False),
        "minted": _subject_kind_bytes(minted=True),
    }[kind_bytes]
    problems = evaluate_contract_law(
        contract,
        path=contract_path(contract.name, contract.version),
        lookup=lambda path: content if path == "subject-kinds/pkg.package.json" else None,
        parent_content=None,
        content=render_contract(contract),
    )
    if accepted:
        assert problems == ()
    else:
        assert [problem.code for problem in problems] == ["cruxible.contract.key_kind_invalid"]


# -- F10: an unresolved enum_of fails closed, never open ---------------------------------


def test_unresolved_enum_of_fails_closed() -> None:
    field_type = parse_type("enum_of security.vulnerability.kev_ransomware_use")
    checked = check_value("no-such-member", field_type, at="/x")
    assert isinstance(checked, FieldInvalid)
    assert checked.problem.code == "not_in_enum"
    checked = check_value(
        "no-such-member", field_type, at="/x", enum_members=lambda _claim_type: None
    )
    assert isinstance(checked, FieldInvalid)
    assert checked.problem.code == "not_in_enum"


# -- F9: a decimal bound keeps its full declared precision -------------------------------


def test_a_decimal_bound_keeps_its_full_declared_precision() -> None:
    long_bound = "0.12345678901234567890123456789"
    bounded = parse_type(f"decimal{{{long_bound}..}}")
    assert bounded.min == long_bound


# -- F8: predicate dispatch evaluates only the needed operator ---------------------------


def test_eq_is_not_defeated_by_an_unsupported_ordering_operator() -> None:
    contract = validate_contract(
        {
            "name": "security.duration-row",
            "version": 1,
            "fields": {"d1": "duration", "d2": "duration"},
        }
    )
    record = check_record(
        {"d1": {"microseconds": 5_000_000}, "d2": {"microseconds": 5_000_000}},
        contract,
        record="/rows/0",
    )
    predicate = _cmp("eq", _f("d1"), _f("d2"))
    assert evaluate_predicate(predicate, fields=record) == "true"


def test_decimal_comparison_with_nan_does_not_raise() -> None:
    contract = validate_contract(
        {"name": "security.decimal-row", "version": 1, "fields": {"x": "decimal"}}
    )
    record = check_record({"x": "0.5"}, contract, record="/rows/0")
    predicate = _cmp("eq", _f("x"), "NaN")
    assert evaluate_predicate(predicate, fields=record) == "false"
