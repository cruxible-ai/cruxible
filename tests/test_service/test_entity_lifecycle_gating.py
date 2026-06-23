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


def _retire_entity_via_batch(
    instance: CruxibleInstance, entity_type: str, entity_id: str, status: str
) -> None:
    """Set the typed entity lifecycle on an entity through the batch write path.

    Builds the lifecycle via the typed constructor (validated against the entity
    status Literal) and stores its serialized form -- the production write path,
    not a hand-authored ``{"lifecycle": {...}}`` blob.
    """
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    properties={},
                    metadata=build_entity_lifecycle_metadata(status=status),  # type: ignore[arg-type]
                )
            ]
        ),
    )


def _retire_part_via_batch(instance: CruxibleInstance, part_id: str, status: str) -> None:
    """Set the typed entity lifecycle on a Part through the batch write path."""
    _retire_entity_via_batch(instance, "Part", part_id, status)


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


def _inline_traversal_part_ids(instance: CruxibleInstance, state: str | None) -> set[str]:
    """Run the `parts_for_vehicle` traversal allowing a runtime state override.

    The config's `parts_for_vehicle` query forbids runtime relationship-state
    overrides, so it can't be driven with an explicit `not-live`/`all` from the
    service surface. This inline definition mirrors it but opts into the override
    so the across-state entry-gating behavior can be asserted explicitly.
    """
    definition = {
        "name": "parts_for_vehicle_overridable",
        "mode": "traversal",
        "entry_point": "Vehicle",
        "traversal": [
            {
                "relationship": "fits",
                "direction": "incoming",
                "filter": {"verified": True},
            }
        ],
        "returns": "list[Part]",
        "allow_relationship_state_override": True,
    }
    from cruxible_core.service import service_query_inline_surface

    res = service_query_inline_surface(
        instance,
        definition,
        {"vehicle_id": "V-2024-CIVIC-EX"},
        relationship_state=state,
    )
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
# Traversal ENTRY gating: a retired entry entity gates the whole traversal
# (codex F-001). The entry of `parts_for_vehicle` is the Vehicle. Retiring it
# must drop every row under `live` -- returning EMPTY results, NOT an error, and
# NOT leaking the retired entry through live path rows -- while `not-live`/`all`
# keep it in scope and return rows. Consistent with the result chokepoint.
# ---------------------------------------------------------------------------


def test_retired_traversal_entry_yields_no_live_rows(
    populated_instance: CruxibleInstance,
) -> None:
    """Retiring the traversal ENTRY (Vehicle) hides the whole traversal under live.

    Before the fix, the entry entity was resolved without any lifecycle check, so
    `parts_for_vehicle(V-2024-CIVIC-EX)` still returned Parts even with the Vehicle
    retired -- leaking the retired entry. The result Parts here are all live, so
    the only thing gating the rows is the (previously ungated) entry.
    """
    _retire_entity_via_batch(populated_instance, "Vehicle", "V-2024-CIVIC-EX", "retired")

    # Default (None -> live) returns ZERO rows -- not an error.
    assert _traversal_part_ids(populated_instance, None) == set()
    # Explicit live agrees (via the overridable mirror; `parts_for_vehicle` itself
    # forbids a runtime state override, so an explicit selector must go through it).
    assert _inline_traversal_part_ids(populated_instance, "live") == set()


def test_retired_traversal_entry_does_not_block_under_all(
    populated_instance: CruxibleInstance,
) -> None:
    """A retired ENTRY does not block the traversal under a non-live read.

    The entry-anchor gate applies only under a live read. Under `all` the retired
    entry is in scope, so the traversal proceeds and the (live) result Parts still
    surface -- proving it was the entry, not the results, that `live` gated out.
    (Result Parts are kept live here on purpose: how a traversal surfaces a retired
    *result* under not-live/all is a separate expansion concern, orthogonal to the
    entry-anchor gate this fix adds.)
    """
    _retire_entity_via_batch(populated_instance, "Vehicle", "V-2024-CIVIC-EX", "retired")

    # `live`: the retired entry gates the whole traversal out.
    assert _inline_traversal_part_ids(populated_instance, "live") == set()
    # `all`: the retired entry is in scope, so the live result Parts still surface.
    assert _inline_traversal_part_ids(populated_instance, "all") == {"BP-1001", "BP-1002"}


def test_retired_entry_does_not_raise_entity_not_found(
    populated_instance: CruxibleInstance,
) -> None:
    """A gated-out (but existing) entry produces zero rows, never an error.

    `EntityNotFoundError` stays reserved for an entry that truly does not exist.
    An entry that EXISTS but is gated out by lifecycle must read like `list` does
    when everything is filtered: empty, no error, no existence leak.
    """
    _retire_entity_via_batch(populated_instance, "Vehicle", "V-2024-CIVIC-EX", "retired")

    # No exception; just empty.
    res = service_query_surface(
        populated_instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
        relationship_state=None,
    )
    assert res.items == []


