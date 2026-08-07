"""The project node, and the guardrail pinning `$.returns` from both ends.

`returns` stays a TOP-LEVEL STRING naming one alias. That is forced, not
conservative: the observed-output publication path picks which evidence
artifact publishes a procedure's envelope with a literal
``json_extract(definition_json, '$.returns')`` and has NO FALLBACK by design.
Moving, nesting or renaming `returns` makes that join match nothing and
silently publishes no envelope -- a failure with no error anywhere.

Nothing pinned that shape, so the project node arrived as the thing `returns`
NAMES rather than as a replacement for it, and this guardrail is the other half.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.analysis import procedure_node_kind
from cruxible_core.procedure.types import ProcedureDefinition, ProcedureProjectStepSchema
from cruxible_core.service import service_run_procedure
from cruxible_core.service.procedures import compile_procedure_definition
from tests.test_procedures.conftest import actor, provider_definition
from tests.test_procedures.test_execution import _accept, _receipt, _stub_provider

_BUDGET = {"wall_clock_s": 30, "max_provider_calls": 3}


def _projecting_definition(name: str = "projector") -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            "name": name,
            "contract_in": "ProcedureInput",
            "graph_format": 2,
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "raw",
                },
                {
                    "id": "shape",
                    "project": {
                        "fields": {
                            "value": "$steps.raw.value",
                            "echoed": "$input.value",
                        }
                    },
                    "as": "result",
                },
            ],
            "returns": "result",
            "precondition": {},
            "budget": _BUDGET,
            "declared_tier": "graph_write",
        }
    )


def test_a_project_node_parses_and_reports_its_kind() -> None:
    definition = _projecting_definition()
    assert isinstance(definition.steps[1], ProcedureProjectStepSchema)
    assert procedure_node_kind(definition.steps[1]) == "project"


def test_a_project_node_is_a_v2_construct() -> None:
    undeclared = _projecting_definition().model_dump(mode="json", by_alias=True, exclude_none=True)
    undeclared.pop("graph_format")
    with pytest.raises(Exception, match="does not declare 'graph_format: 2'"):
        ProcedureDefinition.model_validate(undeclared)


def test_a_projection_may_be_the_returns_alias(procedure_instance: CruxibleInstance) -> None:
    """`project` joins the set of kinds that can produce a procedure's output."""
    plan = compile_procedure_definition(procedure_instance, _projecting_definition())
    assert plan.returns == "result"
    assert plan.steps[1].kind == "project"


def test_a_definition_returning_a_nonexistent_alias_is_still_refused(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            **_projecting_definition("bad_returns").model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            "returns": "not_produced",
        }
    )
    with pytest.raises(ConfigError, match="not produced by any output step"):
        compile_procedure_definition(procedure_instance, definition)


def test_a_projection_assembles_its_output_from_alias_references(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0)) * 2})
    procedure_id = _accept(procedure_instance, _projecting_definition("projector_runs"))
    result = service_run_procedure(procedure_instance, procedure_id, {"value": 21}, actor("runner"))
    assert result.output == {"value": 42, "echoed": 21}
    receipt = _receipt(procedure_instance, result.run.receipt_id or "")
    project_nodes = [
        node for node in receipt.nodes if node.detail.get("fields") == ["echoed", "value"]
    ]
    assert len(project_nodes) == 1


def test_a_projection_cannot_carry_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ProcedureProjectStepSchema.model_validate(
            {"id": "p", "as": "out", "project": {"fields": {}, "compute": "1 + 1"}}
        )


def _stored_definition_json(instance: CruxibleInstance, procedure_id: str) -> str:
    store = instance.get_procedure_store()
    try:
        row = store._conn.execute(
            "SELECT definition_json FROM procedures WHERE procedure_id = ?",
            (procedure_id,),
        ).fetchone()
        assert row is not None
        return str(row["definition_json"])
    finally:
        store.close()


@pytest.mark.parametrize(
    "definition_factory",
    [lambda: provider_definition("v1_returns_shape"), _projecting_definition],
    ids=["format-v1", "format-v2"],
)
def test_the_stored_row_keeps_its_output_alias_at_the_dollar_returns_path(
    definition_factory: Any,
    procedure_instance: CruxibleInstance,
) -> None:
    """THE GUARDRAIL.

    The observed-output publication path selects an evidence artifact with a
    literal ``json_extract(defs.definition_json, '$.returns')`` and no fallback.
    Nothing pinned that path, so a well-meant restructuring of `returns` --
    nesting it under the projection node, say -- would break publication with no
    error anywhere. This asserts the ROW SHAPE that join depends on, for both
    formats, using SQLite's own json_extract rather than a Python re-reading.
    """
    definition = definition_factory()
    procedure_id = _accept(procedure_instance, definition)
    store = procedure_instance.get_procedure_store()
    try:
        extracted = store._conn.execute(
            "SELECT json_extract(definition_json, '$.returns') AS returns_alias "
            "FROM procedures WHERE procedure_id = ?",
            (procedure_id,),
        ).fetchone()["returns_alias"]
    finally:
        store.close()
    assert extracted == definition.returns
    assert isinstance(extracted, str)

    stored = json.loads(_stored_definition_json(procedure_instance, procedure_id))
    assert isinstance(stored["returns"], str), (
        "`returns` must remain a TOP-LEVEL STRING: the publication join reads it "
        "with a literal $.returns path and has no fallback by design"
    )
    assert stored["returns"] in {step.get("as") or step.get("id") for step in stored["steps"]}


def test_the_returns_alias_selects_the_published_evidence_artifact(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other end of the same join.

    `$.returns` is not a decorative field: it is how a run's published output is
    chosen, so the alias it names has to be the alias whose value is stored.
    """
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0)) * 2})
    procedure_id = _accept(procedure_instance, _projecting_definition("returns_join"))
    result = service_run_procedure(procedure_instance, procedure_id, {"value": 3}, actor("runner"))
    stored = json.loads(_stored_definition_json(procedure_instance, procedure_id))
    assert result.step_outputs[stored["returns"]] == result.output
