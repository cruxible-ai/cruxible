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
from cruxible_client.contracts.shapes import Contract

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