def test_missing_entry_still_raises_entity_not_found(
    populated_instance: CruxibleInstance,
) -> None:
    """A truly absent entry still raises -- the gated-out path must not swallow it."""
    from cruxible_core.errors import EntityNotFoundError

    with pytest.raises(EntityNotFoundError):
        service_query_surface(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-DOES-NOT-EXIST"},
            relationship_state=None,
        )


@pytest.mark.parametrize("review_value", ["accepted", "pending", "reviewable"])
def test_review_only_states_gate_traversal_entry_like_live(
    populated_instance: CruxibleInstance,
    review_value: str,
) -> None:
    """Review-only selectors resolve to `live` for the entry, exactly like results."""
    _retire_entity_via_batch(populated_instance, "Vehicle", "V-2024-CIVIC-EX", "retired")
    # Entities have no review axis: review-only selectors gate the entry like live.
    assert _inline_traversal_part_ids(
        populated_instance, review_value
    ) == _inline_traversal_part_ids(populated_instance, "live")


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


# ---------------------------------------------------------------------------
# Relationship-shape gating parity (codex F-002): a retracted edge whose
# endpoints stay LIVE must surface/hide IDENTICALLY through `list edges` and a
# relationship-shaped collection query. The chokepoint must NOT gate the edge's
# (live) target endpoint as if it were the result entity -- the EDGE is the
# logical result and is gated by the relationship-state machine during
# collection, exactly like `list edges`.
# ---------------------------------------------------------------------------


def _retract_fits_endpoints_live(instance: CruxibleInstance) -> None:
    """Retract the `fits(BP-1001, V-2024-CIVIC-EX)` edge via the typed lifecycle
    write, leaving BOTH endpoints LIVE.

    Uses the same typed relationship-lifecycle channel exercised in
    ``test_relationship_lifecycle_write.py`` (no entity lifecycle is touched, so
    the Part and Vehicle endpoints remain live).
    """
    from cruxible_core.graph.assertion_state import RelationshipLifecycleState
    from cruxible_core.service.types import (
        BatchDirectWriteInput,
        BatchRelationshipWriteInput,
    )

    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1001",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-CIVIC-EX",
                    properties={"verified": True, "source": "catalog"},
                    lifecycle=RelationshipLifecycleState(  # type: ignore[arg-type]
                        status="retracted",
                        reason="superseded by newer fitment",
                    ),
                )
            ]
        ),
    )


def _list_edge_ids(instance: CruxibleInstance, state: str | None) -> set[tuple[str, str]]:
    result = service_list(
        instance,
        "edges",
        relationship_type="fits",
        relationship_state=state,
    )
    return {(item["from_id"], item["to_id"]) for item in result.items}


def _query_edge_ids(instance: CruxibleInstance, state: str | None) -> set[tuple[str, str]]:
    """Relationship-shaped collection query equivalent to `list edges`."""
    from cruxible_core.service import service_query_inline_surface

    res = service_query_inline_surface(
        instance,
        {
            "name": "all_fits",
            "mode": "collection",
            "returns": "fits",
            "result_shape": "relationship",
            "allow_relationship_state_override": True,
        },
        {},
        relationship_state=state,
    )
    return {(item.from_id, item.to_id) for item in res.items}


@pytest.mark.parametrize("state", ["not-live", "live", "all"])
def test_retracted_edge_with_live_endpoints_agrees_across_surfaces(
    populated_instance: CruxibleInstance,
    state: str,
) -> None:
    """`list edges` and a relationship-shaped collection query AGREE per state.

    Retract `fits(BP-1001, V-2024-CIVIC-EX)` while both endpoints (Part BP-1001,
    Vehicle V-2024-CIVIC-EX) stay LIVE. The edge is the logical result, gated by
    the relationship-state machine -- so it must:

      * surface under `not-live` (it is the gated-out edge),
      * hide under `live` (retracted edges fall out of the live edge view),
      * surface under `all`.

    Before the fix (codex F-002) the chokepoint gated the edge's LIVE target
    endpoint as the result entity, so the relationship-shaped query returned `[]`
    under `not-live`/`all` while `list edges` returned the edge -- they disagreed.
    """
    edge = ("BP-1001", "V-2024-CIVIC-EX")
    _retract_fits_endpoints_live(populated_instance)

    list_ids = _list_edge_ids(populated_instance, state)
    query_ids = _query_edge_ids(populated_instance, state)

    # The two surfaces must agree on the retracted edge for this state.
    assert (edge in list_ids) == (edge in query_ids)

    if state == "live":
        # Retracted edge hidden by BOTH.
        assert edge not in list_ids
        assert edge not in query_ids
    else:  # not-live / all
        # Retracted edge (live endpoints) surfaced by BOTH.
        assert edge in list_ids
        assert edge in query_ids
