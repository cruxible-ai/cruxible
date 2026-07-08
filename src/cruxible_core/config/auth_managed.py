"""Shared auth-managed entity materialization vocabulary."""

from __future__ import annotations

from typing import Final

LOCAL_OPERATOR_ACTOR_ID: Final = "operator"
LOCAL_OPERATOR_ACTOR_TYPE: Final = "human_user"
LOCAL_OPERATOR_KIND: Final = "human"
LOCAL_OPERATOR_ORG_ID: Final = "local"
LOCAL_OPERATOR_STATUS: Final = "active"

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
