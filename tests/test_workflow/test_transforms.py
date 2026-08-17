"""Pure workflow-transform donor oracles retained after PC-D runtime deletion."""

from __future__ import annotations

import pytest

from cruxible_core.config.schema import (
    AggregateItemsSpec,
    DedupeItemsSpec,
    FilterItemsSpec,
    JoinItemsSpec,
    ShapeItemsSpec,
)
from cruxible_core.errors import QueryExecutionError
from cruxible_core.workflow.transforms import (
    aggregate_items,
    dedupe_items,
    filter_items,
    join_items,
    shape_items,
)


def test_shape_items_casts_and_drops_missing_required_rows() -> None:
    result = shape_items(
        "shape",
        ShapeItemsSpec(
            items=[
                {"asset_id": "A-1", "priority": " 1 ", "exposed": "true"},
                {"asset_id": "", "priority": "2", "exposed": "false"},
            ],
            fields={
                "asset_id": "$item.asset_id",
                "priority": "$item.priority",
                "exposed": "$item.exposed",
            },
            casts={"priority": "int", "exposed": "bool"},
            required=["asset_id"],
            on_missing_required="drop",
        ),
        {},
        {},
    )

    assert result == {
        "items": [{"asset_id": "A-1", "priority": 1, "exposed": True}],
        "input_count": 2,
        "output_count": 1,
        "dropped_count": 1,
        "drop_examples": [{"index": 1, "missing": ["asset_id"]}],
    }


def test_shape_items_rejects_cast_failures() -> None:
    spec = ShapeItemsSpec(
        items=[{"id": "A", "priority": "42px"}],
        fields={"id": "$item.id", "priority": "$item.priority"},
        casts={"priority": "int"},
    )

    with pytest.raises(QueryExecutionError, match="could not cast field 'priority'"):
        shape_items("shape", spec, {}, {})


def test_join_items_fans_out_in_stable_input_order() -> None:
    result = join_items(
        "join",
        JoinItemsSpec(
            left_items=[
                {"asset_id": "A-1", "product_id": "P-1"},
                {"asset_id": "A-2", "product_id": "P-2"},
            ],
            right_items=[
                {"cve_id": "CVE-1", "product_id": "P-1"},
                {"cve_id": "CVE-2", "product_id": "P-1"},
                {"cve_id": "skip", "product_id": None},
            ],
            left_key="$item.product_id",
            right_key="$item.product_id",
            fields={
                "asset_id": "$item.left.asset_id",
                "cve_id": "$item.right.cve_id",
            },
        ),
        {},
        {},
    )

    assert result["items"] == [
        {"asset_id": "A-1", "cve_id": "CVE-1"},
        {"asset_id": "A-1", "cve_id": "CVE-2"},
    ]
    assert result["skipped_right_count"] == 1
    assert result["matched_left_count"] == 1


def test_filter_items_combines_exact_and_typed_comparison_predicates() -> None:
    result = filter_items(
        "filter",
        FilterItemsSpec(
            items=[
                {"kind": "release", "score": "10"},
                {"kind": "release", "score": "2"},
                {"kind": "draft", "score": "20"},
            ],
            where={"kind": "release"},
            comparisons=[{"left": "$item.score", "op": "gte", "right": 5, "value_type": "int"}],
        ),
        {},
        {},
    )

    assert result["items"] == [{"kind": "release", "score": "10"}]
    assert result["filtered_count"] == 2


def test_aggregate_items_groups_and_coerces_numeric_measures() -> None:
    result = aggregate_items(
        "aggregate",
        AggregateItemsSpec(
            items=[
                {"team": "A", "cost": "2"},
                {"team": "B", "cost": "4"},
                {"team": "A", "cost": "3"},
            ],
            group_by={"team": "$item.team"},
            measures={
                "count": {"count": True},
                "cost": {"sum": {"value": "$item.cost", "value_type": "int"}},
            },
        ),
        {},
        {},
    )

    assert result["items"] == [
        {"team": "A", "count": 2, "cost": 5},
        {"team": "B", "count": 1, "cost": 4},
    ]


def test_dedupe_items_keeps_the_highest_rank_per_stable_key() -> None:
    result = dedupe_items(
        "dedupe",
        DedupeItemsSpec(
            items=[
                {"id": "A", "score": 1},
                {"id": "B", "score": 8},
                {"id": "A", "score": 9},
            ],
            keys=["$item.id"],
            strategy="max",
            rank="$item.score",
        ),
        {},
        {},
    )

    assert result["items"] == [{"id": "A", "score": 9}, {"id": "B", "score": 8}]
    assert result["duplicate_count"] == 1
