"""Guardrails for the surviving Playbill transport-permission posture."""

from __future__ import annotations

import inspect

from cruxible_core.mcp.server import BASE_INSTRUCTIONS
from cruxible_core.runtime import permissions as permissions_module
from cruxible_core.server import auth as auth_module


def _flat(text: str) -> str:
    return " ".join(text.lower().split())


def test_permission_module_separates_transport_reachability_from_semantic_authority() -> None:
    doc = _flat(permissions_module.__doc__ or "")

    assert "endpoint reachability only" in doc
    assert "playbill principals and acceptance laws determine semantic authority" in doc
    assert "local cli is still an operator process" in doc


def test_mcp_instructions_publish_the_same_transport_semantic_boundary() -> None:
    instructions = _flat(BASE_INSTRUCTIONS)

    assert "temporary transport tiers" in instructions
    assert "only control endpoint reachability" in instructions
    assert "playbill principals and acceptance laws control semantic authority" in instructions
    assert "a proposal is not accepted state" in instructions
    assert "an approval is not activation" in instructions


def test_runtime_bootstrap_secret_repeatably_authorizes_every_daemon_wide_action() -> None:
    """Host creation is a repeatable operator action, not a one-shot.

    Succeeds `test_runtime_bootstrap_secret_is_single_use_for_host_create_only`,
    which pinned the opposite law: host creation was authorized only while the
    bootstrap secret was UNCLAIMED. Card 98 showed what that cost. Every other
    credential a daemon holds is instance-scoped and is rejected by
    `require_unscoped_operator`, so after the first `credential
    claim-bootstrap` NO credential could allocate a second host, and the only
    repair was restarting the daemon -- which mints a fresh secret at the same
    path, invalidating the operator's saved copy and taking every hosted
    instance offline. A multi-tenant control plane cannot restart the daemon to
    add a tenant.

    The claim itself is still single use; that property lives in the
    `runtime_bootstrap_claims` table, not here.
    """

    source = inspect.getsource(auth_module.token_auth_middleware)

    operator_branch = source.split("elif auth_enabled:", maxsplit=1)[0]
    assert "_is_playbill_host_create_request" in operator_branch
    assert "_is_server_operation_request" in operator_branch
    assert "bootstrap_secret_claimed" not in operator_branch


def test_repeatable_bootstrap_server_operations_are_info_host_show_restart_and_stop() -> None:
    """Pin the exact daemon-operation route set an unscoped operator may repeat.

    PC-DF4 added the pre-init host-show route to the set without moving the
    name, leaving a guardrail whose name asserted a narrower set than its body;
    ops hotfix 1 added the stop route and moved the name with it. Host creation
    is authorized alongside this set through its own route predicate rather than
    by joining it, because it is the one daemon-wide action that carries a body
    and a route of its own shape.
    """

    assert set(auth_module._SERVER_OPERATION_ROUTES) == {
        ("GET", "/api/v1/server/info"),
        ("GET", "/api/v1/{instance_id}/playbill/host"),
        ("POST", "/api/v1/server/restart"),
        ("POST", "/api/v1/server/stop"),
    }
