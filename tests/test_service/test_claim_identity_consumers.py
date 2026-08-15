"""The consumers of edge identity: receipts, records, working set, dry runs.

Grouped here because they share one question -- when a claim's identity is
available, does the thing that records something ABOUT that claim record the
identity, and does it take it from the durable write rather than the request?
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import service_add_entities, service_add_relationships
from cruxible_core.working_set import normalize_edge_record, record_identity, validate_record
from tests.test_cli.conftest import CAR_PARTS_YAML


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CAR_PARTS_YAML)
    created = CruxibleInstance.init(tmp_path, "config.yaml")
    service_add_entities(
        created,
        [
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1",
                properties={"part_number": "BP-1", "name": "Pads", "category": "brakes"},
            ),
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-1",
                properties={"vehicle_id": "V-1", "year": 2024, "make": "Honda", "model": "Civic"},
            ),
        ],
    )
    return created


def _edge() -> RelationshipInstance:
    return RelationshipInstance(
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        properties={"verified": True},
    )


def _stored(instance: CruxibleInstance) -> RelationshipInstance:
    instance.invalidate_graph_cache()
    edge = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert edge is not None
    return edge


def _write_nodes(instance: CruxibleInstance, receipt_id: str) -> list[dict]:
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
    finally:
        store.close()
    assert receipt is not None
    return [node.detail for node in receipt.nodes if node.node_type == "relationship_write"]


def test_create_receipt_stamps_the_durable_claim_id(instance: CruxibleInstance) -> None:
    """The create case is the one the old ordering could never get right."""
    result = service_add_relationships(instance, [_edge()], source="test", source_ref="t")
    assert result.receipt_id is not None
    details = _write_nodes(instance, result.receipt_id)
    assert [detail["claim_id"] for detail in details] == [_stored(instance).claim_id]
    assert details[0]["is_update"] is False


def test_update_receipt_stamps_the_same_claim_id(instance: CruxibleInstance) -> None:
    service_add_relationships(instance, [_edge()], source="test", source_ref="t")
    created_id = _stored(instance).claim_id
    updated = service_add_relationships(
        instance,
        [_edge().model_copy(update={"properties": {"verified": False}})],
        source="test",
        source_ref="t",
    )
    assert updated.receipt_id is not None
    details = _write_nodes(instance, updated.receipt_id)
    assert details[0]["is_update"] is True
    assert details[0]["claim_id"] == created_id
    assert _stored(instance).claim_id == created_id


def test_dry_run_results_exclude_claim_id(instance: CruxibleInstance) -> None:
    """Previews persist nothing, so they must not advertise an identity."""
    preview = service_add_relationships(
        instance,
        [_edge()],
        source="test",
        source_ref="t",
        dry_run=True,
    )
    payload = preview.__dict__ if hasattr(preview, "__dict__") else {}
    assert "claim_id" not in payload
    assert preview.added == 1
    # And the preview really did not write.
    instance.invalidate_graph_cache()
    assert instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits") is None


# ------------------------------------------------------------- working set


def _normalized(payload: dict) -> dict:
    return normalize_edge_record(
        payload,
        read_revision=1,
        as_of="2026-01-01T00:00:00Z",
        receipt_refs=[],
        source_cmd="test",
    )


def test_working_set_normalization_keeps_claim_id() -> None:
    """The normalizer drops unknown fields; claim_id must be a KNOWN one."""
    record = _normalized(
        {
            "relationship_type": "fits",
            "from_type": "Part",
            "from_id": "BP-1",
            "to_type": "Vehicle",
            "to_id": "V-1",
            "edge_key": 3,
            "claim_id": "CLM-abc",
            "properties": {},
            "metadata": {},
        }
    )
    assert record["claim_id"] == "CLM-abc"
    assert validate_record(record) is None


def test_working_set_dedupes_on_claim_id_across_a_re_key() -> None:
    """One claim, two edge_keys (a pull re-keyed it) -> ONE cache identity."""
    before = _normalized(
        {
            "relationship_type": "fits",
            "from_type": "Part",
            "from_id": "BP-1",
            "to_type": "Vehicle",
            "to_id": "V-1",
            "edge_key": 3,
            "claim_id": "CLM-abc",
            "properties": {},
            "metadata": {},
        }
    )
    after = dict(before, edge_key=11)
    assert record_identity(before) == record_identity(after)


def test_working_set_falls_back_to_tuple_and_edge_key_without_an_id() -> None:
    """Pre-identity cache lines keep dedupling exactly as they always did."""
    legacy = _normalized(
        {
            "relationship_type": "fits",
            "from_type": "Part",
            "from_id": "BP-1",
            "to_type": "Vehicle",
            "to_id": "V-1",
            "edge_key": 3,
            "properties": {},
            "metadata": {},
        }
    )
    assert legacy["claim_id"] is None
    assert record_identity(legacy) == (
        "edge",
        "fits",
        "Part",
        "BP-1",
        "Vehicle",
        "V-1",
        3,
    )
    assert record_identity(dict(legacy, edge_key=11)) != record_identity(legacy)


def test_working_set_rejects_a_non_string_claim_id() -> None:
    record = _normalized(
        {
            "relationship_type": "fits",
            "from_type": "Part",
            "from_id": "BP-1",
            "to_type": "Vehicle",
            "to_id": "V-1",
            "properties": {},
            "metadata": {},
        }
    )
    record["claim_id"] = 7
    assert validate_record(record) == "claim_id must be a string or null"
