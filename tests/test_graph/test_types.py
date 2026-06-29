"""Tests for graph runtime model helpers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import EntityLifecycleState, RelationshipAssertion
from cruxible_core.graph.types import (
    EntityInstance,
    EntityMetadata,
    RelationshipInstance,
    RelationshipMetadata,
)


def test_entity_instance_metadata_is_typed_entity_metadata() -> None:
    """``EntityInstance.metadata`` is a typed ``EntityMetadata``, like relationships.

    Mirrors ``RelationshipInstance.metadata: RelationshipMetadata``. A plain dict
    handed at construction is coerced into the typed envelope (owned slices into
    their typed fields), so the runtime object is never a free-form ``dict``.
    """
    # Default: a fresh, undecorated typed envelope.
    bare = EntityInstance(entity_type="Part", entity_id="BP-1")
    assert isinstance(bare.metadata, EntityMetadata)
    assert bare.metadata.lifecycle is None
    assert bare.metadata.to_metadata_dict() == {}

    # A stored flat dict coerces into the typed envelope at construction.
    coerced = EntityInstance(
        entity_type="Part",
        entity_id="BP-1",
        metadata={"lifecycle": {"status": "retired"}},
    )
    assert isinstance(coerced.metadata, EntityMetadata)
    assert coerced.metadata.lifecycle is not None
    assert coerced.metadata.lifecycle.status == "retired"


def test_lifecycle_settable_only_via_typed_field_not_free_form() -> None:
    """There is no free-form path to lifecycle on an ``EntityInstance``.

    ``metadata`` is a typed object: a free-form author key is walled off in
    ``extra`` and cannot be interpreted as the typed lifecycle. Lifecycle is set
    ONLY by constructing the typed ``lifecycle`` field.
    """
    # Typed field: the canonical (only) way to set lifecycle.
    typed = EntityInstance(
        entity_type="Part",
        entity_id="BP-1",
        metadata=EntityMetadata(lifecycle=EntityLifecycleState(status="retired")),
    )
    assert typed.metadata.lifecycle_status() == "retired"

    # The typed envelope has exactly the owned slices plus the walled-off extra;
    # there is no untyped top-level dict an author could write lifecycle into.
    assert set(EntityMetadata.model_fields) == {"lifecycle", "actor_context", "extra"}


def test_entity_metadata_round_trips_actor_context() -> None:
    """The typed ``actor_context`` slice round-trips through ``metadata_json``.

    Matches the live dogfooding shape (the 16 entities carrying ``actor_context``):
    a flat top-level ``actor_context`` object decodes into the typed
    ``GovernedActorContext`` and re-encodes with ``request_id`` omitted.
    """
    stored_in = {
        "actor_context": {
            "actor_id": "robert",
            "actor_type": "human_user",
            "operation_id": "op_close_wi",
            "org_id": "inst_85fcd2ae17234029",
            "timestamp": "2026-06-15T00:06:36.069749+00:00",
        }
    }
    decoded = EntityMetadata.from_metadata(stored_in)
    assert isinstance(decoded.actor_context, GovernedActorContext)
    assert decoded.actor_context.actor_id == "robert"

    stored_out = decoded.to_metadata_dict()
    assert stored_out["actor_context"]["actor_id"] == "robert"
    assert "request_id" not in stored_out["actor_context"]
    # Re-decoding the re-encoded form is idempotent.
    assert EntityMetadata.from_metadata(stored_out).actor_context == decoded.actor_context


def test_entity_metadata_round_trips_lifecycle_through_flat_dict() -> None:
    """The typed envelope encodes/decodes a retired lifecycle losslessly.

    Mirrors the storage path: ``to_metadata_dict`` produces the flat dict persisted
    in the ``metadata_json`` column; ``from_metadata`` decodes it back. ``retired``
    (the one lifecycle value carried by the live dogfooding instance) survives the
    full round-trip with no free-form spelunking.
    """
    envelope = EntityMetadata(lifecycle=EntityLifecycleState(status="retired", reason="rolled up"))

    stored = envelope.to_metadata_dict()
    # The stored shape is a flat dict with the typed lifecycle nested under one key.
    # The lifecycle payload is the full EntityLifecycleState serialization (same
    # bytes the deleted reserved-key encoder produced -- no shape drift).
    assert set(stored) == {"lifecycle"}
    assert stored["lifecycle"]["status"] == "retired"
    assert stored["lifecycle"]["reason"] == "rolled up"

    decoded = EntityMetadata.from_metadata(stored)
    assert decoded.lifecycle is not None
    assert decoded.lifecycle.status == "retired"
    assert decoded.lifecycle.reason == "rolled up"
    assert decoded.lifecycle_status() == "retired"
    assert decoded.is_live() is False


def test_entity_metadata_nests_free_form_keys_under_extra() -> None:
    """Free-form keys serialize NESTED under ``extra``, never beside ``lifecycle``.

    This is the structural wall: nothing free-form can sit at the same level as the
    typed ``lifecycle`` slot, so an author key (even one literally named
    ``lifecycle``) can never be mistaken for or collide with lifecycle state.
    """
    envelope = EntityMetadata(
        lifecycle=EntityLifecycleState(status="superseded"),
        extra={"note": "keep-me", "owner": "team-a"},
    )

    stored = envelope.to_metadata_dict()
    # Free-form keys live under the nested "extra" object, NOT at the top level.
    assert stored["extra"] == {"note": "keep-me", "owner": "team-a"}
    assert "note" not in stored
    assert stored["lifecycle"]["status"] == "superseded"

    decoded = EntityMetadata.from_metadata(stored)
    assert decoded.extra == {"note": "keep-me", "owner": "team-a"}
    assert decoded.lifecycle_status() == "superseded"


def test_entity_metadata_folds_stray_top_level_keys_into_extra() -> None:
    """Stray top-level free-form keys (not owned slices) fold into ``extra``.

    A stored dict that places free-form keys at the top level (e.g. the in-memory
    graph node payload) decodes with those keys folded into ``extra``; owned slices
    (``lifecycle``/``actor_context``) decode into their typed fields.
    """
    decoded = EntityMetadata.from_metadata({"note": "free", "lifecycle": {"status": "retired"}})

    # The owned "lifecycle" key decodes into the typed field; "note" folds to extra.
    assert decoded.lifecycle is not None
    assert decoded.lifecycle.status == "retired"
    assert decoded.extra == {"note": "free"}


def test_entity_metadata_without_lifecycle_decodes_to_default_live() -> None:
    """An undecorated metadata dict (no lifecycle slot) decodes to default live."""
    decoded = EntityMetadata.from_metadata({"note": "free"})

    assert decoded.lifecycle is None
    assert decoded.lifecycle_status() == "live"
    assert decoded.is_live() is True
    # Re-encoding nests the free-form key under "extra"; no lifecycle slot is added.
    assert decoded.to_metadata_dict() == {"extra": {"note": "free"}}


def test_entity_metadata_empty_inputs_decode_to_default() -> None:
    assert EntityMetadata.from_metadata(None).lifecycle is None
    assert EntityMetadata.from_metadata({}).lifecycle_status() == "live"
    # Idempotent on an already-typed envelope.
    typed = EntityMetadata(lifecycle=EntityLifecycleState(status="retired"))
    assert EntityMetadata.from_metadata(typed) is typed


def test_orphaned_is_not_an_authorable_entity_lifecycle_status() -> None:
    """``orphaned`` is a derived health finding, not an authorable lifecycle state.

    It was dropped from the entity lifecycle vocabulary, so constructing the typed
    state with it (the only authoring channel) is rejected at validation time.
    """
    with pytest.raises(ValidationError):
        EntityLifecycleState(status="orphaned")  # type: ignore[arg-type]

    # And it cannot be smuggled in through the metadata decode path either.
    with pytest.raises(ValidationError):
        EntityMetadata.from_metadata({"lifecycle": {"status": "orphaned"}})


def test_relationship_instance_identity_projections_ignore_non_identity_fields() -> None:
    relationship = RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
        edge_key=7,
        properties={"verified": True},
        metadata=RelationshipMetadata(assertion=RelationshipAssertion(group_override=True)),
    )
    changed_non_identity = relationship.model_copy(
        update={
            "edge_key": 8,
            "properties": {"verified": False},
            "metadata": RelationshipMetadata(),
        }
    )

    assert relationship.identity_tuple() == (
        "Part",
        "BP-1",
        "Vehicle",
        "V-1",
        "fits",
    )
    assert changed_non_identity.identity_tuple() == relationship.identity_tuple()
    assert relationship.identity_payload() == {
        "from_type": "Part",
        "from_id": "BP-1",
        "to_type": "Vehicle",
        "to_id": "V-1",
        "relationship_type": "fits",
    }
    assert relationship.endpoint_label() == "Part:BP-1->Vehicle:V-1"
    assert relationship.relationship_label() == "Part:BP-1 -[fits]-> Vehicle:V-1"
