"""The format discriminator, its registries, and the R13/R14 asymmetry."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from cruxible_core.errors import ConfigError
from cruxible_core.procedure import graph_format
from cruxible_core.procedure.graph_format import (
    definition_format_version,
    refuse_unknown_artifact_format,
    register_v2_definition_field,
    register_v2_step_type,
    registered_v2_step_types,
)
from cruxible_core.procedure.types import ProcedureDefinition, ProcedureRecord


def _definition(**overrides: Any) -> ProcedureDefinition:
    payload: dict[str, Any] = {
        "name": "discriminator_probe",
        "steps": [{"id": "call", "provider": "scorer", "input": {}, "as": "rows"}],
        "returns": "rows",
        "precondition": {},
        "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
    }
    payload.update(overrides)
    return ProcedureDefinition.model_validate(payload)


def test_absent_discriminator_is_format_v1_and_leaves_the_wire_untouched() -> None:
    definition = _definition()
    assert definition_format_version(definition) == (1, [])
    assert "graph_format" not in definition.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def test_explicit_null_discriminator_is_still_v1_and_still_absent_from_the_dump() -> None:
    definition = _definition(graph_format=None)
    assert definition_format_version(definition) == (1, [])
    assert "graph_format" not in definition.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def test_r14_declared_v2_without_a_construct_warns_rather_than_refuses() -> None:
    version, warnings = definition_format_version(_definition(graph_format=2))
    assert version == 2
    assert warnings == ["graph_format 2 declared but no graph construct is used"]


def test_r13_an_undeclared_registered_construct_is_refused() -> None:
    class _ProbeStep(BaseModel):
        id: str

    register_v2_step_type(_ProbeStep)
    try:
        definition = _definition()
        object.__setattr__(definition, "steps", [_ProbeStep(id="probe")])
        with pytest.raises(ConfigError, match="does not declare 'graph_format: 2'"):
            definition_format_version(definition)
        # Declared, the same structure is simply format v2 with no warning.
        object.__setattr__(definition, "graph_format", 2)
        assert definition_format_version(definition) == (2, [])
    finally:
        graph_format._V2_ONLY_STEP_TYPES.remove(_ProbeStep)


def test_r13_also_fires_for_a_registered_definition_field() -> None:
    register_v2_definition_field(ProcedureDefinition, "description")
    try:
        with pytest.raises(ConfigError, match="does not declare 'graph_format: 2'"):
            _definition(description="a registered v2-only position, for this test only")
    finally:
        graph_format._V2_ONLY_POSITIONS.remove((ProcedureDefinition, "description"))


def test_graph_format_is_never_itself_a_registered_construct() -> None:
    """The declaration is not a construct.

    Registering it would make the structural check trivially agree with the
    declaration whenever the declaration is 2, so R14 could never fire and R13
    would fire on nothing -- the check would be decoration.
    """
    assert all(
        field != "graph_format" for _model, field in graph_format.registered_v2_definition_fields()
    )


def test_registries_start_empty_of_procedure_step_types_until_a_type_declares_itself() -> None:
    assert all(issubclass(step_type, BaseModel) for step_type in registered_v2_step_types())


def test_unknown_format_refusal_names_the_class_and_the_supported_versions() -> None:
    error = refuse_unknown_artifact_format(
        artifact_class="procedures.json snapshot artifact",
        declared_version=7,
        supported_versions=(1, 2),
    )
    assert isinstance(error, ConfigError)
    assert "procedures.json snapshot artifact" in str(error)
    assert "supported: 1, 2" in str(error)


def test_record_carries_a_format_version_defaulting_to_one() -> None:
    record = ProcedureRecord(
        definition=_definition(),
        definition_digest="sha256:unchecked",
        proposed_actor_context=None,
    )
    assert record.definition_format_version == 1
