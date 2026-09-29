"""MCP registrations for the Playbill-only public surface."""

from __future__ import annotations

import asyncio
from datetime import datetime
from functools import wraps
from typing import Annotated, Any, Callable, Literal, cast

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from cruxible_client import contracts
from cruxible_client.authoring.inputs import AuthoringInputV1, ClaimInput
from cruxible_client.contracts.capture_reads import CaptureReadRequestV1, CaptureReadV1
from cruxible_client.contracts.claim_attestations import ClaimAttestationAppendResultV1
from cruxible_client.contracts.claim_reads import ClaimValuesResultV1
from cruxible_client.contracts.evidence_rule_upgrade import EvidenceRuleUpgradeResultV1
from cruxible_client.contracts.kits import (
    PlaybillKitAddRequestV1,
    PlaybillKitBuildRequestV1,
    PlaybillKitBuildResultV1,
    PlaybillKitChangeResultV1,
    PlaybillKitRemoveRequestV1,
    PlaybillKitStatusV1,
)
from cruxible_client.contracts.provider_installation import (
    PlaybillProviderCatalogV1,
    PlaybillProviderInstallRequestV1,
    PlaybillProviderInstallResultV1,
)
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputV1
from cruxible_core.curation.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    AUDIT_BUDGET_MAX_MAX_BYTES,
    AUDIT_BUDGET_MAX_MAX_ROWS,
    AUDIT_BUDGET_MIN_MAX_BYTES,
    AUDIT_BUDGET_MIN_MAX_ROWS,
)
from cruxible_core.mcp import handlers
from cruxible_core.mcp.results import McpServerInfoResult, McpWhoAmIResult
from cruxible_core.mcp.target import MCP_INSTANCE_ENV, require_instance_id
from cruxible_core.mcp.tool_prompts import tool_description

