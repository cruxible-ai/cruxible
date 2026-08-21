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

    @_tool
    def cruxible_playbill_propose_subject(
        instance_id: str,
        shell: dict[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        """Propose one identity-only governed Subject."""
        return handlers.handle_playbill_propose_subject(instance_id, shell, proposal_name)

    @_tool
    def cruxible_playbill_list_subjects(
        instance_id: str,
    ) -> contracts.PlaybillSubjectList:
        """List accepted Subjects at the current coordinate."""
        return handlers.handle_playbill_list_subjects(instance_id)

    @_tool
    def cruxible_playbill_get_subject(
        instance_id: str, subject_kind: str, subject_id: str
    ) -> contracts.PlaybillSubjectView:
        """Read one accepted Subject envelope and facts."""
        return handlers.handle_playbill_get_subject(instance_id, subject_kind, subject_id)

    @_tool
    def cruxible_playbill_subject_history(
        instance_id: str, subject_kind: str, subject_id: str
    ) -> contracts.PlaybillSubjectHistory:
        """Read one Subject's accepted lineage."""
        return handlers.handle_playbill_subject_history(instance_id, subject_kind, subject_id)

    @_tool
    def cruxible_playbill_propose_claim_type(
        instance_id: str,
        claim_type: dict[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        """Propose one governed ClaimType interface."""
        return handlers.handle_playbill_propose_claim_type(instance_id, claim_type, proposal_name)

    @_tool
    def cruxible_playbill_list_claim_types(
        instance_id: str,
    ) -> contracts.PlaybillClaimTypeList:
        """List accepted ClaimType interfaces."""
        return handlers.handle_playbill_list_claim_types(instance_id)

    @_tool
    def cruxible_playbill_get_claim_type(
        instance_id: str, predicate: str
    ) -> contracts.PlaybillClaimTypeView:
        """Read one accepted ClaimType by predicate."""
        return handlers.handle_playbill_get_claim_type(instance_id, predicate)

    @_tool
    def cruxible_playbill_propose_claim(
        instance_id: str,
        authoring: dict[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillClaimProposal:
        """Propose one direct Claim with its inert Capture."""
        return handlers.handle_playbill_propose_claim(instance_id, authoring, proposal_name)

    @_tool
    def cruxible_playbill_propose_claims(
        instance_id: str,
        authorings: list[dict[str, Any]],
        proposal_name: str,
    ) -> contracts.PlaybillClaimBatchProposal:
        """Propose several direct Claims as one indivisible change set."""
        return handlers.handle_playbill_propose_claims(instance_id, authorings, proposal_name)

    @_tool
    def cruxible_playbill_authoring_create(
        instance_id: str,
        payload: dict[str, Any],
    ) -> contracts.PlaybillAuthoringIntentView:
        """Create or recover a daemon-owned authoring intent."""
        return handlers.handle_playbill_authoring_create(instance_id, payload)

    @_tool
    def cruxible_playbill_authoring_get(
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringIntentView:
        """Read one actor-scoped authoring intent."""
        return handlers.handle_playbill_authoring_get(instance_id, intent_id)

    @_tool
    def cruxible_playbill_authoring_resume(
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringIntentView:
        """Resume one durable authoring continuation."""
        return handlers.handle_playbill_authoring_resume(instance_id, intent_id)

    @_tool
    def cruxible_playbill_authoring_list_pending(
        instance_id: str,
    ) -> contracts.PlaybillAuthoringIntentList:
        """List the authenticated writer's pending intents."""
        return handlers.handle_playbill_authoring_list_pending(instance_id)

    @_tool
    def cruxible_playbill_authoring_compile(
        instance_id: str,
        payload: dict[str, Any],
        intent_id: str | None = None,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        """Create or update an intent and return its complete preflight."""
        return handlers.handle_playbill_authoring_compile(
            instance_id,
            payload,
            intent_id=intent_id,
        )

    @_tool
    def cruxible_playbill_authoring_preflight(
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        """Recompute one intent's complete binding preflight."""
        return handlers.handle_playbill_authoring_preflight(instance_id, intent_id)

    @_tool
    def cruxible_playbill_authoring_submit(
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringSubmitResult:
        """Idempotently submit one passing authoring intent."""
        return handlers.handle_playbill_authoring_submit(instance_id, intent_id)

    @_tool
    def cruxible_playbill_authoring_status(
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillCandidateStatus:
        """Read exactly what separates an intent from acceptance."""
        return handlers.handle_playbill_authoring_status(instance_id, intent_id)

    @_tool
    def cruxible_playbill_authoring_confirm_insertion(
        instance_id: str,
        intent_id: str,
        observation: dict[str, Any],
    ) -> contracts.PlaybillInsertionConfirmResult:
        """Confirm a client-applied insertion and govern its copy citation."""
        return handlers.handle_playbill_authoring_confirm_insertion(
            instance_id,
            intent_id,
            observation,
        )

    @_tool
    def cruxible_playbill_authoring_abandon_insertion(
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillInsertionAbandonResult:
        """Abandon a pending insertion while keeping the accepted self-source Claim."""
        return handlers.handle_playbill_authoring_abandon_insertion(instance_id, intent_id)

    @_tool
    def cruxible_playbill_list_claims(
        instance_id: str,
        subject_path: str | None = None,
        predicate: str | None = None,
        include_retired: bool = False,
    ) -> contracts.PlaybillClaimList:
        """List accepted Claims, optionally by Subject or predicate."""
        return handlers.handle_playbill_list_claims(
            instance_id,
            subject_path=subject_path,
            predicate=predicate,
            include_retired=include_retired,
        )

    @_tool
    def cruxible_playbill_get_claim(instance_id: str, identity: str) -> contracts.PlaybillClaimView:
        """Read one accepted Claim envelope and facts."""
        return handlers.handle_playbill_get_claim(instance_id, identity)

    @_tool
    def cruxible_playbill_claim_history(
        instance_id: str, identity: str
    ) -> contracts.PlaybillClaimHistory:
        """Read one Claim's accepted lineage."""
        return handlers.handle_playbill_claim_history(instance_id, identity)

    @_tool
    def cruxible_playbill_explain_claim(
        instance_id: str,
        identity: str,
        evaluation_time: str | None = None,
    ) -> contracts.PlaybillClaimExplanation:
        """Explain one Claim's verdict, law evidence, and sources."""
        return handlers.handle_playbill_explain_claim(
            instance_id, identity, evaluation_time=evaluation_time
        )

    @_tool
    def cruxible_playbill_propose_query_definition(
        instance_id: str,
        query: dict[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        """Propose one governed QueryDefinition entrypoint."""
        return handlers.handle_playbill_propose_query_definition(instance_id, query, proposal_name)

    @_tool
    def cruxible_playbill_list_query_definitions(
        instance_id: str,
    ) -> contracts.PlaybillQueryDefinitionList:
        """List accepted QueryDefinition entrypoints."""
        return handlers.handle_playbill_list_query_definitions(instance_id)

    @_tool
    def cruxible_playbill_get_query_definition(
        instance_id: str, name: str
    ) -> contracts.PlaybillQueryDefinitionView:
        """Read one accepted QueryDefinition and its contract."""
        return handlers.handle_playbill_get_query_definition(instance_id, name)

    @_tool
    def cruxible_playbill_run_query(
        instance_id: str,
        name: str,
        parameters: dict[str, Any] | None = None,
        evaluation_time: str | None = None,
        budgets: dict[str, Any] | None = None,
    ) -> contracts.PlaybillQueryRun:
        """Execute an accepted QueryDefinition and return its execution receipt."""
        return handlers.handle_playbill_run_query(
            instance_id,
            name,
            parameters=parameters,
            evaluation_time=evaluation_time,
            budgets=budgets,
        )

    @_tool
    def cruxible_playbill_discover(
        instance_id: str,
        query: str | None = None,
        entrypoint: str | None = None,
        evaluation_time: str | None = None,
        profile: Literal["interfaces", "subjects", "all"] = "interfaces",
        budget: dict[str, Any] | None = None,
    ) -> contracts.PlaybillDiscoveryResult:
        """Find accepted interfaces and Subjects by exact or lexical match."""
        return handlers.handle_playbill_discover(
            instance_id,
            query=query,
            entrypoint=entrypoint,
            evaluation_time=evaluation_time,
            profile=profile,
            budget=budget,
        )

    @_tool
    def cruxible_playbill_expand(
        instance_id: str,
        address: dict[str, Any],
        facets: list[str] | None = None,
        evaluation_time: str | None = None,
        budget: dict[str, Any] | None = None,
    ) -> contracts.PlaybillContextCapsule:
        """Expand one accepted address into a bounded context capsule."""
        return handlers.handle_playbill_expand(
            instance_id,
            address,
            evaluation_time=evaluation_time,
            facets=facets or [],
            budget=budget,
        )

    @_tool
    def cruxible_playbill_resolve_coverage(
        instance_id: str,
        observations: list[dict[str, Any]],
        budget: dict[str, Any] | None = None,
        scan_budget: dict[str, Any] | None = None,
    ) -> contracts.PlaybillCoverageResult:
        """Resolve what observed working sources have to do with accepted state."""
        return handlers.handle_playbill_resolve_coverage(
            instance_id,
            observations,
            budget=budget,
            scan_budget=scan_budget,
        )

    @_tool
    def cruxible_playbill_export_floor(
        instance_id: str,
    ) -> contracts.PlaybillFloorExport:
        """Export the deterministic greppable floor as base64 bytes per path."""
        return handlers.handle_playbill_export_floor(instance_id)

    return registered
