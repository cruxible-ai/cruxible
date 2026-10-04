"""MCP registrations for the Playbill-only public surface."""

from __future__ import annotations

import asyncio
from datetime import datetime
from functools import wraps
from typing import Annotated, Any, Callable, Literal, cast

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.json_schema import SkipJsonSchema

from cruxible_client import contracts
from cruxible_client.authoring.inputs import AuthoringInput, ClaimInput
from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.authoring.models import (
    BlockDetachResult,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.capture_reads import CaptureRead, CaptureReadRequest
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.change_control import DryRun, PreviewAt
from cruxible_client.contracts.claim_attestations import ClaimAttestationAppendResult
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
)
from cruxible_client.contracts.compact_query import (
    QueryClaimStatus,
    QueryFilter,
    QueryFollow,
    QueryReceiptDetail,
)
from cruxible_client.contracts.declared_blocks import BlockRepinResult
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.evidence_rule_upgrade import (
    EvidenceRuleUpgradeRequest,
    EvidenceRuleUpgradeResult,
)
from cruxible_client.contracts.get_reads import (
    GET_HISTORY_MAX_LIMIT,
    ByteRange,
    GetResult,
)
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitBuildRequest,
    KitBuildResult,
    KitChangeResult,
    KitRemoveRequest,
    KitStatus,
)
from cruxible_client.contracts.provider_installation import (
    ProviderCatalog,
    ProviderInstallRequest,
    ProviderInstallResult,
)
from cruxible_client.contracts.query.definitions import QueryDefinitionSpec
from cruxible_client.contracts.query.grammar import QueryBudgets
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_client.contracts.write import (
    AddChange,
    CaptureEvidence,
    ClaimValue,
    ContractEvidence,
    ExpectedValue,
    FileEvidence,
    RetireChange,
    SelfEvidence,
    SetChange,
    SlotRef,
    WriteAccept,
    WriteOutcome,
    WriteRetireReason,
    WriteRole,
)
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord
from cruxible_core.claims.claim_type_migrations import ClaimTypeMigrationRequestAny
from cruxible_core.coverage.adapter import WorkingSourceObservation
from cruxible_core.coverage.contracts import CoverageAccessProfile, CoverageCardBudget
from cruxible_core.coverage.indexes import CoverageScanBudget
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
from cruxible_core.service.discovery.next import NextWorkspaceObservation
from cruxible_core.service.procedures.procedure_runs import ProcedureSlotBindingRequest


class McpBlockQuery(BaseModel):
    """One QueryDefinition backing of a block, with its parameter bindings."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    params: dict[str, JsonValue] = Field(default_factory=dict)


class McpRootAlias(BaseModel):
    """One named workspace root used by source catalog compilation."""

    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1)
    path: str = Field(min_length=1)


class McpSourceBinding(BaseModel):
    """One workspace path bound to a declared logical source."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    source_id: str = Field(min_length=1)


def _root_aliases(rows: list[McpRootAlias] | None) -> dict[str, str] | None:
    if rows is None:
        return None
    result = {row.alias: row.path for row in rows}
    if len(result) != len(rows):
        raise ValueError("root_aliases names an alias more than once")
    return result


def _source_bindings(rows: list[McpSourceBinding] | None) -> dict[str, str] | None:
    if rows is None:
        return None
    result = {row.path: row.source_id for row in rows}
    if len(result) != len(rows):
        raise ValueError("bindings names a path more than once")
    return result


class McpFileEvidence(FileEvidence):
    """File evidence as an MCP caller names it; this adapter reads the file.

    The daemon never reads workspace files, so the adapter fills ``observation``
    before the request leaves it. The field stays out of the advertised schema.
    """

    observation: SkipJsonSchema[WorkingSelectionObservation | None] = None


McpEvidence = Annotated[
    SelfEvidence | CaptureEvidence | McpFileEvidence | ContractEvidence,
    Field(discriminator="kind"),
]
_EVIDENCE_DEFAULT = "Default: the write's `because` as self evidence."


class McpSetChange(SetChange):
    __doc__ = SetChange.__doc__

    evidence: McpEvidence | None = Field(default=None, description=_EVIDENCE_DEFAULT)


class McpAddChange(AddChange):
    __doc__ = AddChange.__doc__

    evidence: McpEvidence | None = Field(default=None, description=_EVIDENCE_DEFAULT)


McpChange = Annotated[McpSetChange | McpAddChange | RetireChange, Field(discriminator="op")]


InstanceId = Annotated[
    str | None,
    Field(description=f"Instance; default ${MCP_INSTANCE_ENV}."),
]


ReadAt = Annotated[
    str | int | None,
    Field(
        description=(
            "Accepted generation to read: git oid (12+ hex prefix) or generation number "
            "(all digits, at most 11, is always a number); default: head."
        )
    ),
]


def _dump(model: BaseModel | None) -> dict[str, Any] | None:
    """A typed MCP argument as the JSON object its handler validates again."""

    return None if model is None else model.model_dump(mode="json")


