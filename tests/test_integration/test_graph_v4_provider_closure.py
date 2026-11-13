"""A slotted Call bound by a Line, and the real proposal path over them.

A Procedure names no provider: its one Call names slot ``compute``, and a
Line binds it to the demo build's ``compute`` operation. The helpers here
(``_accepted_procedure``, ``_line``) are shared fixtures for "one accepted
Procedure that calls the demo Provider".
"""

from __future__ import annotations

import pytest

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    evaluate_procedure_law,
    procedure_path,
)
from cruxible_client.contracts.procedures.line_specs import (
    LineSpec,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ProcedureDefinition,
)
from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
from cruxible_client.contracts.providers import render_provider
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from tests.core_support._p2b1_support import accepted_provider, pin
from tests.core_support._support import initialize_local
from tests.support.capabilities import capability_lookup, pinned_slot
from tests.support.lines import line_budgets, line_spec
from tests.support.operation_world import compute_capability, contract_files
from tests.support.procedures import (
    accepted_procedure,
    boundary_contract,
    named,
    procedure_definition,
    procedure_hard_caps,
    procedure_members,
)

#: The governed boundaries ``procedure_members`` accepts beside the Procedure.
_BOUNDARIES = (
    boundary_contract("provider-input"),
    boundary_contract("provider-output", {"value": "int"}),
)


def _definition() -> ProcedureDefinition:
    contract_in = pin("contract-in", "Contract", "provider-input/v1")
    contract_out = pin("contract-out", "Contract", "provider-output/v1")
    return procedure_definition(
        name="provider-v4",
        contract_in=named(contract_in),
        contract_out=named(contract_out),
        slots={"compute": pinned_slot(compute_capability())},
        nodes=(
            CallNode(
                node_id="direct",
                slot="compute",
                input={"value": 1},
                as_="result",
            ),
        ),
        hard_caps=procedure_hard_caps(preset="provider"),
    )


def _accepted_procedure() -> AcceptedProcedure:
    """One accepted Procedure whose single Call names slot ``compute``."""

    return accepted_procedure(_definition(), activation_policy="drain", bound=False)


def _line() -> LineSpec:
    procedure = _accepted_procedure()
    provider = accepted_provider()
    return line_spec(
        "provider-v4-line",
        procedure=ArtifactPin(
            role="procedure",
            target=procedure.procedure.identity,
            artifact_digest=procedure.artifact_digest,
        ),
        bindings={
            "compute": LineBinding(
                build=binding_pin(provider.provider.identity, provider.artifact_digest),
                operation="compute",
            )
        },
        budgets=line_budgets(max_capture_bytes=1024, max_items=10, max_provider_calls=2),
    )


@pytest.fixture
def contracted_demo() -> None:
    """The demo fixtures already declare typed operations; nothing to patch."""


def test_procedure_law_checks_slot_capabilities_against_accepted_versions() -> None:
    procedure = _accepted_procedure().procedure
    accepted = evaluate_procedure_law(
        procedure,
        path=procedure_path(procedure.identity.name),
        predecessor=None,
        capability_lookup=capability_lookup(compute_capability()),
    )
    assert accepted.verdict == "accepted", accepted.diagnostics
    refused = evaluate_procedure_law(
        procedure,
        path=procedure_path(procedure.identity.name),
        predecessor=None,
        capability_lookup=capability_lookup(),
    )
    assert refused.verdict == "refused"
    assert refused.diagnostics[0].code == "cruxible.procedure.slot_capability_unresolved"
    assert "test.compute@1" in refused.diagnostics[0].message


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
    tree.update(procedure_members(named))
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
        "capability",
        "contract",
        "line",
        "procedure",
        "provider",
    }
