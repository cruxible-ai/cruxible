"""Provider registration schema 3: operations, carried shapes, and the registration law."""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.provider_registration import (
    BuiltAgainstCapability,
    BuiltAgainstCaptureKind,
    DeclaredOperation,
    OperationSource,
    PackageRegistration,
    ProviderDeclares,
    RegistrationLawCode,
    capability_reserved_message,
    evaluate_declared_operations,
    evaluate_registration_law,
)
from cruxible_client.contracts.shapes import Contract, contract_digest, validate_contract

JSON = {"format": "json", "limits": {"decoded_bytes": 1024, "depth": 8, "records": 100}}
ROW = validate_contract({"name": "acme.hit", "version": 1, "fields": {"title": "string"}})
PARAM = validate_contract({"name": "acme.param", "version": 1, "fields": {"name": "string"}})
REQUEST = validate_contract(
    {
        "name": "acme.lookup.request",
        "version": 1,
        "fields": {
            "query": "string{max_len: 64}",
            "params": {
                "type": {
                    "kind": "list",
                    "max_items": 8,
                    "item": {
                        "kind": "contract",
                        "contract": {
                            "name": "acme.param",
                            "version": 1,
                            "digest": contract_digest(PARAM).tagged,
                        },
                    },
                },
                "optional": True,
            },
        },
    }
)
RESULT = validate_contract(
    {"name": "acme.lookup.result", "version": 1, "fields": {"document": "capture<acme.hits>"}}
)
DECLINED = validate_contract({"name": "acme.lookup.refusal.declined", "version": 1, "fields": {}})
KIND = {
    "name": "acme.hits",
    "decoding": JSON,
    "layout": {
        "records": {
            "hits": {
                "at": "/hits/*",
                "row": {"name": "acme.hit", "version": 1, "digest": contract_digest(ROW).tagged},
                "fields": {"title": {"at": "title"}},
            }
        }
    },
}


def _sorted(*contracts: Contract) -> list[dict[str, Any]]:
    ordered = sorted(contracts, key=lambda item: (item.name.encode("utf-8"), item.version))
    return [item.model_dump(mode="json") for item in ordered]


def _operation(**changes: Any) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "name": "acme.lookup",
        "description": "Looks one name up and returns the answer as one document.",
        "source": {"path": "src/acme_provider/lookup.py"},
        "serves": ["acme.lookup"],
        "in": "acme.lookup.request@1",
        "out": "acme.lookup.result@1",
        "effect": "external_read",
        "refusals": {"declined": {"contract": "acme.lookup.refusal.declined@1"}},
        "entrypoint": "acme_provider.lookup:Lookup",
    }
    operation.update(changes)
    return operation


def _declares(**changes: Any) -> dict[str, Any]:
    declares: dict[str, Any] = {
        "contracts": _sorted(ROW, PARAM, REQUEST, RESULT, DECLINED),
        "capture_kinds": [KIND],
    }
    declares.update(changes)
    return declares


def _registration(**changes: Any) -> PackageRegistration:
    data: dict[str, Any] = {
        "schema_version": 3,
        "manifest": {"path": "manifest.yaml", "digest": "sha256:" + "a" * 64},
        "operations": [_operation()],
        "declares": _declares(),
    }
    data.update(changes)
    return PackageRegistration.model_validate(data)


def _codes(registration: PackageRegistration) -> list[str]:
    return [problem.code for problem in evaluate_registration_law(registration)]


# -- shape ---------------------------------------------------------------------------------


