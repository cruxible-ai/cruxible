"""`properties: auto` on make_entities / make_relationships / make_candidates.

Auto maps every declared property from ``$item.<name>`` — the 1:1 boilerplate
case that dominated kit build workflows before grammar v2.
"""

from __future__ import annotations

from cruxible_core.config.compact import expand_compact
from cruxible_core.config.schema import (
    CoreConfig,
    MakeCandidatesSpec,
    MakeEntitiesSpec,
    MakeRelationshipsSpec,
)
from cruxible_core.workflow.apply import make_entity_set, make_relationship_set
from cruxible_core.workflow.proposals import make_candidate_set

_CONFIG = CoreConfig.model_validate(
    expand_compact(
        """
name: auto_props_test
version: "1.0"
enums:
  kind: [big, small]
entity_types:
  Widget:
    id: widget_id
    properties:
      name: string
      kind: enum kind
      note: string?
  Bin:
    id: bin_id
    properties:
      name: string
relationships:
  - widget_in_bin: Widget -> Bin
    properties:
      slot: string
      count: int?
"""
    )
)


def test_make_entities_auto_maps_declared_properties() -> None:
    spec = MakeEntitiesSpec(
        entity_type="Widget",
        items="$input.rows",
        entity_id="$item.widget_id",
        properties="auto",
    )
    result = make_entity_set(
        _CONFIG,
        "step",
        spec,
        {"rows": [{"widget_id": "w1", "name": "Widget One", "kind": "big", "note": None}]},
        {},
    )
    entity = result.entities[0]
    assert entity.entity_id == "w1"
    assert entity.properties == {
        "widget_id": "w1",
        "name": "Widget One",
        "kind": "big",
        "note": None,
    }


def test_make_relationships_auto_maps_declared_edge_properties() -> None:
    spec = MakeRelationshipsSpec(
        relationship_type="widget_in_bin",
        items="$input.rows",
        from_type="Widget",
        from_id="$item.widget_id",
        to_type="Bin",
        to_id="$item.bin_id",
        properties="auto",
    )
    result = make_relationship_set(
        _CONFIG,
        "step",
        spec,
        {"rows": [{"widget_id": "w1", "bin_id": "b1", "slot": "A3", "count": 2}]},
        {},
    )
    rel = result.relationships[0]
    assert rel.properties == {"slot": "A3", "count": 2}


def test_make_candidates_auto_maps_declared_edge_properties() -> None:
    spec = MakeCandidatesSpec(
        relationship_type="widget_in_bin",
        items="$input.rows",
        from_type="Widget",
        from_id="$item.widget_id",
        to_type="Bin",
        to_id="$item.bin_id",
        properties="auto",
    )
    result = make_candidate_set(
        _CONFIG,
        "step",
        spec,
        {"rows": [{"widget_id": "w1", "bin_id": "b1", "slot": "A3", "count": None}]},
        {},
    )
    member = result.candidates[0]
    assert member.properties == {"slot": "A3", "count": None}


def test_explicit_properties_map_still_works() -> None:
    spec = MakeEntitiesSpec(
        entity_type="Widget",
        items="$input.rows",
        entity_id="$item.widget_id",
        properties={"widget_id": "$item.widget_id", "name": "$item.name", "kind": "$item.kind"},
    )
    result = make_entity_set(
        _CONFIG,
        "step",
        spec,
        {"rows": [{"widget_id": "w1", "name": "n", "kind": "small"}]},
        {},
    )
    assert result.entities[0].properties == {"widget_id": "w1", "name": "n", "kind": "small"}
