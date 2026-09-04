"""One validation seam: the local MCP door refuses what the served door refuses.

An MCP client with no daemon reaches the facade in process. That path used to
skip whatever the HTTP route's request model checks beyond the facade's own
signature, so a control character in a decommission reason passed the MCP door,
reached the write, and raised a raw pydantic error from inside it -- an untyped
failure where the served route would have answered a refusal the caller can
read. The same class existed for every served-model-only validator.

The inventory below is the law rather than a convenience: a mutating operation
that is not declared, or one declared with a model and dispatched without a
payload, fails here. `None` is a declaration too -- that route carries no
request body, so there is no second model for the local door to disagree with.
"""

from __future__ import annotations

import ast
from pathlib import Path

from cruxible_core.mcp.handlers import MCP_LOCAL_REQUEST_MODELS
from cruxible_core.runtime.permissions import PERMISSION_REQUIREMENTS, PermissionMode

HANDLERS = Path(__file__).resolve().parents[2] / "src/cruxible_core/mcp/handlers.py"


def _dispatch_sites() -> dict[str, bool]:
    """Every local-dispatch site in the MCP handlers: operation -> carries a payload."""

    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"), filename=str(HANDLERS))
    sites: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "_dispatch_remote_or_local":
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        operation = keywords.get("operation_name")
        if not isinstance(operation, ast.Constant) or not isinstance(operation.value, str):
            raise AssertionError("every MCP dispatch names its operation as a literal")
        sites[operation.value] = "local_payload" in keywords
    return sites


def _is_mutating(operation: str) -> bool:
    return PERMISSION_REQUIREMENTS.get(operation, PermissionMode.READ_ONLY) != (
        PermissionMode.READ_ONLY
    )


def test_every_mutating_mcp_operation_declares_its_served_request_model() -> None:
    mutating = {name for name in _dispatch_sites() if _is_mutating(name)}

    assert mutating - set(MCP_LOCAL_REQUEST_MODELS) == set(), (
        "a mutating MCP operation with no declared served request model would validate "
        "less in process than it does over HTTP"
    )
    assert set(MCP_LOCAL_REQUEST_MODELS) - mutating == set(), (
        "the inventory names an operation the MCP handlers no longer dispatch"
    )


def test_every_declared_model_is_given_a_payload_to_validate() -> None:
    sites = _dispatch_sites()
    offenders = [
        operation
        for operation, model in MCP_LOCAL_REQUEST_MODELS.items()
        if model is not None and not sites.get(operation, False)
    ]

    assert offenders == [], (
        "these operations declare a served request model but hand the local door nothing "
        f"to validate: {offenders}"
    )


def test_a_route_without_a_body_declares_that_rather_than_omitting_it() -> None:
    """The `None` entries are the ones that must NOT be given a payload."""

    sites = _dispatch_sites()
    offenders = [
        operation
        for operation, model in MCP_LOCAL_REQUEST_MODELS.items()
        if model is None and sites.get(operation, False)
    ]

    assert offenders == [], (
        f"these operations pass a payload no declared model validates: {offenders}"
    )


def test_read_operations_are_deliberately_absent_from_the_seam() -> None:
    """A read has no write to guard, and listing one would blunt what the law means."""

    reads = {name for name in _dispatch_sites() if not _is_mutating(name)}

    assert reads and reads.isdisjoint(MCP_LOCAL_REQUEST_MODELS)
