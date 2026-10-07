"""Runtime facade shared by HTTP routes and MCP handlers.

This module is intentionally independent of the legacy graph/config runtime.
Public surfaces translate transport contracts here, then delegate to the
typed Cruxible services.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

from pydantic import TypeAdapter, ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.authoring.inputs import AuthoringInput
from cruxible_client.contracts.authoring.models import (
    AuthoringExpectation,
    AuthoringPayload,
    AuthoringProgramStamp,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimAuthoringPayloadV2,
    ClaimTypeSuccessionMember,
    PreflightResult,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.capture_reads import CaptureRead, CaptureReadRequest
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequest,
    ClaimAttestationAppendResult,
)
from cruxible_client.contracts.claim_reads import (
    ClaimBackingsRequest,
    ClaimBackingsResult,
    ClaimReadBatchRequest,
    ClaimReadBatchResult,
)
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
)
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.codes import normalize_code
from cruxible_client.contracts.declared_blocks import ProjectionBlockStampAny
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.errors import (
    BootstrapError,
)
from cruxible_client.contracts.floor import FloorDelta
from cruxible_client.contracts.get_reads import (
    GetBatchRequest,
    GetBatchResult,
    GetRequest,
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
from cruxible_client.contracts.ledger_mirror import (
    LedgerMirrorUnset,
    validate_mirror_url,
)
from cruxible_client.contracts.orient import (
    ORIENT_DEFAULT_LIMIT,
    OrientResult,
    OrientSection,
    OrientSurface,
)
from cruxible_client.contracts.predictions import (
    PredictRequest,
    PredictResult,
    SettleRequest,
    SettleResult,
)
from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.principals import AuthoringRefusal
from cruxible_client.contracts.procedures.artifacts import procedure_path
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreview,
    ProcedureSourcePreviewRequest,
)
from cruxible_client.contracts.provider_installation import (
    ProviderCatalog,
    ProviderInstallRequest,
    ProviderInstallResult,
)
from cruxible_client.contracts.query.definitions import query_definition_path
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_client.contracts.temporal import format_datetime, utc_now
from cruxible_client.contracts.types import (
    CompilerCoordinate,
    GitObjectFormat,
    OperatingProfile,
    PrincipalRecord,
)
from cruxible_client.contracts.validation_messages import validation_lines, validation_summary
from cruxible_client.contracts.write import (
    RetireRequest,
    SetRequest,
    WriteOutcome,
    WriteRequest,
    as_write_request,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord, lint_claim_type_input
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeMigrationRequestAny,
    lint_claim_type_successors,
    service_migrate_claim_type,
)
from cruxible_core.coverage.adapter import WorkingSourceObservation
from cruxible_core.coverage.contracts import CoverageCardBudget
from cruxible_core.coverage.indexes import CoverageScanBudget
from cruxible_core.documents.workspace_file import WorkspaceFileReadRefused
from cruxible_core.errors import (
    AuthenticationError,
    ConfigError,
    DataValidationError,
    PrincipalRefusedError,
)
from cruxible_core.exhaust.consumption import (
    ConsumptionContextV1,
    ConsumptionOperation,
    consumption_artifacts_for_dependency_closure,
    consumption_artifacts_for_paths,
    consumption_receipts_enabled,
    note_consumption_unobserved,
    record_consumption,
)
from cruxible_core.floor.workspace_advertisement import workspace_git_object_format
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.ledger.ledger_mirror import LedgerMirrorStateV1
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.providers.provider_classifiers import PROVIDER_BUCKET_CLASSIFIER_REGISTRY
from cruxible_core.runtime.execution_policy import (
    enforce_customer_code_execution_supported,
)
from cruxible_core.runtime.host_api import attach_workspace
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.permissions import (
    PERMISSION_REQUIREMENTS,
    PermissionMode,
    check_permission,
    current_request_instance_scope,
    get_current_mode,
)
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.actor_identity import local_operator_actor_context
from cruxible_core.server.auth import (
    ResolvedAuthContext,
    get_current_auth_context,
    set_current_operation_id,
)
from cruxible_core.server.config import is_server_auth_enabled
from cruxible_core.server.registry import get_registry
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_document,
    service_propose_playbill_principal_change,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.service.authoring.projection_sync import (
    service_read_playbill_block_sync_backing,
)
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.change_preview import change_entry, change_scope, full_coordinate
from cruxible_core.service.claims.claim_reads import (
    service_read_claim_backings,
    service_read_claim_batch,
)
from cruxible_core.service.claims.claim_type_upgrade import service_upgrade_claim_types
from cruxible_core.service.claims.claim_types import (
    service_propose_playbill_claim_type,
    service_propose_playbill_claim_type_input,
)
from cruxible_core.service.discovery.audit import (
    PlaybillAuditRequestV1,
    service_playbill_audit,
    validate_playbill_audit_request,
)
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.coverage import (
    coverage_access_profile,
    service_resolve_playbill_coverage,
)
from cruxible_core.service.discovery.curation import (
    CurationError,
    PlaybillCurationAcceptFixedRequestV1,
    PlaybillCurationListRequestV1,
    PlaybillCurationObserveRequestV1,
    PlaybillCurationOverruleRequestV1,
    PlaybillCurationSuppressRequestV1,
    PlaybillCurationUnsuppressRequestV1,
    service_accept_fixed_playbill_curation,
    service_list_playbill_curation,
    service_observe_playbill_curation_blocks,
    service_overrule_playbill_curation,
    service_suppress_playbill_curation,
    service_unsuppress_playbill_curation,
    validate_playbill_curation_list_request,
    validate_playbill_curation_observe_request,
)
from cruxible_core.service.discovery.next import (
    NextRequestV1,
    service_playbill_next,
    validate_playbill_next_request,
)
from cruxible_core.service.discovery.orient import OrientCaller, service_playbill_orient
from cruxible_core.service.discovery.since import (
    service_playbill_since,
    validate_playbill_since_request,
)
from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
from cruxible_core.service.evidence.claim_attestations import service_append_claim_attestation
from cruxible_core.service.evidence.source_catalog import (
    service_check_playbill_source_bundle,
    service_playbill_source_context,
    service_propose_playbill_source_bundle,
)
from cruxible_core.service.floor.floor import MANIFEST_PATH, service_export_playbill_floor
from cruxible_core.service.floor.floor_delta import service_playbill_floor_delta
from cruxible_core.service.identity import credential_unbound_refusal, principal_refusal
from cruxible_core.service.kits import (
    service_add_kit,
    service_build_kit,
    service_kit_status,
    service_remove_kit,
)
from cruxible_core.service.procedures.measurements import (
    service_list_playbill_procedure_readings,
    service_measure_playbill_procedure,
)
from cruxible_core.service.procedures.predictions import (
    service_predict_playbill,
    service_settle_playbill_prediction,
)
from cruxible_core.service.procedures.procedure_runs import (
    LineRunNotAccepted,
    LineRunRequest,
    ProcedureNotFound,
    ProcedureReadinessRequestV1,
    ProcedureRetired,
    ProcedureRunRequest,
    line_run_target_rung,
    procedure_run_target_rung,
    run_permission_rung,
    service_playbill_procedure_readiness,
    service_run_playbill_line,
    service_run_playbill_procedure,
)
from cruxible_core.service.procedures.provider_installation import (
    service_install_provider,
    service_provider_catalog,
)
from cruxible_core.service.proposals.proposals import (
    ProposalInventoryStatus,
    WhoAmIActorIdSource,
    service_list_playbill_proposals,
    service_playbill_whoami,
    service_readmit_playbill_proposal,
    service_resolve_playbill_proposal_selector,
    service_withdraw_playbill_proposal,
)
from cruxible_core.service.proposals.publications import (
    service_declare_playbill_block,
    service_depublish_playbill_block,
)
from cruxible_core.service.proposals.review import (
    service_prepare_playbill_approval,
    service_review_playbill_proposal,
)
from cruxible_core.storage.cas import BodyAccessContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_core.service.discovery.operational_viewer import OperationalViewer

_ProposalResultT = TypeVar("_ProposalResultT")
_CurationRequestT = TypeVar("_CurationRequestT")


def _proposal_validation_boundary(
    family: str,
    operation: Callable[[], _ProposalResultT],
) -> _ProposalResultT:
    """Map any residual Pydantic proposal-ref failure to the typed HTTP 400 family."""

    try:
        return operation()
    except ValidationError as exc:
        raise DataValidationError(
            f"Cruxible {family} proposal reference is invalid",
            errors=validation_lines(exc),
        ) from exc


def _curation_validation_boundary(
    operation: Callable[[], _CurationRequestT],
) -> _CurationRequestT:
    """Map internal curation request validation to its typed HTTP 400 family."""

    try:
        return operation()
    except ValidationError as exc:
        raise CurationError(
            f"{CurationError.code}: curation request is malformed: {validation_summary(exc)}"
        ) from exc


_CLAIM_TYPE_MIGRATION_RESPONSE: TypeAdapter[contracts.ClaimTypeMigrationResponse] = TypeAdapter(
    contracts.ClaimTypeMigrationResponse
)


def _credential_actor_context() -> GovernedActorContext | None:
    """The request's principal: a credential's, or an auth-off daemon's claim."""

    auth_context = get_current_auth_context()
    if auth_context is None or auth_context.principal_id is None:
        return None
    try:
        return GovernedActorContext(
            actor_type="service_account",
            actor_id=auth_context.principal_id,
            org_id=auth_context.instance_scope or "local",
            operation_id=new_id("op", length=16, separator="_"),
            timestamp=utc_now(),
        )
    except ValidationError as exc:
        raise ConfigError("hosted governed actor context is required") from exc


def _actor_context() -> GovernedActorContext | None:
    actor = _credential_actor_context()
    if actor is None and not is_server_auth_enabled():
        actor = local_operator_actor_context()
    if actor is not None:
        set_current_operation_id(actor.operation_id)
    return actor


def _unbound_credential() -> ResolvedAuthContext | None:
    """The request's credential when it acts as no principal, else None."""

    auth_context = get_current_auth_context()
    if (
        auth_context is not None
        and auth_context.credential_type == "runtime_credential"
        and auth_context.principal_id is None
    ):
        return auth_context
    return None


