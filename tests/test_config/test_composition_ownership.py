"""Tests for derived composition ownership views."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.config.composer import write_runtime_composed_config
from cruxible_core.config.composition_ownership import resolve_composition_for_instance
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.snapshot.types import UpstreamMetadata

BASE_CONFIG = """\
version: "1.0"
name: reference_world
kind: world_model

contracts:
  empty_input:
    fields: {}
  empty_output:
    fields: {}

entity_types:
  ReferenceThing:
    properties:
      thing_id:
        type: string
        primary_key: true

relationships:
  - name: reference_links_reference
    from: ReferenceThing
    to: ReferenceThing

named_queries:
  reference_lookup:
    entry_point: ReferenceThing
    traversal:
      - relationship: reference_links_reference
        direction: outgoing
    returns: list[ReferenceThing]

providers:
  reference_provider:
    kind: function
    contract_in: empty_input
    contract_out: empty_output
    ref: tests.providers:reference_provider
    version: "1.0.0"

workflows:
  reference_workflow:
    contract_in: empty_input
    returns: reference
    steps:
      - id: call_reference
        provider: reference_provider
        input: {}
        as: reference
"""

OVERLAY_TEMPLATE = """\
version: "1.0"
name: local_world
kind: world_model
extends: __BASE_PATH__

entity_types:
  LocalThing:
    properties:
      local_id:
        type: string
        primary_key: true

relationships:
  - name: local_links_reference
    from: LocalThing
    to: ReferenceThing
    proposal_policy: {}

named_queries:
  local_lookup:
    entry_point: LocalThing
    traversal:
      - relationship: local_links_reference
        direction: outgoing
    returns: list[ReferenceThing]

providers:
  local_provider:
    kind: function
    contract_in: empty_input
    contract_out: empty_output
    ref: tests.providers:local_provider
    version: "1.0.0"

workflows:
  local_workflow:
    contract_in: empty_input
    returns: local
    steps:
      - id: use_reference
        query: reference_lookup
        params: {}
        as: reference
      - id: call_local
        provider: local_provider
        input: {}
        as: local
"""

UNLAYERED_CONFIG = """\
version: "1.0"
name: standalone_world
kind: world_model
entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true
relationships: []
"""


def _write_layered_project(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    base = root / "base.yaml"
    overlay = root / "overlay.yaml"
    base.write_text(BASE_CONFIG)
    overlay.write_text(OVERLAY_TEMPLATE.replace("__BASE_PATH__", str(base)))
    return base, overlay


def test_extends_diff_infers_type_and_surface_ownership(tmp_path: Path) -> None:
    _base, overlay = _write_layered_project(tmp_path / "world")
    instance = CruxibleInstance.init(overlay.parent, overlay.name)

    resolution = resolve_composition_for_instance(instance)

    assert resolution.ownership.source == "extends"
    assert set(resolution.config.entity_types) == {"ReferenceThing", "LocalThing"}
    assert resolution.ownership.upstream_entity_types == {"ReferenceThing"}
    assert resolution.ownership.local_entity_types == {"LocalThing"}
    assert resolution.ownership.upstream_relationship_types == {"reference_links_reference"}
    assert resolution.ownership.local_relationship_types == {"local_links_reference"}
    assert resolution.ownership.upstream_named_queries == {"reference_lookup"}
    assert resolution.ownership.local_named_queries == {"local_lookup"}
    assert resolution.ownership.upstream_workflows == {"reference_workflow"}
    assert resolution.ownership.local_workflows == {"local_workflow"}
    assert resolution.ownership.upstream_providers == {"reference_provider"}
    assert resolution.ownership.local_providers == {"local_provider"}


def test_upstream_metadata_takes_precedence_over_extends(tmp_path: Path) -> None:
    base, overlay = _write_layered_project(tmp_path / "world")
    instance = CruxibleInstance.init(overlay.parent, overlay.name)
    active = overlay.parent / "active.yaml"
    write_runtime_composed_config(
        base_path=base,
        overlay_path=overlay,
        output_path=active,
    )
    overlay.write_text(
        OVERLAY_TEMPLATE.replace("__BASE_PATH__", str(overlay.parent / "missing.yaml"))
    )
    instance.set_upstream_metadata(
        UpstreamMetadata(
            transport_ref="file:///tmp/reference-world",
            world_id="reference-world",
            release_id="v1",
            snapshot_id="snap-1",
            compatibility="additive_schema",
            owned_entity_types=["ReferenceThing"],
            owned_relationship_types=["reference_links_reference"],
            config_path=base.name,
            overlay_config_path=overlay.name,
            active_config_path=active.name,
        )
    )

    resolution = resolve_composition_for_instance(instance)

    assert resolution.ownership.source == "upstream_metadata"
    assert set(resolution.config.entity_types) == {"ReferenceThing", "LocalThing"}
    assert resolution.ownership.upstream_entity_types == {"ReferenceThing"}
    assert resolution.ownership.local_entity_types == {"LocalThing"}
    assert resolution.ownership.surface_ownership_available is True
    assert resolution.ownership.upstream_named_queries == {"reference_lookup"}
    assert resolution.ownership.local_workflows == {"local_workflow"}


def test_unlayered_config_reports_unavailable_ownership(tmp_path: Path) -> None:
    root = tmp_path / "world"
    root.mkdir()
    (root / "config.yaml").write_text(UNLAYERED_CONFIG)
    instance = CruxibleInstance.init(root, "config.yaml")

    resolution = resolve_composition_for_instance(instance)

    assert resolution.ownership.source == "unavailable"
    assert resolution.ownership.ownership_available is False
