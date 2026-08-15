"""Shared auth-managed entity materialization vocabulary."""

from __future__ import annotations

from cruxible_core.actor_vocabulary import (
    LOCAL_OPERATOR_ACTOR_ID as LOCAL_OPERATOR_ACTOR_ID,
)
from cruxible_core.actor_vocabulary import (
    LOCAL_OPERATOR_ACTOR_TYPE as LOCAL_OPERATOR_ACTOR_TYPE,
)
from cruxible_core.actor_vocabulary import (
    LOCAL_OPERATOR_KIND as LOCAL_OPERATOR_KIND,
)
from cruxible_core.actor_vocabulary import (
    LOCAL_OPERATOR_ORG_ID as LOCAL_OPERATOR_ORG_ID,
)
from cruxible_core.actor_vocabulary import (
    LOCAL_OPERATOR_STATUS as LOCAL_OPERATOR_STATUS,
)

AUTH_MANAGED_CREDENTIAL_PROPERTY_NAMES = frozenset(
    {
        "actor_id",
        "actor_type",
        "credential_id",
        "credential_type",
        "created_at",
        "instance_id",
        "kind",
        "label",
        "org_id",
        "permission_mode",
    }
)

AUTH_MANAGED_LOCAL_OPERATOR_PROPERTY_NAMES = frozenset(
    {
        "actor_id",
        "actor_type",
        "kind",
        "label",
        "org_id",
        "status",
    }
)
