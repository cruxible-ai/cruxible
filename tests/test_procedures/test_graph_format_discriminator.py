"""The format discriminator, its registries, and the R13/R14 asymmetry."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from cruxible_core.errors import ConfigError
from cruxible_core.procedure import graph_format
from cruxible_core.procedure.graph_format import (
    SUPPORTED_DECLARED_FORMATS,
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


def test_an_explicit_null_is_refused_because_absence_is_the_whole_spelling() -> None:
    """Explicit null and absence dump identically and are NOT the same wire form.

    A 0.3 core refuses `"graph_format": null` outright, so a reader that accepts
    it takes something the format's own old-reader lock rejected -- and the
    acceptance is invisible afterwards, because by the time the model exists
    both spellings are ``None`` and both dumps have dropped the key.
    """
    with pytest.raises(ConfigError, match="spelled by ABSENCE") as exc_info:
        _definition(graph_format=None)
    assert "supported: 1, 2" in str(exc_info.value)


def test_an_absent_discriminator_is_v1_and_the_key_never_reaches_the_wire() -> None:
    definition = _definition()
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


@pytest.mark.parametrize("declared", [3, 4, 0, -1, 99])
def test_an_unreadable_declared_format_is_refused_rather_than_read_as_v1(
    declared: int,
) -> None:
    """The fail-open this closes.

    ``graph_format`` is a KNOWN key, so ``extra="forbid"`` refuses an unknown
    KEY and can say nothing about an unreadable VALUE. Treating everything that
    is not 2 as v1 would take a definition authored by a future core, digest it
    under v1 rules and execute it with v1 semantics -- silently, and with a
    different digest on each side. That is the exact disease the discriminator
    exists to prevent, arriving through the discriminator itself.
    """
    with pytest.raises(ConfigError, match=r"supported: 1, 2"):
        _definition(graph_format=declared)


def test_the_unreadable_format_refusal_names_the_artifact_class_and_the_remedy() -> None:
    with pytest.raises(ConfigError) as exc_info:
        _definition(graph_format=3)
    message = str(exc_info.value)
    assert "ProcedureDefinition declares format version 3" in message
    assert "Upgrade cruxible-core" in message


def test_an_explicit_graph_format_one_is_refused_because_v1_is_spelled_by_absence() -> None:
    """One format, one wire spelling.

    An explicit 1 is not unreadable -- it is a SECOND spelling for a format
    that already has one. It survives an exclude_none dump, so it yields a
    different digest for an otherwise identical definition, and it makes a 0.3
    core raise extra_forbidden on a definition that core could otherwise read
    perfectly.
    """
    with pytest.raises(ConfigError, match="spelled by ABSENCE"):
        _definition(graph_format=1)


def test_the_two_refusals_send_different_instructions() -> None:
    """An author who wrote 1 must DELETE the key; an operator who met a 3 must
    upgrade. One shared message would hand each of them the other's remedy."""
    with pytest.raises(ConfigError) as explicit_v1:
        _definition(graph_format=1)
    with pytest.raises(ConfigError) as unknown:
        _definition(graph_format=3)
    assert "remove the key" in str(explicit_v1.value)
    assert "Upgrade cruxible-core" not in str(explicit_v1.value)
    assert "remove the key" not in str(unknown.value)


def test_the_legal_wire_spellings_are_exactly_absent_and_two() -> None:
    assert SUPPORTED_DECLARED_FORMATS == frozenset({None, 2})
    assert definition_format_version(_definition())[0] == 1
    assert definition_format_version(_definition(graph_format=2))[0] == 2


@pytest.mark.parametrize("declared", ["2", 2.0, "banana", "1", True, [2], {"v": 2}])
def test_a_coerced_wire_spelling_is_refused_before_pydantic_can_normalize_it(
    declared: Any,
) -> None:
    """Coercion IS the fail-open.

    ``int | None`` is non-strict, so ``"2"`` and ``2.0`` become ``2`` and reach
    the value check already looking legal. An artifact whose discriminator
    arrives as a different JSON type was not written by a core that agrees with
    this one about what the field is, so it is refused rather than normalized
    into agreement.
    """
    with pytest.raises(ConfigError, match="supported: 1, 2"):
        _definition(graph_format=declared)


def test_the_integer_two_is_still_the_one_accepted_declaration() -> None:
    assert _definition(graph_format=2).graph_format == 2
    assert _definition().graph_format is None
