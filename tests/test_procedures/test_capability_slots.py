"""A Procedure's slot names one capability (contract batch, slots slice).

A slot is ``{capability, may_truncate}``. Authoring resolves the capability at
the base or in the same change set, pins it by digest, and types the call
against its request and result Contracts; it refuses an unknown capability and
a pin whose digest differs. The Procedure law reads each slot's effect from its
capability. Core recognises its builtin file read by the capability's name. A
Line binds each slot by its capability and fit (``tests/test_procedures/test_binding_fit.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.inputs import (
    CapabilityInput,
    ChangeSetInput,
    ProcedureInput,
)
from cruxible_client.contracts.capabilities import capability_digest
from cruxible_client.contracts.procedures.artifacts import (
    evaluate_procedure_law,
    procedure_path,
)
from cruxible_client.contracts.procedures.authored import CallStep
from cruxible_client.contracts.procedures.line_specs import (
    SlotCapabilityUnresolved,
    slot_needs,
)
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.shapes import Contract
from cruxible_core.authoring.lowering import AuthoringLoweringError
from tests.support.capabilities import (
    WORKSPACE_FILE,
    capability,
    capability_lookup,
    pinned_slot,
)
from tests.support.operation_world import (
    VALUE,
    author,
    author_procedure,
    compute_capability,
    contract_files,
    slotted_spec,
    world,
)
from tests.support.procedures import (
    accepted_procedure,
    procedure_definition,
    procedure_hard_caps,
    procedure_limits,
    procedure_spec,
)


def _head(instance: Any) -> dict[str, bytes]:
    return instance.tree_at(instance.accepted_coordinate().git_oid)


# -- the slot ---------------------------------------------------------------------------


def test_a_slot_is_one_capability_and_may_truncate_and_nothing_else() -> None:
    slot = ProcedureSlot.model_validate({"capability": "test.compute@1"})
    assert (slot.capability.spelling, slot.capability.digest, slot.may_truncate) == (
        "test.compute@1",
        None,
        False,
    )
    with pytest.raises(ValidationError):
        ProcedureSlot.model_validate(
            {"in": "test.value@1", "out": "test.value@1", "effect": "pure"}
        )
    with pytest.raises(ValidationError):
        ProcedureSlot.model_validate({"capability": "test.compute@1", "effect": "pure"})


def test_a_slot_need_is_its_resolved_capability() -> None:
    pure = compute_capability()
    needs = slot_needs(
        {"compute": ProcedureSlot.model_validate(pinned_slot(pure))},
        capabilities=capability_lookup(pure),
        builtin_names=frozenset(),
    )
    need = needs["compute"]
    assert (need.capability, need.spelling, need.may_truncate, need.builtin) == (
        pure,
        "test.compute@1",
        False,
        False,
    )
    with pytest.raises(SlotCapabilityUnresolved, match="'compute'.*test.compute@1"):
        slot_needs(
            {"compute": ProcedureSlot.model_validate(pinned_slot(pure))},
            capabilities=capability_lookup(),
            builtin_names=frozenset(),
        )


# -- authoring --------------------------------------------------------------------------


def test_authoring_pins_the_capability_and_lowers_the_slot_to_that_pin(tmp_path: Path) -> None:
    instance = world(tmp_path)
    content = author_procedure(instance, slotted_spec())
    from cruxible_client.contracts.procedures.artifacts import parse_procedure

    stored = parse_procedure(content, path=procedure_path("test.slotted"))
    pin = stored.definition.slots["compute"].capability
    assert (pin.name, pin.version, pin.digest) == (
        "test.compute",
        1,
        capability_digest(compute_capability()).tagged,
    )
    assert stored.definition.slots["compute"].model_dump(mode="json") == pinned_slot(
        compute_capability()
    )


def test_an_unknown_capability_is_refused_at_authoring_naming_it_and_the_slot(
    tmp_path: Path,
) -> None:
    instance = world(tmp_path)
    spec = slotted_spec().model_copy(
        update={"slots": {"compute": ProcedureSlot(capability="test.no-such@1")}}
    )
    before = _head(instance)
    with pytest.raises(AuthoringLoweringError) as refused:
        author_procedure(instance, spec)
    assert refused.value.code == "cruxible.authoring.slot_capability_unresolved"
    assert "test.no-such@1" in str(refused.value) and "'compute'" in str(refused.value)
    assert procedure_path("test.slotted") not in _head(instance)
    assert _head(instance) == before


def test_a_pin_whose_digest_differs_is_refused(tmp_path: Path) -> None:
    instance = world(tmp_path)
    wrong = {"name": "test.compute", "version": 1, "digest": "sha256:" + "0" * 64}
    spec = slotted_spec().model_copy(
        update={"slots": {"compute": ProcedureSlot.model_validate({"capability": wrong})}}
    )
    with pytest.raises(AuthoringLoweringError) as refused:
        author_procedure(instance, spec)
    assert refused.value.code == "cruxible.authoring.slot_capability_digest_mismatch"
    assert "test.compute@1" in str(refused.value)


def test_a_capability_authored_in_the_same_change_set_resolves(tmp_path: Path) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError as Refused
    from tests.core_support._identity_support import accept_tree

    instance = world(tmp_path)
    other = Contract.model_validate(
        {"name": "test.other", "version": 1, "fields": {"value": {"type": "int"}}}
    )
    accept_tree(instance, contract_files(other), name="other-contract")
    spec = procedure_spec(
        reference_text=True,
        procedure="test.same-set",
        params={"value": "int"},
        output={"value": "int"},
        slots={"compute": {"capability": "test.same-set@1"}},
        steps=[CallStep(call="result", slot="compute", input={"value": "$params.value"})],
        returns="result",
        hard_caps=procedure_limits(provider_calls=1),
    )
    member = CapabilityInput.model_validate(
        {
            "kind": "capability",
            "name": "test.same-set",
            "version": 1,
            "description": "Answers with another value.",
            "effect": "pure",
            "in": "test.value@1",
            "out": "test.other@1",
        }
    )
    with pytest.raises(Refused):
        author(instance, ProcedureInput(kind="procedure", spec=spec), name="alone")
    changed = author(
        instance,
        ChangeSetInput.model_validate(
            {
                "kind": "change_set",
                "rationale": "A capability and the Procedure naming it.",
                "members": [
                    member.model_dump(mode="json", by_alias=True),
                    {"kind": "procedure", "spec": spec.model_dump(mode="json", by_alias=True)},
                ],
            }
        ),
        name="same-set",
    )
    assert {"capabilities/test.same-set/v1.json", procedure_path("test.same-set")} <= set(changed)


def test_a_call_is_typed_against_the_capability_result(tmp_path: Path) -> None:
    instance = world(tmp_path)
    spec = procedure_spec(
        reference_text=True,
        procedure="test.typed",
        params={"value": "int"},
        output={"value": "int"},
        slots={"compute": {"capability": "test.compute@1"}},
        steps=[CallStep(call="result", slot="compute", input={"value": "$params.value"})],
        returns={"fields": {"value": "$result.missing"}},
        hard_caps=procedure_limits(provider_calls=1),
    )
    with pytest.raises(AuthoringLoweringError) as refused:
        author_procedure(instance, spec)
    assert "missing" in str(refused.value)


# -- the law ----------------------------------------------------------------------------


def _one_call(  # type: ignore[no-untyped-def]
    slot: dict[str, object],
    *,
    effect_policy: bool = False,
    acquires: bool = False,
    field: str = "capture",
):
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
    from cruxible_client.contracts.procedures.models import CallNode

    named = ArtifactNameRef(target=ArtifactIdentity(kind="EffectPolicy", name="test.policy"))
    capture = ArtifactNameRef(target=ArtifactIdentity(kind="CaptureContract", name="test.c"))
    return procedure_definition(
        "test.law",
        slots={"compute": slot},
        nodes=(
            CallNode(
                node_id="call",
                slot="compute",
                input={"value": 1},
                as_="result",
                **({"effect_policy": named} if effect_policy else {}),
                **({"captures": {field: capture}} if acquires else {}),
            ),
        ),
        hard_caps=procedure_hard_caps(preset="provider"),
    )


@pytest.mark.parametrize(
    ("effect", "effect_policy", "acquires", "refused"),
    [
        ("pure", False, False, False),
        ("external_mutation", True, False, False),
        ("external_mutation", False, False, True),
        ("pure", True, False, True),
        ("external_mutation", False, True, True),
    ],
)
def test_the_law_reads_each_slot_effect_from_its_capability(
    effect: str, effect_policy: bool, acquires: bool, refused: bool
) -> None:
    chosen = compute_capability(effect)
    procedure = accepted_procedure(
        _one_call(pinned_slot(chosen), effect_policy=effect_policy, acquires=acquires),
        bound=False,
    ).procedure
    law = evaluate_procedure_law(
        procedure,
        path=procedure_path("test.law"),
        predecessor=None,
        capability_lookup=capability_lookup(chosen),
    )
    if refused:
        assert law.verdict == "refused"
        assert law.diagnostics[0].code == "cruxible.procedure.slot_effect_mismatch"
    else:
        assert law.verdict == "accepted", law.diagnostics


def test_the_law_refuses_a_slot_whose_capability_is_not_accepted() -> None:
    procedure = accepted_procedure(_one_call(pinned_slot(compute_capability())), bound=False)
    law = evaluate_procedure_law(
        procedure.procedure,
        path=procedure_path("test.law"),
        predecessor=None,
        capability_lookup=capability_lookup(),
    )
    assert law.verdict == "refused"
    assert law.diagnostics[0].code == "cruxible.procedure.slot_capability_unresolved"
    assert "'compute'" in law.diagnostics[0].message


# -- the builtin ------------------------------------------------------------------------


def _workspace_read() -> Any:
    """A ``workspace.file@1`` over unrelated Contracts: recognition never reads them."""

    return capability(WORKSPACE_FILE, in_=VALUE, out=VALUE, effect="pure")


def test_the_builtin_is_recognised_by_capability_name_not_contract_digests() -> None:
    from cruxible_core.capabilities.artifact import BUILTIN_CAPABILITY_NAMES
    from cruxible_core.procedures.declared_inputs import reads_workspace

    assert WORKSPACE_FILE in BUILTIN_CAPABILITY_NAMES
    # Its Contracts are not the builtin's handoff: still the builtin, by name.
    assert reads_workspace(ProcedureSlot.model_validate(pinned_slot(_workspace_read())))
    # Another name over the same Contracts is an ordinary slot.
    lookalike = capability("test.file-lookalike", in_=VALUE, out=VALUE, effect="pure")
    assert not reads_workspace(ProcedureSlot.model_validate(pinned_slot(lookalike)))
    assert not reads_workspace(None)


def test_a_builtin_slot_binds_by_name_to_the_builtin_operation() -> None:
    """Whatever request its capability pins (the base kit's is the authored one), a
    slot naming a capability core's builtin serves is marked builtin: it binds by
    name, without a fit check (ruling B1)."""

    from cruxible_core.capabilities.artifact import BUILTIN_CAPABILITY_NAMES

    read = _workspace_read()
    for name in ("workspace.file", "workspace.files"):
        renamed = read.model_copy(update={"name": name})
        slot = ProcedureSlot.model_validate(pinned_slot(read))
        slot = slot.model_copy(
            update={"capability": slot.capability.model_copy(update={"name": name})}
        )
        (need,) = slot_needs(
            {"read": slot},
            capabilities=lambda ref, found=renamed: found,
            builtin_names=BUILTIN_CAPABILITY_NAMES,
        ).values()
        assert need.builtin and need.pin.name == name


def test_a_workspace_read_commits_core_bytes_as_its_one_capture_field() -> None:
    from cruxible_client.contracts.procedures.capture_outputs import procedure_capture_problem
    from cruxible_core.capabilities.artifact import BUILTIN_CAPABILITY_NAMES

    # A workspace.file version whose result declares a plain field, not the Capture.
    read = _workspace_read()
    definition = _one_call(pinned_slot(read), acquires=True, field="capture")

    def problem(builtins: Any) -> Any:
        return procedure_capture_problem(
            definition,
            contract_lookup=lambda ref: VALUE if ref.name == VALUE.name else None,
            capture_lookup=lambda name: object(),  # type: ignore[arg-type,return-value]
            capability_lookup=capability_lookup(read),
            builtin_capabilities=builtins,
        )

    # Recognised by name, the result must be the one capture field core commits.
    found = problem(BUILTIN_CAPABILITY_NAMES)
    assert found is not None and found[0] == "cruxible.procedure.captures_mismatch"
    assert "workspace.file@1" in found[1] and "core's builtin" in found[1]
    # The same slot not recognised is an ordinary call; nothing about core's field.
    ordinary = problem(())
    assert ordinary is not None and "core's builtin" not in ordinary[1]


def test_an_acquiring_call_whose_result_declares_no_capture_field_is_refused() -> None:
    """A Capture is a named field of the result Contract, never the whole result."""

    from cruxible_client.contracts.procedures.capture_outputs import procedure_capture_problem
    from cruxible_core.capabilities.artifact import BUILTIN_CAPABILITY_NAMES

    plain = compute_capability("external_read")
    definition = _one_call(pinned_slot(plain), acquires=True)
    found = procedure_capture_problem(
        definition,
        contract_lookup=lambda ref: VALUE if ref.name == VALUE.name else None,
        capture_lookup=lambda name: object(),  # type: ignore[arg-type,return-value]
        capability_lookup=capability_lookup(plain),
        builtin_capabilities=BUILTIN_CAPABILITY_NAMES,
    )
    assert found is not None and found[0] == "cruxible.procedure.captures_mismatch"
    assert "never the whole result" in found[1]
