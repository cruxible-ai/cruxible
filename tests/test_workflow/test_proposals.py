"""Tests for built-in workflow proposal helpers."""

from __future__ import annotations

from cruxible_core.config.schema import MapSignalsSpec
from cruxible_core.workflow.proposals import map_signal_batch, signal_mapping_snapshot


def test_map_signal_batch_records_score_bucket_basis() -> None:
    spec = MapSignalsSpec(
        signal_source="model_score",
        items=[
            {"from_id": "A", "to_id": "B1", "score": 0.9, "note": "high"},
            {"from_id": "A", "to_id": "B2", "score": 0.6, "note": "middle"},
            {"from_id": "A", "to_id": "B3", "score": 0.2, "note": "low"},
        ],
        from_id="$item.from_id",
        to_id="$item.to_id",
        evidence="$item.note",
        score={"path": "score", "support_gte": 0.8, "unsure_gte": 0.5},
    )

    batch = map_signal_batch("score_signals", spec, {}, {})

    assert [signal.signal for signal in batch.signals] == [
        "support",
        "unsure",
        "contradict",
    ]
    assert [signal.basis.model_dump(mode="json") for signal in batch.signals] == [
        {"mode": "score", "path": "score", "value": 0.9, "matched": "support_gte"},
        {"mode": "score", "path": "score", "value": 0.6, "matched": "unsure_gte"},
        {
            "mode": "score",
            "path": "score",
            "value": 0.2,
            "matched": "below_unsure_gte",
        },
    ]
    assert signal_mapping_snapshot(spec) == {
        "mode": "score",
        "path": "score",
        "support_gte": 0.8,
        "unsure_gte": 0.5,
    }


def test_map_signal_batch_records_enum_bucket_basis() -> None:
    spec = MapSignalsSpec(
        signal_source="catalog",
        items=[{"from_id": "A", "to_id": "B", "verdict": "reject"}],
        from_id="$item.from_id",
        to_id="$item.to_id",
        enum={
            "path": "verdict",
            "map": {"match": "support", "fallback": "unsure", "reject": "contradict"},
        },
    )

    batch = map_signal_batch("catalog_signals", spec, {}, {})

    assert batch.signals[0].signal == "contradict"
    assert batch.signals[0].basis is not None
    assert batch.signals[0].basis.model_dump(mode="json") == {
        "mode": "enum",
        "path": "verdict",
        "value": "reject",
        "matched": "reject",
    }
    assert signal_mapping_snapshot(spec) == {
        "mode": "enum",
        "path": "verdict",
        "map": {"match": "support", "fallback": "unsure", "reject": "contradict"},
    }
