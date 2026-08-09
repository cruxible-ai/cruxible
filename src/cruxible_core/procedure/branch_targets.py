"""Closed classification of procedure control-edge target kinds."""

from __future__ import annotations

from collections.abc import Collection
from typing import get_args

from cruxible_core.config.schema import StepKind
from cruxible_core.procedure.types import _TOP_LEVEL_STEP_KINDS

PROCEDURE_ONLY_NODE_KINDS: frozenset[str] = frozenset(
    {"repeat", "guard", "project", "propose_group_from"}
)

BRANCH_TARGETABLE_KINDS: frozenset[str] = frozenset(
    {
        "query",
        "provider",
        "assert",
        "assert_not_truncated",
        "assert_count",
        "assert_exists",
        "shape_items",
        "join_items",
        "filter_items",
        "aggregate_items",
        "dedupe_items",
        "repeat",
        "guard",
        "project",
        "propose_group_from",
    }
)
"""Kinds whose entry may safely be selected by a procedure control edge."""

NEVER_BRANCH_TARGETABLE: dict[str, str] = {
    "make_candidates": "configured-workflow proposal staging is outside procedures",
    "map_signals": "configured-workflow proposal staging is outside procedures",
    "propose_relationship_group": "procedures use the single pending-group bridge instead",
    "make_entities": "configured-workflow mutation staging is outside procedures",
    "make_relationships": "configured-workflow mutation staging is outside procedures",
    "register_source_artifacts": "source-artifact mutation is outside procedures",
    "apply_entities": "direct graph writes are outside procedure control flow",
    "apply_relationships": "direct graph writes are outside procedure control flow",
    "apply_all": "direct graph writes are outside procedure control flow",
}
"""Kinds refused from procedures, each pinned to a review-visible reason."""


def assert_branch_target_classification(
    *,
    step_kinds: Collection[str] | None = None,
    top_level_step_kinds: Collection[str] | None = None,
) -> None:
    """Fail when either kind universe gains an unclassified member."""
    shared = set((str(kind) for kind in get_args(StepKind)) if step_kinds is None else step_kinds)
    top_level = set(_TOP_LEVEL_STEP_KINDS if top_level_step_kinds is None else top_level_step_kinds)
    universe = shared | set(PROCEDURE_ONLY_NODE_KINDS)
    targetable = set(BRANCH_TARGETABLE_KINDS)
    never = set(NEVER_BRANCH_TARGETABLE)
    unclassified = sorted(universe - targetable - never)
    overlap = sorted(targetable & never)
    missing_top_level = sorted(top_level - targetable)
    stale = sorted((targetable | never) - universe)
    assert not unclassified, f"unclassified procedure branch-target kinds: {unclassified}"
    assert not overlap, f"kinds classified as both targetable and never-targetable: {overlap}"
    assert not missing_top_level, (
        f"_TOP_LEVEL_STEP_KINDS members missing from BRANCH_TARGETABLE_KINDS: {missing_top_level}"
    )
    assert not stale, f"branch-target classification names kinds outside the universe: {stale}"


__all__ = [
    "BRANCH_TARGETABLE_KINDS",
    "NEVER_BRANCH_TARGETABLE",
    "PROCEDURE_ONLY_NODE_KINDS",
    "assert_branch_target_classification",
]