def _read_at(at: Any) -> Any:
    """A read's ``at`` for the handler: a generation number travels as its decimal."""

    return str(at) if isinstance(at, int) and not isinstance(at, bool) else at


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
    def cruxible_provider_catalog(
        instance_id: InstanceId = None,
    ) -> ProviderCatalog:
        """Discover provider packages available from the configured repository."""
        return handlers.handle_playbill_provider_catalog(require_instance_id(instance_id))

    @_tool
    def cruxible_provider_install(
        instance_id: InstanceId = None,
        *,
        request: ProviderInstallRequest,
    ) -> ProviderInstallResult:
        """Install a provider package and propose its definitions; requires ADMIN."""
        return handlers.handle_playbill_provider_install(require_instance_id(instance_id), request)

    @_tool
    def cruxible_kit_build(
        instance_id: InstanceId = None,
        *,
        request: KitBuildRequest,
    ) -> KitBuildResult:
        """Export this instance's definitions under the owned prefixes as one kit release."""
        return handlers.handle_playbill_kit_build(require_instance_id(instance_id), request)

    @_tool
    def cruxible_kit_status(instance_id: InstanceId = None) -> KitStatus:
        """List installed kits and the kit paths edited since install."""
        return handlers.handle_playbill_kit_status(require_instance_id(instance_id))

    @_tool
    def cruxible_kit_add(
        instance_id: InstanceId = None,
        *,
        request: KitAddRequest,
    ) -> KitChangeResult:
        """Propose installing or upgrading a kit as one change set; activation is separate."""
        return handlers.handle_playbill_kit_add(require_instance_id(instance_id), request)

    @_tool
    def cruxible_claim_type_upgrade(
        instance_id: InstanceId = None,
        *,
        request: ClaimTypeUpgradeRequest,
    ) -> ClaimTypeUpgradeResult:
        """Propose moving live ClaimTypes to v7 (dry_run evaluates and proposes nothing)."""
        return handlers.handle_playbill_claim_type_upgrade(
            require_instance_id(instance_id), request
        )

    @_tool
    def cruxible_evidence_rules_upgrade(
        instance_id: InstanceId = None,
        *,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> EvidenceRuleUpgradeResult:
        """Propose moving live ClaimTypes to evidence rules that name contracts by identity."""
        return handlers.handle_playbill_evidence_rules_upgrade(
            require_instance_id(instance_id),
            EvidenceRuleUpgradeRequest(dry_run=dry_run, at=at),
        )

    @_tool
    def cruxible_kit_remove(
        instance_id: InstanceId = None,
        *,
        request: KitRemoveRequest,
    ) -> KitChangeResult:
        """Propose retiring every artifact a kit installed; activation is separate."""
        return handlers.handle_playbill_kit_remove(require_instance_id(instance_id), request)

    @_tool
    def cruxible_init(
        instance_id: InstanceId = None,
        *,
        principals: list[PrincipalRecord],
        operating_profile: Literal["local", "cloud"] = "local",
        require_independent_approval: bool = False,
        git_object_format: Literal["sha1", "sha256"] | None = None,
    ) -> contracts.InitResult:
        """Bootstrap Cruxible from client-generated public principals.

        Provider installation is a separate administrative operation.
        """
        return handlers.handle_playbill_init(
            require_instance_id(instance_id),
            [item.model_dump(mode="json") for item in principals],
            operating_profile,
            require_independent_approval,
            git_object_format=git_object_format,
        )

    @_tool
    def cruxible_store_body(
        instance_id: InstanceId = None, *, content_base64: str
    ) -> contracts.CasObjectResult:
        """Store inert exact body bytes."""
        return handlers.handle_playbill_store_body(require_instance_id(instance_id), content_base64)

    @_tool
    def cruxible_propose_document(
        instance_id: InstanceId = None,
        *,
        shell: DocumentShell,
        proposal_name: str,
        source_compilation_digest: str | None = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.ProposalInspection:
        """Propose a governed Document create or supersession."""
        return handlers.handle_playbill_propose_document(
            require_instance_id(instance_id),
            shell.model_dump(mode="json"),
            proposal_name,
            source_compilation_digest,
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_inspect_proposal(
        instance_id: InstanceId = None, *, proposal_id: str
    ) -> contracts.ProposalInspection:
        """Inspect immutable proposal evidence."""
        return handlers.handle_playbill_inspect_proposal(
            require_instance_id(instance_id), proposal_id
        )

    @_tool
    def cruxible_inspect_refusal(
        instance_id: InstanceId = None, *, proposal_id: str
    ) -> contracts.RefusalInspection:
        """Inspect typed admission and law diagnostics."""
        return handlers.handle_playbill_inspect_refusal(
            require_instance_id(instance_id), proposal_id
        )

    @_tool
    def cruxible_review(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        include_body: bool = False,
    ) -> contracts.ProposalReview:
        """Render a structured candidate review."""
        return handlers.handle_playbill_review(
            require_instance_id(instance_id), proposal_id, include_body=include_body
        )

    @_tool
    def cruxible_prepare_approval(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        signer_id: str,
        include_body: bool = False,
    ) -> contracts.ApprovalChallenge:
        """Fetch the exact statement for a client-held signer."""
        return handlers.handle_playbill_prepare_approval(
            require_instance_id(instance_id),
            proposal_id,
            signer_id=signer_id,
            include_body=include_body,
        )

    @_tool
    def cruxible_submit_approval(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        attestation: ApprovalAttestation,
    ) -> contracts.ApprovalReceipt:
        """Submit a public approval attestation."""
        return handlers.handle_playbill_submit_approval(
            require_instance_id(instance_id), proposal_id, attestation.model_dump(mode="json")
        )

    @_tool
    def cruxible_approve(
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
    ) -> contracts.ApprovalReceipt:
        """Approve a proposal with a local key: challenge, sign and submit in one call."""
        return handlers.handle_playbill_approve(
            require_instance_id(instance_id),
            proposal_id,
            signer_id=signer_id,
            candidate_digest=candidate_digest,
        )

    @_tool
    def cruxible_activate(
        instance_id: InstanceId = None, *, proposal_id: str
    ) -> contracts.WorkspaceActivationResult:
        """Settle by compare-and-set and refresh the configured client-owned floor."""
        return handlers.handle_playbill_activate(require_instance_id(instance_id), proposal_id)

    @_tool
    def cruxible_whoami(instance_id: InstanceId = None) -> McpWhoAmIResult:
        """Name the resolved instance, the credential-derived actor, and its registration."""
        return handlers.handle_playbill_whoami(require_instance_id(instance_id))

    @_tool
    def cruxible_orient(
        instance_id: InstanceId = None,
        *,
        kind: Annotated[
            str | None,
            Field(description="One Subject kind to read in full, e.g. 'dev.roadmap_item'."),
        ] = None,
        section: Annotated[
            contracts.OrientSection | None,
            Field(description="Page one artifact family as compact rows instead of the map."),
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.ORIENT_MAX_LIMIT)
        ] = contracts.ORIENT_DEFAULT_LIMIT,
        cursor: Annotated[
            str | None, Field(description="next_cursor from the previous page of this view.")
        ] = None,
        at: ReadAt = None,
        evaluation_time: Annotated[
            str | None, Field(description="ISO-8601 instant; defaults to now.")
        ] = None,
    ) -> contracts.OrientResult:
        """Map accepted state: kinds and predicates, artifacts, you, attention, next calls."""
        return handlers.handle_playbill_orient(
            require_instance_id(instance_id),
            kind=kind,
            section=section,
            limit=limit,
            cursor=cursor,
            at=_read_at(at),
            evaluation_time=evaluation_time,
        )

    @_tool
    def cruxible_proposal_list(
        instance_id: InstanceId = None,
        *,
        status: Literal["open", "settled", "incomplete"] | None = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.PROPOSAL_LIST_MAX_LIMIT)
        ] = contracts.PROPOSAL_LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> contracts.ProposalList:
        """List one page of proposal evidence; pass next_cursor back while truncated."""
        return handlers.handle_playbill_list_proposals(
            require_instance_id(instance_id), status, limit=limit, cursor=cursor
        )

    @_tool
    def cruxible_proposal_readmit(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.ProposalReadmitResult:
        """Re-admit one stale proposal against the current accepted coordinate."""
        return handlers.handle_playbill_readmit_proposal(
            require_instance_id(instance_id), proposal_id, dry_run=dry_run, at=at
        )

    @_tool
    def cruxible_proposal_withdraw(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        reason: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.ProposalWithdrawResult:
        """Retire one open proposal that will never be activated."""
        return handlers.handle_playbill_withdraw_proposal(
            require_instance_id(instance_id), proposal_id, reason, dry_run=dry_run, at=at
        )

    @_tool
    def cruxible_read_capture(
        instance_id: InstanceId = None, *, request: CaptureReadRequest
    ) -> CaptureRead:
        """Read exact retained Capture evidence with a byte budget and body permission."""
        return handlers.handle_playbill_read_capture(require_instance_id(instance_id), request)

    @_tool
    def cruxible_source_context(
        instance_id: InstanceId = None,
    ) -> contracts.SourceContext:
        """Fetch path-free inputs for local source compilation."""
        return handlers.handle_playbill_source_context(require_instance_id(instance_id))

    @_tool
    def cruxible_source_check(
        instance_id: InstanceId = None,
        *,
        bundle: Annotated[
            SourceCompilationBundle | None,
            Field(description="A compiled source bundle; omit when passing catalog_path."),
        ] = None,
        catalog_path: Annotated[
            str | None,
            Field(description="Workspace source catalog to compile first; omit with bundle."),
        ] = None,
        repository_root: str = ".",
        local_catalog_path: str | None = None,
        root_aliases: list[McpRootAlias] | None = None,
    ) -> contracts.SourceCheckResult:
        """Compare a compiled bundle or catalog-declared workspace sources with accepted state."""
        return handlers.handle_playbill_source_check(
            require_instance_id(instance_id),
            bundle=None if bundle is None else bundle.model_dump(mode="json"),
            catalog_path=catalog_path,
            repository_root=repository_root,
            local_catalog_path=local_catalog_path,
            root_aliases=_root_aliases(root_aliases),
        )

    @_tool
    def cruxible_propose_source_bundle(
        instance_id: InstanceId = None,
        *,
        bundle: SourceCompilationBundle,
        source_name: str,
        proposal_name: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.ProposalInspection:
        """Propose frozen source bytes without a client path."""
        return handlers.handle_playbill_propose_source_bundle(
            require_instance_id(instance_id),
            bundle.model_dump(mode="json"),
            source_name=source_name,
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_compiler_upgrade(
        instance_id: InstanceId = None,
        *,
        target_compiler_digest: str,
        base: contracts.AcceptedCoordinate,
        proposal_name: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.ProposalInspection:
        """Propose an admin-only compiler upgrade; review, approve and activate separately."""
        return handlers.handle_playbill_compiler_upgrade(
            require_instance_id(instance_id),
            target_compiler_digest,
            base.model_dump(mode="json"),
            proposal_name,
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_propose_principal_change(
        instance_id: InstanceId = None,
        *,
        principal: PrincipalRecord,
        proposal_name: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.ProposalInspection:
        """Propose principal registration, rotation, revocation, or recovery."""
        return handlers.handle_playbill_propose_principal_change(
            require_instance_id(instance_id),
            principal.model_dump(mode="json"),
            proposal_name,
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_propose_claim_type(
        instance_id: InstanceId = None,
        *,
        input: ClaimTypeInputRecord,
        proposal_name: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.ClaimTypeInputProposalResult:
        """Propose one governed ClaimType interface."""
        return handlers.handle_playbill_propose_claim_type(
            require_instance_id(instance_id),
            input.model_dump(mode="json"),
            proposal_name,
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_claim_type_migrate(
        instance_id: InstanceId = None,
        *,
        request: ClaimTypeMigrationRequestAny,
    ) -> contracts.ClaimTypeMigrationResponse:
        """Propose one ClaimType successor and its dependent dispositions atomically."""
        return handlers.handle_playbill_migrate_claim_type(
            require_instance_id(instance_id), request.model_dump(mode="json")
        )

    @_tool
    def cruxible_claim_attest(
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
            contracts.AcceptedCoordinate | None,
            Field(description="With capture_digests: the accepted coordinate you read."),
        ] = None,
        attested_at: Annotated[
            datetime | None,
            Field(description="With capture_digests: when you observed the new Capture."),
        ] = None,
    ) -> ClaimAttestationAppendResult:
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
            referent_coordinate=_dump(referent_coordinate),
            attested_at=attested_at,
        )

    @_tool
    def cruxible_authoring_create(
        instance_id: InstanceId = None,
        *,
        payload: AuthoringInput,
    ) -> contracts.AuthoringIntentViewRecord:
        """Create or recover a daemon-owned authoring intent."""
        return handlers.handle_playbill_authoring_create(
            require_instance_id(instance_id), payload.model_dump(mode="json")
        )

    @_tool
    def cruxible_authoring_example(
        name: contracts.AuthoringExampleName,
        claim_id: str | None = None,
        capture_digest: str | None = None,
    ) -> contracts.AuthoringExampleResult:
        """Return one model-constructed input template with no daemon call."""
        return handlers.handle_playbill_authoring_example(
            name,
            claim_id=claim_id,
            capture_digest=capture_digest,
        )

    @_tool
    def cruxible_authoring_get(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.AuthoringIntentViewRecord:
        """Read one actor-scoped authoring intent."""
        return handlers.handle_playbill_authoring_get(require_instance_id(instance_id), intent_id)

    @_tool
    def cruxible_authoring_resume(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.AuthoringIntentViewRecord:
        """Resume one durable authoring continuation."""
        return handlers.handle_playbill_authoring_resume(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_authoring_list_pending(
        instance_id: InstanceId = None,
    ) -> contracts.AuthoringIntentListRecord:
        """List the authenticated writer's pending intents."""
        return handlers.handle_playbill_authoring_list_pending(require_instance_id(instance_id))

    @_tool
    def cruxible_authoring_compile(
        instance_id: InstanceId = None,
        *,
        payload: AuthoringInput,
        intent_id: str | None = None,
    ) -> contracts.AuthoringPreflightResult:
        """Create or update an intent and return its complete preflight."""
        return handlers.handle_playbill_authoring_compile(
            require_instance_id(instance_id),
            payload.model_dump(mode="json"),
            intent_id=intent_id,
        )

    @_tool
    def cruxible_authoring_bind(
        instance_id: InstanceId = None,
        *,
        source_path: str,
        anchor: str,
        payload: ClaimInput,
        window_lines: int | None = None,
    ) -> contracts.AuthoringPreflightResult:
        """Bind one exact workspace anchor and compile the derived Flow-A observation."""
        return handlers.handle_playbill_authoring_bind(
            require_instance_id(instance_id),
            source_path=source_path,
            anchor=anchor,
            payload=payload,
            window_lines=window_lines,
        )

    @_tool
    def cruxible_authoring_preflight(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.AuthoringPreflightResult:
        """Recompute one intent's complete binding preflight."""
        return handlers.handle_playbill_authoring_preflight(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_authoring_rebase(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.AuthoringIntentViewRecord:
        """Rebase one stale authoring intent onto the current accepted coordinate."""
        return handlers.handle_playbill_authoring_rebase(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_authoring_submit(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.AuthoringSubmitResultRecord:
        """Idempotently submit one passing authoring intent."""
        return handlers.handle_playbill_authoring_submit(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_authoring_status(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
    ) -> contracts.CandidateStatusRecord:
        """Read exactly what separates an intent from acceptance."""
        return handlers.handle_playbill_authoring_status(
            require_instance_id(instance_id), intent_id
        )

    @_tool
    def cruxible_authoring_abandon_insertion(
        instance_id: InstanceId = None,
        *,
        intent_id: str,
        expectation_id: str | None = None,
    ) -> contracts.InsertionAbandonResultRecord:
        """Abandon a pending insertion while keeping the accepted self-source Claim."""
        return handlers.handle_playbill_authoring_abandon_insertion(
            require_instance_id(instance_id),
            intent_id,
            expectation_id,
        )

    @_tool
    def cruxible_block_repin(
        instance_id: InstanceId = None,
        *,
        block: Annotated[str, Field(description="The block id in its marker.")],
        file: Annotated[
            str | None, Field(description="The page, workspace-relative; or give `source`.")
        ] = None,
        source: Annotated[
            str | None, Field(description="The page's catalog source id; or give `file`.")
        ] = None,
        claims: Annotated[
            list[str] | None, Field(description="Claim backings; omitted keeps the block's.")
        ] = None,
        queries: Annotated[
            list[McpBlockQuery] | None,
            Field(description="QueryDefinition backings; omitted keeps the block's."),
        ] = None,
        artifacts: Annotated[
            list[str] | None,
            Field(description="Subject or ClaimType identities; omitted keeps the block's."),
        ] = None,
        currency_policy: Literal["warn", "require_current"] | None = None,
        backing_digest: Annotated[
            str | None,
            Field(description="The successor digest an ambiguity refusal named, alone."),
        ] = None,
        dry_run: Annotated[
            bool | None, Field(description="true: compute the stamp and write nothing.")
        ] = None,
    ) -> BlockRepinResult:
        """Stamp (or restamp) one projection block; this adapter computes the stamp."""
        return handlers.handle_playbill_block_repin(
            require_instance_id(instance_id),
            block=block,
            file=file,
            source=source,
            claims=claims,
            queries=None
            if queries is None
            else [(item.query, dict(item.params)) for item in queries],
            artifacts=artifacts,
            currency_policy=currency_policy,
            backing_digest=backing_digest,
            dry_run=dry_run,
        )

    @_tool
    def cruxible_block_sync(
        instance_id: InstanceId = None,
        *,
        files: Annotated[
            list[str] | None, Field(description="Pages to check, workspace-relative.")
        ] = None,
        all_sources: Annotated[
            bool, Field(description="Check every page the source catalog names.")
        ] = False,
    ) -> contracts.BlockSyncResult:
        """Check each block's backings against the instance; reads only, edits no page."""
        return handlers.handle_playbill_block_sync(
            require_instance_id(instance_id),
            files=files or (),
            all_sources=all_sources,
        )

    @_tool
    def cruxible_block_detach(
        instance_id: InstanceId = None,
        *,
        files: Annotated[
            list[str],
            Field(
                min_length=1,
                description="Pages whose retired blocks lose their markers, body kept.",
            ),
        ],
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> BlockDetachResult:
        """Remove retired blocks' markers from pages (bodies kept); dry_run edits nothing."""
        return handlers.handle_playbill_block_detach(
            require_instance_id(instance_id), files=files, dry_run=dry_run, at=at
        )

    @_tool
    def cruxible_block_depublish(
        instance_id: InstanceId = None,
        *,
        source_id: str,
        block_id: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.BlockDepublishResult:
        """Release the publication registration that demands one page block."""
        return handlers.handle_playbill_block_depublish(
            require_instance_id(instance_id), source_id, block_id, dry_run=dry_run, at=at
        )

    @_tool
    def cruxible_get(
        instance_id: InstanceId = None,
        *,
        ref: Annotated[
            str,
            Field(
                description=(
                    "Any reference you have seen: CLM-… (or a unique prefix), kind/id, "
                    "a predicate, ClaimType:/Document:/Procedure:/query:/CaptureContract:<name>, "
                    "an artifact path, a proposal id or prefix, or an operational reference: "
                    "Line:<name> (or the Line identity digest next names), CAP-<12+ hex> or "
                    "Capture:<digest>, ResolutionContract:<name>, Mandate:<name>."
                )
            ),
        ],
        detail: Literal["summary", "evidence", "why", "history", "proof", "body"] = "summary",
        range: Annotated[
            ByteRange | None,
            Field(description='Byte range [start, end) of a Document body; detail="body" only.'),
        ] = None,
        at: ReadAt = None,
        evaluation_time: Annotated[
            str | None,
            Field(description="ISO-8601 instant verdicts are evaluated at; default now."),
        ] = None,
        limit: Annotated[
            int | None,
            Field(
                ge=1,
                le=GET_HISTORY_MAX_LIMIT,
                description='Revisions per detail="history" page, newest first; default 20.',
            ),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description='next_cursor of the previous detail="history" page.'),
        ] = None,
    ) -> GetResult:
        """Read one governed thing by reference, values first."""
        return handlers.handle_playbill_get(
            require_instance_id(instance_id),
            ref=ref,
            detail=detail,
            range=range,
            at=_read_at(at),
            evaluation_time=evaluation_time,
            limit=limit,
            cursor=cursor,
        )

    @_tool
    def cruxible_set(
        instance_id: InstanceId = None,
        *,
        subject: Annotated[str, Field(description="The Subject as kind/id.")],
        field: Annotated[
            str,
            Field(description="A field of the Subject's kind, as orient names it."),
        ],
        value: Annotated[
            ClaimValue,
            Field(
                description=(
                    "The value: an enum member, text, number or boolean; a Subject as kind/id "
                    "for a Subject-valued field; the text itself for exact content."
                )
            ),
        ],
        because: Annotated[str, Field(description="Why; also the default evidence.")],
        evidence: Annotated[
            McpEvidence | None,
            Field(
                description=(
                    'Default {"kind": "self", "self": because}. Or {"kind": "capture", '
                    '"capture": "CAP-<12 hex>" or "sha256:…"}, {"kind": "contract", "contract": '
                    '"<CaptureContract>"} (its newest Capture about the Subject), or '
                    '{"kind": "file", "file": "PATH#ANCHOR"}.'
                )
            ),
        ] = None,
        role: Annotated[
            WriteRole | None,
            Field(description="Only when the field permits more than one role."),
        ] = None,
        contend: Annotated[
            bool,
            Field(description="Contest the live value instead of replacing it."),
        ] = False,
        expect: Annotated[
            ExpectedValue | None,
            Field(
                description=(
                    "Compare-and-set: the value you read (a list for several, [] for none); "
                    "refuses slot_changed, showing the value, if the field holds another."
                )
            ),
        ] = None,
        dry_run: Annotated[
            bool, Field(description="Run every check up to the commit; write nothing.")
        ] = False,
        accept: Annotated[
            WriteAccept,
            Field(description="if_allowed: accept now when policy lets you; never: propose only."),
        ] = "if_allowed",
        at: Annotated[
            str | None,
            Field(
                description=(
                    "The coordinate you read at (git oid or 12+ hex prefix); the set refuses "
                    "if the field changed since. Default: current head."
                )
            ),
        ] = None,
    ) -> WriteOutcome:
        """Put one value in one field of one Subject, replacing the live value."""
        return handlers.handle_playbill_set(
            require_instance_id(instance_id),
            subject=subject,
            field=field,
            value=value,
            because=because,
            evidence=evidence,
            role=role,
            contend=contend,
            expect=expect,
            dry_run=dry_run,
            accept=accept,
            at=at,
        )

    @_tool
    def cruxible_retire(
        instance_id: InstanceId = None,
        *,
        target: Annotated[
            str | SlotRef,
            Field(
                description=(
                    'A Claim ID (CLM-…), or {"subject": "kind/id", "field": "…"} for the '
                    "one live value of a field."
                )
            ),
        ],
        because: Annotated[str, Field(description="Why it ends.")],
        reason: Annotated[
            WriteRetireReason,
            Field(
                description=("was-rescinded (withdrawn), was-wrong (it was false), or superseded.")
            ),
        ] = "was-rescinded",
        expect: Annotated[
            ExpectedValue | None,
            Field(
                description=(
                    "Compare-and-set: the field's live value (every live value, as a list, "
                    "for a many-valued field); refuses slot_changed if it holds another."
                )
            ),
        ] = None,
        dry_run: Annotated[
            bool, Field(description="Run every check up to the commit; write nothing.")
        ] = False,
        accept: Annotated[
            WriteAccept,
            Field(description="if_allowed: accept now when policy lets you; never: propose only."),
        ] = "if_allowed",
        at: Annotated[
            str | None,
            Field(description="The coordinate you read at; default: current head."),
        ] = None,
    ) -> WriteOutcome:
        """End one live Claim, and what depends on it, in one change set."""
        return handlers.handle_playbill_retire(
            require_instance_id(instance_id),
            target=target,
            because=because,
            reason=reason,
            expect=expect,
            dry_run=dry_run,
            accept=accept,
            at=at,
        )

    @_tool
    def cruxible_write(
        instance_id: InstanceId = None,
        *,
        changes: Annotated[
            list[McpChange],
            Field(
                min_length=1,
                description=(
                    'Each {"op": "set"|"add", "subject", "field", "value"} or '
                    '{"op": "retire", "target"}; add puts one more value in a many-valued field.'
                ),
            ),
        ],
        because: Annotated[str, Field(description="Why: the change set's rationale.")],
        subject: Annotated[
            str | None,
            Field(
                description=(
                    "The Subject (kind/id) of every change that names none; a change's own "
                    "subject overrides it."
                )
            ),
        ] = None,
        dry_run: Annotated[
            bool, Field(description="Run every check up to the commit; write nothing.")
        ] = False,
        accept: Annotated[
            WriteAccept,
            Field(description="if_allowed: accept now when policy lets you; never: propose only."),
        ] = "if_allowed",
        at: Annotated[
            str | None,
            Field(description="The coordinate you read at; default: current head."),
        ] = None,
    ) -> WriteOutcome:
        """Apply set, add and retire changes together as one change set."""
        return handlers.handle_playbill_write(
            require_instance_id(instance_id),
            changes=changes,
            because=because,
            subject=subject,
            dry_run=dry_run,
            accept=accept,
            at=at,
        )

    @_tool
    def cruxible_query(
        instance_id: InstanceId = None,
        *,
        kind: str | None = None,
        where: list[QueryFilter] | None = None,
        contains: str | None = None,
        select: list[str] | None = None,
        follow: list[QueryFollow] | None = None,
        order_by: list[str] | None = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.QUERY_MAX_LIMIT)
        ] = contracts.QUERY_DEFAULT_LIMIT,
        cursor: str | None = None,
        name: str | None = None,
        params: dict[str, str | int | bool | None] | None = None,
        status: list[QueryClaimStatus] | None = None,
        claims: bool = False,
        budgets: QueryBudgets | None = None,
        receipt: QueryReceiptDetail = "compact",
        at: ReadAt = None,
        evaluation_time: str | None = None,
    ) -> contracts.QueryResultRecord:
        """Query accepted state: rows of values with flags; pass next_cursor while truncated."""
        return handlers.handle_playbill_query(
            require_instance_id(instance_id),
            kind=kind,
            where=where,
            contains=contains,
            select=select,
            follow=follow,
            order_by=order_by,
            limit=limit,
            cursor=cursor,
            name=name,
            params=params,
            status=status,
            claims=claims,
            budgets=budgets,
            receipt=receipt,
            at=_read_at(at),
            evaluation_time=evaluation_time,
        )

    @_tool
    def cruxible_query_spec(
        instance_id: InstanceId = None,
        *,
        spec: QueryDefinitionSpec,
        limit: Annotated[
            int, Field(ge=1, le=contracts.QUERY_MAX_LIMIT)
        ] = contracts.QUERY_DEFAULT_LIMIT,
        cursor: str | None = None,
        at: ReadAt = None,
        evaluation_time: str | None = None,
    ) -> contracts.QueryResultRecord:
        """Run one full QueryDefinition spec inline: the query verb's rows, flags and paging."""
        return handlers.handle_playbill_query_spec(
            require_instance_id(instance_id),
            spec=spec,
            limit=limit,
            cursor=cursor,
            at=_read_at(at),
            evaluation_time=evaluation_time,
        )

    @_tool
    def cruxible_procedure_readiness(
        instance_id: InstanceId = None,
        *,
        name: str,
        evaluation_time: str,
    ) -> contracts.ProcedureReadiness:
        """Inspect one accepted Procedure's bindings and executable profile."""
        return handlers.handle_playbill_procedure_readiness(
            require_instance_id(instance_id),
            name,
            evaluation_time=evaluation_time,
        )

    @_tool
    def cruxible_procedure_bind(
        instance_id: InstanceId = None,
        *,
        name: str,
        bindings: list[ProcedureSlotBindingRequest],
    ) -> contracts.ProcedureBindResult:
        """Propose exact accepted bindings for one Procedure's open slots."""
        return handlers.handle_playbill_procedure_bind(
            require_instance_id(instance_id),
            name,
            bindings=[item.model_dump(mode="json") for item in bindings],
        )

    @_tool
    def cruxible_procedure_run(
        instance_id: InstanceId = None,
        *,
        name: str,
        input: Annotated[
            JsonValue,
            Field(
                description="The Procedure's input; checked against its accepted input contract."
            ),
        ],
        evaluation_time: str | None = None,
        at: contracts.AcceptedCoordinate | None = None,
        resolution_contract: contracts.ResolutionContractReference | None = None,
        trigger_event: contracts.TriggerEventReference | None = None,
    ) -> contracts.ProcedureRunState:
        """Run one accepted Procedure deterministically."""
        return handlers.handle_playbill_procedure_run(
            require_instance_id(instance_id),
            name,
            evaluation_time=evaluation_time,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
            at=_dump(at),
            input=input,
        )

    @_tool
    def cruxible_procedure_run_status(
        instance_id: InstanceId = None,
        *,
        run_id: str,
    ) -> contracts.ProcedureRunState:
        """Read one durable Procedure run state and its exact next operation."""
        return handlers.handle_playbill_procedure_run_status(
            require_instance_id(instance_id), run_id
        )

    @_tool
    def cruxible_procedure_measure(
        instance_id: InstanceId = None,
        *,
        name: str,
        request: contracts.ProcedureMeasureRequest,
    ) -> contracts.ProcedureMeasureResult:
        """Evaluate due Procedure measurements from real evidence and credit one run."""
        return handlers.handle_playbill_procedure_measure(
            require_instance_id(instance_id), name, request
        )

    @_tool
    def cruxible_procedure_readings(
        instance_id: InstanceId = None,
        *,
        name: str,
        request: contracts.ProcedureReadingsRequest,
    ) -> contracts.ProcedureReadingsResult:
        """Inspect measurement standing and retained exact-grain readings."""
        return handlers.handle_playbill_procedure_readings(
            require_instance_id(instance_id), name, request
        )

    @_tool
    def cruxible_line_check(
        instance_id: InstanceId = None, *, line: str, request: contracts.LineTriggerCheckRequest
    ) -> contracts.LineTriggerCheckResult:
        """Check a Line's trigger and admitted occurrences; never enqueue or execute."""
        return handlers.handle_playbill_line_check(require_instance_id(instance_id), line, request)

    @_tool
    def cruxible_line_arm(
        instance_id: InstanceId = None,
        *,
        line: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.LineArm:
        """Arm a Line forward-only; the daemon admits what it matches under your credential."""
        return handlers.handle_playbill_line_arm(
            require_instance_id(instance_id), line, dry_run=dry_run, at=at
        )

    @_tool
    def cruxible_line_disarm(
        instance_id: InstanceId = None,
        *,
        line: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.LineArm:
        """Stop a Line admitting work on its own; admitted runs are not cancelled."""
        return handlers.handle_playbill_line_disarm(
            require_instance_id(instance_id), line, dry_run=dry_run, at=at
        )

    @_tool
    def cruxible_line_status(instance_id: InstanceId = None, *, line: str) -> contracts.LineArm:
        """Read a Line's current arm, or its last one and why it stopped."""
        return handlers.handle_playbill_line_status(require_instance_id(instance_id), line)

    @_tool
    def cruxible_line_evaluate(
        instance_id: InstanceId = None, *, line: str, request: contracts.LineEvaluateRequest
    ) -> contracts.LineTriggerCheckResult:
        """Explicitly evaluate a historical range into pending work; never execute."""
        return handlers.handle_playbill_line_evaluate(
            require_instance_id(instance_id), line, request
        )

    @_tool
    def cruxible_line_dispatch(
        instance_id: InstanceId = None, *, line: str, request: contracts.LineDispatchRequest
    ) -> contracts.LineDispatchResult:
        """Execute retained pending occurrences using the current authenticated actor."""
        return handlers.handle_playbill_line_dispatch(
            require_instance_id(instance_id), line, request
        )

    @_tool
    def cruxible_line_run(
        instance_id: InstanceId = None,
        *,
        line: str,
        trigger: str | None = None,
        evaluation_time: str | None = None,
        occurrence_id: str | None = None,
        resolution_contract: contracts.ResolutionContractReference | None = None,
        trigger_event: contracts.TriggerEventReference | None = None,
    ) -> contracts.ProcedureRunState:
        """Trigger one due occurrence of an accepted Line.

        Name the Trigger it fires on; omit it for a Line no Trigger aims at.
        """
        return handlers.handle_playbill_line_run(
            require_instance_id(instance_id),
            line,
            trigger=trigger,
            occurrence_id=occurrence_id,
            evaluation_time=evaluation_time,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
        )

    @_tool
    def cruxible_resolution_contracts(
        instance_id: InstanceId = None,
        *,
        claim_id: str | None = None,
        request: contracts.ResolutionContractsRequest | None = None,
    ) -> contracts.ResolutionContractsResult:
        """Find accepted resolution contracts testing a Claim, by Claim ID.

        The daemon resolves the Claim's accepted version. ``request`` is the
        advanced form carrying an exact hypothesis reference; pass one or the other.
        """
        if (claim_id is None) == (request is None):
            raise ValueError("pass exactly one of claim_id or request")
        return handlers.handle_playbill_resolution_contracts(
            require_instance_id(instance_id),
            request or contracts.ResolutionContractsRequest(hypothesis=cast(str, claim_id)),
        )

    @_tool
    def cruxible_predict(
        instance_id: InstanceId = None,
        *,
        request: contracts.PredictRequest,
    ) -> contracts.PredictResult:
        """Propose a governed test of an accepted Claim, named by Claim ID in the hypothesis."""
        return handlers.handle_playbill_predict(require_instance_id(instance_id), request)

    @_tool
    def cruxible_settle(
        instance_id: InstanceId = None,
        *,
        prediction_id: str,
        observation: str | None = None,
        request: contracts.SettleRequest | None = None,
    ) -> contracts.SettleResult:
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
            request or contracts.SettleRequest(observation=observation),
        )

    @_tool
    def cruxible_next(
        instance_id: InstanceId = None,
        *,
        evaluation_time: str | None = None,
        access_profile: CoverageAccessProfile | None = None,
        expiring_within: Annotated[
            CanonicalDuration | None,
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
        limit: Annotated[int | None, Field(ge=1, le=contracts.NEXT_MAX_LIMIT)] = None,
        cursor: str | None = None,
    ) -> contracts.NextResult:
        """Rank outstanding repair work with the exact next operation for each row."""
        return handlers.handle_playbill_next(
            require_instance_id(instance_id),
            evaluation_time=evaluation_time,
            access_profile=_dump(access_profile),
            expiring_within=_dump(expiring_within),
            since_result_digest=since_result_digest,
            limit=limit,
            cursor=cursor,
        )

    @_tool
    def cruxible_since(
        instance_id: InstanceId = None,
        *,
        generation: int,
        at: contracts.AcceptedCoordinate | None = None,
        access_profile: CoverageAccessProfile | None = None,
        max_rows: Annotated[int, Field(ge=1, le=1000)] = 100,
        max_bytes: Annotated[int, Field(ge=1, le=1_048_576)] = 65_536,
        cursor: contracts.SinceCursor | None = None,
    ) -> contracts.SinceResult:
        """Read accepted ChangeSet members after one generation."""
        return handlers.handle_playbill_since(
            require_instance_id(instance_id),
            generation=generation,
            at=_dump(at),
            access_profile=_dump(access_profile),
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=_dump(cursor),
        )

    @_tool
    def cruxible_curation_list(
        instance_id: InstanceId = None,
        *,
        evaluation_time: str,
        access_profile: CoverageAccessProfile | None = None,
        workspace_observation: NextWorkspaceObservation | None = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.CURATION_LIST_MAX_LIMIT)
        ] = contracts.CURATION_LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> contracts.CurationListResult:
        """List one page of curation patterns and ingest block observations.

        Pass next_cursor back while the result is truncated.
        """
        return handlers.handle_playbill_curation_list(
            require_instance_id(instance_id),
            evaluation_time=evaluation_time,
            access_profile=_dump(access_profile),
            workspace_observation=_dump(workspace_observation),
            limit=limit,
            cursor=cursor,
        )

    @_tool
    def cruxible_audit(
        instance_id: InstanceId = None,
        *,
        evaluation_time: str,
        access_profile: CoverageAccessProfile | None = None,
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
        cursor: contracts.AuditCursor | None = None,
    ) -> contracts.AuditResult:
        """Read ranked Claim verification work and record completed coverage."""
        return handlers.handle_playbill_audit(
            require_instance_id(instance_id),
            evaluation_time=evaluation_time,
            access_profile=_dump(access_profile),
            claim_type_identities=claim_type_identities or [],
            subject_kinds=subject_kinds or [],
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=_dump(cursor),
        )

    @_tool
    def cruxible_curation_overrule(
        instance_id: InstanceId = None,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        attribution_refs: list[str] | None = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.CurationActionResult:
        """Resolve one detector-version item as mechanically inapplicable."""
        return handlers.handle_playbill_curation_overrule(
            require_instance_id(instance_id),
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            attribution_refs=attribution_refs or [],
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_curation_accept_fixed(
        instance_id: InstanceId = None,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        accepted_proposal_id: str,
        accepted_changeset_digest: str,
        attribution_refs: list[str] | None = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.CurationActionResult:
        """Link one curation item to its exact accepted resolving ChangeSet."""
        return handlers.handle_playbill_curation_accept_fixed(
            require_instance_id(instance_id),
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=accepted_proposal_id,
            accepted_changeset_digest=accepted_changeset_digest,
            attribution_refs=attribution_refs or [],
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_curation_suppress(
        instance_id: InstanceId = None,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        scope: Literal["item", "pattern", "instance"],
        until_generation: int | None = None,
        attribution_refs: list[str] | None = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.CurationActionResult:
        """Hide curation work temporarily without resolving its detector facts."""
        return handlers.handle_playbill_curation_suppress(
            require_instance_id(instance_id),
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=scope,
            until_generation=until_generation,
            attribution_refs=attribution_refs or [],
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_coverage(
        instance_id: InstanceId = None,
        *,
        observations: Annotated[
            list[WorkingSourceObservation] | None,
            Field(description="Working-source observations you built; omit with bindings."),
        ] = None,
        bindings: Annotated[
            list[McpSourceBinding] | None,
            Field(
                description=(
                    "Path/source_id bindings; the adapter reads the selected workspace "
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
        budget: CoverageCardBudget | None = None,
        scan_budget: CoverageScanBudget | None = None,
    ) -> contracts.CoverageResult:
        """Resolve what working sources have to do with accepted state."""
        return handlers.handle_playbill_coverage(
            require_instance_id(instance_id),
            observations=(
                None
                if observations is None
                else [item.model_dump(mode="json") for item in observations]
            ),
            bindings=_source_bindings(bindings),
            files=tuple(files or ()),
            ranges=tuple(ranges or ()),
            grep_results_path=grep_results_path,
            whole_working_set=whole_working_set,
            budget=_dump(budget),
            scan_budget=_dump(scan_budget),
        )

    @_tool
    def cruxible_workspace_source_compile(
        instance_id: InstanceId = None,
        *,
        catalog_path: str,
        repository_root: str = ".",
        local_catalog_path: str | None = None,
        root_aliases: list[McpRootAlias] | None = None,
    ) -> SourceCompilationBundle:
        """Compile declared workspace sources against accepted daemon context."""
        return handlers.handle_playbill_workspace_source_compile(
            require_instance_id(instance_id),
            catalog_path=catalog_path,
            repository_root=repository_root,
            local_catalog_path=local_catalog_path,
            root_aliases=_root_aliases(root_aliases) or {},
        )

    @_tool
    def cruxible_floor_export(
        instance_id: InstanceId = None,
        *,
        mode: Annotated[
            handlers.FloorExportMode,
            Field(
                description=(
                    "bytes: return base64 files per floor path; write: verify and write "
                    ".cruxible/floor in the MCP workspace; status: report whether that "
                    "floor is current, stale, or missing."
                )
            ),
        ],
        force: Annotated[
            bool, Field(description="write only: replace a non-empty floor directory.")
        ] = False,
        include: Annotated[
            list[contracts.FloorExportPart] | None,
            Field(
                description=(
                    "bytes/write: opt-in floor parts. 'discovery' adds the discovery cards "
                    "(subjects/, claim-types/, procedures/, coverage-manifest.json) to the "
                    "grep-first current/, documents/ and provenance/."
                )
            ),
        ] = None,
    ) -> (
        contracts.FloorExport | contracts.WorkspaceFloorWriteResult | contracts.WorkspaceFloorStatus
    ):
        """Export the accepted greppable floor as bytes, write it locally, or report its status."""
        return handlers.handle_playbill_floor_export(
            require_instance_id(instance_id),
            mode=mode,
            force=force,
            include=tuple(include or ()),
        )

    return registered
