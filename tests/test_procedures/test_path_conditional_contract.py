"""Analysis 4 -- per-path contract checking (§3.5) and the typed warning channel.

Two verdicts existed before graph procedures: a contract field was consumed or
it was not. Under branching there is a third, and it is the common one -- the
field the escalation arm reads and no other path touches. This file pins that
verdict, the typed channel that carries it, and the dual-emit invariant that
keeps the deprecated string list honest.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service.procedures import (
    WARNING_CONTRACT_FIELD_PATH_CONDITIONAL,
    WARNING_CONTRACT_FIELD_UNCONSUMED,
    lint_procedure_definition_authoring,
    lint_procedure_definition_authoring_typed,
)

_CONFIG_CONTRACT = "BranchingInput"

_CONFIG_YAML = """\
version: "1.0"
name: procedure_path_conditional

entity_types:
  Task:
    properties:
      task_id:
        type: string
        primary_key: true

relationships: []

contracts:
  BranchingInput:
    fields:
      value:
        type: int
      escalation_note:
        type: string
      unused_field:
        type: string

providers:
  exported_action:
    kind: tool
    contract_in: cruxible.JsonObject
    contract_out: cruxible.JsonObject
    ref: https://example.invalid/action
    version: "1.0"
    runtime: http_json
    procedure_access: graph_write
    config:
      timeout_s: 5