def _write_actor_context(instance_id: str) -> GovernedActorContext | None:
    """The request's actor at a write boundary, refused if its claim cannot write here.

    A configured principal ID (an auth-off daemon's claim) must name a registered,
    active principal before it writes; reads stay open, so an agent can read
    while its registration awaits activation. A bearer credential's principal is
    checked when the credential authenticates. A bearer credential bound to no
    principal is refused here with ``credential_unbound`` and its mint repair, so
    every write door gives the same typed refusal rather than a generic
    authentication error.
    """

    actor = _actor_context()
    if actor is None:
        unbound = _unbound_credential()
        if unbound is not None:
            raise credential_unbound_refusal(
                credential_id=unbound.credential_id, credential_label=unbound.credential_label
            )
    auth_context = get_current_auth_context()
    if (
        actor is not None
        and auth_context is not None
        and auth_context.credential_type == "principal_claim"
    ):
        try:
            instance = get_playbill_manager().get(instance_id)
        except BootstrapError:
            # No registry exists before init; init checks the owner it names.
            return actor
        refusal = principal_refusal(instance, actor.actor_id, configured=True)
        if refusal is not None:
            raise refusal
    return actor


def _actor_id(instance_id: str) -> str:
    """Use credential-derived request identity at every Cruxible write boundary."""

    actor = _write_actor_context(instance_id)
    if actor is None:
        raise AuthenticationError("Cruxible writes require an authenticated actor identity")
    return actor.actor_id


def _require_writer(instance_id: str) -> None:
    """The write boundary for an instance mutation that records no actor.

    Every instance mutation passes the same principal refusal as an attributed
    write -- an unbound credential or an unregistered claim is refused -- before
    it has any side effect. The architecture guardrail
    ``test_every_instance_write_passes_the_principal_boundary`` holds new writes
    to it.
    """

    _actor_id(instance_id)


def _access(instance_id: str, *, include_body: bool) -> BodyAccessContext:
    actor = _actor_context()
    principal_id = "anonymous" if actor is None else actor.actor_id
    if include_body:
        check_permission("cruxible_body_read", instance_id=instance_id)
    return BodyAccessContext(principal_id=principal_id, can_read_body=include_body)


def _consumption_context() -> ConsumptionContextV1 | None:
    actor = _actor_context()
    if actor is None:
        return None
    return ConsumptionContextV1(
        actor_context=actor,
        access_profile_id=coverage_access_profile().profile_id,
    )


def _record_consumed_paths(
    instance_id: str,
    *,
    operation: ConsumptionOperation,
    coordinate: AcceptedCoordinate,
    paths: tuple[str, ...],
) -> None:
    if not consumption_receipts_enabled():
        # Nothing is recorded, so the served paths are not read again; an
        # instance that was observing is marked unobserved, once per process.
        note_consumption_unobserved(
            get_playbill_manager().get(instance_id),
            context=_consumption_context(),
            coordinate=coordinate,
        )
        return
    instance = get_playbill_manager().get(instance_id)
    record_consumption(
        instance,
        context=_consumption_context(),
        operation=operation,
        coordinate=coordinate,
        # One receipt names exactly the paths it served: read those, not the
        # whole generation.
        artifacts=consumption_artifacts_for_paths(
            instance.blobs_at(coordinate.git_oid, paths),
            paths,
        ),
    )


def playbill_init(
    instance_id: str,
    *,
    principals: tuple[PrincipalRecord, ...],
    operating_profile: OperatingProfile = "local",
    require_independent_approval: bool = False,
    workspace_root: str | None = None,
    workspace_attachment_authorized: bool = False,
    git_object_format: GitObjectFormat | None = None,
    mirror_url: str | None = None,
) -> contracts.InitResult:
    check_permission("cruxible_init", instance_id=instance_id)
    if mirror_url is not None:
        # Validated before any state exists. A malformed remote is the operator's
        # typo, and finding it after bootstrap would leave a live instance whose
        # only repair is a verb they have not been told about yet.
        validate_mirror_url(mirror_url)
    # An unbound operator credential (the bootstrap claim) designates the owner:
    # it is the operator, and it acts as no principal. Every other caller must
    # itself be one of the owners it names.
    operator_designates = _unbound_credential() is not None
    actor_id = None if operator_designates else _actor_id(instance_id)
    if not principals:
        raise BootstrapError("bootstrap requires at least one client principal")
    ordinary = {
        item.principal_id
        for item in principals
        if item.status == "active" and item.kind == "ordinary"
    }
    if actor_id is not None and actor_id not in ordinary:
        owners = ", ".join(sorted(ordinary)) or "none"
        raise PrincipalRefusedError(
            "cruxible.identity.init_owner_mismatch",
            f"init makes the process that runs it an owner, but this process acts as "
            f"{actor_id!r} and the owner principals named are: {owners}; repair: "
            "`cruxible init --principal-id ID --key-dir DIR` makes you the owner "
            "under ID (with daemon auth off no bootstrap secret is needed)",
            repair=RepairOperation(
                operation="cruxible.init",
                arguments={"principal_id": actor_id},
            ),
        )
    registry = get_registry()
    attached_for_init = False
    if workspace_root is not None:
        if not workspace_attachment_authorized:
            raise ConfigError(
                "Workspace attachment requires a caller connected directly through the local "
                "Unix socket"
            )
        try:
            workspace_git_object_format(Path(workspace_root))
        except ValueError as exc:
            raise ConfigError("Workspace attachment requires one local Git worktree") from exc
        record = registry.get(instance_id)
        if record is None:
            raise ConfigError(f"Instance '{instance_id}' is not a governed daemon host")
        # One attach path for a host before or after its init (Q16): an already
        # initialized host is attached in place, never rebuilt.
        attached_for_init = attach_workspace(instance_id, workspace_root) and not (
            Path(record.location).exists()
        )
    try:
        instance = get_playbill_manager().initialize(
            instance_id,
            client_principals=principals,
            operating_profile=operating_profile,
            require_independent_approval=require_independent_approval,
            git_object_format=git_object_format,
        )
    except BaseException:
        if attached_for_init and workspace_root is not None:
            registry.detach_governed_workspace(
                instance_id,
                expected_workspace_root=workspace_root,
            )
        raise
    if mirror_url is not None:
        # Bind the mirror before subsequent governed proposals.
        instance.set_ledger_mirror(mirror_url)
    return contracts.InitResult(
        instance_id=instance_id,
        coordinate=contracts.AcceptedCoordinate.model_validate(
            AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
        ),
        trust_root=instance.trust_root.model_dump(mode="json"),
        recovery_posture=instance.descriptor.recovery_posture,
        approval_policy_mode=instance.inspect().approval_policy_mode,
        workspace_advertisement=instance.settled_workspace_advertisement(),
    )


