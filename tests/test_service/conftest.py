"""Shared fixtures for service layer tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance

# Re-use the car parts config from CLI tests
from tests.test_cli.conftest import CAR_PARTS_YAML  # noqa: F401


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temporary project directory with a config file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CAR_PARTS_YAML)
    return tmp_path


@pytest.fixture
def initialized_instance(tmp_project: Path) -> CruxibleInstance:
    """Create an initialized .cruxible/ instance."""
    return CruxibleInstance.init(tmp_project, "config.yaml")


@pytest.fixture
def populated_graph() -> EntityGraph:
    """Build a graph with vehicles, parts, and fitments for testing."""
    g = EntityGraph()

    g.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-2024-CIVIC-EX",
            properties={
                "vehicle_id": "V-2024-CIVIC-EX",
                "year": 2024,
                "make": "Honda",
                "model": "Civic",
            },
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-2024-ACCORD-SPORT",
            properties={
                "vehicle_id": "V-2024-ACCORD-SPORT",
                "year": 2024,
                "make": "Honda",
                "model": "Accord",
            },
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1001",
            properties={
                "part_number": "BP-1001",
                "name": "Ceramic Brake Pads",
                "category": "brakes",
                "price": 49.99,
            },
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1002",
            properties={
                "part_number": "BP-1002",
                "name": "Performance Brake Pads",
                "category": "brakes",
                "price": 89.99,
            },
        )
    )

    g.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1001",
            to_type="Vehicle",
            to_id="V-2024-CIVIC-EX",
            properties={"verified": True, "source": "catalog"},
        )
    )
    g.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1001",
            to_type="Vehicle",
            to_id="V-2024-ACCORD-SPORT",
            properties={"verified": True, "source": "catalog"},
        )
    )
    g.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1002",
            to_type="Vehicle",
            to_id="V-2024-CIVIC-EX",
            properties={"verified": True, "source": "user_report"},
        )
    )
    g.add_relationship(
        RelationshipInstance(
            relationship_type="replaces",
            from_type="Part",
            from_id="BP-1002",
            to_type="Part",
            to_id="BP-1001",
            properties={"direction": "upgrade", "confidence": 0.95},
        )
    )

    return g


@pytest.fixture
def populated_instance(
    initialized_instance: CruxibleInstance,
    populated_graph: EntityGraph,
) -> CruxibleInstance:
    """Instance with a populated graph saved."""
    initialized_instance.save_graph(populated_graph)
    return initialized_instance
