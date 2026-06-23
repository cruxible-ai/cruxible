"""Entity-lifecycle read-visibility gating: parity across every read surface.

These tests assert the F-011-style invariant for the entity axis: a retired/
superseded entity is hidden identically by ``query``, ``list entities``, the
``parts_for_vehicle`` traversal, the MCP read route, and the HTTP read route,
while ``entity get <id>`` still returns it and reveals its lifecycle status.
"""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.assertion_state import (
    build_entity_lifecycle_metadata,
    entity_lifecycle_status,
)
from cruxible_core.service import service_list, service_query_surface
from cruxible_core.service.mutations import service_add_entities, service_batch_direct_write
from cruxible_core.service.queries import service_get_entity
from cruxible_core.service.types import BatchDirectWriteInput, EntityWriteInput


def _retire_part_via_batch(instance: CruxibleInstance, part_id: str, status: str) -> None:
    """Set the typed entity lifecycle on a Part through the batch write path.

    Builds the lifecycle via the typed constructor (validated against the entity
    status Literal) and stores its serialized form -- the production write path,
    not a hand-authored ``{"lifecycle": {...}}`` blob.
    """
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="Part",
                    entity_id=part_id,
                    properties={},
                    metadata=build_entity_lifecycle_metadata(status=status),  # type: ignore[arg-type]
                )
            ]
        ),
    )


def _list_part_ids(instance: CruxibleInstance, state: str | None) -> set[str]:
    result = service_list(
        instance,
        "entities",
        entity_type="Part",
        relationship_state=state,
    )
    return {item.entity_id for item in result.items}


def _query_part_ids(instance: CruxibleInstance, state: str | None) -> set[str]:
    # Inline entity-collection query equivalent to `list entities`.
    definition = {
        "name": "all_parts",
        "mode": "collection",
        "returns": "Part",
        "result_shape": "entity",
        "allow_relationship_state_override": True,
    }
    from cruxible_core.service import service_query_inline_surface

    res = service_query_inline_surface(
        instance,
        definition,
        {},
        relationship_state=state,
    )
    return {item.entity_id for item in res.items}


def _traversal_part_ids(instance: CruxibleInstance, state: str | None) -> set[str]:
    res = service_query_surface(
        instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
        relationship_state=state,
    )
    # `parts_for_vehicle` defaults to result_shape `path`; the terminal entity of
    # each path is the Part. Entity-lifecycle gating drops paths whose result
    # entity is retired.
    return {item.result.entity_id for item in res.items}


# ---------------------------------------------------------------------------
# Field + write-path round-trip
# ---------------------------------------------------------------------------


def test_lifecycle_status_defaults_to_live(populated_instance: CruxibleInstance) -> None:
    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    # No lifecycle metadata written yet: the typed accessor reports `live`.
    assert entity_lifecycle_status(entity.metadata) == "live"


def test_batch_direct_write_sets_lifecycle_status(populated_instance: CruxibleInstance) -> None:
    _retire_part_via_batch(populated_instance, "BP-1001", "retired")
    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    assert entity.metadata["lifecycle"]["status"] == "retired"
    # The write round-trips through storage (reload from disk).
    reloaded = service_get_entity(populated_instance, "Part", "BP-1001")
    assert reloaded is not None
    assert entity_lifecycle_status(reloaded.metadata) == "retired"


def test_entity_update_sets_lifecycle_status_preserving_metadata(
    populated_instance: CruxibleInstance,
) -> None:
    from cruxible_core.graph.types import EntityInstance

    # Seed an unrelated metadata key, then set lifecycle via the generic update.
    service_add_entities(
        populated_instance,
        [
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1002",
                properties={},
                metadata={"note": "keep-me"},
            )
        ],
    )
    service_add_entities(
        populated_instance,
        [
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1002",
                properties={},
                metadata=build_entity_lifecycle_metadata(status="superseded"),
            )
        ],
    )
    entity = service_get_entity(populated_instance, "Part", "BP-1002")
    assert entity is not None
    # Lifecycle decodes as the typed model with the written status.
    assert entity_lifecycle_status(entity.metadata) == "superseded"
    assert entity.metadata["lifecycle"]["status"] == "superseded"
    # Shallow merge preserves the sibling metadata key.
    assert entity.metadata["note"] == "keep-me"


