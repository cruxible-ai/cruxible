"""Pure v1-to-v2 procedure lift coverage for the convergence sweep."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.procedure.digest import compute_node_digests
from cruxible_core.procedure.migration import (
    assert_pure_v1_to_v2_lift,
    lift_v1_procedure_definition,
)
from cruxible_core.procedure.types import ProcedureDefinition, compute_procedure_definition_digest
from tests.test_procedures.conftest import provider_definition


def _payload(definition: ProcedureDefinition) -> dict[str, Any]:
    return definition.model_dump(mode="json", by_alias=True, exclude_none=True)


def test_lift_adds_only_graph_format_and_preserves_node_local_digests() -> None:
    predecessor = provider_definition("lift_without_rewrite")

    successor = lift_v1_procedure_definition(predecessor)

    predecessor_payload = _payload(predecessor)
    successor_payload = _payload(successor)
    assert successor_payload.pop("graph_format") == 2
    assert successor_payload == predecessor_payload
    assert compute_procedure_definition_digest(successor) != compute_procedure_definition_digest(
        predecessor
    )
    assert {
        node_id: digests.local_digest
        for node_id, digests in compute_node_digests(successor).items()
    } == {
        node_id: digests.local_digest
        for node_id, digests in compute_node_digests(predecessor).items()
    }


def test_lift_that_would_change_a_step_is_refused_and_names_the_procedure() -> None:
    predecessor = provider_definition("named_step_refusal")
    successor = lift_v1_procedure_definition(predecessor)
    changed_payload = _payload(successor)
    changed_steps = changed_payload["steps"]
    assert isinstance(changed_steps, list)
    changed_steps[0]["input"]["value"] = 7
    changed_successor = ProcedureDefinition.model_validate(changed_payload)

    with pytest.raises(
        ConfigError,
        match="Procedure migration refused for 'named_step_refusal': lift would change a step",
    ):
        assert_pure_v1_to_v2_lift(predecessor, changed_successor)
