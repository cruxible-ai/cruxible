from __future__ import annotations

from pathlib import Path

from tests.support.state_cross_section import (
    CrossSectionTokenRegistry,
    QueryCrossSectionSpec,
    StateCrossSectionSpec,
    assert_matches_golden,
    build_state_cross_section,
    diff_state,
)
from tests.test_cli.conftest import CAR_PARTS_YAML

from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import service_add_entities, service_add_relationships, service_query


def _new_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CAR_PARTS_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _seed_vehicle_and_part(instance: CruxibleInstance) -> None:
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-CIVIC",
                properties={
                    "vehicle_id": "V-CIVIC",
                    "year": 2024,
                    "make": "Honda",
                    "model": "Civic",
                },
            ),
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1234",
                properties={"part_number": "BP-1234", "name": "Brake Pad", "category": "brakes"},
            ),
        ],
        _create_receipt=False,
    )


def test_cross_section_is_allowlist_based(tmp_path: Path) -> None:
    instance = _new_instance(tmp_path)
    _seed_vehicle_and_part(instance)

    report = build_state_cross_section(
        instance,
        StateCrossSectionSpec(entity_types=("Part",)),
    )

    assert report["graph"]["counts"]["entities_by_type"] == {"Part": 1}
    assert report["graph"]["entities"] == [
        {
            "entity_id": "BP-1234",
            "entity_type": "Part",
            "properties": {"category": "brakes", "name": "Brake Pad"},
        }
    ]
    assert "Vehicle" not in report["graph"]["counts"]["entities_by_type"]


def test_cross_section_tokenization_is_global_across_state_and_snapshots(
    tmp_path: Path,
) -> None:
    instance = _new_instance(tmp_path)
    _seed_vehicle_and_part(instance)
    instance.create_snapshot()

    report = build_state_cross_section(
        instance,
        StateCrossSectionSpec(include_state=True, include_snapshots=True),
    )

    assert report["state"]["head_snapshot_id"] == report["snapshots"][0]["snapshot_id"]
    assert report["state"]["head_snapshot_id"].startswith("<SNAPSHOT_")


def test_cross_section_query_capture_does_not_persist_receipts(tmp_path: Path) -> None:
    instance = _new_instance(tmp_path)
    _seed_vehicle_and_part(instance)

    report = build_state_cross_section(
        instance,
        StateCrossSectionSpec(
            queries=(
                QueryCrossSectionSpec(
                    name="parts_for_vehicle",
                    params={"vehicle_id": "V-CIVIC"},
                    include_receipt_summary=True,
                ),
            ),
            include_receipts=True,
        ),
    )

    assert report["queries"][0]["receipt"] == {
        "operation_type": "query",
        "parameters": {"vehicle_id": "V-CIVIC"},
        "query_name": "parts_for_vehicle",
    }
    assert report["receipts"] == []


def test_cross_section_query_capture_is_stable_across_repeated_builds(tmp_path: Path) -> None:
    instance = _new_instance(tmp_path)
    _seed_vehicle_and_part(instance)
    spec = StateCrossSectionSpec(
        queries=(
            QueryCrossSectionSpec(
                name="parts_for_vehicle",
                params={"vehicle_id": "V-CIVIC"},
                include_receipt_summary=True,
            ),
        ),
    )

    first = build_state_cross_section(instance, spec)
    second = build_state_cross_section(instance, spec)

    assert first == second
    assert diff_state(first, second) == {"summary": {"changed": False}, "version": 1}


def test_cross_section_receipt_section_uses_existing_receipts(tmp_path: Path) -> None:
    instance = _new_instance(tmp_path)
    _seed_vehicle_and_part(instance)
    persisted = service_query(instance, "parts_for_vehicle", {"vehicle_id": "V-CIVIC"})

    report = build_state_cross_section(
        instance,
        StateCrossSectionSpec(include_receipts=True),
    )

    assert persisted.receipt_id is not None
    receipt_entry = report["receipts"][0]
    assert receipt_entry["query_name"] == "parts_for_vehicle"
    assert receipt_entry["receipt_id"].startswith("<RECEIPT_")
    assert receipt_entry["created_at"] == "<TIMESTAMP>"


def test_state_diff_reports_selected_graph_changes(tmp_path: Path) -> None:
    instance = _new_instance(tmp_path)
    _seed_vehicle_and_part(instance)
    registry = CrossSectionTokenRegistry()
    spec = StateCrossSectionSpec(entity_types=("Part",), relationship_types=("fits",))
    before = build_state_cross_section(instance, spec, token_registry=registry)

    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1234",
                properties={
                    "part_number": "BP-1234",
                    "name": "Ceramic Brake Pad",
                    "category": "brakes",
                },
            )
        ],
    )
    service_add_relationships(
        instance,
        [
            RelationshipInstance(
                from_type="Part",
                from_id="BP-1234",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"verified": True, "source": "fixture"},
            )
        ],
        source="fixture",
        source_ref="seed",
    )

    after = build_state_cross_section(instance, spec, token_registry=registry)
    diff = diff_state(before, after)

    assert diff["summary"] == {
        "entities_added": 0,
        "entities_changed": 1,
        "entities_removed": 0,
        "relationships_added": 1,
        "relationships_changed": 0,
        "relationships_removed": 0,
    }
    assert diff["graph"]["entities"]["changed"][0]["changed_fields"] == ["properties.name"]
    added_relationship = diff["graph"]["relationships"]["added"][0]
    assert added_relationship["relationship_type"] == "fits"
    assert "edge_key" not in added_relationship


def test_cross_section_includes_edge_key_only_for_same_pair_multi_edges(
    tmp_path: Path,
) -> None:
    instance = _new_instance(tmp_path)
    _seed_vehicle_and_part(instance)
    graph = instance.load_graph()
    graph.add_relationship(
        RelationshipInstance(
            from_type="Part",
            from_id="BP-1234",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-CIVIC",
            properties={"verified": True, "source": "first"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            from_type="Part",
            from_id="BP-1234",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-CIVIC",
            properties={"verified": False, "source": "second"},
        )
    )
    instance.save_graph(graph)

    report = build_state_cross_section(
        instance,
        StateCrossSectionSpec(relationship_types=("fits",)),
    )

    relationships = report["graph"]["relationships"]
    assert len(relationships) == 2
    assert {relationship["edge_key"] for relationship in relationships} == {0, 1}


def test_state_diff_matches_golden(tmp_path: Path) -> None:
    instance = _new_instance(tmp_path)
    _seed_vehicle_and_part(instance)
    registry = CrossSectionTokenRegistry()
    spec = StateCrossSectionSpec(entity_types=("Part",), relationship_types=("fits",))
    before = build_state_cross_section(instance, spec, token_registry=registry)

    service_add_relationships(
        instance,
        [
            RelationshipInstance(
                from_type="Part",
                from_id="BP-1234",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"verified": True, "source": "fixture"},
            )
        ],
        source="fixture",
        source_ref="seed",
    )

    after = build_state_cross_section(instance, spec, token_registry=registry)
    assert_matches_golden(
        diff_state(before, after),
        Path("tests/goldens/state_cross_section/car_parts_state_diff.json"),
    )