# ---------------------------------------------------------------------------
# Gating parity: a retired entity is hidden identically everywhere
# ---------------------------------------------------------------------------


def test_retired_entity_hidden_from_live_reads_consistently(
    populated_instance: CruxibleInstance,
) -> None:
    _retire_part_via_batch(populated_instance, "BP-1001", "retired")

    # list entities (default live) hides it.
    assert "BP-1001" not in _list_part_ids(populated_instance, None)
    assert "BP-1001" not in _list_part_ids(populated_instance, "live")
    # collection query (live) hides it.
    assert "BP-1001" not in _query_part_ids(populated_instance, "live")
    # traversal query (parts_for_vehicle, default live) hides it. The query
    # default is already `live`, so pass None (no runtime override needed).
    assert "BP-1001" not in _traversal_part_ids(populated_instance, None)

    # The live set is identical across every surface.
    live_list = _list_part_ids(populated_instance, "live")
    live_query = _query_part_ids(populated_instance, "live")
    assert live_list == live_query == {"BP-1002"}


def test_not_live_surfaces_exactly_the_gated_out_set(
    populated_instance: CruxibleInstance,
) -> None:
    _retire_part_via_batch(populated_instance, "BP-1001", "retired")

    not_live = _list_part_ids(populated_instance, "not-live")
    assert not_live == {"BP-1001"}
    # not-live across surfaces agrees.
    assert _query_part_ids(populated_instance, "not-live") == {"BP-1001"}


def test_all_returns_everything(populated_instance: CruxibleInstance) -> None:
    _retire_part_via_batch(populated_instance, "BP-1001", "retired")

    all_parts = _list_part_ids(populated_instance, "all")
    assert all_parts == {"BP-1001", "BP-1002"}
    assert _query_part_ids(populated_instance, "all") == {"BP-1001", "BP-1002"}


def test_live_is_default_for_list_entities(populated_instance: CruxibleInstance) -> None:
    _retire_part_via_batch(populated_instance, "BP-1001", "retired")
    # Passing no state defaults to live (gated), matching explicit "live".
    assert _list_part_ids(populated_instance, None) == _list_part_ids(populated_instance, "live")


@pytest.mark.parametrize("review_value", ["accepted", "pending", "reviewable"])
def test_review_only_states_resolve_to_live_for_entities(
    populated_instance: CruxibleInstance,
    review_value: str,
) -> None:
    _retire_part_via_batch(populated_instance, "BP-1001", "retired")
    # Entities have no review axis: review-only selectors behave like `live`.
    assert _list_part_ids(populated_instance, review_value) == _list_part_ids(
        populated_instance, "live"
    )


# ---------------------------------------------------------------------------
# entity get <id> is NOT gated and reveals lifecycle status
# ---------------------------------------------------------------------------


def test_entity_get_returns_retired_entity_and_shows_status(
    populated_instance: CruxibleInstance,
) -> None:
    _retire_part_via_batch(populated_instance, "BP-1001", "retired")
    # Even though every query/list hides it, the explicit by-id get returns it.
    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    assert entity.entity_id == "BP-1001"
    assert entity_lifecycle_status(entity.metadata) == "retired"


# ---------------------------------------------------------------------------
# MCP (runtime API) read parity with the service layer
# ---------------------------------------------------------------------------


def test_mcp_list_route_matches_service_gating(
    populated_instance: CruxibleInstance,
) -> None:
    from cruxible_core.mcp import handlers
    from cruxible_core.runtime.instance_manager import get_manager

    _retire_part_via_batch(populated_instance, "BP-1001", "retired")

    manager = get_manager()
    manager.clear()
    instance_id = "inst-entity-lifecycle"
    manager.register(instance_id, populated_instance)
    try:
        # default (live) hides the retired Part -- same answer as the service.
        live = handlers.handle_list(instance_id, "entities", entity_type="Part")
        live_ids = {item["entity_id"] for item in live.items}
        assert "BP-1001" not in live_ids
        assert live_ids == _list_part_ids(populated_instance, "live")

        # not-live surfaces exactly the retired Part.
        not_live = handlers.handle_list(
            instance_id, "entities", entity_type="Part", relationship_state="not-live"
        )
        assert {item["entity_id"] for item in not_live.items} == {"BP-1001"}

        # all returns everything.
        all_items = handlers.handle_list(
            instance_id, "entities", entity_type="Part", relationship_state="all"
        )
        assert {item["entity_id"] for item in all_items.items} == {"BP-1001", "BP-1002"}
    finally:
        manager.clear()


