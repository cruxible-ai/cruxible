"""MCP registrations for the Playbill-only public surface."""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Annotated, Any, Callable, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from cruxible_client import contracts
from cruxible_client.authoring.inputs import AuthoringInputV1, ClaimInput
from cruxible_client.authoring.seed import SeedPlanResultV1
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendResultV1,
    PreparedClaimAttestationRequestV1,
)
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_core import __version__
from cruxible_core.mcp import handlers
from cruxible_core.mcp.tool_prompts import tool_description
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1
from cruxible_core.playbill.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    AUDIT_BUDGET_MAX_MAX_BYTES,
    AUDIT_BUDGET_MAX_MAX_ROWS,
    AUDIT_BUDGET_MIN_MAX_BYTES,
    AUDIT_BUDGET_MIN_MAX_ROWS,
)


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
        require_independent_approval: bool = False,
        seed: bool = True,
        git_object_format: Literal["sha1", "sha256"] | None = None,
    ) -> contracts.PlaybillInitResult:
        """Bootstrap Playbill from client-generated public principals.

        `seed=False` is the explicit opt-out, matching CLI `--no-seed` and the
        HTTP/SDK `seed` field: the instance is created, the seed step is skipped,
        and the result carries a typed `provider_seed` row with status
        `unseeded` naming the one repair that finishes it.
        """
        return handlers.handle_playbill_init(
            instance_id,
            principals,
            operating_profile,
            require_independent_approval,
            seed=seed,
            git_object_format=git_object_format,
        )

    @_tool
    def cruxible_playbill_instance_decommission(
        instance_id: str, reason: str
    ) -> contracts.PlaybillInstanceDecommissionResultV1:
        """End one instance's governed writes; reads keep serving and nothing is deleted."""
        return handlers.handle_playbill_instance_decommission(instance_id, reason)

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
    ) -> contracts.PlaybillWorkspaceActivationResult:
        """Settle by compare-and-set and refresh the configured client-owned floor."""
        return handlers.handle_playbill_activate(instance_id, proposal_id)

    @_tool
    def cruxible_playbill_whoami(instance_id: str) -> contracts.PlaybillWhoAmI:
        """Explain the credential-derived actor and its accepted registration."""
        return handlers.handle_playbill_whoami(instance_id)

    @_tool
    def cruxible_playbill_proposal_list(
        instance_id: str,
        status: Literal["open", "settled"] | None = None,
    ) -> contracts.PlaybillProposalList:
        """List open or settled proposal evidence at the current coordinate."""
        return handlers.handle_playbill_list_proposals(instance_id, status)

    @_tool
    def cruxible_playbill_proposal_readmit(
        instance_id: str,
        proposal_id: str,
    ) -> contracts.PlaybillProposalReadmitResult:
        """Re-admit one stale proposal against the current accepted coordinate."""
        return handlers.handle_playbill_readmit_proposal(instance_id, proposal_id)

    @_tool
    def cruxible_playbill_proposal_withdraw(
        instance_id: str,
        proposal_id: str,
        reason: str,
    ) -> contracts.PlaybillProposalWithdrawResult:
        """Retire one open proposal that will never be activated."""
        return handlers.handle_playbill_withdraw_proposal(instance_id, proposal_id, reason)

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
        """Read one accepted Subject envelope, facts, and incoming relations."""
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
        input: ClaimTypeInputV1,
        proposal_name: str,
    ) -> contracts.PlaybillClaimTypeInputProposalResult:
        """Propose one governed ClaimType interface."""
        return handlers.handle_playbill_propose_claim_type(
            instance_id, input.model_dump(mode="json"), proposal_name
        )

    @_tool
    def cruxible_playbill_claim_type_migrate(
        instance_id: str,
        request: dict[str, Any],
    ) -> contracts.PlaybillClaimTypeMigrationResponse:
        """Propose one ClaimType successor and its dependent dispositions atomically."""
        return handlers.handle_playbill_migrate_claim_type(instance_id, request)

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
    def cruxible_playbill_claim_retire(
        instance_id: str,
        claim_id: str,
        request: dict[str, Any],
    ) -> contracts.PlaybillClaimRetireResponse:
        """Preflight or submit one attributed Claim retirement closure."""
        return handlers.handle_playbill_retire_claim(instance_id, claim_id, request)

    @_tool
    def cruxible_playbill_claim_attest(
        instance_id: str,
        claim_id: str,
        stance: Literal["support", "contradict", "unsure"],
        note: str | None = None,
    ) -> ClaimAttestationAppendResultV1:
        """Sign that the caller examined this exact Claim, then append the evidence."""

        return handlers.handle_playbill_claim_attest(instance_id, claim_id, stance, note)

    @_tool
    def cruxible_playbill_claim_attest_new_capture(
        instance_id: str,
        request: PreparedClaimAttestationRequestV1,
    ) -> ClaimAttestationAppendResultV1:
        """Sign and append a structured new-Capture Claim observation."""

        return handlers.handle_playbill_claim_attest_new_capture(instance_id, request)

    @_tool
    def cruxible_playbill_authoring_create(
        instance_id: str,
        payload: AuthoringInputV1,
    ) -> contracts.PlaybillAuthoringIntentView:
        """Create or recover a daemon-owned authoring intent."""
        return handlers.handle_playbill_authoring_create(
            instance_id, payload.model_dump(mode="json")
        )

    @_tool
    def cruxible_playbill_authoring_example(
        name: contracts.PlaybillAuthoringExampleName,
        claim_id: str | None = None,
        capture_digest: str | None = None,
    ) -> contracts.PlaybillAuthoringExampleResult:
        """Return one model-constructed input template with no daemon call."""
        return handlers.handle_playbill_authoring_example(
            name,
            claim_id=claim_id,
            capture_digest=capture_digest,
        )

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
        payload: AuthoringInputV1,
        intent_id: str | None = None,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        """Create or update an intent and return its complete preflight."""
        return handlers.handle_playbill_authoring_compile(
            instance_id,
            payload.model_dump(mode="json"),
            intent_id=intent_id,
        )

    @_tool
    def cruxible_playbill_authoring_bind(
        instance_id: str,
        source_path: str,
        anchor: str,
        payload: ClaimInput,
        window_lines: int | None = None,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        """Bind one exact workspace anchor and compile the derived Flow-A observation."""
        return handlers.handle_playbill_authoring_bind(
            instance_id,
            source_path=source_path,
            anchor=anchor,
            payload=payload,
            window_lines=window_lines,
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
        expectation_id: str | None = None,
    ) -> contracts.PlaybillInsertionConfirmResultV2:
        """Confirm an exact stamped publication."""
        return handlers.handle_playbill_authoring_confirm_insertion(
            instance_id,
            intent_id,
            observation,
            expectation_id,
        )

    @_tool
    def cruxible_playbill_authoring_prepare_publication(
        instance_id: str,
        intent_id: str,
        observation: dict[str, Any],
        expectation_id: str | None = None,
    ) -> contracts.PlaybillInsertionPrepareResult:
        """Prepare an exact Claim-backed publication from fresh whole-source bytes."""
        return handlers.handle_playbill_authoring_prepare_publication(
            instance_id,
            intent_id,
            observation,
            expectation_id,
        )

    @_tool
    def cruxible_playbill_authoring_abandon_insertion(
        instance_id: str,
        intent_id: str,
        expectation_id: str | None = None,
    ) -> contracts.PlaybillInsertionAbandonResult:
        """Abandon a pending insertion while keeping the accepted self-source Claim."""
        return handlers.handle_playbill_authoring_abandon_insertion(
            instance_id,
            intent_id,
            expectation_id,
        )

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
    def cruxible_playbill_get_claim(
        instance_id: str,
        identity: str,
        evaluation_time: str | None = None,
    ) -> contracts.PlaybillClaimViewV2:
        """Read one accepted Claim with its capture-admission accounts."""
        return handlers.handle_playbill_get_claim(
            instance_id,
            identity,
            evaluation_time=evaluation_time,
        )

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
    ) -> contracts.PlaybillClaimExplanationV2 | contracts.PlaybillClaimExplanationV3:
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
    def cruxible_playbill_policies_in_force(
        instance_id: str,
    ) -> contracts.PlaybillPolicyInForceList:
        """List live governed policies embedded in and standing beside accepted artifacts."""
        return handlers.handle_playbill_policies_in_force(instance_id)

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
    def cruxible_playbill_procedure_readiness(
        instance_id: str,
        name: str,
        evaluation_time: str,
    ) -> contracts.PlaybillProcedureReadiness:
        """Inspect one accepted Procedure's bindings and executable profile."""
        return handlers.handle_playbill_procedure_readiness(
            instance_id,
            name,
            evaluation_time=evaluation_time,
        )

    @_tool
    def cruxible_playbill_procedure_bind(
        instance_id: str,
        name: str,
        bindings: list[dict[str, Any]],
    ) -> contracts.PlaybillProcedureBindResult:
        """Propose exact accepted bindings for one Procedure's open slots."""
        return handlers.handle_playbill_procedure_bind(
            instance_id,
            name,
            bindings=bindings,
        )

    @_tool
    def cruxible_playbill_procedure_run(
        instance_id: str,
        name: str,
        input: Any,
        evaluation_time: str | None = None,
        at: dict[str, Any] | None = None,
    ) -> contracts.PlaybillProcedureRunState:
        """Run one accepted query-only Procedure deterministically."""
        return handlers.handle_playbill_procedure_run(
            instance_id,
            name,
            evaluation_time=evaluation_time,
            at=at,
            input=input,
        )

    @_tool
    def cruxible_playbill_procedure_run_status(
        instance_id: str,
        run_id: str,
    ) -> contracts.PlaybillProcedureRunState:
        """Read one durable Procedure run state and its exact next operation."""
        return handlers.handle_playbill_procedure_run_status(instance_id, run_id)

    @_tool
    def cruxible_playbill_line_run(
        instance_id: str,
        line_identity_digest: str,
        evaluation_time: str | None = None,
        occurrence_id: str | None = None,
    ) -> contracts.PlaybillProcedureRunState:
        """Trigger one due occurrence of an accepted Line."""
        return handlers.handle_playbill_line_run(
            instance_id,
            line_identity_digest,
            occurrence_id=occurrence_id,
            evaluation_time=evaluation_time,
        )

    @_tool
    def cruxible_playbill_predict(
        instance_id: str,
        request: contracts.PlaybillPredictRequestV1,
    ) -> contracts.PlaybillPredictResultV1:
        """Propose a predicted Claim with an exact settlement rule and deadline."""
        return handlers.handle_playbill_predict(instance_id, request)

    @_tool
    def cruxible_playbill_settle(
        instance_id: str,
        prediction_id: str,
        request: contracts.PlaybillSettleRequestV1,
    ) -> contracts.PlaybillSettleResultV1:
        """Settle one prediction from an accepted observation or mandated terminal."""
        return handlers.handle_playbill_settle_prediction(instance_id, prediction_id, request)

    @_tool
    def cruxible_playbill_discover(
        instance_id: str,
        query: str | None = None,
        entrypoint: str | None = None,
        evaluation_time: str | None = None,
        profile: Literal["interfaces", "subjects", "all"] = "interfaces",
        budget: dict[str, Any] | None = None,
    ) -> contracts.PlaybillDiscoveryResult | contracts.PlaybillInterfaceInventory:
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
    def cruxible_playbill_search(
        instance_id: str,
        mode: Literal["search", "list", "orient"],
        query: str | None = None,
        kinds: list[Literal["claim", "procedure", "demand"]] | None = None,
        subject: dict[str, Any] | None = None,
        statuses: list[Literal["accepted", "conflicted", "overturned", "refused", "retired"]]
        | None = None,
        cursor: dict[str, Any] | None = None,
        evaluation_time: str | None = None,
        budgets: dict[str, Any] | None = None,
    ) -> contracts.PlaybillSearchResult:
        """Search, list, or orient over accepted Claims and Procedures."""
        return handlers.handle_playbill_search(
            instance_id,
            mode=mode,
            query=query,
            kinds=kinds,
            subject=subject,
            statuses=statuses,
            cursor=cursor,
            evaluation_time=evaluation_time,
            budgets=budgets,
        )

    @_tool
    def cruxible_playbill_since(
        instance_id: str,
        generation: int,
        at: dict[str, Any] | None = None,
        access_profile: dict[str, Any] | None = None,
        max_rows: Annotated[int, Field(ge=1, le=1000)] = 100,
        max_bytes: Annotated[int, Field(ge=1, le=1_048_576)] = 65_536,
        cursor: dict[str, Any] | None = None,
    ) -> contracts.PlaybillSinceResult:
        """Read accepted ChangeSet members after one generation."""
        return handlers.handle_playbill_since(
            instance_id,
            generation=generation,
            at=at,
            access_profile=access_profile,
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        )

    @_tool
    def cruxible_playbill_curation_list(
        instance_id: str,
        evaluation_time: str,
        access_profile: dict[str, Any] | None = None,
        workspace_observation: dict[str, Any] | None = None,
    ) -> contracts.PlaybillCurationListResult:
        """List mechanical curation patterns and explicitly ingest block observations."""
        return handlers.handle_playbill_curation_list(
            instance_id,
            evaluation_time=evaluation_time,
            access_profile=access_profile,
            workspace_observation=workspace_observation,
        )

    @_tool
    def cruxible_playbill_audit(
        instance_id: str,
        evaluation_time: str,
        access_profile: dict[str, Any] | None = None,
        claim_type_identities: list[str] | None = None,
        subject_kinds: list[str] | None = None,
        max_rows: Annotated[
            int,
            Field(ge=AUDIT_BUDGET_MIN_MAX_ROWS, le=AUDIT_BUDGET_MAX_MAX_ROWS),
        ] = AUDIT_BUDGET_DEFAULT_MAX_ROWS,
        max_bytes: Annotated[
            int,
            Field(ge=AUDIT_BUDGET_MIN_MAX_BYTES, le=AUDIT_BUDGET_MAX_MAX_BYTES),
        ] = AUDIT_BUDGET_DEFAULT_MAX_BYTES,
        cursor: dict[str, Any] | None = None,
    ) -> contracts.PlaybillAuditResult:
        """Read ranked Claim verification work and record completed coverage."""
        return handlers.handle_playbill_audit(
            instance_id,
            evaluation_time=evaluation_time,
            access_profile=access_profile,
            claim_type_identities=claim_type_identities or [],
            subject_kinds=subject_kinds or [],
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        )

    @_tool
    def cruxible_playbill_curation_overrule(
        instance_id: str,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        attribution_refs: list[str] | None = None,
    ) -> contracts.PlaybillCurationActionResult:
        """Resolve one detector-version item as mechanically inapplicable."""
        return handlers.handle_playbill_curation_overrule(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            attribution_refs=attribution_refs or [],
        )

    @_tool
    def cruxible_playbill_curation_accept_fixed(
        instance_id: str,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        accepted_proposal_id: str,
        accepted_changeset_digest: str,
        attribution_refs: list[str] | None = None,
    ) -> contracts.PlaybillCurationActionResult:
        """Link one curation item to its exact accepted resolving ChangeSet."""
        return handlers.handle_playbill_curation_accept_fixed(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=accepted_proposal_id,
            accepted_changeset_digest=accepted_changeset_digest,
            attribution_refs=attribution_refs or [],
        )

    @_tool
    def cruxible_playbill_curation_suppress(
        instance_id: str,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        scope: Literal["item", "pattern", "instance"],
        until_generation: int | None = None,
        attribution_refs: list[str] | None = None,
    ) -> contracts.PlaybillCurationActionResult:
        """Hide curation work temporarily without resolving its detector facts."""
        return handlers.handle_playbill_curation_suppress(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=scope,
            until_generation=until_generation,
            attribution_refs=attribution_refs or [],
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
    def cruxible_playbill_workspace_source_compile(
        instance_id: str,
        catalog_path: str,
        repository_root: str = ".",
        local_catalog_path: str | None = None,
        root_aliases: dict[str, str] | None = None,
    ) -> SourceCompilationBundle:
        """Compile declared workspace sources against accepted daemon context."""
        return handlers.handle_playbill_workspace_source_compile(
            instance_id,
            catalog_path=catalog_path,
            repository_root=repository_root,
            local_catalog_path=local_catalog_path,
            root_aliases=root_aliases or {},
        )

    @_tool
    def cruxible_playbill_workspace_source_check(
        instance_id: str,
        catalog_path: str,
        repository_root: str = ".",
        local_catalog_path: str | None = None,
        root_aliases: dict[str, str] | None = None,
    ) -> contracts.PlaybillSourceCheckResult:
        """Compile workspace sources and report their alignment to accepted state."""
        return handlers.handle_playbill_workspace_source_check(
            instance_id,
            catalog_path=catalog_path,
            repository_root=repository_root,
            local_catalog_path=local_catalog_path,
            root_aliases=root_aliases or {},
        )

    @_tool
    def cruxible_playbill_workspace_coverage_resolve(
        instance_id: str,
        bindings: dict[str, str],
        files: list[str] | None = None,
        ranges: list[str] | None = None,
        grep_results_path: str | None = None,
        whole_working_set: bool = False,
        budget: dict[str, Any] | None = None,
        scan_budget: dict[str, Any] | None = None,
    ) -> contracts.PlaybillCoverageResult:
        """Resolve selected workspace files while the adapter derives observations."""
        return handlers.handle_playbill_workspace_coverage_resolve(
            instance_id,
            bindings=bindings,
            files=tuple(files or ()),
            ranges=tuple(ranges or ()),
            grep_results_path=grep_results_path,
            whole_working_set=whole_working_set,
            budget=budget,
            scan_budget=scan_budget,
        )

    @_tool
    def cruxible_playbill_workspace_coverage_status(
        instance_id: str,
        bindings: dict[str, str],
        budget: dict[str, Any] | None = None,
        scan_budget: dict[str, Any] | None = None,
    ) -> contracts.PlaybillCoverageResult:
        """Resolve the complete declared workspace scope as one coverage status."""
        return handlers.handle_playbill_workspace_coverage_status(
            instance_id,
            bindings=bindings,
            budget=budget,
            scan_budget=scan_budget,
        )

    @_tool
    def cruxible_playbill_seed_plan(
        bundle_path: str,
        proposal_name: str,
    ) -> SeedPlanResultV1:
        """Plan a workspace seed bundle offline without opening a proposal."""
        return handlers.handle_playbill_seed_plan(
            bundle_path=bundle_path,
            proposal_name=proposal_name,
        )

    @_tool
    def cruxible_playbill_export_floor(
        instance_id: str,
    ) -> contracts.PlaybillFloorExport:
        """Export the deterministic greppable floor as base64 bytes per path."""
        return handlers.handle_playbill_export_floor(instance_id)

    @_tool
    def cruxible_playbill_workspace_floor_export(
        instance_id: str,
        force: bool = False,
    ) -> contracts.PlaybillWorkspaceFloorWriteResult:
        """Verify and write the floor under the configured MCP workspace."""
        return handlers.handle_playbill_workspace_floor_export(
            instance_id,
            force=force,
        )

    @_tool
    def cruxible_playbill_workspace_floor_status(
        instance_id: str,
    ) -> contracts.PlaybillWorkspaceFloorStatus:
        """Report whether the configured local floor is current, stale, or missing."""
        return handlers.handle_playbill_workspace_floor_status(instance_id)

    return registered