"""


@pytest.fixture
def lint_instance(tmp_path: Any) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _definition(steps: list[dict[str, Any]], **overrides: Any) -> ProcedureDefinition:
    payload: dict[str, Any] = {
        "name": "path_probe",
        "contract_in": _CONFIG_CONTRACT,
        "steps": steps,
        "returns": "final",
        "precondition": {},
        "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
        "declared_tier": "graph_write",
        "graph_format": 2,
    }
    payload.update(overrides)
    if payload.get("graph_format") is None:
        payload.pop("graph_format", None)
    return ProcedureDefinition.model_validate(payload)


def _tail(alias: str = "final") -> dict[str, Any]:
    return {
        "id": "tail",
        "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
        "as": alias,
    }


def _branching_definition(**overrides: Any) -> ProcedureDefinition:
    """One guard; only the true arm reads `escalation_note`."""
    return _definition(
        [
            {
                "id": "gate",
                "guard": {"left": "$input.value", "op": "gt", "right": 10},
                "on_true": "escalate",
                "on_false": "tail",
                "message": "below threshold",
            },
            {
                "step": {
                    "id": "escalate",
                    "provider": "exported_action",
                    "input": {"note": "$input.escalation_note"},
                    "as": "escalated",
                },
                "next": "tail",
            },
            _tail(),
        ],
        **overrides,
    )


def _codes(instance: CruxibleInstance, definition: ProcedureDefinition) -> list[str]:
    config = instance.load_config()
    return [
        warning.code for warning in lint_procedure_definition_authoring_typed(definition, config)
    ]


def test_a_field_read_on_one_arm_only_is_path_conditional(
    lint_instance: CruxibleInstance,
) -> None:
    config = lint_instance.load_config()
    warnings = lint_procedure_definition_authoring_typed(_branching_definition(), config)
    conditional = [
        warning for warning in warnings if warning.code == WARNING_CONTRACT_FIELD_PATH_CONDITIONAL
    ]
    assert [warning.node_ids for warning in conditional] == [["escalate"]]
    assert "escalation_note" in conditional[0].message


def test_a_field_no_step_reads_is_still_unconsumed_not_conditional(
    lint_instance: CruxibleInstance,
) -> None:
    """The existing verdict is unmoved: no path is not some path."""
    config = lint_instance.load_config()
    warnings = lint_procedure_definition_authoring_typed(_branching_definition(), config)
    unconsumed = [
        warning for warning in warnings if warning.code == WARNING_CONTRACT_FIELD_UNCONSUMED
    ]
    assert len(unconsumed) == 1
    assert "unused_field" in unconsumed[0].message


def test_a_field_every_path_reads_is_not_warned_about(
    lint_instance: CruxibleInstance,
) -> None:
    """`value` is read by the guard, which every path passes through."""
    assert WARNING_CONTRACT_FIELD_PATH_CONDITIONAL in _codes(lint_instance, _branching_definition())
    config = lint_instance.load_config()
    warnings = lint_procedure_definition_authoring_typed(_branching_definition(), config)
    assert not any("'value'" in warning.message for warning in warnings)


def test_a_linear_definition_never_produces_a_conditional_verdict(
    lint_instance: CruxibleInstance,
) -> None:
    """Linearity has one path, so consumption is total or absent -- never partial.

    This is what keeps every v1 definition's warning list byte-identical; T4
    states it over the whole corpus.
    """
    definition = _definition(
        [
            {
                "id": "call",
                "provider": "exported_action",
                "input": {
                    "value": "$input.value",
                    "note": "$input.escalation_note",
                    "other": "$input.unused_field",
                },
                "as": "called",
            },
            _tail(),
        ],
        graph_format=None,
    )
    assert WARNING_CONTRACT_FIELD_PATH_CONDITIONAL not in _codes(lint_instance, definition)


def test_a_guard_operand_counts_as_consumption(
    lint_instance: CruxibleInstance,
) -> None:
    """A guard reading `$input.x` consumes x as surely as a provider input does.

    Before this the scan looked only at step reference templates, so a field
    read ONLY by a guard was reported as unconsumed -- advice to delete a field
    the definition cannot run without.
    """
    definition = _definition(
        [
            {
                "id": "gate",
                "guard": {"left": "$input.unused_field", "op": "eq", "right": "x"},
                "on_true": "tail",
                "on_false": "tail",
                "message": "no match",
            },
            _tail(),
        ],
        budget={"wall_clock_s": 30, "max_provider_calls": 0},
    )
    config = lint_instance.load_config()
    warnings = lint_procedure_definition_authoring_typed(definition, config)
    unconsumed = [
        warning.message for warning in warnings if warning.code == WARNING_CONTRACT_FIELD_UNCONSUMED
    ]
    assert not any("unused_field" in message for message in unconsumed)


def test_r10_refuses_an_undeclared_field_read_by_a_guard(
    lint_instance: CruxibleInstance,
) -> None:
    """The same impossibility, in the position the linear grammar had no room for."""
    definition = _definition(
        [
            {
                "id": "gate",
                "guard": {"left": "$input.nonexistent", "op": "eq", "right": "x"},
                "on_true": "tail",
                "on_false": "tail",
                "message": "no match",
            },
            _tail(),
        ],
        budget={"wall_clock_s": 30, "max_provider_calls": 0},
    )
    config = lint_instance.load_config()
    with pytest.raises(ConfigError, match="undeclared contract_in field 'nonexistent'"):
        lint_procedure_definition_authoring_typed(definition, config)


def test_the_string_channel_is_derived_from_the_typed_one(
    lint_instance: CruxibleInstance,
) -> None:
    """Dual-emit, and the two cannot drift because one is built from the other."""
    config = lint_instance.load_config()
    definition = _branching_definition()
    typed = lint_procedure_definition_authoring_typed(definition, config)
    strings = lint_procedure_definition_authoring(definition, config)
    assert strings == [warning.message for warning in typed]


def test_an_abort_arm_is_not_a_path_that_skipped_the_field(
    lint_instance: CruxibleInstance,
) -> None:
    """A refusal is not an execution that completed without the input.

    Counting abort arms as paths would make nearly every field downstream of a
    guard path-conditional, and the verdict would stop meaning anything.
    """
    definition = _definition(
        [
            {
                "id": "gate",
                "guard": {"left": "$input.value", "op": "gt", "right": 10},
                "message": "below threshold",
            },
            {
                "id": "escalate",
                "provider": "exported_action",
                "input": {
                    "note": "$input.escalation_note",
                    "other": "$input.unused_field",
                },
                "as": "escalated",
            },
            _tail(),
        ],
    )
    assert WARNING_CONTRACT_FIELD_PATH_CONDITIONAL not in _codes(lint_instance, definition)
