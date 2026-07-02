"""Shared auth-managed entity materialization vocabulary."""

from __future__ import annotations

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
