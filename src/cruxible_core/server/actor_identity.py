"""Pure runtime actor identity used by Playbill and legacy adapters."""

from __future__ import annotations

from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.temporal import utc_now
from cruxible_core.actor_vocabulary import (
    LOCAL_OPERATOR_ACTOR_ID as LOCAL_OPERATOR_ACTOR_ID,
)
from cruxible_core.actor_vocabulary import (
    LOCAL_OPERATOR_ACTOR_TYPE as LOCAL_OPERATOR_ACTOR_TYPE,
)
from cruxible_core.actor_vocabulary import LOCAL_OPERATOR_KIND as LOCAL_OPERATOR_KIND
from cruxible_core.actor_vocabulary import LOCAL_OPERATOR_ORG_ID as LOCAL_OPERATOR_ORG_ID
from cruxible_core.actor_vocabulary import (
    LOCAL_OPERATOR_STATUS as LOCAL_OPERATOR_STATUS,
)
from cruxible_core.playbill.actor_context import GovernedActorContext


def local_operator_actor_context(*, request_id: str | None = None) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type=LOCAL_OPERATOR_ACTOR_TYPE,
        actor_id=LOCAL_OPERATOR_ACTOR_ID,
        org_id=LOCAL_OPERATOR_ORG_ID,
        operation_id=new_id("op", length=16, separator="_"),
        timestamp=utc_now(),
        request_id=request_id,
    )


__all__ = [
    "LOCAL_OPERATOR_ACTOR_ID",
    "LOCAL_OPERATOR_ACTOR_TYPE",
    "LOCAL_OPERATOR_KIND",
    "LOCAL_OPERATOR_ORG_ID",
    "LOCAL_OPERATOR_STATUS",
    "local_operator_actor_context",
]
