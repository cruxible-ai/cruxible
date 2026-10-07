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
    ProcedureMandateInput,
)
from cruxible_client.contracts.procedures.models import ProcedureHardCaps
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
    compiled = transport.compile_authoring_input(
        instance_id,
        input=payload.model_dump(mode="json"),  # type: ignore[attr-defined]
    )
    assert compiled.verdict == "passed", compiled.frontier
    submitted = transport.submit_authoring_intent(instance_id, str(compiled.certificate.intent_id))
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


def test_an_observe_only_line_runs_without_a_mandate(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """The example Procedure only observes, so its Line needs no mandate to run or arm."""

    http, instance_id, key = playbill_http
    _accept(http, instance_id, key, _members("procedure", "line"))
    transport = _transport(http)
    run = transport.run_line(instance_id, "replace-me", occurrence_id=None)
    assert run.status == "succeeded", run.terminal
    assert run.result == {"count": 1}
    armed = transport.enable_line(instance_id, "replace-me")
    assert armed.state == "enabled"


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
    run = _transport(http).run_line(instance_id, "replace-me", occurrence_id=None)
    assert run.status == "succeeded", run.terminal


def test_singleton_line_and_policy_inputs_lower_through_the_tagless_union(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, key = playbill_http
    _accept(http, instance_id, key, authoring_example("procedure"))
    policy = authoring_example("acquisition-policy")
    assert isinstance(policy, AcquisitionPolicyInput)
    _accept(http, instance_id, key, policy)
    line = authoring_example("line")
    compiled = _transport(http).compile_authoring_input(
        instance_id, input=line.model_dump(mode="json")
    )
    created = _transport(http).get_authoring_intent(
        instance_id, str(compiled.certificate.intent_id)
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
    compiled = _transport(http).compile_authoring_input(
        instance_id, input=change_set.model_dump(mode="json")
    )
    assert compiled.verdict != "passed"
    frontier = str(compiled.frontier)
    assert "cruxible.authoring.line_parameters_refused" in frontier
    assert "count" in frontier


def test_example_templates_agree_on_names_and_caps() -> None:
    procedure = authoring_example("procedure")
    mandate = authoring_example("procedure-mandate")
    line = authoring_example("line")
    assert isinstance(procedure, ProcedureInput)
    assert isinstance(mandate, ProcedureMandateInput)
    assert isinstance(line, LineInput)
    assert procedure.definition["name"] == mandate.procedure_name == line.procedure_name
    caps = ProcedureHardCaps.model_validate(procedure.definition["hard_caps"])
    assert mandate.resource_ceiling == caps


def test_a_mandate_wider_than_its_procedure_names_each_widened_cap(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, _key = playbill_http
    mandate = authoring_example("procedure-mandate")
    assert isinstance(mandate, ProcedureMandateInput)
    wide = mandate.model_copy(
        update={
            "resource_ceiling": mandate.resource_ceiling.model_copy(
                update={"max_items": 1000, "max_provider_calls": 100}
            )
        }
    )
    change_set = ChangeSetInput(
        kind="change_set",
        members=(authoring_example("procedure"), wide),  # type: ignore[arg-type]
    )
    compiled = _transport(http).compile_authoring_input(
        instance_id, input=change_set.model_dump(mode="json")
    )
    assert compiled.verdict != "passed"
    frontier = str(compiled.frontier)
    assert "resource_ceiling_widens_procedure" in frontier
    assert "max_items 1000 > 200" in frontier
    assert "max_provider_calls 100 > 0" in frontier


def test_the_run_tier_follows_what_the_line_or_procedure_can_do(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """Observe-only runs are reads; a run that can propose needs governed write."""

    from cruxible_client.contracts.line_dispatch import LineDispatchRequest
    from cruxible_core.errors import PermissionDeniedError
    from cruxible_core.runtime import playbill_api
    from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        ProcedureRunRequest,
    )

    http, instance_id, key = playbill_http
    procedure = authoring_example("procedure")
    mandate = authoring_example("procedure-mandate")
    assert isinstance(procedure, ProcedureInput)
    assert isinstance(mandate, ProcedureMandateInput)
    proposer = procedure.model_copy(
        update={
            "definition": {**procedure.definition, "name": "proposer", "terminal_capability": 2}
        }
    )
    change_set = ChangeSetInput(
        kind="change_set",
        members=(
            procedure,
            authoring_example("line"),  # type: ignore[arg-type]
            proposer,
            LineInput(kind="line", name="proposer", procedure_name="proposer"),
            mandate.model_copy(update={"name": "proposer", "procedure_name": "proposer"}),
        ),
    )
    _accept(http, instance_id, key, change_set)

    def line_run(name: str) -> object:
        return playbill_api.playbill_line_run(instance_id, name, request=LineRunRequest(line=name))

    def procedure_run(name: str) -> object:
        return playbill_api.playbill_procedure_run(
            instance_id, name, request=ProcedureRunRequest(input={})
        )

    with request_permission_scope(PermissionMode.READ_ONLY):
        assert line_run("replace-me").status == "succeeded"  # type: ignore[attr-defined]
        assert procedure_run("replace-me").status == "succeeded"  # type: ignore[attr-defined]
        for denied in (
            lambda: line_run("proposer"),
            lambda: procedure_run("proposer"),
            lambda: playbill_api.playbill_line_dispatch(
                instance_id, "proposer", request=LineDispatchRequest()
            ),
        ):
            with pytest.raises(PermissionDeniedError) as refused:
                denied()
            assert refused.value.required_mode == "GOVERNED_WRITE"
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        assert line_run("proposer").status == "succeeded"  # type: ignore[attr-defined]
