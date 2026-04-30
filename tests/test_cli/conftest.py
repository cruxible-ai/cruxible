"""Shared fixtures for CLI tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.server.registry import reset_registry

CAR_PARTS_YAML = """\
version: "1.0"
name: car_parts_compatibility
description: Vehicle-to-part fitment

entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
      make:
        type: string
      model:
        type: string
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
      category:
        type: string
        enum: [brakes, suspension, engine, electrical, body, interior]
      price:
        type: float
        optional: true

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
      source:
        type: string
        optional: true
    reverse_name: fitted_parts
  - name: replaces
    from: Part
    to: Part
    properties:
      direction:
        type: string
        enum: [upgrade, downgrade, equivalent]
      confidence:
        type: float

named_queries:
  parts_for_vehicle:
    description: Find all parts that fit a specific vehicle
    entry_point: Vehicle
    traversal:
      - relationship: fits
        direction: incoming
        filter:
          verified: true
    returns: "list[Part]"
  vehicles_for_part:
    description: Find all vehicles a part fits
    entry_point: Part
    traversal:
      - relationship: fits
        direction: outgoing
    returns: "list[Vehicle]"

constraints: []

ingestion:
  vehicles:
    entity_type: Vehicle
    id_column: vehicle_id
  parts:
    entity_type: Part
    id_column: part_number
  fitments:
    relationship_type: fits
    from_column: part_number
    to_column: vehicle_id
"""


@pytest.fixture(autouse=True)
def reset_server_mode_env(monkeypatch):
    """Clear server-mode env and caches between CLI tests."""
    monkeypatch.delenv("CRUXIBLE_AGENT_MODE", raising=False)
    monkeypatch.delenv("CRUXIBLE_REQUIRE_SERVER", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_STATE_DIR", raising=False)
    monkeypatch.delenv("CRUXIBLE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("CRUXIBLE_DECISION_RECORD_ID", raising=False)
    reset_client_cache()
    reset_registry()
    yield
    reset_client_cache()
    reset_registry()


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temporary project directory with a config file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CAR_PARTS_YAML)
    return tmp_path


@pytest.fixture
def initialized_project(tmp_project: Path) -> CruxibleInstance:
    """Create an initialized .cruxible/ instance."""
    return CruxibleInstance.init(tmp_project, "config.yaml")


@pytest.fixture
def populated_graph() -> EntityGraph:
    """Build a graph with vehicles, parts, and fitments for testing."""
    g = EntityGraph()

    # Vehicles
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

    # Parts
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

    # Fitments (Part -> Vehicle)
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

    # Replacement
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
    initialized_project: CruxibleInstance,
    populated_graph: EntityGraph,
) -> CruxibleInstance:
    """Instance with a populated graph saved."""
    initialized_project.save_graph(populated_graph)
    return initialized_project


@pytest.fixture
def vehicles_csv(tmp_project: Path) -> Path:
    """Create a vehicles CSV file."""
    csv_path = tmp_project / "vehicles.csv"
    csv_path.write_text(
        "vehicle_id,year,make,model\n"
        "V-2024-CIVIC-EX,2024,Honda,Civic\n"
        "V-2024-ACCORD-SPORT,2024,Honda,Accord\n"
    )
    return csv_path


@pytest.fixture
def parts_csv(tmp_project: Path) -> Path:
    """Create a parts CSV file."""
    csv_path = tmp_project / "parts.csv"
    csv_path.write_text(
        "part_number,name,category,price\n"
        "BP-1001,Ceramic Brake Pads,brakes,49.99\n"
        "BP-1002,Performance Brake Pads,brakes,89.99\n"
    )
    return csv_path


@pytest.fixture
def fitments_csv(tmp_project: Path) -> Path:
    """Create a fitments CSV file."""
    csv_path = tmp_project / "fitments.csv"
    csv_path.write_text(
        "part_number,vehicle_id,verified,source\n"
        "BP-1001,V-2024-CIVIC-EX,true,catalog\n"
        "BP-1001,V-2024-ACCORD-SPORT,true,catalog\n"
        "BP-1002,V-2024-CIVIC-EX,true,user_report\n"
    )
    return csv_path
