"""Ordered enums on the client: World sugar, the world stub and vocabulary.yaml."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import Cruxible
from cruxible_client.authoring.compact_query import QueryNameError
from cruxible_client.authoring.vocabulary_file import _field_entry, entry_input, lower_entry
from cruxible_client.contracts.shapes import EnumType
from cruxible_client.contracts.vocabulary_file import VocabularyField
from tests.support.claim_types import claim_type
from tests.test_client.test_playbill_sdk_query import _QueryClient
from tests.test_client.test_playbill_sdk_world import _workspace

WHEN = datetime(2026, 9, 7, 12, tzinfo=UTC)
RANKED = {"kind": "enum", "members": ["low", "high"], "ordered": True}


def _playbill(tmp_path: Path, *, ordered: bool) -> tuple[Cruxible, _QueryClient]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _workspace(tmp_path)
    client = _QueryClient()
    if ordered:
        client.severity_overrides = {"value": RANKED}
    playbill = Cruxible._from_client(  # type: ignore[arg-type]
        client, instance_id="inst_world", workspace=tmp_path, clock=lambda: WHEN
    )
    return playbill, client


@pytest.mark.parametrize("operator", ["lt", "lte", "gt", "gte"])
def test_world_sugar_orders_an_ordered_enum_by_member(tmp_path: Path, operator: str) -> None:
    playbill, _client = _playbill(tmp_path, ordered=True)
    kind = playbill.world().sec.vulnerability
    request = kind.where(**{f"severity__{operator}": "low"}).request()
    assert [(item.operator, item.value) for item in request.where] == [(operator, "low")]
    with pytest.raises(QueryNameError, match="not a member") as refused:
        kind.where(**{f"severity__{operator}": "urgent"})
    assert "high" in refused.value.nearest


def test_world_sugar_refuses_to_order_an_unordered_enum(tmp_path: Path) -> None:
    playbill, client = _playbill(tmp_path, ordered=False)
    with pytest.raises(QueryNameError, match="its enum is unordered"):
        playbill.world().sec.vulnerability.where(severity__gt="low")
    assert client.requests == []


def test_the_world_reads_ordered_from_the_value_type(tmp_path: Path) -> None:
    ordered, _ = _playbill(tmp_path / "a", ordered=True)
    plain, _ = _playbill(tmp_path / "b", ordered=False)
    severity = ordered.world().claim_type("sec.vuln.severity")
    assert severity.value_type == EnumType(members=("low", "high"), ordered=True)
    assert severity.members == ("low", "high")
    assert plain.world().claim_type("sec.vuln.severity").value_type.ordered is False  # type: ignore[union-attr]


def test_the_stub_types_rank_filters_only_for_an_ordered_enum(tmp_path: Path) -> None:
    ordered, _ = _playbill(tmp_path / "a", ordered=True)
    plain, _ = _playbill(tmp_path / "b", ordered=False)
    ranked_stub = ordered.world().stub()
    plain_stub = plain.world().stub()
    for operator in ("lt", "lte", "gt", "gte"):
        assert f"severity__{operator}: Literal['low', 'high'] | None = ...," in ranked_stub
        assert f"severity__{operator}:" not in plain_stub
    assert "# ordered=true (members in ascending rank)" in ranked_stub
    assert "ordered=true" not in plain_stub


# -- vocabulary.yaml --------------------------------------------------------------------


def _field(**values: Any) -> VocabularyField:
    return VocabularyField.model_validate(values)


def test_ordered_goes_with_enum() -> None:
    with pytest.raises(ValueError, match="ordered goes with enum"):
        _field(ordered=True)
    with pytest.raises(ValueError, match="ordered goes with enum"):
        _field(ordered=True, value="string{max_len: 32}")


def test_an_ordered_entry_lowers_to_an_ordered_enum_and_exports_back() -> None:
    entry = _field(enum=["low", "medium", "high"], ordered=True, description="How bad it is.")
    lowered = lower_entry("t.item.priority", entry, None)
    assert lowered.value == EnumType(members=("low", "medium", "high"), ordered=True)
    exported = _field_entry(lowered, key=False)
    assert exported["enum"] == ["low", "medium", "high"] and exported["ordered"] is True
    assert list(exported)[:2] == ["enum", "ordered"]


def test_an_unordered_enum_exports_without_ordered() -> None:
    lowered = lower_entry("t.item.status", _field(enum=["open", "closed"]), None)
    assert lowered.value == EnumType(members=("open", "closed"))
    assert "ordered" not in _field_entry(lowered, key=False)


def test_an_edit_that_leaves_ordered_out_keeps_the_accepted_ordering() -> None:
    accepted = claim_type(
        "t.item.priority", value_type={"kind": "enum", "members": ["low", "high"], "ordered": True}
    )
    kept = entry_input("t.item.priority", _field(enum=["low", "medium", "high"]), accepted)
    assert kept.value == EnumType(members=("low", "medium", "high"), ordered=True)
    dropped = entry_input("t.item.priority", _field(enum=["low", "high"], ordered=False), accepted)
    assert dropped.value == EnumType(members=("low", "high"))