InstanceId = Annotated[
    str | None,
    Field(description=f"Instance to act on; defaults to the server's {MCP_INSTANCE_ENV}."),
]


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
    def cruxible_server_info() -> McpServerInfoResult:
        """Return daemon metadata, or the credential's instance when it is instance-scoped."""
        return handlers.handle_server_info()

    @_tool
    def cruxible_playbill_provider_catalog(
        instance_id: InstanceId = None,
    ) -> PlaybillProviderCatalogV1:
        """Discover provider packages available from the configured repository."""
        return handlers.handle_playbill_provider_catalog(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_provider_install(
        instance_id: InstanceId = None,
        *,
        request: PlaybillProviderInstallRequestV1,
    ) -> PlaybillProviderInstallResultV1:
        """Install a provider package and propose its definitions; requires ADMIN."""
        return handlers.handle_playbill_provider_install(require_instance_id(instance_id), request)

    @_tool
    def cruxible_playbill_kit_build(
        instance_id: InstanceId = None,
        *,
        request: PlaybillKitBuildRequestV1,
    ) -> PlaybillKitBuildResultV1:
        """Export this instance's definitions under the owned prefixes as one kit release."""
        return handlers.handle_playbill_kit_build(require_instance_id(instance_id), request)

    @_tool
    def cruxible_playbill_kit_status(instance_id: InstanceId = None) -> PlaybillKitStatusV1:
        """List installed kits and the kit paths edited since install."""
        return handlers.handle_playbill_kit_status(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_kit_add(
        instance_id: InstanceId = None,
        *,
        request: PlaybillKitAddRequestV1,
    ) -> PlaybillKitChangeResultV1:
        """Propose installing or upgrading a kit as one change set; activation is separate."""
        return handlers.handle_playbill_kit_add(require_instance_id(instance_id), request)

    @_tool
    def cruxible_playbill_evidence_rules_upgrade(
        instance_id: InstanceId = None,
    ) -> EvidenceRuleUpgradeResultV1:
        """Propose moving live ClaimTypes to evidence rules that name contracts by identity."""
        return handlers.handle_playbill_evidence_rules_upgrade(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_kit_remove(
        instance_id: InstanceId = None,
        *,
        request: PlaybillKitRemoveRequestV1,
    ) -> PlaybillKitChangeResultV1:
        """Propose retiring every artifact a kit installed; activation is separate."""
        return handlers.handle_playbill_kit_remove(require_instance_id(instance_id), request)

    @_tool
    def cruxible_playbill_init(
        instance_id: InstanceId = None,
        *,
        principals: list[dict[str, Any]],
        operating_profile: Literal["local", "cloud"] = "local",
        require_independent_approval: bool = False,
        git_object_format: Literal["sha1", "sha256"] | None = None,
    ) -> contracts.PlaybillInitResult:
        """Bootstrap Playbill from client-generated public principals.

        Provider installation is a separate administrative operation.
        """
        return handlers.handle_playbill_init(
            require_instance_id(instance_id),
            principals,
            operating_profile,
            require_independent_approval,
            git_object_format=git_object_format,
        )

    @_tool
    def cruxible_playbill_store_body(
        instance_id: InstanceId = None, *, content_base64: str
    ) -> contracts.PlaybillCasObjectResult:
        """Store inert exact body bytes."""
        return handlers.handle_playbill_store_body(require_instance_id(instance_id), content_base64)

    @_tool
    def cruxible_playbill_propose_document(
        instance_id: InstanceId = None,
        *,
        shell: dict[str, Any],
        proposal_name: str,
        source_compilation_digest: str | None = None,
    ) -> contracts.PlaybillProposalInspection:
        """Propose a governed Document create or supersession."""
        return handlers.handle_playbill_propose_document(
            require_instance_id(instance_id), shell, proposal_name, source_compilation_digest
        )

    @_tool
    def cruxible_playbill_inspect_proposal(
        instance_id: InstanceId = None, *, proposal_id: str
    ) -> contracts.PlaybillProposalInspection:
        """Inspect immutable proposal evidence."""
        return handlers.handle_playbill_inspect_proposal(
            require_instance_id(instance_id), proposal_id
        )

    @_tool
    def cruxible_playbill_inspect_refusal(
        instance_id: InstanceId = None, *, proposal_id: str
    ) -> contracts.PlaybillRefusalInspection:
        """Inspect typed admission and law diagnostics."""
        return handlers.handle_playbill_inspect_refusal(
            require_instance_id(instance_id), proposal_id
        )

    @_tool
    def cruxible_playbill_review(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        include_body: bool = False,
    ) -> contracts.PlaybillProposalReview:
        """Render a structured candidate review."""
        return handlers.handle_playbill_review(
            require_instance_id(instance_id), proposal_id, include_body=include_body
        )

    @_tool
    def cruxible_playbill_prepare_approval(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        signer_id: str,
        include_body: bool = False,
    ) -> contracts.PlaybillApprovalChallenge:
        """Fetch the exact statement for a client-held signer."""
        return handlers.handle_playbill_prepare_approval(
            require_instance_id(instance_id),
            proposal_id,
            signer_id=signer_id,
            include_body=include_body,
        )

    @_tool
    def cruxible_playbill_submit_approval(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        attestation: dict[str, Any],
    ) -> contracts.PlaybillApprovalReceipt:
        """Submit a public approval attestation."""
        return handlers.handle_playbill_submit_approval(
            require_instance_id(instance_id), proposal_id, attestation
        )

    @_tool
    def cruxible_playbill_approve(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        signer_id: Annotated[
            str | None,
            Field(
                description=(
                    "Principal to sign as; its key is <signer_id>.ed25519 in the server's "
                    "CRUXIBLE_MCP_KEY_DIR. Omit when that directory holds exactly one key."
                )
            ),
        ] = None,
        candidate_digest: Annotated[
            str | None,
            Field(
                description=(
                    "The candidate_digest you reviewed; the approval refuses if the "
                    "proposal now signs a different candidate."
                )
            ),
        ] = None,
    ) -> contracts.PlaybillApprovalReceipt:
        """Approve a proposal with a local key: challenge, sign and submit in one call."""
        return handlers.handle_playbill_approve(
            require_instance_id(instance_id),
            proposal_id,
            signer_id=signer_id,
            candidate_digest=candidate_digest,
        )

    @_tool
    def cruxible_playbill_activate(
        instance_id: InstanceId = None, *, proposal_id: str
    ) -> contracts.PlaybillWorkspaceActivationResult:
        """Settle by compare-and-set and refresh the configured client-owned floor."""
        return handlers.handle_playbill_activate(require_instance_id(instance_id), proposal_id)

    @_tool
    def cruxible_playbill_whoami(instance_id: InstanceId = None) -> McpWhoAmIResult:
        """Name the resolved instance, the credential-derived actor, and its registration."""
        return handlers.handle_playbill_whoami(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_proposal_list(
        instance_id: InstanceId = None,
        *,
        status: Literal["open", "settled", "incomplete"] | None = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.PLAYBILL_PROPOSAL_LIST_MAX_LIMIT)
        ] = contracts.PLAYBILL_PROPOSAL_LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> contracts.PlaybillProposalList:
        """List one page of proposal evidence; pass next_cursor back while truncated."""
        return handlers.handle_playbill_list_proposals(
            require_instance_id(instance_id), status, limit=limit, cursor=cursor
        )

    @_tool
    def cruxible_playbill_proposal_readmit(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
    ) -> contracts.PlaybillProposalReadmitResult:
        """Re-admit one stale proposal against the current accepted coordinate."""
        return handlers.handle_playbill_readmit_proposal(
            require_instance_id(instance_id), proposal_id
        )

    @_tool
    def cruxible_playbill_proposal_withdraw(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        reason: str,
    ) -> contracts.PlaybillProposalWithdrawResult:
        """Retire one open proposal that will never be activated."""
        return handlers.handle_playbill_withdraw_proposal(
            require_instance_id(instance_id), proposal_id, reason
        )

    @_tool
    def cruxible_playbill_list_documents(
        instance_id: InstanceId = None,
    ) -> contracts.PlaybillDocumentList:
        """List accepted Documents at the current coordinate."""
        return handlers.handle_playbill_list_documents(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_get_document(
        instance_id: InstanceId = None, *, identity: str
    ) -> contracts.PlaybillDocumentView:
        """Read one accepted Document envelope and facts."""
        return handlers.handle_playbill_get_document(require_instance_id(instance_id), identity)

    @_tool
    def cruxible_playbill_read_capture(
        instance_id: InstanceId = None, *, request: CaptureReadRequestV1
    ) -> CaptureReadV1:
        """Read exact retained Capture evidence with a byte budget and body permission."""
        return handlers.handle_playbill_read_capture(require_instance_id(instance_id), request)

    @_tool
    def cruxible_playbill_dereference(
        instance_id: InstanceId = None, *, identity: str
    ) -> contracts.PlaybillBodyRead:
        """Dereference verified accepted body bytes."""
        return handlers.handle_playbill_dereference(require_instance_id(instance_id), identity)

    @_tool
    def cruxible_playbill_history(
        instance_id: InstanceId = None, *, identity: str
    ) -> contracts.PlaybillDocumentHistory:
        """Read one Document's replay-verified history."""
        return handlers.handle_playbill_history(require_instance_id(instance_id), identity)

    @_tool
    def cruxible_playbill_explain(
        instance_id: InstanceId = None,
        *,
        subject: dict[str, Any],
        at: dict[str, Any],
        detail: Literal["summary", "evidence", "proof"] = "summary",
        include_body: bool = False,
    ) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail:
        """Explain governance and provenance at an exact coordinate."""
        return handlers.handle_playbill_explain(
            require_instance_id(instance_id),
            subject,
            at,
            detail=detail,
            include_body=include_body,
        )

    @_tool
    def cruxible_playbill_source_context(
        instance_id: InstanceId = None,
    ) -> contracts.PlaybillSourceContext:
        """Fetch path-free inputs for local source compilation."""
        return handlers.handle_playbill_source_context(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_source_check(
        instance_id: InstanceId = None,
        *,
        bundle: Annotated[
            dict[str, Any] | None,
            Field(description="A compiled source bundle; omit when passing catalog_path."),
        ] = None,
        catalog_path: Annotated[
            str | None,
            Field(description="Workspace source catalog to compile first; omit with bundle."),
        ] = None,
        repository_root: str = ".",
        local_catalog_path: str | None = None,
        root_aliases: dict[str, str] | None = None,
    ) -> contracts.PlaybillSourceCheckResult:
        """Compare a compiled bundle or catalog-declared workspace sources with accepted state."""
        return handlers.handle_playbill_source_check(
            require_instance_id(instance_id),
            bundle=bundle,
            catalog_path=catalog_path,
            repository_root=repository_root,
            local_catalog_path=local_catalog_path,
            root_aliases=root_aliases,
        )

    @_tool
    def cruxible_playbill_propose_source_bundle(
        instance_id: InstanceId = None,
        *,
        bundle: dict[str, Any],
        source_name: str,
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        """Propose frozen source bytes without a client path."""
        return handlers.handle_playbill_propose_source_bundle(
            require_instance_id(instance_id),
            bundle,
            source_name=source_name,
            proposal_name=proposal_name,
        )

    @_tool
    def cruxible_playbill_list_principals(
        instance_id: InstanceId = None,
    ) -> contracts.PlaybillPrincipalList:
        """List accepted public principal records."""
        return handlers.handle_playbill_list_principals(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_compiler_upgrade(
        instance_id: InstanceId = None,
        *,
        target_compiler_digest: str,
        base: dict[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        """Propose an admin-only compiler upgrade; review, approve and activate separately."""
        return handlers.handle_playbill_compiler_upgrade(
            require_instance_id(instance_id),
            target_compiler_digest,
            base,
            proposal_name,
        )

    @_tool
    def cruxible_playbill_propose_principal_change(
        instance_id: InstanceId = None,
        *,
        principal: dict[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        """Propose principal registration, rotation, revocation, or recovery."""
        return handlers.handle_playbill_propose_principal_change(
            require_instance_id(instance_id), principal, proposal_name
        )

    @_tool
    def cruxible_playbill_list_subjects(
        instance_id: InstanceId = None,
        subject_kind: str | None = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.PLAYBILL_SUBJECT_LIST_MAX_LIMIT)
        ] = contracts.PLAYBILL_SUBJECT_LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> contracts.PlaybillSubjectList:
        """One page of Subjects (kind, id, live Claim count); follow next_cursor while truncated."""
        return handlers.handle_playbill_list_subjects(
            require_instance_id(instance_id), subject_kind=subject_kind, limit=limit, cursor=cursor
        )

    @_tool
    def cruxible_playbill_get_subject(
        instance_id: InstanceId = None, *, subject_kind: str, subject_id: str
    ) -> contracts.PlaybillSubjectView:
        """Read one accepted Subject envelope, facts, and incoming relations."""
        return handlers.handle_playbill_get_subject(
            require_instance_id(instance_id), subject_kind, subject_id
        )

    @_tool
    def cruxible_playbill_subject_history(
        instance_id: InstanceId = None, *, subject_kind: str, subject_id: str
    ) -> contracts.PlaybillSubjectHistory:
        """Read one Subject's accepted lineage."""
        return handlers.handle_playbill_subject_history(
            require_instance_id(instance_id), subject_kind, subject_id
        )

    @_tool
    def cruxible_playbill_propose_claim_type(
        instance_id: InstanceId = None,
        *,
        input: ClaimTypeInputV1,
        proposal_name: str,
    ) -> contracts.PlaybillClaimTypeInputProposalResult:
        """Propose one governed ClaimType interface."""
        return handlers.handle_playbill_propose_claim_type(
            require_instance_id(instance_id), input.model_dump(mode="json"), proposal_name
        )

    @_tool
    def cruxible_playbill_claim_type_migrate(
        instance_id: InstanceId = None,
        *,
        request: dict[str, Any],
    ) -> contracts.PlaybillClaimTypeMigrationResponse:
        """Propose one ClaimType successor and its dependent dispositions atomically."""
        return handlers.handle_playbill_migrate_claim_type(
            require_instance_id(instance_id), request
        )

    @_tool
    def cruxible_playbill_list_claim_types(
        instance_id: InstanceId = None,
    ) -> contracts.PlaybillClaimTypeList:
        """List accepted ClaimType interfaces."""
        return handlers.handle_playbill_list_claim_types(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_get_claim_type(
        instance_id: InstanceId = None, *, predicate: str
    ) -> contracts.PlaybillClaimTypeView:
        """Read one accepted ClaimType by predicate."""
        return handlers.handle_playbill_get_claim_type(require_instance_id(instance_id), predicate)

    @_tool
    def cruxible_playbill_claim_retire(
        instance_id: InstanceId = None,
        *,
        claim_id: str,
        request: dict[str, Any],
    ) -> contracts.PlaybillClaimRetireResponse:
        """Preflight or submit one attributed Claim retirement closure."""
        return handlers.handle_playbill_retire_claim(
            require_instance_id(instance_id), claim_id, request
        )

    @_tool
    def cruxible_playbill_claim_attest(
        instance_id: InstanceId = None,
        *,
        claim_id: str,
        stance: Literal["support", "contradict", "unsure"],
        note: str | None = None,
        valid_until: datetime | None = None,
        capture_digests: Annotated[
            list[str] | None,
            Field(
                description=(
                    "New Captures you examined; attests on that new evidence instead of "
                    "the Claim's own citations."
                )
            ),
        ] = None,
        referent_coordinate: Annotated[
            dict[str, Any] | None,
            Field(description="With capture_digests: the accepted coordinate you read."),
        ] = None,
        attested_at: Annotated[
            datetime | None,
            Field(description="With capture_digests: when you observed the new Capture."),
        ] = None,
    ) -> ClaimAttestationAppendResultV1:
        """Sign that the caller examined this exact Claim, then append the evidence.

        ``unsure`` holds the Claim's contested ``next`` rows until what was examined
        changes; a hold on stale or uncovered evidence lapses at ``valid_until``,
        else after the ClaimType's ``unsure_hold_for`` (default 30 days).
        """

        return handlers.handle_playbill_claim_attest(
            require_instance_id(instance_id),
            claim_id,
            stance,
            note,
            valid_until,
            capture_digests=capture_digests,
            referent_coordinate=referent_coordinate,
            attested_at=attested_at,
        )

    @_tool
    def cruxible_playbill_authoring_create(
        instance_id: InstanceId = None,
        *,
        payload: AuthoringInputV1,
    ) -> contracts.PlaybillAuthoringIntentView:
        """Create or recover a daemon-owned authoring intent."""
        return handlers.handle_playbill_authoring_create(
            require_instance_id(instance_id), payload.model_dump(mode="json")
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
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringIntentView:
        """Read one actor-scoped authoring intent."""
        return handlers.handle_playbill_authoring_get(require_instance_id(instance_id), intent_id)

    @_tool
    def cruxible_playbill_authoring_resume(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringIntentView:
        """Resume one durable authoring continuation."""
        return handlers.handle_playbill_authoring_resume(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_playbill_authoring_list_pending(
        instance_id: InstanceId = None,
    ) -> contracts.PlaybillAuthoringIntentList:
        """List the authenticated writer's pending intents."""
        return handlers.handle_playbill_authoring_list_pending(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_authoring_compile(
        instance_id: InstanceId = None,
        *,
        payload: AuthoringInputV1,
        intent_id: str | None = None,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        """Create or update an intent and return its complete preflight."""
        return handlers.handle_playbill_authoring_compile(
            require_instance_id(instance_id),
            payload.model_dump(mode="json"),
            intent_id=intent_id,
        )

    @_tool
    def cruxible_playbill_authoring_bind(
        instance_id: InstanceId = None,
        *,
        source_path: str,
        anchor: str,
        payload: ClaimInput,
        window_lines: int | None = None,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        """Bind one exact workspace anchor and compile the derived Flow-A observation."""
        return handlers.handle_playbill_authoring_bind(
            require_instance_id(instance_id),
            source_path=source_path,
            anchor=anchor,
            payload=payload,
            window_lines=window_lines,
        )

    @_tool
    def cruxible_playbill_authoring_preflight(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        """Recompute one intent's complete binding preflight."""
        return handlers.handle_playbill_authoring_preflight(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_playbill_authoring_rebase(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringIntentView:
        """Rebase one stale authoring intent onto the current accepted coordinate."""
        return handlers.handle_playbill_authoring_rebase(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_playbill_authoring_submit(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringSubmitResult:
        """Idempotently submit one passing authoring intent."""
        return handlers.handle_playbill_authoring_submit(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_playbill_authoring_status(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.PlaybillCandidateStatus:
        """Read exactly what separates an intent from acceptance."""
        return handlers.handle_playbill_authoring_status(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_playbill_authoring_abandon_insertion(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
        expectation_id: str | None = None,
    ) -> contracts.PlaybillInsertionAbandonResult:
        """Abandon a pending insertion while keeping the accepted self-source Claim."""
        return handlers.handle_playbill_authoring_abandon_insertion(
            require_instance_id(instance_id),
            intent_id,
            expectation_id,
        )

    @_tool
    def cruxible_playbill_block_declare(
        instance_id: InstanceId = None,
        *,
        stamp: dict[str, Any],
    ) -> contracts.PlaybillBlockDeclareResultV1:
        """Register one projection block a workspace just stamped into its page."""
        return handlers.handle_playbill_block_declare(require_instance_id(instance_id), stamp)

    @_tool
    def cruxible_playbill_block_depublish(
        instance_id: InstanceId = None,
        *,
        source_id: str,
        block_id: str,
    ) -> contracts.PlaybillBlockDepublishResultV1:
        """Release the publication registration that demands one page block."""
        return handlers.handle_playbill_block_depublish(
            require_instance_id(instance_id), source_id, block_id
        )

    @_tool
    def cruxible_playbill_list_claims(
        instance_id: InstanceId = None,
        *,
        subject_path: str | None = None,
        predicate: str | None = None,
        include_retired: bool = False,
        subject_kind: str | None = None,
    ) -> contracts.PlaybillClaimList:
        """List accepted Claims, optionally by Subject, Subject kind or predicate."""
        return handlers.handle_playbill_list_claims(
            require_instance_id(instance_id),
            subject_path=subject_path,
            predicate=predicate,
            include_retired=include_retired,
            subject_kind=subject_kind,
        )

    @_tool
    def cruxible_playbill_claim_values(
        instance_id: InstanceId = None,
        *,
        subject_kind: str,
        predicates: list[str],
        subject_ids: list[str] | None = None,
        evaluation_time: str | None = None,
    ) -> ClaimValuesResultV1:
        """Status table: each live Claim's value and verdict for Subjects of one kind.

        Covers every Subject of ``subject_kind`` (or only ``subject_ids``) for the
        given fully qualified predicates, one row per Claim with ``subject_id``,
        ``value`` and ``verdict``, without full Claim views.
        """
        return handlers.handle_playbill_claim_values(
            require_instance_id(instance_id),
            subject_kind=subject_kind,
            predicates=predicates,
            subject_ids=subject_ids,
            evaluation_time=evaluation_time,
        )

    @_tool
    def cruxible_playbill_get_claim(
        instance_id: InstanceId = None,
        *,
        identity: str,
        evaluation_time: str | None = None,
    ) -> contracts.PlaybillClaimViewV2:
        """Read one accepted Claim with its capture-admission accounts."""
        return handlers.handle_playbill_get_claim(
            require_instance_id(instance_id),
            identity,
            evaluation_time=evaluation_time,
        )

    @_tool
    def cruxible_playbill_claim_history(
        instance_id: InstanceId = None, *, identity: str
    ) -> contracts.PlaybillClaimHistory:
        """Read one Claim's accepted lineage."""
        return handlers.handle_playbill_claim_history(require_instance_id(instance_id), identity)

    @_tool
    def cruxible_playbill_explain_claim(
        instance_id: InstanceId = None,
        *,
        identity: str,
        evaluation_time: str | None = None,
    ) -> contracts.PlaybillClaimExplanationV2 | contracts.PlaybillClaimExplanationV3:
        """Explain one Claim's verdict, law evidence, and sources."""
        return handlers.handle_playbill_explain_claim(
            require_instance_id(instance_id), identity, evaluation_time=evaluation_time
        )

    @_tool
    def cruxible_playbill_list_query_definitions(
        instance_id: InstanceId = None,
    ) -> contracts.PlaybillQueryDefinitionList:
        """List accepted QueryDefinition entrypoints."""
        return handlers.handle_playbill_list_query_definitions(require_instance_id(instance_id))

    @_tool
    def cruxible_playbill_policies_in_force(
        instance_id: InstanceId = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.PLAYBILL_POLICY_LIST_MAX_LIMIT)
        ] = contracts.PLAYBILL_POLICY_LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> contracts.PlaybillPolicyInForceList:
        """List one page of live governed policies; pass next_cursor back while truncated."""
        return handlers.handle_playbill_policies_in_force(
            require_instance_id(instance_id), limit=limit, cursor=cursor
        )

    @_tool
    def cruxible_playbill_get_query_definition(
        instance_id: InstanceId = None, *, name: str
    ) -> contracts.PlaybillQueryDefinitionView:
        """Read one accepted QueryDefinition and its contract."""
        return handlers.handle_playbill_get_query_definition(require_instance_id(instance_id), name)

    @_tool
    def cruxible_playbill_run_query(
        instance_id: InstanceId = None,
        *,
        name: str,
        parameters: dict[str, Any] | None = None,
        evaluation_time: str | None = None,
        budgets: dict[str, Any] | None = None,
    ) -> contracts.PlaybillQueryRun:
        """Execute an accepted QueryDefinition and return its execution receipt."""
        return handlers.handle_playbill_run_query(
            require_instance_id(instance_id),
            name,
            parameters=parameters,
            evaluation_time=evaluation_time,
            budgets=budgets,
        )

    @_tool
    def cruxible_playbill_procedure_readiness(
        instance_id: InstanceId = None,
        *,
        name: str,
        evaluation_time: str,
    ) -> contracts.PlaybillProcedureReadiness:
        """Inspect one accepted Procedure's bindings and executable profile."""
        return handlers.handle_playbill_procedure_readiness(
            require_instance_id(instance_id),
            name,
            evaluation_time=evaluation_time,
        )

    @_tool
    def cruxible_playbill_procedure_bind(
        instance_id: InstanceId = None,
        *,
        name: str,
        bindings: list[dict[str, Any]],
    ) -> contracts.PlaybillProcedureBindResult:
        """Propose exact accepted bindings for one Procedure's open slots."""
        return handlers.handle_playbill_procedure_bind(
            require_instance_id(instance_id),
            name,
            bindings=bindings,
        )

    @_tool
    def cruxible_playbill_procedure_run(
        instance_id: InstanceId = None,
        *,
        name: str,
        input: Any,
        evaluation_time: str | None = None,
        at: dict[str, Any] | None = None,
        resolution_contract: contracts.ResolutionContractReferenceV1 | None = None,
        trigger_event: contracts.TriggerEventReferenceV1 | None = None,
    ) -> contracts.PlaybillProcedureRunState:
        """Run one accepted Procedure deterministically."""
        return handlers.handle_playbill_procedure_run(
            require_instance_id(instance_id),
            name,
            evaluation_time=evaluation_time,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
            at=at,
            input=input,
        )

    @_tool
    def cruxible_playbill_procedure_run_status(
        instance_id: InstanceId = None,
        *,
        run_id: str,
    ) -> contracts.PlaybillProcedureRunState:
        """Read one durable Procedure run state and its exact next operation."""
        return handlers.handle_playbill_procedure_run_status(
            require_instance_id(instance_id), run_id
        )

    @_tool
    def cruxible_playbill_procedure_measure(
        instance_id: InstanceId = None,
        *,
        name: str,
        request: contracts.PlaybillProcedureMeasureRequestV1,
    ) -> contracts.PlaybillProcedureMeasureResultV1:
        """Evaluate due Procedure measurements from real evidence and credit one run."""
        return handlers.handle_playbill_procedure_measure(
            require_instance_id(instance_id), name, request
        )

    @_tool
    def cruxible_playbill_procedure_readings(
        instance_id: InstanceId = None,
        *,
        name: str,
        request: contracts.PlaybillProcedureReadingsRequestV1,
    ) -> contracts.PlaybillProcedureReadingsResultV1:
        """Inspect measurement standing and retained exact-grain readings."""
        return handlers.handle_playbill_procedure_readings(
            require_instance_id(instance_id), name, request
        )

    @_tool
    def cruxible_playbill_line_check(
        instance_id: InstanceId = None, *, line: str, request: contracts.LineTriggerCheckRequestV1
    ) -> contracts.LineTriggerCheckResultV1:
        """Check a Line's trigger and admitted occurrences; never enqueue or execute."""
        return handlers.handle_playbill_line_check(require_instance_id(instance_id), line, request)

    @_tool
    def cruxible_playbill_line_arm(
        instance_id: InstanceId = None, *, line: str
    ) -> contracts.LineArmV1:
        """Arm a Line forward-only; the daemon admits what it matches under your credential."""
        return handlers.handle_playbill_line_arm(require_instance_id(instance_id), line)

    @_tool
    def cruxible_playbill_line_disarm(
        instance_id: InstanceId = None, *, line: str
    ) -> contracts.LineArmV1:
        """Stop a Line admitting work on its own; admitted runs are not cancelled."""
        return handlers.handle_playbill_line_disarm(require_instance_id(instance_id), line)

    @_tool
    def cruxible_playbill_line_status(
        instance_id: InstanceId = None, *, line: str
    ) -> contracts.LineArmV1:
        """Read a Line's current arm, or its last one and why it stopped."""
        return handlers.handle_playbill_line_status(require_instance_id(instance_id), line)

    @_tool
    def cruxible_playbill_line_evaluate(
        instance_id: InstanceId = None, *, line: str, request: contracts.LineEvaluateRequestV1
    ) -> contracts.LineTriggerCheckResultV1:
        """Explicitly evaluate a historical range into pending work; never execute."""
        return handlers.handle_playbill_line_evaluate(
            require_instance_id(instance_id), line, request
        )

    @_tool
    def cruxible_playbill_line_dispatch(
        instance_id: InstanceId = None, *, line: str, request: contracts.LineDispatchRequestV1
    ) -> contracts.LineDispatchResultV1:
        """Execute retained pending occurrences using the current authenticated actor."""
        return handlers.handle_playbill_line_dispatch(
            require_instance_id(instance_id), line, request
        )

    @_tool
    def cruxible_playbill_line_run(
        instance_id: InstanceId = None,
        *,
        line: str,
        evaluation_time: str | None = None,
        occurrence_id: str | None = None,
        resolution_contract: contracts.ResolutionContractReferenceV1 | None = None,
        trigger_event: contracts.TriggerEventReferenceV1 | None = None,
    ) -> contracts.PlaybillProcedureRunState:
        """Trigger one due occurrence of an accepted Line."""
        return handlers.handle_playbill_line_run(
            require_instance_id(instance_id),
            line,
            occurrence_id=occurrence_id,
            evaluation_time=evaluation_time,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
        )

    @_tool
    def cruxible_playbill_resolution_contracts(
        instance_id: InstanceId = None,
        *,
        claim_id: str | None = None,
        request: contracts.ResolutionContractsRequestV1 | None = None,
    ) -> contracts.ResolutionContractsResultV1:
        """Find accepted resolution contracts testing a Claim, by Claim ID.

        The daemon resolves the Claim's accepted version. ``request`` is the
        advanced form carrying an exact hypothesis reference; pass one or the other.
        """
        if (claim_id is None) == (request is None):
            raise ValueError("pass exactly one of claim_id or request")
        return handlers.handle_playbill_resolution_contracts(
            require_instance_id(instance_id),
            request or contracts.ResolutionContractsRequestV1(hypothesis=cast(str, claim_id)),
        )

    @_tool
    def cruxible_playbill_predict(
        instance_id: InstanceId = None,
        *,
        request: contracts.PlaybillPredictRequestV2,
    ) -> contracts.PlaybillPredictResultV2:
        """Propose a governed test of an accepted Claim, named by Claim ID in the hypothesis."""
        return handlers.handle_playbill_predict(require_instance_id(instance_id), request)

    @_tool
    def cruxible_playbill_settle(
        instance_id: InstanceId = None,
        *,
        prediction_id: str,
        observation: str | None = None,
        request: contracts.PlaybillSettleRequestV2 | None = None,
    ) -> contracts.PlaybillSettleResultV2:
        """Settle one prediction from the Claim ID of an accepted observation.

        ``prediction_id`` is the contract name or the RSC-... window id ``next``
        names; ``observation`` is the settling Claim's ID. ``request`` is the
        advanced form (exact references or mandated terminal evidence).
        """
        if (observation is None) == (request is None):
            raise ValueError("pass exactly one of observation or request")
        return handlers.handle_playbill_settle_prediction(
            require_instance_id(instance_id),
            prediction_id,
            request or contracts.PlaybillSettleRequestV2(observation=observation),
        )

    @_tool
    def cruxible_playbill_discover(
        instance_id: InstanceId = None,
        *,
        query: str | None = None,
        entrypoint: str | None = None,
        evaluation_time: str | None = None,
        profile: Literal["interfaces", "subjects", "all"] = "interfaces",
        budget: dict[str, Any] | None = None,
    ) -> contracts.PlaybillDiscoveryResult | contracts.PlaybillInterfaceInventory:
        """Find accepted interfaces and Subjects by exact or lexical match."""
        return handlers.handle_playbill_discover(
            require_instance_id(instance_id),
            query=query,
            entrypoint=entrypoint,
            evaluation_time=evaluation_time,
            profile=profile,
            budget=budget,
        )

    @_tool
    def cruxible_playbill_search(
        instance_id: InstanceId = None,
        *,
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
            require_instance_id(instance_id),
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
    def cruxible_playbill_next(
        instance_id: InstanceId = None,
        *,
        evaluation_time: str | None = None,
        access_profile: dict[str, Any] | None = None,
        expiring_within: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Evidence-expiration lead window as {'microseconds': N}; defaults to 7 days."
                )
            ),
        ] = None,
        since_result_digest: Annotated[
            str | None,
            Field(description="A prior result_digest; return only rows new since that queue."),
        ] = None,
        limit: Annotated[int | None, Field(ge=1, le=contracts.PLAYBILL_NEXT_MAX_LIMIT)] = None,
        cursor: str | None = None,
    ) -> contracts.PlaybillNextResult:
        """Rank outstanding repair work with the exact next operation for each row."""
        return handlers.handle_playbill_next(
            require_instance_id(instance_id),
            evaluation_time=evaluation_time,
            access_profile=access_profile,
            expiring_within=expiring_within,
            since_result_digest=since_result_digest,
            limit=limit,
            cursor=cursor,
        )

    @_tool
    def cruxible_playbill_since(
        instance_id: InstanceId = None,
        *,
        generation: int,
        at: dict[str, Any] | None = None,
        access_profile: dict[str, Any] | None = None,
        max_rows: Annotated[int, Field(ge=1, le=1000)] = 100,
        max_bytes: Annotated[int, Field(ge=1, le=1_048_576)] = 65_536,
        cursor: dict[str, Any] | None = None,
    ) -> contracts.PlaybillSinceResult:
        """Read accepted ChangeSet members after one generation."""
        return handlers.handle_playbill_since(
            require_instance_id(instance_id),
            generation=generation,
            at=at,
            access_profile=access_profile,
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        )

    @_tool
    def cruxible_playbill_curation_list(
        instance_id: InstanceId = None,
        *,
        evaluation_time: str,
        access_profile: dict[str, Any] | None = None,
        workspace_observation: dict[str, Any] | None = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.PLAYBILL_CURATION_LIST_MAX_LIMIT)
        ] = contracts.PLAYBILL_CURATION_LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> contracts.PlaybillCurationListResult:
        """List one page of curation patterns and ingest block observations.

        Pass next_cursor back while the result is truncated.
        """
        return handlers.handle_playbill_curation_list(
            require_instance_id(instance_id),
            evaluation_time=evaluation_time,
            access_profile=access_profile,
            workspace_observation=workspace_observation,
            limit=limit,
            cursor=cursor,
        )

    @_tool
    def cruxible_playbill_audit(
        instance_id: InstanceId = None,
        *,
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
            require_instance_id(instance_id),
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
        instance_id: InstanceId = None,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        attribution_refs: list[str] | None = None,
    ) -> contracts.PlaybillCurationActionResult:
        """Resolve one detector-version item as mechanically inapplicable."""
        return handlers.handle_playbill_curation_overrule(
            require_instance_id(instance_id),
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            attribution_refs=attribution_refs or [],
        )

    @_tool
    def cruxible_playbill_curation_accept_fixed(
        instance_id: InstanceId = None,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        accepted_proposal_id: str,
        accepted_changeset_digest: str,
        attribution_refs: list[str] | None = None,
    ) -> contracts.PlaybillCurationActionResult:
        """Link one curation item to its exact accepted resolving ChangeSet."""
        return handlers.handle_playbill_curation_accept_fixed(
            require_instance_id(instance_id),
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=accepted_proposal_id,
            accepted_changeset_digest=accepted_changeset_digest,
            attribution_refs=attribution_refs or [],
        )

    @_tool
    def cruxible_playbill_curation_suppress(
        instance_id: InstanceId = None,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        scope: Literal["item", "pattern", "instance"],
        until_generation: int | None = None,
        attribution_refs: list[str] | None = None,
    ) -> contracts.PlaybillCurationActionResult:
        """Hide curation work temporarily without resolving its detector facts."""
        return handlers.handle_playbill_curation_suppress(
            require_instance_id(instance_id),
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=scope,
            until_generation=until_generation,
            attribution_refs=attribution_refs or [],
        )

    @_tool
    def cruxible_playbill_expand(
        instance_id: InstanceId = None,
        *,
        address: dict[str, Any],
        facets: list[str] | None = None,
        evaluation_time: str | None = None,
        budget: dict[str, Any] | None = None,
    ) -> contracts.PlaybillContextCapsule:
        """Expand one accepted address into a bounded context capsule."""
        return handlers.handle_playbill_expand(
            require_instance_id(instance_id),
            address,
            evaluation_time=evaluation_time,
            facets=facets or [],
            budget=budget,
        )

    @_tool
    def cruxible_playbill_coverage(
        instance_id: InstanceId = None,
        *,
        observations: Annotated[
            list[dict[str, Any]] | None,
            Field(description="Working-source observations you built; omit with bindings."),
        ] = None,
        bindings: Annotated[
            dict[str, str] | None,
            Field(
                description=(
                    "Logical source bindings; the adapter reads the selected workspace "
                    "files (files, ranges, grep_results_path, or whole_working_set)."
                )
            ),
        ] = None,
        files: list[str] | None = None,
        ranges: list[str] | None = None,
        grep_results_path: str | None = None,
        whole_working_set: Annotated[
            bool, Field(description="With bindings, cover every declared workspace file.")
        ] = False,
        budget: dict[str, Any] | None = None,
        scan_budget: dict[str, Any] | None = None,
    ) -> contracts.PlaybillCoverageResult:
        """Resolve what working sources have to do with accepted state."""
        return handlers.handle_playbill_coverage(
            require_instance_id(instance_id),
            observations=observations,
            bindings=bindings,
            files=tuple(files or ()),
            ranges=tuple(ranges or ()),
            grep_results_path=grep_results_path,
            whole_working_set=whole_working_set,
            budget=budget,
            scan_budget=scan_budget,
        )

    @_tool
    def cruxible_playbill_workspace_source_compile(
        instance_id: InstanceId = None,
        *,
        catalog_path: str,
        repository_root: str = ".",
        local_catalog_path: str | None = None,
        root_aliases: dict[str, str] | None = None,
    ) -> SourceCompilationBundle:
        """Compile declared workspace sources against accepted daemon context."""
        return handlers.handle_playbill_workspace_source_compile(
            require_instance_id(instance_id),
            catalog_path=catalog_path,
            repository_root=repository_root,
            local_catalog_path=local_catalog_path,
            root_aliases=root_aliases or {},
        )

    @_tool
    def cruxible_playbill_floor_export(
        instance_id: InstanceId = None,
        *,
        mode: Annotated[
            handlers.FloorExportMode,
            Field(
                description=(
                    "bytes: return base64 files per floor path; write: verify and write "
                    ".playbill/floor in the MCP workspace; status: report whether that "
                    "floor is current, stale, or missing."
                )
            ),
        ],
        force: Annotated[
            bool, Field(description="write only: replace a non-empty floor directory.")
        ] = False,
    ) -> (
        contracts.PlaybillFloorExport
        | contracts.PlaybillWorkspaceFloorWriteResult
        | contracts.PlaybillWorkspaceFloorStatus
    ):
        """Export the accepted greppable floor as bytes, write it locally, or report its status."""
        return handlers.handle_playbill_floor_export(
            require_instance_id(instance_id),
            mode=mode,
            force=force,
        )

    return registered
