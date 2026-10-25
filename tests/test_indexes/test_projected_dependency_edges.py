"""A cold dependency edge tree from projected pins equals the from-scratch oracle."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.claims.closure import _edges, build_dependency_edge_tree, dependency_artifacts
from cruxible_core.indexes.evaluated_state import EvaluationRows


def _world(tmp_path, historical):
    from tests.core_support._knowledge_loop_support import seed_claims, seed_claims_into
    from tests.core_support.legacy_capture_builders import initialize_legacy_local

    if not historical:
        return seed_claims(tmp_path)
    instance, owner = initialize_legacy_local(tmp_path)
    seed_claims_into(instance, owner)
    return instance, owner


@pytest.mark.parametrize("historical", [True, False])
def test_projected_edges_equal_edges_parsed_from_every_member(tmp_path: Path, historical) -> None:

    # Subjects, ClaimTypes, CaptureContracts and Claims: every pin role it carries.
    instance, _owner = _world(tmp_path, historical)
    coordinate = instance.accepted_coordinate()
    oracle = _edges(
        dependency_artifacts(instance.tree_at(coordinate.git_oid), by_name=not historical),
        by_name=not historical,
    )
    assert oracle
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = EvaluationRows(projection)
        projected = rows.projected_edges()
        assert projected == oracle
        index = rows.dependencies()
        assert index.edge_root == build_dependency_edge_tree(oracle).root


@pytest.mark.parametrize("historical", [True, False])
def test_selected_states_and_outgoing_proofs_are_carried_and_equal_the_cold_path(
    tmp_path, monkeypatch, historical
):
    from cruxible_client.contracts.content_memo import artifact_request
    from cruxible_core.claims import closure
    from cruxible_core.indexes import evaluated_state

    instance, _owner = _world(tmp_path, historical)
    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    cold = {state.path: state for state in dependency_artifacts(tree, by_name=not historical)}
    calls = []
    original = evaluated_state._outgoing_edges

    def counted(source, **kwargs):
        calls.append(source.path)
        return original(source, **kwargs)

    monkeypatch.setattr(evaluated_state, "_outgoing_edges", counted)
    with artifact_request(), instance.bind_accepted_projection(coordinate) as projection:
        rows = EvaluationRows(projection)
        for path, expected in cold.items():
            # The early format check and SQL-selected dependency reader use
            # the same immutable state; the uncarried parser is the oracle.
            early = closure.parse_dependency_artifact(path, tree[path], by_name=not historical)
            assert rows.state(path) is early
            assert (
                early
                == expected
                == closure._parse_dependency_artifact(path, tree[path], by_name=not historical)
            )
            first = rows.outgoing(path)
            assert rows.outgoing(path) is first
            assert first == original(
                expected,
                states=cold,
                by_name=not historical,
                paths_by_identity={state.identity.qualified: state.path for state in cold.values()},
            )
        assert len(calls) == len(cold)
