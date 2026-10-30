"""MCP registrations for the public surface."""

from __future__ import annotations

import asyncio
from datetime import datetime
from functools import wraps
from typing import Annotated, Any, Callable, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel
from pydantic.json_schema import SkipJsonSchema

from cruxible_client import contracts
from cruxible_client.authoring.inputs import AuthoringInput, ClaimInput
from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.authoring.models import (
    BlockDetachResult,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.authoring_landing import AuthoringAccept
from cruxible_client.contracts.builtin_claim_types import BuiltinClaimTypeName
from cruxible_client.contracts.capture_contract_inputs import CaptureContractInputRecord
from cruxible_client.contracts.capture_erasure import CaptureEraseRequest, CaptureEraseResult
from cruxible_client.contracts.capture_reads import CaptureRead, CaptureReadRequest
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.change_control import DryRun, PreviewAt
from cruxible_client.contracts.claim_attestations import ClaimAttestationAppendResult
from cruxible_client.contracts.compact_query import (
    QueryClaimStatus,
    QueryFilter,
    QueryFollow,
    QueryReceiptDetail,
)
from cruxible_client.contracts.declared_blocks import BlockRepinResult
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.duplicates import (
    DUPLICATES_AUDIT_DEFAULT_LIMIT,
    DUPLICATES_AUDIT_MAX_LIMIT,
    DuplicatesAuditResult,
)
from cruxible_client.contracts.file_catalog import FileCompilationBundle
from cruxible_client.contracts.get_reads import (
    GET_HISTORY_MAX_LIMIT,
    ByteRange,
    GetResult,
)
from cruxible_client.contracts.kits import (
    KitBuildRequest,
    KitBuildResult,
    KitBundle,
    KitChangeResult,
    KitRemoveRequest,
    KitStatus,
)
from cruxible_client.contracts.procedures.bindings import ProcedureBindings
from cruxible_client.contracts.provider_installation import (
    ProviderCatalog,
    ProviderInstallRequest,
    ProviderInstallResult,
    ProviderMoveRequest,
    ProviderMoveResult,
    ProviderRetireRequest,
    ProviderRetireResult,
)
from cruxible_client.contracts.query.definitions import QueryDefinitionSpec
from cruxible_client.contracts.query.grammar import QueryBudgets
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_client.contracts.write import (
    AddChange,
    CaptureEvidence,
    ContractEvidence,
    CreateChange,
    DocOp,
    ExpectedValue,
    FileEvidence,
    RebindChange,
    RetireChange,
    SectionEvidence,
    SelfEvidence,
    SetChange,
    SlotRef,
    WriteAccept,
    WriteOutcome,
    WriteRetireReason,
    WriteRole,
    WriteValue,
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


class McpBlockQuery(BaseModel):
    """One QueryDefinition backing of a block, with its parameter bindings."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    params: dict[str, JsonValue] = Field(default_factory=dict)


class McpRootAlias(BaseModel):
    """One named workspace root used by file catalog compilation."""

    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1)
    path: str = Field(min_length=1)


class McpSourceBinding(BaseModel):
    """One workspace path bound to a logical source, overriding the catalog."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    source: str = Field(
        min_length=1,
        description="The source as PLANE:IDENTITY: external:NAME or ledger:PATH.",
    )


class McpProcedureBindings(RootModel[ProcedureBindings]):
    """Provider choices keyed by the accepted Procedure's declared slot names."""


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
    result = {row.path: row.source for row in rows}
    if len(result) != len(rows):
        raise ValueError("bindings names a path more than once")
    return result


class McpSectionEvidence(SectionEvidence):
    """Cite a Markdown heading path."""

    observation: SkipJsonSchema[WorkingSelectionObservation | None] = None


class McpFileEvidence(FileEvidence):
    """Cite catalogued file text; the adapter reads the file."""

    observation: SkipJsonSchema[WorkingSelectionObservation | None] = None


McpEvidence = Annotated[
    SelfEvidence | CaptureEvidence | McpFileEvidence | ContractEvidence | McpSectionEvidence,
    Field(discriminator="kind"),
]
_EVIDENCE_DEFAULT = "Default: the write's `because` as self evidence."
_HOLD_DESCRIPTION = (
    "Sign only: leave activating a candidate this approval completes to someone else."
)


class McpSetChange(SetChange):
    __doc__ = SetChange.__doc__

    evidence: McpEvidence | None = Field(default=None, description=_EVIDENCE_DEFAULT)


class McpAddChange(AddChange):
    __doc__ = AddChange.__doc__

    evidence: McpEvidence | None = Field(default=None, description=_EVIDENCE_DEFAULT)


class McpRebindChange(RebindChange):
    __doc__ = RebindChange.__doc__

    target: str = Field(description="CLM-…")
    # MCP cites file and section text only, never a fresh Capture, so the
    # Capture-wide re-bind (which needs one) is not offered here.
    from_capture: SkipJsonSchema[str | None] = None
    citation: str | None = Field(default=None, description="Its 12+ hex; default: one of FILE.")
    evidence: McpFileEvidence | McpSectionEvidence


McpChange = Annotated[
    CreateChange | McpSetChange | McpAddChange | RetireChange | McpRebindChange,
    Field(discriminator="op"),
]


def _observed_rebinds(changes: list[Any]) -> list[Any]:
    """Each re-bind's new text, read from the MCP workspace before the write leaves here."""

    if not any(isinstance(item, RebindChange) for item in changes):
        return changes
    from cruxible_client.authoring.write_evidence import observe_evidence
    from cruxible_core.mcp.workspace import mcp_workspace_root

    root = mcp_workspace_root()
    return [
        item.model_copy(update={"evidence": observe_evidence(item.evidence, workspace=root)})
        if isinstance(item, RebindChange)
        else item
        for item in changes
    ]


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
    def cruxible_provider_list(
        instance_id: InstanceId = None,
    ) -> ProviderCatalog:
        """List installed Providers, then the configured repository's packages, with interfaces."""
        return handlers.handle_playbill_provider_catalog(require_instance_id(instance_id))

    @_tool
    def cruxible_provider_install(
        instance_id: InstanceId = None,
        *,
        request: ProviderInstallRequest,
    ) -> ProviderInstallResult:
        """Install a provider package and register it; lands when policy needs no approval."""
        return handlers.handle_playbill_provider_install(require_instance_id(instance_id), request)

    @_tool
    def cruxible_provider_retire(
        instance_id: InstanceId = None,
        *,
        request: ProviderRetireRequest,
    ) -> ProviderRetireResult:
        """Retire an unpinned provider build, retaining its environment."""
        return handlers.handle_playbill_provider_retire(require_instance_id(instance_id), request)

    @_tool
    def cruxible_provider_move(
        instance_id: InstanceId = None,
        *,
        request: ProviderMoveRequest,
    ) -> ProviderMoveResult:
        """Move Lines to a provider build; preview every re-pin and automation it stops."""
        return handlers.handle_playbill_provider_move(require_instance_id(instance_id), request)

    @_tool
    def cruxible_kit_build(
        instance_id: InstanceId = None,
        *,
        request: KitBuildRequest,
    ) -> KitBuildResult:
        """Export this instance's definitions under the owned prefixes as one kit release."""
        return handlers.handle_playbill_kit_build(require_instance_id(instance_id), request)

    @_tool
    def cruxible_kit_status(
        instance_id: InstanceId = None,
        *,
        offline: Annotated[
            bool, Field(description="true: skip looking up newer releases in the registry.")
        ] = False,
    ) -> KitStatus:
        """List installed kits, paths edited since install, and newer releases."""
        return handlers.handle_playbill_kit_status(
            require_instance_id(instance_id), offline=offline
        )

    @_tool
    def cruxible_kit_add(
        instance_id: InstanceId = None,
        *,
        reference: Annotated[
            str | None,
            Field(
                description=(
                    "A registry reference (project-state:1.0.0 or "
                    "ghcr.io/acme/kits/foo@sha256:...); this adapter pulls and verifies it. "
                    "Or give bundle."
                )
            ),
        ] = None,
        bundle: Annotated[
            KitBundle | None, Field(description="An inline kit bundle; or give reference.")
        ] = None,
        keep: Annotated[
            list[str] | None,
            Field(description="Definitions (KIND:NAME) to keep as this instance has them."),
        ] = None,
        keep_local_edits: Annotated[
            bool, Field(description="Keep every definition edited here since install.")
        ] = False,
        retire_dependents: Annotated[
            list[str] | None,
            Field(description="Dropped definitions to retire with their live dependents."),
        ] = None,
        keep_build: Annotated[
            list[str] | None,
            Field(description="Lines (Line:NAME) to keep on their current provider builds."),
        ] = None,
        allow_downgrade: bool = False,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> KitChangeResult:
        """Install or upgrade a kit as one change set; it always proposes."""
        return handlers.handle_playbill_kit_add(
            require_instance_id(instance_id),
            reference=reference,
            bundle=bundle,
            keep=keep or (),
            keep_local_edits=keep_local_edits,
            retire_dependents=retire_dependents or (),
            keep_build=keep_build or (),
            allow_downgrade=allow_downgrade,
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_kit_remove(
        instance_id: InstanceId = None,
        *,
        request: KitRemoveRequest,
    ) -> KitChangeResult:
        """Retire every artifact a kit installed: lands at once when the approval policy
        requires no approval, otherwise it stops at proposed (previews by default)."""
        return handlers.handle_playbill_kit_remove(require_instance_id(instance_id), request)

    @_tool
    def cruxible_body_store(
        instance_id: InstanceId = None, *, content_base64: str
    ) -> contracts.CasObjectResult:
        """Store inert exact body bytes."""
        return handlers.handle_playbill_store_body(require_instance_id(instance_id), content_base64)

    @_tool
    def cruxible_document_propose(
        instance_id: InstanceId = None,
        *,
        shell: DocumentShell,
        proposal_name: str,
        source_compilation_digest: str | None = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
        accept: AuthoringAccept = "if_allowed",
    ) -> contracts.ProposalInspection:
        """Propose a governed Document create or supersession; lands when allowed."""
        return handlers.handle_playbill_propose_document(
            require_instance_id(instance_id),
            shell.model_dump(mode="json"),
            proposal_name,
            source_compilation_digest,
            dry_run=dry_run,
            at=at,
            accept=accept,
        )

    @_tool
    def cruxible_proposal_review(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        include_body: bool = False,
        item_reasons_after: str | None = None,
        item_reasons_limit: int | None = None,
    ) -> contracts.ProposalReview:
        """Render a structured candidate review; a Procedure proposal's item reasons page."""
        return handlers.handle_playbill_review(
            require_instance_id(instance_id),
            proposal_id,
            include_body=include_body,
            item_reasons_after=item_reasons_after,
            item_reasons_limit=item_reasons_limit,
        )

    @_tool
    def cruxible_proposal_approve_prepare(
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
    def cruxible_proposal_approve_submit(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        attestation: ApprovalAttestation,
        hold: Annotated[bool, Field(description=_HOLD_DESCRIPTION)] = False,
    ) -> contracts.ProposalApprovalResult:
        """Submit an approval; stale lowering origins return a fresh proposal to review."""
        return handlers.handle_playbill_submit_approval(
            require_instance_id(instance_id),
            proposal_id,
            attestation.model_dump(mode="json"),
            hold=hold,
        )

    @_tool
    def cruxible_proposal_approve(
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
        hold: Annotated[bool, Field(description=_HOLD_DESCRIPTION)] = False,
    ) -> contracts.ProposalApprovalResult:
        """Approve with a local key; stale lowering origins recompile for fresh review."""
        return handlers.handle_playbill_approve(
            require_instance_id(instance_id),
            proposal_id,
            signer_id=signer_id,
            candidate_digest=candidate_digest,
            hold=hold,
        )

    @_tool
    def cruxible_proposal_activate(
        instance_id: InstanceId = None, *, proposal_id: str
    ) -> contracts.ProposalActivationResult:
        """Activate a candidate; stale lowering origins recompile at head under normal policy.

        The daemon delivers the floor to a workspace it serves; exact reads after
        activation use get or query at the receipt's coordinate.
        """
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
            Field(description="One Subject kind to read in full, e.g. 'project.work_item'."),
        ] = None,
        section: Annotated[
            contracts.OrientSection | None,
            Field(
                description=(
                    "Page one family as rows; graph: Subjects+edges; pins: what pins what; "
                    "activity: what just happened, newest first; sources: each logical "
                    "source's landings, latest landing and contracts."
                )
            ),
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
        status: Literal["open", "awaiting_readmit", "settled", "incomplete"] | None = None,
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
        """Recompile retained input at head; other origins readmit their exact bytes."""
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
    def cruxible_proposal_reject(
        instance_id: InstanceId = None,
        *,
        proposal_id: str,
        reason: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.ProposalRejectResult:
        """Reject one open proposal as its reviewer, recording why."""
        return handlers.handle_playbill_reject_proposal(
            require_instance_id(instance_id), proposal_id, reason, dry_run=dry_run, at=at
        )

    @_tool
    def cruxible_capture_read(
        instance_id: InstanceId = None, *, request: CaptureReadRequest
    ) -> CaptureRead:
        """Read exact retained Capture evidence with a byte budget and body permission."""
        return handlers.handle_playbill_read_capture(require_instance_id(instance_id), request)

    @_tool
    def cruxible_capture_erase(
        instance_id: InstanceId = None,
        *,
        capture: Annotated[str, Field(description="A Capture digest or its CAP- handle.")],
        because: Annotated[
            str, Field(description="Why, recorded and shown; never repeat the erased bytes.")
        ],
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> CaptureEraseResult:
        """Erase one Capture's retained body (bytes gone, envelope and record kept)."""
        return handlers.handle_playbill_erase_capture(
            require_instance_id(instance_id),
            CaptureEraseRequest(capture_digest=capture, because=because, dry_run=dry_run, at=at),
        )

    @_tool
    def cruxible_files_check(
        instance_id: InstanceId = None,
        *,
        bundle: Annotated[
            FileCompilationBundle | None,
            Field(description="A compiled bundle to check as given; omit to compile the catalog."),
        ] = None,
        catalog_path: Annotated[
            str | None,
            Field(description="The portable catalog; omitted, the workspace's is discovered."),
        ] = None,
        repository_root: str = ".",
        local_catalog_path: str | None = None,
        root_aliases: list[McpRootAlias] | None = None,
    ) -> contracts.FileCheckResult:
        """Compare catalogued workspace files (or a bundle) with accepted state."""
        return handlers.handle_playbill_files_check(
            require_instance_id(instance_id),
            bundle=None if bundle is None else bundle.model_dump(mode="json"),
            catalog_path=catalog_path,
            repository_root=repository_root,
            local_catalog_path=local_catalog_path,
            root_aliases=_root_aliases(root_aliases),
        )

    @_tool
    def cruxible_files_propose(
        instance_id: InstanceId = None,
        *,
        file: Annotated[str, Field(description="The catalog file to propose.")],
        proposal_name: str,
        catalog_path: Annotated[
            str | None,
            Field(description="The portable catalog; omitted, the workspace's is discovered."),
        ] = None,
        repository_root: str = ".",
        local_catalog_path: str | None = None,
        root_aliases: list[McpRootAlias] | None = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
        accept: AuthoringAccept = "if_allowed",
    ) -> contracts.ProposalInspection:
        """Propose one catalogued file as its Document; lands when allowed."""
        return handlers.handle_playbill_files_propose(
            require_instance_id(instance_id),
            file=file,
            proposal_name=proposal_name,
            catalog_path=catalog_path,
            repository_root=repository_root,
            local_catalog_path=local_catalog_path,
            root_aliases=_root_aliases(root_aliases),
            dry_run=dry_run,
            at=at,
            accept=accept,
        )

    @_tool
    def cruxible_compiler_upgrade(
        instance_id: InstanceId = None,
        *,
        target_compiler_digest: Annotated[
            str | None,
            Field(description="Target compiler digest; omit for the compiler the daemon runs."),
        ] = None,
        base: Annotated[
            contracts.AcceptedCoordinate | None,
            Field(description="Accepted coordinate to bind; omit for the current head."),
        ] = None,
        proposal_name: Annotated[
            str | None, Field(description="Omit for upgrade-to-<revision label>.")
        ] = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.ProposalInspection:
        """Propose an admin-only compiler upgrade; review, approve and activate separately."""
        return handlers.handle_playbill_compiler_upgrade(
            require_instance_id(instance_id),
            target_compiler_digest,
            None if base is None else base.model_dump(mode="json"),
            proposal_name,
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_principal_propose(
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
    def cruxible_capture_contract_propose(
        instance_id: InstanceId = None,
        *,
        input: CaptureContractInputRecord,
        proposal_name: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
        accept: AuthoringAccept = "if_allowed",
    ) -> contracts.ProposalInspection:
        """Propose a CaptureContract: name and shape, optional policy or budget; edits merge."""
        return handlers.handle_playbill_propose_capture_contract(
            require_instance_id(instance_id),
            input.model_dump(mode="json", exclude_unset=True),
            proposal_name,
            dry_run=dry_run,
            at=at,
            accept=accept,
        )

    @_tool
    def cruxible_claim_type_propose(
        instance_id: InstanceId = None,
        *,
        input: ClaimTypeInputRecord | None = None,
        builtin: BuiltinClaimTypeName | None = None,
        proposal_name: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
        accept: AuthoringAccept = "if_allowed",
    ) -> contracts.ClaimTypeInputProposalResult:
        """Propose one governed ClaimType, or adopt a builtin by name; lands when allowed."""
        if (input is None) == (builtin is None):
            raise ValueError("pass exactly one of input or builtin")
        if builtin is not None:
            return handlers.handle_playbill_adopt_builtin_claim_type(
                require_instance_id(instance_id),
                builtin,
                proposal_name,
                dry_run=dry_run,
                at=at,
                accept=accept,
            )
        assert input is not None
        return handlers.handle_playbill_propose_claim_type(
            require_instance_id(instance_id),
            input.model_dump(mode="json"),
            proposal_name,
            dry_run=dry_run,
            at=at,
            accept=accept,
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
    def cruxible_authoring_list(
        instance_id: InstanceId = None,
    ) -> contracts.AuthoringIntentListRecord:
        """List the authenticated writer's pending intents."""
        return handlers.handle_playbill_authoring_list(require_instance_id(instance_id))

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
        occurrence: Annotated[
            int | None,
            Field(ge=1, description="The 1-based anchor occurrence when it is not unique."),
        ] = None,
    ) -> contracts.AuthoringPreflightResult:
        """Bind one exact workspace anchor as evidence and compile the Claim it observes."""
        return handlers.handle_playbill_authoring_bind(
            require_instance_id(instance_id),
            source_path=source_path,
            anchor=anchor,
            payload=payload,
            window_lines=window_lines,
            occurrence=occurrence,
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
    def cruxible_authoring_submit(
        instance_id: InstanceId = None,
        *,
        payload: AuthoringInput | None = None,
        intent_id: str | None = None,
        accept: AuthoringAccept = "if_allowed",
    ) -> contracts.AuthoringSubmitResultRecord:
        """Compile and submit a payload in one call, or submit a staged intent by ID.

        With both, the payload replaces that staged intent's payload first. A
        refused preflight returns the unsubmitted intent with its diagnostics.
        Lands when policy and caller allow; accept="never" keeps the proposal.
        Idempotent: resubmitting an accepted intent reports its coordinate.
        """
        return handlers.handle_playbill_authoring_submit(
            require_instance_id(instance_id),
            payload=None if payload is None else payload.model_dump(mode="json"),
            intent_id=intent_id,
            accept=accept,
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
    def cruxible_block_repin(
        instance_id: InstanceId = None,
        *,
        block: Annotated[str, Field(description="The block id in its marker.")],
        file: Annotated[str, Field(description="The page's file catalog name.")],
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
        render: Annotated[
            bool,
            Field(description="true: write the body as a table or list from its one query."),
        ] = False,
        dry_run: Annotated[
            bool | None, Field(description="true: compute the stamp and write nothing.")
        ] = None,
    ) -> BlockRepinResult:
        """Stamp (or restamp) one projection block; this adapter computes the stamp."""
        return handlers.handle_playbill_block_repin(
            require_instance_id(instance_id),
            block=block,
            file=file,
            claims=claims,
            queries=None
            if queries is None
            else [(item.query, dict(item.params)) for item in queries],
            artifacts=artifacts,
            currency_policy=currency_policy,
            backing_digest=backing_digest,
            render=render,
            dry_run=dry_run,
        )

    @_tool
    def cruxible_block_sync(
        instance_id: InstanceId = None,
        *,
        files: Annotated[
            list[str] | None, Field(description="Pages to check, workspace-relative.")
        ] = None,
        all_files: Annotated[
            bool, Field(description="Check every page the file catalog names.")
        ] = False,
    ) -> contracts.BlockSyncResult:
        """Check each block's backings against the instance; reads only, edits no page."""
        return handlers.handle_playbill_block_sync(
            require_instance_id(instance_id),
            files=files or (),
            all_files=all_files,
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
        file_id: Annotated[
            str,
            Field(
                description=(
                    "The stored projection file identity; the file and catalog entry "
                    "need not still exist."
                ),
            ),
        ],
        block_id: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.BlockDepublishResult:
        """Release the publication registration that demands one page block."""
        return handlers.handle_playbill_block_depublish(
            require_instance_id(instance_id), file_id, block_id, dry_run=dry_run, at=at
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
                    "a predicate, ClaimType:/Document:/Procedure:/query:/"
                    "CaptureContract:<name>, "
                    "an artifact path, a proposal id or prefix, or an operational reference: "
                    "Line:<name> (or the Line identity digest next names), CAP-<12+ hex> or "
                    "Capture:<digest>, ResolutionContract:<name>, Mandate:<name>."
                )
            ),
        ],
        detail: Literal[
            "summary", "evidence", "why", "history", "incoming", "runs", "proof", "body"
        ] = "summary",
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
                description="Rows per paged detail; default 20.",
            ),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="next_cursor of the previous page."),
        ] = None,
        field: Annotated[
            str | None,
            Field(description='detail="history" on a Subject: only this field\'s events.'),
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
            field=field,
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
            WriteValue,
            Field(
                description=(
                    "The value: an enum member, text, number or boolean; a Subject as kind/id "
                    "for a Subject-valued field; the text itself for exact content; "
                    '{"none": null} when the field holds nothing.'
                )
            ),
        ],
        because: Annotated[str, Field(description="Why; also the default evidence.")],
        evidence: Annotated[
            McpEvidence | None,
            Field(
                description=(
                    "Default: self evidence from because; otherwise cite a Capture, "
                    "CaptureContract or workspace file."
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
                    'CLM-…, a Subject kind/id, or {"subject", "field"} (+"value" for one '
                    "of several)."
                )
            ),
        ],
        because: Annotated[str, Field(description="Why it ends.")],
        with_claims: Annotated[
            bool, Field(description="A Subject target: also retire its live Claims.")
        ] = False,
        reason: Annotated[
            WriteRetireReason,
            Field(
                description=("was-rescinded (withdrawn), was-wrong (it was false), or superseded.")
            ),
        ] = "was-rescinded",
        expect: Annotated[
            ExpectedValue | None,
            Field(
                description=("Compare-and-set: the live value(s), [] for none; else slot_changed.")
            ),
        ] = None,
        dry_run: Annotated[bool, Field(description="Check everything; write nothing.")] = False,
        accept: Annotated[
            WriteAccept,
            Field(description="if_allowed: accept when policy lets you; never: propose."),
        ] = "if_allowed",
        at: Annotated[
            str | None,
            Field(description="The coordinate you read at."),
        ] = None,
    ) -> WriteOutcome:
        """End one live Claim, and what depends on it, or a Subject, in one change set."""
        return handlers.handle_playbill_retire(
            require_instance_id(instance_id),
            target=target,
            because=because,
            with_claims=with_claims,
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
            list[McpChange] | None,
            Field(
                description=(
                    'Refer to a create\'s Subject as {"created": label} in subject or value, '
                    "in any order."
                ),
            ),
        ] = None,
        because: Annotated[str, Field(description="Why: the change set's rationale.")],
        docs: list[DocOp] | None = None,
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
        """Write doc sections and Claims together. Cite prose with kind=section evidence."""
        return handlers.handle_playbill_write(
            require_instance_id(instance_id),
            changes=_observed_rebinds(changes or []),
            docs=docs or [],
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
        bindings: McpProcedureBindings | None = None,
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
            bindings=None if bindings is None else bindings.root,
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
    def cruxible_procedure_dispose(
        instance_id: InstanceId = None,
        *,
        name: str,
        request: contracts.ProcedureResolutionDispositionRequest,
    ) -> contracts.ProcedureResolutionDispositionResult:
        """Record your upheld/overturned judgment and reason on a measurement resolution.

        Find resolution_id in procedure readings or get Procedure:NAME. Core
        records your judgment; overturn reopens the latest answer's contract.
        """
        return handlers.handle_playbill_procedure_dispose(
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
    def cruxible_line_enable(
        instance_id: InstanceId = None,
        *,
        line: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.LineEnablement:
        """Enable a Line forward-only; the daemon admits what it matches under your credential."""
        return handlers.handle_playbill_line_enable(
            require_instance_id(instance_id), line, dry_run=dry_run, at=at
        )

    @_tool
    def cruxible_line_disable(
        instance_id: InstanceId = None,
        *,
        line: str,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.LineEnablement:
        """Stop a Line admitting work on its own; admitted runs are not cancelled."""
        return handlers.handle_playbill_line_disable(
            require_instance_id(instance_id), line, dry_run=dry_run, at=at
        )

    @_tool
    def cruxible_line_evaluate(
        instance_id: InstanceId = None, *, line: str, request: contracts.LineEvaluateRequest
    ) -> contracts.LineEvaluateResult:
        """Evaluate a Line's Triggers over a range into pending work; never execute.

        ``request.dry_run`` only reports what the range makes eligible (a read).
        """
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
    def cruxible_inbox_list(
        instance_id: InstanceId = None,
        *,
        line: str | None = None,
        status: contracts.InboxItemStatus | None = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.INBOX_LIST_MAX_LIMIT)
        ] = contracts.INBOX_LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> contracts.InboxList:
        """List one page of Line inbox items; pass next_cursor back while truncated."""
        return handlers.handle_playbill_inbox_list(
            require_instance_id(instance_id), line=line, status=status, limit=limit, cursor=cursor
        )

    @_tool
    def cruxible_inbox_get(instance_id: InstanceId = None, *, item_id: str) -> contracts.InboxItem:
        """Read one inbox item whole: envelope, payload as posted, and its latest dispositions."""
        return handlers.handle_playbill_inbox_get(require_instance_id(instance_id), item_id)

    @_tool
    def cruxible_inbox_ack(
        instance_id: InstanceId = None, *, item_id: str
    ) -> contracts.InboxDispositionResult:
        """Acknowledge one inbox item: seen, and owed nothing further."""
        return handlers.handle_playbill_inbox_ack(require_instance_id(instance_id), item_id)

    @_tool
    def cruxible_inbox_dismiss(
        instance_id: InstanceId = None, *, item_id: str, reason: str
    ) -> contracts.InboxDispositionResult:
        """Dismiss one inbox item, recording why it needs nothing."""
        return handlers.handle_playbill_inbox_dismiss(
            require_instance_id(instance_id), item_id, reason
        )

    @_tool
    def cruxible_inbox_link(
        instance_id: InstanceId = None, *, item_id: str, target: contracts.InboxLinkTarget
    ) -> contracts.InboxDispositionResult:
        """Link one inbox item to the proposal or Subject that records your judgment of it."""
        return handlers.handle_playbill_inbox_link(
            require_instance_id(instance_id), item_id, target
        )

    @_tool
    def cruxible_line_run(
        instance_id: InstanceId = None,
        *,
        line: str,
        event: contracts.TriggerEventReference | None = None,
        repeat: bool = False,
        evaluation_time: str | None = None,
        occurrence_id: str | None = None,
        resolution_contract: contracts.ResolutionContractReference | None = None,
    ) -> contracts.ProcedureRunState:
        """Run an accepted Line once now, as a manual occurrence; never a Trigger's.

        ``event`` is the event input for a Line whose Procedure takes one;
        ``repeat`` runs an event its Triggers already admitted again.
        ``occurrence_id`` asserts this admission, including its daemon instant;
        use line_dispatch to select a retained pending occurrence.
        """
        return handlers.handle_playbill_line_run(
            require_instance_id(instance_id),
            line,
            occurrence_id=occurrence_id,
            evaluation_time=evaluation_time,
            resolution_contract=resolution_contract,
            event=event,
            repeat=repeat,
        )

    @_tool
    def cruxible_prediction_list(
        instance_id: InstanceId = None,
        *,
        claim: str,
    ) -> contracts.ResolutionContractsResult:
        """List the accepted predictions that test one Claim, by Claim ID.

        The daemon resolves the Claim's current accepted version and returns the
        accepted resolution contracts whose hypothesis is that exact version.
        Pending predictions and settlement outcomes are read with ``cruxible_get``
        (``ResolutionContract:<name>``) and ``cruxible_next``.
        """
        return handlers.handle_playbill_prediction_list(
            require_instance_id(instance_id),
            contracts.ResolutionContractsRequest(hypothesis=claim),
        )

    @_tool
    def cruxible_prediction_propose(
        instance_id: InstanceId = None,
        *,
        request: contracts.PredictRequest,
    ) -> contracts.PredictResult:
        """Propose a governed test of an accepted Claim, named by Claim ID in the hypothesis."""
        return handlers.handle_playbill_predict(require_instance_id(instance_id), request)

    @_tool
    def cruxible_prediction_settle(
        instance_id: InstanceId = None,
        *,
        prediction_id: str,
        observation: str,
    ) -> contracts.SettleResult:
        """Settle one prediction from the Claim ID of an accepted observation.

        ``prediction_id`` is the contract name or the RSC-... window id ``next``
        names; ``observation`` is the settling Claim's ID. Advanced typed
        settlement remains available through the SDK and HTTP.
        """
        return handlers.handle_playbill_settle_prediction(
            require_instance_id(instance_id),
            prediction_id,
            contracts.SettleRequest(observation=observation),
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
        access_profile: CoverageAccessProfile | None = None,
        limit: Annotated[
            int, Field(ge=1, le=contracts.CURATION_LIST_MAX_LIMIT)
        ] = contracts.CURATION_LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> contracts.CurationListResult:
        """List one page of the curation queue detection recorded; a pure read.

        Pass next_cursor back while the result is truncated.
        """
        return handlers.handle_playbill_curation_list(
            require_instance_id(instance_id),
            access_profile=_dump(access_profile),
            limit=limit,
            cursor=cursor,
        )

    @_tool
    def cruxible_curation_duplicates(
        instance_id: InstanceId = None,
        *,
        kind: str,
        limit: Annotated[
            int, Field(ge=1, le=DUPLICATES_AUDIT_MAX_LIMIT)
        ] = DUPLICATES_AUDIT_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> DuplicatesAuditResult:
        """List possible duplicates of one kind for you to resolve; a pure, bounded read.

        Pass next_cursor back while the result is truncated.
        """
        return handlers.handle_playbill_curation_duplicates(
            require_instance_id(instance_id), kind=kind, limit=limit, cursor=cursor
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
        accepted_proposal_id: Annotated[
            str | None, Field(description="The fixing proposal; or give accepted_generation.")
        ] = None,
        accepted_changeset_digest: Annotated[
            str | None, Field(description="Pins accepted_proposal_id to its ChangeSet.")
        ] = None,
        accepted_generation: Annotated[
            int | None, Field(ge=1, description="The fixing accepted generation.")
        ] = None,
        attribution_refs: list[str] | None = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.CurationActionResult:
        """Link one curation item to the accepted change that fixed it."""
        return handlers.handle_playbill_curation_accept_fixed(
            require_instance_id(instance_id),
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=accepted_proposal_id,
            accepted_changeset_digest=accepted_changeset_digest,
            accepted_generation=accepted_generation,
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
        scope: Annotated[
            Literal["item", "lineage"],
            Field(description="item: this item; lineage: also its pattern's successors."),
        ],
        until_generation: int | None = None,
        attribution_refs: list[str] | None = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.CurationActionResult:
        """Hide an item or its lineage without resolving it."""
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
    def cruxible_curation_unsuppress(
        instance_id: InstanceId = None,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        suppression_event_id: Annotated[
            str | None, Field(description="Needed only when the item carries several.")
        ] = None,
        attribution_refs: list[str] | None = None,
        dry_run: DryRun = None,
        at: PreviewAt = None,
    ) -> contracts.CurationActionResult:
        """Lift a suppression on an item, so what it hid is listed again."""
        return handlers.handle_playbill_curation_unsuppress(
            require_instance_id(instance_id),
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            suppression_event_id=suppression_event_id,
            attribution_refs=attribution_refs or [],
            dry_run=dry_run,
            at=at,
        )

    @_tool
    def cruxible_coverage_resolve(
        instance_id: InstanceId = None,
        *,
        observations: Annotated[
            list[WorkingSourceObservation] | None,
            Field(description="Working-source observations you built; omit to select files."),
        ] = None,
        bindings: Annotated[
            list[McpSourceBinding] | None,
            Field(
                description=(
                    "Bindings overriding the file catalog, path by path; the adapter reads "
                    "the selected workspace files (files, ranges, grep_results, or "
                    "whole_working_set)."
                )
            ),
        ] = None,
        files: list[str] | None = None,
        ranges: list[str] | None = None,
        grep_results: Annotated[
            str | None,
            Field(
                description=(
                    "Grep output (path:line:text lines) selecting the ranges to cover, "
                    "passed inline."
                )
            ),
        ] = None,
        whole_working_set: Annotated[
            bool, Field(description="Cover every bound workspace file.")
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
            grep_results=grep_results,
            whole_working_set=whole_working_set,
            budget=_dump(budget),
            scan_budget=_dump(scan_budget),
        )

    @_tool
    def cruxible_files_compile(
        instance_id: InstanceId = None,
        *,
        catalog_path: Annotated[
            str | None,
            Field(description="The portable catalog; omitted, the workspace's is discovered."),
        ] = None,
        repository_root: str = ".",
        local_catalog_path: str | None = None,
        root_aliases: list[McpRootAlias] | None = None,
    ) -> FileCompilationBundle:
        """Compile catalogued workspace files against accepted daemon context."""
        return handlers.handle_playbill_files_compile(
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
                    "grep-first current/, changes/ and README."
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
