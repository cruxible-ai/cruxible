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


# --- F06 fix round 3: the wrapper envelopes themselves close too -------------


def test_an_undeclared_key_beside_a_field_results_envelope_is_refused():
    """Codex's exact reproduction: ``undeclared: retained`` beside the
    standard ``present`` members of a valid ``field_result`` envelope."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_field_result_envelope",
            "version": 1,
            "fields": {"wrapped": {"type": "field_result<test.inner@1>"}},
        }
    )
    resolve = closure_lookup((INNER, outer))
    good = {"wrapped": {"tag": "present", "at": "/value", "value": {"value": 1}}}
    validate_operation_value(good, outer, resolve=resolve)
    bad = {"wrapped": {**good["wrapped"], "undeclared": "retained"}}
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)
    # The "absent" and "invalid" arms close to their own (smaller) key sets too.
    with pytest.raises(OperationValueRefused):
        validate_operation_value(
            {"wrapped": {"tag": "absent", "undeclared": "x"}}, outer, resolve=resolve
        )
    with pytest.raises(OperationValueRefused):
        validate_operation_value(
            {
                "wrapped": {
                    "tag": "invalid",
                    "problem": {"code": "absent_required"},
                    "undeclared": "x",
                }
            },
            outer,
            resolve=resolve,
        )


def test_an_undeclared_key_beside_a_records_envelope_is_refused():
    """``record<C>``'s own envelope (``record``, ``key``, ``fields``) closes,
    not only the per-field present values inside ``fields``."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_record_envelope",
            "version": 1,
            "fields": {"wrapped": {"type": "record<test.inner@1>"}},
        }
    )
    resolve = closure_lookup((INNER, outer))
    good = {
        "wrapped": {
            "record": "claims/aa/x.json",
            "key": {},
            "fields": {"value": {"tag": "present", "at": "/value", "value": 1}},
        }
    }
    validate_operation_value(good, outer, resolve=resolve)
    bad = {"wrapped": {**good["wrapped"], "undeclared": "retained"}}
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)


def test_an_undeclared_key_beside_a_results_record_arm_envelope_is_refused():
    """``result<C>``'s "record" arm envelope (``tag``, ``record``) closes,
    not only the nested record<C> envelope it carries."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_result_envelope",
            "version": 1,
            "fields": {"wrapped": {"type": "result<test.inner@1>"}},
        }
    )
    resolve = closure_lookup((INNER, outer))
    good = {
        "wrapped": {
            "tag": "record",
            "record": {
                "record": "claims/aa/x.json",
                "key": {},
                "fields": {"value": {"tag": "present", "at": "/value", "value": 1}},
            },
        }
    }
    validate_operation_value(good, outer, resolve=resolve)
    bad = {"wrapped": {**good["wrapped"], "undeclared": "retained"}}
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)


def test_an_undeclared_key_beside_a_refusals_envelope_is_refused():
    """``refusal<C>``'s own envelope (``code``, ``contract``, ``body``)
    closes, not only the body's nested values."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_refusal_envelope",
            "version": 1,
            "fields": {"wrapped": {"type": "refusal<test.inner@1>"}},
        }
    )
    resolve = closure_lookup((INNER, outer))
    inner_digest = contract_digest(INNER).tagged
    contract_ref = {"name": "test.inner", "version": 1, "digest": inner_digest}
    good = {"wrapped": {"code": "some_code", "contract": contract_ref, "body": {"value": 1}}}
    validate_operation_value(good, outer, resolve=resolve)
    bad = {"wrapped": {**good["wrapped"], "undeclared": "retained"}}
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)


# --- F06a fix round 4: the refusal's Contract reference closes too ----------


def test_an_undeclared_key_inside_a_refusals_contract_reference_is_refused():
    """F06a: Codex's exact reproduction -- a refusal's typed Contract
    reference (``name``, ``version``, ``digest``) stayed open, so
    ``undeclared: retained`` beside the correct members passed through."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_refusal_contract_ref",
            "version": 1,
            "fields": {"wrapped": {"type": "refusal<test.inner@1>"}},
        }
    )
    resolve = closure_lookup((INNER, outer))
    inner_digest = contract_digest(INNER).tagged
    good_ref = {"name": "test.inner", "version": 1, "digest": inner_digest}
    good = {"wrapped": {"code": "some_code", "contract": good_ref, "body": {"value": 1}}}
    validate_operation_value(good, outer, resolve=resolve)
    bad_ref = {**good_ref, "undeclared": "retained"}
    bad = {"wrapped": {"code": "some_code", "contract": bad_ref, "body": {"value": 1}}}
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)


def test_an_undeclared_key_inside_a_nested_refusals_contract_reference_is_refused():
    """The same closure reached through a wrapping Contract (F06's
    "nested combinations" test gap)."""

    outer = Contract.model_validate(
        {
            "name": "test.outer_refusal_contract_ref_nested",
            "version": 1,
            "fields": {
                "choices": {
                    "type": "list<refusal<test.inner@1>>{max_items: 5}",
                },
            },
        }
    )
    resolve = closure_lookup((INNER, outer))
    inner_digest = contract_digest(INNER).tagged
    good_ref = {"name": "test.inner", "version": 1, "digest": inner_digest}
    good = {
        "choices": [{"code": "some_code", "contract": good_ref, "body": {"value": 1}}],
    }
    validate_operation_value(good, outer, resolve=resolve)
    bad_ref = {**good_ref, "undeclared": "retained"}
    bad = {
        "choices": [{"code": "some_code", "contract": bad_ref, "body": {"value": 1}}],
    }
    with pytest.raises(OperationValueRefused) as excinfo:
        validate_operation_value(bad, outer, resolve=resolve)
    assert "undeclared" in str(excinfo.value)
