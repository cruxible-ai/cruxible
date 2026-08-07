"""The procedure step union is untagged, so its members must be distinguishable.

Converting to a ``Field(discriminator=...)`` union would put a discriminator
key in the wire form of every step and therefore move every stored v1
definition's digest. The union stays untagged and this test carries the burden
the discriminator would have carried: every member must round-trip through the
union back to its own type, and no member's wire form may validate as another.
"""

from __future__ import annotations

import typing
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from cruxible_core.config.schema import WorkflowStepSchema
from cruxible_core.procedure.types import (
    ProcedureFlowStepSchema,
    ProcedureGuardStepSchema,
    ProcedureProjectStepSchema,
    ProcedureRepeatStepSchema,
    ProcedureStepSchema,
)

UNION_MEMBERS: tuple[type[BaseModel], ...] = typing.get_args(ProcedureStepSchema)
ADAPTER: TypeAdapter[Any] = TypeAdapter(ProcedureStepSchema)

REPRESENTATIVES: dict[type[BaseModel], dict[str, Any]] = {
    WorkflowStepSchema: {"id": "plain", "provider": "scorer", "input": {}, "as": "rows"},
    ProcedureRepeatStepSchema: {
        "id": "loop",
        "as": "rows",
        "repeat": {
            "max_attempts": 2,
            "until": {
                "left": "$steps.attempt.done",
                "op": "eq",
                "right": True,
                "message": "not done",
            },
            "steps": [{"id": "attempt", "provider": "scorer", "input": {}, "as": "attempt"}],
        },
    },
    ProcedureGuardStepSchema: {
        "id": "gate",
        "guard": {"left": "$steps.rows.count", "op": "gt", "right": 0},
        "on_true": "build",
        "on_false": "$abort",
        "message": "no rows",
    },
    ProcedureFlowStepSchema: {
        "step": {"id": "wrapped", "provider": "scorer", "input": {}, "as": "rows"},
        "next": "gate",
    },
    ProcedureProjectStepSchema: {
        "id": "shape",
        "as": "result",
        "project": {"fields": {"value": "$steps.rows.value"}},
    },
}


def test_every_union_member_has_a_representative() -> None:
    assert set(UNION_MEMBERS) == set(REPRESENTATIVES), (
        "a new step type joined the union without a representative here; it has "
        "not been shown distinguishable from the existing members"
    )


@pytest.mark.parametrize("member", UNION_MEMBERS, ids=lambda m: m.__name__)
def test_each_member_round_trips_to_its_own_type(member: type[BaseModel]) -> None:
    parsed = ADAPTER.validate_python(REPRESENTATIVES[member])
    assert type(parsed) is member
    reparsed = ADAPTER.validate_python(parsed.model_dump(mode="json", by_alias=True))
    assert type(reparsed) is member


@pytest.mark.parametrize("member", UNION_MEMBERS, ids=lambda m: m.__name__)
def test_no_other_member_accepts_this_members_wire_form(member: type[BaseModel]) -> None:
    payload = REPRESENTATIVES[member]
    accepting = [other for other in UNION_MEMBERS if _accepts(other, payload)]
    assert accepting == [member], (
        f"{member.__name__}'s wire form is also accepted by "
        f"{[other.__name__ for other in accepting if other is not member]}; the "
        "untagged union can no longer tell them apart"
    )


def _accepts(model: type[BaseModel], payload: dict[str, Any]) -> bool:
    try:
        model.model_validate(payload)
    except Exception:  # noqa: BLE001 - "did it parse" is the whole question
        return False
    return True


def test_the_flow_wrapper_has_no_id_of_its_own() -> None:
    """A settable outer id would be an unconstrained alias for one node."""
    assert "id" not in ProcedureFlowStepSchema.model_fields
    wrapper = ProcedureFlowStepSchema.model_validate(REPRESENTATIVES[ProcedureFlowStepSchema])
    assert wrapper.id == "wrapped"
    assert wrapper.as_ == "rows"
