"""A cold dependency edge tree from projected pins equals the from-scratch oracle."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.claims.closure import _edges, build_dependency_edge_tree, dependency_artifacts
from cruxible_core.indexes.evaluated_state import EvaluationRows


def _world(tmp_path):
    from tests.core_support._knowledge_loop_support import seed_claims

    return seed_claims(tmp_path)


def test_projected_edges_equal_edges_parsed_from_every_member(tmp_path: Path) -> None:

    # Subjects, ClaimTypes, CaptureContracts and Claims: every pin role it carries.
    instance, _owner = _world(tmp_path)
    coordinate = instance.accepted_coordinate()
    oracle = _edges(
        dependency_artifacts(instance.tree_at(coordinate.git_oid), by_name=True),
        by_name=True,
    )
    assert oracle
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = EvaluationRows(projection)
        projected = rows.projected_edges()
        assert projected == oracle
        index = rows.dependencies()
        assert index.edge_root == build_dependency_edge_tree(oracle).root


def test_selected_states_and_outgoing_proofs_are_carried_and_equal_the_cold_path(
    tmp_path, monkeypatch
):
    from cruxible_client.contracts.content_memo import artifact_request
    from cruxible_core.claims import closure
    from cruxible_core.indexes import evaluated_state

    instance, _owner = _world(tmp_path)
    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    cold = {state.path: state for state in dependency_artifacts(tree, by_name=True)}
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
            early = closure.parse_dependency_artifact(path, tree[path], by_name=True)
            assert rows.state(path) is early
            if path.startswith("claims/"):
                assert early is closure.parse_dependency_artifact(path, tree[path], by_name=True)
            assert (
                early
                == expected
                == closure._parse_dependency_artifact(path, tree[path], by_name=True)
            )
            first = rows.outgoing(path)
            assert rows.outgoing(path) is first
            assert first == original(
                expected,
                states=cold,
                by_name=True,
                paths_by_identity={state.identity.qualified: state.path for state in cold.values()},
            )
        assert len(calls) == len(cold)
