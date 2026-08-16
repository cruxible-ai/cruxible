"""Entity-lifecycle read-visibility gating across retained donor surfaces.

These tests assert the F-011-style invariant for the entity axis: a retired/
superseded entity is hidden identically by ``query``, ``list entities``, the
``parts_for_vehicle`` traversal, and direct service reads, while the explicit
by-id getter still returns it and reveals its lifecycle status.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError, TerminalLifecycleWriteRefusedError
from cruxible_core.graph.assertion_state import EntityLifecycleState
from cruxible_core.graph.types import EntityInstance, EntityMetadata
from cruxible_core.query.enums import QueryVisibilityState
from cruxible_core.query.types import QueryPathRow, QueryRelationshipRow
from cruxible_core.service import service_list, service_query_surface
from cruxible_core.service.mutations import (
    service_add_entities,
    service_add_entity_inputs,
    service_batch_direct_write,
)
from cruxible_core.service.queries import service_get_entity
from cruxible_core.service.types import BatchDirectWriteInput, EntityWriteInput
from tests.support.terminal_lifecycle import (
    seed_entity_lifecycle,
    seed_relationship_lifecycle,
)


def _lifecycle_metadata(status: str) -> dict[str, Any]:
    """Build a stored entity-metadata dict carrying a typed lifecycle status.

    The production write path runs through the typed ``EntityMetadata`` envelope
    (validating ``status`` against the entity lifecycle Literal) and re-encodes it
    to the flat storable dict -- never a hand-authored ``{"lifecycle": {...}}`` blob.
    """
    return EntityMetadata(
        lifecycle=EntityLifecycleState(status=status)  # type: ignore[arg-type]
    ).to_metadata_dict()


def _entity_lifecycle_status(metadata: Any) -> str:
    """Decode the typed lifecycle status from a stored entity-metadata dict."""
    return EntityMetadata.from_metadata(metadata).lifecycle_status()


def _retire_entity(
    instance: CruxibleInstance, entity_type: str, entity_id: str, status: str
) -> None:
    """Seed a TERMINAL entity lifecycle through the trusted chokepoint capability.

    Terminal statuses (``retired``/``superseded``) are refused on every free-write
    path, including the exported service functions and the local CLI -- that
    refusal is asserted in the dedicated tests below. These gating tests are about
    READS, so they seed through ``trusted_lifecycle_transition=True``, the internal
    capability the dedicated receipted verbs will carry.
    """
    seed_entity_lifecycle(instance, entity_type, entity_id, status)


def _retire_part(instance: CruxibleInstance, part_id: str, status: str) -> None:
    """Seed the typed entity lifecycle on a Part."""
    _retire_entity(instance, "Part", part_id, status)


def _list_part_ids(
    instance: CruxibleInstance,
    state: QueryVisibilityState | None,
) -> set[str]:
    result = service_list(
        instance,
        "entities",
        entity_type="Part",
        relationship_state=state,
    )
    return {item.entity_id for item in result.items}


def _query_part_ids(
    instance: CruxibleInstance,
    state: QueryVisibilityState | None,
) -> set[str]:
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
    return {cast(EntityInstance, item).entity_id for item in res.items}


def _traversal_part_ids(
    instance: CruxibleInstance,
    state: QueryVisibilityState | None,
) -> set[str]:
    res = service_query_surface(
        instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
        relationship_state=state,
    )
    # `parts_for_vehicle` defaults to result_shape `path`; the terminal entity of
    # each path is the Part. Entity-lifecycle gating drops paths whose result
    # entity is retired.
    return {cast(QueryPathRow, item).result.entity_id for item in res.items}


def _inline_traversal_part_ids(
    instance: CruxibleInstance,
    state: QueryVisibilityState | None,
) -> set[str]:
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
    return {cast(QueryPathRow, item).result.entity_id for item in res.items}


# ---------------------------------------------------------------------------
# Field + write-path round-trip
# ---------------------------------------------------------------------------


def test_lifecycle_status_defaults_to_live(populated_instance: CruxibleInstance) -> None:
    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    # No lifecycle metadata written yet: the typed accessor reports `live`.
    assert _entity_lifecycle_status(entity.metadata) == "live"


def test_trusted_lifecycle_transition_sets_and_round_trips_terminal_status(
    populated_instance: CruxibleInstance,
) -> None:
    """The trusted capability CAN write a terminal status, and it round-trips.

    This is the other half of the refusal: the chokepoint refuses the free-write
    path but stays open only to the dedicated receipted lifecycle verbs.
    """
    _retire_part(populated_instance, "BP-1001", "retired")
    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    assert entity.metadata.lifecycle is not None
    assert entity.metadata.lifecycle.status == "retired"
    # The write round-trips through storage (reload from disk).
    reloaded = service_get_entity(populated_instance, "Part", "BP-1001")
    assert reloaded is not None
    assert _entity_lifecycle_status(reloaded.metadata) == "retired"


def test_batch_direct_write_service_refuses_terminal_lifecycle(
    populated_instance: CruxibleInstance,
) -> None:
    """``service_batch_direct_write`` refuses terminal lifecycle in the BATCH shape.

    The exported service function is the free-write channel the local CLI calls
    directly — it never passes through a contract mapper, so this refusal has to
    come from the graph chokepoint. Nothing is persisted.
    """
    with pytest.raises(
        TerminalLifecycleWriteRefusedError,
        match="cruxible entity retire",
    ):
        service_batch_direct_write(
            populated_instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Part",
                        entity_id="BP-1001",
                        properties={},
                        metadata=_lifecycle_metadata("retired"),
                    )
                ]
            ),
        )
    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    assert _entity_lifecycle_status(entity.metadata) == "live"


def test_batch_direct_write_dry_run_refuses_terminal_lifecycle(
    populated_instance: CruxibleInstance,
) -> None:
    """A preview refuses identically — a dry run must not report a write as valid."""
    with pytest.raises(TerminalLifecycleWriteRefusedError):
        service_batch_direct_write(
            populated_instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Part",
                        entity_id="BP-1001",
                        properties={},
                        metadata=_lifecycle_metadata("superseded"),
                    )
                ]
            ),
            dry_run=True,
        )


@pytest.mark.parametrize("status", ["retired", "superseded"])
def test_service_add_entity_inputs_refuses_terminal_lifecycle(
    populated_instance: CruxibleInstance,
    status: str,
) -> None:
    """``service_add_entity_inputs`` refuses terminal lifecycle too."""
    with pytest.raises(TerminalLifecycleWriteRefusedError):
        service_add_entity_inputs(
            populated_instance,
            [
                EntityWriteInput(
                    entity_type="Part",
                    entity_id="BP-1001",
                    properties={},
                    metadata=_lifecycle_metadata(status),
                )
            ],
        )
    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    assert _entity_lifecycle_status(entity.metadata) == "live"


def test_entity_update_refuses_terminal_lifecycle_and_preserves_metadata(
    populated_instance: CruxibleInstance,
) -> None:
    """A generic entity UPDATE cannot terminate the entity, and changes nothing.

    Previously this exercised the bypass: ``service_add_entities`` accepted and
    persisted a hand-built ``EntityMetadata`` carrying ``superseded``. It is now
    refused at the chokepoint, and the entity's earlier metadata is untouched.
    """
    # Seed an unrelated metadata key through the still-permitted write path.
    service_add_entities(
        populated_instance,
        [
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1002",
                properties={},
                metadata=EntityMetadata.from_metadata({"note": "keep-me"}),
            )
        ],
    )
    with pytest.raises(TerminalLifecycleWriteRefusedError):
        service_add_entities(
            populated_instance,
            [
                EntityInstance(
                    entity_type="Part",
                    entity_id="BP-1002",
                    properties={},
                    metadata=EntityMetadata.from_metadata(_lifecycle_metadata("superseded")),
                )
            ],
        )
    entity = service_get_entity(populated_instance, "Part", "BP-1002")
    assert entity is not None
    # Still live, and the free-form sibling key from the earlier write survives in
    # the typed envelope's `extra` slot.
    assert _entity_lifecycle_status(entity.metadata) == "live"
    assert entity.metadata.extra["note"] == "keep-me"


def test_retired_entity_cannot_be_reactivated_through_the_service(
    populated_instance: CruxibleInstance,
) -> None:
    """A retired identity is preserved for the future receipted reinstate verb."""
    _retire_part(populated_instance, "BP-1002", "retired")
    with pytest.raises(
        DataValidationError,
        match=r"Part:BP-1002 is retired.*deferred reinstate adjudication",
    ):
        service_batch_direct_write(
            populated_instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Part",
                        entity_id="BP-1002",
                        properties={},
                        metadata=_lifecycle_metadata("live"),
                    )
                ]
            ),
        )
    entity = service_get_entity(populated_instance, "Part", "BP-1002")
    assert entity is not None
    assert _entity_lifecycle_status(entity.metadata) == "retired"


# ---------------------------------------------------------------------------
# Gating parity: a retired entity is hidden identically everywhere
# ---------------------------------------------------------------------------


def test_retired_entity_hidden_from_live_reads_consistently(
    populated_instance: CruxibleInstance,
) -> None:
    _retire_part(populated_instance, "BP-1001", "retired")

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
    _retire_part(populated_instance, "BP-1001", "retired")

    not_live = _list_part_ids(populated_instance, "not-live")
    assert not_live == {"BP-1001"}
    # not-live across surfaces agrees.
    assert _query_part_ids(populated_instance, "not-live") == {"BP-1001"}


def test_all_returns_everything(populated_instance: CruxibleInstance) -> None:
    _retire_part(populated_instance, "BP-1001", "retired")

    all_parts = _list_part_ids(populated_instance, "all")
    assert all_parts == {"BP-1001", "BP-1002"}
    assert _query_part_ids(populated_instance, "all") == {"BP-1001", "BP-1002"}


def test_live_is_default_for_list_entities(populated_instance: CruxibleInstance) -> None:
    _retire_part(populated_instance, "BP-1001", "retired")
    # Passing no state defaults to live (gated), matching explicit "live".
    assert _list_part_ids(populated_instance, None) == _list_part_ids(populated_instance, "live")


@pytest.mark.parametrize("review_value", ["accepted", "pending", "reviewable"])
def test_review_only_states_resolve_to_live_for_entities(
    populated_instance: CruxibleInstance,
    review_value: QueryVisibilityState,
) -> None:
    _retire_part(populated_instance, "BP-1001", "retired")
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
    _retire_entity(populated_instance, "Vehicle", "V-2024-CIVIC-EX", "retired")

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
    _retire_entity(populated_instance, "Vehicle", "V-2024-CIVIC-EX", "retired")

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
    _retire_entity(populated_instance, "Vehicle", "V-2024-CIVIC-EX", "retired")

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
    review_value: QueryVisibilityState,
) -> None:
    """Review-only selectors resolve to `live` for the entry, exactly like results."""
    _retire_entity(populated_instance, "Vehicle", "V-2024-CIVIC-EX", "retired")
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
    _retire_part(populated_instance, "BP-1001", "retired")
    # Even though every query/list hides it, the explicit by-id get returns it.
    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    assert entity.entity_id == "BP-1001"
    assert _entity_lifecycle_status(entity.metadata) == "retired"


# ---------------------------------------------------------------------------
# Retained service-level lifecycle and authorization parity
# ---------------------------------------------------------------------------


def test_list_service_matches_lifecycle_gating(
    populated_instance: CruxibleInstance,
) -> None:
    _retire_part(populated_instance, "BP-1001", "retired")

    live = service_list(populated_instance, "entities", entity_type="Part")
    live_ids = {item.entity_id for item in live.items}
    assert "BP-1001" not in live_ids
    assert live_ids == _list_part_ids(populated_instance, "live")

    not_live = service_list(
        populated_instance,
        "entities",
        entity_type="Part",
        relationship_state="not-live",
    )
    assert {item.entity_id for item in not_live.items} == {"BP-1001"}

    all_items = service_list(
        populated_instance,
        "entities",
        entity_type="Part",
        relationship_state="all",
    )
    assert {item.entity_id for item in all_items.items} == {"BP-1001", "BP-1002"}


def test_free_form_metadata_lifecycle_key_does_not_set_typed_lifecycle(
    populated_instance: CruxibleInstance,
) -> None:
    from cruxible_core.service.lifecycle_inputs import entity_metadata_with_lifecycle

    metadata = entity_metadata_with_lifecycle(
        {"lifecycle": {"status": "retired"}},
        None,
    )
    service_add_entity_inputs(
        populated_instance,
        [EntityWriteInput(entity_type="Part", entity_id="BP-1001", metadata=metadata)],
    )

    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    assert _entity_lifecycle_status(entity.metadata) == "live"
    assert entity.metadata.lifecycle is None
    assert entity.metadata.extra["lifecycle"] == {"status": "retired"}


def test_free_form_lifecycle_key_is_nested_under_extra() -> None:
    from cruxible_core.service.lifecycle_inputs import entity_metadata_with_lifecycle

    metadata = entity_metadata_with_lifecycle(
        {"note": "keep-me", "lifecycle": {"status": "retired"}},
        None,
    )
    envelope = EntityMetadata.from_metadata(metadata)
    assert envelope.lifecycle is None
    assert envelope.extra == {
        "note": "keep-me",
        "lifecycle": {"status": "retired"},
    }


def test_typed_lifecycle_mapper_still_sets_non_terminal_status(
    populated_instance: CruxibleInstance,
) -> None:
    from types import SimpleNamespace

    from cruxible_core.service.lifecycle_inputs import entity_metadata_with_lifecycle

    metadata = entity_metadata_with_lifecycle(
        {"note": "still-here"},
        SimpleNamespace(status="live", reason="reinstated"),
    )
    service_add_entity_inputs(
        populated_instance,
        [EntityWriteInput(entity_type="Part", entity_id="BP-1001", metadata=metadata)],
    )

    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    assert _entity_lifecycle_status(entity.metadata) == "live"
    assert entity.metadata.lifecycle is not None
    assert entity.metadata.lifecycle.reason == "reinstated"
    assert entity.metadata.extra["note"] == "still-here"


@pytest.mark.parametrize("status", ["retired", "superseded"])
def test_terminal_entity_lifecycle_is_refused_by_the_mapper(
    populated_instance: CruxibleInstance,
    status: str,
) -> None:
    from types import SimpleNamespace

    from cruxible_core.service.lifecycle_inputs import entity_metadata_with_lifecycle

    with pytest.raises(
        TerminalLifecycleWriteRefusedError,
        match="cruxible entity retire",
    ):
        entity_metadata_with_lifecycle(
            {},
            SimpleNamespace(status=status, reason=None),
        )

    entity = service_get_entity(populated_instance, "Part", "BP-1001")
    assert entity is not None
    assert _entity_lifecycle_status(entity.metadata) == "live"


# ---------------------------------------------------------------------------
# Relationship-shape gating parity (codex F-002): a retracted edge whose
# endpoints stay LIVE must surface/hide IDENTICALLY through `list edges` and a
# relationship-shaped collection query. The chokepoint must NOT gate the edge's
# (live) target endpoint as if it were the result entity -- the EDGE is the
# logical result and is gated by the relationship-state machine during
# collection, exactly like `list edges`.
# ---------------------------------------------------------------------------


def _retract_fits_endpoints_live(instance: CruxibleInstance) -> None:
    """Retract the `fits(BP-1001, V-2024-CIVIC-EX)` edge, leaving BOTH endpoints LIVE.

    ``retracted`` is terminal, so it is seeded through the trusted chokepoint
    capability (see :mod:`tests.support.terminal_lifecycle`) rather than the free
    write path, which refuses it. No entity lifecycle is touched, so the Part and
    Vehicle endpoints remain live -- which is the whole point of this fixture.
    """
    seed_relationship_lifecycle(
        instance,
        from_type="Part",
        from_id="BP-1001",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-2024-CIVIC-EX",
        status="retracted",
        reason="superseded by newer fitment",
        properties={"verified": True, "source": "catalog"},
    )


def _list_edge_ids(
    instance: CruxibleInstance,
    state: QueryVisibilityState | None,
) -> set[tuple[str, str]]:
    result = service_list(
        instance,
        "edges",
        relationship_type="fits",
        relationship_state=state,
    )
    return {(item["from_id"], item["to_id"]) for item in result.items}


def _query_edge_ids(
    instance: CruxibleInstance,
    state: QueryVisibilityState | None,
) -> set[tuple[str, str]]:
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
    return {
        (cast(QueryRelationshipRow, item).from_id, cast(QueryRelationshipRow, item).to_id)
        for item in res.items
    }


@pytest.mark.parametrize("state", ["not-live", "live", "all"])
def test_retracted_edge_with_live_endpoints_agrees_across_surfaces(
    populated_instance: CruxibleInstance,
    state: QueryVisibilityState,
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