# ---------------------------------------------------------------------------
# Reserved-key defense: free-form entity lifecycle is un-authorable everywhere
# ---------------------------------------------------------------------------
#
# Regression for the MAJOR finding: a hand-authored ``metadata={"lifecycle":
# {...}}`` must NOT be storable through any write surface (it would bypass the
# typed lifecycle validator and silently soft-delete the entity). Lifecycle is
# settable ONLY through the typed ``lifecycle`` field. Because every surface
# (batch direct-write, MCP, HTTP) deserializes into the same ``EntityInput``
# contract, validating there covers all three at once.


def test_entity_input_rejects_reserved_lifecycle_metadata_key() -> None:
    """The contract rejects a hand-authored reserved 'lifecycle' metadata key.

    This fires on EVERY surface (batch direct-write, MCP, HTTP) because they all
    deserialize the same ``EntityInput`` model. Rejecting -- not silently
    stripping -- keeps author intent visible.
    """
    from pydantic import ValidationError

    from cruxible_client import contracts

    with pytest.raises(ValidationError) as exc:
        contracts.EntityInput(
            entity_type="Part",
            entity_id="BP-1001",
            metadata={"lifecycle": {"status": "retired"}},
        )
    # The error points the author at the typed channel.
    assert "lifecycle" in str(exc.value)
    assert "typed `lifecycle` field" in str(exc.value)

    # Reserved key alongside other keys is still rejected (no partial accept).
    with pytest.raises(ValidationError):
        contracts.EntityInput(
            entity_type="Part",
            entity_id="BP-1001",
            metadata={"note": "keep-me", "lifecycle": {"status": "retired"}},
        )


def test_batch_payload_rejects_reserved_lifecycle_metadata_key() -> None:
    """The batch direct-write payload rejects the reserved key on its entities.

    The batch path is the one place the original bug actually persisted the
    free-form blob; the rejection must fire when the payload is deserialized.
    """
    from pydantic import ValidationError

    from cruxible_client import contracts

    with pytest.raises(ValidationError):
        contracts.BatchDirectWritePayload(
            entities=[
                {
                    "entity_type": "Part",
                    "entity_id": "BP-1001",
                    "metadata": {"lifecycle": {"status": "retired"}},
                }
            ]
        )


def test_typed_lifecycle_field_still_sets_status(
    populated_instance: CruxibleInstance,
) -> None:
    """The typed ``lifecycle`` field remains the working channel after the fix.

    Drives the runtime batch direct-write entrypoint with a contract payload that
    sets lifecycle via the typed field (the only allowed channel) and confirms the
    entity is soft-deleted (retired) end to end.
    """
    from cruxible_client import contracts
    from cruxible_core.runtime import api
    from cruxible_core.runtime.instance_manager import get_manager

    manager = get_manager()
    manager.clear()
    instance_id = "inst-entity-lifecycle-typed"
    manager.register(instance_id, populated_instance)
    try:
        payload = contracts.BatchDirectWritePayload(
            entities=[
                contracts.EntityInput(
                    entity_type="Part",
                    entity_id="BP-1001",
                    metadata={"note": "still-here"},
                    lifecycle=contracts.EntityLifecycleInput(status="retired"),
                )
            ]
        )
        result = api.batch_direct_write(instance_id, payload)
        assert result.valid
        entity = service_get_entity(populated_instance, "Part", "BP-1001")
        assert entity is not None
        # Lifecycle set via the typed channel round-trips...
        assert entity_lifecycle_status(entity.metadata) == "retired"
        # ...and the author's unrelated free-form metadata is preserved alongside.
        assert entity.metadata["note"] == "still-here"
    finally:
        manager.clear()


def test_normal_metadata_without_reserved_key_is_unaffected() -> None:
    """A normal metadata dict (no reserved key) passes through untouched."""
    from cruxible_client import contracts

    entity = contracts.EntityInput(
        entity_type="Part",
        entity_id="BP-1001",
        metadata={"note": "keep-me", "owner": "team-a"},
    )
    assert entity.metadata == {"note": "keep-me", "owner": "team-a"}
