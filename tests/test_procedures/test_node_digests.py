"""Corpus G4-G9, G11, G12 -- the Merkle properties, stated as tests.

G1-G3 pin the frozen v1 identity. These pin what the two node-digest flavours
promise: which edits move which digest, and -- more importantly -- which edits
move NOTHING, because that is the half a reading's survival depends on.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.procedure.digest import (
    BASE_ENVELOPE_FIELDS,
    DIGEST_FUNCTIONS,
    compute_node_digests,
    definition_envelope,
    registered_envelope_fields,
)
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    compute_procedure_definition_digest,
)
from tests.test_procedures.test_definition_digest_corpus import ENTRIES, IDS

_BUDGET = {"wall_clock_s": 30, "max_provider_calls": 5}


def _branching_payload() -> dict[str, Any]:
    return {
        "name": "merkle_probe",
        "graph_format": 2,
        "steps": [
            {"id": "read", "provider": "scorer", "input": {"seed": 1}, "as": "rows"},
            {
                "id": "gate",
                "guard": {"left": "count(rows, items)", "op": "gt", "right": 0},
                "on_true": "hot",
                "on_false": "cold",
                "message": "no rows",
            },
            {
                "step": {"id": "hot", "provider": "scorer", "input": {"arm": "hot"}, "as": "hot_v"},
                "next": "tail",
            },
            {"id": "cold", "provider": "scorer", "input": {"arm": "cold"}, "as": "cold_v"},
            {"id": "tail", "provider": "scorer", "input": {}, "as": "final"},
        ],
        "returns": "final",
        "precondition": {},
        "budget": _BUDGET,
    }


def _digests(payload: dict[str, Any]) -> dict[str, Any]:
    definition = ProcedureDefinition.model_validate(payload)
    return {node_id: node for node_id, node in compute_node_digests(definition).items()}


def _root(payload: dict[str, Any]) -> str:
    return compute_procedure_definition_digest(ProcedureDefinition.model_validate(payload))


# --- G4 --------------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_g4_an_absent_v2_field_moves_no_byte_and_an_explicit_null_is_refused(
    entry: dict[str, Any],
) -> None:
    """G4, in the stronger form the discriminator now permits.

    The property G4 pins is that an optional v2 field cannot move a v1 digest.
    For `graph_format` that is now guaranteed by REFUSAL rather than by
    normalization: absence is the only spelling of v1, so there is no second
    authored form to compare against. That is strictly better than "both
    spellings digest the same", because the two spellings were never
    equivalent -- a 0.3 core refuses the explicit null and accepts the absence.
    """
    baseline = ProcedureDefinition.model_validate(entry["normalized_dump_v032"])
    assert compute_procedure_definition_digest(baseline) == entry["digest_v1"]
    assert "graph_format" not in baseline.model_dump(mode="json", by_alias=True, exclude_none=True)
    with pytest.raises(ConfigError, match="spelled by ABSENCE"):
        ProcedureDefinition.model_validate({**entry["normalized_dump_v032"], "graph_format": None})


# --- G5 --------------------------------------------------------------------


def test_g5_node_digests_are_deterministic_and_insertion_order_insensitive() -> None:
    payload = _branching_payload()
    reordered = copy.deepcopy(payload)
    reordered["steps"][0]["input"] = {"z": 1, "a": 2}
    payload["steps"][0]["input"] = {"a": 2, "z": 1}
    assert _digests(payload)["read"].local_digest == _digests(reordered)["read"].local_digest
    assert _digests(payload) == _digests(payload)


# --- G6 --------------------------------------------------------------------


def test_g6_editing_a_node_moves_it_and_its_ancestors_and_nothing_below() -> None:
    before = _digests(_branching_payload())
    edited_payload = _branching_payload()
    edited_payload["steps"][4]["input"] = {"changed": True}
    after = _digests(edited_payload)

    assert after["tail"].local_digest != before["tail"].local_digest
    # Every ancestor's subtree moves, because the subtree is exactly "this node
    # and everything it can lead to".
    for ancestor in ("read", "gate", "hot", "cold"):
        assert after[ancestor].subtree_digest != before[ancestor].subtree_digest
        assert after[ancestor].local_digest == before[ancestor].local_digest
    assert _root(_branching_payload()) != _root(edited_payload)


def test_g6_no_descendant_digest_moves_when_an_ancestor_changes() -> None:
    before = _digests(_branching_payload())
    edited_payload = _branching_payload()
    edited_payload["steps"][0]["input"] = {"seed": 99}
    after = _digests(edited_payload)
    for descendant in ("gate", "hot", "cold", "tail"):
        assert after[descendant].local_digest == before[descendant].local_digest
        assert after[descendant].subtree_digest == before[descendant].subtree_digest


# --- G7 / G11 --------------------------------------------------------------


def test_g7_swapping_arms_moves_the_guard_subtree_but_not_its_local() -> None:
    """The control-target exclusion, and the reason it exists.

    If arm targets were part of the guard's local preimage, a swap would change
    the identity of a decision point that still asks exactly the same question,
    and every reading bound to it would detach.
    """
    before = _digests(_branching_payload())
    swapped = _branching_payload()
    swapped["steps"][1]["on_true"] = "cold"
    swapped["steps"][1]["on_false"] = "hot"
    after = _digests(swapped)
    assert after["gate"].local_digest == before["gate"].local_digest
    assert after["gate"].subtree_digest != before["gate"].subtree_digest


def test_g11_retargeting_on_true_moves_the_subtree_but_not_the_local() -> None:
    payload = _branching_payload()
    # Give the true arm a second reachable step so retargeting away from `hot`
    # leaves no orphan (R3 would refuse the definition before it could be
    # digested).
    payload["steps"][1]["on_true"] = "hot"
    payload["steps"][2] = {**payload["steps"][2], "next": "cold"}
    before = _digests(payload)
    retargeted = copy.deepcopy(payload)
    retargeted["steps"][1]["on_true"] = "cold"
    retargeted["steps"][1]["on_false"] = "hot"
    after = _digests(retargeted)
    assert after["gate"].local_digest == before["gate"].local_digest
    assert after["gate"].subtree_digest != before["gate"].subtree_digest


def test_a_flow_edge_does_not_change_the_wrapped_nodes_local_identity() -> None:
    """Adding an edge to a step is a topology edit, not a content edit."""
    payload = _branching_payload()
    unwrapped = _branching_payload()
    unwrapped["steps"][2] = unwrapped["steps"][2]["step"]
    assert _digests(payload)["hot"].local_digest == _digests(unwrapped)["hot"].local_digest


# --- G8 --------------------------------------------------------------------


def test_g8_editing_inside_the_false_arm_leaves_the_guard_and_the_true_arm_alone() -> None:
    before = _digests(_branching_payload())
    edited = _branching_payload()
    edited["steps"][3]["input"] = {"arm": "cold", "extra": True}
    after = _digests(edited)
    assert after["gate"].local_digest == before["gate"].local_digest
    assert after["hot"].local_digest == before["hot"].local_digest
    assert after["hot"].subtree_digest == before["hot"].subtree_digest
    assert after["cold"].local_digest != before["cold"].local_digest
    assert after["gate"].subtree_digest != before["gate"].subtree_digest


# --- G9 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "renamed_probe"),
        ("description", "a description that was not there"),
        ("contract_in", "cruxible.JsonObject"),
        ("contract_out", "cruxible.JsonObject"),
        ("declared_tier", "admin"),
        ("evidence_outputs", ["final"]),
        ("precondition", {"entity_type": "Task", "condition": {"status": "open"}}),
        ("budget", {"wall_clock_s": 31, "max_provider_calls": 5}),
    ],
)
def test_g9_the_envelope_is_total(field: str, value: Any) -> None:
    """Nothing definitional escapes the digest.

    v1's root committed only the steps, so changing a budget, a tier or a
    predeclared measurement left the identity untouched -- a definition could
    change without saying so.
    """
    payload = _branching_payload()
    baseline = _root(payload)
    payload[field] = value
    assert _root(payload) != baseline


def test_g9_returns_is_in_the_envelope() -> None:
    payload = _branching_payload()
    baseline = _root(payload)
    payload["returns"] = "hot_v"
    assert _root(payload) != baseline


# --- G12 -------------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_g12_lifting_to_v2_moves_the_definition_digest_and_no_node_local_digest(
    entry: dict[str, Any],
) -> None:
    """Sweep continuity, stated precisely.

    A pure v1->v2 lift adds `graph_format: 2` at the ENVELOPE level and changes
    no step, so every node local digest is byte-identical across it. Node- and
    arm-grain readings therefore cross the boundary by digest match without
    anything special happening. The definition digest changes by design.
    """
    v1 = ProcedureDefinition.model_validate(entry["normalized_dump_v032"])
    v2 = ProcedureDefinition.model_validate({**entry["normalized_dump_v032"], "graph_format": 2})
    before = {node_id: node.local_digest for node_id, node in compute_node_digests(v1).items()}
    after = {node_id: node.local_digest for node_id, node in compute_node_digests(v2).items()}
    assert before == after
    assert compute_procedure_definition_digest(v2) != compute_procedure_definition_digest(v1)


# --- the dispatcher --------------------------------------------------------


def test_the_dispatcher_routes_each_format_to_its_own_function() -> None:
    linear = ProcedureDefinition.model_validate(
        {
            "name": "dispatch_probe",
            "steps": [{"id": "a", "provider": "scorer", "input": {}, "as": "rows"}],
            "returns": "rows",
            "precondition": {},
            "budget": _BUDGET,
        }
    )
    declared = ProcedureDefinition.model_validate(
        {**linear.model_dump(mode="json", by_alias=True, exclude_none=True), "graph_format": 2}
    )
    assert compute_procedure_definition_digest(linear) == DIGEST_FUNCTIONS[1](linear)
    assert compute_procedure_definition_digest(declared) == DIGEST_FUNCTIONS[2](declared)
    assert DIGEST_FUNCTIONS[1](linear) != DIGEST_FUNCTIONS[2](declared)


def test_the_envelope_registry_starts_with_the_base_fields_only() -> None:
    definition = ProcedureDefinition.model_validate(_branching_payload())
    assert tuple(definition_envelope(definition)) == BASE_ENVELOPE_FIELDS
    assert registered_envelope_fields() == ()


def test_a_repeat_body_lives_in_the_repeat_nodes_local_content() -> None:
    payload = {
        "name": "repeat_digest_probe",
        "graph_format": 2,
        "steps": [
            {
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
                    "steps": [
                        {"id": "attempt", "provider": "scorer", "input": {}, "as": "attempt"}
                    ],
                },
            }
        ],
        "returns": "rows",
        "precondition": {},
        "budget": _BUDGET,
    }
    digests = _digests(payload)
    # Nested nodes are addressable, so a reading can bind inside a loop body.
    assert "loop/attempt" in digests
    edited = copy.deepcopy(payload)
    edited["steps"][0]["repeat"]["steps"][0]["input"] = {"changed": True}
    # Editing the body changes what the repeat node DOES, so its LOCAL moves.
    assert _digests(edited)["loop"].local_digest != digests["loop"].local_digest
