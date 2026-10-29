"""F06: closed-record validation recurses through the full B1 type grammar.

``validate_operation_value`` must reject an undeclared key at every record
boundary a value's declared type reaches, however deeply it is wrapped in
lists or chosen by a variant tag. ``_closed`` previously handled only a
direct nested Contract or one list of Contracts, so a deeper wrapper (a list
of lists, or a variant arm) escaped closure checking entirely.
"""

from __future__ import annotations

import pytest

from cruxible_client.contracts.provider_operations import (
    OperationValueRefused,
    closure_lookup,
    validate_operation_value,
)
from cruxible_client.contracts.shapes import Contract, contract_digest

INNER = Contract.model_validate(
    {"name": "test.inner", "version": 1, "fields": {"value": {"type": "int"}}}
)
OTHER = Contract.model_validate(
    {"name": "test.other", "version": 1, "fields": {"label": {"type": "string"}}}
)


def test_an_undeclared_key_two_lists_deep_is_refused():
    """``list<list<Contract>>``: F06's exact reproduction."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_list_of_lists",
            "version": 1,
            "fields": {
                "items": {"type": "list<list<test.inner@1>{max_items: 5}>{max_items: 5}"},
            },
        }
    )
    resolve = closure_lookup((INNER, outer))
    value = {"items": [[{"value": 1, "undeclared": "accepted"}]]}
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(value, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)
    # The declared shape alone still validates.
    validate_operation_value({"items": [[{"value": 1}]]}, outer, resolve=resolve)


def test_an_undeclared_key_in_a_variant_arm_is_refused():
    """A variant's chosen arm is one Contract; its record closes too."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_variant",
            "version": 1,
            "fields": {
                "choice": {
                    "type": {
                        "kind": "variant",
                        "arms": {
                            "inner": {"name": "test.inner", "version": 1},
                            "other": {"name": "test.other", "version": 1},
                        },
                    }
                },
            },
        }
    )
    resolve = closure_lookup((INNER, OTHER, outer))
    value = {"choice": {"tag": "inner", "value": 1, "undeclared": "accepted"}}
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(value, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)
    validate_operation_value({"choice": {"tag": "inner", "value": 1}}, outer, resolve=resolve)


def test_an_undeclared_key_in_a_list_of_variants_is_refused():
    """Wrapping combine: a list of variant-chosen records still closes."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_list_of_variants",
            "version": 1,
            "fields": {
                "choices": {
                    "type": {
                        "kind": "list",
                        "item": {
                            "kind": "variant",
                            "arms": {"inner": {"name": "test.inner", "version": 1}},
                        },
                        "max_items": 5,
                    }
                },
            },
        }
    )
    resolve = closure_lookup((INNER, outer))
    value = {"choices": [{"tag": "inner", "value": 1, "undeclared": "accepted"}]}
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(value, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)


# --- F06 fix round 2: the four B1 wrapper kinds -------------------------------

WRAPPER = Contract.model_validate(
    {"name": "test.wrapper", "version": 1, "fields": {"value": {"type": "test.inner@1"}}}
)


def test_an_undeclared_key_inside_a_field_results_present_value_is_refused():
    """Codex's exact reproduction: ``field_result<review.inner@1>`` accepted
    ``{'tag': 'present', 'at': '/value', 'value': {'value': 1, 'undeclared':
    'accepted'}}`` when ``review.inner`` declares only ``value: int``."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_field_result",
            "version": 1,
            "fields": {"wrapped": {"type": "field_result<test.inner@1>"}},
        }
    )
    resolve = closure_lookup((INNER, outer))
    bad = {"wrapped": {"tag": "present", "at": "/value", "value": {"value": 1, "undeclared": "x"}}}
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)
    good = {"wrapped": {"tag": "present", "at": "/value", "value": {"value": 1}}}
    validate_operation_value(good, outer, resolve=resolve)


def test_an_undeclared_key_inside_a_records_present_field_is_refused():
    """``record<C>``'s ``fields`` are each ``field_result<C.f>``; an
    undeclared key inside one field's present value must refuse too."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_record",
            "version": 1,
            "fields": {"wrapped": {"type": "record<test.wrapper@1>"}},
        }
    )
    resolve = closure_lookup((INNER, WRAPPER, outer))
    bad = {
        "wrapped": {
            "record": "claims/aa/x.json",
            "key": {},
            "fields": {
                "value": {
                    "tag": "present",
                    "at": "/value",
                    "value": {"value": 1, "undeclared": "x"},
                }
            },
        }
    }
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)
    good = {
        "wrapped": {
            "record": "claims/aa/x.json",
            "key": {},
            "fields": {"value": {"tag": "present", "at": "/value", "value": {"value": 1}}},
        }
    }
    validate_operation_value(good, outer, resolve=resolve)


def test_an_undeclared_key_inside_a_results_record_arm_is_refused():
    """``result<C>``'s ``record`` arm is a ``record<C>`` envelope; closure
    must traverse it too, not only ``invalid_record``/``duplicate``."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_result",
            "version": 1,
            "fields": {"wrapped": {"type": "result<test.wrapper@1>"}},
        }
    )
    resolve = closure_lookup((INNER, WRAPPER, outer))
    bad = {
        "wrapped": {
            "tag": "record",
            "record": {
                "record": "claims/aa/x.json",
                "key": {},
                "fields": {
                    "value": {
                        "tag": "present",
                        "at": "/value",
                        "value": {"value": 1, "undeclared": "x"},
                    }
                },
            },
        }
    }
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)


def test_an_undeclared_key_inside_a_refusals_body_is_refused():
    """``refusal<C>``'s ``body`` is a plain record of ``C`` (not
    field_result-wrapped); its own nested values still close."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_refusal",
            "version": 1,
            "fields": {"wrapped": {"type": "refusal<test.wrapper@1>"}},
        }
    )
    resolve = closure_lookup((INNER, WRAPPER, outer))
    wrapper_digest = contract_digest(WRAPPER).tagged
    contract_ref = {"name": "test.wrapper", "version": 1, "digest": wrapper_digest}
    bad = {
        "wrapped": {
            "code": "some_code",
            "contract": contract_ref,
            "body": {"value": {"value": 1, "undeclared": "x"}},
        }
    }
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)
    good = {
        "wrapped": {
            "code": "some_code",
            "contract": contract_ref,
            "body": {"value": {"value": 1}},
        }
    }
    validate_operation_value(good, outer, resolve=resolve)
