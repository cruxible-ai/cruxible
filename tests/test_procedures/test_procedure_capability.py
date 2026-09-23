"""A Procedure's capability is what its terminals and children need, never authored."""

import pytest

from cruxible_client.contracts.procedures.models import (
    AUTHORITY_RUNG,
    RUNG_AUTHORITY,
    derived_terminal_capability,
)


@pytest.mark.parametrize(
    ("kinds", "child_rung", "expected"),
    [
        ((), 0, "observe"),
        (("transform", "emit_capture", "return"), 0, "observe"),
        (("guard", "propose_change_set", "halt"), 0, "propose"),
        (("emit_capture", "settle_change_set"), 0, "settle"),
        (("transform", "return"), 2, "propose"),
    ],
)
def test_capability_is_the_highest_terminal_or_child_need(kinds, child_rung, expected) -> None:
    nodes = [{"kind": kind, "node_id": f"n{index}"} for index, kind in enumerate(kinds)]
    level = derived_terminal_capability(nodes, child_rung=child_rung)
    assert RUNG_AUTHORITY[level] == expected
    assert AUTHORITY_RUNG[expected] == level
