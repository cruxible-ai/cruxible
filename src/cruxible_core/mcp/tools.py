"""MCP registrations for the Playbill-only public surface."""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP

from cruxible_client import contracts
from cruxible_core import __version__
from cruxible_core.mcp import handlers
from cruxible_core.mcp.tool_prompts import tool_description


def register_tools(
    server: FastMCP,
    *,
    offload_sync_calls: bool = False,
) -> list[str]:
    registered: list[str] = []

    def _tool(fn: Callable[..., Any]) -> Callable[..., Any]:
        registered_fn = fn
        if offload_sync_calls:

            @wraps(fn)
            async def run_in_worker(*args: Any, **kwargs: Any) -> Any:
                return await asyncio.to_thread(fn, *args, **kwargs)

            registered_fn = run_in_worker
        server.tool(description=tool_description(fn.__name__))(registered_fn)
        registered.append(fn.__name__)
        return fn

    @_tool
    def cruxible_version() -> dict[str, str]:
        """Return the running cruxible-core version."""
        return {"version": __version__}

    @_tool
    def cruxible_server_info() -> contracts.ServerInfoResult:
        """Return daemon metadata without loading semantic state."""
        return handlers.handle_server_info()

    @_tool
    def cruxible_playbill_host_create(
        instance_id: str | None = None,
    ) -> contracts.PlaybillHostResult:
        """Allocate an empty daemon host before Playbill bootstrap."""
        return handlers.handle_playbill_host_create(instance_id)

    @_tool
    def cruxible_playbill_init(
        instance_id: str,
        principals: list[dict[str, Any]],
        operating_profile: Literal["local", "cloud"] = "local",
    ) -> contracts.PlaybillInitResult:
        """Bootstrap Playbill from client-generated public principals."""
        return handlers.handle_playbill_init(instance_id, principals, operating_profile)

    @_tool
    def cruxible_playbill_store_body(
        instance_id: str, content_base64: str
    ) -> contracts.PlaybillCasObjectResult:
        """Store inert exact body bytes."""
        return handlers.handle_playbill_store_body(instance_id, content_base64)

    @_tool
    def cruxible_playbill_propose_document(
        instance_id: str,
        shell: dict[str, Any],
        proposal_name: str,
        source_compilation_digest: str | None = None,
    ) -> contracts.PlaybillProposalInspection:
        """Propose a governed Document create or supersession."""
        return handlers.handle_playbill_propose_document(
            instance_id, shell, proposal_name, source_compilation_digest
        )

    @_tool
    def cruxible_playbill_inspect_proposal(
        instance_id: str, proposal_id: str
    ) -> contracts.PlaybillProposalInspection:
        """Inspect immutable proposal evidence."""
        return handlers.handle_playbill_inspect_proposal(instance_id, proposal_id)

    @_tool
    def cruxible_playbill_inspect_refusal(
        instance_id: str, proposal_id: str
    ) -> contracts.PlaybillRefusalInspection:
        """Inspect typed admission and law diagnostics."""
        return handlers.handle_playbill_inspect_refusal(instance_id, proposal_id)

    @_tool
    def cruxible_playbill_review(
        instance_id: str,
        proposal_id: str,
        include_body: bool = False,
    ) -> contracts.PlaybillProposalReview:
        """Render a structured candidate review."""
        return handlers.handle_playbill_review(instance_id, proposal_id, include_body=include_body)

    @_tool
    def cruxible_playbill_prepare_approval(
        instance_id: str,
        proposal_id: str,
        signer_id: str,
        include_body: bool = False,
    ) -> contracts.PlaybillApprovalChallenge:
        """Fetch the exact statement for a client-held signer."""
        return handlers.handle_playbill_prepare_approval(
            instance_id,
            proposal_id,
            signer_id=signer_id,
            include_body=include_body,
        )

    @_tool
    def cruxible_playbill_submit_approval(
        instance_id: str,
        proposal_id: str,
        attestation: dict[str, Any],
    ) -> contracts.PlaybillApprovalReceipt:
        """Submit a public approval attestation."""
        return handlers.handle_playbill_submit_approval(instance_id, proposal_id, attestation)

    @_tool
    def cruxible_playbill_activate(
        instance_id: str, proposal_id: str
    ) -> contracts.PlaybillActivationReceipt:
        """Settle an approved candidate by compare-and-set."""
        return handlers.handle_playbill_activate(instance_id, proposal_id)

    @_tool
    def cruxible_playbill_list_documents(
        instance_id: str,
    ) -> contracts.PlaybillDocumentList:
        """List accepted Documents at the current coordinate."""
        return handlers.handle_playbill_list_documents(instance_id)

    @_tool
    def cruxible_playbill_get_document(
        instance_id: str, identity: str
    ) -> contracts.PlaybillDocumentView:
        """Read one accepted Document envelope and facts."""
        return handlers.handle_playbill_get_document(instance_id, identity)

    @_tool
    def cruxible_playbill_dereference(
        instance_id: str, identity: str
    ) -> contracts.PlaybillBodyRead:
        """Dereference verified accepted body bytes."""
        return handlers.handle_playbill_dereference(instance_id, identity)

    @_tool
    def cruxible_playbill_history(
        instance_id: str, identity: str
    ) -> contracts.PlaybillDocumentHistory:
        """Read one Document's replay-verified history."""
        return handlers.handle_playbill_history(instance_id, identity)

    @_tool
    def cruxible_playbill_explain(
        instance_id: str,
        subject: dict[str, Any],
        at: dict[str, Any],
        detail: Literal["summary", "evidence", "proof"] = "summary",
        include_body: bool = False,
    ) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail:
        """Explain governance and provenance at an exact coordinate."""
        return handlers.handle_playbill_explain(
            instance_id,
            subject,
            at,
            detail=detail,
            include_body=include_body,
        )

    @_tool
    def cruxible_playbill_source_context(
        instance_id: str,
    ) -> contracts.PlaybillSourceContext:
        """Fetch path-free inputs for local source compilation."""
        return handlers.handle_playbill_source_context(instance_id)

    @_tool
    def cruxible_playbill_check_source_bundle(
        instance_id: str, bundle: dict[str, Any]
    ) -> contracts.PlaybillSourceCheckResult:
        """Compare a compiled source bundle with accepted state."""
        return handlers.handle_playbill_check_source_bundle(instance_id, bundle)

    @_tool
    def cruxible_playbill_propose_source_bundle(
        instance_id: str,
        bundle: dict[str, Any],
        source_name: str,
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        """Propose frozen source bytes without a client path."""
        return handlers.handle_playbill_propose_source_bundle(
            instance_id,
            bundle,
            source_name=source_name,
            proposal_name=proposal_name,
        )

    @_tool
    def cruxible_playbill_list_principals(
        instance_id: str,
    ) -> contracts.PlaybillPrincipalList:
        """List accepted public principal records."""
        return handlers.handle_playbill_list_principals(instance_id)

    @_tool
    def cruxible_playbill_propose_principal_change(
        instance_id: str,
        principal: dict[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        """Propose principal registration, rotation, revocation, or recovery."""
        return handlers.handle_playbill_propose_principal_change(
            instance_id, principal, proposal_name
        )

    return registered
