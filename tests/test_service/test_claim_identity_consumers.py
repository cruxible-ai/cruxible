"""The retained receipt and preview consumers of durable edge identity.

Grouped here because they share one question -- when a claim's identity is
available, does the thing that records something ABOUT that claim record the
identity, and does it take it from the durable write rather than the request?
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service.mutations import service_add_entities, service_add_relationships
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
