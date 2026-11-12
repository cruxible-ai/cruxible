"""The Capability artifact: shape, path, digest, canonical form, law, and the slot."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.canonical import typed_digest
from cruxible_client.contracts.capabilities import (
    CAPABILITY_DIGEST_DOMAIN,
    Capability,
    CapabilityFormatError,
    CapabilityLawCode,
    CapabilityRef,
    CapabilitySlot,
    capability_digest,
    capability_identity,
    capability_path,
    capability_path_for_identity,
    capability_path_parts,
    capability_pin,
    capability_ref_for_identity,
    capability_signature,
    capability_slot_signature,
    evaluate_capability_law,
    parse_capability,
    parse_capability_ref,
    render_capability,
    tree_capability_resolver,
)
from cruxible_client.contracts.shapes import (
    CONTRACT_DIGEST_DOMAIN,
    Contract,
    contract_digest,
    contract_path,
    render_contract,
    validate_contract,
)

BUILTIN = frozenset({"workspace.file"})


def _contract(name: str, fields: dict[str, object], *, version: int = 1) -> Contract:
    return validate_contract({"name": name, "version": version, "fields": fields})


REQUEST = _contract("acme.lookup.request", {"query": "string{max_len: 64}"})
RESULT = _contract("acme.lookup.result", {"document": "capture<*>"})


def _pin(contract: Contract) -> dict[str, object]:
    return {
        "name": contract.name,
        "version": contract.version,
        "digest": contract_digest(contract).tagged,
    }


def _capability(**changes: object) -> Capability:
    data: dict[str, object] = {
        "name": "acme.lookup",
        "version": 1,
        "description": "Looks one name up and returns the answer as one Capture.",
        "effect": "external_read",
        "in": _pin(REQUEST),
        "out": _pin(RESULT),
    }
    data.update(changes)
    return Capability.model_validate(data)


def _tree(*capabilities: Capability, contracts: tuple[Contract, ...] = (REQUEST, RESULT)):
    tree = {contract_path(item.name, item.version): render_contract(item) for item in contracts}
    tree.update(
        {capability_path(item.name, item.version): render_capability(item) for item in capabilities}
    )
    return tree


def _law(capability: Capability, tree: dict[str, bytes], *, parent: bytes | None = None):
    path = capability_path(capability.name, capability.version)
    return evaluate_capability_law(
        capability,
        path=path,
        lookup=tree.get,
        parent_content=parent,
        content=render_capability(capability),
        builtin_capabilities=BUILTIN,
    )


def _codes(problems) -> list[str]:
    return [problem.code for problem in problems]


# -- shape -------------------------------------------------------------------------------


def test_a_capability_reads_name_at_n_pins_and_keeps_the_in_alias() -> None:
    capability = _capability(**{"in": "acme.lookup.request@1", "out": "acme.lookup.result@1"})

    assert capability.in_.spelling == "acme.lookup.request@1"
    assert capability.in_.digest is None
    assert not capability.pinned
    dumped = capability.model_dump(mode="json")
    assert set(dumped) == {
        "artifact_format",
        "name",
        "version",
        "description",
        "effect",
        "in",
        "out",
    }
    assert dumped["artifact_format"] == "cruxible-capability-v1"


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Acme.lookup"},
        {"name": ""},
        {"description": ""},
        {"description": " padded "},
        {"effect": "side_effect"},
        {"version": 2},
        {"predecessor_digest": "sha256:" + "0" * 64},
        {"enumerates": {}},
    ],
)
def test_a_capability_refuses_bad_shapes(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _capability(**changes)


def test_a_later_version_names_its_predecessor() -> None:
    later = _capability(version=2, predecessor_digest="sha256:" + "a" * 64)

    assert later.predecessor_digest == "sha256:" + "a" * 64


def test_the_path_identity_and_ref_helpers_agree() -> None:
    assert capability_path("acme.lookup", 3) == "capabilities/acme.lookup/v3.json"
    assert capability_path_parts("capabilities/acme.lookup/v3.json") == ("acme.lookup", 3)
    assert capability_path_parts("contracts/acme.lookup/v3.json") is None
    assert capability_path_parts("capabilities/acme.lookup/v0.json") is None
    identity = capability_identity("acme.lookup", 3)
    assert identity.qualified == "Capability:acme.lookup/v3"
    assert capability_ref_for_identity(identity) == CapabilityRef(name="acme.lookup", version=3)
    assert capability_path_for_identity("acme.lookup/v3") == "capabilities/acme.lookup/v3.json"
    assert parse_capability_ref(" acme.lookup@3 ") == CapabilityRef(name="acme.lookup", version=3)
    with pytest.raises(CapabilityFormatError):
        parse_capability_ref("acme.lookup")
    with pytest.raises(CapabilityFormatError):
        capability_path("Bad", 1)


def test_the_digest_has_its_own_domain_and_covers_the_description() -> None:
    capability = _capability()
    payload = capability.model_dump(mode="json")

    assert capability_digest(capability) == typed_digest(
        type(capability_digest(capability)), CAPABILITY_DIGEST_DOMAIN, payload
    )
    assert CAPABILITY_DIGEST_DOMAIN != CONTRACT_DIGEST_DOMAIN
    reworded = _capability(description="Looks one name up; returns one Capture.")
    assert capability_digest(reworded) != capability_digest(capability)
    assert capability_pin(capability).digest == capability_digest(capability).tagged


def test_parse_is_strict_and_canonical() -> None:
    capability = _capability()
    path = capability_path(capability.name, 1)
    content = render_capability(capability)

    assert parse_capability(content, path=path) == capability
    with pytest.raises(CapabilityFormatError, match="canonical wire form"):
        parse_capability(content.rstrip(b"\n"), path=path)
    with pytest.raises(CapabilityFormatError, match="identity/path"):
        parse_capability(content, path="capabilities/other/v1.json")
    unpinned = _capability(**{"in": "acme.lookup.request@1"})
    with pytest.raises(CapabilityFormatError, match="pins both Contracts"):
        parse_capability(render_capability(unpinned), path=path)
    with pytest.raises(CapabilityFormatError):
        parse_capability(b"{", path=path)


def test_the_tree_resolver_honours_a_pinned_digest() -> None:
    capability = _capability()
    resolve = tree_capability_resolver(_tree(capability))

    assert resolve(CapabilityRef(name="acme.lookup", version=1)) == capability
    assert resolve(capability_pin(capability)) == capability
    wrong = CapabilityRef(name="acme.lookup", version=1, digest="sha256:" + "f" * 64)
    assert resolve(wrong) is None
    assert resolve(CapabilityRef(name="acme.lookup", version=2)) is None


def test_the_signature_is_the_capability_in_out_and_effect() -> None:
    signature = capability_signature(_capability())

    assert signature.in_.digest == contract_digest(REQUEST).tagged
    assert signature.out.digest == contract_digest(RESULT).tagged
    assert signature.effect == "external_read"


# -- the law -------------------------------------------------------------------------------


def test_the_law_accepts_a_first_version_whose_pins_resolve() -> None:
    capability = _capability()

    assert _law(capability, _tree(capability)) == ()


def test_the_law_codes_are_a_closed_vocabulary() -> None:
    assert set(get_args(CapabilityLawCode)) == {
        "cruxible.capability.immutable",
        "cruxible.capability.predecessor_missing",
        "cruxible.capability.predecessor_mismatch",
        "cruxible.capability.effect_changed",
        "cruxible.capability.pin_unresolved",
        "cruxible.capability.pin_digest_mismatch",
        "cruxible.capability.reserved",
    }


def test_an_accepted_version_is_immutable() -> None:
    accepted = _capability()
    edited = _capability(description="Looks a name up.")

    assert _codes(_law(edited, _tree(edited), parent=render_capability(accepted))) == [
        "cruxible.capability.immutable"
    ]


def test_a_successor_names_the_accepted_predecessor_digest_and_keeps_the_effect() -> None:
    first = _capability()
    expected = capability_digest(first).tagged
    second = _capability(version=2, predecessor_digest=expected)
    assert _law(second, _tree(first, second)) == ()

    wrong = _capability(version=2, predecessor_digest="sha256:" + "b" * 64)
    assert _codes(_law(wrong, _tree(first, wrong))) == ["cruxible.capability.predecessor_mismatch"]

    missing = _capability(version=3, predecessor_digest=expected)
    assert _codes(_law(missing, _tree(first, missing))) == [
        "cruxible.capability.predecessor_missing"
    ]

    pure = _capability(version=2, predecessor_digest=expected, effect="pure")
    problems = _law(pure, _tree(first, pure))
    assert _codes(problems) == ["cruxible.capability.effect_changed"]
    assert "new capability name" in problems[0].message


def test_both_pins_resolve_to_the_exact_accepted_contracts() -> None:
    capability = _capability()
    assert _codes(_law(capability, _tree(capability, contracts=(REQUEST,)))) == [
        "cruxible.capability.pin_unresolved"
    ]

    stale = _capability(**{"out": {**_pin(RESULT), "digest": "sha256:" + "c" * 64}})
    assert _codes(_law(stale, _tree(stale))) == ["cruxible.capability.pin_digest_mismatch"]

    unpinned = _capability(**{"in": "acme.lookup.request@1"})
    assert _codes(_law(unpinned, _tree(unpinned))) == ["cruxible.capability.pin_digest_mismatch"]


def test_workspace_names_are_core_s_but_any_version_of_a_builtin_name_is_allowed() -> None:
    builtin = _capability(name="workspace.file", effect="pure")
    assert _law(builtin, _tree(builtin)) == ()

    second = _capability(
        name="workspace.file",
        effect="pure",
        version=2,
        predecessor_digest=capability_digest(builtin).tagged,
    )
    assert _law(second, _tree(builtin, second)) == ()

    invented = _capability(name="workspace.folder", effect="pure")
    problems = _law(invented, _tree(invented))
    assert _codes(problems) == ["cruxible.capability.reserved"]
    assert "workspace.file" in problems[0].message


# -- the slot ------------------------------------------------------------------------------


def test_a_slot_names_one_capability_pin_and_may_truncate() -> None:
    slot = CapabilitySlot.model_validate({"capability": "acme.lookup@1"})

    assert slot.capability == CapabilityRef(name="acme.lookup", version=1)
    assert slot.may_truncate is False
    pinned = CapabilitySlot.model_validate(
        {"capability": capability_pin(_capability()).model_dump(mode="json"), "may_truncate": True}
    )
    assert pinned.capability.digest == capability_digest(_capability()).tagged
    assert pinned.model_dump(mode="json")["capability"]["digest"] == pinned.capability.digest
    with pytest.raises(ValidationError):
        CapabilitySlot.model_validate({"capability": "acme.lookup@1", "in": "a@1"})


def test_a_slot_signature_is_derived_from_its_capability() -> None:
    capability = _capability()
    resolve = tree_capability_resolver(_tree(capability))
    slot = CapabilitySlot(capability=capability_pin(capability))

    assert capability_slot_signature(slot, resolve) == capability_signature(capability)
    missing = CapabilitySlot.model_validate({"capability": "acme.other@1"})
    assert capability_slot_signature(missing, resolve) is None
