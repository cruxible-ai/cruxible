"""Materialize auth-managed graph entities from runtime credentials."""

from __future__ import annotations

from collections.abc import Mapping

from cruxible_core.config.auth_managed import (
    AUTH_MANAGED_CREDENTIAL_PROPERTY_NAMES,
    AUTH_MANAGED_LOCAL_OPERATOR_PROPERTY_NAMES,
    LOCAL_OPERATOR_ACTOR_ID,
    LOCAL_OPERATOR_ACTOR_TYPE,
    LOCAL_OPERATOR_KIND,
    LOCAL_OPERATOR_ORG_ID,
    LOCAL_OPERATOR_STATUS,
)
from cruxible_core.config.schema import EntityTypeSchema
from cruxible_core.governance.actors import ActorType, GovernedActorContext
from cruxible_core.graph.operations import apply_entity, validate_entity
from cruxible_core.graph.types import EntityInstance, EntityMetadata
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.primitives import new_id
from cruxible_core.server.config import is_server_auth_enabled
from cruxible_core.server.credentials import RuntimeCredentialRecord
from cruxible_core.service.direct_write_policy import TOKEN_MINT_SOURCE
from cruxible_core.temporal import utc_now

_AUTH_MANAGED_ACTOR_TYPE: ActorType = "service_account"
_AUTH_MANAGED_CREDENTIAL_TYPE = "runtime_credential"
_LOCAL_OPERATOR_ACTOR_TYPE: ActorType = LOCAL_OPERATOR_ACTOR_TYPE


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


def materialize_local_operator_auth_managed_entities(
    instance: InstanceProtocol,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Materialize the auth-off local operator for auth-managed entity types.

    Auth-on daemons use runtime credentials as the identity source of truth:
    credential mint/rotation calls ``materialize_auth_managed_entities`` above.
    Auth-off daemons have no credential ceremony, so a declared local operator is
    materialized through the same sanctioned ``token_mint`` graph source. Direct
    writers remain unable to write ``mint_only`` types.
    """
    if is_server_auth_enabled(environ):
        return []

    config = instance.load_config()
    auth_managed_types = [
        (entity_type, schema)
        for entity_type, schema in config.entity_types.items()
        if schema.auth_managed
    ]
    if not auth_managed_types:
        return []

    graph = instance.load_graph()
    actor_context: GovernedActorContext | None = None
    materialized: list[str] = []
    written_entities: list[EntityInstance] = []
    for entity_type, schema in auth_managed_types:
        properties = _local_operator_properties(schema)
        existing = graph.get_entity(entity_type, LOCAL_OPERATOR_ACTOR_ID)
        if _local_operator_entity_is_current(existing, properties):
            continue
        if actor_context is None:
            actor_context = local_operator_actor_context()
        validated = validate_entity(
            config,
            graph,
            entity_type,
            LOCAL_OPERATOR_ACTOR_ID,
            properties,
            metadata=EntityMetadata(actor_context=actor_context),
        )
        apply_entity(
            graph,
            validated,
            config=config,
            source=TOKEN_MINT_SOURCE,
        )
        written = graph.get_entity(entity_type, LOCAL_OPERATOR_ACTOR_ID)
        assert written is not None
        written_entities.append(written)
        materialized.append(entity_type)

    if written_entities:
        instance.save_graph_delta(graph, entities=written_entities)
    return materialized


def local_operator_actor_context(*, request_id: str | None = None) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type=_LOCAL_OPERATOR_ACTOR_TYPE,
        actor_id=LOCAL_OPERATOR_ACTOR_ID,
        org_id=LOCAL_OPERATOR_ORG_ID,
        operation_id=new_id("op", length=16, separator="_"),
        timestamp=utc_now(),
        request_id=request_id,
    )


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


def _local_operator_properties(schema: EntityTypeSchema) -> dict[str, object]:
    values: dict[str, object | None] = {
        "actor_id": LOCAL_OPERATOR_ACTOR_ID,
        "actor_type": LOCAL_OPERATOR_ACTOR_TYPE,
        "kind": LOCAL_OPERATOR_KIND,
        "label": LOCAL_OPERATOR_ACTOR_ID,
        "org_id": LOCAL_OPERATOR_ORG_ID,
        "status": LOCAL_OPERATOR_STATUS,
    }
    primary_key = schema.get_primary_key()
    return {
        name: value
        for name, value in values.items()
        if name in AUTH_MANAGED_LOCAL_OPERATOR_PROPERTY_NAMES
        and name in schema.properties
        and name != primary_key
        and value is not None
    }


def _local_operator_entity_is_current(
    entity: EntityInstance | None,
    properties: Mapping[str, object],
) -> bool:
    if entity is None:
        return False
    for name, value in properties.items():
        if entity.properties.get(name) != value:
            return False
    actor_context = entity.metadata.actor_context
    return (
        actor_context is not None
        and actor_context.actor_type == _LOCAL_OPERATOR_ACTOR_TYPE
        and actor_context.actor_id == LOCAL_OPERATOR_ACTOR_ID
        and actor_context.org_id == LOCAL_OPERATOR_ORG_ID
    )
