"""Materialize auth-managed graph entities from runtime credentials."""

from __future__ import annotations

from cruxible_core.config.auth_managed import AUTH_MANAGED_CREDENTIAL_PROPERTY_NAMES
from cruxible_core.config.schema import EntityTypeSchema
from cruxible_core.governance.actors import ActorType, GovernedActorContext
from cruxible_core.graph.operations import apply_entity, validate_entity
from cruxible_core.graph.types import EntityInstance, EntityMetadata
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.primitives import new_id
from cruxible_core.server.credentials import RuntimeCredentialRecord
from cruxible_core.service.direct_write_policy import TOKEN_MINT_SOURCE
from cruxible_core.temporal import utc_now

_AUTH_MANAGED_ACTOR_TYPE: ActorType = "service_account"
_AUTH_MANAGED_CREDENTIAL_TYPE = "runtime_credential"


def materialize_auth_managed_entities(
    instance: InstanceProtocol,
    credential: RuntimeCredentialRecord,
) -> list[str]:
    """Materialize configured auth-managed entity types for *credential*.

    The credential store is the identity source of truth. Config only opts entity
    types into materialization with ``auth_managed: true``; no type name is
    special-cased here. The entity id is the runtime actor id used by authenticated
    write attribution today: the credential label. That keeps re-mint/rotation for
    the same actor label idempotent while still recording the current credential id
    on schemas that declare a ``credential_id`` property.
    """
    config = instance.load_config()
    auth_managed_types = [
        (entity_type, schema)
        for entity_type, schema in config.entity_types.items()
        if schema.auth_managed
    ]
    if not auth_managed_types:
        return []

    graph = instance.load_graph()
    actor_context = _credential_actor_context(credential)
    materialized: list[str] = []
    written_entities: list[EntityInstance] = []
    for entity_type, schema in auth_managed_types:
        entity_id = actor_context.actor_id
        properties = _credential_properties(schema, credential, actor_context)
        validated = validate_entity(
            config,
            graph,
            entity_type,
            entity_id,
            properties,
            metadata=EntityMetadata(actor_context=actor_context),
        )
        apply_entity(
            graph,
            validated,
            config=config,
            source=TOKEN_MINT_SOURCE,
        )
        written = graph.get_entity(entity_type, entity_id)
        assert written is not None
        written_entities.append(written)
        materialized.append(entity_type)

    instance.save_graph_delta(graph, entities=written_entities)
    return materialized


def _credential_actor_context(credential: RuntimeCredentialRecord) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type=_AUTH_MANAGED_ACTOR_TYPE,
        actor_id=credential.label,
        org_id=credential.instance_id,
        operation_id=new_id("op", length=16, separator="_"),
        timestamp=utc_now(),
    )


def _credential_properties(
    schema: EntityTypeSchema,
    credential: RuntimeCredentialRecord,
    actor_context: GovernedActorContext,
) -> dict[str, object]:
    values: dict[str, object | None] = {
        "actor_id": actor_context.actor_id,
        "actor_type": actor_context.actor_type,
        "credential_id": credential.credential_id,
        "credential_type": _AUTH_MANAGED_CREDENTIAL_TYPE,
        "created_at": credential.created_at,
        "instance_id": credential.instance_id,
        "kind": actor_context.actor_type,
        "label": credential.label,
        "org_id": actor_context.org_id,
        "permission_mode": credential.permission_mode.name.lower(),
    }
    primary_key = schema.get_primary_key()
    return {
        name: value
        for name, value in values.items()
        if name in AUTH_MANAGED_CREDENTIAL_PROPERTY_NAMES
        and name in schema.properties
        and name != primary_key
        and value is not None
    }
