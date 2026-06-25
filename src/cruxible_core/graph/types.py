"""Runtime graph types for entity instances and relationship instances.

These are the runtime objects stored in the EntityGraph, distinct from
the schema types (PropertySchema, EntityTypeSchema, etc.) which define
the config structure.
"""

from __future__ import annotations

from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    model_serializer,
    model_validator,
)

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import (
    EntityLifecycleState,
    EntityLifecycleStatus,
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
    relationship_assertion_from_metadata,
    relationship_is_live,
    relationship_lifecycle_is_active,
)
from cruxible_core.graph.evidence import RelationshipEvidence
from cruxible_core.graph.provenance import (
    RelationshipProvenance,
    dump_provenance,
    load_provenance,
    make_provenance,
    provenance_group_id,
    stamp_provenance_modified,
)
from cruxible_core.temporal import is_effective


def make_node_id(entity_type: str, entity_id: str) -> str:
    """Build the canonical node ID for a (type, id) pair."""
    return f"{entity_type}:{entity_id}"


def split_node_id(node_id: str) -> tuple[str, str]:
    """Split a canonical node ID back into (entity_type, entity_id).

    Inverse of ``make_node_id``.  Handles entity IDs that contain colons.
    """
    entity_type, sep, entity_id = node_id.partition(":")
    if not sep:
        raise ValueError(f"Invalid node_id: {node_id!r}")
    return entity_type, entity_id


class EntityInstance(BaseModel):
    """A single entity instance in the graph."""

    entity_type: str
    entity_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    # Typed metadata envelope, mirroring ``RelationshipInstance.metadata:
    # RelationshipMetadata``. There is NO free-form ``dict`` metadata slot on an
    # entity: owned slices (lifecycle, actor_context) are named typed fields and any
    # author free-form metadata is walled off inside ``EntityMetadata.extra`` -- so
    # free-form data can never sit beside, or be mistaken for, the typed lifecycle.
    metadata: EntityMetadata = Field(default_factory=lambda: EntityMetadata())

    def node_id(self) -> str:
        """Return the unique node ID for this entity."""
        return make_node_id(self.entity_type, self.entity_id)


class EntityMetadata(BaseModel):
    """Cruxible-owned, typed metadata for an entity instance.

    The exact entity-side analogue of :class:`RelationshipMetadata`: a typed model
    that IS the entity's ``metadata`` field (``EntityInstance.metadata``), not a
    transient view over a free-form ``dict``. Every slice is a NAMED field:

    * ``lifecycle`` -- the typed :class:`EntityLifecycleState` (the canonical
      soft-delete / supersession axis, gated out of live reads), mirroring how
      ``RelationshipLifecycleState`` rides inside ``RelationshipMetadata``.
    * ``actor_context`` -- the :class:`GovernedActorContext` that last wrote the
      entity, stamped by the governed write path.
    * ``extra`` -- any author-supplied, non-Cruxible free-form metadata. It
      serializes NESTED under an ``"extra"`` key so nothing free-form can ever sit
      at the same level as -- or be mistaken for -- the typed ``lifecycle`` state.

    Because ``lifecycle`` is a dedicated typed field and free-form keys are walled
    off inside ``extra``, lifecycle is settable ONLY via the typed ``lifecycle``
    field: there is no reserved-key convention and no free-form path to lifecycle.

    The serialized shape is a flat dict whose owned slices are top-level
    (``{"lifecycle": {...}, "actor_context": {...}, "extra": {...}}``); ``None`` /
    empty slices are dropped, so an undecorated entity serializes to ``{}``. This is
    exactly the dict persisted in the ``metadata_json`` column and returned on the
    read/wire surfaces. A stored dict decodes straight back via
    :meth:`from_metadata` (Cruxible-owned keys validate into their typed fields;
    everything else folds into ``extra``); there is NO legacy/old-shape parser.
    """

    model_config = ConfigDict(extra="forbid")

    lifecycle: EntityLifecycleState | None = None
    actor_context: GovernedActorContext | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_mapping(cls, value: Any) -> Any:
        """Decode a stored flat metadata dict (or an already-typed envelope).

        Cruxible-owned keys (``lifecycle``, ``actor_context``) validate into their
        typed fields; every other key folds into ``extra``. ``extra`` itself, when
        already present (the re-encoded form), is merged with any stray siblings. An
        already-typed :class:`EntityMetadata` passes through unchanged; empty /
        ``None`` decodes to the default (all owned fields ``None``, empty ``extra``).
        """
        if value is None:
            return {}
        if isinstance(value, EntityMetadata):
            return value
        if not isinstance(value, dict):
            raise TypeError("entity metadata must be a mapping")
        payload = dict(value)
        extra: dict[str, Any] = {}
        nested_extra = payload.pop("extra", None)
        if isinstance(nested_extra, dict):
            extra.update(nested_extra)
        elif nested_extra is not None:
            raise TypeError("entity metadata 'extra' must be a mapping")
        owned = {"lifecycle", "actor_context"}
        for key in [k for k in payload if k not in owned]:
            extra[key] = payload.pop(key)
        payload["extra"] = extra
        return payload

    @model_serializer(mode="plain")
    def _serialize(self) -> dict[str, Any]:
        """Serialize to the flat stored/wire dict, dropping unset / empty slices.

        Produces ``{}`` for an undecorated entity and the nested typed objects
        otherwise, with free-form keys nested under ``"extra"``. ``exclude_none`` on
        the nested models keeps the bytes identical to the historical encode path
        (e.g. ``actor_context`` without ``request_id``).
        """
        out: dict[str, Any] = {}
        if self.lifecycle is not None:
            out["lifecycle"] = self.lifecycle.model_dump(mode="json")
        if self.actor_context is not None:
            out["actor_context"] = self.actor_context.model_dump(mode="json", exclude_none=True)
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_metadata(cls, value: Any) -> EntityMetadata:
        """Decode a stored entity-metadata dict (or typed envelope) into this model.

        Thin wrapper over ``model_validate`` for read sites that hold the flat dict
        form (e.g. the in-memory graph node payload). The ``lifecycle`` /
        ``actor_context`` keys, when present, are the typed serializations this model
        wrote; all other keys fold into ``extra``. There is no old shape to tolerate.
        """
        if isinstance(value, EntityMetadata):
            return value
        return cls.model_validate(value)

    def to_metadata_dict(self) -> dict[str, Any]:
        """Re-encode this typed model into the flat storable metadata dict.

        The inverse of :meth:`from_metadata`; identical to ``model_dump()`` and used
        at the storage / in-memory-graph boundary, mirroring how
        ``RelationshipMetadata`` round-trips through its own json column.
        """
        return self.model_dump()

    def lifecycle_status(self) -> EntityLifecycleStatus:
        """Return the typed lifecycle status, defaulting to ``live`` when absent."""
        return self.lifecycle.status if self.lifecycle is not None else "live"

    def is_live(self) -> bool:
        """Return whether the entity participates in live graph semantics.

        Live when the lifecycle status is ``live`` (the default for an undecorated
        entity) and -- if a bounded effective window is set -- the current time is
        within it.
        """
        lifecycle = self.lifecycle
        if lifecycle is None:
            return True
        if lifecycle.status != "live":
            return False
        return is_effective(
            effective_from=lifecycle.effective_from,
            effective_until=lifecycle.effective_until,
        )


