"""Ruling B1: a builtin capability slot binds only core's builtin, and the builtin no other slot.

Signatures do not decide it. An ordinary package build that copies the
builtin's handoff Contracts (so its signature is byte for byte the builtin's)
and serves a capability of its own is refused for a ``workspace.file`` slot,
at Line acceptance and again at run admission; and core's builtin operation is
refused for a slot naming any capability it does not serve, even one pinning
exactly its Contracts.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.capabilities import capability_pin
from cruxible_client.contracts.procedures.line_specs import (
    LINE_BINDING_BUILTIN_MISMATCH,
    SlotNeed,
    builtin_binding_problem,
    line_binding_problem,
    operation_spelling,
)
from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
from cruxible_client.contracts.providers import AcceptedProvider, provider_digest, provider_path
from cruxible_core.errors import RequestRefusedError
from cruxible_core.governance.seed_artifacts.workspace_file import (
    workspace_file_builtin_provider,
)
from tests.support.capabilities import capability
from tests.support.operation_world import operation, operation_build
from tests.test_procedures.test_b2_operations import SOURCE_LINE, accepted_line, sourced_world

#: The fixture Procedure's project step is executor-only (D-R37 h1).
pytestmark = pytest.mark.executor_only

IMPOSTER = "imposter-provider"


def _imposter() -> Any:
    """An ordinary build whose ``demo.read`` has the builtin's exact signature."""

    return operation_build(
        provider_id=IMPOSTER,
        operations=(
            operation(
                "demo.read",
                refusals={"provider_declined": True},
                signature=("workspace.file.request@1", "workspace.file.result@1"),
            ),
        ),
    )


@pytest.mark.xfail(strict=True, reason="owner: file-read")
def test_line_acceptance_refuses_an_imposter_for_a_builtin_slot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    imposter = _imposter()
    (served,) = imposter.operations
    assert served.serves == ("demo.read",)
    assert served.signature() == workspace_file_builtin_provider().operations[0].signature()

    with pytest.raises(Exception) as refused:
        sourced_world(tmp_path, bindings={"read": imposter.identity.name}, builds=(imposter,))

    message = str(refused.value)
    assert LINE_BINDING_BUILTIN_MISMATCH in message or "only core's builtin" in message
    assert "'read'" in message and "workspace.file@1" in message
    assert f"{imposter.identity.name}#demo.read" in message


@pytest.mark.xfail(strict=True, reason="owner: file-read")
def test_run_admission_refuses_an_imposter_for_a_builtin_slot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure, procedure_path
    from cruxible_core.procedures.reference_binding import bind_procedure_references

    imposter = _imposter()
    instance, _root = sourced_world(
        tmp_path, bindings={"read": "cruxible-builtin"}, builds=(imposter,)
    )
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        envelope = projection.typed.envelope("Procedure:test.sourced")
        procedure = projection.typed.source(envelope.identity)
        accepted = AcceptedProcedure(
            path=procedure_path("test.sourced"),
            procedure=procedure,
            artifact_digest=envelope.artifact_digest,
        )

        def lookup(identity):  # type: ignore[no-untyped-def]
            row = projection.typed.envelope(identity.qualified)
            source = projection.typed.source(identity.qualified)
            return None if row is None or source is None else (source, row.artifact_digest)

        offered = LineBinding(
            build=binding_pin(imposter.identity, provider_digest(imposter).tagged),
            operation="demo.read",
        )
        with pytest.raises(RequestRefusedError) as refused:
            bind_procedure_references(accepted, lookup=lookup, bindings={"read": offered})

    assert refused.value.error_code == LINE_BINDING_BUILTIN_MISMATCH
    assert "'read'" in str(refused.value) and "workspace.file@1" in str(refused.value)
    assert offered.spelling in str(refused.value)
    # The Line the world accepted binds the builtin, and still runs past admission.
    assert accepted_line(instance, SOURCE_LINE).bindings["read"].build.target.name == (
        "cruxible-builtin"
    )


def test_the_builtin_binds_no_slot_naming_another_capability() -> None:
    builtin = workspace_file_builtin_provider()
    accepted = AcceptedProvider(
        path=provider_path(builtin.identity.name),
        provider=builtin,
        artifact_digest=provider_digest(builtin).tagged,
    )
    (served,) = builtin.operations
    # A capability of another name pinning exactly the builtin's Contracts.
    lookalike = capability("demo.file-reader", in_=served.in_, out=served.out, effect=served.effect)
    slots = {"x": SlotNeed(pin=capability_pin(lookalike), capability=lookalike)}
    binding = LineBinding(
        build=binding_pin(builtin.identity, accepted.artifact_digest),
        operation="workspace.file",
    )
    problem = line_binding_problem(
        {"x": binding}, slots=slots, providers={builtin.identity.qualified: accepted}
    )
    assert problem is not None and problem[0] == LINE_BINDING_BUILTIN_MISMATCH
    spelled = operation_spelling(binding.build.target.name, binding.operation)
    assert "demo.file-reader@1" in problem[1] and spelled in problem[1]
    # The same slot over its own builtin capability binds, by name and unfitted.
    own = capability("workspace.files", in_=served.in_, out=served.out, effect=served.effect)
    builtin_slot = SlotNeed(pin=capability_pin(own), capability=own, builtin=True)
    assert (
        line_binding_problem(
            {"x": binding},
            slots={"x": builtin_slot},
            providers={builtin.identity.qualified: accepted},
        )
        is None
    )


def test_the_rule_reads_served_names_not_signatures() -> None:
    (builtin_operation,) = workspace_file_builtin_provider().operations
    (imposter_operation,) = _imposter().operations
    spelled = "x#y"
    assert (
        builtin_binding_problem(
            "read",
            capability="workspace.file@1",
            builtin=True,
            operation=builtin_operation,
            spelling=spelled,
        )
        is None
    )
    assert (
        builtin_binding_problem(
            "read",
            capability="workspace.file@1",
            builtin=True,
            operation=imposter_operation,
            spelling=spelled,
        )
        is not None
    )
    assert (
        builtin_binding_problem(
            "x",
            capability="demo.read@1",
            builtin=False,
            operation=imposter_operation,
            spelling=spelled,
        )
        is None
    )
    assert (
        builtin_binding_problem(
            "x",
            capability="demo.read@1",
            builtin=False,
            operation=builtin_operation,
            spelling=spelled,
        )
        is not None
    )
