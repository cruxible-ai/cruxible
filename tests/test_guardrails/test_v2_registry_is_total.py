"""Every step type is CLASSIFIED as format-v1-legal or format-v2-only.

An unclassified step type is the quiet failure mode: it parses, it is invisible
to the structural check, and a definition using it is digested under v1 rules
while an old core happily parses and mis-executes it. So the classification is
total by construction, and adding a step type is a deliberate act.
"""

from __future__ import annotations

import typing
from pathlib import Path

from pydantic import BaseModel

from cruxible_core.config.schema import WorkflowStepSchema
from cruxible_core.procedure.graph_format import registered_v2_step_types
from cruxible_core.procedure.types import (
    ProcedureRepeatStepSchema,
    ProcedureStepSchema,
)

V1_LEGAL_STEP_TYPES: frozenset[type[BaseModel]] = frozenset(
    {
        # The shared configured-workflow grammar. Present in every stored v1
        # definition; adding it to the v2 envelope would make every definition
        # structurally v2.
        WorkflowStepSchema,
        # Procedure-only, but it shipped in v1 and its wire form is unchanged.
        ProcedureRepeatStepSchema,
    }
)

UNION_MEMBERS: tuple[type[BaseModel], ...] = typing.get_args(ProcedureStepSchema)


def test_every_union_member_is_classified_exactly_once() -> None:
    v2_types = frozenset(registered_v2_step_types())
    unclassified = [
        member.__name__
        for member in UNION_MEMBERS
        if member not in V1_LEGAL_STEP_TYPES and member not in v2_types
    ]
    assert unclassified == [], (
        f"{unclassified} joined the procedure step union without a format "
        "classification. Register it with register_v2_step_type in the commit "
        "that declares it, or pin it in V1_LEGAL_STEP_TYPES with the reason it "
        "is legal in a definition that declares no graph_format."
    )
    both = [
        member.__name__
        for member in UNION_MEMBERS
        if member in V1_LEGAL_STEP_TYPES and member in v2_types
    ]
    assert both == [], f"{both} are classified as both v1-legal and v2-only"


def test_v1_legal_pins_name_live_union_members() -> None:
    stale = [member.__name__ for member in V1_LEGAL_STEP_TYPES if member not in UNION_MEMBERS]
    assert stale == [], f"v1-legal pins name types outside the union: {stale}"


def test_registered_v2_step_types_are_union_members() -> None:
    stray = [
        step_type.__name__
        for step_type in registered_v2_step_types()
        if step_type not in UNION_MEMBERS
    ]
    assert stray == [], (
        f"{stray} are registered as v2-only step types but cannot appear in a "
        "definition, so the structural check can never observe them"
    )


def test_t3_keeps_both_of_its_halves() -> None:
    """T3 is two claims, and losing either is invisible in a green run.

    The dual-execution half runs a handful of shapes end to end; the
    corpus-wide half asserts order equivalence over EVERY frozen definition.
    A refactor that drops one leaves the other passing and the suite green,
    which is exactly how a gate stops guarding without anyone noticing.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "test_procedures"
        / "test_linear_definitions_execute_identically.py"
    ).read_text()
    for required in (
        "def test_the_successor_walk_visits_the_flat_list_order",
        "def test_t3_a_linear_definition_runs_identically_through_both_executors",
    ):
        assert required in source, f"T3 lost a half: {required} is gone"
