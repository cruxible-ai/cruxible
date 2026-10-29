"""A slotted Call bound by a Line, and the real proposal path over them.

A Procedure names no provider: its one Call names slot ``compute``, and a
Line binds it to the demo build's ``compute`` operation. The helpers here
(``_accepted_procedure``, ``_line``) are shared fixtures for "one accepted
Procedure that calls the demo Provider".
"""

from __future__ import annotations

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    evaluate_procedure_law,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.line_specs import (
    LineSpec,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
)
from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
from cruxible_client.contracts.providers import render_provider
from cruxible_client.contracts.shapes import contract_digest
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from tests.core_support._p2b1_support import accepted_provider, pin
from tests.core_support._support import initialize_local
from tests.support.operation_world import VALUE, contract_files
from tests.support.procedures import MAX_WALL_CLOCK, RUN_WALL_CLOCK, accepted_procedure, named


def _value_ref() -> dict[str, object]:
    return {"name": VALUE.name, "version": VALUE.version, "digest": contract_digest(VALUE).tagged}


def _definition() -> ProcedureDefinition:
    contract_in = pin("contract-in", "Contract", "provider-input")
    contract_out = pin("contract-out", "Contract", "provider-output")
    return ProcedureDefinition.model_validate(
        {
            "name": "provider-v4",
            "contract_in": named(contract_in),
            "contract_out": named(contract_out),
            "slots": {"compute": {"in": _value_ref(), "out": _value_ref(), "effect": "pure"}},
            "nodes": (
                CallNode(
                    node_id="direct",
                    slot="compute",
                    input={"value": 1},
                    as_="result",
                ),
            ),
            "returns": "result",
            "budget": ProcedureBudget(
                wall_clock=RUN_WALL_CLOCK,
                max_provider_calls=2,
                max_capture_bytes=1024,
                max_items=10,
            ),
            "hard_caps": ProcedureHardCaps(
                max_wall_clock=MAX_WALL_CLOCK,
                max_provider_calls=4,
                max_capture_bytes=2048,
                max_items=20,
                max_repeat_attempts=2,
            ),
            "terminal_capability": 1,
        }
    )


def _accepted_procedure() -> AcceptedProcedure:
    """One accepted Procedure whose single Call names slot ``compute``."""

    return accepted_procedure(_definition(), activation_policy="drain", bound=False)


def _line() -> LineSpec:
    procedure = _accepted_procedure()
    provider = accepted_provider()
    return LineSpec(
        identity=ArtifactIdentity(kind="Line", name="provider-v4-line"),
        occurrence_epoch=1,
        procedure=ArtifactPin(
            role="procedure",
            target=procedure.procedure.identity,
            artifact_digest=procedure.artifact_digest,
        ),
        parameters={},
        bindings={
            "compute": LineBinding(
                build=binding_pin(provider.provider.identity, provider.artifact_digest),
                operation="compute",
            )
        },
        max_authority="observe",
        budgets={
            "max_capture_bytes": 1024,
            "max_items": 10,
            "max_provider_calls": 2,
            "max_wall_clock_microseconds": RUN_WALL_CLOCK.microseconds,
        },
        epsilon={"$decimal": "0"},
    )


@pytest.fixture
def contracted_demo() -> None:
    """The demo fixtures already declare typed operations; nothing to patch."""


def test_procedure_law_checks_slot_contracts_against_accepted_versions() -> None:
    procedure = _accepted_procedure().procedure
    accepted = evaluate_procedure_law(
        procedure,
        path=procedure_path(procedure.identity.name),
        predecessor=None,
        contracts={(VALUE.name, VALUE.version): contract_digest(VALUE).tagged},
    )
    assert accepted.verdict == "accepted", accepted.diagnostics
    refused = evaluate_procedure_law(
        procedure,
        path=procedure_path(procedure.identity.name),
        predecessor=None,
        contracts={},
    )
    assert refused.verdict == "refused"


def test_real_proposal_path_closes_contracts_build_procedure_and_line(tmp_path) -> None:
    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
    from tests.support.procedures import revalidated

    instance, _owner = initialize_local(tmp_path)
    provider = accepted_provider()
    procedure = _accepted_procedure()
    named = revalidated(procedure.procedure)
    line = _line()
    line = line.model_copy(
        update={
            "procedure": line.procedure.model_copy(
                update={"artifact_digest": procedure_artifact_digest(named).tagged}
            )
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(contract_files())
    tree[provider.path] = render_provider(provider.provider)
    tree[procedure.path] = render_procedure(named)
    tree[line_spec_path(line.identity.name)] = render_line_spec(line)

    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/provider-line-e2e",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-24T16:00:00.000000Z",
    )

    assert result.evaluation.verdict == "candidate", result.evaluation.diagnostics
    assert result.candidate is not None
    assert {member.artifact_kind for member in result.candidate.members} == {
        "contract",
        "line",
        "procedure",
        "provider",
    }
