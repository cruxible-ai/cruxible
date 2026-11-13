"""A Contract with no fields is the closed empty record: it accepts only ``{}``.

Used for an empty selector, a refusal without detail and a parameterless
operation. It digests, renders and parses like any other Contract, and both
value checkers (``check_value`` through a nested pin, and the operation
checker's closure) accept ``{}`` and refuse any key.
"""

from __future__ import annotations

import pytest

from cruxible_client.contracts.provider_operations import (
    OperationValueRefused,
    closure_lookup,
    validate_operation_value,
)
from cruxible_client.contracts.shapes import (
    Contract,
    FieldInvalid,
    FieldPresent,
    check_value,
    contract_digest,
    contract_path,
    parse_contract,
    parse_type,
    pin_contract_refs,
    render_contract,
)

EMPTY = Contract.model_validate({"name": "test.empty", "version": 1, "fields": {}})


def _pinned(spelling: str, fields: dict[str, object]) -> Contract:
    """A Contract nesting ``test.empty@1``, pinned by digest as an accepted one is."""

    digest = contract_digest(EMPTY).tagged
    contract = Contract.model_validate({"name": spelling, "version": 1, "fields": fields})
    return pin_contract_refs(contract, lambda ref: ref.model_copy(update={"digest": digest}))


def test_an_empty_contract_renders_digests_and_parses_back() -> None:
    assert EMPTY.fields == {}
    path = contract_path("test.empty", 1)
    content = render_contract(EMPTY)
    assert b'"fields": {}' in content
    assert parse_contract(content, path=path) == EMPTY
    assert contract_digest(parse_contract(content, path=path)) == contract_digest(EMPTY)
    # A different empty Contract is a different artifact.
    other = Contract.model_validate({"name": "test.other-empty", "version": 1, "fields": {}})
    assert contract_digest(other) != contract_digest(EMPTY)


def test_an_empty_contract_accepts_only_the_empty_record() -> None:
    resolve = closure_lookup((EMPTY,))
    assert validate_operation_value({}, EMPTY, resolve=resolve) == {}
    with pytest.raises(OperationValueRefused) as caught:
        validate_operation_value({"anything": 1}, EMPTY, resolve=resolve)
    assert caught.value.problems == ("anything: undeclared field",)
    with pytest.raises(OperationValueRefused):
        validate_operation_value([], EMPTY, resolve=resolve)


def test_a_nested_empty_field_accepts_empty_and_refuses_any_key() -> None:
    outer = _pinned(
        "test.with-selector",
        {"selector": "test.empty@1", "selectors": "list<test.empty@1>{max_items: 4}"},
    )
    resolve = closure_lookup((EMPTY, outer))
    good = {"selector": {}, "selectors": [{}, {}]}
    assert validate_operation_value(good, outer, resolve=resolve) == good
    for bad, where in (
        ({"selector": {"x": 1}, "selectors": []}, "selector.x: undeclared field"),
        ({"selector": {}, "selectors": [{"y": None}]}, "selectors[0].y: undeclared field"),
    ):
        with pytest.raises(OperationValueRefused) as caught:
            validate_operation_value(bad, outer, resolve=resolve)
        assert where in caught.value.problems
    with pytest.raises(OperationValueRefused) as caught:
        validate_operation_value({"selector": None, "selectors": []}, outer, resolve=resolve)
    assert caught.value.problems == ("selector: type_mismatch",)
    # The field grammar alone: a record, any record shape, through its pin.
    field_type = outer.fields["selector"].type
    assert isinstance(check_value({}, field_type, at="/selector", resolve=resolve), FieldPresent)
    assert isinstance(check_value("x", field_type, at="/selector", resolve=resolve), FieldInvalid)
    assert parse_type("test.empty@1").kind == "contract"
