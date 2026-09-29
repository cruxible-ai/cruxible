"""Lines and acquisition policies are authorable from the tagless CLI/MCP input."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.examples import authoring_example
from cruxible_client.authoring.inputs import (
    AcquisitionPolicyInput,
    ChangeSetInput,
    LineInput,
    ProcedureInput,
    ProcedureMandateInputV1,
)
from cruxible_client.contracts.procedures.models import ProcedureHardCapsV3
from cruxible_client.transport.http import CruxibleClient
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate


def _transport(http: TestClient) -> CruxibleClient:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    return transport


def _members(*names: str) -> ChangeSetInput:
    members = [authoring_example(name) for name in names]  # type: ignore[arg-type]
    return ChangeSetInput.model_validate(
        {"kind": "change_set", "members": [m.model_dump(mode="json") for m in members]}
    )


def _accept(http: TestClient, instance_id: str, key: Path, payload: object) -> str:
    transport = _transport(http)
    compiled = transport.compile_playbill_authoring_input(
        instance_id,
        input=payload.model_dump(mode="json"),  # type: ignore[attr-defined]
    )
    assert compiled.verdict == "passed", compiled.frontier
    submitted = transport.submit_playbill_authoring_intent(
        instance_id, str(compiled.certificate["intent_id"])
    )
    proposal_id = submitted.status.proposal_id
    assert proposal_id is not None
    _approve_and_activate(http, instance_id, key, proposal_id)
    return proposal_id


@pytest.mark.parametrize(
    "names",
    [
        ("procedure", "line"),
        ("procedure", "line", "acquisition-policy"),
        ("procedure", "procedure-mandate"),
        ("procedure", "line", "procedure-mandate", "acquisition-policy"),
    ],
)
def test_the_procedure_line_policy_and_mandate_examples_are_accepted_together(
    playbill_http: tuple[TestClient, str, Path],
    names: tuple[str, ...],
) -> None:
    http, instance_id, key = playbill_http
    change_set = _members(*names)
    _accept(http, instance_id, key, change_set)


def test_a_line_may_name_the_example_policy_even_though_it_acquires_nothing(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, key = playbill_http
    line = authoring_example("line")
    assert isinstance(line, LineInput)
    change_set = ChangeSetInput(
        kind="change_set",
        members=(
            authoring_example("procedure"),  # type: ignore[arg-type]
            authoring_example("acquisition-policy"),  # type: ignore[arg-type]
            line.model_copy(update={"acquisition_policy_name": "replace-me"}),
        ),
    )
    _accept(http, instance_id, key, change_set)


def test_singleton_line_and_policy_inputs_lower_through_the_tagless_union(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, key = playbill_http
    _accept(http, instance_id, key, authoring_example("procedure"))
    policy = authoring_example("acquisition-policy")
    assert isinstance(policy, AcquisitionPolicyInput)
    _accept(http, instance_id, key, policy)
    line = authoring_example("line")
    created = _transport(http).create_playbill_authoring_input(
        instance_id, input=line.model_dump(mode="json")
    )
    assert created.intent["semantic_identity"] == "Line:replace-me"


def test_line_parameters_are_checked_against_the_procedure_input_at_authoring(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, _key = playbill_http
    line = authoring_example("line")
    assert isinstance(line, LineInput)
    change_set = ChangeSetInput(
        kind="change_set",
        members=(
            authoring_example("procedure"),  # type: ignore[arg-type]
            line.model_copy(update={"parameters": {"count": 1}}),
        ),
    )
    compiled = _transport(http).compile_playbill_authoring_input(
        instance_id, input=change_set.model_dump(mode="json")
    )
    assert compiled.verdict != "passed"
    frontier = str(compiled.frontier)
    assert "playbill.authoring.line_parameters_refused" in frontier
    assert "count" in frontier


def test_example_templates_agree_on_names_and_caps() -> None:
    procedure = authoring_example("procedure")
    mandate = authoring_example("procedure-mandate")
    line = authoring_example("line")
    assert isinstance(procedure, ProcedureInput)
    assert isinstance(mandate, ProcedureMandateInputV1)
    assert isinstance(line, LineInput)
    assert procedure.definition["name"] == mandate.procedure_name == line.procedure_name
    caps = ProcedureHardCapsV3.model_validate(procedure.definition["hard_caps"])
    assert mandate.resource_ceiling == caps
