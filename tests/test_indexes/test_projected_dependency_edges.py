"""A cold dependency edge tree from projected pins equals the from-scratch oracle."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.claims.closure import _edges, build_dependency_edge_tree, dependency_artifacts
from cruxible_core.indexes.evaluated_state import EvaluationRows


def test_projected_edges_equal_edges_parsed_from_every_member(tmp_path: Path) -> None:
    from tests.core_support._knowledge_loop_support import seed_claims

    # Subjects, ClaimTypes, CaptureContracts and Claims: every pin role it carries.
    instance, _owner = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    oracle = _edges(dependency_artifacts(instance.tree_at(coordinate.git_oid)))
    assert oracle
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = EvaluationRows(projection)
        projected = rows.projected_edges()
        assert projected == oracle
        index = rows.dependencies()
        assert index.edge_root == build_dependency_edge_tree(oracle).root