# ``EntityInstance.metadata`` forward-references ``EntityMetadata`` (declared above
# ``EntityMetadata`` so the entity type reads naturally); resolve the reference now
# that the typed envelope exists.
EntityInstance.model_rebuild()


class RelationshipMetadata(BaseModel):
    """Cruxible-owned metadata for a relationship instance."""

    provenance: RelationshipProvenance | None = None
    assertion: RelationshipAssertion = Field(default_factory=RelationshipAssertion)
    evidence: RelationshipEvidence | None = None


class RelationshipInstance(BaseModel):
    """A single relationship instance in the graph.

    Also used as the target reference in feedback records. The
    ``relationship_type`` field accepts ``"relationship"`` during
    validation so that legacy feedback JSON round-trips correctly.
    """

    model_config = ConfigDict(populate_by_name=True)

    relationship_type: str = Field(
        validation_alias=AliasChoices("relationship_type", "relationship"),
    )
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: RelationshipMetadata = Field(default_factory=RelationshipMetadata)

    def from_node_id(self) -> str:
        """Return the source node ID."""
        return make_node_id(self.from_type, self.from_id)

    def to_node_id(self) -> str:
        """Return the target node ID."""
        return make_node_id(self.to_type, self.to_id)

    def identity_tuple(self) -> tuple[str, str, str, str, str]:
        """Return the stable relationship identity tuple."""
        return (
            self.from_type,
            self.from_id,
            self.to_type,
            self.to_id,
            self.relationship_type,
        )

    def identity_payload(self) -> dict[str, str]:
        """Return the public relationship identity payload."""
        return {
            "from_type": self.from_type,
            "from_id": self.from_id,
            "to_type": self.to_type,
            "to_id": self.to_id,
            "relationship_type": self.relationship_type,
        }

    def endpoint_label(self) -> str:
        """Return a compact source-to-target endpoint label."""
        return f"{self.from_type}:{self.from_id}->{self.to_type}:{self.to_id}"

    def relationship_label(self) -> str:
        """Return a compact relationship tuple label."""
        return (
            f"{self.from_type}:{self.from_id} -[{self.relationship_type}]-> "
            f"{self.to_type}:{self.to_id}"
        )


__all__ = [
    "EntityInstance",
    "EntityLifecycleState",
    "EntityMetadata",
    "RelationshipInstance",
    "RelationshipAssertion",
    "RelationshipLifecycleState",
    "RelationshipMetadata",
    "RelationshipEvidence",
    "RelationshipProvenance",
    "RelationshipReviewState",
    "dump_provenance",
    "load_provenance",
    "make_node_id",
    "make_provenance",
    "provenance_group_id",
    "relationship_assertion_from_metadata",
    "relationship_is_live",
    "relationship_lifecycle_is_active",
    "split_node_id",
    "stamp_provenance_modified",
]
