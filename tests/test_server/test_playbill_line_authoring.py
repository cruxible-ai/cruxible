"""Lines and mandates are authorable from the tagless CLI/MCP input."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.examples import authoring_example
from cruxible_client.authoring.inputs import (
    ChangeSetInput,
    LineInput,
    ProcedureInput,
    ProcedureMandateInput,
)
from cruxible_client.contracts.procedures.authored import (
    ClaimsStep,
    GuardStep,
    ProjectStep,
    ReadStep,
)
from cruxible_client.transport.http import CruxibleClient
from tests.support.procedures import procedure_spec_data, ref
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
    submitted = transport.submit_authoring_intent(
        instance_id, str(compiled.certificate.intent_id), accept="never"
    )
    proposal_id = submitted.status.proposal_id
    assert proposal_id is not None
    _approve_and_activate(http, instance_id, key, proposal_id)
    return proposal_id


@pytest.mark.parametrize(
    "names",
    [
        ("procedure", "line"),
        ("procedure", "line", "procedure-mandate"),
    ],
)
def test_the_procedure_line_policy_and_mandate_examples_are_accepted_together(
    playbill_http: tuple[TestClient, str, Path],
    names: tuple[str, ...],
) -> None:
    """A Mandate names an *accepted or same-ChangeSet* Line (ruling C3).

    `MEMBER_STAGING_ORDER` in `cruxible_core.authoring.lowering` stages
    `line` before `procedure_mandate`, so a same-set Mandate pinning a Line
    authored in the same change set resolves the Line's bytes in the tree
    already staged when the Mandate renders.
    """

    http, instance_id, key = playbill_http
    _accept(http, instance_id, key, _members(*names))


def test_an_observe_only_line_runs_without_a_mandate(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """The example Procedure only observes, so its Line needs no mandate to run or enable."""

    http, instance_id, key = playbill_http
    _accept(http, instance_id, key, _members("procedure", "line"))
    transport = _transport(http)
    run = transport.run_line(instance_id, "replace-me", occurrence_id=None)
    assert run.status == "succeeded", run.terminal
    assert run.result == {"count": 1}
    enabled = transport.enable_line(instance_id, "replace-me")
    assert enabled.state == "enabled"


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


def test_example_templates_agree_on_names() -> None:
    procedure = authoring_example("procedure")
    mandate = authoring_example("procedure-mandate")
    line = authoring_example("line")
    assert isinstance(procedure, ProcedureInput)
    assert isinstance(mandate, ProcedureMandateInput)
    assert isinstance(line, LineInput)
    assert procedure.spec.procedure == line.procedure_name
    assert mandate.line_name == line.name
    # A Mandate grants authority only; the Procedure's hard caps bound every
    # run and the Line owns the operating budget within them.
    assert "resource_ceiling" not in type(mandate).model_fields


def test_a_mandate_naming_a_resource_ceiling_is_refused_naming_the_line_budget() -> None:
    mandate = authoring_example("procedure-mandate")
    assert isinstance(mandate, ProcedureMandateInput)
    data = mandate.model_dump(mode="json")
    data["resource_ceiling"] = {"max_items": 1}
    with pytest.raises(ValueError, match="procedure_mandate_ceiling_retired"):
        ProcedureMandateInput.model_validate(data)


def test_a_line_budget_wider_than_its_procedure_names_each_widened_cap(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, key = playbill_http
    line = authoring_example("line")
    assert isinstance(line, LineInput)
    wide = line.model_copy(update={"budgets": {"max_items": 1000, "max_provider_calls": 100}})
    _accept(http, instance_id, key, _members("procedure"))
    compiled = _transport(http).compile_authoring_input(
        instance_id, input=wide.model_dump(mode="json")
    )
    assert compiled.verdict != "passed"
    frontier = str(compiled.frontier)
    assert "cruxible.authoring.line_budget_exceeds_caps" in frontier
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
    # A Procedure's capability is what its terminals can do: this one can propose
    # (on a branch its runs never take) and otherwise returns its count.
    proposer = ProcedureInput.model_validate(
        procedure_spec_data(
            base=procedure.spec.model_dump(mode="json", by_alias=True),
            procedure="proposer",
            returns=None,
            steps=[
                GuardStep(
                    guard={"op": "eq", "left": {"literal": True}, "right": {"literal": True}},
                    else_={"goto": "read-offer"},
                ),
                ProjectStep(project="result", fields={"count": 0}, leaf=True),
                ReadStep(
                    read="offered",
                    claim_type="project.work_item.slug",
                    subject="wi-42",
                    subject_kind="project.work_item",
                    all=1,
                    id="read-offer",
                ),
                ClaimsStep(
                    claims=[
                        {"type": "project.work_item.slug", "value": "wi-42", "role": "normative"}
                    ],
                    for_each=ref("$steps.offered"),
                    subject={"kind": "project.work_item", "key": {"slug": "wi-42"}},
                    id="offer",
                ),
            ],
        )
    )
    from cruxible_client.contracts.authoring.inputs import (
        ClaimTypeInput,
        SubjectInput,
        SubjectKindInput,
    )
    from tests.core_support._subject_kind_support import bare_slug_claim_type

    change_set = ChangeSetInput(
        kind="change_set",
        members=(
            ClaimTypeInput(kind="claim_type", claim_type=bare_slug_claim_type("project.work_item")),
            SubjectKindInput(
                kind="subject_kind", name="project.work_item", keys=("project.work_item.slug",)
            ),
            SubjectInput(kind="subject", subject_kind="project.work_item", key={"slug": "wi-42"}),
            procedure,
            authoring_example("line"),  # type: ignore[arg-type]
            proposer,
            LineInput(kind="line", name="proposer", procedure_name="proposer"),
        ),
    )
    # The proposer Mandate names an *accepted* Line (see the staging-order note
    # on test_a_mandate_wider_than_its_procedure_names_each_widened_cap above),
    # so it is accepted in its own change set after its Line exists.
    _accept(http, instance_id, key, change_set)
    _accept(
        http,
        instance_id,
        key,
        mandate.model_copy(update={"name": "proposer", "line_name": "proposer"}),
    )

    def line_run(name: str) -> object:
        result = playbill_api.playbill_line_run(
            instance_id, name, request=LineRunRequest(line=name)
        )
        assert result.status == "succeeded", result.model_dump_json()
        return result

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
