"""Every caller surface bounds slot count, slot names and Provider references."""

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.inputs import LineInput
from cruxible_client.contracts.authoring.models import LineAuthoringPayload
from cruxible_client.contracts.procedures.bindings import (
    MAX_PROCEDURE_BINDINGS,
    MAX_PROCEDURE_PROVIDER_REFERENCE,
    MAX_PROCEDURE_SLOT_NAME,
)
from cruxible_core.mcp.tools import McpProcedureBindings
from cruxible_core.service.procedures.procedure_runs import ProcedureRunRequest


@pytest.fixture(params=["run", "line", "payload", "mcp"])
def validate(request):
    def build(bindings):
        if request.param == "run":
            return ProcedureRunRequest(input={}, bindings=bindings).bindings
        if request.param == "line":
            return LineInput(
                kind="line", name="test", procedure_name="test", bindings=bindings
            ).bindings
        if request.param == "payload":
            return LineAuthoringPayload(
                name="test", procedure_name="test", bindings=bindings
            ).bindings
        return McpProcedureBindings.model_validate(bindings).root

    return build


def test_binding_surfaces_accept_the_declared_size_limits(validate):
    bindings = {f"slot-{i}": "build" for i in range(MAX_PROCEDURE_BINDINGS)}
    bindings.pop("slot-0")
    bindings["s" * MAX_PROCEDURE_SLOT_NAME] = "p" * MAX_PROCEDURE_PROVIDER_REFERENCE
    assert validate(bindings) == bindings


@pytest.mark.parametrize(
    "bindings",
    [
        {f"slot-{i}": "build" for i in range(MAX_PROCEDURE_BINDINGS + 1)},
        {"s" * (MAX_PROCEDURE_SLOT_NAME + 1): "build"},
        {"slot": "p" * (MAX_PROCEDURE_PROVIDER_REFERENCE + 1)},
        {"": "build"},
        {"slot": ""},
    ],
    ids=["too-many", "long-key", "long-value", "empty-key", "empty-value"],
)
def test_binding_surfaces_refuse_oversized_or_empty_choices(validate, bindings):
    with pytest.raises(ValidationError):
        validate(bindings)