def playbill_instance_decommission(
    instance_id: str,
    *,
    reason: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.InstanceDecommissionResult:
    """Stamp the terminal lifecycle state on one instance, deleting nothing.

    Reads keep serving at the accepted coordinate forever; every further
    governed write refuses typed. Archiving or erasing the directory afterwards
    is the operator's own step and no verb performs it. It cannot be undone, so
    it previews by default and commits only with ``at`` (R12).
    """

    check_permission("cruxible_instance_decommission", instance_id=instance_id)
    with change_entry(dry_run, "irreversible"):
        instance = get_playbill_manager().get(instance_id)
        with change_scope(
            instance,
            dry_run=dry_run,
            at=at,
            kind="irreversible",
            operation="cruxible.instance.decommission",
            describe=f"decommissioning instance {instance_id}",
        ) as mode:
            record = instance.decommission(
                reason=reason,
                decommissioned_by=_actor_id(instance_id),
                confirm_head=mode.confirm_head,
            )
            return contracts.InstanceDecommissionResult(
                status="would_decommission" if mode.previewing else "decommissioned",
                instance_id=instance_id,
                reason=record.reason,
                decommissioned_at=record.decommissioned_at,
                decommissioned_by=record.decommissioned_by,
                coordinate=full_coordinate(instance),
            )


def _mirror_receipt(
    instance_id: str,
    *,
    url: str,
    state: LedgerMirrorStateV1 | None,
) -> contracts.LedgerMirror:
    """Render the mirror as a reader sees it, with no attempt read as behind."""

    if state is None or state.url != url:
        return contracts.LedgerMirror(
            instance_id=instance_id,
            mirror_url=url,
            status="behind",
            detail="nothing has been published to this remote yet",
        )
    return contracts.LedgerMirror(
        instance_id=instance_id,
        mirror_url=url,
        status=state.status,
        attempted_at=state.attempted_at,
        published_main_oid=state.published_main_oid,
        requested_sequence=state.requested_sequence,
        attempted_sequence=state.attempted_sequence,
        published_sequence=state.published_sequence,
        published_refs=dict(state.published_refs),
        wait_sequence=state.wait_sequence,
        detail=state.detail,
    )


def _would_publish(
    instance: PlaybillInstance, instance_id: str, *, url: str, detail: str
) -> contracts.LedgerMirror:
    return contracts.LedgerMirror(
        instance_id=instance_id,
        mirror_url=url,
        status="would_publish",
        detail=detail,
        coordinate=full_coordinate(instance),
    )


def playbill_ledger_set_mirror(
    instance_id: str,
    *,
    url: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.LedgerMirror:
    """Bind the remote and wait boundedly for its initial publication attempt.

    Operational configuration rather than a governed change: it proposes
    nothing, accepts nothing, and moves no coordinate. It is an ADMIN lever all
    the same, because it names where a copy of every accepted byte is sent --
    which cannot be called back, so it previews by default and commits only
    with ``at`` (R12).
    """

    check_permission("cruxible_ledger_set_mirror", instance_id=instance_id)
    with change_entry(dry_run, "irreversible"):
        _require_writer(instance_id)
        instance = get_playbill_manager().get(instance_id)
        with change_scope(
            instance,
            dry_run=dry_run,
            at=at,
            kind="irreversible",
            operation="cruxible.ledger.set-mirror",
            describe=f"binding ledger mirror {url}",
        ) as mode:
            state = instance.set_ledger_mirror(url, confirm_head=mode.confirm_head)
            if mode.previewing:
                return _would_publish(
                    instance,
                    instance_id,
                    url=url,
                    detail="would bind this remote and publish every accepted ref to it now",
                )
        return _mirror_receipt(instance_id, url=instance.ledger_mirror_url() or url, state=state)


def playbill_ledger_clear_mirror(
    instance_id: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.LedgerMirrorCleared:
    """Unbind the mirror so nothing more is published; what was sent stays sent."""

    check_permission("cruxible_ledger_set_mirror", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        _require_writer(instance_id)
        instance = get_playbill_manager().get(instance_id)
        with change_scope(
            instance,
            dry_run=dry_run,
            at=at,
            kind="direct",
            operation="cruxible.ledger.set-mirror",
            describe="clearing the ledger mirror",
        ) as mode:
            previous = instance.clear_ledger_mirror(confirm_head=mode.confirm_head)
            if previous is None:
                return contracts.LedgerMirrorCleared(
                    instance_id=instance_id, status="already_clear"
                )
            if mode.previewing:
                return contracts.LedgerMirrorCleared(
                    instance_id=instance_id,
                    status="would_clear",
                    previous_mirror_url=previous,
                    coordinate=full_coordinate(instance),
                )
        return contracts.LedgerMirrorCleared(
            instance_id=instance_id, status="cleared", previous_mirror_url=previous
        )


def playbill_ledger_publish(
    instance_id: str,
    *,
    timeout: float = 60.0,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.LedgerMirror:
    """Request publication to the configured mirror and wait for its acknowledgment."""

    check_permission("cruxible_ledger_publish", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        _require_writer(instance_id)
        if isinstance(timeout, bool) or not 0 <= timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        instance = get_playbill_manager().get(instance_id)
        url = instance.ledger_mirror_url()
        if url is None:
            raise LedgerMirrorUnset()
        with change_scope(
            instance,
            dry_run=dry_run,
            at=at,
            kind="direct",
            operation="cruxible.ledger.publish",
            describe="publishing the ledger",
        ) as mode:
            if mode.previewing:
                current = _mirror_receipt(
                    instance_id, url=url, state=instance.ledger_mirror_state()
                )
                return current.model_copy(
                    update={
                        "status": "would_publish",
                        "detail": "would request publication of the accepted and review refs "
                        f"(the mirror reads {current.status})",
                        "coordinate": full_coordinate(instance),
                    }
                )
            state = instance.publish_ledger_mirror(timeout=timeout)
        return _mirror_receipt(instance_id, url=url, state=state)


def playbill_provider_catalog(instance_id: str) -> ProviderCatalog:
    check_permission("cruxible_provider_list", instance_id=instance_id)
    manager = get_playbill_manager()
    manager.get(instance_id)
    return _proposal_validation_boundary(
        "provider catalog", lambda: service_provider_catalog(manager.provider_runtime_operator())
    )


def playbill_provider_install(
    instance_id: str,
    request: ProviderInstallRequest,
) -> ProviderInstallResult:
    check_permission("cruxible_provider_install", instance_id=instance_id)
    enforce_customer_code_execution_supported()
    with change_entry(request.dry_run, "direct"):
        manager = get_playbill_manager()
        return _proposal_validation_boundary(
            "provider installation",
            lambda: service_install_provider(
                manager.get(instance_id),
                operator=manager.provider_runtime_operator(),
                request=request,
                actor_id=_actor_id(instance_id),
                timestamp=canonical_candidate_timestamp(utc_now()),
            ),
        )


def playbill_kit_build(instance_id: str, request: KitBuildRequest) -> KitBuildResult:
    check_permission("cruxible_kit_build", instance_id=instance_id)
    actor = _actor_context()
    return _proposal_validation_boundary(
        "kit build",
        lambda: service_build_kit(
            get_playbill_manager().get(instance_id),
            request,
            principal_id=None if actor is None else actor.actor_id,
        ),
    )


def playbill_kit_status(instance_id: str) -> KitStatus:
    check_permission("cruxible_kit_status", instance_id=instance_id)
    return _proposal_validation_boundary(
        "kit status", lambda: service_kit_status(get_playbill_manager().get(instance_id))
    )


def playbill_kit_add(instance_id: str, request: KitAddRequest) -> KitChangeResult:
    check_permission("cruxible_kit_add", instance_id=instance_id)
    with change_entry(request.dry_run, "derived"):
        return _proposal_validation_boundary(
            "kit add",
            lambda: service_add_kit(
                get_playbill_manager().get(instance_id),
                request,
                actor_id=_actor_id(instance_id),
                timestamp=canonical_candidate_timestamp(utc_now()),
            ),
        )


def playbill_claim_type_upgrade(
    instance_id: str, request: ClaimTypeUpgradeRequest
) -> ClaimTypeUpgradeResult:
    check_permission("cruxible_claim_type_upgrade", instance_id=instance_id)
    with change_entry(request.dry_run, "derived"):
        return _proposal_validation_boundary(
            "claim type upgrade",
            lambda: service_upgrade_claim_types(
                get_playbill_manager().get(instance_id),
                request=request,
                actor_id=_actor_id(instance_id),
                timestamp=canonical_candidate_timestamp(utc_now()),
            ),
        )


def playbill_kit_remove(instance_id: str, request: KitRemoveRequest) -> KitChangeResult:
    check_permission("cruxible_kit_remove", instance_id=instance_id)
    with change_entry(request.dry_run, "derived"):
        return _proposal_validation_boundary(
            "kit removal",
            lambda: service_remove_kit(
                get_playbill_manager().get(instance_id),
                request,
                actor_id=_actor_id(instance_id),
                timestamp=canonical_candidate_timestamp(utc_now()),
            ),
        )


def playbill_store_body(instance_id: str, *, content_base64: str) -> contracts.CasObjectResult:
    check_permission("cruxible_body_store", instance_id=instance_id)
    _require_writer(instance_id)
    try:
        content = base64.b64decode(content_base64, validate=True)
    except ValueError as exc:
        raise DataValidationError("Cruxible body is not canonical base64") from exc
    result = service_store_playbill_body(get_playbill_manager().get(instance_id), content=content)
    return contracts.CasObjectResult.model_validate(result.model_dump(mode="json"))


def playbill_propose_document(
    instance_id: str,
    *,
    shell: DocumentShell,
    proposal_name: str,
    source_compilation_digest: str | None = None,
    base: AcceptedCoordinate | None = None,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalInspection:
    check_permission("cruxible_propose", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        result = _proposal_validation_boundary(
            "document",
            lambda: service_propose_playbill_document(
                get_playbill_manager().get(instance_id),
                shell=shell,
                actor_id=_actor_id(instance_id),
                proposal_name=proposal_name,
                timestamp=canonical_candidate_timestamp(utc_now()),
                source_compilation_digest=source_compilation_digest,
                base=base,
                dry_run=dry_run,
                at=at,
            ),
        )
        return contracts.ProposalInspection.model_validate(result.model_dump(mode="json"))


def playbill_propose_compiler_upgrade(
    instance_id: str,
    *,
    target: CompilerCoordinate,
    base: AcceptedCoordinate,
    proposal_name: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalInspection:
    check_permission("cruxible_compiler_upgrade", instance_id=instance_id)
    from cruxible_core.service.authoring.documents import service_propose_compiler_upgrade

    with change_entry(dry_run, "direct"):
        result = _proposal_validation_boundary(
            "compiler_upgrade",
            lambda: service_propose_compiler_upgrade(
                get_playbill_manager().get(instance_id),
                target=target,
                base=base,
                actor_id=_actor_id(instance_id),
                proposal_name=proposal_name,
                timestamp=canonical_candidate_timestamp(utc_now()),
                dry_run=dry_run,
                preview_at=at,
            ),
        )
        return contracts.ProposalInspection.model_validate(result.model_dump(mode="json"))


def playbill_propose_principal_change(
    instance_id: str,
    *,
    principal: PrincipalRecord,
    proposal_name: str,
    base: AcceptedCoordinate | None = None,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalInspection:
    check_permission("cruxible_principal_change", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        result = _proposal_validation_boundary(
            "principal",
            lambda: service_propose_playbill_principal_change(
                get_playbill_manager().get(instance_id),
                principal=principal,
                actor_id=_actor_id(instance_id),
                proposal_name=proposal_name,
                timestamp=canonical_candidate_timestamp(utc_now()),
                base=base,
                dry_run=dry_run,
                at=at,
            ),
        )
        return contracts.ProposalInspection.model_validate(result.model_dump(mode="json"))


def playbill_list_proposals(
    instance_id: str,
    *,
    status: ProposalInventoryStatus | None = None,
    limit: int = contracts.PROPOSAL_LIST_DEFAULT_LIMIT,
    cursor: str | None = None,
) -> contracts.ProposalList:
    check_permission("cruxible_read", instance_id=instance_id)
    result = service_list_playbill_proposals(
        get_playbill_manager().get(instance_id),
        status=status,
        limit=limit,
        cursor=cursor,
    )
    return contracts.ProposalList.model_validate(result.model_dump(mode="json"))


def playbill_resolve_proposal_selector(
    instance_id: str,
    selector: str,
) -> contracts.ProposalSelectorResult:
    check_permission("cruxible_read", instance_id=instance_id)
    result = service_resolve_playbill_proposal_selector(
        get_playbill_manager().get(instance_id),
        selector=selector,
    )
    return contracts.ProposalSelectorResult.model_validate(result.model_dump(mode="json"))


def playbill_readmit_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalReadmitResult:
    check_permission("cruxible_propose", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        result = service_readmit_playbill_proposal(
            get_playbill_manager().get(instance_id),
            proposal_id=proposal_id,
            actor_id=_actor_id(instance_id),
            dry_run=dry_run,
            at=at,
        )
    return contracts.ProposalReadmitResult.model_validate(result.model_dump(mode="json"))


def playbill_withdraw_proposal(
    instance_id: str,
    proposal_id: str,
    reason: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalWithdrawResult:
    check_permission("cruxible_propose", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        result = service_withdraw_playbill_proposal(
            get_playbill_manager().get(instance_id),
            proposal_id=proposal_id,
            actor_id=_actor_id(instance_id),
            reason=reason,
            withdrawn_at=canonical_candidate_timestamp(utc_now()),
            # No bound instance scope IS the daemon-wide operator credential (or
            # an auth-off local daemon, which has one operator): the same reading
            # `require_unscoped_operator` makes for every other daemon-wide lever.
            unscoped_operator=current_request_instance_scope() is None,
            dry_run=dry_run,
            at=at,
        )
    return contracts.ProposalWithdrawResult.model_validate(result.model_dump(mode="json"))


def playbill_whoami(instance_id: str) -> contracts.WhoAmI:
    check_permission("cruxible_read", instance_id=instance_id)
    auth_context = get_current_auth_context()
    actor_id: str | None
    credential_label: str | None
    actor_id_source: WhoAmIActorIdSource
    if auth_context is not None and auth_context.credential_type == "runtime_credential":
        actor_id = auth_context.principal_id
        credential_label = auth_context.credential_label
        actor_id_source = "runtime_credential" if actor_id is not None else "unbound_credential"
    elif auth_context is not None and auth_context.credential_type == "principal_claim":
        assert auth_context.principal_id is not None
        actor_id = auth_context.principal_id
        credential_label = None
        actor_id_source = "principal_claim"
    else:
        actor = _actor_context()
        if actor is None:
            raise AuthenticationError("Cruxible identity requires an authenticated actor")
        actor_id = actor.actor_id
        credential_label = None
        actor_id_source = "local_operator"
    result = service_playbill_whoami(
        get_playbill_manager().get(instance_id),
        actor_id=actor_id,
        credential_label=credential_label,
        actor_id_source=actor_id_source,
        authenticated=auth_context is not None and auth_context.authenticated,
        permission_mode=get_current_mode(),
        credential_id=None if auth_context is None else auth_context.credential_id,
    )
    return contracts.WhoAmI.model_validate(result.model_dump(mode="json"))


def playbill_head(
    instance_id: str, *, at: AcceptedCoordinate | str | None = None
) -> contracts.Head:
    """The accepted head (or ``at``) and its generation: the cheapest coordinate read."""

    from cruxible_core.service.discovery.orient import service_playbill_head

    check_permission("cruxible_read", instance_id=instance_id)
    return service_playbill_head(get_playbill_manager().get(instance_id), at=at)


def playbill_orient(
    instance_id: str,
    *,
    kind: str | None = None,
    section: OrientSection | None = None,
    limit: int = ORIENT_DEFAULT_LIMIT,
    cursor: str | None = None,
    at: AcceptedCoordinate | str | None = None,
    evaluation_time: datetime | None = None,
    surface: OrientSurface = "cli",
    caller_tools: tuple[str, ...] | None = None,
) -> OrientResult:
    """The orient map; the caller is whoever ``whoami`` resolves the transport to."""

    check_permission("cruxible_orient", instance_id=instance_id)
    try:
        identity: contracts.WhoAmI | None = playbill_whoami(instance_id)
    except AuthenticationError:
        identity = None
    lane_state, lane_code, lane_detail = (
        get_playbill_manager().provider_runtime_operator().lane_status()
    )
    return service_playbill_orient(
        get_playbill_manager().get(instance_id),
        kind=kind,
        section=section,
        limit=limit,
        cursor=cursor,
        at=at,
        evaluation_time=evaluation_time,
        surface=surface,
        caller_rung=get_current_mode().value - 1,
        caller_tools=caller_tools,
        caller=(
            None
            if identity is None
            else OrientCaller(
                actor_id=identity.actor_id,
                credential_permission_mode=identity.credential_permission_mode,
                configured=identity.actor_id_source != "local_operator",
                credential_label=identity.credential_label,
            )
        ),
        provider_lane=contracts.ProviderLaneStatus(
            state=lane_state, code=lane_code, detail=lane_detail
        ),
        consumers_running=get_playbill_manager().consumer_runner.running,
    )


def playbill_review_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    include_body: bool = False,
    workspace_observation: Mapping[str, object] | None = None,
) -> contracts.ProposalReview:
    check_permission("cruxible_proposal_review", instance_id=instance_id)
    result = service_review_playbill_proposal(
        get_playbill_manager().get(instance_id),
        proposal_id=proposal_id,
        access=_access(instance_id, include_body=include_body),
        workspace_observation=workspace_observation,
    )
    return contracts.ProposalReview.model_validate(result.model_dump(mode="json"))


def playbill_prepare_approval(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    include_body: bool = False,
) -> contracts.ApprovalChallenge:
    check_permission("cruxible_proposal_review", instance_id=instance_id)
    result = service_prepare_playbill_approval(
        get_playbill_manager().get(instance_id),
        proposal_id=proposal_id,
        signer_id=signer_id,
        access=_access(instance_id, include_body=include_body),
    )
    return contracts.ApprovalChallenge.model_validate(result.model_dump(mode="json"))


def playbill_submit_approval(
    instance_id: str,
    proposal_id: str,
    *,
    attestation: ApprovalAttestation,
) -> contracts.ApprovalReceipt:
    check_permission("cruxible_proposal_approve_submit", instance_id=instance_id)
    result = service_submit_playbill_approval(
        get_playbill_manager().get(instance_id),
        proposal_id=proposal_id,
        attestation=attestation,
        authenticated_submitter=_actor_id(instance_id),
    )
    return contracts.ApprovalReceipt.model_validate(result.model_dump(mode="json"))


def playbill_activate(
    instance_id: str,
    proposal_id: str,
) -> contracts.ActivationReceipt:
    check_permission("cruxible_proposal_activate", instance_id=instance_id)
    activated_by = _actor_id(instance_id)
    result = service_activate_playbill_proposal(
        get_playbill_manager().get(instance_id),
        proposal_id=proposal_id,
        activated_by=activated_by,
    )
    return contracts.ActivationReceipt.model_validate(result.model_dump(mode="json"))


def playbill_read_capture(instance_id: str, request: CaptureReadRequest) -> CaptureRead:
    check_permission("cruxible_body_read", instance_id=instance_id)
    return service_read_playbill_capture(
        get_playbill_manager().get(instance_id),
        request=request,
        access=_access(instance_id, include_body=True),
    )


def playbill_source_context(instance_id: str) -> contracts.SourceContext:
    check_permission("cruxible_read", instance_id=instance_id)
    result = service_playbill_source_context(get_playbill_manager().get(instance_id))
    return contracts.SourceContext.model_validate(result.model_dump(mode="json"))


def playbill_check_source_bundle(
    instance_id: str,
    *,
    bundle: SourceCompilationBundle,
) -> contracts.SourceCheckResult:
    check_permission("cruxible_read", instance_id=instance_id)
    result = service_check_playbill_source_bundle(
        get_playbill_manager().get(instance_id), bundle=bundle
    )
    return contracts.SourceCheckResult.model_validate(result.model_dump(mode="json"))


def playbill_propose_source_bundle(
    instance_id: str,
    *,
    bundle: SourceCompilationBundle,
    source_name: str,
    proposal_name: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalInspection:
    check_permission("cruxible_propose", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        result = _proposal_validation_boundary(
            "source bundle",
            lambda: service_propose_playbill_source_bundle(
                get_playbill_manager().get(instance_id),
                bundle=bundle,
                source_name=source_name,
                actor_id=_actor_id(instance_id),
                proposal_name=proposal_name,
                timestamp=canonical_candidate_timestamp(utc_now()),
                dry_run=dry_run,
                at=at,
            ),
        )
    return contracts.ProposalInspection.model_validate(result.model_dump(mode="json"))


def _evaluation_time(value: datetime | None) -> datetime:
    return utc_now() if value is None else value


def playbill_propose_claim_type(
    instance_id: str,
    *,
    claim_type: ClaimType,
    proposal_name: str,
    base: AcceptedCoordinate | None = None,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalInspection:
    check_permission("cruxible_propose", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        instance = get_playbill_manager().get(instance_id)
        coordinate = instance.accepted_coordinate()
        result = _proposal_validation_boundary(
            "claim type",
            lambda: service_propose_playbill_claim_type(
                instance,
                claim_type=claim_type,
                actor_id=_actor_id(instance_id),
                proposal_name=proposal_name,
                timestamp=canonical_candidate_timestamp(utc_now()),
                base=base,
                dry_run=dry_run,
                at=at,
            ),
        )
        values = result.model_dump(mode="json")
        lint = lint_claim_type_input(instance, claim_type, coordinate=coordinate)
        if lint.warnings:
            values["lint"] = lint.model_dump(mode="json")
        return contracts.ProposalInspection.model_validate(values)


def playbill_propose_claim_type_input(
    instance_id: str,
    *,
    input: ClaimTypeInputRecord,
    proposal_name: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ClaimTypeInputProposalResult:
    check_permission("cruxible_propose", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        result = _proposal_validation_boundary(
            "claim type input",
            lambda: service_propose_playbill_claim_type_input(
                get_playbill_manager().get(instance_id),
                input=input,
                actor_id=_actor_id(instance_id),
                proposal_name=proposal_name,
                timestamp=canonical_candidate_timestamp(utc_now()),
                dry_run=dry_run,
                at=at,
            ),
        )
        return contracts.ClaimTypeInputProposalResult.model_validate(result.model_dump(mode="json"))


def playbill_migrate_claim_type(
    instance_id: str,
    *,
    request: ClaimTypeMigrationRequestAny,
) -> contracts.ClaimTypeMigrationResponse:
    check_permission("cruxible_propose", instance_id=instance_id)
    result = service_migrate_claim_type(
        get_playbill_manager().get(instance_id),
        request=request,
        actor=AuthenticatedActor(actor_id=_actor_id(instance_id)),
    )
    return _CLAIM_TYPE_MIGRATION_RESPONSE.validate_python(result.model_dump(mode="json"))


def _permits(tool_name: str, *, instance_id: str) -> bool:
    """Whether the caller's tier and credential scope admit ``tool_name``, without refusing."""

    if get_current_mode() < PERMISSION_REQUIREMENTS[tool_name]:
        return False
    scope = current_request_instance_scope()
    return scope is None or scope == instance_id


def _write_outcome(instance_id: str, request: WriteRequest) -> WriteOutcome:
    # The preview's guards go up before the actor is resolved: resolving a
    # principal claim opens the instance, and a cold open may repair on disk.
    with change_entry(request.dry_run, "direct"):
        caller = WriteCaller(
            actor=AuthenticatedActor(actor_id=_actor_id(instance_id)),
            may_activate=_permits("cruxible_proposal_activate", instance_id=instance_id),
        )
        return service_playbill_write(
            get_playbill_manager().get(instance_id), request=request, caller=caller
        )


def playbill_set(instance_id: str, *, request: SetRequest) -> WriteOutcome:
    """Put one value in one field of one Subject; see ``service_playbill_write``."""

    check_permission("cruxible_set", instance_id=instance_id)
    return _write_outcome(instance_id, as_write_request(request))


def playbill_retire(instance_id: str, *, request: RetireRequest) -> WriteOutcome:
    """End one live Claim, by ID or by its Subject and field."""

    check_permission("cruxible_retire", instance_id=instance_id)
    return _write_outcome(instance_id, as_write_request(request))


def playbill_write(instance_id: str, *, request: WriteRequest) -> WriteOutcome:
    """Apply a batch of set, add and retire changes as one change set."""

    check_permission("cruxible_write", instance_id=instance_id)
    return _write_outcome(instance_id, request)


def playbill_append_claim_attestation(
    instance_id: str,
    *,
    request: ClaimAttestationAppendRequest,
) -> ClaimAttestationAppendResult:
    check_permission("cruxible_claim_attest", instance_id=instance_id)
    return service_append_claim_attestation(
        get_playbill_manager().get(instance_id),
        request=request,
        actor_id=_actor_id(instance_id),
    )


def playbill_recover_claim_attestations(instance_id: str) -> None:
    """Synchronously restore the sole replay-valid evidence-ledger head."""

    check_permission("cruxible_claim_attestation_recover", instance_id=instance_id)
    _require_writer(instance_id)
    get_playbill_manager().get(instance_id).claim_attestation_evidence_store().recover()


def _authoring_coordinator(
    instance_id: str,
) -> tuple[AuthoringIntentCoordinator, AuthenticatedActor]:
    actor = AuthenticatedActor(actor_id=_actor_id(instance_id))
    instance = get_playbill_manager().get(instance_id)
    return AuthoringIntentCoordinator.for_instance(instance), actor


def playbill_prediction_list(
    instance_id: str, *, request: contracts.ResolutionContractsRequest
) -> contracts.ResolutionContractsResult:
    from cruxible_core.service.procedures.resolution_contracts import service_resolution_contracts

    check_permission("cruxible_prediction_list", instance_id=instance_id)
    return service_resolution_contracts(get_playbill_manager().get(instance_id), request)


def playbill_predict(
    instance_id: str,
    *,
    request: PredictRequest,
) -> PredictResult:
    """Submit a governed test of an already accepted hypothesis."""

    check_permission("cruxible_prediction_propose", instance_id=instance_id)
    actor_context = _write_actor_context(instance_id)
    if actor_context is None:
        raise AuthenticationError("Prediction authoring requires an authenticated actor identity")
    return service_predict_playbill(
        get_playbill_manager().get(instance_id),
        request=request,
        actor=AuthenticatedActor(actor_id=actor_context.actor_id),
        evaluation_time=actor_context.timestamp,
    )


def playbill_settle_prediction(
    instance_id: str,
    prediction_id: str,
    *,
    request: SettleRequest,
) -> SettleResult:
    """Settle one prediction through admission or retained terminal authority."""

    check_permission("cruxible_prediction_settle", instance_id=instance_id)
    actor_context = _write_actor_context(instance_id)
    if actor_context is None:
        raise AuthenticationError("Prediction settlement requires an authenticated actor identity")
    return service_settle_playbill_prediction(
        get_playbill_manager().get(instance_id),
        prediction_id=prediction_id,
        request=request,
        actor_context=actor_context,
        recorded_at=actor_context.timestamp,
    )


def playbill_authoring_get(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringIntentViewRecord:
    check_permission("cruxible_authoring_get", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.get(intent_id, actor=actor)
    return contracts.AuthoringIntentViewRecord.model_validate(result.model_dump(mode="json"))


def playbill_authoring_list(
    instance_id: str,
) -> contracts.AuthoringIntentListRecord:
    check_permission("cruxible_authoring_list", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.list_pending(actor=actor)
    return contracts.AuthoringIntentListRecord.model_validate(result.model_dump(mode="json"))


def _authoring_preflight_result(
    coordinator: AuthoringIntentCoordinator,
    *,
    actor: AuthenticatedActor,
    result: PreflightResult,
    payload: AuthoringPayload | None = None,
) -> contracts.AuthoringPreflightResult:
    """Serve one preflight with its advisory lint; ``payload`` when nothing was stored."""

    values = result.model_dump(mode="json")
    if payload is None:
        payload = coordinator.store.get(
            result.certificate.intent_id,
            actor_id=actor.actor_id,
        ).payload

    def _at() -> AcceptedProjectionCoordinate:
        at = result.certificate.accepted_coordinate
        return coordinator.instance.resolve_accepted_coordinate(
            git_oid=at.git_oid,
            semantic_root=at.semantic_root,
            generation_root=at.generation_root,
            compiler_digest=at.compiler_digest,
        )

    if isinstance(payload, ClaimAuthoringPayloadV2 | ClaimAuthoringPayload):
        claim_type = payload.dependency_drafts.claim_type
        if claim_type is not None:
            source_ids = (
                (payload.source.source_id,)
                if isinstance(payload.source, WorkingSelectionObservation)
                else ()
            )
            lint = lint_claim_type_input(
                coordinator.instance,
                claim_type,
                coordinate=_at(),
                anticipated_source_ids=source_ids,
            )
            if lint.warnings:
                values["lint"] = lint.model_dump(mode="json")
    elif isinstance(payload, ChangeSetAuthoringPayload):
        # A succession authored as a member is the same decision the operator
        # road takes, so it owes the same evidence-policy reading.
        successors = tuple(
            member.successor
            for member in payload.members
            if isinstance(member, ClaimTypeSuccessionMember)
        )
        if successors:
            change_set_lint = lint_claim_type_successors(
                coordinator.instance,
                successors,
                coordinate=_at(),
            )
            if change_set_lint is not None:
                values["lint"] = change_set_lint.model_dump(mode="json")
    return contracts.AuthoringPreflightResult.model_validate(values)


def playbill_authoring_compile(
    instance_id: str,
    *,
    payload: AuthoringPayload,
    intent_id: str | None = None,
    reference_expectations: tuple[AuthoringExpectation, ...] | None = None,
    program_stamp: AuthoringProgramStamp | None = None,
) -> contracts.AuthoringPreflightResult:
    check_permission("cruxible_authoring_compile", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.compile(
        actor=actor,
        payload=payload,
        canonical_timestamp=canonical_candidate_timestamp(utc_now()),
        intent_id=intent_id,
        reference_expectations=reference_expectations,
        program_stamp=program_stamp,
    )
    return _authoring_preflight_result(coordinator, actor=actor, result=result)


def playbill_authoring_compile_and_submit(
    instance_id: str,
    *,
    payload: AuthoringPayload,
    reference_expectations: tuple[AuthoringExpectation, ...],
    program_stamp: AuthoringProgramStamp,
    intent_id: str | None = None,
) -> contracts.AuthoringSubmitResultRecord:
    check_permission("cruxible_authoring_compile", instance_id=instance_id)
    check_permission("cruxible_authoring_submit", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.compile_and_submit(
        actor=actor,
        payload=payload,
        canonical_timestamp=canonical_candidate_timestamp(utc_now()),
        intent_id=intent_id,
        reference_expectations=reference_expectations,
        program_stamp=program_stamp,
    )
    submitted = contracts.AuthoringSubmitResultRecord.model_validate(result.model_dump(mode="json"))
    preflight = result.intent.last_preflight
    if preflight is None:
        return submitted
    return submitted.model_copy(
        update={
            "preflight": _authoring_preflight_result(coordinator, actor=actor, result=preflight)
        }
    )


def playbill_authoring_submit_input(
    instance_id: str,
    *,
    input: AuthoringInput,
    intent_id: str | None = None,
) -> contracts.AuthoringSubmitResultRecord:
    """Compile one tagless input and submit it; the preflight it ran rides along."""

    check_permission("cruxible_authoring_compile", instance_id=instance_id)
    check_permission("cruxible_authoring_submit", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.submit_input(
        actor=actor,
        input=input,
        canonical_timestamp=canonical_candidate_timestamp(utc_now()),
        intent_id=intent_id,
    )
    submitted = contracts.AuthoringSubmitResultRecord.model_validate(result.model_dump(mode="json"))
    preflight = result.intent.last_preflight
    if preflight is None:
        return submitted
    return submitted.model_copy(
        update={
            "preflight": _authoring_preflight_result(coordinator, actor=actor, result=preflight)
        }
    )


def playbill_authoring_preview_input(
    instance_id: str,
    *,
    input: AuthoringInput,
) -> contracts.AuthoringPreflightResult:
    """Preflight one tagless input exactly as a submit would, and save no intent.

    Every refusal the submit would return comes back in the frontier; nothing is
    stored, so the certificate names an intent that does not exist.
    """

    check_permission("cruxible_authoring_compile", instance_id=instance_id)
    with change_entry(True, "direct"):
        coordinator, actor = _authoring_coordinator(instance_id)
        intent, computed = coordinator.preview_input(
            actor=actor,
            input=input,
            canonical_timestamp=canonical_candidate_timestamp(utc_now()),
        )
        return _authoring_preflight_result(
            coordinator, actor=actor, result=computed.result, payload=intent.payload
        )


def playbill_authoring_compile_input(
    instance_id: str,
    *,
    input: AuthoringInput,
    intent_id: str | None = None,
) -> contracts.AuthoringPreflightResult:
    check_permission("cruxible_authoring_compile", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.compile_input(
        actor=actor,
        input=input,
        canonical_timestamp=canonical_candidate_timestamp(utc_now()),
        intent_id=intent_id,
    )
    return _authoring_preflight_result(coordinator, actor=actor, result=result)


def playbill_authoring_preflight(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringPreflightResult:
    check_permission("cruxible_authoring_preflight", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.preflight(intent_id, actor=actor)
    return _authoring_preflight_result(coordinator, actor=actor, result=result)


def playbill_authoring_rebase(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringIntentViewRecord:
    check_permission("cruxible_authoring_rebase", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.rebase(intent_id, actor=actor)
    return contracts.AuthoringIntentViewRecord.model_validate(result.model_dump(mode="json"))


def playbill_authoring_submit(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringSubmitResultRecord:
    check_permission("cruxible_authoring_submit", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.submit(intent_id, actor=actor)
    return contracts.AuthoringSubmitResultRecord.model_validate(result.model_dump(mode="json"))


def playbill_authoring_status(
    instance_id: str,
    intent_id: str,
) -> contracts.CandidateStatusRecord:
    check_permission("cruxible_authoring_status", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.status(intent_id, actor=actor)
    return contracts.CandidateStatusRecord.model_validate(result.model_dump(mode="json"))


def playbill_block_declare(
    instance_id: str,
    stamp: ProjectionBlockStampAny,
) -> contracts.BlockDeclareResult:
    """Register one projection block a workspace just stamped into its page."""

    check_permission("cruxible_block_declare", instance_id=instance_id)
    instance = get_playbill_manager().get(instance_id)
    _coordinator, actor = _authoring_coordinator(instance_id)
    return service_declare_playbill_block(
        instance,
        actor_id=actor.actor_id,
        stamp=stamp,
        declared_at=cast(str, format_datetime(utc_now())),
    )


def playbill_block_depublish(
    instance_id: str,
    source_id: str,
    block_id: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.BlockDepublishResult:
    """Release one declared block registration, addressed as the page names it."""

    check_permission("cruxible_block_depublish", instance_id=instance_id)
    with change_entry(dry_run, "direct"):
        _require_writer(instance_id)
        instance = get_playbill_manager().get(instance_id)
        return service_depublish_playbill_block(
            instance,
            source_id=source_id,
            block_id=block_id,
            dry_run=dry_run,
            at=at,
        )


def playbill_read_claim_batch(
    instance_id: str, *, request: ClaimReadBatchRequest
) -> ClaimReadBatchResult:
    check_permission("cruxible_read", instance_id=instance_id)
    result = service_read_claim_batch(get_playbill_manager().get(instance_id), request=request)
    _record_consumed_paths(
        instance_id,
        operation="playbill.claim.get",
        coordinate=AcceptedCoordinate.model_validate(result.coordinate.model_dump()),
        paths=tuple(str(view.envelope["path"]) for view in result.claims),
    )
    return result


def playbill_read_claim_backings(
    instance_id: str, *, request: ClaimBackingsRequest
) -> ClaimBackingsResult:
    check_permission("cruxible_read", instance_id=instance_id)
    return service_read_claim_backings(get_playbill_manager().get(instance_id), request=request)


def playbill_check_projection_blocks(
    instance_id: str,
    *,
    request: contracts.ProjectionCheckRequest,
) -> contracts.ProjectionCheckResult:
    from cruxible_core.service.authoring.projection_sync import service_check_projection_blocks

    check_permission("cruxible_read", instance_id=instance_id)
    return service_check_projection_blocks(get_playbill_manager().get(instance_id), request=request)


def playbill_read_block_sync_backing(
    instance_id: str,
    *,
    request: contracts.BlockSyncReadRequest,
) -> contracts.BlockSyncReadResult:
    check_permission("cruxible_read", instance_id=instance_id)
    return service_read_playbill_block_sync_backing(
        get_playbill_manager().get(instance_id),
        request=request,
    )


def playbill_query(
    instance_id: str,
    *,
    request: contracts.QueryRequest,
) -> contracts.QueryResultRecord:
    """Answer one ``query`` call (compact, spec or named) as one page of values."""

    check_permission("cruxible_read", instance_id=instance_id)
    served: set[str] = set()
    result = service_playbill_query(
        get_playbill_manager().get(instance_id),
        request=request,
        served_claims=served,
    )
    coordinate = AcceptedCoordinate.model_validate(
        result.receipt.coordinate.model_dump(mode="json")
    )
    if result.receipt.mode == "named" and request.name is not None:
        _record_consumed_paths(
            instance_id,
            operation="playbill.query.run",
            coordinate=coordinate,
            paths=(query_definition_path(request.name),),
        )
    if served:
        # Every Claim a row served is read at the answer's coordinate, as get
        # and the former claim_values read recorded it, named on the page or not.
        _record_consumed_paths(
            instance_id,
            operation="playbill.claim.get",
            coordinate=coordinate,
            paths=tuple(sorted(served)),
        )
    return result


def playbill_procedure_source_preview(
    instance_id: str, *, request: ProcedureSourcePreviewRequest
) -> ProcedureSourcePreview:
    from cruxible_core.service.procedures.source_preview import service_preview_procedure_source

    check_permission("cruxible_get", instance_id=instance_id)
    return service_preview_procedure_source(
        get_playbill_manager().get(instance_id), request=request
    )


def playbill_procedure_readiness(
    instance_id: str,
    name: str,
    *,
    request: ProcedureReadinessRequestV1,
) -> contracts.ProcedureReadiness:
    check_permission("cruxible_get", instance_id=instance_id)
    result = service_playbill_procedure_readiness(
        get_playbill_manager().get(instance_id),
        name=name,
        request=request,
    )
    return contracts.ProcedureReadiness.model_validate(result.model_dump(mode="json"))


def _check_run_target_permission(
    tool_name: str, instance_id: str, target_rung: Callable[[], int]
) -> None:
    """Gate a run by what its target can do, after the read-tier and hosted gates.

    The verb itself runs the static read-tier pre-gate and the hosted-execution
    gate first, so a shared hosted profile refuses before any instance is read.
    Running an observe-only Line or Procedure is a read. One whose terminals
    can propose or settle writes governed state, so it needs governed write.
    A target with no live accepted artifact has nothing to gate: the service
    refuses it after its own request checks, and re-checks the caller's tier
    for any target it finds.
    """

    try:
        rung = target_rung()
    except (ProcedureNotFound, ProcedureRetired, LineRunNotAccepted):
        check_permission(tool_name, instance_id=instance_id)
        return
    required = PermissionMode(run_permission_rung(rung) + 1)
    check_permission(tool_name, instance_id=instance_id, required_override=required)


def playbill_procedure_run(
    instance_id: str,
    name: str,
    *,
    request: ProcedureRunRequest,
) -> contracts.ProcedureRunState:
    check_permission("cruxible_procedure_run", instance_id=instance_id, audit_success=False)
    # A shared hosted profile with no isolated execution backend cannot run
    # customer code at all; refuse at the served boundary so the operator gets
    # the mapped error instead of a node refusal buried in a run journal.
    enforce_customer_code_execution_supported()
    _check_run_target_permission(
        "cruxible_procedure_run",
        instance_id,
        lambda: procedure_run_target_rung(
            get_playbill_manager().get(instance_id), name, request.at
        ),
    )
    actor = _write_actor_context(instance_id)
    if actor is None:
        raise AuthenticationError("Procedure run requires an authenticated actor identity")
    manager = get_playbill_manager()
    try:
        workspace_file_reader = manager.workspace_file_reader(instance_id)
    except WorkspaceFileReadRefused:
        # Reader construction is operational. Non-workspace runs remain usable;
        # the executor gives workspace occurrences their typed binding refusal.
        workspace_file_reader = None
    result = service_run_playbill_procedure(
        manager.get(instance_id),
        name=name,
        request=request,
        actor_context=actor,
        provider_runtime_operator=manager.provider_runtime_operator(),
        workspace_file_reader=workspace_file_reader,
        caller_rung=get_current_mode().value - 1,
    )
    instance = manager.get(instance_id)
    consumption_context = ConsumptionContextV1(
        actor_context=actor,
        access_profile_id=coverage_access_profile().profile_id,
    )
    if consumption_receipts_enabled():
        record_consumption(
            instance,
            context=consumption_context,
            operation="playbill.procedure.run.resolve",
            coordinate=result.coordinate,
            artifacts=consumption_artifacts_for_dependency_closure(
                instance,
                result.coordinate,
                procedure_path(name),
            ),
        )
    else:
        note_consumption_unobserved(
            instance, context=consumption_context, coordinate=result.coordinate
        )
    return contracts.ProcedureRunState.model_validate(result.model_dump(mode="json"))


def playbill_procedure_run_status(
    instance_id: str,
    run_id: str,
) -> contracts.ProcedureRunState:
    """One run's state; another principal's enabling credential is withheld as on its card."""

    from cruxible_core.service.discovery.runs import procedure_run_status

    check_permission("cruxible_get", instance_id=instance_id)
    return procedure_run_status(
        get_playbill_manager().get(instance_id),
        run_id,
        viewer=_operational_viewer(instance_id),
    )


def playbill_procedure_measure(
    instance_id: str,
    name: str,
    *,
    request: contracts.ProcedureMeasureRequest,
) -> contracts.ProcedureMeasureResult:
    """Evaluate due measurements, persist their resolutions, and credit one run.

    The served due/pending/resume door: a retry replays the standing
    resolution and reading rather than minting a second one, and a crash
    between the two appends resumes at the reading.
    """

    check_permission("cruxible_procedure_measure", instance_id=instance_id)
    actor_context = _write_actor_context(instance_id)
    if actor_context is None:
        raise AuthenticationError("Measurement evaluation requires an authenticated actor identity")
    return service_measure_playbill_procedure(
        get_playbill_manager().get(instance_id),
        name=name,
        request=request,
        actor_context=actor_context,
        recorded_at=actor_context.timestamp,
    )


def playbill_procedure_readings(
    instance_id: str,
    name: str,
    *,
    request: contracts.ProcedureReadingsRequest,
) -> contracts.ProcedureReadingsResult:
    """Inspect measurement standing and retained readings. Never writes."""

    check_permission("cruxible_procedure_readings", instance_id=instance_id)
    return service_list_playbill_procedure_readings(
        get_playbill_manager().get(instance_id),
        name=name,
        request=request,
        evaluation_time=_evaluation_time(None),
    )


def playbill_line_enable(
    instance_id: str, line: str, *, dry_run: bool | None = None, at: str | None = None
) -> contracts.LineEnablement:
    """Enable a Line forward-only under the calling credential."""

    check_permission("cruxible_line_enable", instance_id=instance_id)
    from cruxible_core.runtime.line_arms import current_arm_principal
    from cruxible_core.service.procedures.line_dispatch import service_enable_line

    with change_entry(dry_run, "direct"):
        # Resolved behind the guards: a principal claim's check opens the instance.
        actor = _write_actor_context(instance_id)
        if actor is None:
            raise AuthenticationError("Enabling a Line requires an authenticated actor identity")
        manager = get_playbill_manager()
        return service_enable_line(
            manager.get(instance_id),
            line,
            principal=current_arm_principal(),
            actor=actor,
            now=_evaluation_time(None),
            daemon_id=manager.consumer_runner.daemon_id,
            dry_run=dry_run,
            at=at,
        )


def playbill_line_disable(
    instance_id: str, line: str, *, dry_run: bool | None = None, at: str | None = None
) -> contracts.LineEnablement:
    """Stop a Line admitting work on its own; admitted runs are not cancelled."""

    check_permission("cruxible_line_disable", instance_id=instance_id)
    from cruxible_core.service.procedures.line_dispatch import service_disable_line

    with change_entry(dry_run, "direct"):
        actor = _write_actor_context(instance_id)
        if actor is None:
            raise AuthenticationError("Disabling a Line requires an authenticated actor identity")
        return service_disable_line(
            get_playbill_manager().get(instance_id),
            line,
            actor=actor,
            now=_evaluation_time(None),
            dry_run=dry_run,
            at=at,
        )


def playbill_line_evaluate(
    instance_id: str, line: str, *, request: contracts.LineEvaluateRequest
) -> contracts.LineEvaluateResult:
    """Evaluate a Line's Triggers over a range: a dry run reads, otherwise it enqueues.

    A dry run is a read; enqueueing records dispatch state, so it needs governed write.
    """

    check_permission(
        "cruxible_line_evaluate",
        instance_id=instance_id,
        required_override=None if request.dry_run else PermissionMode.GOVERNED_WRITE,
    )
    actor = None if request.dry_run else _write_actor_context(instance_id)
    if actor is None and not request.dry_run:
        raise AuthenticationError("Evaluation requires an authenticated actor identity")
    from cruxible_core.service.procedures.line_dispatch import service_evaluate_line

    return service_evaluate_line(
        get_playbill_manager().get(instance_id),
        line,
        request,
        actor=actor,
        now=_evaluation_time(None),
    )


def playbill_line_dispatch(
    instance_id: str, line: str, *, request: contracts.LineDispatchRequest
) -> contracts.LineDispatchResult:
    check_permission("cruxible_line_dispatch", instance_id=instance_id, audit_success=False)
    # A shared hosted profile with no isolated execution backend cannot run
    # customer code at all; refuse at the served boundary so the operator gets
    # the mapped error instead of a node refusal buried in a run journal.
    enforce_customer_code_execution_supported()
    _check_run_target_permission(
        "cruxible_line_dispatch",
        instance_id,
        lambda: line_run_target_rung(get_playbill_manager().get(instance_id), line),
    )
    actor = _write_actor_context(instance_id)
    if actor is None:
        raise AuthenticationError("Dispatch requires an authenticated actor identity")
    from cruxible_core.service.procedures.line_dispatch import service_dispatch_line

    manager = get_playbill_manager()
    try:
        workspace_file_reader = manager.workspace_file_reader(instance_id)
    except WorkspaceFileReadRefused:
        workspace_file_reader = None
    return service_dispatch_line(
        manager.get(instance_id),
        line,
        request,
        actor=actor,
        now=_evaluation_time(None),
        caller_rung=get_current_mode().value - 1,
        provider_runtime_operator=manager.provider_runtime_operator(),
        workspace_file_reader=workspace_file_reader,
    )


def playbill_line_run(
    instance_id: str,
    line_identity_digest: str,
    *,
    request: LineRunRequest,
) -> contracts.ProcedureRunState:
    check_permission("cruxible_line_run", instance_id=instance_id, audit_success=False)
    # A shared hosted profile with no isolated execution backend cannot run
    # customer code at all; refuse at the served boundary so the operator gets
    # the mapped error instead of a node refusal buried in a run journal.
    enforce_customer_code_execution_supported()
    _check_run_target_permission(
        "cruxible_line_run",
        instance_id,
        lambda: line_run_target_rung(get_playbill_manager().get(instance_id), line_identity_digest),
    )
    actor = _write_actor_context(instance_id)
    if actor is None:
        raise AuthenticationError("Line run requires an authenticated actor identity")
    manager = get_playbill_manager()
    try:
        workspace_file_reader = manager.workspace_file_reader(instance_id)
    except WorkspaceFileReadRefused:
        # Reader construction is operational, exactly as on the direct lane:
        # a Line with no workspace occurrence still runs, and a workspace
        # occurrence gets its typed binding refusal inside the run journal.
        workspace_file_reader = None
    result = service_run_playbill_line(
        manager.get(instance_id),
        path_identity_digest=line_identity_digest,
        request=request,
        actor_context=actor,
        caller_rung=get_current_mode().value - 1,
        provider_runtime_operator=manager.provider_runtime_operator(),
        workspace_file_reader=workspace_file_reader,
        evaluation_instant_skew=manager.procedure_run_config().evaluation_instant_skew,
    )
    return contracts.ProcedureRunState.model_validate(result.model_dump(mode="json"))


def _caller_authoring_refusal(instance_id: str) -> AuthoringRefusal | None:
    """Why this caller cannot author here, as whoami reports it, less the tier.

    ``next`` gates each repair's tier itself; this is the rest -- an unbound
    credential, an unconfigured, unregistered or inactive principal, or a
    decommissioned instance -- which refuses every repair that writes.
    """

    try:
        refusal = playbill_whoami(instance_id).authoring_refusal
    except AuthenticationError:
        return None
    if (
        refusal is None
        or normalize_code(refusal.code) == "cruxible.identity.permission_insufficient"
    ):
        return None
    return refusal


def playbill_next(
    instance_id: str,
    *,
    request: NextRequestV1 | Mapping[str, object],
) -> contracts.NextResult:
    check_permission("cruxible_next", instance_id=instance_id)
    lane_state, lane_code, lane_detail = (
        get_playbill_manager().provider_runtime_operator().lane_status()
    )
    # The caller's principal comes from the authenticated transport, as
    # `whoami` resolves it, so no request can ask for another signer's work.
    actor = _actor_context()
    result = service_playbill_next(
        get_playbill_manager().get(instance_id),
        request=validate_playbill_next_request(request),
        provider_lane=contracts.ProviderLaneStatus(
            state=lane_state,
            code=lane_code,
            detail=lane_detail,
        ),
        caller_principal_id=None if actor is None else actor.actor_id,
        consumers_running=get_playbill_manager().consumer_runner.running,
        caller_rung=get_current_mode().value - 1,
        caller_authoring_refusal=_caller_authoring_refusal(instance_id),
    )
    return contracts.NextResult.model_validate(result.model_dump(mode="json"))


def playbill_curation_list(
    instance_id: str,
    *,
    request: PlaybillCurationListRequestV1 | Mapping[str, object],
) -> contracts.CurationListResult:
    check_permission("cruxible_curation_list", instance_id=instance_id)
    parsed = validate_playbill_curation_list_request(request)
    result = service_list_playbill_curation(get_playbill_manager().get(instance_id), request=parsed)
    return contracts.CurationListResult.model_validate(result.model_dump(mode="json"))


def playbill_curation_observe(
    instance_id: str,
    *,
    request: PlaybillCurationObserveRequestV1 | Mapping[str, object],
) -> contracts.CurationObserveResult:
    check_permission("cruxible_curation_observe", instance_id=instance_id)
    parsed = validate_playbill_curation_observe_request(request)
    with change_entry(parsed.dry_run, "direct"):
        result = service_observe_playbill_curation_blocks(
            get_playbill_manager().get(instance_id),
            request=parsed,
            actor_context=_curation_actor(instance_id),
        )
    return contracts.CurationObserveResult.model_validate(result.model_dump(mode="json"))


def playbill_audit(
    instance_id: str,
    *,
    request: PlaybillAuditRequestV1 | Mapping[str, object],
) -> contracts.AuditResult:
    check_permission("cruxible_audit", instance_id=instance_id)
    actor = _actor_context()
    if actor is None:
        raise AuthenticationError("Cruxible audit reads require an attributed actor")
    parsed = validate_playbill_audit_request(request)
    result = service_playbill_audit(
        get_playbill_manager().get(instance_id),
        request=parsed,
        actor_context=actor,
    )
    return contracts.AuditResult.model_validate(result.model_dump(mode="json"))


def _curation_actor(instance_id: str) -> GovernedActorContext:
    actor = _write_actor_context(instance_id)
    if actor is None:
        raise AuthenticationError("Cruxible curation actions require an attributed actor")
    return actor


def playbill_curation_overrule(
    instance_id: str,
    *,
    request: PlaybillCurationOverruleRequestV1 | Mapping[str, object],
) -> contracts.CurationActionResult:
    check_permission("cruxible_curation_overrule", instance_id=instance_id)
    parsed = _curation_validation_boundary(
        lambda: (
            request
            if isinstance(request, PlaybillCurationOverruleRequestV1)
            else PlaybillCurationOverruleRequestV1.model_validate(request)
        )
    )
    with change_entry(parsed.dry_run, "direct"):
        result = service_overrule_playbill_curation(
            get_playbill_manager().get(instance_id),
            request=parsed,
            actor_context=_curation_actor(instance_id),
        )
    return contracts.CurationActionResult.model_validate(result.model_dump(mode="json"))


def playbill_curation_accept_fixed(
    instance_id: str,
    *,
    request: PlaybillCurationAcceptFixedRequestV1 | Mapping[str, object],
) -> contracts.CurationActionResult:
    check_permission("cruxible_curation_accept_fixed", instance_id=instance_id)
    parsed = _curation_validation_boundary(
        lambda: (
            request
            if isinstance(request, PlaybillCurationAcceptFixedRequestV1)
            else PlaybillCurationAcceptFixedRequestV1.model_validate(request)
        )
    )
    with change_entry(parsed.dry_run, "direct"):
        result = service_accept_fixed_playbill_curation(
            get_playbill_manager().get(instance_id),
            request=parsed,
            actor_context=_curation_actor(instance_id),
        )
    return contracts.CurationActionResult.model_validate(result.model_dump(mode="json"))


def playbill_curation_suppress(
    instance_id: str,
    *,
    request: PlaybillCurationSuppressRequestV1 | Mapping[str, object],
) -> contracts.CurationActionResult:
    check_permission("cruxible_curation_suppress", instance_id=instance_id)
    parsed = _curation_validation_boundary(
        lambda: (
            request
            if isinstance(request, PlaybillCurationSuppressRequestV1)
            else PlaybillCurationSuppressRequestV1.model_validate(request)
        )
    )
    with change_entry(parsed.dry_run, "direct"):
        result = service_suppress_playbill_curation(
            get_playbill_manager().get(instance_id),
            request=parsed,
            actor_context=_curation_actor(instance_id),
        )
    return contracts.CurationActionResult.model_validate(result.model_dump(mode="json"))


def playbill_curation_unsuppress(
    instance_id: str,
    *,
    request: PlaybillCurationUnsuppressRequestV1 | Mapping[str, object],
) -> contracts.CurationActionResult:
    check_permission("cruxible_curation_unsuppress", instance_id=instance_id)
    parsed = _curation_validation_boundary(
        lambda: (
            request
            if isinstance(request, PlaybillCurationUnsuppressRequestV1)
            else PlaybillCurationUnsuppressRequestV1.model_validate(request)
        )
    )
    with change_entry(parsed.dry_run, "direct"):
        result = service_unsuppress_playbill_curation(
            get_playbill_manager().get(instance_id),
            request=parsed,
            actor_context=_curation_actor(instance_id),
        )
    return contracts.CurationActionResult.model_validate(result.model_dump(mode="json"))


def playbill_since(
    instance_id: str,
    *,
    request: contracts.SinceRequest | Mapping[str, object],
) -> contracts.SinceResult:
    check_permission("cruxible_since", instance_id=instance_id)
    parsed = validate_playbill_since_request(request)
    return service_playbill_since(get_playbill_manager().get(instance_id), request=parsed)


def playbill_resolve_coverage(
    instance_id: str,
    *,
    observations: tuple[WorkingSourceObservation, ...],
    at: AcceptedCoordinate | None = None,
    budget: CoverageCardBudget | None = None,
    scan_budget: CoverageScanBudget | None = None,
) -> contracts.CoverageResult:
    """Resolve one batch of working-set observations into the current coverage result.

    The one vendor-neutral coverage operation of §11.7. Every request form it
    has to serve -- a file read with a line/range selection, a grep result
    batch, a set of changed filesystem paths, an explicit source occurrence, and
    a working-set scope -- arrives here already reduced by the adapter to
    observations and the spans they carry, because an adapter contains no
    semantic logic and this operation reads no filesystem.

    A successful outer call appends local, idempotent per-artifact consumption
    receipts.  They add no authority and enter no accepted-state or answer
    digest; they only account for which accepted artifacts this service
    actually delivered.

    The access profile is derived from this surface's read authority and is
    never accepted from the caller, so a request cannot widen its own
    disclosure.
    """

    check_permission("cruxible_read", instance_id=instance_id)
    result = service_resolve_playbill_coverage(
        get_playbill_manager().get(instance_id),
        instance_id=instance_id,
        observations=observations,
        at=at,
        budget=budget,
        scan_budget=scan_budget,
    )
    claim_paths = tuple(
        address.artifact_path
        for span in result.spans
        for card in span.cards
        for address in card.claim_addresses
    )
    _record_consumed_paths(
        instance_id,
        operation="playbill.coverage.resolve",
        coordinate=result.at,
        paths=claim_paths,
    )
    return contracts.CoverageResult(
        coordinate=contracts.AcceptedCoordinate.model_validate(result.at.model_dump(mode="json")),
        result=result.model_dump(mode="json"),
    )


def playbill_export_floor(
    instance_id: str,
    *,
    at: AcceptedCoordinate | None = None,
    include: tuple[contracts.FloorExportPart, ...] = (),
) -> contracts.FloorExport:
    """Return the deterministic floor as base64 bytes keyed by floor path.

    The service returns a path-to-bytes map and writes nothing; materializing a
    directory from this contract is the client's act, never the daemon's.
    """

    check_permission("cruxible_read", instance_id=instance_id)
    # Document bodies keep their own read boundary: the floor carries them only
    # for a caller who may read bodies, and says how to read them otherwise.
    may_read_bodies = get_current_mode() >= PERMISSION_REQUIREMENTS["cruxible_body_read"]
    files = service_export_playbill_floor(
        get_playbill_manager().get(instance_id),
        at=at,
        include=include,
        access=_access(instance_id, include_body=may_read_bodies),
    )
    manifest = json.loads(files[MANIFEST_PATH])
    return contracts.FloorExport(
        tag=manifest["format"],
        coordinate=contracts.AcceptedCoordinate.model_validate(manifest["coordinate"]),
        manifest=manifest,
        files=[
            contracts.FloorFile(
                path=path,
                content_base64=base64.b64encode(content).decode("ascii"),
            )
            for path, content in files.items()
        ],
    )


def playbill_floor_delta(
    instance_id: str,
    *,
    at: AcceptedCoordinate | None = None,
    base_generation: int | None = None,
    base_renderer: str | None = None,
) -> FloorDelta:
    """Return what brings a client's floor at ``base_generation`` to ``at`` (default: head).

    Deterministic for (head, base): the files whose ``changed_at`` is after the
    base, the paths dropped since, and both manifest digests; or, for a
    missing, unknown, newer or foreign-renderer base, the whole floor.
    """

    check_permission("cruxible_read", instance_id=instance_id)
    instance = get_playbill_manager().get(instance_id)
    head = AcceptedCoordinate.from_internal(instance.accepted_coordinate()) if at is None else at
    return service_playbill_floor_delta(
        instance, head=head, base_generation=base_generation, base_renderer=base_renderer
    )


def _credential_principal_resolver(instance_id: str) -> Callable[[str], str | None]:
    """Resolve a runtime credential of this instance to the principal it is bound to."""

    from cruxible_core.server.credentials import get_runtime_credential_store

    store = get_runtime_credential_store()

    def resolve(credential_id: str) -> str | None:
        # Revoked records still name their principal: a rotation revokes the
        # arming credential, and its principal keeps seeing its own arms.
        record = store.get(credential_id)
        if record is None or record.instance_id != instance_id:
            return None
        return record.principal_id

    return resolve


def _operational_viewer(instance_id: str) -> OperationalViewer:
    """Who reads operational state here, as the transport authenticated them.

    Arming credentials are shown only to an admin, themselves, or another
    credential bound to the same principal: only a bearer credential bound to
    a principal sees that principal's other credentials; an unbound
    credential or a claim never does.
    """

    from cruxible_core.service.discovery.operational_viewer import OperationalViewer

    auth = get_current_auth_context()
    bearer = auth if auth is not None and auth.credential_type == "runtime_credential" else None
    bound_to = None if bearer is None else bearer.principal_id
    return OperationalViewer(
        credential_id=None if bearer is None else bearer.credential_id,
        admin=get_current_mode() >= PermissionMode.ADMIN,
        principal_id=bound_to,
        credential_principal=(
            None if bound_to is None else _credential_principal_resolver(instance_id)
        ),
    )


def playbill_get(instance_id: str, *, request: GetRequest) -> GetResult:
    """One governed thing by reference; a Document body needs body-read permission."""

    from cruxible_client.contracts.claim_types import claim_type_path
    from cruxible_client.contracts.query.definitions import query_definition_path
    from cruxible_core.service.discovery.get import service_playbill_get

    check_permission("cruxible_get", instance_id=instance_id)
    # Consumption is recorded at the full coordinate, which a summary answer
    # leaves out unless the caller asked for it.
    result = service_playbill_get(
        get_playbill_manager().get(instance_id),
        request=request.model_copy(update={"full_coordinate": True}),
        # A Document's why maps its source to its body when the caller may read
        # bodies; it never refuses for want of that permission.
        access=_access(
            instance_id,
            include_body=request.detail == "body"
            or (
                request.detail == "why" and _permits("cruxible_body_read", instance_id=instance_id)
            ),
        ),
        installed_classifier_digests=(
            PROVIDER_BUCKET_CLASSIFIER_REGISTRY.installed_classifier_digests
        ),
        viewer=_operational_viewer(instance_id),
    )
    read_at = result.accepted_coordinate
    assert read_at is not None
    if not request.full_coordinate and request.detail != "proof":
        result = result.model_copy(update={"accepted_coordinate": None})
    consumed: dict[str, tuple[ConsumptionOperation, str]] = {
        "claim": ("playbill.claim.get", claim_path(result.ref) if result.kind == "claim" else ""),
        "subject": ("playbill.subject.get", f"subjects/{result.ref}.json"),
        "claim_type": (
            "playbill.claim_type.get",
            claim_type_path(result.ref.removeprefix("ClaimType:"))
            if result.kind == "claim_type"
            else "",
        ),
        "query": (
            "playbill.query_definition.get",
            query_definition_path(result.ref.removeprefix("query:"))
            if result.kind == "query"
            else "",
        ),
    }
    if result.kind in consumed and request.detail != "history":
        operation, path = consumed[result.kind]
        _record_consumed_paths(
            instance_id,
            operation=operation,
            coordinate=AcceptedCoordinate.model_validate(read_at.model_dump()),
            paths=(path,),
        )
    return result


def playbill_get_batch(instance_id: str, *, request: GetBatchRequest) -> GetBatchResult:
    """Several references at one coordinate: each is one ``get``, pinned to the first's."""

    results: list[GetResult] = []
    pinned: contracts.AcceptedCoordinate | str | None = request.at
    for ref in request.refs:
        result = playbill_get(
            instance_id,
            request=GetRequest(
                ref=ref,
                detail=request.detail,
                at=pinned,
                evaluation_time=request.evaluation_time,
                surface=request.surface,
                full_coordinate=True,
            ),
        )
        pinned = result.accepted_coordinate
        results.append(result)
    assert isinstance(pinned, contracts.AcceptedCoordinate)
    return GetBatchResult(coordinate=pinned, results=tuple(results))


__all__ = [name for name in globals() if name.startswith("playbill_")]