def test_a_sound_registration_passes_the_law() -> None:
    registration = _registration()

    assert evaluate_registration_law(registration) == ()
    operation = registration.operations[0]
    assert operation.in_.spelling == "acme.lookup.request@1"
    assert operation.may_truncate is False
    dumped = operation.model_dump(mode="json")
    assert "may_truncate" not in dumped and "enumerates" not in dumped
    assert list(dumped) == [
        "name",
        "description",
        "source",
        "serves",
        "in",
        "out",
        "effect",
        "refusals",
        "entrypoint",
    ]
    assert [ref.spelling for ref in operation.contract_refs()] == [
        "acme.lookup.request@1",
        "acme.lookup.result@1",
        "acme.lookup.refusal.declined@1",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"enumerates": {"capture": "document", "selector": "/x", "fields": {"a": {"at": "a"}}}},
        {"description": ""},
        {"description": "trailing "},
        {"serves": []},
        {"serves": ["b.cap", "a.cap"]},
        {"serves": ["a.cap", "a.cap"]},
        {"serves": [f"acme.cap{index:02d}" for index in range(17)]},
        {"serves": ["Acme.lookup"]},
        {"source": {"path": "/abs/lookup.py"}},
        {"source": {"path": "src/../lookup.py"}},
        {"source": {"path": "src/lookup.py", "revision": "a" * 40}},
        {"source": {"path": "src/lookup.py", "repository": "git@github.com:acme/p.git"}},
        {"source": {"path": "src/lookup.py", "repository": "https://x.example", "revision": "v1"}},
        {"entrypoint": "acme_provider.lookup"},
        {"name": "Acme"},
        {"refusals": {"Declined": {"contract": "acme.lookup.refusal.declined@1"}}},
    ],
)
def test_an_operation_refuses_bad_shapes(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DeclaredOperation.model_validate(_operation(**changes))


def test_a_source_names_its_repository_and_full_revision() -> None:
    source = OperationSource(
        path="kits/acme/src/lookup.py",
        repository="https://github.com/acme/kits",
        revision="0123456789abcdef0123456789abcdef01234567",
    )

    assert source.model_dump(mode="json")["revision"].startswith("0123")
    assert OperationSource(path="lookup.py").model_dump(mode="json") == {"path": "lookup.py"}


def test_operations_and_declares_are_sorted_and_unique() -> None:
    with pytest.raises(ValidationError, match="sorted by name"):
        _registration(operations=[_operation(name="b.op"), _operation(name="a.op")])
    with pytest.raises(ValidationError, match="sorted by name and version"):
        ProviderDeclares.model_validate(_declares(contracts=_sorted(PARAM) + _sorted(ROW)))
    with pytest.raises(ValidationError, match="sorted by name, once each"):
        ProviderDeclares.model_validate(_declares(capture_kinds=[KIND, KIND]))
    with pytest.raises(ValidationError):
        ProviderDeclares.model_validate(_declares(contracts=[]))
    with pytest.raises(ValidationError):
        _registration(schema_version=2)


def test_built_against_records_capabilities_and_kinds_in_order() -> None:
    entries = [
        {"kind": "Capability", "name": "acme.lookup", "version": 1, "digest": "sha256:" + "1" * 64},
        {"kind": "Capability", "name": "acme.lookup", "version": 2, "digest": "sha256:" + "2" * 64},
        {"kind": "CaptureKind", "name": "acme.hits", "digest": "sha256:" + "3" * 64},
    ]
    declares = ProviderDeclares.model_validate(_declares(built_against=entries))

    assert [type(item) for item in declares.built_against] == [
        BuiltAgainstCapability,
        BuiltAgainstCapability,
        BuiltAgainstCaptureKind,
    ]
    with pytest.raises(ValidationError, match="sorted by kind"):
        ProviderDeclares.model_validate(_declares(built_against=entries[::-1]))
    with pytest.raises(ValidationError):
        ProviderDeclares.model_validate(
            _declares(
                built_against=[{"kind": "Contract", "name": "a", "digest": "sha256:" + "1" * 64}]
            )
        )


def test_the_carried_lookup_honours_a_pinned_digest() -> None:
    declares = ProviderDeclares.model_validate(_declares())
    resolve = declares.lookup()

    assert resolve(ROW.ref) == ROW
    assert resolve(ROW.ref.model_copy(update={"digest": contract_digest(ROW).tagged})) == ROW
    assert resolve(ROW.ref.model_copy(update={"digest": "sha256:" + "0" * 64})) is None
    assert declares.capture_kind("acme.hits") is not None
    assert declares.capture_kind("acme.other") is None


# -- the law -------------------------------------------------------------------------------


def test_the_law_codes_are_a_closed_vocabulary() -> None:
    assert set(get_args(RegistrationLawCode)) == {
        "cruxible.provider.capability_reserved",
        "cruxible.provider.declared_contract_missing",
        "cruxible.provider.declared_contract_mismatch",
        "cruxible.provider.declared_contract_unpinned",
        "cruxible.provider.declared_capture_kind_missing",
        "cruxible.provider.declared_unreached",
    }


def test_no_operation_serves_a_workspace_capability() -> None:
    registration = _registration(operations=[_operation(serves=["acme.lookup", "workspace.file"])])
    problems = evaluate_registration_law(registration)

    assert [problem.code for problem in problems] == ["cruxible.provider.capability_reserved"]
    assert capability_reserved_message("workspace.file") in problems[0].message
    assert capability_reserved_message("workspace.file") == "`workspace.file` is served by core."


def test_every_reference_resolves_inside_declares() -> None:
    missing = _registration(declares=_declares(contracts=_sorted(ROW, PARAM, REQUEST, RESULT)))
    assert _codes(missing) == ["cruxible.provider.declared_contract_missing"]

    pinned = _operation(
        **{"in": {"name": "acme.lookup.request", "version": 1, "digest": "sha256:" + "0" * 64}}
    )
    assert _codes(_registration(operations=[pinned])) == [
        "cruxible.provider.declared_contract_mismatch"
    ]

    exact = _operation(
        **{
            "in": {
                "name": "acme.lookup.request",
                "version": 1,
                "digest": contract_digest(REQUEST).tagged,
            }
        }
    )
    assert _codes(_registration(operations=[exact])) == []


def test_carried_contracts_pin_and_carry_their_nested_contracts() -> None:
    no_param = _registration(declares=_declares(contracts=_sorted(ROW, REQUEST, RESULT, DECLINED)))
    assert _codes(no_param) == ["cruxible.provider.declared_contract_missing"]

    unpinned_request = validate_contract(
        {
            "name": "acme.lookup.request",
            "version": 1,
            "fields": {"params": "list<acme.param@1>{max_items: 8}"},
        }
    )
    unpinned = _registration(
        declares=_declares(contracts=_sorted(ROW, PARAM, unpinned_request, RESULT, DECLINED))
    )
    assert _codes(unpinned) == ["cruxible.provider.declared_contract_unpinned"]


def test_every_named_capture_kind_is_carried_and_nothing_unreached_is() -> None:
    no_kind = _registration(declares=_declares(capture_kinds=[]))
    assert _codes(no_kind) == [
        "cruxible.provider.declared_capture_kind_missing",
        "cruxible.provider.declared_unreached",
    ]

    any_result = validate_contract(
        {"name": "acme.lookup.result", "version": 1, "fields": {"document": "capture<*>"}}
    )
    extra_kind = _registration(
        declares=_declares(contracts=_sorted(ROW, PARAM, REQUEST, any_result, DECLINED))
    )
    assert _codes(extra_kind) == [
        "cruxible.provider.declared_unreached",
        "cruxible.provider.declared_unreached",
    ]

    stray = validate_contract({"name": "acme.stray", "version": 1, "fields": {}})
    with_stray = _registration(
        declares=_declares(contracts=_sorted(ROW, PARAM, REQUEST, RESULT, DECLINED, stray))
    )
    problems = evaluate_registration_law(with_stray)
    assert [problem.code for problem in problems] == ["cruxible.provider.declared_unreached"]
    assert "acme.stray@1" in problems[0].message


def test_a_raw_kind_is_carried_without_a_layout() -> None:
    raw_result = validate_contract(
        {
            "name": "acme.lookup.result",
            "version": 1,
            "fields": {"responses": "list<capture<acme.raw>>{max_items: 4}"},
        }
    )
    registration = _registration(
        declares=_declares(
            contracts=_sorted(PARAM, REQUEST, raw_result, DECLINED),
            capture_kinds=[{"name": "acme.raw", "decoding": JSON}],
        )
    )

    assert evaluate_registration_law(registration) == ()


def test_a_carried_layout_passes_the_layout_law() -> None:
    bad_kind = {
        **KIND,
        "layout": {
            "records": {
                "hits": {
                    "at": "/hits/*",
                    "row": {
                        "name": "acme.hit",
                        "version": 1,
                        "digest": contract_digest(ROW).tagged,
                    },
                    "fields": {"score": {"at": "score"}},
                }
            }
        },
    }
    problems = evaluate_declared_operations(
        (DeclaredOperation.model_validate(_operation()),),
        ProviderDeclares.model_validate(_declares(capture_kinds=[bad_kind])),
    )

    assert [problem.code for problem in problems] == [
        "cruxible.capture_kind.layout_field_unknown",
        "cruxible.capture_kind.layout_field_missing",
    ]
    assert problems[0].message.startswith("carried CaptureKind `acme.hits`:")


# -- references inside a carried layout's field-rule types (review F-002) -------------------

PART = validate_contract({"name": "acme.meta-part", "version": 1, "fields": {"name": "string"}})
META = validate_contract(
    {
        "name": "acme.meta",
        "version": 1,
        "fields": {
            "part": {
                "type": {
                    "kind": "contract",
                    "contract": {
                        "name": "acme.meta-part",
                        "version": 1,
                        "digest": contract_digest(PART).tagged,
                    },
                }
            }
        },
    }
)
OTHER_ROW = validate_contract(
    {"name": "acme.other-row", "version": 1, "fields": {"note": "string"}}
)
OTHER_KIND = {
    "name": "acme.other",
    "decoding": JSON,
    "layout": {
        "records": {
            "notes": {
                "at": "/notes/*",
                "row": {
                    "name": "acme.other-row",
                    "version": 1,
                    "digest": contract_digest(OTHER_ROW).tagged,
                },
                "fields": {"note": {"at": "note"}},
            }
        }
    },
}
#: Every wrapper a field-rule type may put around a Contract reference.
META_SPELLINGS = (
    "acme.meta@1",
    "list<acme.meta@1>{max_items: 4}",
    "variant{one: acme.meta@1}",
    "field_result<acme.meta@1>",
    "record<acme.meta@1>",
)


def _kind_with(location: str, spelling: str) -> dict[str, Any]:
    """KIND with ``spelling`` as the type of a header field, a key part or a field rule."""

    hits = dict(KIND["layout"]["records"]["hits"])
    layout: dict[str, Any] = {"records": {"hits": hits}}
    if location == "header":
        layout["header"] = {"meta": {"at": "/meta", "type": spelling, "presence": "required"}}
    elif location == "key":
        hits["key"] = {"meta": {"at": "meta", "type": spelling}}
    else:
        hits["fields"] = {"title": {"at": "title", "type": spelling}}
    return {**KIND, "layout": layout}


@pytest.mark.parametrize("location", ["header", "key", "field"])
@pytest.mark.parametrize("spelling", META_SPELLINGS)
def test_a_contract_a_layout_rule_names_is_reached_and_must_be_carried(
    location: str, spelling: str
) -> None:
    kind = _kind_with(location, spelling)
    missing = _registration(declares=_declares(capture_kinds=[kind]))
    problems = evaluate_registration_law(missing)
    assert [problem.code for problem in problems] == ["cruxible.provider.declared_contract_missing"]
    assert "acme.meta@1" in problems[0].message

    carried = _registration(
        declares=_declares(
            contracts=_sorted(ROW, PARAM, REQUEST, RESULT, DECLINED, META, PART),
            capture_kinds=[kind],
        )
    )
    assert evaluate_registration_law(carried) == ()


def test_a_nested_dependency_of_a_layout_rule_contract_is_checked_too() -> None:
    kind = _kind_with("header", "acme.meta@1")
    without_part = _registration(
        declares=_declares(
            contracts=_sorted(ROW, PARAM, REQUEST, RESULT, DECLINED, META), capture_kinds=[kind]
        )
    )
    problems = evaluate_registration_law(without_part)

    assert [problem.code for problem in problems] == ["cruxible.provider.declared_contract_missing"]
    assert "acme.meta-part@1" in problems[0].message


def test_a_kind_a_layout_rule_names_is_reached_and_its_layout_followed() -> None:
    kind = _kind_with("header", "capture<acme.other>")
    missing = _registration(declares=_declares(capture_kinds=[kind]))
    assert _codes(missing) == ["cruxible.provider.declared_capture_kind_missing"]

    carried = _registration(
        declares=_declares(
            contracts=_sorted(ROW, PARAM, REQUEST, RESULT, DECLINED, OTHER_ROW),
            capture_kinds=[kind, OTHER_KIND],
        )
    )
    assert evaluate_registration_law(carried) == ()
