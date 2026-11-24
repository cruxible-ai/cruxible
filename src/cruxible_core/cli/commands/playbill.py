"""Cruxible Family-1 CLI, including local compilation and client-held signing."""

from __future__ import annotations

import getpass
import json
import os
import shlex
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar, cast, get_args

import click
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from cruxible_client import (
    CruxibleClient,
    contracts,
    observe_next_workspace,
)
from cruxible_client._error_base import CoreError, printable
from cruxible_client.artifacts import (
    is_layout,
    unpack_artifact,
    write_layout,
)
from cruxible_client.authoring.attestations import (
    local_attestation_signer_from_environment,
    principal_records,
)
from cruxible_client.authoring.bind import bind_catalogued_selection_input
from cruxible_client.authoring.blocks import repin_projection_block, sync_projection_blocks
from cruxible_client.authoring.compact_query import (
    WHERE_SYNTAX,
    parse_where,
    render_query_table,
)
from cruxible_client.authoring.context import ContextResolutionError, resolve_context
from cruxible_client.authoring.doc_writes import write_workspace
from cruxible_client.authoring.examples import (
    AUTHORING_EXAMPLE_FACTORIES,
    AUTHORING_EXAMPLE_NAMES,
    AuthoringExampleName,
    authoring_example,
    authoring_example_note,
    document_example,
)
from cruxible_client.authoring.files import (
    FileCatalogEdit,
    catalog_file,
    compile_client_file_context,
    load_file_catalog,
    remove_catalog_entry,
    root_aliases,
)
from cruxible_client.authoring.folder_identity import write_folder_identity
from cruxible_client.authoring.inputs import AuthoringInput, ClaimInput
from cruxible_client.authoring.principal_settings import principal_key, principal_key_missing
from cruxible_client.authoring.sdk_types import SourceSelectionError
from cruxible_client.authoring.selectors import WorkspaceFiles, catalog_file_paths
from cruxible_client.authoring.signing import sign_runtime_credential_mint
from cruxible_client.authoring.vocabulary_file import (
    vocabulary_apply_for,
    vocabulary_export_for,
    vocabulary_plan_for,
)
from cruxible_client.authoring.workspace import (
    WorkspaceAttachmentError,
    daemon_floor_delivery,
    floor_export_parts,
    next_unobserved_reasons,
    observe_next_workspace_with_coverage,
    observe_projection_coverage,
    validate_workspace_config_write,
    workspace_floor_freshness,
    workspace_vocabulary_at,
    workspace_vocabulary_freshness,
    write_workspace_config,
    write_workspace_floor,
    write_workspace_floor_delta,
)
from cruxible_client.authoring.world_stub import render_world_stub_for, write_world_stub
from cruxible_client.authoring.write_evidence import (
    observe_changes,
    rebind_evidence,
)
from cruxible_client.compatibility import require_authoring_compatibility
from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.authoring.models import BlockSyncResult
from cruxible_client.contracts.authoring_landing import AuthoringAccept
from cruxible_client.contracts.builtin_claim_types import BUILTIN_CLAIM_TYPES, BuiltinClaimTypeName
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.capture_contract_inputs import (
    CaptureContractInputRecord,
    capture_contract_input_template,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimStance,
)
from cruxible_client.contracts.codes import normalize_code
from cruxible_client.contracts.compact_query import QueryClaim
from cruxible_client.contracts.compiler_upgrade_landing import (
    CompilerUpgradeLanded,
    CompilerUpgradeProposed,
    CompilerUpgradeResult,
    CompilerUpgradeSigningKey,
)
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.duplicates import (
    DUPLICATES_AUDIT_DEFAULT_LIMIT,
    DUPLICATES_AUDIT_MAX_LIMIT,
)
from cruxible_client.contracts.errors import (
    CanonicalEncodingError,
    ReviewedCandidateChanged,
    SinceRequestInvalid,
)
from cruxible_client.contracts.file_catalog import FileCatalog, FileCompilationBundle
from cruxible_client.contracts.get_text import attestation_counts_text as _attestation_counts_text
from cruxible_client.contracts.get_text import backlink_line as _backlink_line
from cruxible_client.contracts.get_text import (
    file_ref_shown,
    history_lines,
    proposed_reason_lines,
    render_get,
)

# Re-exported under the names the CLI's tests read them by.
from cruxible_client.contracts.get_text import (  # noqa: F401
    get_value_text as _get_value_text,
)
from cruxible_client.contracts.get_text import (  # noqa: F401
    provider_operation_line as _provider_operation_line,
)
from cruxible_client.contracts.get_text import run_node_line as _run_node_line
from cruxible_client.contracts.kit_requires import KitRequirement
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitAutomationTemplate,
    KitBuildRequest,
    KitChangeResult,
    KitRemoveRequest,
    KitRequirementStatus,
    kit_version_key,
)
from cruxible_client.contracts.line_dispatch import LineDispatchItem, LineEvaluateResult
from cruxible_client.contracts.line_inbox import compact_inbox_list
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.principals import is_canonical_principal_id
from cruxible_client.contracts.procedures.results import (
    ProcedureHaltTerminal,
    ProcedureTerminalEgressChild,
)
from cruxible_client.contracts.procedures.windows import TriggerEventReference
from cruxible_client.contracts.proposal_models import canonical_proposal_ref_name
from cruxible_client.contracts.provider_installation import (
    ProviderInstallRequest,
    ProviderInstallResult,
    ProviderMoveRequest,
    ProviderRetireRequest,
)
from cruxible_client.contracts.public_names import public_name as _public_name
from cruxible_client.contracts.repair_spelling import render_repair
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_client.contracts.resolution_contracts import ResolutionContractReference
from cruxible_client.contracts.temporal import (
    parse_datetime,
    parse_iso_duration_microseconds,
)
from cruxible_client.contracts.text_display import (  # noqa: F401
    NEXT_STATUS_ATTENTION as _NEXT_STATUS_ATTENTION,
)
from cruxible_client.contracts.text_display import (
    awaiting_text,
    landing_lines,
    next_lines,
    render_orient,
    render_review_summary,
    short_oid,
    write_outcome_lines,
)
from cruxible_client.contracts.text_display import (  # noqa: F401
    next_claim_about as _next_claim_about,
)
from cruxible_client.contracts.text_display import (  # noqa: F401
    next_requirement_hint as _next_requirement_hint,
)
from cruxible_client.contracts.types import PrincipalKind, PrincipalRecord
from cruxible_client.contracts.validation_messages import validation_summary
from cruxible_client.contracts.workspace_layout import WorkspaceDirectoryConflict
from cruxible_client.contracts.write import (
    AddChange,
    AppendSection,
    Change,
    ContractEvidence,
    DocOp,
    RetireRequest,
    SetChange,
    SetRequest,
    SubjectRef,
    WriteOutcome,
    WriteRequest,
    as_write_request,
    capture_handle,
)
from cruxible_client.errors import DataValidationError
from cruxible_client.kits import (
    KIT_ARTIFACT,
    add_kit_files,
    check_kit_updates,
    compact_kit_change,
    kit_automation_template,
    read_kit_build_files,
    resolve_kit,
    resolve_kit_image,
    stage_kit_providers,
    with_automation_template,
    write_kit_directory,
)
from cruxible_core.adapters.block_detach import detach_projection_pages
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord, claim_type_input_template
from cruxible_core.claims.claim_type_migrations import ClaimTypeMigrationRequest
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _can_confirm,
    _dispatch_cli,
    _echo_active_write_target,
    _echo_creation_write_target,
    _emit_brief,
    _emit_json,
    _remember_principal_settings,
    _require_instance_id,
    _root_ctx_obj,
    _set_aside_folder_identity,
    _transport_target,
    brief_option,
    change_control_options,
    echo_preview_next,
    ensure_cli_daemon,
    json_option,
    no_daemon_message,
)
from cruxible_core.cli.context import load_cli_context
from cruxible_core.cli.main import handle_errors
from cruxible_core.cli.payloads import (
    PayloadFile,
    payload_label,
    read_payload_bytes,
    read_payload_document,
    read_payload_text,
)
from cruxible_core.cli.payloads import model_field_errors as _model_field_errors
from cruxible_core.cli.payloads import read_mapping as _read_mapping
from cruxible_core.cli.payloads import read_model as _read_model
from cruxible_core.cli.principal_settings import (
    PRINCIPAL_KEY_ENV,
    PRINCIPAL_SETTINGS_FILE,
    default_key_dir,
    write_principal_settings,
)
from cruxible_core.cli.provider_wheels import (
    install_provider_wheel,
    stage_kit_provider_directory,
)
from cruxible_core.coverage.adapter import (
    WorkingPathBindingsV1,
    WorkingSourceObservation,
)
from cruxible_core.coverage.contracts import CoverageAccessProfile, CoverageResultV3
from cruxible_core.coverage.render import (
    render_coverage_manifest,
)
from cruxible_core.coverage.workspace import bindings_from_mapping, observe_workspace
from cruxible_core.curation.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    AUDIT_BUDGET_MAX_MAX_BYTES,
    AUDIT_BUDGET_MAX_MAX_ROWS,
    AUDIT_BUDGET_MIN_MAX_BYTES,
    AUDIT_BUDGET_MIN_MAX_ROWS,
)
from cruxible_core.floor.workspace_advertisement import containing_git_workspace_root
from cruxible_core.governance.init_custody import init_resume_marker, prepare_init_custody
from cruxible_core.governance.keys import (
    GeneratedKeyMaterial,
    generate_client_principal_key,
    preview_client_principal,
    validate_client_principal_key_target,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.kits.base_kit import bundled_kit_reference
from cruxible_core.ledger.signing import LocalEd25519ApprovalSigner
from cruxible_core.server.config import get_runtime_bearer_token, get_server_state_root
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
)
from cruxible_core.service.proposals.review import (
    ProposalReview,
    render_playbill_approval_summary,
    render_playbill_proposal_review,
    render_playbill_proposal_review_pointer,
)
from cruxible_core.workspace_root import (
    WorkspaceBoundaryRefused,
    WorkspaceRoot,
    WorkspaceRootSource,
    refuse_unusable_root,
    resolve_workspace_root,
)

ResultT = TypeVar("ResultT")


def _parse_expiring_duration(
    _context: click.Context,
    _parameter: click.Parameter,
    value: str,
) -> int:
    try:
        return parse_iso_duration_microseconds(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


def _server_call(
    operation: Callable[[CruxibleClient, str], ResultT],
    *,
    command_name: str,
) -> ResultT:
    instance_id = _require_instance_id()
    result = _dispatch_cli(
        lambda client: operation(client, instance_id),
        lambda: None,
        allow_local=False,
        command_name=command_name,
    )
    return cast(ResultT, result)


def _read_since_access_profile(path: str) -> dict[str, Any]:
    """Read a CoverageAccessProfile file for since, filling model defaults.

    A file that is not a valid profile surfaces the same typed since refusal
    the daemon would give, before any request is built.
    """
    payload = _read_mapping(path)
    try:
        return CoverageAccessProfile.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise SinceRequestInvalid.from_validation_errors(
            [
                {**err, "loc": ("access_profile", *err.get("loc", ()))}
                for err in exc.errors(include_url=False)
            ]
        ) from exc


_AUTHORING_INPUT_ADAPTER: TypeAdapter[AuthoringInput] = TypeAdapter(AuthoringInput)
_CLAIM_TYPE_MIGRATION_ADAPTER: TypeAdapter[ClaimTypeMigrationRequest] = TypeAdapter(
    ClaimTypeMigrationRequest
)


def _authoring_examples_for(payload: Mapping[str, Any]) -> tuple[str, ...]:
    kind = payload.get("kind")
    if kind == "procedure":
        return ("procedure",)
    if kind == "query_definition":
        return ("query-definition", "query-claims-by-type")
    if kind != "claim":
        return tuple(AUTHORING_EXAMPLE_FACTORIES)
    source = payload.get("source")
    if isinstance(source, Mapping):
        if source.get("kind") == "working_selection":
            return ("claim-flow-a",)
        if source.get("kind") == "self_source":
            return ("claim-self-source",)
    return ("claim-flow-a", "claim-self-source")


def _validation_path(location: tuple[object, ...]) -> str:
    rendered = "$"
    for item in location:
        if isinstance(item, int):
            rendered += f"[{item}]"
        elif isinstance(item, str) and not item.startswith("playbill-"):
            rendered += f".{item}"
    return rendered


def _read_authoring_input(path: str) -> AuthoringInput:
    payload = _read_mapping(path)
    try:
        return _AUTHORING_INPUT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        examples = ", ".join(
            f"cruxible authoring example {name}" for name in _authoring_examples_for(payload)
        )
        errors = "; ".join(
            f"{_validation_path(tuple(item['loc']))}: {item['msg']}"
            for item in exc.errors(include_url=False)
        )
        raise click.ClickException(
            f"Invalid authoring input: {errors}. Matching example: {examples}"
        ) from exc


@dataclass(frozen=True)
class _GitWorkspaceEnvironmentNote:
    """Advisory that ambient Git repository selection lost to the process CWD."""

    code: Literal["inherited_git_workspace_ignored"]
    cwd_workspace_root: Path
    inherited_workspace_root: Path


@dataclass(frozen=True)
class _LocalWorkspaceResult:
    """The workspace root this command runs in, and any ignored ambient-repository advisory.

    ``git_root`` is the Git worktree the start directory lies in, whether or not
    the workspace root is that worktree (a TCP daemon cannot attach one).
    """

    workspace_root: Path | None
    note: _GitWorkspaceEnvironmentNote | None
    source: WorkspaceRootSource | None = None
    git_root: Path | None = None


def _inherited_git_workspace_root() -> Path | None:
    """Resolve only the advisory root selected by ambient Git variables."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            cwd=Path.cwd(),
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        return Path(completed.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _explicit_workspace_root(value: str) -> Path:
    """``--workspace PATH``: the workspace root PATH resolves to; any folder, Git or not."""

    try:
        selected = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise click.UsageError(f"--workspace is not an accessible directory: {value}") from exc
    try:
        found = resolve_workspace_root(selected)
    except WorkspaceDirectoryConflict as exc:
        raise click.UsageError(f"--workspace {value}: {exc}") from exc
    assert found is not None
    return found.root


def _workspace_config_transport() -> dict[str, str]:
    ensure_cli_daemon()
    obj = _root_ctx_obj()
    if obj.get("server_socket"):
        return {"server_socket": str(obj["server_socket"])}
    if obj.get("server_url"):
        return {"server_url": str(obj["server_url"])}
    raise click.UsageError("Server mode is required to attach a workspace")


def _workspace_discovery_off() -> bool:
    if _root_ctx_obj().get("no_workspace"):
        return True
    return os.environ.get("CRUXIBLE_NO_WORKSPACE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _local_workspace_root(
    *,
    fallback_to_start: bool = True,
    required: str | None = None,
    custody: bool = False,
) -> _LocalWorkspaceResult:
    """Resolve this command's workspace root from the CWD (`cruxible_core.workspace_root`).

    The nearest ancestor holding ``.cruxible/``, else the enclosing Git worktree,
    else (with ``fallback_to_start``) the CWD itself. An inherited repository
    selector (``GIT_DIR``) never chooses it. A root that is no workspace (home, a
    filesystem root, the state root) is None, or with ``required`` (the command
    name) a refusal naming why; so is any root while workspace discovery is off,
    except for ``custody``: keys stay out of a workspace whether or not this
    command uses it.
    """

    if _workspace_discovery_off() and not custody:
        if required is not None:
            raise click.UsageError(
                f"{required} needs a workspace folder, but workspace discovery is off "
                "(--no-workspace or CRUXIBLE_NO_WORKSPACE)"
            )
        return _LocalWorkspaceResult(workspace_root=None, note=None)
    named = os.environ.get("CRUXIBLE_WORKSPACE")
    try:
        start = Path(named).expanduser() if named else Path.cwd()
    except OSError as exc:
        if required is not None:
            raise click.UsageError(f"{required} needs a workspace folder: {exc}") from exc
        return _LocalWorkspaceResult(workspace_root=None, note=None)
    git_root = containing_git_workspace_root(start)
    try:
        # CRUXIBLE_WORKSPACE names the root itself: trusted whoever owns it.
        found = resolve_workspace_root(
            start,
            fallback_to_start=fallback_to_start or bool(named),
            trust=start if named else None,
        )
        if named and found is not None and found.root != start.resolve():
            found = WorkspaceRoot(refuse_unusable_root(start.resolve()), "directory")
    except OSError as exc:
        if required is not None:
            raise click.UsageError(f"{required} needs a workspace folder: {exc}") from exc
        return _LocalWorkspaceResult(workspace_root=None, note=None, git_root=git_root)
    except WorkspaceBoundaryRefused as exc:
        if required is not None or not custody:
            raise click.UsageError(str(exc)) from exc
        # Keys stay out of the untrusted workspace too.
        return _LocalWorkspaceResult(
            workspace_root=Path(exc.workspace), note=None, git_root=git_root
        )
    except WorkspaceDirectoryConflict as exc:
        if required is not None:
            raise click.UsageError(f"{required} needs a workspace folder: {exc}") from exc
        return _LocalWorkspaceResult(workspace_root=None, note=None, git_root=git_root)
    if found is None:
        return _LocalWorkspaceResult(workspace_root=None, note=None, git_root=git_root)
    repository_selectors = ("GIT_DIR", "GIT_WORK_TREE")
    note = None
    if found.source == "git" and any(name in os.environ for name in repository_selectors):
        inherited_root = _inherited_git_workspace_root()
        if inherited_root is not None and inherited_root != found.root:
            note = _GitWorkspaceEnvironmentNote(
                code="inherited_git_workspace_ignored",
                cwd_workspace_root=found.root,
                inherited_workspace_root=inherited_root,
            )
    return _LocalWorkspaceResult(
        workspace_root=found.root, note=note, source=found.source, git_root=git_root
    )


def _emit_git_workspace_note(resolution: _LocalWorkspaceResult) -> None:
    _root_ctx_obj()["git_workspace_note"] = (
        None
        if resolution.note is None
        else contracts.GitWorkspaceNote(
            code=resolution.note.code,
            cwd_workspace_root=str(resolution.note.cwd_workspace_root),
            inherited_workspace_root=str(resolution.note.inherited_workspace_root),
        )
    )
    if resolution.note is None:
        return
    note = resolution.note
    click.echo(
        f"Note [{note.code}]: CWD worktree {str(note.cwd_workspace_root)!r} takes "
        f"precedence over inherited Git worktree {str(note.inherited_workspace_root)!r}.",
        err=True,
    )


def _display_file_paths(workspace: Path | None = None) -> Mapping[str, str]:
    """The workspace catalog's file names to paths, read once for one command's text.

    Text names a cited file by its path; JSON keeps the stored name. ``workspace``
    is the command's own ``--workspace-root``; without one, the workspace around
    the CWD. No workspace or catalog maps nothing, and text shows names.
    """

    if workspace is None:
        try:
            workspace = _local_workspace_root(fallback_to_start=False).workspace_root
        except click.UsageError:
            return {}
    return catalog_file_paths(workspace)


def _existing_workspace_root() -> Path | None:
    """An existing workspace root around the CWD (``.cruxible/`` or Git), quietly."""

    resolution = _local_workspace_root(fallback_to_start=False, custody=True)
    return resolution.workspace_root or resolution.git_root


def _custody_workspace_root() -> Path | None:
    """Resolve the one workspace boundary shared by every custody operation.

    An existing workspace (a folder holding ``.cruxible/``, or a Git worktree);
    a plain folder becomes one when init or attach writes its ``.cruxible/``.
    """

    resolution = _local_workspace_root(fallback_to_start=False, custody=True)
    _emit_git_workspace_note(resolution)
    return resolution.workspace_root or resolution.git_root


def _with_git_workspace_note(result: Any) -> Any:
    note = _root_ctx_obj().get("git_workspace_note")
    if note is None:
        return result
    return result.model_copy(update={"git_workspace_note": note})


def _json_receipt(result: Any) -> dict[str, Any]:
    """Elide only an absent client advisory while preserving nested null fields."""

    payload = result.model_dump(mode="json")
    if payload.get("git_workspace_note") is None:
        payload.pop("git_workspace_note", None)
    return cast(dict[str, Any], payload)


def _forbidden_roots_for(workspace_root: Path | None) -> tuple[Path, ...]:
    return () if workspace_root is None else (workspace_root,)


def _custody_forbidden_roots() -> tuple[Path, ...]:
    return _forbidden_roots_for(_custody_workspace_root())


def _refuse_tcp_workspace_operation(git_workspace: Path | None) -> None:
    if git_workspace is not None and _root_ctx_obj().get("server_url"):
        raise click.UsageError(
            f"This command is running in Git worktree {str(git_workspace)!r}, but TCP cannot "
            "attach a daemon-local workspace. Use --server-socket for local attachment, or "
            "run from outside the worktree for an intentionally unattached remote host."
        )


def _creation_workspace(
    workspace_path: str | None, *, fallback_to_start: bool
) -> _LocalWorkspaceResult:
    """The workspace a host create or init attaches, and the TCP refusal it needs.

    ``--workspace PATH`` is taken as given (a remote host keeps it client-local).
    Otherwise it is found from the CWD; over TCP a Git worktree refuses (a remote
    daemon cannot attach it) and a plain folder stays unattached.
    """

    if workspace_path is not None:
        return _LocalWorkspaceResult(
            workspace_root=_explicit_workspace_root(workspace_path), note=None
        )
    tcp = bool(_root_ctx_obj().get("server_url"))
    resolution = _local_workspace_root(
        fallback_to_start=fallback_to_start and not tcp,
        required=(
            None if tcp or not fallback_to_start or _workspace_discovery_off() else "cruxible init"
        ),
    )
    _emit_git_workspace_note(resolution)
    _refuse_tcp_workspace_operation(resolution.git_root)
    if tcp:
        return _LocalWorkspaceResult(workspace_root=None, note=resolution.note)
    return resolution


def _write_bundle(path: str, bundle: FileCompilationBundle) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("xb") as handle:
            handle.write(canonical_bytes(bundle.model_dump(mode="json")) + b"\n")
    except FileExistsError as exc:
        raise click.ClickException(f"Refusing to overwrite existing bundle: {output}") from exc


def _root_aliases(values: tuple[str, ...]) -> dict[str, Path]:
    return root_aliases(values)


def _catalog(portable_path: str, local_path: str | None) -> FileCatalog:
    return load_file_catalog(
        Path(portable_path).expanduser(),
        None if local_path is None else Path(local_path).expanduser(),
    )


def _compile_remote_context(
    client: CruxibleClient,
    instance_id: str,
    *,
    catalog: FileCatalog,
    repository_root: Path,
    aliases: dict[str, Path],
) -> FileCompilationBundle:
    return compile_client_file_context(
        client,
        instance_id,
        catalog=catalog,
        repository_root=repository_root,
        aliases=aliases,
    )


@click.group("playbill")
def playbill_group() -> None:
    """Govern Documents through Cruxible's proposal and acceptance ledger."""


@playbill_group.group("host")
def host_group() -> None:
    """Allocate daemon-owned hosts without adopting config or semantic state."""


@playbill_group.group("workspace")
def workspace_group() -> None:
    """Bind local client configuration to an existing registered host."""


@workspace_group.command("attach")
@click.option("--instance-id", default=None, help="Existing registered daemon host ID.")
@click.option("--replace", is_flag=True, help="Replace a differing workspace config.")
@change_control_options
@click.option("--no-floor-delivery", is_flag=True, help="Opt out of daemon floor delivery.")
@json_option
@handle_errors
def attach_workspace(
    instance_id: str | None,
    no_floor_delivery: bool,
    replace: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Attach this folder (Git or not) to a daemon host, initialized or not.

    The folder is the nearest one holding .cruxible/, else the enclosing Git
    worktree, else the current directory. A host with no workspace registers
    this one (an initialized host included, as long as a Git worktree is in its
    ledger's Git object format); a host registered to this folder gets the
    client config, and its ledger mirror is rebound: after `git init` in a plain
    folder, attaching again turns the mirror on. An uninitialized host attaches
    the same way; `cruxible init` in the folder then creates its ledger and the
    world stub.
    """

    resolution = _local_workspace_root(required="cruxible workspace attach")
    _emit_git_workspace_note(resolution)
    workspace = resolution.workspace_root
    assert workspace is not None
    ensure_cli_daemon()
    if not _root_ctx_obj().get("server_socket"):
        raise click.UsageError(
            "cruxible workspace attach requires a local --server-socket so the daemon can "
            "prove its registered workspace path"
        )
    selected = instance_id or _require_instance_id()
    registration = _dispatch_cli(
        lambda client: client.host_workspace_registration(selected),
        lambda: None,
        allow_local=False,
        command_name="cruxible workspace attach",
    )
    assert isinstance(registration, contracts.HostWorkspaceRegistration)
    registered = registration.workspace_path
    attached: contracts.HostWorkspaceAttachResult | None = None
    if registration.status != "registered" or (
        registered is not None and Path(registered).resolve(strict=False) == workspace
    ):
        # Attaching the folder a host already has registers nothing and rebinds
        # its ledger mirror (a folder that became a Git repository turns it on).
        attached = _dispatch_cli(
            lambda client: client.host_workspace_attach(
                selected, workspace_root=str(workspace), dry_run=dry_run, at=at
            ),
            lambda: None,
            allow_local=False,
            command_name="cruxible workspace attach",
        )
        assert isinstance(attached, contracts.HostWorkspaceAttachResult)
        if attached.status == "would_attach":
            if output_json:
                _emit_json(attached.model_dump(mode="json"))
            else:
                click.echo(
                    f"Would attach {workspace} to Cruxible host {selected}; nothing was "
                    "registered or written"
                )
                echo_preview_next(attached.status, attached.coordinate)
            return
        registered = attached.workspace_root
    if registered is None or Path(registered).resolve(strict=False) != workspace:
        raise WorkspaceAttachmentError(
            instance_id=selected,
            requested_workspace=str(workspace),
            registered_workspace=registered,
        )
    # Every road that reaches here attached the folder (a host registered to
    # another folder refused above).
    assert attached is not None
    if dry_run:
        click.echo(f"{workspace} is already attached to Cruxible host {selected}; nothing changes")
        return
    mirror = _ledger_mirror_off_line(workspace)
    transport_values = _workspace_config_transport()
    transport = str(next(iter(transport_values.values())))
    _echo_active_write_target(
        instance_id=selected,
        instance_source="explicit" if instance_id is not None else None,
    )
    config_path = write_workspace_config(
        workspace,
        instance_id=selected,
        replace=replace,
        **transport_values,
    )
    _dispatch_cli(
        lambda client: client.set_floor_delivery(selected, enabled=not no_floor_delivery),
        lambda: None,
        allow_local=False,
        command_name="cruxible workspace attach",
    )
    # An uninitialized host has no accepted world to stub yet; `cruxible init`
    # in this folder creates its ledger and writes the stub.
    if attached.initialized:
        _write_world_stub(workspace, selected)
    acting = _acting_principal_key_dir()
    identity_path = (
        None
        if acting is None
        else _record_folder_identity(
            workspace,
            instance_id=selected,
            principal_id=acting[0],
            key_dir=acting[1],
            token=get_runtime_bearer_token(),
            replace=replace,
        )
    )
    result = contracts.WorkspaceAttachResult(
        instance_id=selected,
        workspace_root=str(workspace),
        config_path=str(config_path),
        transport=transport,
    )
    result = _with_git_workspace_note(result)
    if output_json:
        _emit_json(
            {
                **_json_receipt(result),
                **({} if identity_path is None else {"folder_identity_path": str(identity_path)}),
            }
        )
        return
    click.echo(f"Attached workspace {workspace} to Cruxible host {selected}")
    click.echo(f"Config: {config_path}")
    if identity_path is not None and acting is not None:
        click.echo(f"Folder identity: {identity_path} (agents started here act as {acting[0]})")
    if mirror is not None:
        click.echo(mirror)
    if not attached.initialized:
        click.echo(f"Host {selected} is not initialized yet; next: cruxible init")


@workspace_group.command("detach")
@click.option("--instance-id", default=None, help="Existing registered daemon host ID.")
@change_control_options
@json_option
@handle_errors
def detach_workspace(
    instance_id: str | None, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Release a daemon host from the folder it is registered against.

    The registry allows one host per folder, so re-binding a folder to a second
    host needs the first one released. Nothing governed changes: the host keeps
    its ledger and every read it has ever served, and stops being the host of
    this directory. Attach the folder to the new host afterwards.
    """

    selected = instance_id or _require_instance_id()
    _echo_active_write_target(
        instance_id=selected,
        instance_source="explicit" if instance_id is not None else None,
    )
    result = _dispatch_cli(
        lambda client: client.host_workspace_detach(selected, dry_run=dry_run, at=at),
        lambda: None,
        allow_local=False,
        command_name="cruxible workspace detach",
    )
    assert isinstance(result, contracts.WorkspaceDetachResult)
    if output_json:
        _emit_json(_json_receipt(result))
        return
    if result.status == "not_registered":
        click.echo(f"Cruxible host {selected} registers no workspace")
        return
    if result.status == "would_detach":
        click.echo(f"Would detach {result.workspace_root} from Cruxible host {selected}")
        echo_preview_next(result.status, result.coordinate)
        return
    click.echo(f"Detached {result.workspace_root} from Cruxible host {selected}")


#: What init and attach print when the attached folder is no Git repository.
LEDGER_MIRROR_OFF = (
    "Ledger mirror: off, because this folder is not a Git repository (Cruxible works "
    "the same without one). To turn it on, run `git init` here, then "
    "`cruxible workspace attach`."
)


def _ledger_mirror_off_line(workspace: Path) -> str | None:
    """The plain line saying the ledger mirror is off, when the folder is not Git."""

    return None if containing_git_workspace_root(workspace) == workspace else LEDGER_MIRROR_OFF


def _write_world_stub(workspace: Path, instance_id: str) -> None:
    """Write the world stub into a workspace the first time it is bound to a host.

    With floor delivery on, the daemon keeps it current from then on; `cruxible
    stub` writes it explicitly.
    """

    rendered = _dispatch_cli(
        lambda client: render_world_stub_for(client, instance_id, workspace=workspace),
        lambda: None,
        allow_local=False,
        command_name="cruxible stub",
    )
    if rendered is not None:
        write_world_stub(workspace, rendered)


def _active_server_transport() -> str:
    obj = _root_ctx_obj()
    if obj.get("server_url"):
        return str(obj["server_url"])
    if obj.get("server_socket"):
        return f"unix socket {obj['server_socket']}"
    return "configured Cruxible server"


@host_group.command("show")
@click.argument("instance_id")
@json_option
@handle_errors
def show_host(instance_id: str, output_json: bool) -> None:
    """Show one existing daemon host and its write compatibility."""

    result = _dispatch_cli(
        lambda client: client.show_host(instance_id),
        lambda: None,
        allow_local=False,
        command_name="cruxible host show",
    )
    assert isinstance(result, contracts.HostInspection)
    transport = _active_server_transport()
    if output_json:
        payload = result.model_dump(mode="json")
        payload["transport"] = transport
        _emit_json(payload)
        return
    click.echo(f"Cruxible host: {result.instance_id}")
    click.echo(f"Transport: {transport}")
    click.echo(f"Managed root: {result.managed_root or '-'}")
    click.echo(f"Workspace root: {result.workspace_root or '-'}")
    click.echo(f"Floor delivery: {'on (default)' if result.floor_delivery else 'off (opted out)'}")
    click.echo(f"Compiler coordinate: {result.compiler_coordinate or '-'}")
    click.echo(f"Compiler revision: {result.compiler_revision or '-'}")
    click.echo(f"Compatibility: {result.compatibility}")
    if result.reason is not None:
        click.echo(f"Reason: {result.reason.code}: {result.reason.detail}")
        for repair in result.reason.repair_commands:
            click.echo(f"Repair: {repair}")


@host_group.command("create")
@click.option("--instance-id", default=None, help="Optional caller-selected opaque ID.")
@click.option(
    "--workspace",
    "workspace_path",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Explicit workspace folder (Git or not) to configure; remote paths stay client-local.",
)
@click.option("--replace", is_flag=True, help="Replace a differing workspace config.")
@change_control_options
@json_option
@handle_errors
def create_host(
    instance_id: str | None,
    workspace_path: str | None,
    replace: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Allocate an empty host and remember it as the active instance."""

    git_workspace = _creation_workspace(workspace_path, fallback_to_start=False).workspace_root
    workspace_root = (
        str(git_workspace)
        if git_workspace is not None and _root_ctx_obj().get("server_socket")
        else None
    )
    config_transport = _workspace_config_transport() if git_workspace is not None else {}
    if git_workspace is not None:
        validate_workspace_config_write(
            git_workspace,
            instance_id=instance_id,
            replace=replace,
            **config_transport,
        )
    result = _dispatch_cli(
        lambda client: client.create_host(
            instance_id=instance_id, workspace_root=workspace_root, dry_run=dry_run, at=at
        ),
        lambda: None,
        allow_local=False,
        command_name="cruxible host create",
    )
    assert isinstance(result, contracts.HostResult)
    result = _with_git_workspace_note(result)
    if dry_run:
        if output_json:
            _emit_json(_json_receipt(result))
        else:
            click.echo(f"Cruxible host: {result.instance_id} ({result.status}); nothing written")
            echo_preview_next(result.status, result.coordinate)
        return
    if git_workspace is not None:
        write_workspace_config(
            git_workspace,
            instance_id=result.instance_id,
            replace=replace,
            **config_transport,
        )
    _activate_server_instance(result.instance_id)
    if output_json:
        _emit_json(_json_receipt(result))
        return
    click.echo(f"Cruxible host: {result.instance_id} ({result.status})")


@playbill_group.command("init")
@click.option(
    "--key-dir",
    default=None,
    help=(
        "Client custody directory outside the workspace and the daemon state root "
        "(default: $XDG_CONFIG_HOME/cruxible/keys/INSTANCE/PRINCIPAL, else under ~/.config)."
    ),
)
@click.option(
    "--principal-id",
    default=None,
    help=(
        "Owner principal ID this init makes you (default: CRUXIBLE_PRINCIPAL_ID, the "
        "global --principal-id, else your OS username)."
    ),
)
@click.option(
    "--reviewer-key-dir",
    default=None,
    help=(
        "Custody directory for a second ordinary principal, `reviewer`, outside the "
        "workspace and the daemon state root (default with --require-independent-approval: "
        "$XDG_CONFIG_HOME/cruxible/keys/INSTANCE/reviewer, else under ~/.config)."
    ),
)
@click.option(
    "--require-independent-approval",
    is_flag=True,
    help=(
        "Require one non-creator ordinary approval for governed changes; also creates "
        "the `reviewer` principal that gives it."
    ),
)
@click.option("--recovery-key-dir", default=None, help="Optional offline recovery custody dir.")
@click.option("--recovery-principal-id", default="recovery", show_default=True)
@click.option("--profile", type=click.Choice(["local", "cloud"]), default="local")
@click.option(
    "--workspace",
    "workspace_path",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Explicit workspace folder (Git or not) to configure; remote paths stay client-local.",
)
@click.option("--replace", is_flag=True, help="Replace a differing workspace config.")
@click.option(
    "--object-format",
    "object_format",
    type=click.Choice(["sha1", "sha256"]),
    default=None,
    help=(
        "Ledger Git object format. Default: inherit an attached workspace, else sha1. "
        "A value that contradicts the attached workspace is refused."
    ),
)
@click.option(
    "--mirror-url",
    "mirror_url",
    default=None,
    help=(
        "Remote this ledger publishes to after every write. Never a URL carrying a "
        "credential; bind one later with 'cruxible ledger set-mirror'."
    ),
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Also print the public keys, settings files, workspace refs and folder identity.",
)
@json_option
@handle_errors
def init_playbill(
    key_dir: str | None,
    principal_id: str | None,
    reviewer_key_dir: str | None,
    require_independent_approval: bool,
    recovery_key_dir: str | None,
    recovery_principal_id: str,
    profile: str,
    workspace_path: str | None,
    replace: bool,
    object_format: str | None,
    mirror_url: str | None,
    verbose: bool,
    output_json: bool,
) -> None:
    """Make you the owner: create client custody and bootstrap the approval policy.

    With no instance selected, init first creates the host (as `cruxible host
    create` does) and selects it, so a new project is one bare `cruxible init`.
    The owner principal ID defaults to your OS username and the key directory to
    a per-user config path outside the workspace and the daemon state root. The
    CLI remembers the owner's settings and acts as the owner from then on
    (`cruxible context use --principal ID` switches). With
    --require-independent-approval (or --reviewer-key-dir) init also creates a
    second ordinary principal, `reviewer`, whose key defaults to the sibling
    per-user config path; `cruxible context use --principal reviewer` acts as it.

    With daemon auth off, the owner principal ID is the identity this process
    claims for the init request; no bootstrap secret is needed.

    The folder is the workspace, Git or not: over the local socket init attaches
    the nearest folder holding .cruxible/, else the enclosing Git worktree, else
    the current directory, and writes .cruxible/coverage.json there. Safe to run
    again: in a folder whose instance is already initialized it reports that
    instance and changes nothing. The host a remembered context names belongs to
    another folder, so it is never initialized here and never replaced.
    """

    # Independent approval needs a non-creator approver: init creates `reviewer`,
    # in its own custody directory (the default one sits beside the owner's).
    with_reviewer = require_independent_approval or reviewer_key_dir is not None
    if with_reviewer:
        # A named owner is checked before anything is resolved; a defaulted one below.
        _refuse_owner_named_reviewer(
            principal_id if principal_id is not None else _root_ctx_obj().get("principal_id")
        )
    if not ensure_cli_daemon():
        raise click.UsageError(no_daemon_message("cruxible init"))
    git_workspace = _creation_workspace(workspace_path, fallback_to_start=True).workspace_root
    transport = _transport_target(_root_ctx_obj())
    if transport is None:  # pragma: no cover - guarded by _get_client above
        raise click.UsageError("Server mode is required for cruxible init")
    if git_workspace is not None:
        transport = _target_init_folder(git_workspace) or transport
    # Only once the target folder is resolved (retargeting drops another folder's
    # record): init repairs a folder, so its record's refusal is reported, not raised.
    folder_refusal = _set_aside_folder_identity()
    selected_instance = _root_ctx_obj().get("instance_id")
    instance_source = str(_root_ctx_obj().get("target_instance_source") or "")
    if selected_instance is not None and instance_source == "workspace":
        initialized_here = _initialized_host(str(selected_instance))
        if initialized_here is not None:
            if folder_refusal is not None:
                click.echo(f"warning: {folder_refusal}", err=True)
            _echo_already_initialized(
                initialized_here,
                workspace=git_workspace,
                transport=transport,
                output_json=output_json,
            )
            return
    configured = _root_ctx_obj().get("principal_id")
    if principal_id is None:
        principal_id = configured if configured is not None else _os_principal_id()
    config_transport = _workspace_config_transport() if git_workspace is not None else {}
    if git_workspace is not None:
        validate_workspace_config_write(
            git_workspace,
            instance_id=selected_instance,
            replace=replace,
            **config_transport,
        )
    if configured is not None and configured != principal_id:
        raise click.UsageError(
            f"--principal-id {principal_id} disagrees with the configured principal "
            f"{configured} (CRUXIBLE_PRINCIPAL_ID or the global --principal-id); repair: "
            "pass one principal ID"
        )
    if with_reviewer:
        _refuse_owner_named_reviewer(principal_id)
    if get_runtime_bearer_token() is None:
        # Auth off: the init request claims the owner it creates. With a bearer
        # credential the credential decides who acts, so no claim is sent.
        _root_ctx_obj()["principal_id"] = principal_id
    workspace = git_workspace
    # Keys stay out of every workspace, attached or not: over TCP an enclosing
    # Git worktree (or .cruxible folder) is still forbidden custody.
    custody_roots = _init_custody_roots(
        workspace if workspace is not None else _existing_workspace_root()
    )
    # Every explicit custody directory is checked before a host is allocated or
    # a key generated; the default one as soon as the host names it.
    for role, flag, explicit in (
        ("owner", "--key-dir", key_dir),
        ("reviewer", "--reviewer-key-dir", reviewer_key_dir),
        ("recovery", "--recovery-key-dir", recovery_key_dir),
    ):
        if explicit is not None:
            _refuse_custody_inside(role, Path(explicit), custody_roots, flag=flag)
    if selected_instance is None and get_runtime_bearer_token() is not None:
        # Allocating a host takes the daemon's bootstrap secret, and init takes
        # the new host's own admin credential: no one bearer token does both.
        # CRUXIBLE_SERVER_BEARER_TOKEN being set in this process's environment
        # says nothing about whether the daemon actually requires auth -- it
        # may be inherited from an earlier session against a different host.
        # Report the configured value and its source, not a guess about the
        # daemon's posture.
        raise click.UsageError(
            "CRUXIBLE_SERVER_BEARER_TOKEN is set in this environment, and no instance is "
            "selected; init cannot tell whether that token is a host's bootstrap secret "
            "or an already-claimed admin credential, so nothing was created. If the "
            "daemon requires auth, repair: `CRUXIBLE_SERVER_BEARER_TOKEN=<bootstrap "
            "secret> cruxible host create`, then `cruxible credential claim-bootstrap "
            "--secret-file <secret file>`, then `CRUXIBLE_SERVER_BEARER_TOKEN=<the admin "
            "token it printed> cruxible init` (or select an allocated host with "
            "--instance-id). If this token is stale and the daemon does not require "
            "auth, unset CRUXIBLE_SERVER_BEARER_TOKEN and rerun."
        )
    if selected_instance is not None:
        _refuse_reinit(
            str(selected_instance),
            principal_id=principal_id,
            key_dir=(
                Path(key_dir).expanduser()
                if key_dir is not None
                else default_key_dir(str(selected_instance), principal_id)
            ),
            custody_roots=custody_roots,
        )
    selected = (
        str(selected_instance)
        if selected_instance
        else _create_host_for_init(
            transport,
            workspace_root=(
                str(git_workspace)
                if git_workspace is not None and _root_ctx_obj().get("server_socket")
                else None
            ),
            workspace=git_workspace,
            replace=replace,
            config_transport=config_transport,
        )
    )
    owner_key_dir = (
        Path(key_dir).expanduser()
        if key_dir is not None
        else _refuse_custody_inside(
            "owner",
            default_key_dir(selected, principal_id),
            custody_roots,
            flag="--key-dir",
        )
    )
    reviewer_dir = (
        None
        if not with_reviewer
        else Path(reviewer_key_dir).expanduser()
        if reviewer_key_dir is not None
        else _refuse_custody_inside(
            "reviewer",
            default_key_dir(selected, _INIT_REVIEWER_ID),
            custody_roots,
            flag="--reviewer-key-dir",
        )
    )
    specifications: list[tuple[Path, str, PrincipalKind]] = [
        (owner_key_dir, principal_id, "ordinary")
    ]
    if reviewer_dir is not None:
        specifications.append((reviewer_dir, _INIT_REVIEWER_ID, "ordinary"))
    if recovery_key_dir is not None:
        specifications.append(
            (Path(recovery_key_dir).expanduser(), recovery_principal_id, "recovery")
        )
    # Refuse a write-incompatible daemon before keys are generated, not after.
    from cruxible_core.cli.commands import _common

    require_authoring_compatibility(_common._get_client())
    materials, markers = prepare_init_custody(
        transport=transport,
        instance_id=selected,
        specifications=tuple(specifications),
        forbidden_roots=custody_roots,
    )
    owner = materials[0]
    reviewer = materials[1] if reviewer_dir is not None else None
    result = _server_call(
        lambda client, active: client.init(
            active,
            principals=[item.principal.model_dump(mode="json") for item in materials],
            operating_profile=cast(Any, profile),
            require_independent_approval=require_independent_approval,
            git_object_format=cast(Any, object_format),
            mirror_url=mirror_url,
            **(
                {"workspace_root": str(git_workspace)}
                if git_workspace is not None and _root_ctx_obj().get("server_socket")
                else {}
            ),
        ),
        command_name="cruxible init",
    )
    result = _with_git_workspace_note(result)
    if git_workspace is not None:
        write_workspace_config(
            git_workspace,
            instance_id=result.instance_id,
            replace=replace,
            **config_transport,
        )
    for marker in markers:
        marker.unlink()
    _remember_initialized_host(result.instance_id, workspace=git_workspace)
    owner_token = _mint_owner_credential(owner, principal_id=principal_id)
    # The reviewer gets what `principal add` gives a principal: a settings file
    # and, with auth on, its own graph_write credential, so `context use
    # --principal reviewer` works without --signer-id or a hand-set identity.
    reviewer_token = (
        None
        if reviewer is None
        else _mint_owner_credential(reviewer, principal_id=_INIT_REVIEWER_ID, mode="graph_write")
    )
    settings = write_principal_settings(
        owner_key_dir,
        ctx_obj=_root_ctx_obj(),
        instance_id=result.instance_id,
        principal_id=principal_id,
        private_key_path=owner.private_key_path,
        token=owner_token,
        written_by="cruxible init",
    )
    _remember_principal_settings(result.instance_id, principal_id, settings, activate=True)
    reviewer_settings = None
    if reviewer is not None and reviewer_dir is not None:
        reviewer_settings = write_principal_settings(
            reviewer_dir,
            ctx_obj=_root_ctx_obj(),
            instance_id=result.instance_id,
            principal_id=_INIT_REVIEWER_ID,
            private_key_path=reviewer.private_key_path,
            token=reviewer_token,
            written_by="cruxible init",
        )
        _remember_principal_settings(
            result.instance_id, _INIT_REVIEWER_ID, reviewer_settings, activate=False
        )
    if git_workspace is not None:
        _write_world_stub(git_workspace, result.instance_id)
    identity_path = _record_folder_identity(
        git_workspace,
        instance_id=result.instance_id,
        principal_id=principal_id,
        key_dir=owner_key_dir,
        token=owner_token,
        replace=replace,
    )
    if output_json:
        _emit_json(
            {
                **_json_receipt(result),
                "owner_settings_path": str(settings),
                **(
                    {}
                    if reviewer is None or reviewer_settings is None
                    else {
                        "reviewer_principal_id": _INIT_REVIEWER_ID,
                        "reviewer_private_key_path": str(reviewer.private_key_path),
                        "reviewer_settings_path": str(reviewer_settings),
                    }
                ),
                **({} if identity_path is None else {"folder_identity_path": str(identity_path)}),
            }
        )
        return
    # What matters: the instance, who owns it and where the key is, the policy and
    # the base kit. The rest is behind --verbose and `cruxible context show`.
    click.echo(
        f"Cruxible initialized: {result.instance_id} at {short_oid(result.coordinate.git_oid)}"
    )
    click.echo(f"Owner: {principal_id}, private key {owner.private_key_path}")
    click.echo(
        "The owner key signs approvals; it is separate from daemon auth ("
        + (
            "your bearer credential)."
            if owner_token is not None
            else "off: any process of this OS user may call the daemon)."
        )
    )
    click.echo(f"Approval policy: {result.approval_policy_mode}")
    if result.base_kit is not None:
        kit = result.base_kit
        if kit.status == "accepted":
            click.echo(f"Base kit: {kit.kit_id} {kit.version} installed")
        else:
            approver = _INIT_REVIEWER_ID if reviewer is not None else _SIGNER_PLACEHOLDER
            click.echo(
                f"Base kit: {kit.kit_id} {kit.version} awaits approval, proposal "
                f"{kit.proposal_id} (cruxible proposal approve {kit.proposal_id} "
                f"--signer-id {approver}, as a second principal)"
            )
    if result.workspace_advertisement.failure_code is not None:
        click.echo(f"Workspace ref failure: {result.workspace_advertisement.failure_code}")
    if reviewer is not None:
        click.echo(
            f"Reviewer: {_INIT_REVIEWER_ID}, private key {reviewer.private_key_path} "
            f"(cruxible context use --principal {_INIT_REVIEWER_ID}; another process: "
            f"CRUXIBLE_ENV_FILE={shlex.quote(str(reviewer_settings))})"
        )
    if not verbose:
        click.echo("More: cruxible context show")
        return
    click.echo(f"Workspace refs: {result.workspace_advertisement.status}")
    if result.workspace_advertisement.status == "not_git":
        click.echo(LEDGER_MIRROR_OFF)
    click.echo(f"Owner public key: {owner.principal.public_key}")
    click.echo(f"Owner settings: {settings}")
    click.echo(
        f"The CLI now acts as {principal_id} on this host (remembered in its context; "
        "`cruxible context use --principal ID` switches). Another process loads the "
        f"settings with: set -a; . {settings}; set +a"
        + (
            "."
            if owner_token is not None
            else ". With daemon auth off the principal ID is a claim of identity, not "
            "authentication: every process of this OS user is equally trusted."
        )
    )
    if identity_path is not None:
        click.echo(
            f"Folder identity: {identity_path} (an MCP server or CLI started in this folder "
            f"acts as {principal_id} unless its environment names another principal)"
        )
    if reviewer is not None:
        click.echo(f"Reviewer public key: {reviewer.principal.public_key}")
        click.echo(f"Reviewer settings: {reviewer_settings}")


#: The principal ID `cruxible init` gives the second (reviewer) principal.
_INIT_REVIEWER_ID = "reviewer"


def _refuse_owner_named_reviewer(owner: object) -> None:
    """Refuse an owner whose ID is the one init gives the reviewer it creates."""

    if owner == _INIT_REVIEWER_ID:
        raise click.UsageError(
            f"the owner principal ID {_INIT_REVIEWER_ID!r} is the ID init gives the second "
            "principal (--require-independent-approval or --reviewer-key-dir); nothing was "
            "written. Repair: pass another --principal-id"
        )


def _record_folder_identity(
    workspace: Path | None,
    *,
    instance_id: str,
    principal_id: str,
    key_dir: Path,
    token: str | None,
    replace: bool,
) -> Path | None:
    """Record the folder's default principal (`.cruxible/identity.json`), local socket only.

    Only over a local socket with daemon auth off: with a bearer credential the
    credential decides who acts, and a TCP daemon's folder is not this one. The
    record names a principal ID and its key directory, never a key or a token.
    """

    if workspace is None or token is not None or not _root_ctx_obj().get("server_socket"):
        return None
    path, status = write_folder_identity(
        workspace,
        instance_id=instance_id,
        principal_id=principal_id,
        key_dir=key_dir,
        replace=replace,
    )
    if status == "kept":
        click.echo(
            f"note: {path} keeps naming another principal of {instance_id} as this "
            "folder's default; rerun with --replace to make it "
            f"{principal_id}",
            err=True,
        )
        return None
    return path


def _acting_principal_key_dir() -> tuple[str, Path] | None:
    """The principal this process acts as and its key directory, when both are known.

    Known when the CLI acts as a principal and its key is ``<id>.ed25519`` in a
    directory: the remembered settings' (or the folder record's) key, or one
    named by CRUXIBLE_PRINCIPAL_KEY.
    """

    principal_id = _root_ctx_obj().get("principal_id")
    raw_key = os.environ.get(PRINCIPAL_KEY_ENV)
    if not isinstance(principal_id, str) or not raw_key:
        return None
    key = Path(raw_key).expanduser()
    if not key.is_absolute() or key.name != f"{principal_id}.ed25519" or not key.is_file():
        return None
    return principal_id, key.parent


def _os_principal_id() -> str:
    """The owner principal ID a bare init defaults to: the OS username, lowercased."""

    try:
        name = getpass.getuser().strip().lower()
    except (KeyError, OSError):  # pragma: no cover - no passwd entry and no LOGNAME/USER
        name = ""
    if not is_canonical_principal_id(name):
        raise click.UsageError(
            f"your OS username {name!r} is not a principal ID (lowercase letter first, then "
            "letters, digits, '_', '.' or '-'); repair: `cruxible init --principal-id ID`"
        )
    return name


def _create_host_for_init(
    transport: str,
    *,
    workspace_root: str | None,
    workspace: Path | None = None,
    replace: bool = False,
    config_transport: Mapping[str, str] | None = None,
) -> str:
    """Allocate the host a bare init initializes, and select it before anything else.

    Selecting it first means a retry after a failed init initializes this same
    host instead of allocating another: the folder's .cruxible/coverage.json
    names it at once (and with no folder, the remembered context does). The
    host is created attached to the folder init will attach (on a local
    socket), so a folder already attached to another host refuses here, before
    any host exists.
    """

    _echo_creation_write_target({})
    created = _dispatch_cli(
        lambda client: client.create_host(workspace_root=workspace_root, dry_run=False),
        lambda: None,
        allow_local=False,
        command_name="cruxible init",
    )
    assert isinstance(created, contracts.HostResult)
    if workspace is not None:
        write_workspace_config(
            workspace,
            instance_id=created.instance_id,
            replace=replace,
            **dict(config_transport or {}),
        )
    _remember_initialized_host(created.instance_id, workspace=workspace)
    click.echo(f"Created Cruxible host {created.instance_id} @ {transport}", err=True)
    return created.instance_id


def _target_init_folder(folder: Path) -> str | None:
    """Make init act on ``folder``'s own instance, or on none; the new transport, if moved.

    The root resolution chose for the current directory: another folder's
    binding (``init --workspace B`` run from A), the remembered context (another
    folder's), or nothing. None of that is this folder's, so init drops it and
    the remembered principal loaded with it, and takes ``folder``'s own binding
    when it has one (its transport too, unless the transport was named
    explicitly). An instance named explicitly or by the environment keeps its
    precedence.
    """

    obj = _root_ctx_obj()
    source = str(obj.get("target_instance_source") or "")
    if obj.get("instance_id") is not None and source in {"explicit", "environment"}:
        return None
    current = obj.get("playbill_workspace")
    if source == "workspace" and current is not None and Path(current) == folder:
        return None
    forget = obj.pop("forget_remembered_principal", None)
    if callable(forget):
        forget()
    obj["instance_id"] = None
    obj["target_instance_source"] = "local"
    obj["workspace_binding_path"] = None
    try:
        own = resolve_context(workspace=folder, environ={}, remembered={})
    except (ContextResolutionError, WorkspaceDirectoryConflict) as exc:
        raise click.UsageError(f"cruxible init: the workspace binding of {folder}: {exc}") from exc
    if not own.workspace_attached:
        return None
    obj["instance_id"] = own.instance_id
    obj["target_instance_source"] = "workspace"
    obj["workspace_binding_path"] = (
        None if own.workspace_binding_path is None else str(own.workspace_binding_path)
    )
    if obj.get("target_transport_source") in {"explicit", "environment"}:
        return None
    obj["server_url"], obj["server_socket"] = own.server_url, own.server_socket
    obj["target_transport_source"] = "workspace"
    obj.pop("_client", None)
    return _transport_target(obj)


def _remember_initialized_host(instance_id: str, *, workspace: Path | None) -> None:
    """Add the host init made to the remembered context, never replacing another.

    An empty context (or one naming this host) remembers it as the active
    instance. A context naming another host keeps it: in a bound folder the
    folder's .cruxible/coverage.json selects the new host; elsewhere the
    command says how to select it. Remembered principals are always kept.
    """

    remembered = load_cli_context().instance_id
    if remembered is None or remembered == instance_id:
        _activate_server_instance(instance_id)
        return
    _root_ctx_obj()["instance_id"] = instance_id
    if _root_ctx_obj().get("cruxible_kept_context_note") == instance_id:
        return
    _root_ctx_obj()["cruxible_kept_context_note"] = instance_id
    where = (
        "this folder's .cruxible/coverage.json selects it here"
        if workspace is not None
        else f"select it with --instance-id {instance_id} or `cruxible context use {instance_id}`"
    )
    click.echo(
        f"note: the CLI context still remembers host {remembered}; host {instance_id} was "
        f"added beside it ({where})",
        err=True,
    )


def _initialized_host(instance_id: str) -> contracts.HostInspection | None:
    """The host's inspection when the daemon reports it initialized, else None."""

    try:
        inspection = _dispatch_cli(
            lambda client: client.show_host(instance_id),
            lambda: None,
            allow_local=False,
            command_name="cruxible init",
        )
    except (CoreError, click.ClickException):
        return None
    if (
        isinstance(inspection, contracts.HostInspection)
        and inspection.compatibility != "uninitialized"
    ):
        return inspection
    return None


def _echo_already_initialized(
    inspection: contracts.HostInspection,
    *,
    workspace: Path | None,
    transport: str,
    output_json: bool,
) -> None:
    """Report the instance a folder already has; init changes nothing there."""

    binding = _root_ctx_obj().get("workspace_binding_path")
    if output_json:
        _emit_json(
            {
                "tag": "cruxible-init-already-initialized-v1",
                "status": "already_initialized",
                "instance_id": inspection.instance_id,
                "transport": transport,
                "workspace_root": None if workspace is None else str(workspace),
                "workspace_binding_path": binding,
                "compatibility": inspection.compatibility,
            }
        )
        return
    click.echo(
        f"Cruxible is already initialized here: host {inspection.instance_id} @ {transport} "
        f"(selected by {binding or '.cruxible/coverage.json'}); nothing changed."
    )
    click.echo("Next: `cruxible orient` maps it; `cruxible whoami` shows who you act as.")


def _refuse_reinit(
    instance_id: str, *, principal_id: str, key_dir: Path, custody_roots: tuple[Path, ...]
) -> None:
    """Refuse, before anything is written, an init of a host that init already ran for.

    A retry of an interrupted init carries its resume marker and goes on. Without
    one, an owner key already in custody, or a host the daemon reports
    initialized, means this init would only fail later: name the host, how it
    was selected, and the ways forward.
    """

    source = str(_root_ctx_obj().get("target_instance_source") or "context")
    repair = (
        "Repair: to start a new project, run `cruxible init` in that project's folder over "
        "the local socket (its .cruxible/coverage.json then selects the new host, and "
        "nothing the CLI remembers is replaced), or allocate one first with `cruxible host "
        f"create`; to act on {instance_id}, use its settings "
        f"(`cruxible context use --principal ID`)"
    )
    target = validate_client_principal_key_target(
        key_dir, principal_id=principal_id, kind="ordinary", forbidden_roots=custody_roots
    )
    if init_resume_marker(target).exists():
        return
    if target.private_key_path.exists():
        raise click.UsageError(
            f"Cruxible host {instance_id} (selected by {source}) already has {principal_id}'s "
            f"init key at {target.directory}, so init already ran for it; nothing was written. "
            f"{repair}, or pass --key-dir DIR for a key of its own"
        )
    try:
        inspection = _dispatch_cli(
            lambda client: client.show_host(instance_id),
            lambda: None,
            allow_local=False,
            command_name="cruxible init",
        )
    except (CoreError, click.ClickException):
        # An uninitialized host is shown only to an unscoped operator, so a
        # refusal here is no evidence that it is initialized.
        return
    if (
        isinstance(inspection, contracts.HostInspection)
        and inspection.compatibility != "uninitialized"
    ):
        raise click.UsageError(
            f"Cruxible host {instance_id} (selected by {source}) is already initialized; init "
            f"runs once per host and nothing was written. {repair}"
        )


def _init_custody_roots(workspace: Path | None) -> tuple[Path, ...]:
    """Where init never puts a key: the workspace and the local daemon state root."""

    roots = [] if workspace is None else [workspace]
    try:
        roots.append(get_server_state_root())
    except CoreError:  # pragma: no cover - no resolvable local state root
        pass
    return tuple(root.expanduser().resolve() for root in roots)


def _within_real_name(candidate: Path, root: Path) -> bool:
    """Containment on resolved names, case-insensitively (macOS volumes ignore case)."""

    inner = tuple(part.casefold() for part in candidate.parts)
    outer = tuple(part.casefold() for part in root.parts)
    return inner[: len(outer)] == outer


def _refuse_custody_inside(
    role: str, directory: Path, roots: tuple[Path, ...], *, flag: str
) -> Path:
    """``directory`` (symlinks resolved) if it is outside every root; else a refusal."""

    resolved = directory.expanduser().resolve()
    for root in roots:
        if _within_real_name(resolved, root):
            raise click.UsageError(
                f"the {role} key directory lies inside {root}; keys stay outside every "
                f"workspace and the daemon state root (custody path={str(resolved)!r}, "
                f"forbidden root={str(root)!r}). Repair: `cruxible init {flag} DIR` with DIR "
                "outside both"
            )
    return resolved


def _mint_owner_credential(
    owner: GeneratedKeyMaterial, *, principal_id: str, mode: str = "admin"
) -> str | None:
    """With daemon auth on, mint an init principal its own credential; return its token.

    The operator credential that ran init acts as no principal, so the owner
    (admin) and the reviewer (graph_write) each need one that acts as them. The
    principal's fresh key signs its consent. With auth off nothing is minted:
    the configured principal ID is the identity.
    """

    if get_runtime_bearer_token() is None:
        # An auth-on daemon answers nothing without a bearer credential.
        return None
    identity = _server_call(
        lambda client, instance_id: client.whoami(instance_id),
        command_name="cruxible init",
    )
    if not identity.authenticated or identity.actor_id == principal_id:
        return None
    minted = _server_call(
        lambda client, instance_id: client.create_runtime_credential(
            instance_id,
            principal_id=principal_id,
            permission_mode=cast(Any, mode),
            principal_proof=sign_runtime_credential_mint(
                instance_id=instance_id,
                principal_id=principal_id,
                permission_mode=cast(Any, mode),
                label=principal_id,
                private_key_path=owner.private_key_path,
                forbidden_roots=_custody_forbidden_roots(),
            ),
        ),
        command_name="cruxible init",
    )
    return minted.token


@playbill_group.group("body")
def body_group() -> None:
    """Store inert Document body bytes."""


@body_group.command("store")
@click.argument("path", type=PayloadFile())
@json_option
@handle_errors
def store_body(path: str, output_json: bool) -> None:
    """Store PATH's bytes (`-` reads stdin) as an inert body and print its digest.

    The bytes are stored, never interpreted; a Document proposal names the
    digest as its body.
    """

    content = read_payload_bytes(path)
    result = _server_call(
        lambda client, instance_id: client.store_body(instance_id, content),
        command_name="cruxible body store",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(result.digest)


@playbill_group.group("instance")
def instance_group() -> None:
    """Read and end the lifecycle of one governed instance."""


@instance_group.command("decommission")
@click.option("--reason", required=True, help="Why this instance stops accepting writes.")
@change_control_options
@json_option
@handle_errors
def decommission_instance(
    reason: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """End this instance's governed writes without deleting anything.

    Reads keep serving at the accepted coordinate and every byte stays on disk.
    Archiving or erasing the directory afterwards is your own step; no verb here
    performs it, and the state cannot be reversed. So it previews first; the
    confirmation is that preview's coordinate: ``--commit --at OID``.
    """

    result = _server_call(
        lambda client, instance_id: client.decommission_instance(
            instance_id, reason=reason, dry_run=dry_run, at=at
        ),
        command_name="cruxible instance decommission",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if result.status == "would_decommission":
        click.echo(f"Would decommission instance {result.instance_id}; nothing changed.")
        click.echo(f"Reason: {printable(result.reason)}")
        echo_preview_next(result.status, result.coordinate)
        return
    click.echo(f"Instance {result.instance_id} decommissioned at {result.decommissioned_at}.")
    # Operator prose reaches the terminal here; escape it so a control
    # character in the reason cannot forge a line of daemon output.
    click.echo(f"Reason: {printable(result.reason)}")
    click.echo(f"By: {printable(result.decommissioned_by)}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")
    click.echo("Reads keep serving; nothing was deleted. Archive the directory yourself.")


@instance_group.command("rebuild-next")
@json_option
@handle_errors
def rebuild_next(output_json: bool) -> None:
    """Drop the next worker's facts and rows and derive them again from nothing.

    The repair when the worker cannot follow its logs forward (a stalled
    `next` consumer names it). It changes no accepted state and no evidence;
    reads fold every Claim until it finishes.
    """

    result = _server_call(
        lambda client, instance_id: client.rebuild_next(instance_id),
        command_name="cruxible instance rebuild-next",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(
        f"Rebuilt next for {result.instance_id} at {result.coordinate.git_oid} "
        f"(format {result.format}): {result.claims} Claims, {result.keys} row keys, "
        f"{result.rows} rows."
    )


@playbill_group.group("ledger")
def ledger_group() -> None:
    """Publish this instance's ledger to a mirror reviewers can clone."""


@ledger_group.command("set-mirror")
@click.argument("url", required=False)
@click.option(
    "--clear",
    is_flag=True,
    help="Unbind the mirror so nothing more is published (what was sent stays sent).",
)
@change_control_options
@json_option
@handle_errors
def set_ledger_mirror(
    url: str | None, clear: bool, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Bind the remote and wait boundedly for its initial publication attempt.

    The URL must carry no credential: the daemon reads its token from its own
    environment, and `orient` shows this string to anyone who may read the
    instance at all. Every accepted byte is sent there at once and cannot be
    called back, so it previews first; commit that preview with
    ``--commit --at OID``. ``--clear`` (no URL) unbinds the mirror and commits
    by default; preview it with ``--dry-run``.
    """

    if clear == (url is not None):
        raise click.UsageError("pass either URL or --clear")
    if clear:
        cleared = _server_call(
            lambda client, instance_id: client.clear_ledger_mirror(
                instance_id, dry_run=dry_run, at=at
            ),
            command_name="cruxible ledger set-mirror",
        )
        if output_json:
            _emit_json(cleared.model_dump(mode="json"))
            return
        if cleared.status == "already_clear":
            click.echo("No ledger mirror is bound.")
            return
        verb = "Would stop" if cleared.status == "would_clear" else "Stopped"
        click.echo(f"{verb} publishing to {cleared.previous_mirror_url}.")
        echo_preview_next(cleared.status, cleared.coordinate)
        return
    assert url is not None
    result = _server_call(
        lambda client, instance_id: client.set_ledger_mirror(
            instance_id, url=url, dry_run=dry_run, at=at
        ),
        command_name="cruxible ledger set-mirror",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Ledger mirror: {result.mirror_url}")
    click.echo(f"Publication: {result.status}")
    _echo_publication_detail(result.detail)
    echo_preview_next(result.status, result.coordinate)


def _echo_publication_detail(detail: str | None) -> None:
    """Print a publication detail (git's own error text) with its real line breaks.

    Each line is indented under ``Detail:`` and escaped, so a remote's message
    keeps its shape but cannot pass for a line of this command's own output.
    """

    if detail is None:
        return
    lines = [line for line in detail.splitlines() if line.strip()]
    if len(lines) <= 1:
        click.echo(f"Detail: {printable(lines[0] if lines else detail)}")
        return
    click.echo("Detail:")
    for line in lines:
        click.echo(f"  {printable(line)}")


@ledger_group.command("publish")
@click.option("--timeout", type=click.FloatRange(0, 60), default=60.0, show_default=True)
@change_control_options
@json_option
@handle_errors
def ledger_publish(timeout: float, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Wait for publication to the configured mirror; timeout 0 only requests it.

    Exits 1 when publication ends behind (the attempt failed): the mirror does
    not hold the accepted ledger.
    """

    result = _server_call(
        lambda client, instance_id: client.publish_ledger(
            instance_id, timeout=timeout, dry_run=dry_run, at=at
        ),
        command_name="cruxible ledger publish",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(f"Publication: {result.status}")
        if result.wait_sequence is not None:
            acknowledged = result.published_sequence >= result.wait_sequence
            click.echo(
                f"Request {result.wait_sequence}: "
                f"{'acknowledged' if acknowledged else 'awaiting acknowledgment'}"
            )
        _echo_publication_detail(result.detail)
        echo_preview_next(result.status, result.coordinate)
    if result.status == "behind":
        raise SystemExit(1)


@playbill_group.group("provider")
def provider_group() -> None:
    """Manage governed Provider artifacts."""


@provider_group.command("list")
@json_option
@handle_errors
def list_provider_packages(output_json: bool) -> None:
    """List the Providers installed here, then the configured repository's packages."""
    result = _server_call(
        lambda client, instance_id: client.list_provider_packages(instance_id),
        command_name="cruxible provider list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo("Installed:" if result.installed else "Installed: none")
    for installed in result.installed:
        runs = "" if installed.package is None else f" ({installed.package} {installed.version})"
        marker = " [newest]" if installed.newest else ""
        operations = ", ".join(installed.operations) or "-"
        click.echo(f"  {installed.build}{runs}{marker}: {operations}")
    if result.packages:
        click.echo("Provider repository:")
    for package in result.packages:
        click.echo(f"  {package.name} {package.version}: {', '.join(package.operations)}")
    if result.detail:
        click.echo(result.detail)


def _provider_move_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``--move`` and ``--only LINE``: move Lines to the installed build, in one proposal."""

    command = click.option(
        "--only",
        "only",
        multiple=True,
        help="With --move, move only this Line (Line:NAME, repeatable).",
    )(fn)
    return click.option(
        "--move",
        is_flag=True,
        help=(
            "Also move the live Lines binding other builds of this provider to the new "
            "build, in the same proposal as its registration."
        ),
    )(command)


@provider_group.command("install")
@click.argument("package_or_wheel")
@click.option(
    "--lock",
    "lock_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="The lock file a local wheel was built with (required with a wheel).",
)
@click.option(
    "--dependency",
    "dependencies",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="A locked dependency wheel to transfer with a local wheel (repeatable).",
)
@click.option("--extra", "extras", multiple=True, help="A package extra to install (repeatable).")
@click.option(
    "--control-domain",
    default="operator",
    show_default=True,
    help="The control domain the Provider definition records for this package.",
)
@click.option("--reverify", is_flag=True, help="Recheck a retained installation explicitly.")
@_provider_move_options
@change_control_options
@json_option
@handle_errors
def install_provider(
    package_or_wheel: str,
    lock_path: Path | None,
    dependencies: tuple[Path, ...],
    extras: tuple[str, ...],
    control_domain: str,
    reverify: bool,
    move: bool,
    only: tuple[str, ...],
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Install a package by name (NAME or NAME==VERSION) or transfer a local wheel.

    By name, the package comes from the configured provider repository, or else
    from the provider index (PyPI unless the operator configured indexes). The
    install proposes the package's registration, which lands at once when the
    approval policy requires no approval; otherwise it stops at proposed
    (awaiting_approval) for the ordinary review and activation. ``--dry-run`` is
    a validation-only preview by name: it resolves the package and, if it is
    already prepared here, evaluates the registration it would propose; it
    fetches, builds, registers and proposes nothing. ``--move`` also moves the
    live Lines binding other builds of the provider to this build, in the same
    proposal (``provider move --to`` it, as one review).
    """
    if only and not move:
        raise click.UsageError("--only names Lines to move; it requires --move")
    if package_or_wheel.endswith(".whl"):
        if lock_path is None:
            raise click.UsageError("a local wheel requires --lock")
        if dry_run:
            raise click.UsageError(
                "--dry-run previews an install by name; a local wheel is transferred to the "
                "daemon before anything can be evaluated"
            )
        result = _server_call(
            lambda client, instance_id: install_provider_wheel(
                client,
                instance_id,
                wheel=Path(package_or_wheel),
                lock=lock_path,
                dependency_wheels=dependencies,
                extras=extras,
                control_domain=control_domain,
                reverify=reverify,
                move=move,
                move_only=only,
                at=at,
            ),
            command_name="cruxible provider install",
        )
    else:
        if lock_path is not None or dependencies:
            raise click.UsageError("--lock and --dependency apply to a local wheel")
        package, pinned, version = package_or_wheel.partition("==")
        request = ProviderInstallRequest(
            package=package,
            version=version if pinned else None,
            extras=tuple(sorted(set(extras))),
            control_domain=control_domain,
            reverify=reverify,
            move=move,
            move_only=tuple(sorted(set(only))),
            dry_run=dry_run,
            at=at,
        )
        result = _server_call(
            lambda client, instance_id: client.install_provider(instance_id, request),
            command_name="cruxible provider install",
        )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_provider_install(result)


def _echo_provider_install(result: ProviderInstallResult) -> None:
    click.echo(f"{result.build or result.provider_id}: {result.status}")
    _echo_provider_repins(result.moved, result.stops, result.automation_carries)
    if result.detail:
        click.echo(result.detail)
    if result.preview_scope == "validation_only":
        click.echo(
            "Validation-only preview; it did not run: "
            + ", ".join(step.replace("_", " ") for step in result.not_run)
        )
        echo_preview_next(result.status, result.coordinate)
    if result.proposal_id:
        click.echo(f"Proposal: {result.proposal_id}")
    for operation in result.operations:
        if operation.missing_requirements:
            click.echo(f"{operation.operation}: {', '.join(operation.missing_requirements)}")


@provider_group.command("publish")
@click.argument(
    "file", type=click.Path(exists=True, dir_okay=False, path_type=Path), metavar="FILE"
)
@click.option("--name", default=None, help="Distribution and provider name (default: FILE's stem).")
@click.option(
    "--version",
    "version",
    default=None,
    help="Normalized version (default: 0.0.0.devN from the file and the shapes it carries).",
)
@_provider_move_options
@click.option(
    "--control-domain",
    default="operator",
    show_default=True,
    help="The control domain the Provider definition records for this package.",
)
@click.option(
    "--runtime-wheel",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="A cruxible-provider-runtime wheel to build with and transfer (else the published one).",
)
@click.option(
    "--dependency",
    "dependencies",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="A dependency wheel to lock against and transfer (repeatable).",
)
@click.option(
    "--declares",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="A declares block of Contracts and CaptureKinds the instance does not hold.",
)
@click.option("--offline", is_flag=True, help="Lock and build from the uv cache and local wheels.")
@change_control_options
@json_option
@handle_errors
def publish_provider(  # noqa: PLR0913
    file: Path,
    name: str | None,
    version: str | None,
    move: bool,
    only: tuple[str, ...],
    control_domain: str,
    runtime_wheel: Path | None,
    dependencies: tuple[Path, ...],
    declares: Path | None,
    offline: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Build FILE's @derive operations into a provider and install it.

    The Contracts and CaptureKinds the operations reach come from this instance
    (and --declares); the wheel is locked and built here, then installed as
    `provider install WHEEL --lock` would. Without --version the version derives
    from the file, so an edit is a new build and an unchanged file the same one.
    With --move the build's registration and the Lines moved to it are one
    proposal: an observation-only settle grant across a pure build keeps settle.
    """

    from cruxible_core.cli.provider_publish import publish_provider_file

    if only and not move:
        raise click.UsageError("--only names Lines to move; it requires --move")
    result = _server_call(
        lambda client, instance_id: publish_provider_file(
            client,
            instance_id,
            file,
            name=name,
            version=version,
            move=move,
            only=tuple(sorted(set(only))),
            control_domain=control_domain,
            runtime_wheel=runtime_wheel,
            dependency_wheels=dependencies,
            declares=declares,
            offline=offline,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible provider publish",
    )
    if output_json:
        _emit_json(result.as_json())
        return
    package = result.package
    click.echo(
        f"Built {package.distribution} {package.version} ({Path(package.file).name}): "
        + ", ".join(package.operations)
    )
    _echo_provider_install(result.install)


@provider_group.command("retire")
@click.argument("build")
@change_control_options
@json_option
@handle_errors
def retire_provider(build: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Retire NAME/BUILD when no live definition pins it; keep its runtime environment."""
    request = ProviderRetireRequest(build=build, dry_run=dry_run, at=at)
    result = _server_call(
        lambda client, instance_id: client.retire_provider(instance_id, request),
        command_name="cruxible provider retire",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(f"{result.build}: {result.status}")
        for identity in result.pinned_by:
            click.echo(f"  pinned by {identity}")
        for warning in result.warnings:
            click.echo(f"warning: {printable(warning)}", err=True)
        if result.detail:
            click.echo(result.detail)
        if result.proposal_id:
            click.echo(f"Proposal: {result.proposal_id}")
        echo_preview_next(result.status, result.coordinate)


@provider_group.command("move")
@click.argument("provider")
@click.option(
    "--to", required=True, help="newest (most recently registered) or an exact build key."
)
@click.option("--only", multiple=True, help="Move only this Line (Line:NAME, repeatable).")
@change_control_options
@json_option
@handle_errors
def move_provider(
    provider: str,
    to: str,
    only: tuple[str, ...],
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Move Lines to NAME's build; carry Lines disabled and Mandates propose-only.

    Under compiler revision 74 an observation-only settle grant the move leaves
    pure and non-widening keeps settle, and its enabled Line stays enabled.
    """
    request = ProviderMoveRequest(
        provider=provider, to=to, only=tuple(sorted(set(only))), dry_run=dry_run, at=at
    )
    result = _server_call(
        lambda client, instance_id: client.move_provider(instance_id, request),
        command_name="cruxible provider move",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(f"{result.provider} -> {result.to.resolved}: {result.status}")
        _echo_provider_repins(result.moved, result.stops, result.automation_carries)
        if result.detail:
            click.echo(result.detail)
        if result.proposal_id:
            click.echo(f"Proposal: {result.proposal_id}")
        echo_preview_next(result.status, result.coordinate)


def _echo_provider_repins(repins: Any, stops: Any, carries: Any = ()) -> None:
    for item in repins:
        click.echo(
            f"  {item.action}: {item.identity}: {item.from_build} -> {item.to_build}"
            + (f" ({item.reason})" if item.reason else "")
        )
    for item in carries:
        phase = "planned " if item.phase == "planned" else ""
        if item.kind == "line":
            click.echo(
                f"  {phase}carry: {item.identity}: disabled by upgrade to "
                f"{item.new_procedure_digest or 'pending Procedure version'}; line enable to resume"
            )
        elif item.new_grant == "settle":
            click.echo(
                f"  {phase}carry: {item.identity}: retains observation settle on "
                f"{', '.join(item.retained_scope)}; retains Line enablement"
            )
        else:
            click.echo(
                f"  {phase}carry: {item.identity}: {item.old_grant} -> propose"
                + (f" ({item.demotion.replace('_', ' ')})" if item.demotion else "")
                + ("; settle re-grant required" if item.loses_settle else "")
            )
            if item.subject_creation_namespace_limitation:
                click.echo(
                    "    Subject creation needs explicit propose namespace authoring; "
                    "namespace preserved."
                )
    for item in stops:
        click.echo(f"  stop {item.kind}: {item.identity} (over {item.stopped_by})")


@playbill_group.group("kit")
def kit_group() -> None:
    """Export and import definition kits."""


_KIT_CONSEQUENCE_TEXT = {
    "overwrites_your_edit": "overwrites your edit",
    "re_adds_retired": "re-adds a retired definition",
    "consumer_retired": "keeps your retirement",
    "takes_over_outside_definition": "takes over a definition you defined outside the kit",
    "replaces_carried_definition": "replaces a definition the kit depends on",
    "release_dropped": "the release dropped it",
}
#: A divergence this instance keeps, said from the kept side: on a `keep` plan
#: line, then as `kit status` lists it afterwards.
_KIT_KEEP_TEXT = {
    "overwrites_your_edit": ("keeps your edit", "your edit"),
    "re_adds_retired": ("keeps it retired", "retired here"),
    "consumer_retired": ("kept retired", "you retired it"),
    "takes_over_outside_definition": (
        "keeps your definition from outside the kit",
        "your definition from outside the kit",
    ),
    "replaces_carried_definition": (
        "keeps your version of a definition the kit depends on",
        "your version of a definition the kit depends on",
    ),
    "release_dropped": ("keeps it; the release dropped it", "the release dropped it"),
}


def _echo_automation_template(
    path: str | None, *, on_disk: Path | None = None, reference: str | None = None
) -> None:
    """One line naming the automation template: where it is on disk, or how to get it."""

    if path is None:
        return
    name = Path(path).name
    if on_disk is not None:
        # The kit's own copy is the release's, kept as shipped: the adopter
        # tailors a copy in the workspace and submits that.
        click.echo(
            f"automation template: {on_disk} (never installed: copy it into your "
            f"workspace, tailor the copy, then cruxible authoring submit {shlex.quote(name)})"
        )
    else:
        click.echo(
            f"automation template: {path} (never installed: cruxible kit pull {reference} "
            f"--out DIR writes it, and --json output carries its content; copy it into your "
            f"workspace, tailor the copy, then cruxible authoring submit {shlex.quote(name)})"
        )


def _pull_reference(source: str, origin: str) -> str:
    """What ``kit pull`` takes for this kit: the pinned reference a registry answered,
    or the local path as given (a layout's origin names its digest, not a path)."""

    return source if Path(source).expanduser().exists() else origin


def _kit_directory_file(source: str, path: str | None) -> Path | None:
    """Where a kit file is on disk when ``source`` is a kit directory (not a layout)."""

    directory = Path(source).expanduser()
    if path is None or not directory.is_dir() or is_layout(directory):
        return None
    return (directory / path).absolute()


def _echo_kit_requires(requires: tuple[KitRequirement, ...]) -> None:
    """The kits a release requires: kit build leaves out what they own."""

    if requires:
        click.echo(
            "  requires: " + ", ".join(f"{item.kit_id} >= {item.min_version}" for item in requires)
        )


def _echo_kit_requirements(requires: Sequence[KitRequirementStatus]) -> None:
    """Each kit a release requires and whether this instance meets it (add and status)."""

    for required in requires:
        held = (
            "not installed"
            if required.installed_version is None
            else f"installed {required.installed_version}"
        )
        click.echo(
            f"  requires {required.kit_id} >= {required.min_version}: "
            f"{'met' if required.met else 'not met'} ({held})"
        )


def _kit_plan_heading(kind: str) -> str:
    """A kit plan group's heading: every identity kind capitalized (``document`` too)."""

    return kind[:1].upper() + kind[1:]


def _exit_if_kit_blocked(result: KitChangeResult) -> None:
    """A commit that was blocked changed nothing: exit 1 (a blocked preview is an answer)."""

    if result.status == "blocked":
        raise SystemExit(1)


def _echo_kit_refusals(result: KitChangeResult) -> None:
    if result.detail:
        click.echo(printable(result.detail))
    for refusal in result.refusals:
        click.echo(refusal.code)
        click.echo(f"Next: {printable(refusal.repair)}")


def _kit_plan_counts(result: KitChangeResult) -> list[str]:
    """One line per plan action: how many paths of each kind (``retire: 120 (...)``)."""

    counts: dict[str, dict[str, int]] = {}
    for item in result.plan:
        if item.action == "unchanged":
            continue
        kind = _kit_plan_heading((item.identity or item.path).partition(":")[0])
        by_kind = counts.setdefault(item.action, {})
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return [
        f"  {action}: {sum(by_kind.values()):,} ("
        + ", ".join(f"{kind} {count:,}" for kind, count in sorted(by_kind.items()))
        + ")"
        for action, by_kind in sorted(counts.items())
    ]


def _echo_kit_change(
    result: KitChangeResult,
    *,
    template_on_disk: Path | None = None,
    source: str | None = None,
    blocked_plan: Literal["list", "counts"] | None = None,
) -> None:
    """One kit change. ``blocked_plan`` (kit remove) puts a blocked change's refusal
    and repair first, then its plan as counts per kind, or listed after them."""

    refusal_first = blocked_plan is not None and result.status in {"blocked", "would_block"}
    version = "" if result.version is None else f" {result.version}"
    transition = ""
    if result.transition is not None:
        transition = f" ({result.transition}"
        if result.installed_version is not None:
            transition += f" from {result.installed_version}"
        transition += ")"
    click.echo(f"{result.kit_id}{version}: {result.status}{transition}")
    for step in result.providers:
        line = f"  provider {step.provider_id} ({step.package} {step.version}): "
        line += step.action.replace("_", " ")
        if step.proposal_id is not None:
            line += f", proposal {step.proposal_id}"
        if step.detail is not None:
            line += f" ({printable(step.detail)})"
        click.echo(line)
    _echo_kit_requirements(result.requires)
    _echo_provider_repins(result.repins, result.stops, result.automation_carries)
    if result.provenance is not None:
        provenance = result.provenance
        click.echo(
            f"Built by {provenance.principal_id or 'an unattributed principal'} on "
            f"{provenance.instance_id} at {provenance.coordinate.git_oid[:12]} (claimed)"
        )
    if refusal_first:
        _echo_kit_refusals(result)
        counts = _kit_plan_counts(result)
        if counts:
            click.echo("Plan:")
            for line in counts:
                click.echo(line)
            if blocked_plan == "counts":
                click.echo("(--detail lists each path)")
    groups: dict[str, list[str]] = {}
    shows_unchanged = result.unchanged is None
    for item in result.plan:
        if item.action == "unchanged" and not shows_unchanged:
            continue
        kind = _kit_plan_heading((item.identity or item.path).partition(":")[0])
        named = _public_name(item.identity) if item.identity else item.path
        if item.consequence == "consumer_retired":
            # S7: your retirement stands; nothing to decide.
            groups.setdefault(kind, []).append(f"  kept retired: {named}")
            continue
        line = f"  {item.action}: {named}"
        notes = []
        if item.consequence is not None:
            notes.append(
                _KIT_KEEP_TEXT[item.consequence][0]
                if item.action == "keep"
                else _KIT_CONSEQUENCE_TEXT[item.consequence]
            )
        if item.dependent_count:
            notes.append(f"{item.dependent_count} dependent(s)")
        if item.detail is not None:
            notes.append(printable(item.detail))
        if notes:
            line += f" ({'; '.join(notes)})"
        groups.setdefault(kind, []).append(line)
    if not refusal_first or blocked_plan == "list":
        for kind in sorted(groups):
            click.echo(f"{kind}:")
            for line in groups[kind]:
                click.echo(line)
    if result.unchanged:
        click.echo(f"unchanged: {result.unchanged:,} (--detail lists each)")
    if not refusal_first:
        _echo_kit_refusals(result)
    _echo_automation_template(
        result.automation_template, on_disk=template_on_disk, reference=source
    )
    for item in result.plan:
        if (
            result.status in {"would_propose", "would_block"}
            and item.action == "replace"
            and item.consequence == "takes_over_outside_definition"
        ):
            click.echo(f"Next: rerun cruxible kit add with --keep {item.identity} to keep yours.")
    if result.proposal_id and result.status == "blocked" and result.proposal_open:
        # Admitted, then accepted state moved before it could land: it stays open.
        click.echo(
            f"Proposal: {result.proposal_id} (open; withdraw it: cruxible proposal withdraw "
            f"{result.proposal_id} --reason '<TEXT>')"
        )
    elif result.proposal_id and result.status != "blocked":
        # A refused change leaves nothing to act on (--json keeps the refused record's id).
        click.echo(f"Proposal: {result.proposal_id}")
        if result.status == "proposed":
            if result.approval_required:
                click.echo(
                    f"Next: cruxible proposal approve {result.proposal_id} "
                    f"--signer-id {_SIGNER_PLACEHOLDER} (the approval that completes it "
                    "activates it)"
                )
            elif result.detail is None:
                # Otherwise the detail already names who activates it.
                click.echo(f"Next: cruxible proposal activate {result.proposal_id}")
    echo_preview_next(result.status, result.coordinate)


@kit_group.command("build")
@click.option("--id", "kit_id", required=True, help="Kit id, lowercase with hyphens.")
@click.option("--version", "version", required=True, help="Release version, MAJOR.MINOR.PATCH.")
@click.option(
    "--owns",
    "owns",
    multiple=True,
    required=True,
    help="Identity prefix the kit defines, ending in '.' (repeatable).",
)
@click.option(
    "--out", "out", required=True, type=click.Path(path_type=Path), help="New kit directory."
)
@click.option(
    "--provider",
    "providers",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help=(
        "A provider package directory to bundle (repeatable): its pyproject.toml, its "
        "uv.lock, and in dist/ its built wheel plus the wheel of each dependency the lock "
        "names by path. The kit carries the wheels and lock, never source."
    ),
)
@click.option(
    "--default-provider",
    "default_providers",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help=(
        "A published provider package directory the kit names as the default provider "
        "for its Procedures' slots (repeatable): pyproject.toml, uv.lock and the "
        "published wheel in dist/ (path dependencies' wheels are not needed). The kit "
        "carries none of its files. kit add installs it by name at exactly this version "
        "from the provider index, refusing any other build, unless live operations of "
        "other providers already installed serve every slot's capability and fit it; "
        "a daemon with provider_repository configured refuses that install (install "
        "the repository's build first with provider install)."
    ),
)
@click.option(
    "--automation-template",
    "automation_template",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "The kit's automation template: one change set of Lines, Triggers, Mandates and "
        "Runbooks, at the kit's root under its own file name. The build refuses unless it "
        "authors cleanly against this instance's accepted definitions (a preview: nothing "
        "is stored); kit add never installs or submits it."
    ),
)
@click.option(
    "--file",
    "kit_files",
    multiple=True,
    metavar="PATH",
    help=(
        "A plain file the kit carries for its reader, such as README.md or a reference "
        "page (repeatable). It keeps its path relative to the workspace root and is "
        "listed in the manifest's files with its sha256; it is never a definition."
    ),
)
@json_option
@handle_errors
def build_kit(
    kit_id: str,
    version: str,
    owns: tuple[str, ...],
    out: Path,
    providers: tuple[Path, ...],
    default_providers: tuple[Path, ...],
    automation_template: Path | None,
    kit_files: tuple[str, ...],
    output_json: bool,
) -> None:
    """Export this instance's owned definitions as one self-contained kit release.

    Provider packages are named with --provider or --default-provider (the same
    build this instance installed); kit add installs the providers before the
    definitions, a default one only when no installed build already serves
    every slot the kit's Procedures declare. --automation-template names and
    checks the automation template adopters tailor and submit.
    """
    if out.exists():
        raise click.UsageError(f"{out} already exists")
    template = (
        None
        if automation_template is None
        else KitAutomationTemplate(
            path=automation_template.name,
            content=automation_template.read_text(encoding="utf-8"),
        )
    )

    def build(client: Any, instance_id: str) -> Any:
        request = KitBuildRequest(
            kit_id=kit_id,
            version=version,
            owns=tuple(sorted(set(owns))),
            providers=tuple(
                stage_kit_provider_directory(client, instance_id, directory)
                for directory in providers
            )
            + tuple(
                stage_kit_provider_directory(client, instance_id, directory, delivery="index")
                for directory in default_providers
            ),
            automation_template=template,
        )
        return client.build_kit(instance_id, request)

    carried: tuple[tuple[str, bytes], ...] = ()
    if kit_files:
        workspace = _existing_workspace_root() or Path.cwd()
        # Read before the build, so a missing or outside path refuses first.
        carried = read_kit_build_files(kit_files, workspace=workspace)
    bundle = add_kit_files(_server_call(build, command_name="cruxible kit build").bundle, carried)
    write_kit_directory(bundle, out)
    if output_json:
        _emit_json(bundle.manifest.model_dump(mode="json"))
    else:
        click.echo(
            f"{kit_id} {version}: {len(bundle.artifacts)} artifacts, "
            f"{len(bundle.manifest.providers)} provider package(s), "
            f"content {bundle.manifest.content_digest}"
        )
        _echo_kit_requires(bundle.manifest.requires)
        template_path = bundle.manifest.automation_template
        _echo_automation_template(
            template_path, on_disk=_kit_directory_file(str(out), template_path)
        )


@kit_group.command("add")
@click.argument("kit")
@click.option(
    "--source", "source", default=None, help="Recorded origin; defaults to where KIT came from."
)
@click.option(
    "--keep",
    "keep",
    multiple=True,
    metavar="IDENTITY",
    help=(
        "Keep this instance's version of a definition (ClaimType:acme.account.seats) "
        "instead of the release's, or keep one the release dropped (repeatable)."
    ),
)
@click.option(
    "--keep-local-edits",
    is_flag=True,
    help="Keep every definition edited here since the kit installed it.",
)
@click.option(
    "--retire-dependents",
    "retire_dependents",
    multiple=True,
    metavar="IDENTITY",
    help="Retire a definition the release dropped together with its live dependents.",
)
@click.option(
    "--keep-build",
    multiple=True,
    metavar="Line:NAME",
    help="Keep this Line on its current provider builds (repeatable).",
)
@click.option(
    "--allow-downgrade", is_flag=True, help="Install a release older than the installed one."
)
@click.option(
    "--detail",
    "show_unchanged",
    is_flag=True,
    help="List every path, the unchanged ones too (by default they are counted).",
)
@change_control_options
@json_option
@handle_errors
def add_kit(
    kit: str,
    source: str | None,
    keep: tuple[str, ...],
    keep_local_edits: bool,
    retire_dependents: tuple[str, ...],
    keep_build: tuple[str, ...],
    allow_downgrade: bool,
    show_unchanged: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Install or upgrade KIT as one change set; it always proposes.

    KIT is ``base`` (the base kit this core bundles: installs or upgrades it, offline),
    a kit directory, an OCI image layout, or a registry reference such as
    ``project-state:1.0.0`` or ``ghcr.io/acme/kits/foo@sha256:...``. A definition
    this instance holds differently takes the release's version (the preview
    says what that does: overwrites your edit, re-adds a retired definition,
    takes over one defined outside the kit) unless --keep or --keep-local-edits
    keeps yours. Every Line bound to a shipped provider moves to its exact build;
    --keep-build Line:NAME keeps its current builds. The preview names each
    re-pin, each carried disabled Line, and every Mandate downgraded to propose.
    Triggers stay unchanged; use line enable to resume and re-grant settle explicitly.
    Replaced definitions' other dependents are carried along. The change
    lands at once when the approval policy requires no approval, otherwise it
    stops at proposed. A kit that bundles provider packages installs the missing
    ones first (admin permission, the transfer install; registry dependencies
    resolve from the daemon's provider index, PyPI unless configured), installs a
    default provider by name from that index unless live operations of installed
    providers already serve every slot's capability (satisfied), and proposes the
    definitions once they land (each provider carries its own shapes); an
    install awaiting approval stops at awaiting_providers, and its activation
    proposes the definitions on their own, as you (kit status shows the resume).
    It previews by default; commit with ``--commit --at OID``.
    """
    # ``base`` is the base kit this core bundles, read before any directory or registry.
    bundle, origin = bundled_kit_reference(kit) or resolve_kit(kit)
    # Checked before the daemon, so a bad template refuses before anything is proposed.
    template = kit_automation_template(bundle)

    def add(client: Any, instance_id: str) -> Any:
        # A commit installs the bundled providers, so it stages their files;
        # a preview installs nothing and sends none.
        staged = (
            stage_kit_providers(client, instance_id, bundle)
            if dry_run is False
            else bundle.model_copy(update={"provider_files": ()})
        )
        request = KitAddRequest(
            bundle=staged,
            source=source or origin,
            keep=tuple(sorted(set(keep))),
            keep_local_edits=keep_local_edits,
            retire_dependents=tuple(sorted(set(retire_dependents))),
            keep_build=tuple(sorted(set(keep_build))),
            allow_downgrade=allow_downgrade,
            dry_run=dry_run,
            at=at,
        )
        return client.add_kit(instance_id, request)

    result = with_automation_template(_server_call(add, command_name="cruxible kit add"), template)
    if not show_unchanged:
        result = compact_kit_change(result)
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_kit_change(
            result,
            template_on_disk=_kit_directory_file(kit, result.automation_template),
            source=_pull_reference(kit, origin),
        )
    _exit_if_kit_blocked(result)


@kit_group.command("pull")
@click.argument("reference")
@click.option("--out", "out", required=True, type=click.Path(path_type=Path))
@click.option(
    "--layout", is_flag=True, help="Write an OCI image layout instead of a kit directory."
)
@json_option
@handle_errors
def pull_kit(reference: str, out: Path, layout: bool, output_json: bool) -> None:
    """Fetch and verify a kit into OUT, without installing it.

    REFERENCE is what ``kit add`` takes: a kit directory, an OCI image layout, or a
    registry reference such as ``security:1.3.0``.
    """
    if out.exists():
        raise click.UsageError(f"{out} already exists")
    # The layout keeps the published bytes exactly, so it carries the same digest.
    image, origin = resolve_kit_image(reference)
    bundle = unpack_artifact(KIT_ARTIFACT, image)
    if layout:
        write_layout(image, out, ref=origin)
    else:
        write_kit_directory(bundle, out)
    template = bundle.manifest.automation_template
    if output_json:
        _emit_json(
            {
                "source": origin,
                "content_digest": bundle.manifest.content_digest,
                **({} if template is None else {"automation_template": template}),
            }
        )
    else:
        click.echo(f"{origin}: {len(bundle.artifacts)} artifacts")
        _echo_automation_template(
            template,
            on_disk=None if layout else _kit_directory_file(str(out), template),
            reference=_pull_reference(reference, origin),
        )


@kit_group.command("status")
@click.option(
    "--offline", is_flag=True, help="Skip looking up newer releases in the kit's registry."
)
@json_option
@handle_errors
def kit_status(offline: bool, output_json: bool) -> None:
    """List installed kits, paths edited since install, and newer releases available.

    For a kit installed from a registry the client lists the repository's tags
    (short timeout) to show the latest available version; installing it stays
    explicit (kit add REFERENCE:VERSION).
    """
    result = check_kit_updates(
        _server_call(
            lambda client, instance_id: client.kit_status(instance_id),
            command_name="cruxible kit status",
        ),
        offline=offline,
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if not result.kits and not result.resumes:
        click.echo("No kits installed.")
    for resume in result.resumes:
        line = f"{resume.kit_id} {resume.version}: kit add {resume.state} (as {resume.actor_id})"
        if resume.waiting_on:
            line += f"; waiting on provider install {', '.join(resume.waiting_on)}"
        if resume.proposal_id is not None:
            line += f"; proposal {resume.proposal_id}"
        click.echo(line)
        if resume.detail:
            click.echo(f"  {printable(resume.detail)}")
        if resume.proposal_open and resume.proposal_id is not None:
            click.echo(
                f"  next: cruxible proposal approve {resume.proposal_id} --signer-id "
                f"{_SIGNER_PLACEHOLDER} (the approval that completes it activates it)"
            )
        elif resume.proposal_awaiting_readmit:
            from cruxible_core.kits.base_kit_pin import BASE_KIT_PIN

            # The head changed what it touches: the add is planned again at the head.
            # `base` names the bundled base kit, so it is printed only for that exact
            # release (id and content digest); another origin label could name any kit.
            if (resume.kit_id, resume.content_digest) == (
                BASE_KIT_PIN.kit_id,
                BASE_KIT_PIN.content_digest,
            ):
                rerun = "cruxible kit add base"
            else:
                rerun = "cruxible kit add '<KIT>'"
                if resume.source is not None:
                    rerun += f" (recorded origin: {printable(resume.source)})"
            click.echo(f"  awaiting readmit: accepted state changed what it touches; next: {rerun}")
    for kit in result.kits:
        available = ""
        if kit.update_check == "checked" and kit.latest_available is not None:
            newer = kit_version_key(kit.latest_available) > kit_version_key(kit.version)
            differs = (
                kit.latest_available == kit.version
                and kit.latest_content_digest is not None
                and kit.latest_content_digest != kit.content_digest
            )
            if newer:
                available = f"; latest {kit.latest_available} available"
            elif differs:
                # Same version, other content: not up to date, whatever the version says.
                available = (
                    f"; differs from the available {kit.latest_available}, content "
                    f"{kit.latest_content_digest}"
                )
            else:
                available = "; up to date"
        elif kit.update_check != "checked":
            available = f"; update check {kit.update_check.replace('_', ' ')}"
        # The content digest (what kit add pins), not an OCI image digest.
        click.echo(f"{kit.kit_id} {kit.version} content {kit.content_digest}{available}")
        if kit.source is not None:
            click.echo(f"  source: {kit.source}")
        if kit.provenance is not None:
            click.echo(
                f"  built by {kit.provenance.principal_id or 'an unattributed principal'} on "
                f"{kit.provenance.instance_id} at {kit.provenance.coordinate.git_oid[:12]} "
                "(claimed)"
            )
        for provider in kit.providers:
            state: str = provider.state
            if provider.state == "differs" and provider.installed_version is not None:
                state += f" (installed: {provider.installed_version})"
            click.echo(
                f"  provider {provider.provider_id} ({provider.package} {provider.version}): "
                f"{state}"
            )
        _echo_kit_requirements(kit.requires)
        for path in kit.drifted:
            click.echo(f"  edited locally: {path}")
        for kept in kit.kept:
            click.echo(
                f"  kept: {_public_name(kept.identity)} ({_KIT_KEEP_TEXT[kept.consequence][1]})"
            )


@kit_group.command("remove")
@click.argument("kit_id")
@click.option(
    "--detail",
    "show_plan",
    is_flag=True,
    help="On a blocked removal, also list each path it would retire or keep.",
)
@change_control_options
@json_option
@handle_errors
def remove_kit(
    kit_id: str, show_plan: bool, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Retire every artifact KIT_ID installed (previews by default).

    Lands at once when the approval policy requires no approval, otherwise it
    stops at proposed. Removal retires the kit's definitions that are still live,
    and a retired definition cannot be added back in this release.
    Live Claims under its definitions block it until they are retired;
    automation you authored over the kit is never retired by it. A blocked
    removal prints the refusal and its repair first, then counts per kind;
    --detail lists each path.
    """
    request = KitRemoveRequest(kit_id=kit_id, dry_run=dry_run, at=at)
    result = _server_call(
        lambda client, instance_id: client.remove_kit(instance_id, request),
        command_name="cruxible kit remove",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_kit_change(result, blocked_plan="list" if show_plan else "counts")
    _exit_if_kit_blocked(result)


@playbill_group.group("document")
def document_group() -> None:
    """Propose and read governed Documents."""


@document_group.command("propose")
@click.option("--envelope", type=PayloadFile())
@click.option("--template", is_flag=True, help="Print a Document proposal template.")
@click.option("--name", "proposal_name")
@click.option("--no-accept", is_flag=True, help="Propose only; do not land the change.")
@change_control_options
@json_option
@handle_errors
def propose_document(
    envelope: str | None,
    template: bool,
    proposal_name: str | None,
    no_accept: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose one Document; it lands when policy and your tier allow.

    --no-accept keeps the proposal for someone to activate.
    """

    if (envelope is None) == (not template):
        raise click.UsageError("choose exactly one of --envelope or --template")
    if template:
        if proposal_name is not None:
            raise click.UsageError("--name applies only when --envelope is supplied")
        _emit_json(document_example().model_dump(mode="json"))
        return
    if proposal_name is None:
        raise click.UsageError("--name is required with --envelope")
    assert envelope is not None
    shell = _read_model(envelope, DocumentShell)
    result = _server_call(
        lambda client, instance_id: client.propose_document(
            instance_id,
            shell=shell.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
            accept="never" if no_accept else "if_allowed",
        ),
        command_name="cruxible document propose",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_proposal(result, result.lint)


@playbill_group.group("capture")
def capture_group() -> None:
    """Read retained evidence from completed observations and Procedures, or erase a body."""


@capture_group.command("read")
@click.argument("capture_digest")
@click.option(
    "--max-bytes",
    type=click.IntRange(min=0),
    default=4 * 1024 * 1024,
    show_default=True,
    help="The most body bytes returned.",
)
@handle_errors
def read_capture(capture_digest: str, max_bytes: int) -> None:
    """Verify and read one retained Capture: its envelope and body, as JSON.

    CAPTURE_DIGEST is the Capture's digest. The body is base64 (`body`), at most
    --max-bytes of it; reading it needs body-read permission. An erased or
    unavailable body answers `unavailable` with its reason.
    """

    from cruxible_client.contracts.capture_reads import CaptureReadRequest

    result = _server_call(
        lambda client, instance_id: client.read_capture(
            instance_id, CaptureReadRequest(capture_digest=capture_digest, max_bytes=max_bytes)
        ),
        command_name="cruxible capture read",
    )
    _emit_json(result.model_dump(mode="json"))


@capture_group.command("erase")
@click.argument("capture")
@click.option(
    "--because",
    required=True,
    help="Why the body is erased; recorded and shown wherever the Capture is read, so "
    "never repeat the erased bytes.",
)
@change_control_options
@json_option
@handle_errors
def erase_capture(
    capture: str, because: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Erase one Capture's retained body: the bytes are destroyed, the record stays.

    CAPTURE is a digest or the CAP- handle a get card prints. Only a Capture
    whose CaptureContract authorizes erasure (erasure: authorized_by_rule) can
    be erased. The envelope, its contract, its landing records and an erasure
    record (who, when, why) stay; Claims citing it keep their history and read
    the evidence as erased, no longer replayable. A live Claim citing it can
    still be revised or retired; `rebind --from-capture CAPTURE --capture NEW`
    moves every Claim citing it onto a fresh observation under the same
    contract. Requires graph_write. It cannot be undone, so it previews first;
    commit that preview with --commit --at DIGEST. A refusal prints its
    repair.
    """

    from cruxible_client.contracts.capture_erasure import CaptureEraseRequest

    try:
        request = CaptureEraseRequest(
            capture_digest=capture, because=because, dry_run=dry_run, at=at
        )
    except ValidationError as exc:
        raise click.ClickException(validation_summary(exc)) from exc
    result = _server_call(
        lambda client, instance_id: client.erase_capture(instance_id, request),
        command_name="cruxible capture erase",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    record = result.erasure
    handle = capture_handle(record.capture_digest)
    verb = {
        "would_erase": "Would erase",
        "erased": "Erased",
        "already_erased": "Already erased",
    }[result.status]
    count = len(record.body_digests)
    click.echo(
        f"{verb} the retained body of {handle} ({count} object{'s' if count != 1 else ''}) "
        f"under {_public_name(record.capture_contract)}"
    )
    if result.status != "would_erase":
        click.echo(f"By: {printable(record.erased_by)} at {record.erased_at.isoformat()}")
    click.echo(f"Because: {printable(record.because)}")
    if result.citing_claim_count:
        listed = ", ".join(result.citing_claims)
        more = result.citing_claim_count - len(result.citing_claims)
        click.echo(
            f"Cited by {result.citing_claim_count} live Claim"
            f"{'s' if result.citing_claim_count != 1 else ''}: {listed}"
            + (f" (+{more} more)" if more > 0 else "")
        )
    if result.left_without_evidence:
        click.echo(
            "Left with no retained evidence (next lists them as claim_evidence_erased): "
            + ", ".join(result.left_without_evidence)
        )
    if result.status == "would_erase":
        click.echo(
            "Verdicts will read it as no longer replayable; each field's rules decide the rest."
        )
        echo_preview_next(result.status, result.coordinate)


@playbill_group.group("proposal")
def proposal_group() -> None:
    """Inspect, review, approve, and activate candidates."""


@proposal_group.command("list")
@click.option(
    "--status",
    type=click.Choice(["open", "awaiting_readmit", "settled", "incomplete"]),
    default=None,
    help="awaiting_readmit: the head changed what it touches since its base (named "
    "under the row); it waits for its author's readmit.",
)
@click.option(
    "--limit",
    default=contracts.PROPOSAL_LIST_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, contracts.PROPOSAL_LIST_MAX_LIMIT),
    help="Proposals per page.",
)
@click.option("--cursor", default=None, help="Continue a previous page of the same listing.")
@json_option
@handle_errors
def list_proposals(status: str | None, limit: int, cursor: str | None, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_proposals(
            instance_id,
            status=cast(Any, status),
            limit=limit,
            cursor=cursor,
        ),
        command_name="cruxible proposal list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    from cruxible_client.contracts.text_display import proposal_list_lines

    for line in proposal_list_lines(result):
        click.echo(line)
    _echo_list_continuation(result.next_cursor)


@proposal_group.command("readmit")
@click.argument("proposal_id")
@change_control_options
@json_option
@handle_errors
def readmit_proposal(
    proposal_id: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    result = _server_call(
        lambda client, instance_id: client.readmit_proposal(
            instance_id,
            client.resolve_proposal_selector(instance_id, proposal_id).proposal_id,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible proposal readmit",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if result.proposal is None:
        click.echo(_already_live_line(result.source_proposal_id))
        return
    proposal = result.proposal.proposal
    evaluation = proposal.get("evaluation", {})
    admission = proposal.get("admission", {})
    if result.proposal.status != "admitted":
        click.echo(f"{result.proposal.status}  from {result.source_proposal_id}")
        echo_preview_next(result.proposal.status, result.proposal.accepted_coordinate)
        return
    click.echo(
        f"{evaluation.get('verdict')}  {admission.get('proposal_id')}  "
        f"from {result.source_proposal_id}"
    )


@proposal_group.command("withdraw")
@click.argument("proposal_id")
@click.option(
    "--reason",
    required=True,
    help="Why this proposal will never be settled. Recorded verbatim.",
)
@change_control_options
@json_option
@handle_errors
def withdraw_proposal(
    proposal_id: str, reason: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Retire an open proposal that will never be activated."""

    result = _server_call(
        lambda client, instance_id: client.withdraw_proposal(
            instance_id,
            client.resolve_proposal_selector(instance_id, proposal_id).proposal_id,
            reason=reason,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible proposal withdraw",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    already = " (already withdrawn)" if result.already_withdrawn else ""
    click.echo(f"{result.status}  {result.proposal_id}  {result.withdrawn_at}{already}")
    click.echo(f"Reason: {result.reason}")
    echo_preview_next(result.status, result.coordinate)


@proposal_group.command("reject")
@click.argument("proposal_id")
@click.option(
    "--reason",
    required=True,
    help="Why this proposal should not be accepted. Recorded verbatim.",
)
@change_control_options
@json_option
@handle_errors
def reject_proposal(
    proposal_id: str, reason: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Reject another actor's open proposal as its reviewer."""

    result = _server_call(
        lambda client, instance_id: client.reject_proposal(
            instance_id,
            client.resolve_proposal_selector(instance_id, proposal_id).proposal_id,
            reason=reason,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible proposal reject",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    already = " (already rejected)" if result.already_rejected else ""
    click.echo(f"{result.status}  {result.proposal_id}  {result.rejected_at}{already}")
    click.echo(f"Reason: {result.reason}")
    echo_preview_next(result.status, result.coordinate)


@proposal_group.command("review")
@click.argument("proposal_id")
@click.option("--include-body/--redacted", default=None)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose projection catalog and markers are observed locally.",
)
@click.option(
    "--item-reasons-after",
    default=None,
    help="Page a Procedure proposal's item reasons after this cursor (the last page's More:).",
)
@click.option(
    "--item-reasons-limit",
    default=None,
    type=click.IntRange(1, 500),
    help="Item reasons per page (default 50).",
)
@click.option(
    "--detail",
    is_flag=True,
    help="The full candidate review: every member's artifact and semantic delta, "
    "governance and attestation coverage.",
)
@click.option(
    "--changes-from",
    default=0,
    type=click.IntRange(0),
    help="Show value changes from this position (the last page's More values:).",
)
@click.option(
    "--definitions-from",
    default=0,
    type=click.IntRange(0),
    help="Show definitions from this position (the last page's More definitions:).",
)
@click.option("--summary-after", default=None, help="Next group page cursor (grouped rows).")
@click.option("--summary-group", default=None, help="Page the stored rows of this exact group.")
@click.option("--rows-after", default=None, help="Next row page cursor.")
@click.option("--rows-limit", default=5, type=click.IntRange(1, 10))
@click.option("--groups-limit", default=5, type=click.IntRange(1, 10))
@click.option("--findings-after", default=None, help="Next findings page cursor.")
@json_option
@handle_errors
def review_proposal(
    proposal_id: str,
    include_body: bool | None,
    workspace_root: str,
    item_reasons_after: str | None,
    item_reasons_limit: int | None,
    output_json: bool,
    detail: bool,
    summary_after: str | None,
    summary_group: str | None,
    rows_after: str | None,
    rows_limit: int,
    groups_limit: int,
    findings_after: str | None,
    changes_from: int,
    definitions_from: int,
) -> None:
    """Read a proposal before approving it: its value changes and what each cites, and its
    definitions. ``--json`` adds the stored fields grouped by kind; ``--detail`` is the
    full candidate review."""

    workspace = Path(workspace_root)
    grouped = output_json or any(item is not None for item in (summary_after, summary_group))

    def _review_at_observed_coordinate(
        client: CruxibleClient, instance_id: str
    ) -> contracts.ProposalReview | contracts.ProposalReviewSummary:
        observation: dict[str, object] | None = None
        if detail:
            head = client.head(instance_id)
            next_observation = observe_next_workspace(workspace)
            observation = {
                "tag": "playbill-review-workspace-observation-v1",
                "presentation_policy": next_observation.get("presentation_policy"),
                "presentation_policy_notes": next_observation.get("presentation_policy_notes", []),
                "projection_coverage": None,
            }
            projection = observe_projection_coverage(
                workspace,
                coordinate=contracts.AcceptedCoordinate.model_validate(
                    head.coordinate.model_dump(mode="json")
                ),
            )
            if projection is not None:
                observation["projection_coverage"] = projection
        resolved_id = client.resolve_proposal_selector(instance_id, proposal_id).proposal_id
        return client.review_proposal(
            instance_id,
            resolved_id,
            include_body=(True if include_body is None else include_body)
            if detail
            else include_body,
            detail=detail,
            summary_after=summary_after,
            summary_group=summary_group,
            rows_after=rows_after,
            rows_limit=rows_limit,
            groups_limit=groups_limit,
            findings_after=findings_after,
            workspace_observation=observation,
            item_reasons_after=item_reasons_after,
            item_reasons_limit=item_reasons_limit,
            changes_from=changes_from,
            definitions_from=definitions_from,
            groups=grouped,
        )

    result = _server_call(
        _review_at_observed_coordinate,
        command_name="cruxible proposal review",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    elif isinstance(result, contracts.ProposalReviewSummary):
        click.echo(render_review_summary(result, file_paths=_display_file_paths(workspace)))
    else:
        review = ProposalReview.model_validate(result.model_dump(mode="json"))
        click.echo(render_playbill_proposal_review_pointer(review), nl=False)


@proposal_group.command("approve")
@click.argument("proposal_id")
@click.option(
    "--signer-id",
    default=None,
    help="Signing principal (default: CRUXIBLE_PRINCIPAL_ID or the global --principal-id).",
)
@click.option(
    "--key",
    "private_key_path",
    default=None,
    envvar=PRINCIPAL_KEY_ENV,
    type=click.Path(dir_okay=False),
    help=(
        "The signer's private key (also CRUXIBLE_PRINCIPAL_KEY; default: the key the "
        "signer's remembered settings or `cruxible init` keep)."
    ),
)
@click.option(
    "--yes",
    is_flag=True,
    help="Sign without the prompt, printing one counts line and any earlier refusals; "
    "needed where no one is at the terminal.",
)
@click.option(
    "--candidate-digest",
    "candidate_digest",
    default=None,
    metavar="DIGEST",
    help="The candidate digest you reviewed (`proposal review` prints it): sign that "
    "candidate or refuse. If accepted state moved and the proposal was proposed again "
    "at the head, the refusal names the new proposal and says whether its members are "
    "identical. Without it, one proposed again with identical members is signed.",
)
@click.option(
    "--hold",
    is_flag=True,
    help="Sign only: leave activating a candidate this approval completes to someone else.",
)
@click.option(
    "--full",
    is_flag=True,
    help="Render every member's artifact and semantic delta, not the change summary.",
)
@json_option
@handle_errors
def approve_proposal(
    proposal_id: str,
    signer_id: str | None,
    private_key_path: str | None,
    yes: bool,
    candidate_digest: str | None,
    hold: bool,
    full: bool,
    output_json: bool,
) -> None:
    """Sign one reviewed candidate; the approval that completes it activates it.

    Activation runs in the same command when no approval is still required and
    your tier may activate (graph_write). `--hold` signs only, for a reviewer
    leaving activation timing to someone else, who runs `proposal activate`.
    Otherwise the result says what remains. Without a terminal to prompt at,
    it refuses unless --yes is passed.

    When accepted state moved since the proposal was made and nothing it
    touches changed, the same change set is proposed again at the head as a new
    proposal, and that one is signed. `--candidate-digest` binds the approval
    to the candidate you reviewed instead: it then refuses, naming the new
    proposal to review.
    """

    if not yes and not _can_confirm():
        raise click.UsageError(
            "proposal approve asks before signing and no one is at this terminal; review it "
            f"with `cruxible proposal review {proposal_id}`, then rerun with --yes"
        )
    configured = _root_ctx_obj().get("principal_id")
    if signer_id is None:
        signer_id = str(configured) if configured is not None else _signer_from_daemon()
    resolved_signer: str = signer_id

    # The prompt shows the reviewer's view `proposal review` prints (its first page).
    shows_summary = not (output_json or full or yes)

    def _resolve_and_prepare(
        client: CruxibleClient, instance_id: str
    ) -> tuple[
        str,
        contracts.ApprovalChallenge,
        CompilerUpgradeSigningKey,
        contracts.ProposalReviewSummary | None,
    ]:
        key = _signing_key(private_key_path, instance_id=instance_id, signer_id=resolved_signer)
        if key is None:
            raise click.UsageError(_missing_signing_key_help(instance_id, resolved_signer))
        resolved_id = client.resolve_proposal_selector(instance_id, proposal_id).proposal_id
        # A signer registered after the proposal's base cannot sign its candidate;
        # the challenge then lands it at the head and names the landed proposal.
        challenge = client.prepare_approval(
            instance_id,
            resolved_id,
            signer_id=resolved_signer,
            include_body=True,
            reviewed_candidate_digest=candidate_digest,
        )
        summary = None
        if shows_summary:
            page = client.review_proposal(instance_id, challenge.proposal_id, groups=False)
            assert isinstance(page, contracts.ProposalReviewSummary)
            summary = page
        return resolved_id, challenge, key, summary

    resolved_id, challenge, key, summary = _server_call(
        _resolve_and_prepare,
        command_name="cruxible proposal approve",
    )
    said_landed = challenge.proposal_id != resolved_id
    if said_landed:
        if not output_json:
            click.echo(
                f"Landed at the head as {challenge.proposal_id}: the same change set "
                f"(proposed as {resolved_id}, before {resolved_signer} was registered); "
                "signing it"
            )
        resolved_id = challenge.proposal_id
    review = ProposalReview.model_validate(challenge.review.model_dump(mode="json"))
    if not output_json:
        if full:
            click.echo(render_playbill_proposal_review(review), nl=False)
        elif yes:
            for line in _approval_counts_lines(review):
                click.echo(line)
        else:
            assert summary is not None
            click.echo(
                render_playbill_approval_summary(review, summary, file_paths=_display_file_paths()),
                nl=False,
            )
    if key.source != "option":
        click.echo(_signing_key_line(key), err=True)
    if not yes and not click.confirm("Sign this exact candidate?", err=True):
        # Declining is an answer, not an error: say so and sign nothing.
        click.echo("Not approved.", err=True)
        raise SystemExit(1)
    principal = PrincipalRecord.model_validate(challenge.signer_principal)
    signer = LocalEd25519ApprovalSigner.open(
        signer_id=resolved_signer,
        private_key_path=Path(key.private_key_path),
        expected_public_key=principal.public_key,
        forbidden_roots=_custody_forbidden_roots(),
    )
    statement = ApprovalStatement.model_validate(challenge.statement)
    if candidate_digest is not None and statement.payload_digest != candidate_digest:
        # The daemon refuses this itself; the key never signs past a challenge
        # that is not for the reviewed candidate, whatever served it.
        raise ReviewedCandidateChanged(
            proposal_id,
            reviewed_candidate_digest=candidate_digest,
            current_proposal_id=challenge.proposal_id,
            current_candidate_digest=statement.payload_digest,
        )
    attestation = signer.sign(statement)
    result = _server_call(
        lambda client, instance_id: client.submit_approval(
            instance_id,
            resolved_id,
            attestation=attestation.model_dump(mode="json"),
            hold=hold,
            reviewed_candidate_digest=candidate_digest,
        ),
        command_name="cruxible proposal approve",
    )
    landed = None if candidate_digest is not None else _landed_same_change_set(result)
    if landed is not None:
        said_landed = True
        # Nothing it touches changed since its base: the daemon landed the same
        # change set at the head under its author. That candidate is what
        # settles, so the approval confirmed above signs it.
        if not output_json:
            click.echo(
                f"Landed at the head as {landed}: the same change set "
                f"(proposed as {resolved_id}); signing it"
            )

        def _approve_landed(
            client: CruxibleClient, instance_id: str
        ) -> contracts.ProposalApprovalResult:
            challenge = client.prepare_approval(instance_id, landed, signer_id=resolved_signer)
            statement = ApprovalStatement.model_validate(challenge.statement)
            return client.submit_approval(
                instance_id,
                landed,
                attestation=signer.sign(statement).model_dump(mode="json"),
                hold=hold,
            )

        result = _server_call(_approve_landed, command_name="cruxible proposal approve")
    result = _with_git_workspace_note(result)
    if output_json:
        _emit_json(_json_receipt(result))
        return
    if isinstance(result, contracts.ProposalRecompiled):
        _echo_recompiled_proposal(result)
        return
    if isinstance(result, contracts.ActivationReceipt):
        _echo_activation(result)
        return
    # Candidate, then the proposal it settles, as the Signing line names them.
    click.echo(f"Approved {result.candidate_digest} ({result.proposal_id}) as {result.signer_id}")
    if result.readmits is not None and not said_landed:
        # Said above when this command landed it; here the proposal passed is
        # itself a readmission, and the receipt names what it replaced.
        click.echo(_replaces_line(result.proposal_id, result.readmits))
    for line in _approval_outcome_lines(result):
        click.echo(line)


def _replaces_line(proposal_id: str, source_id: str) -> str:
    """A readmission's receipt names the proposal it replaces, which is the one reviewed."""

    return f"{proposal_id} replaces {source_id} (readmitted at the head); act on {proposal_id}"


def _signer_from_daemon() -> str:
    """The signer when this process configures none: the principal the daemon says acts.

    A bearer credential bound to a principal names it. A process acting as no
    principal is refused exactly as every other proposal act refuses it (the
    daemon's ``authoring_refusal``: ``principal_unconfigured`` or
    ``credential_unbound``), with ``--principal-id ID --key FILE`` added to the
    repair: the submitter must be a principal too, so ``--signer-id`` alone
    would still refuse.
    """

    from cruxible_client.contracts.repairs import RepairOperation
    from cruxible_core.errors import PrincipalRefusedError

    identity = _server_call(
        lambda client, instance_id: client.whoami(instance_id),
        command_name="cruxible proposal approve",
    )
    if identity.can_author and identity.actor_id is not None:
        return identity.actor_id
    refusal = identity.authoring_refusal
    if refusal is None or refusal.code not in {
        "cruxible.identity.principal_unconfigured",
        "cruxible.identity.credential_unbound",
    }:
        if identity.actor_id is not None and identity.actor_id_source != "local_operator":
            return identity.actor_id
        raise click.UsageError(
            "proposal approve needs the signing principal; repair: pass --signer-id ID "
            "or set CRUXIBLE_PRINCIPAL_ID"
        )
    repair = refusal.repair
    if isinstance(repair, RepairOperation):
        repair = repair.model_copy(
            update={"arguments": {**repair.arguments, "or_pass": "--principal-id ID --key FILE"}}
        )
    code: Literal[
        "cruxible.identity.principal_unconfigured", "cruxible.identity.credential_unbound"
    ] = (
        "cruxible.identity.principal_unconfigured"
        if refusal.code == "cruxible.identity.principal_unconfigured"
        else "cruxible.identity.credential_unbound"
    )
    raise PrincipalRefusedError(
        code,
        f"{refusal.detail}; to approve, pass --principal-id ID --key FILE (it signs as that "
        "principal; --signer-id names a different signer, whose approval it submits)",
        repair=repair,
    )


def _landed_same_change_set(result: contracts.ProposalApprovalResult) -> str | None:
    """The proposal an approval landed at the head with the same change set, awaiting review.

    An approval of a proposal whose base moved, with nothing it touches changed
    since, lands the change set at the head as a new candidate; an approval
    signs exactly one candidate, so the landed one is signed next.
    """

    if (
        isinstance(result, contracts.ProposalRecompiled)
        and result.status == "pending_review"
        and result.members_identical
        and result.proposal_id is not None
    ):
        return result.proposal_id
    return None


def _approval_counts_lines(review: ProposalReview) -> list[str]:
    """What `approve --yes` prints before signing: one counts line, then every earlier
    refusal of a proposed statement on its evidence (`--full` renders everything)."""

    dispositions = Counter(member.disposition for member in review.members)
    counts = ", ".join(f"{count} {name}" for name, count in sorted(dispositions.items()))
    total = len(review.members)
    lines = [
        f"Signing {review.candidate_digest} ({review.proposal_id}): {total} change"
        f"{'s' if total != 1 else ''}" + (f" ({counts})" if counts else "")
    ]
    for member in review.members:
        for receipt in member.prior_refusals:
            verb = "retired" if receipt.kind == "claim_retired" else "rejected"
            subject = (
                f"Claim {receipt.claim_id}"
                if receipt.kind == "claim_retired"
                else f"proposal {receipt.proposal_id} (Claim {receipt.claim_id})"
            )
            code = "" if receipt.reason_code is None else f" [{receipt.reason_code}]"
            lines.append(
                f"  earlier refusal {member.path}: {subject} {verb} by {receipt.decided_by} "
                f"at {receipt.decided_at}{code}"
            )
    return lines


def _already_live_line(proposal_id: str) -> str:
    return (
        f"{proposal_id} is already live at the head: every change it carried is accepted; "
        "closed as answered, nothing to review."
    )


def _echo_recompiled_proposal(result: contracts.ProposalRecompiled) -> None:
    if result.status == "already_live" or result.proposal_id is None:
        click.echo(_already_live_line(result.source_proposal_id))
        return
    click.echo(
        f"Recompiled as {result.proposal_id}; review this new proposal "
        f"(members identical: {result.members_identical})"
    )
    for member in result.changed_members:
        click.echo(f"  {member.change}: {member.path}")
    if result.changed_members_truncated:
        click.echo("  Changed member paths truncated; review the new proposal for all members.")
    operation = "review" if result.status == "pending_review" else "activate"
    click.echo(f"Next: cruxible proposal {operation} {result.proposal_id}")


def _approval_outcome_lines(receipt: contracts.ApprovalReceipt) -> list[str]:
    """What the approval did after signing: the activation, or what remains."""

    activation = receipt.activation
    if activation is not None:
        if activation.status == "accepted" and activation.accepted_coordinate is not None:
            return [f"Activated at {activation.accepted_coordinate.git_oid}"]
        return [
            "Not activated: another change was accepted first; "
            f"Next: cruxible proposal readmit {receipt.proposal_id}"
        ]
    proposal = receipt.proposal_id
    if receipt.not_activated == "approvals_remaining":
        count = receipt.approvals_remaining
        return [
            f"Awaiting {count} more approval{'s' if count != 1 else ''}"
            f"{_awaiting_whom(receipt)}; the approval that completes it activates it",
            *(f"Next: {step}" for step in _awaiting_approve_steps(receipt)),
        ]
    if receipt.not_activated == "held":
        return [f"Held: not activated. Next: cruxible proposal activate {proposal}"]
    if receipt.not_activated == "activation_not_permitted":
        return [
            "Not activated: your tier cannot activate (needs graph_write). "
            f"Next: someone who can runs cruxible proposal activate {proposal}"
        ]
    if receipt.not_activated == "stale":
        return [
            "Not activated: accepted state moved past this proposal. "
            f"Next: cruxible proposal readmit {proposal}"
        ]
    return []


def _awaiting_whom(receipt: contracts.ApprovalReceipt) -> str:
    """Whose signatures are outstanding, as the receipt names them."""

    parts = [f"{signer} must sign" for signer in receipt.required_signers]
    others = receipt.approvals_remaining - len(receipt.required_signers)
    if others and receipt.eligible_signers:
        parts.append(f"{others} from: {', '.join(receipt.eligible_signers)}")
    return f" ({'; '.join(parts)})" if parts else ""


def _awaiting_approve_steps(receipt: contracts.ApprovalReceipt) -> list[str]:
    """The approve command for each outstanding signer the receipt names.

    It names the signer and no key: the signer's key is found by the one
    lookup (its remembered settings here), or the refusal names the repair.
    """

    proposal = receipt.proposal_id
    steps = [
        f"cruxible proposal approve {proposal} --signer-id {signer}"
        for signer in receipt.required_signers
    ]
    if receipt.approvals_remaining > len(receipt.required_signers):
        signer = (
            receipt.eligible_signers[0]
            if len(receipt.eligible_signers) == 1
            else _SIGNER_PLACEHOLDER
        )
        steps.append(f"cruxible proposal approve {proposal} --signer-id {signer}")
    return steps


#: A signer only the caller can name, as every suggested command shows it.
_SIGNER_PLACEHOLDER = "'<SIGNER_ID>'"


@proposal_group.command("activate")
@click.argument("proposal_id")
@brief_option
@json_option
@handle_errors
def activate_proposal(proposal_id: str, output_brief: bool, output_json: bool) -> None:
    """Settle a candidate nothing activated yet; the daemon delivers the floor.

    That is one whose approval was held (`proposal approve --hold`), one that
    needs no approval, or one another signer completed without activating.

    Activation is a daemon act and writes nothing locally. A workspace the local
    daemon serves gets its floor from the daemon's floor-refresh trigger; other
    setups pull it with `cruxible floor export`. Read exactly what was accepted
    with `cruxible get` at the receipt's coordinate; `cruxible next` reports any
    projection block the change left stale.
    """

    result = _server_call(
        lambda client, instance_id: client.activate_proposal(
            instance_id,
            client.resolve_proposal_selector(instance_id, proposal_id).proposal_id,
        ),
        command_name="cruxible proposal activate",
    )
    if output_brief:
        _emit_brief(
            outcome=result.status,
            ids={
                "proposal": result.proposal_id,
                "coordinate": (
                    None
                    if result.accepted_coordinate is None
                    else result.accepted_coordinate.git_oid
                ),
            },
            next_command="cruxible next --brief",
        )
        return
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if isinstance(result, contracts.ProposalRecompiled):
        _echo_recompiled_proposal(result)
        return
    _echo_activation(result)


def _echo_activation(result: contracts.ActivationReceipt) -> None:
    if result.readmits is not None:
        click.echo(_replaces_line(result.proposal_id, result.readmits))
    if result.status == "accepted" and result.accepted_coordinate is not None:
        click.echo(f"accepted ({result.accepted_coordinate.git_oid[:12]}) by {result.activated_by}")
        click.echo("next: cruxible next")
    else:
        click.echo(
            f"not accepted ({result.status}): accepted state moved after this proposal was checked"
        )
        click.echo(f"next: cruxible proposal readmit {result.proposal_id}")


@playbill_group.command("whoami")
@json_option
@handle_errors
def whoami(output_json: bool) -> None:
    """Explain the transport-derived actor, permission mode, and principal status."""

    result = _server_call(
        lambda client, instance_id: client.whoami(instance_id),
        command_name="cruxible whoami",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Actor: {result.actor_id or 'none (this credential acts as no principal)'}")
    if result.actor_id_source == "runtime_credential":
        click.echo(
            f"Actor ID is the principal this bearer credential is bound to "
            f"(credential: {result.credential_label})"
        )
    elif result.actor_id_source == "unbound_credential":
        click.echo(
            f"Bearer credential {result.credential_label} is bound to no principal: it keeps "
            "its transport authority but cannot author"
        )
    elif result.actor_id_source == "principal_claim":
        click.echo("Actor ID comes from the configured principal ID (CRUXIBLE_PRINCIPAL_ID)")
    else:
        click.echo("Actor ID comes from the local operator identity (no principal configured)")
    if not result.authenticated:
        click.echo(
            "Identity is a claim, not authentication: daemon auth is off, so every "
            "process of this OS user is equally trusted."
        )
    click.echo(f"Credential permission mode: {result.credential_permission_mode}")
    click.echo(f"Principal registration: {result.principal_registration_status or 'none'}")
    click.echo(f"Active principals: {', '.join(result.active_principal_ids) or 'none'}")
    if result.authoring_refusal is None:
        click.echo("Can author: yes")
    else:
        refusal = result.authoring_refusal
        click.echo(f"Can author: no ({refusal.code})")
        click.echo(f"  Why: {refusal.detail}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@playbill_group.group("files")
def files_group() -> None:
    """Compile catalogued workspace files into exact-byte Document proposals."""


def _file_options(function: Callable[..., Any]) -> Callable[..., Any]:
    function = click.option(
        "--root",
        "repository_root",
        default=None,
        help="Workspace root the catalog's locators resolve against (default: this worktree).",
    )(function)
    function = click.option(
        "--local-catalog",
        default=None,
        help="A local overlay catalog (default: .cruxible/files.local.yaml when present).",
    )(function)
    function = click.option(
        "--root-alias",
        multiple=True,
        help="NAME=PATH for a local catalog entry's root_alias (repeatable).",
    )(function)
    return click.option(
        "--catalog",
        "portable_catalog",
        default=None,
        help="The portable catalog (default: the workspace's .cruxible/files.yaml).",
    )(function)


def _file_catalog_and_root(
    portable_catalog: str | None, local_catalog: str | None, repository_root: str | None
) -> tuple[FileCatalog, Path]:
    """The catalog the other commands use, discovered the same way unless named."""

    if repository_root is not None:
        root = Path(repository_root).expanduser().resolve()
    else:
        resolution = _local_workspace_root()
        _emit_git_workspace_note(resolution)
        root = resolution.workspace_root or Path.cwd().resolve()
    if portable_catalog is not None:
        return _catalog(portable_catalog, local_catalog), root
    if local_catalog is not None:
        raise click.UsageError("--local-catalog needs --catalog; omit both to discover them")
    return WorkspaceFiles(root).catalog, root


@files_group.command("compile")
@_file_options
@click.option(
    "--output",
    required=True,
    type=click.Path(dir_okay=False),
    help="Where to write the bundle (refuses an existing file).",
)
@json_option
@handle_errors
def compile_files(
    portable_catalog: str | None,
    local_catalog: str | None,
    root_alias: tuple[str, ...],
    repository_root: str | None,
    output: str,
    output_json: bool,
) -> None:
    """Freeze every catalogued Document's exact bytes into a bundle for files propose.

    Each entry that declares a Document compiles to its next revision against
    the accepted head; evidence-only entries compile to nothing. Writes only
    the local bundle file.
    """

    catalog, root = _file_catalog_and_root(portable_catalog, local_catalog, repository_root)
    bundle = _server_call(
        lambda client, instance_id: _compile_remote_context(
            client,
            instance_id,
            catalog=catalog,
            repository_root=root,
            aliases=_root_aliases(root_alias),
        ),
        command_name="cruxible files compile",
    )
    _write_bundle(output, bundle)
    if output_json:
        _emit_json(bundle.manifest.model_dump(mode="json"))
    else:
        click.echo(f"Compiled {bundle.manifest.compilation_digest} -> {output}")


@files_group.command("check")
@_file_options
@json_option
@handle_errors
def check_files(
    portable_catalog: str | None,
    local_catalog: str | None,
    root_alias: tuple[str, ...],
    repository_root: str | None,
    output_json: bool,
) -> None:
    """Compare each catalogued Document's file with accepted and pending state; writes nothing.

    Each file reads aligned, modified, ahead, pending, behind, diverged or
    untracked.
    """

    catalog, root = _file_catalog_and_root(portable_catalog, local_catalog, repository_root)

    def call(client: CruxibleClient, instance_id: str) -> contracts.FileCheckResult:
        bundle = _compile_remote_context(
            client,
            instance_id,
            catalog=catalog,
            repository_root=root,
            aliases=_root_aliases(root_alias),
        )
        return client.check_file_bundle(instance_id, bundle=bundle.model_dump(mode="json"))

    result = _server_call(call, command_name="cruxible files check")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for alignment in result.alignments:
        click.echo(f"{alignment.get('name')}: {alignment.get('state')}")
    if not result.alignments:
        click.echo("No catalogued Documents to check.")


def _catalog_edit_root(repository_root: str | None) -> Path:
    if repository_root is not None:
        return Path(repository_root).expanduser().resolve()
    resolution = _local_workspace_root(required="cruxible files add/remove")
    _emit_git_workspace_note(resolution)
    assert resolution.workspace_root is not None
    return resolution.workspace_root


def _echo_catalog_edit(result: FileCatalogEdit, *, output_json: bool) -> None:
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if result.status == "removed":
        suffix = (
            " (it was the last entry; the catalog file is gone)" if result.catalog_removed else ""
        )
        click.echo(f"Removed file {result.name} from {result.catalog_path}{suffix}")
        return
    assert result.entry is not None
    # A file with no Document is still a citable source and a projection target.
    shape = (
        f"Document {result.entry.document_id}"
        if result.entry.is_document
        else "no Document: cite it, or project blocks into it"
    )
    click.echo(
        f"{result.status.capitalize()} file {result.name} -> {result.entry.locator} "
        f"({shape}) in {result.catalog_path}"
    )


@files_group.command("add")
@click.argument(
    "path", nargs=-1, required=True, metavar="[NAME] PATH", type=click.Path(dir_okay=False)
)
@click.option("--document-id", default=None, help="The Document this file compiles to.")
@click.option("--document-kind", default=None, help="The Document's kind (with --document-id).")
@click.option("--title", default=None, help="The Document's title (with --document-id).")
@click.option(
    "--media-type",
    default=None,
    help="The Document's media type (default: from the file suffix, e.g. text/markdown).",
)
@click.option(
    "--scope",
    "governance_scope",
    multiple=True,
    help="A governance scope the Document is in (repeatable; with --document-id).",
)
@click.option(
    "--required-tier",
    type=click.Choice(["governed_write", "graph_write", "admin"]),
    default=None,
    help="The tier activating the Document needs (default graph_write).",
)
@click.option("--public-uri", default=None, help="A public URI for the Document.")
@click.option("--replace", is_flag=True, help="Update NAME's entry when it is already catalogued.")
@click.option(
    "--root",
    "repository_root",
    default=None,
    help="Workspace root the catalog belongs to (default: this workspace folder).",
)
@json_option
@handle_errors
def add_file(
    path: tuple[str, ...],
    document_id: str | None,
    document_kind: str | None,
    title: str | None,
    media_type: str | None,
    governance_scope: tuple[str, ...],
    required_tier: str | None,
    public_uri: str | None,
    replace: bool,
    repository_root: str | None,
    output_json: bool,
) -> None:
    """Catalogue PATH under NAME in the workspace's .cruxible/files.yaml.

    NAME is the stable source name evidence is cited under (`--evidence-file
    PATH#ANCHOR` resolves through it); left out, it is doc-<path>, the name a
    write citing the file gives it. With --document-id, --document-kind,
    --title and --scope the entry also declares the Document it compiles to
    (files compile, files propose); without them the file is still a source
    to cite and a page to project blocks into, just not a Document. The
    whole catalog is validated before the file is rewritten: an existing NAME
    refuses unless --replace, and a partial Document or a duplicate Document
    target refuses. Writes only the catalog file (commit it with Git, if you use Git).
    """

    # NAME is optional and comes first: one argument is PATH, two are NAME PATH.
    if len(path) > 2:
        raise click.UsageError("files add takes PATH, or NAME PATH")
    result = catalog_file(
        _catalog_edit_root(repository_root),
        name=path[0] if len(path) == 2 else None,
        path=Path(path[-1]),
        document_id=document_id,
        document_kind=document_kind,
        title=title,
        media_type=media_type,
        governance_scope=governance_scope,
        required_tier=cast(Any, required_tier),
        public_uri=public_uri,
        replace=replace,
    )
    _echo_catalog_edit(result, output_json=output_json)


@files_group.command("remove")
@click.argument("name")
@click.option(
    "--root",
    "repository_root",
    default=None,
    help="Workspace root the catalog belongs to (default: this workspace folder).",
)
@json_option
@handle_errors
def remove_file(name: str, repository_root: str | None, output_json: bool) -> None:
    """Remove NAME's entry from the workspace's .cruxible/files.yaml.

    Evidence already cited under NAME keeps its retained passage; new
    citations of the file stop resolving. Writes only the catalog file.
    """

    result = remove_catalog_entry(_catalog_edit_root(repository_root), name=name)
    _echo_catalog_edit(result, output_json=output_json)


@files_group.command("propose")
@click.option(
    "--bundle",
    "bundle_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="A bundle files compile wrote (default: compile the workspace catalog now).",
)
@click.option("--file", "file", required=True, help="The catalog file to propose.")
@click.option("--name", "proposal_name", required=True, help="The proposal's name.")
@click.option("--no-accept", is_flag=True, help="Propose only; do not land the change.")
@_file_options
@change_control_options
@json_option
@handle_errors
def propose_files(
    bundle_path: str | None,
    file: str,
    proposal_name: str,
    no_accept: bool,
    portable_catalog: str | None,
    local_catalog: str | None,
    root_alias: tuple[str, ...],
    repository_root: str | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose one catalogued file as its Document's next revision.

    Without --bundle the workspace catalog is compiled first, exactly as
    files compile does; with it, the frozen bundle is proposed as written.
    An ordinary Document proposal that lands when policy and your tier allow
    (--no-accept keeps the proposal); one that needs an approver waits for
    them. The compilation it came from is recorded with it.
    """

    if bundle_path is not None and (
        portable_catalog or local_catalog or root_alias or repository_root
    ):
        raise click.UsageError("a --bundle is proposed as written; it takes no catalog options")
    frozen = None if bundle_path is None else _read_model(bundle_path, FileCompilationBundle)
    catalog_and_root = (
        None
        if frozen is not None
        else _file_catalog_and_root(portable_catalog, local_catalog, repository_root)
    )

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        bundle = frozen
        if bundle is None:
            assert catalog_and_root is not None
            catalog, root = catalog_and_root
            bundle = _compile_remote_context(
                client,
                instance_id,
                catalog=catalog,
                repository_root=root,
                aliases=_root_aliases(root_alias),
            )
        if file not in {item.source.name for item in bundle.documents}:
            raise click.ClickException(
                f"file {file!r} compiles to no Document: it is not catalogued, or its "
                "entry is evidence-only (give it document_id, document_kind, title, "
                "media_type and governance_scope to propose it)"
            )
        return client.propose_file_bundle(
            instance_id,
            bundle=bundle.model_dump(mode="json"),
            file=file,
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
            accept="never" if no_accept else "if_allowed",
        )

    result = _server_call(call, command_name="cruxible files propose")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    admission = result.proposal.get("admission")
    proposal_id = admission.get("proposal_id") if isinstance(admission, dict) else None
    click.echo(f"{file}: {result.status}")
    if proposal_id:
        click.echo(f"Proposal: {proposal_id}")
    _echo_landing(result.outcome)
    echo_preview_next(result.status, result.accepted_coordinate)


@playbill_group.group("principal")
def principal_group() -> None:
    """List and govern ordinary/recovery public keys."""


@principal_group.command("add")
@click.argument("principal_id")
@click.option(
    "--kind",
    type=click.Choice(("ordinary", "recovery")),
    default="ordinary",
    show_default=True,
    help="Closed principal kind; daemon is instance-owned.",
)
@click.option(
    "--key-dir",
    required=True,
    help="New principal's custody directory: receives its key and its cruxible.env settings.",
)
@click.option(
    "--name", "proposal_name", default=None, help="Proposal name (default: add-PRINCIPAL_ID)."
)
@click.option(
    "--signer-key",
    default=None,
    envvar=PRINCIPAL_KEY_ENV,
    type=click.Path(dir_okay=False),
    help=(
        "Your own private key (also CRUXIBLE_PRINCIPAL_KEY). Approves and activates the "
        "registration in the same command; without it the registration is only proposed."
    ),
)
@click.option(
    "--mode",
    "permission_mode",
    type=click.Choice(("read_only", "governed_write", "graph_write", "admin")),
    default="governed_write",
    show_default=True,
    help=(
        "Tier of the bearer credential minted for the new principal when the daemon runs "
        "with auth. governed_write proposes and authors but cannot approve or activate."
    ),
)
@change_control_options
@json_option
@handle_errors
def add_principal(
    principal_id: str,
    kind: str,
    key_dir: str,
    proposal_name: str | None,
    signer_key: str | None,
    permission_mode: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Set up one principal (an agent) in one command.

    Generates the principal's key in `--key-dir`, proposes its registration,
    and, with `--signer-key` (your own key), approves and activates it. When
    the daemon runs with auth it also mints the principal's bearer credential,
    signed with the new key. Everything the agent needs -- connection settings,
    principal ID, key path, and that credential -- lands in `DIR/cruxible.env`.
    `--dry-run` evaluates the registration and writes nothing: no key, no
    settings, no proposal.
    """

    principal_kind = cast(PrincipalKind, kind)
    try:
        ref_name = canonical_proposal_ref_name(proposal_name or f"add-{principal_id}")
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--name") from exc
    custody = Path(key_dir).expanduser()
    mode = cast(contracts.RuntimeCredentialPermissionMode, permission_mode)
    if dry_run:
        preview = _preview_principal_change(
            principal_id=principal_id,
            kind=principal_kind,
            key_dir=custody,
            proposal_name=ref_name,
            at=at,
            refuse_existing=True,
        )
        _emit_principal_preview(preview, output_json=output_json)
        return

    def call(client: CruxibleClient, instance_id: str) -> _PrincipalAddOutcome:
        existing = principal_records(client, instance_id)
        if any(item.principal_id == principal_id for item in existing):
            raise click.ClickException(f"Cruxible principal already exists: {principal_id}")
        require_authoring_compatibility(client)
        material = generate_client_principal_key(
            custody,
            principal_id=principal_id,
            kind=principal_kind,
            forbidden_roots=_custody_forbidden_roots(),
        )
        proposed = client.propose_principal_change(
            instance_id,
            principal=material.principal.model_dump(mode="json"),
            proposal_name=ref_name,
            dry_run=False if dry_run is False else None,
            at=at,
        )
        outcome = _PrincipalAddOutcome(
            instance_id=instance_id,
            material=material,
            proposal=proposed,
            proposal_id=_admitted_proposal_id(proposed),
        )
        if signer_key is None or outcome.proposal_id is None:
            return outcome
        identity = client.whoami(instance_id)
        if identity.actor_id is None:
            return outcome
        outcome.signer_id = identity.actor_id
        approval = _approve_with_key(
            client,
            instance_id,
            outcome.proposal_id,
            signer_id=identity.actor_id,
            private_key_path=Path(signer_key).expanduser(),
        )
        # The actor's own signature completes a lifecycle candidate, so the
        # approval activates it unless more approvals are required.
        outcome.approval = approval if isinstance(approval, contracts.ApprovalReceipt) else None
        outcome.activated = (
            isinstance(approval, contracts.ApprovalReceipt)
            and approval.activation is not None
            and approval.activation.status == "accepted"
        )
        if outcome.activated and identity.authenticated and principal_kind == "ordinary":
            minted = client.create_runtime_credential(
                instance_id,
                principal_id=principal_id,
                permission_mode=mode,
                principal_proof=sign_runtime_credential_mint(
                    instance_id=instance_id,
                    principal_id=principal_id,
                    permission_mode=mode,
                    label=principal_id,
                    private_key_path=material.private_key_path,
                    forbidden_roots=_custody_forbidden_roots(),
                ),
            )
            outcome.credential = minted
        return outcome

    outcome = _server_call(call, command_name="cruxible principal add")
    token = None if outcome.credential is None else outcome.credential.token
    settings = write_principal_settings(
        custody,
        ctx_obj=_root_ctx_obj(),
        instance_id=outcome.instance_id,
        principal_id=principal_id,
        private_key_path=outcome.material.private_key_path,
        token=token,
        written_by="cruxible principal add",
    )
    # Known to this CLI's context, not acted as: `context use --principal` switches.
    _remember_principal_settings(outcome.instance_id, principal_id, settings, activate=False)
    next_steps = _principal_add_next_steps(outcome, principal_id, custody, permission_mode)
    if output_json:
        _emit_json(
            {
                "principal_id": principal_id,
                "status": "active" if outcome.activated else "proposed",
                "proposal_id": outcome.proposal_id,
                "private_key_path": str(outcome.material.private_key_path),
                "settings_path": str(settings),
                "credential": (
                    None
                    if outcome.credential is None
                    else outcome.credential.credential.model_dump(mode="json")
                ),
                "next_steps": next_steps,
                "proposal": outcome.proposal.model_dump(mode="json"),
            }
        )
        return
    state = "registered and active" if outcome.activated else "proposed, not yet active"
    click.echo(f"Principal {principal_id}: {state}")
    if outcome.proposal_id is not None:
        click.echo(f"Proposal: {outcome.proposal_id}")
    click.echo(f"Private key retained locally at: {outcome.material.private_key_path}")
    if outcome.credential is not None:
        credential = outcome.credential.credential
        click.echo(
            f"Bearer credential: {credential.credential_id} ({credential.permission_mode}), "
            "written to the settings file, not printed"
        )
    click.echo(f"Settings: {settings}")
    click.echo(f"The agent loads them with: set -a; . {settings}; set +a")
    for step in next_steps:
        click.echo(f"Next: {step}")


@dataclass
class _PrincipalAddOutcome:
    instance_id: str
    material: GeneratedKeyMaterial
    proposal: contracts.ProposalInspection
    proposal_id: str | None
    signer_id: str | None = None
    activated: bool = False
    credential: contracts.RuntimeCredentialResult | None = None
    approval: contracts.ApprovalReceipt | None = None


def _admitted_proposal_id(proposed: contracts.ProposalInspection) -> str | None:
    admission = proposed.proposal.get("admission")
    if isinstance(admission, Mapping) and isinstance(admission.get("proposal_id"), str):
        return str(admission["proposal_id"])
    return None


def _approve_with_key(
    client: CruxibleClient,
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    private_key_path: Path,
) -> contracts.ProposalApprovalResult:
    challenge = client.prepare_approval(instance_id, proposal_id, signer_id=signer_id)
    principal = PrincipalRecord.model_validate(challenge.signer_principal)
    signer = LocalEd25519ApprovalSigner.open(
        signer_id=signer_id,
        private_key_path=private_key_path,
        expected_public_key=principal.public_key,
        forbidden_roots=_custody_forbidden_roots(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge.statement))
    return client.submit_approval(
        instance_id, proposal_id, attestation=attestation.model_dump(mode="json")
    )


def _principal_add_next_steps(
    outcome: _PrincipalAddOutcome, principal_id: str, custody: Path, permission_mode: str
) -> list[str]:
    if outcome.activated:
        return []
    if outcome.proposal_id is None:
        return [
            "the registration proposal was not admitted; inspect it with `cruxible proposal list`"
        ]
    signer = outcome.signer_id or _SIGNER_PLACEHOLDER
    steps = []
    approval = outcome.approval
    if approval is not None and approval.not_activated == "approvals_remaining":
        # Signed, but more signatures are required: activating now would refuse.
        steps.extend(_awaiting_approve_steps(approval))
    elif outcome.signer_id is None:
        steps.append(f"cruxible proposal approve {outcome.proposal_id} --signer-id {signer}")
        steps.append(f"cruxible proposal activate {outcome.proposal_id}")
    else:
        steps.append(f"cruxible proposal activate {outcome.proposal_id}")
    steps.append(
        f"with daemon auth on: cruxible credential mint --principal-id {principal_id} "
        f"--key-dir {custody} --mode {permission_mode} (writes the token into "
        f"{PRINCIPAL_SETTINGS_FILE})"
    )
    return steps


def _preview_principal_change(
    *,
    principal_id: str,
    kind: PrincipalKind | None,
    key_dir: Path,
    proposal_name: str,
    at: str | None,
    refuse_existing: bool,
) -> contracts.ProposalInspection:
    """Preview a principal change whose key would be generated: nothing is written.

    The record carries a throwaway in-memory public key; the change set, the
    principal-lifecycle law and the approval it needs are exactly the commit's.
    """

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        matches = principal_records(client, instance_id)
        target = next((item for item in matches if item.principal_id == principal_id), None)
        if refuse_existing and target is not None:
            raise click.ClickException(f"Cruxible principal already exists: {principal_id}")
        if not refuse_existing and target is None:
            raise click.ClickException(f"Unknown Cruxible principal: {principal_id}")
        principal_kind = kind if target is None else target.kind
        assert principal_kind is not None
        record = preview_client_principal(
            key_dir,
            principal_id=principal_id,
            kind=principal_kind,
            forbidden_roots=_custody_forbidden_roots(),
        )
        return client.propose_principal_change(
            instance_id,
            principal=record.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=True,
            at=at,
        )

    return _server_call(call, command_name="cruxible principal change")


def _emit_principal_preview(preview: contracts.ProposalInspection, *, output_json: bool) -> None:
    if output_json:
        _emit_json(preview.model_dump(mode="json"))
        return
    click.echo(f"Principal change: {preview.status}; nothing was written")
    echo_preview_next(preview.status, preview.accepted_coordinate)


def _principal_successor(
    *,
    target_id: str,
    key_dir: str,
    proposal_name: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
    command: str,
) -> None:
    """Propose a successor key for ``target_id`` and print it (``rotate`` and ``recover``).

    S5: the new key directory gets the principal's settings file, as `principal
    add` writes one: the connection, the instance, the new key and the bearer
    token of the settings file the CLI remembers for the principal. The CLI keeps
    acting with the old file until the change is active (activation is signed
    with the current key); the output names the new file to load then.
    """

    generated: list[tuple[str, GeneratedKeyMaterial]] = []

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        matches = principal_records(client, instance_id)
        target = next((item for item in matches if item.principal_id == target_id), None)
        if target is None:
            raise click.ClickException(f"Unknown Cruxible principal: {target_id}")
        require_authoring_compatibility(client)
        material = generate_client_principal_key(
            Path(key_dir).expanduser(),
            principal_id=target_id,
            kind=target.kind,
            forbidden_roots=_custody_forbidden_roots(),
        )
        generated.append((instance_id, material))
        return client.propose_principal_change(
            instance_id,
            principal=material.principal.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        )

    result = (
        _preview_principal_change(
            principal_id=target_id,
            kind=None,
            key_dir=Path(key_dir).expanduser(),
            proposal_name=proposal_name,
            at=at,
            refuse_existing=False,
        )
        if dry_run
        else _server_call(call, command_name="cruxible principal change")
    )
    settings = (
        _successor_settings(target_id, Path(key_dir).expanduser(), *generated[0], command=command)
        if generated
        else None
    )
    if output_json:
        payload = result.model_dump(mode="json")
        if settings is not None:
            payload["settings_path"] = str(settings)
        _emit_json(payload)
        return
    _echo_proposal(result, result.lint, principal_change=True)
    if settings is not None:
        click.echo(f"Settings for the new key: {settings}")
        click.echo(f"Once the change is active, load them with: set -a; . {settings}; set +a")


def _successor_settings(
    principal_id: str,
    key_dir: Path,
    instance_id: str,
    material: GeneratedKeyMaterial,
    *,
    command: str,
) -> Path:
    """Write the settings file beside a successor key, carrying the remembered token."""

    from cruxible_client.authoring.principal_settings import read_principal_settings
    from cruxible_core.cli.context import principal_binding

    token: str | None = None
    transport = _transport_target(_root_ctx_obj())
    if transport is not None:
        known = load_cli_context().principals.get(principal_binding(transport, instance_id))
        remembered = None if known is None else known.settings.get(principal_id)
        if remembered is not None:
            try:
                token = read_principal_settings(Path(remembered)).token
            except (OSError, ValueError):
                token = None
    return write_principal_settings(
        key_dir,
        ctx_obj=_root_ctx_obj(),
        instance_id=instance_id,
        principal_id=principal_id,
        private_key_path=material.private_key_path,
        token=token,
        written_by=command,
    )


@principal_group.command("rotate")
@click.argument("principal_id")
@click.option("--key-dir", required=True)
@click.option("--name", "proposal_name", required=True)
@change_control_options
@json_option
@handle_errors
def rotate_principal(
    principal_id: str,
    key_dir: str,
    proposal_name: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose a new key for PRINCIPAL_ID; activation needs your own key's signature.

    What the change records depends on who is acting: when you are PRINCIPAL_ID
    it is a rotation; when another principal proposes it, a recovery.
    `principal recover` proposes the same change.
    """

    _principal_successor(
        target_id=principal_id,
        key_dir=key_dir,
        proposal_name=proposal_name,
        dry_run=dry_run,
        at=at,
        output_json=output_json,
        command="cruxible principal rotate",
    )


@principal_group.command("recover")
@click.argument("principal_id")
@click.option("--key-dir", required=True)
@click.option("--name", "proposal_name", required=True)
@change_control_options
@json_option
@handle_errors
def recover_principal(
    principal_id: str,
    key_dir: str,
    proposal_name: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose a new key for a locked-out or revoked PRINCIPAL_ID, acting as yourself.

    What the change records depends on who is acting: when another principal
    proposes it, it is a recovery; when you are PRINCIPAL_ID, a rotation. The
    signature that activates it is yours, never the locked-out principal's.
    `principal rotate` proposes the same change.
    """

    _principal_successor(
        target_id=principal_id,
        key_dir=key_dir,
        proposal_name=proposal_name,
        dry_run=dry_run,
        at=at,
        output_json=output_json,
        command="cruxible principal recover",
    )


@principal_group.command("revoke")
@click.argument("principal_id")
@click.option("--name", "proposal_name", required=True)
@change_control_options
@json_option
@handle_errors
def revoke_principal(
    principal_id: str, proposal_name: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Propose revoking a principal (and, once active, every credential that acts as it)."""

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        matches = principal_records(client, instance_id)
        target = next((item for item in matches if item.principal_id == principal_id), None)
        if target is None:
            raise click.ClickException(f"Unknown Cruxible principal: {principal_id}")
        revoked = target.model_copy(update={"status": "revoked"})
        return client.propose_principal_change(
            instance_id,
            principal=revoked.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        )

    result = _server_call(call, command_name="cruxible principal revoke")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_proposal(result, result.lint, principal_change=True)


@playbill_group.group("claim-type")
def claim_type_group() -> None:
    """Propose and read the governed predicate vocabulary."""


_CLAIM_TYPE_INVERSE_HINT = (
    'Optional: "inverse": {"key": {"kind": "slot"}, "evidence_rules": ["RULE_ID"]} records '
    "absence (a none) on Captures one of those rules admits; a many-valued type keys it "
    '{"kind": "object"} or {"kind": "object_key", "predicate": KEY}; "stale_after": "P7D" '
    "overrides its freshness."
)


@playbill_group.group("capture-contract")
def capture_contract_group() -> None:
    """Author the shape and policies of captured evidence."""


@capture_contract_group.command("propose")
@click.option(
    "--input",
    "input_path",
    type=PayloadFile(),
    help="CaptureContractInputRecord: name and shape; edits state only what changes.",
)
@click.option(
    "--template", is_flag=True, help="Print a minimal input without contacting the daemon."
)
@click.option("--expanded", is_flag=True, help="With --template, include the defaults.")
@click.option("--name", "proposal_name")
@click.option("--no-accept", is_flag=True, help="Propose only; do not land the change.")
@change_control_options
@json_option
@handle_errors
def propose_capture_contract(
    input_path: str | None,
    template: bool,
    expanded: bool,
    proposal_name: str | None,
    no_accept: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose one CaptureContract; it lands when policy and your tier allow."""
    if (input_path is None) == (not template):
        raise click.UsageError("provide exactly one of --input or --template")
    if expanded and not template:
        raise click.UsageError("--expanded applies only to --template")
    if template:
        _emit_json(capture_contract_input_template(expanded=expanded))
        return
    assert input_path is not None
    record = CaptureContractInputRecord.model_validate(_read_mapping(input_path))
    result = _server_call(
        lambda client, instance_id: client.propose_capture_contract_input(
            instance_id,
            input=record.model_dump(mode="json", exclude_unset=True),
            proposal_name=proposal_name or record.name,
            dry_run=dry_run,
            at=at,
            accept="never" if no_accept else "if_allowed",
        ),
        command_name="cruxible capture-contract propose",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_proposal(result, None)


@claim_type_group.command("propose")
@click.option(
    "--input",
    "input_path",
    type=PayloadFile(),
    help=(
        "A ClaimTypeInputRecord. A new ClaimType states predicate and cardinality. "
        "allowed_subject_kinds defaults to the predicate prefix before its last dot. "
        "value declares a native Contract field type (default string{max_len: 256}); "
        "Subject links use subject<K1 | K2>. Prose lives in Documents. "
        "String values default canonical_form to [nfc, trim]. "
        "permitted_roles defaults to [default_role] or [normative]; exactly [observation] "
        "defaults evidence_requirement to captured and needs evidence: [CONTRACT, ...]. "
        "Other roles default to self: one rule admitting own words and workspace file and "
        "section citations; evidence: [CONTRACT, ...] adds the shaped contracts. No "
        "admission requirements; supported, current resolution. An edit states only what "
        "changes: an omitted field keeps the accepted value. State canonical_form [] for "
        "no transforms (also needed for a non-string field that keys a SubjectKind). "
        "State inverse (key, evidence_rules, "
        "optional stale_after) for a type that records absence: a none. Durations take "
        "ISO-8601 (P30D)."
    ),
)
@click.option(
    "--template",
    is_flag=True,
    help="Print a minimal ClaimTypeInputRecord without contacting the daemon (stderr names "
    "the optional inverse field).",
)
@click.option(
    "--expanded",
    is_flag=True,
    help="With --template: print every default the minimal record takes.",
)
@click.option(
    "--builtin",
    type=click.Choice(sorted(BUILTIN_CLAIM_TYPES)),
    help="Adopt a built-in ClaimType this instance does not hold yet (new instances start "
    "with them).",
)
@click.option("--name", "proposal_name")
@click.option("--no-accept", is_flag=True, help="Propose only; do not land the change.")
@change_control_options
@json_option
@handle_errors
def propose_claim_type(
    input_path: str | None,
    template: bool,
    expanded: bool,
    builtin: str | None,
    proposal_name: str | None,
    no_accept: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose one ClaimType; it lands when policy and your tier allow.

    An edit of a ClaimType that live Claims use carries them to the new
    version. --no-accept keeps the proposal for someone to activate.
    """

    if sum((input_path is not None, template, builtin is not None)) != 1:
        raise click.UsageError("provide exactly one of --input, --template or --builtin")
    if expanded and not template:
        raise click.UsageError("--expanded applies only to --template")
    accept: AuthoringAccept = "never" if no_accept else "if_allowed"
    if template:
        _emit_json(claim_type_input_template(expanded=expanded).model_dump(mode="json"))
        click.echo(_CLAIM_TYPE_INVERSE_HINT, err=True)
        return
    if builtin is not None:
        builtin_result = _server_call(
            lambda client, instance_id: client.propose_builtin_claim_type(
                instance_id,
                builtin=cast(BuiltinClaimTypeName, builtin),
                proposal_name=proposal_name or f"builtin-{builtin.replace('.', '-')}",
                dry_run=dry_run,
                at=at,
                accept=accept,
            ),
            command_name="cruxible claim-type propose",
        )
        if output_json:
            _emit_json(builtin_result.model_dump(mode="json"))
            return
        _echo_proposal(builtin_result.proposal, builtin_result.lint)
        return
    assert input_path is not None
    try:
        claim_type_input = ClaimTypeInputRecord.model_validate(_read_mapping(input_path))
    except ValidationError as exc:
        raise click.ClickException(
            "Invalid ClaimType input: "
            + "; ".join(
                f"{_validation_path(tuple(item['loc']))}: {item['msg']}"
                for item in exc.errors(include_url=False)
            )
            + ". `cruxible claim-type propose --template` prints a minimal record"
        ) from exc
    input_result = _server_call(
        lambda client, instance_id: client.propose_claim_type_input(
            instance_id,
            input=claim_type_input.model_dump(mode="json"),
            proposal_name=proposal_name or claim_type_input.predicate,
            dry_run=dry_run,
            at=at,
            accept=accept,
        ),
        command_name="cruxible claim-type propose",
    )
    if output_json:
        _emit_json(input_result.model_dump(mode="json"))
        return
    _echo_proposal(input_result.proposal, input_result.lint)


def _claim_type_lint_meaning(code: str) -> str:
    """``: what it means`` for a lint warning that has words, else nothing."""

    from cruxible_client.contracts.claim_type_inputs import CLAIM_TYPE_LINT_TEXT

    meaning = CLAIM_TYPE_LINT_TEXT.get(code)
    return "" if meaning is None else f": {meaning}"


def _echo_proposal(
    inspection: contracts.ProposalInspection,
    lint: contracts.ClaimTypeProposalLint | None,
    *,
    principal_change: bool = False,
) -> None:
    """A proposal in a few lines; ``--json`` carries the whole record.

    A principal change never lands on submit: it needs its proposer's own
    signature, so its next line is that approval.
    """

    admission = inspection.proposal.get("admission") or {}
    evaluation = inspection.proposal.get("evaluation") or {}
    proposal_id = admission.get("proposal_id") if isinstance(admission, Mapping) else None
    verdict = evaluation.get("verdict", "-") if isinstance(evaluation, Mapping) else "-"
    click.echo(f"{inspection.status}  {proposal_id or '-'}  verdict={verdict}")
    diagnostics = evaluation.get("diagnostics", ()) if isinstance(evaluation, Mapping) else ()
    for item in diagnostics:
        if isinstance(item, Mapping):
            click.echo(f"  {item.get('code')}: {printable(str(item.get('message', '')))}")
    for warning in () if lint is None else lint.warnings:
        contract = (
            f" (contract {_public_name(warning.contract_identity)})"
            if warning.contract_identity and warning.contract_identity != "unresolved"
            else ""
        )
        click.echo(
            f"lint: {warning.code} at {warning.field_path}{contract}"
            + _claim_type_lint_meaning(warning.code)
        )
    actor = admission.get("actor_id") if isinstance(admission, Mapping) else None
    if inspection.outcome is not None:
        _echo_landing(inspection.outcome, inspection.warnings)
    elif (
        proposal_id
        and principal_change
        and verdict == "candidate"
        and isinstance(actor, str)
        and not inspection.status.startswith("would")
    ):
        click.echo(
            f"next: cruxible proposal approve {proposal_id} --signer-id {actor} "
            "(a principal change needs its proposer's own signature)"
        )
    elif proposal_id:
        click.echo(f"next: cruxible get {proposal_id}")
    echo_preview_next(inspection.status, inspection.accepted_coordinate)


def _echo_landing(outcome: contracts.AuthoringLanding | None, warnings: Sequence[str] = ()) -> None:
    """Whether a direct propose landed its change, or why it stopped and what to run.

    ``warnings`` (S7): each kit-bundled definition the change retires.
    """

    for warning in warnings:
        click.echo(f"warning: {printable(warning)}", err=True)
    if outcome is None:
        return
    for line in landing_lines(outcome):
        click.echo(line)


@claim_type_group.command("retire")
@click.argument("predicates", nargs=-1, required=True, metavar="PREDICATE...")
@click.option("--because", required=True, help="Why the field is retired; the change's rationale.")
@click.option(
    "--reason",
    type=click.Choice(["superseded", "was-wrong", "was-rescinded"]),
    default="superseded",
    show_default=True,
    help="The retirement reason each live Claim of the field takes.",
)
@click.option("--dry-run", is_flag=True, help="Preflight the change set; save nothing.")
@click.option("--no-accept", is_flag=True, help="Propose only; do not land the change.")
@json_option
@handle_errors
def retire_claim_type(
    predicates: tuple[str, ...],
    because: str,
    reason: str,
    dry_run: bool,
    no_accept: bool,
    output_json: bool,
) -> None:
    """Retire one or more fields (ClaimTypes) as one change set.

    Each field's live Claims retire with it (--reason); it lands when policy
    and your tier allow, like any change set. The SDK spelling is
    ``cx.changes(rationale=...).retire_claim_type(predicate)``.
    """

    from cruxible_client.authoring.inputs import ChangeSetInput, ClaimTypeSuccessionInput
    from cruxible_client.contracts.claim_types import ClaimType, retired_successor
    from cruxible_client.contracts.get_reads import GetRequest

    def call(client: CruxibleClient, instance_id: str) -> Any:
        members = []
        for predicate in sorted(set(predicates)):
            proof = client.get(
                instance_id, request=GetRequest(ref=f"ClaimType:{predicate}", detail="proof")
            ).proof
            assert proof is not None
            members.append(
                ClaimTypeSuccessionInput(
                    kind="claim_type_succession",
                    successor=retired_successor(ClaimType.model_validate(proof["envelope"])),
                    retire_dependents=cast(Any, reason),
                )
            )
        payload = ChangeSetInput(
            kind="change_set", members=tuple(members), rationale=because
        ).model_dump(mode="json")
        if dry_run:
            return client.preview_authoring_input(instance_id, input=payload)
        return client.submit_authoring_input(
            instance_id, input=payload, accept="never" if no_accept else "if_allowed"
        )

    result = _server_call(call, command_name="cruxible claim-type retire")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if isinstance(result, contracts.AuthoringPreflightResult):
        click.echo(f"would {'pass' if result.verdict == 'passed' else 'refuse'}")
        for item in result.frontier.diagnostics:
            click.echo(f"  {item.code}: {printable(item.message)}")
        return
    reason_line = _submit_refusal_reason(result)
    if reason_line is not None:
        click.echo(f"refused: {reason_line}")
        return
    _echo_landing(result.outcome, result.warnings)


@claim_type_group.command("migrate")
@click.argument("request_file", type=PayloadFile())
@click.option("--no-accept", is_flag=True, help="Propose only; do not land the change.")
@json_option
@handle_errors
def migrate_claim_type(request_file: str, no_accept: bool, output_json: bool) -> None:
    """Propose one ClaimType successor and every dependent disposition atomically.

    A submitted migration lands when policy and your tier allow, like a change
    set; --no-accept (or "accept": "never" in the request) keeps the proposal.
    To retire fields, ``cruxible claim-type retire`` needs no request file.
    """

    try:
        request = _CLAIM_TYPE_MIGRATION_ADAPTER.validate_python(_read_mapping(request_file))
    except ValidationError as exc:
        raise click.ClickException(
            f"Invalid ClaimType migration: {validation_summary(exc)}"
        ) from exc
    if no_accept:
        request = request.model_copy(update={"accept": "never"})
    result = _server_call(
        lambda client, instance_id: client.migrate_claim_type(
            instance_id,
            request=request.model_dump(mode="json"),
        ),
        command_name="cruxible claim-type migrate",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if result.tag == "playbill-claim-type-migration-preflight-v1":
        click.echo("ClaimType migration preflight")
        click.echo(f"Coordinate: {result.coordinate.git_oid}")
        click.echo(f"Successor: {result.successor_artifact_digest}")
        click.echo(f"Blast radius: {len(result.dependents)} dependent(s)")
        for dependent in result.dependents:
            identity = dependent.get("identity", {})
            identity_name = identity.get("name", dependent.get("claim_id", "<unknown>"))
            click.echo(
                f"  {identity.get('kind', 'Claim')}:{identity_name}"
                f" ({dependent.get('artifact_kind', 'claim')})"
            )
    else:
        click.echo("ClaimType migration proposal")
        click.echo(f"Operation: {result.operation_digest}")
        click.echo(f"Dependents: {len(result.dependents)}")
        admission = result.proposal.proposal.get("admission", {})
        proposal_id = admission.get("proposal_id", "<submitted>")
        click.echo(f"Proposal: {proposal_id}")
        if result.proposal.outcome is not None:
            _echo_landing(result.proposal.outcome, result.proposal.warnings)
        else:
            click.echo(f"Next: cruxible proposal approve {proposal_id}")
    click.echo("Semantic delta:")
    if not result.semantic_delta:
        click.echo("  (no semantic field changes)")
    for row in result.semantic_delta:
        before = (
            "<absent>"
            if row.before.state == "absent"
            else json.dumps(row.before.value, sort_keys=True, ensure_ascii=False)
        )
        after = (
            "<absent>"
            if row.after.state == "absent"
            else json.dumps(row.after.value, sort_keys=True, ensure_ascii=False)
        )
        click.echo(f"  {row.field_path or '/'}: {before} -> {after}")
    click.echo("Lint:")
    if result.lint is None or not result.lint.warnings:
        click.echo("  none")
    else:
        for warning in result.lint.warnings:
            click.echo(
                f"  {warning.field_path}: {warning.code}" + _claim_type_lint_meaning(warning.code)
            )


@playbill_group.group("claim")
def claim_group() -> None:
    """Propose, read, and explain first-class governed Claims."""


@playbill_group.group("prediction")
def prediction_group() -> None:
    """Propose, settle and list predictions: testable claims about a later observation.

    A prediction is stored as a ResolutionContract (`get ResolutionContract:NAME`
    reads one); `next` names each bound window that is ready to settle.
    """


@prediction_group.command("list")
@click.argument("claim")
@json_option
@handle_errors
def list_predictions(claim: str, output_json: bool) -> None:
    """List the accepted predictions that test one Claim (CLM-... or Claim:CLM-...).

    The daemon resolves the Claim's current accepted version and returns the
    accepted resolution contracts whose hypothesis is that exact version, retired
    ones included; contracts testing an earlier version of the Claim are not
    listed. A pending (unaccepted) prediction is a proposal: `cruxible proposal list`
    names it and `cruxible get PROPOSAL_ID` reads it. An accepted one and its
    settlement are read with `cruxible get ResolutionContract:NAME` and `cruxible next`.
    """

    request = contracts.ResolutionContractsRequest(hypothesis=claim)
    result = _server_call(
        lambda client, instance_id: client.list_predictions(instance_id, request=request),
        command_name="cruxible prediction list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if not result.contracts:
        click.echo("No accepted prediction tests this Claim version.")
        return
    for view in result.contracts:
        click.echo(
            f"{view.reference.identity.qualified} {view.reference.artifact_digest} "
            f"{view.contract.lifecycle.state}"
        )


@prediction_group.command("propose")
@click.argument("request_file", type=PayloadFile())
@json_option
@handle_errors
def propose_prediction(request_file: str, output_json: bool) -> None:
    """Propose a prediction about an accepted Claim from a PredictRequest file.

    The prediction becomes a governed resolution contract proposal; review,
    approve and activate it like any other, then settle it with `prediction
    settle` once a later observation is accepted.
    """

    try:
        request = contracts.PredictRequest.model_validate(_read_mapping(request_file))
    except ValidationError as exc:
        raise click.ClickException(
            f"Invalid prediction request: {validation_summary(exc)}"
        ) from exc
    result = _server_call(
        lambda client, instance_id: client.predict(instance_id, request=request),
        command_name="cruxible prediction propose",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Contract: {result.contract_identity}")
    click.echo(f"Proposal: {result.proposal_id}")


@prediction_group.command("settle")
@click.argument("prediction_id")
@click.option(
    "--observation",
    "observation",
    help="Claim ID (CLM-...) of the accepted observation that settles the prediction.",
)
@click.option(
    "--request",
    "request_file",
    type=PayloadFile(),
    help=(
        "Advanced: a SettleRequest file (exact contract reference, anchor event, "
        "or terminal evidence)."
    ),
)
@json_option
@handle_errors
def settle_prediction(
    prediction_id: str,
    observation: str | None,
    request_file: str | None,
    output_json: bool,
) -> None:
    """Settle one prediction from a later accepted observation.

    PREDICTION_ID is the contract name or the bound window id (RSC-...) that
    `cruxible next` names; the daemon resolves the exact contract, window and
    observation version from it and `--observation CLM-...`.
    """

    if (observation is None) == (request_file is None):
        raise click.UsageError(
            "provide --observation CLAIM_ID (the accepted observation that settles it) "
            "or --request FILE"
        )
    try:
        request = (
            contracts.SettleRequest.model_validate(_read_mapping(request_file))
            if request_file is not None
            else contracts.SettleRequest(observation=observation)
        )
    except ValidationError as exc:
        raise click.ClickException(
            f"Invalid settlement request: {validation_summary(exc)}"
        ) from exc
    result = _server_call(
        lambda client, instance_id: client.settle_prediction(
            instance_id,
            prediction_id,
            request=request,
        ),
        command_name="cruxible prediction settle",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Prediction {result.prediction_id}: settled")
    click.echo(f"Outcome: {result.resolution['settlement_outcome']}")


@playbill_group.group("action")
def action_group() -> None:
    """Fire a registered internal action on demand, outside any Trigger's schedule."""


@action_group.command("run")
@click.argument("action_name", metavar="NAME")
@click.option(
    "--wait",
    is_flag=True,
    help="Wait for the owning part's own progress to show this fire processed.",
)
@click.option(
    "--wait-timeout",
    "wait_timeout_seconds",
    type=float,
    default=60.0,
    show_default=True,
    help="Bound in seconds on --wait.",
)
@json_option
@handle_errors
def run_action(
    action_name: str, wait: bool, wait_timeout_seconds: float, output_json: bool
) -> None:
    """Fire one registered internal action (NAME, e.g. evidence.sweep) by hand.

    The fire lands in the same trigger journal a Trigger's own schedule
    writes to, so the daemon's normal consumer runner picks it up and runs
    it exactly as a scheduled fire; NAME need not have any Trigger aimed at
    it. Without --wait this only records the fire for the daemon's own next
    pass; --wait additionally waits, bounded by --wait-timeout, until the
    owning part's own recorded progress shows this fire processed -- it
    starts and runs no work itself (see the result's outcome).
    """

    request = contracts.ActionRunRequest(
        action=action_name, wait=wait, wait_timeout_seconds=wait_timeout_seconds
    )
    result = _server_call(
        lambda client, instance_id: client.run_internal_action(instance_id, request=request),
        command_name="cruxible action run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Fired {result.action} (event {result.event_id})")
    click.echo(f"Outcome: {result.outcome.status}")
    if result.outcome.detail:
        click.echo(f"Detail: {result.outcome.detail}")


@claim_group.command("attest")
@click.argument("claim_id")
@click.option("--support", is_flag=True)
@click.option("--contradict", is_flag=True)
@click.option("--unsure", is_flag=True)
@click.option(
    "--because",
    default=None,
    help="Your own words; cited as a self-source Capture.",
)
@click.option(
    "--evidence-file",
    default=None,
    help=(
        "PATH#ANCHOR: cite text found once in a workspace file; the write catalogues "
        "the file if it is not yet."
    ),
)
@click.option(
    "--section",
    "section_ref",
    default=None,
    help="FILE#Heading/Path: cite a catalogued file's section (FILE# for the preamble).",
)
@click.option(
    "--own-words",
    is_flag=True,
    help="With --section: the section's text is your own words, not observed evidence.",
)
@click.option(
    "--capture",
    default=None,
    help="Cite an existing Capture: its handle CAP-<12+ hex>, or its sha256 digest.",
)
@click.option(
    "--evidence-contract",
    default=None,
    help="Cite the newest verified Capture of this CaptureContract about the Claim's Subject.",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog --evidence-file and --section read.",
)
@click.option(
    "--valid-until",
    default=None,
    help=(
        "ISO-8601 end of this attestation. An --unsure hold on stale or uncovered "
        "evidence otherwise lapses after the ClaimType's unsure_hold_for (default 30 days)."
    ),
)
@json_option
@handle_errors
def attest_claim(
    claim_id: str,
    support: bool,
    contradict: bool,
    unsure: bool,
    because: str | None,
    evidence_file: str | None,
    section_ref: str | None,
    own_words: bool,
    capture: str | None,
    evidence_contract: str | None,
    workspace_root: str,
    valid_until: str | None,
    output_json: bool,
) -> None:
    """Sign that this caller examined the current exact Claim.

    With no evidence option it cites the Claim's own evidence. --because cites
    your own words; --evidence-file, --section (with --own-words), --capture or
    --evidence-contract cite evidence the way `cruxible set` does. It stays a
    signed append, never a proposal. The attestation is shown on the Claim (get
    --detail evidence) and counts toward the verdict only through the
    ClaimType's consequence policy; with none, a standing --contradict is
    resurfaced in `next` for an agent to judge. --unsure holds the Claim's
    contested `next` rows until what you examined changes. --json prints the
    structured append result.
    """

    from cruxible_client.authoring.attestations import attest_claim as attest
    from cruxible_client.authoring.write_evidence import rebind_evidence

    selected = tuple(
        value
        for enabled, value in (
            (support, "support"),
            (contradict, "contradict"),
            (unsure, "unsure"),
        )
        if enabled
    )
    if len(selected) != 1:
        raise click.UsageError("choose exactly one of --support, --contradict, or --unsure")
    given = [
        flag
        for flag, value in (
            ("--evidence-file", evidence_file),
            ("--section", section_ref),
            ("--capture", capture),
            ("--evidence-contract", evidence_contract),
        )
        if value is not None
    ]
    if len(given) > 1:
        raise click.UsageError(f"pass one of {', '.join(given)}, not several")
    if own_words and section_ref is None:
        raise click.UsageError("--own-words applies to --section")
    evidence: Any = None
    try:
        if evidence_contract is not None:
            evidence = ContractEvidence(contract=evidence_contract)
        elif given:
            evidence = rebind_evidence(
                file=evidence_file,
                section=section_ref,
                capture=capture,
                workspace=Path(workspace_root),
            )
            if own_words:
                evidence = evidence.model_copy(update={"own_words": True})
    except (ValidationError, ValueError) as exc:
        raise click.UsageError(str(exc)) from None
    stance = selected[0]
    claim = claim_id.removeprefix("Claim:")
    attested_at = datetime.now(UTC)

    def call(client: CruxibleClient, instance_id: str):  # type: ignore[no-untyped-def]
        signer = local_attestation_signer_from_environment(
            client,
            instance_id,
            workspace_root=_custody_workspace_root(),
        )
        appended = attest(
            client,
            instance_id,
            claim_id=claim,
            stance=cast(ClaimStance, stance),
            signer=signer,
            attested_at=attested_at,
            because=because,
            evidence=evidence,
            workspace=Path(workspace_root),
            valid_until=(
                None
                if valid_until is None
                else datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
            ),
        )
        if output_json:
            return appended, None
        return appended, _attestation_shown(client, instance_id, claim, appended, attested_at)

    result, shown = _server_call(call, command_name="cruxible claim attest")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(
        f"Recorded {stance} on {claim} at {result.recorded_at.isoformat()} "
        f"(event {result.event_digest})"
    )
    if isinstance(shown, str):
        click.echo(f"counts toward verdict: unknown ({shown})")
    else:
        click.echo(f"counts toward verdict: {_attestation_counts_text(shown)}")


def _attestation_shown(
    client: Any, instance_id: str, claim: str, appended: Any, attested_at: datetime
) -> Any:
    """The Claim's own account of the event just recorded, or why it could not be read."""

    from cruxible_client.contracts.get_reads import GetRequest

    try:
        read = client.get(
            instance_id,
            request=GetRequest(
                ref=claim,
                detail="evidence",
                surface="cli",
                # Read at the later of the two clocks, so the new word is current.
                evaluation_time=max(attested_at, appended.recorded_at),
            ),
        )
    except CoreError as exc:
        return printable(str(exc))
    evidence = getattr(read, "evidence", None)
    for item in () if evidence is None else evidence.attestations:
        if item.event == appended.event_digest:
            return item
    return "the Claim's evidence does not list the event"


@playbill_group.group("authoring")
def authoring_group() -> None:
    """Author definitions as governed changes: a payload in, a proposal out.

    \b
    One call:    authoring submit PAYLOAD        (compile, preflight and submit)
    Preview:     authoring submit PAYLOAD --dry-run
    Staged work: authoring compile PAYLOAD, then compile PAYLOAD --intent-id ID
                 as often as needed, and submit --intent-id ID (an intent
                 the head moved past advances when it checks clean there)
    Templates:   authoring example [NAME]
    Find work:   authoring list, authoring get ID, authoring status ID

    PAYLOAD is a tagless authoring input file, or - for stdin. Value changes
    (set, add, retire, write) take the shorter value lane instead.
    """


@authoring_group.command("example")
@click.argument(
    "name", required=False, metavar="[NAME]", type=click.Choice(AUTHORING_EXAMPLE_NAMES)
)
@click.option(
    "--attestation-claim-id",
    help="Claim ID an attestation-door example revises (with --capture-digest).",
)
@click.option(
    "--capture-digest",
    help="Capture digest an attestation-door example cites (with --attestation-claim-id).",
)
@handle_errors
def authoring_example_command(
    name: str | None,
    attestation_claim_id: str | None,
    capture_digest: str | None,
) -> None:
    """Print one model-generated authoring payload template, or list the names.

    \b
    With NAME, prints that example as one JSON document on stdout (any note goes
    to stderr), ready to edit and pass to `authoring compile -` or
    `authoring submit -`. Without NAME, lists every example name. Nothing is sent
    to the daemon.

    \b
    Input kind family: claim | procedure |
    subject | subject_kind | subject_succession | query_definition |
    approval_policy | procedure_runtime_policy | procedure_mandate |
    line | trigger | runbook | contract | capability | change_set (tagless).

    \b
    Change-set member kind family: claim | claim_type | claim_type_succession |
    claim_retirement | subject | subject_kind | subject_succession |
    query_definition | procedure_mandate | line | trigger | runbook | contract |
    capability | procedure.
    claim_type, claim_type_succession and
    claim_retirement are member kinds only -- none is a top-level input.
    approval_policy and procedure_runtime_policy are the reverse: the member
    union parses either, but a change set refuses either, so author each as its
    own singleton input.

    \b
    One payload is one changeset: a change_set input carries any mix of those
    member kinds, lowers once, and admits or refuses whole, typed to the member
    index that offends. `change-set` prints a mixed set and
    `claim-type-succession` a vocabulary evolution; `procedure`, `line`,
    `trigger` and `procedure-mandate` are accepted together as members of one
    change set.
    """

    if name is None:
        if attestation_claim_id is not None or capture_digest is not None:
            raise click.UsageError("--attestation-claim-id/--capture-digest require NAME")
        for example_name in AUTHORING_EXAMPLE_NAMES:
            click.echo(example_name)
        return
    try:
        example = authoring_example(
            cast(AuthoringExampleName, name),
            claim_id=attestation_claim_id,
            capture_digest=capture_digest,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    click.echo(
        json.dumps(
            example.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    note = authoring_example_note(cast(AuthoringExampleName, name))
    if note is not None:
        # Beside the payload, never in it: stdout stays one JSON document.
        click.echo(f"# {note}", err=True)


@authoring_group.command("get")
@click.argument("intent_id")
@handle_errors
def get_authoring_intent(intent_id: str) -> None:
    """Read one of your authoring intents: payload, base, last preflight and status.

    With `authoring list`, this is how staged work is picked up again after the
    conversation that started it is gone.
    """

    result = _server_call(
        lambda client, instance_id: client.get_authoring_intent(instance_id, intent_id),
        command_name="cruxible authoring get",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("list")
@handle_errors
def list_pending_authoring_intents() -> None:
    """List your authoring intents that are still in progress, newest state each."""

    result = _server_call(
        lambda client, instance_id: client.list_pending_authoring_intents(instance_id),
        command_name="cruxible authoring list",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("compile")
@click.argument("payload", type=PayloadFile())
@click.option(
    "--intent-id",
    default=None,
    help="Revise this staged intent instead of creating one; the daemon keeps every revision.",
)
@handle_errors
def compile_authoring(payload: str, intent_id: str | None) -> None:
    """Stage PAYLOAD as an intent and run every check on it, without submitting.

    PAYLOAD is a tagless authoring input file, or - for stdin (`authoring
    example` prints templates). Without --intent-id, compile creates a new
    intent and reports its ID in certificate.intent_id; with --intent-id, the
    payload replaces that intent's payload. The result is the complete
    preflight: verdict, certificate and every diagnostic. A refused preflight
    also names its diagnostics on stderr and exits 1, as submit does. Nothing is
    proposed until `authoring submit --intent-id ID`; for one-shot work,
    `authoring submit PAYLOAD` compiles and submits in one call.
    """

    # Parsed before any daemon call: a malformed payload names its field path
    # and the matching example without needing a reachable daemon.
    parsed_input = _read_authoring_input(payload)
    result = _server_call(
        lambda client, instance_id: client.compile_authoring_input(
            instance_id,
            input=parsed_input.model_dump(mode="json"),
            intent_id=intent_id,
        ),
        command_name="cruxible authoring compile",
    )
    _emit_json(result.model_dump(mode="json"))
    _exit_if_preflight_refused(result)


@authoring_group.command("bind")
@click.option("--file", "source_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--anchor", required=True)
@click.option("--window-lines", type=click.IntRange(min=0), default=None)
@click.option(
    "--occurrence",
    type=click.IntRange(min=1),
    default=None,
    help="Select the 1-based anchor occurrence when the anchor is not unique.",
)
@click.option(
    "--payload-file",
    required=True,
    type=PayloadFile(),
    help="Claim stub whose source holds only the working tag and the catalog source_id.",
)
@click.option(
    "--workspace-root",
    default=None,
    type=click.Path(file_okay=False),
    help="The workspace whose file catalog names --file (default: this worktree).",
)
@handle_errors
def bind_authoring_selection(
    source_path: str,
    anchor: str,
    window_lines: int | None,
    occurrence: int | None,
    payload_file: str,
    workspace_root: str | None,
) -> None:
    """Bind a Claim to one exact anchor in a local file, then compile it.

    The payload file is a Claim stub whose source names only the working tag and
    a logical source_id, which must be the name the workspace's file catalog
    gives --file. Bind reads --file, finds --anchor (--occurrence picks one of
    several matches, --window-lines widens the cited window), derives the exact
    observation and compiles the Claim as a new staged intent.
    """

    if workspace_root is not None:
        workspace = Path(workspace_root).expanduser().resolve()
    else:
        resolution = _local_workspace_root()
        _emit_git_workspace_note(resolution)
        workspace = resolution.workspace_root or Path.cwd().resolve()
    parsed_input = _read_authoring_input(payload_file)
    if not isinstance(parsed_input, ClaimInput):
        raise click.ClickException("authoring bind accepts only a claim input")
    payload = bind_catalogued_selection_input(
        parsed_input,
        workspace=workspace,
        path=Path(source_path).expanduser().resolve(),
        anchor=anchor,
        window_lines=window_lines,
        occurrence=occurrence,
    )
    result = _server_call(
        lambda client, instance_id: client.compile_authoring(
            instance_id,
            payload=payload.model_dump(mode="json"),
            intent_id=None,
        ),
        command_name="cruxible authoring bind",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("preflight")
@click.argument("intent_id")
@brief_option
@handle_errors
def preflight_authoring_intent(intent_id: str, output_brief: bool) -> None:
    """Re-run every check on a staged intent.

    A refused intent whose base is behind the accepted head is checked again at
    the head and, when it passes there, advances to it; one with a payload fault
    stays refused and is revised with `authoring compile PAYLOAD --intent-id ID`.
    A refused preflight names its diagnostics on stderr and exits 1, as submit does.
    """

    result = _server_call(
        lambda client, instance_id: client.preflight_authoring_intent(instance_id, intent_id),
        command_name="cruxible authoring preflight",
    )
    if output_brief:
        codes = [item.code for item in result.frontier.diagnostics]
        if result.unchanged:
            # S2: identical to accepted state; a submit would propose nothing.
            _emit_brief(outcome="unchanged", ids={"intent": intent_id}, next_command=None)
            return
        _emit_brief(
            outcome=result.verdict + (f" ({', '.join(codes)})" if codes else ""),
            ids={"intent": intent_id},
            next_command=(
                f"cruxible authoring submit --intent-id {intent_id}"
                if result.verdict == "passed"
                else f"cruxible authoring compile - --intent-id {intent_id}  # repaired payload"
            ),
        )
    else:
        _emit_json(result.model_dump(mode="json"))
    _exit_if_preflight_refused(result)


@authoring_group.command("submit")
@click.argument(
    "payload",
    required=False,
    metavar="[PAYLOAD]",
    type=PayloadFile(),
)
@click.option(
    "--intent-id",
    default=None,
    help="Submit this staged intent; with PAYLOAD, revise it to PAYLOAD first.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Return the preflight submitting PAYLOAD would run, with every refusal; save nothing.",
)
@click.option("--no-accept", is_flag=True, help="Submit a proposal without activating it.")
@brief_option
@json_option
@handle_errors
def submit_authoring_intent(
    payload: str | None,
    intent_id: str | None,
    dry_run: bool,
    no_accept: bool,
    output_brief: bool,
    output_json: bool,
) -> None:
    """Compile, check and submit PAYLOAD as one proposal, or submit a staged intent.

    \b
    authoring submit PAYLOAD                  compile and submit in one call
    authoring submit PAYLOAD --dry-run        every refusal, nothing saved (exits 1 if refused)
    authoring submit --intent-id ID           submit a staged intent
    authoring submit PAYLOAD --intent-id ID   revise a staged intent, then submit

    PAYLOAD is a tagless authoring input file, or - for stdin. A refused
    preflight returns the unsubmitted intent with its diagnostics; a passing one
    lands immediately when policy and caller allow. Otherwise the typed result
    names why it stopped and the next step. --no-accept keeps the proposal.
    The full record prints as JSON; a refused preflight prints the brief block
    (outcome, ids, reason, next) instead, with the record behind --json.
    """

    if payload is None and intent_id is None:
        raise click.UsageError("provide PAYLOAD, --intent-id, or both")
    if dry_run and (payload is None or intent_id is not None):
        raise click.UsageError("--dry-run previews a new PAYLOAD and takes no --intent-id")
    # Parsed before any daemon call: a malformed payload names its field path
    # and the matching example without needing a reachable daemon.
    parsed = None if payload is None else _read_authoring_input(payload).model_dump(mode="json")
    if dry_run:
        assert parsed is not None
        preview = _server_call(
            lambda client, instance_id: client.preview_authoring_input(instance_id, input=parsed),
            command_name="cruxible authoring submit",
        )
        if output_brief:
            codes = [item.code for item in preview.frontier.diagnostics]
            _emit_brief(
                outcome=f"would {'pass' if preview.verdict == 'passed' else 'refuse'}"
                + (f" ({', '.join(codes)})" if codes else ""),
                ids={},
                next_command=(
                    f"cruxible authoring submit {shlex.quote(payload or '')}"
                    if preview.verdict == "passed" and payload != "-"
                    else None
                ),
            )
        else:
            _emit_json(preview.model_dump(mode="json"))
        _exit_if_preflight_refused(preview)
        return

    def call(client: CruxibleClient, instance_id: str) -> contracts.AuthoringSubmitResultRecord:
        accept: AuthoringAccept = "never" if no_accept else "if_allowed"
        if parsed is None:
            assert intent_id is not None
            return client.submit_authoring_intent(instance_id, intent_id, accept=accept)
        return client.submit_authoring_input(
            instance_id, input=parsed, intent_id=intent_id, accept=accept
        )

    submitted = _server_call(call, command_name="cruxible authoring submit")
    outcome = submitted.outcome
    refused = isinstance(outcome, contracts.AuthoringRefused)
    if output_brief or (refused and not output_json):
        if isinstance(outcome, contracts.AuthoringLanded):
            headline = "accepted"
            coordinate: str | None = outcome.accepted_coordinate.git_oid
            reason = None
            next_command = _landed_line_next(submitted.members)
        else:
            headline = (
                f"refused ({outcome.reason})"
                if isinstance(outcome, contracts.AuthoringRefused)
                else f"submitted ({submitted.status.state})"
            )
            coordinate = None
            reason = outcome.message
            next_command = outcome.next
        _emit_brief(
            outcome=headline,
            ids={
                "intent": str(submitted.intent.get("intent_id")),
                "proposal": getattr(outcome, "proposal_id", None),
                "coordinate": coordinate,
            },
            reason=_submit_refusal_reason(submitted) or reason,
            next_command=next_command,
        )
    else:
        _emit_json(submitted.model_dump(mode="json"))
    if refused:
        # Nothing was proposed: a script reading the exit code must not see success.
        raise click.exceptions.Exit(1)


def _landed_line_next(members: Sequence[Mapping[str, Any]]) -> str | None:
    """A landed change that created Lines: enabling them is the next step. O(members)."""

    names = [
        str(member["path"]).removeprefix("lines/").removesuffix(".json")
        for member in members
        if str(member.get("path", "")).startswith("lines/")
        and member.get("disposition") == "create"
    ]
    return " && ".join(f"cruxible line enable {shlex.quote(name)}" for name in names) or None


def _submit_refusal_reason(submitted: Any) -> str | None:
    """Render the complete typed preflight refusal on one transcript line."""

    if submitted.status.state != "preflight_refused" or not isinstance(submitted.intent, Mapping):
        return None
    return _preflight_refusal_reason(submitted.intent.get("last_preflight"))


def _exit_if_preflight_refused(result: contracts.AuthoringPreflightResult) -> None:
    """Every authoring command refuses alike: the refusal line on stderr, exit 1.

    The record (or brief) is already on stdout; a script or agent reading the
    exit code of compile, preflight or a dry run must not see success.
    """

    if result.verdict != "passed":
        click.echo(
            f"refused: {_preflight_refusal_reason(result.model_dump(mode='json'))}", err=True
        )
        raise click.exceptions.Exit(1)


def _preflight_refusal_reason(preflight: object) -> str:
    """One transcript line for a refused preflight: every diagnostic and blocked check."""

    frontier = preflight.get("frontier") if isinstance(preflight, Mapping) else None
    diagnostics = frontier.get("diagnostics") if isinstance(frontier, Mapping) else None
    blocked_checks = frontier.get("blocked_checks") if isinstance(frontier, Mapping) else None
    if not isinstance(diagnostics, list | tuple) and not isinstance(blocked_checks, list | tuple):
        return "preflight refused without a delivered diagnostic"
    rendered: list[str] = []
    shown: set[str] = set()
    for diagnostic in diagnostics if isinstance(diagnostics, list | tuple) else ():
        if not isinstance(diagnostic, Mapping):
            continue
        code = diagnostic.get("code")
        message = diagnostic.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            continue
        one_line_message = " ".join(message.split())
        # A message that already leads with its code is not prefixed twice.
        rendered.append(
            one_line_message if one_line_message.startswith(code) else f"{code}: {one_line_message}"
        )
        shown.add(code)
    for blocked in blocked_checks if isinstance(blocked_checks, list | tuple) else ():
        if not isinstance(blocked, Mapping):
            continue
        check = blocked.get("check")
        reason = blocked.get("reason")
        blocked_by = blocked.get("blocked_by")
        if (
            not isinstance(check, str)
            or not isinstance(reason, str)
            or not isinstance(blocked_by, list | tuple)
            or not all(isinstance(dependency, str) for dependency in blocked_by)
        ):
            continue
        if blocked_by and set(blocked_by) <= shown:
            continue  # blocked only by a refusal printed above: nothing new to say
        dependencies = ", ".join(blocked_by)
        one_line_reason = " ".join(reason.split())
        rendered.append(f"blocked {check} by {dependencies}: {one_line_reason}")
    return "; ".join(rendered) or "preflight refused without a delivered diagnostic"


@authoring_group.command("status")
@click.argument("intent_id")
@handle_errors
def authoring_intent_status(intent_id: str) -> None:
    """Read what still separates an intent from acceptance.

    The answer names each remaining condition (a refusal to repair, an approval
    to collect, an activation to run) without acting for the actor who owns it.
    """

    result = _server_call(
        lambda client, instance_id: client.authoring_intent_status(instance_id, intent_id),
        command_name="cruxible authoring status",
    )
    _emit_json(result.model_dump(mode="json"))


# -- the write verbs: set, retire, write --------------------------------------------


class _WriteFileV1(BaseModel):
    """What ``cruxible write FILE`` reads: the changes, and why."""

    model_config = ConfigDict(extra="forbid")

    because: str | None = Field(
        default=None, min_length=1, description="Why; --because overrides it."
    )
    subject: SubjectRef | None = Field(
        default=None,
        description="The Subject (kind/id) of every change that names none.",
    )
    changes: list[Change] = Field(default_factory=list, max_length=500)
    docs: list[DocOp] = Field(default_factory=list, max_length=500)


def _write_options(function: Callable[..., Any]) -> Callable[..., Any]:
    for option in reversed(
        (
            click.option("--dry-run", is_flag=True, help="Run every check; write nothing."),
            click.option(
                "--no-accept",
                is_flag=True,
                help="Only propose, even when policy would let this write be accepted now.",
            ),
            click.option(
                "--at",
                "at_oid",
                default=None,
                help="The git oid (or 12+ hex prefix) you read at; refuse if the slot moved.",
            ),
            json_option,
        )
    ):
        function = option(function)
    return function


def _write_text(outcome: WriteOutcome) -> None:
    for text, err in write_outcome_lines(outcome):
        click.echo(text, err=err)


_awaiting_text = awaiting_text


def _finish_write(outcome: WriteOutcome, *, output_json: bool) -> None:
    if output_json:
        _emit_json(outcome.model_dump(mode="json"))
    else:
        _write_text(outcome)
    if outcome.refused:
        raise SystemExit(1)


def _write_request(model: type[ResultT], fields: Mapping[str, Any], *, example: str) -> ResultT:
    validator = getattr(model, "model_validate")
    try:
        return cast(ResultT, validator({**fields, "surface": "cli"}))
    except ValidationError as exc:
        raise click.UsageError(
            "; ".join(_model_field_errors(exc)) + f" (example: {example})"
        ) from None


_BASED_ON_HELP = (
    "A Claim (CLM-..., or SUBJECT#FIELD for that field's one live Claim) this value was "
    "judged from, pinned at its current version; repeatable. A later revision or "
    "retirement of it flags this value in next."
)


def _based_on_option_value(based_on: tuple[str, ...], clear: bool) -> list[str] | None:
    """--based-on IDs, [] for --clear-based-on, None to keep a revision's pins."""

    if clear and based_on:
        raise click.UsageError("pass --based-on or --clear-based-on, not both")
    if clear:
        return []
    return list(based_on) if based_on else None


def _expect_option_value(values: tuple[str, ...]) -> str | tuple[str, ...] | None:
    """``--expect`` given once is the value; given again, every live value."""

    if not values:
        return None
    return values[0] if len(values) == 1 else values


_EXPECT_HELP = (
    "Refuse unless the field holds this value now (compare-and-set); repeat it "
    "for every value of a many-valued field."
)


def _evidence_option_value(
    evidence_file: str | None,
    capture: str | None,
    workspace_root: str,
    contract: str | None = None,
) -> dict[str, Any] | None:
    given = [
        flag
        for flag, value in (
            ("--evidence-file", evidence_file),
            ("--capture", capture),
            ("--evidence-contract", contract),
        )
        if value is not None
    ]
    if len(given) > 1:
        raise click.UsageError(f"pass one of {', '.join(given)}, not both")
    if capture is not None:
        return {"kind": "capture", "capture": capture}
    if contract is not None:
        return {"kind": "contract", "contract": contract}
    if evidence_file is None:
        return None
    # Read on this side by the workspace write (_workspace_write), as write does.
    return {"kind": "file", "file": evidence_file}


_NONE_HELP = (
    "Write a none: FIELD holds nothing (an inverse Claim; its ClaimType declares one). "
    "Cite evidence its inverse rules admit."
)


_SECTION_HELP = (
    "FILE#Heading/Path: cite that section of a workspace file (FILE# for the preamble); "
    "with --section-body, the section this write appends and cites."
)
_SECTION_BODY_HELP = (
    "With --section: append that heading with this body (- reads stdin) and cite it, "
    "in the same write; FILE is created and catalogued if new."
)
_OWN_WORDS_HELP = "With --section: the section's text is your own words, not observed evidence."
#: The doc op id a --section-body append is cited by.
_SECTION_BODY_ID = "section"
_SECTION_BODY_EXAMPLE = (
    "cruxible set dev.item/tidy-cli status done --section notes.md#Decisions/tidy-cli "
    '--section-body "Shipped in 0.6." --because "Shipped."'
)


def _section_option_value(
    section_ref: str | None,
    section_body: str | None,
    own_words: bool,
    *,
    others: Sequence[tuple[str, object]],
    workspace_root: str,
) -> tuple[Any, list[AppendSection]]:
    """--section (and --section-body, --own-words): the evidence, and the append it cites.

    --section alone cites an existing section, as rebind and claim attest do.
    With --section-body the write appends that heading (under its parents)
    with the body and cites the new section by the append's id: prose and
    the Claim it backs land in one write. Returns (None, []) without --section.
    """

    from cruxible_client.authoring.write_evidence import rebind_evidence
    from cruxible_client.contracts.write import SectionEvidence
    from cruxible_core.cli.payloads import STDIN, _claim_stdin, read_payload_text

    if section_ref is None:
        if section_body is not None:
            raise click.UsageError(
                "--section-body needs --section FILE#Heading/Path: the heading it appends"
            )
        if own_words:
            raise click.UsageError("--own-words applies to --section")
        return None, []
    given = [flag for flag, value in others if value is not None]
    if given:
        raise click.UsageError(f"pass --section or {', '.join(given)}, not both")
    if section_body is None:
        try:
            evidence = rebind_evidence(section=section_ref, workspace=Path(workspace_root))
        except ValueError as exc:
            raise click.UsageError(f"--section: {exc}") from None
        return (evidence.model_copy(update={"own_words": True}) if own_words else evidence), []
    file, mark, rest = section_ref.partition("#")
    heading = tuple(item.strip() for item in rest.split("/") if item.strip())
    if not file or not mark or not heading:
        raise click.UsageError(
            "--section with --section-body takes FILE#Heading/Path: the heading to append"
        )
    if section_body == STDIN:
        _claim_stdin(click.get_current_context(silent=True))
        section_body = read_payload_text(STDIN)
    try:
        append = AppendSection(
            id=_SECTION_BODY_ID,
            file=file,
            under=heading[:-1],
            heading=heading[-1],
            text=section_body,
        )
    except ValidationError as exc:
        raise click.UsageError("--section: " + "; ".join(_model_field_errors(exc))) from None
    evidence = SectionEvidence(ref=_SECTION_BODY_ID, own_words=True if own_words else None)
    return evidence, [append]


@playbill_group.command("set")
@click.argument("subject")
@click.argument("field")
@click.argument("value", required=False)
@click.option("--none", "none_", is_flag=True, help=_NONE_HELP + " Leave VALUE out.")
@click.option("--because", required=True, help="Why; also the default evidence.")
@click.option(
    "--evidence-file",
    default=None,
    help=(
        "PATH#ANCHOR: cite text found once in a workspace file; the write catalogues "
        "the file if it is not yet."
    ),
)
@click.option(
    "--capture",
    default=None,
    help="Cite an existing Capture: its handle CAP-<12+ hex>, or its sha256 digest.",
)
@click.option(
    "--evidence-contract",
    default=None,
    help="Cite the newest verified Capture of this CaptureContract about SUBJECT.",
)
@click.option("--section", "section_ref", default=None, help=_SECTION_HELP)
@click.option("--section-body", default=None, help=_SECTION_BODY_HELP)
@click.option("--own-words", is_flag=True, help=_OWN_WORDS_HELP)
@click.option("--role", default=None, help="Only when the field permits several roles.")
@click.option("--contend", is_flag=True, help="Contest the live value instead of replacing it.")
@click.option("--expect", "expect", multiple=True, help=_EXPECT_HELP)
@click.option(
    "--expect-absent",
    is_flag=True,
    help="Refuse unless the field holds no value now (compare-and-set on an empty field).",
)
@click.option(
    "--based-on",
    "based_on",
    multiple=True,
    metavar="CLAIM_ID",
    help=_BASED_ON_HELP,
)
@click.option(
    "--clear-based-on",
    is_flag=True,
    help="Drop the based-on pins this revision would keep.",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog --evidence-file and --section read.",
)
@_write_options
@handle_errors
def set_value(
    subject: str,
    field: str,
    value: str | None,
    none_: bool,
    because: str,
    evidence_file: str | None,
    capture: str | None,
    evidence_contract: str | None,
    section_ref: str | None,
    section_body: str | None,
    own_words: bool,
    role: str | None,
    contend: bool,
    expect: tuple[str, ...],
    expect_absent: bool,
    based_on: tuple[str, ...],
    clear_based_on: bool,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Set FIELD of SUBJECT (kind/id) to VALUE, replacing the live value.

    The Claim it replaces is found for you. SUBJECT may also be kind/<key
    value> as written (kind/CVE-2021-44228): a one-key kind's Subject that does
    not exist yet is added by that key. VALUE is text: an enum member, a number or true/false
    for such fields, a Subject as kind/id, or bounded text. --none
    instead states that FIELD holds nothing (a none), replacing the live value.
    --based-on CLM-... pins a Claim the value was judged from: a later revision
    or retirement of it flags this value in next (claim_dependency_stale).
    --section FILE#Heading/Path cites that section; with --section-body the
    write appends it and cites it in one step (reasoning past a 280-character
    --because goes there).
    """

    if expect_absent and expect:
        raise click.UsageError("pass --expect or --expect-absent, not both")
    if none_ == (value is not None):
        raise click.UsageError("pass VALUE, or --none for a field that holds nothing; not both")
    section_evidence, docs = _section_option_value(
        section_ref,
        section_body,
        own_words,
        others=(
            ("--evidence-file", evidence_file),
            ("--capture", capture),
            ("--evidence-contract", evidence_contract),
        ),
        workspace_root=workspace_root,
    )
    request = _write_request(
        SetRequest,
        {
            "subject": subject,
            "field": field,
            "value": {"none": None} if none_ else value,
            "because": because,
            "evidence": section_evidence
            or _evidence_option_value(evidence_file, capture, workspace_root, evidence_contract),
            "role": role,
            "contend": contend,
            "expect": () if expect_absent else _expect_option_value(expect),
            "based_on": _based_on_option_value(based_on, clear_based_on),
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example='cruxible set dev.item/tidy-cli status done --because "Shipped."',
    )
    if evidence_file is not None or section_evidence is not None:
        written = as_write_request(request)
        if docs:
            # The append the section evidence cites, in the same change set.
            written = _write_request(
                WriteRequest,
                {**dict(written), "docs": docs},
                example=_SECTION_BODY_EXAMPLE,
            )
        _workspace_write(
            written,
            workspace_root=workspace_root,
            send=lambda observed: _server_call(
                lambda client, instance_id: client.write(instance_id, request=observed),
                command_name="cruxible set",
            ),
            output_json=output_json,
        )
        return
    outcome = _server_call(
        lambda client, instance_id: client.set(instance_id, request=request),
        command_name="cruxible set",
    )
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("add")
@click.argument("subject")
@click.argument("field")
@click.argument("value")
@click.option(
    "--none",
    "none_",
    is_flag=True,
    help=_NONE_HELP.replace("holds nothing", "does not hold VALUE")
    + " VALUE is the value (or key value) it does not hold.",
)
@click.option("--because", required=True, help="Why; also the default evidence.")
@click.option(
    "--evidence-file",
    default=None,
    help=(
        "PATH#ANCHOR: cite text found once in a workspace file; the write catalogues "
        "the file if it is not yet."
    ),
)
@click.option(
    "--capture",
    default=None,
    help="Cite an existing Capture: its handle CAP-<12+ hex>, or its sha256 digest.",
)
@click.option(
    "--evidence-contract",
    default=None,
    help="Cite the newest verified Capture of this CaptureContract about SUBJECT.",
)
@click.option("--section", "section_ref", default=None, help=_SECTION_HELP)
@click.option("--section-body", default=None, help=_SECTION_BODY_HELP)
@click.option("--own-words", is_flag=True, help=_OWN_WORDS_HELP)
@click.option("--role", default=None, help="Only when the field permits several roles.")
@click.option(
    "--expect-absent",
    is_flag=True,
    help="Refuse when the value is already there, instead of answering it as done.",
)
@click.option(
    "--based-on",
    "based_on",
    multiple=True,
    metavar="CLAIM_ID",
    help=_BASED_ON_HELP,
)
@click.option(
    "--clear-based-on",
    is_flag=True,
    help="Drop the based-on pins this revision would keep.",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog --evidence-file and --section read.",
)
@_write_options
@handle_errors
def add_value(
    subject: str,
    field: str,
    value: str,
    none_: bool,
    because: str,
    evidence_file: str | None,
    capture: str | None,
    evidence_contract: str | None,
    section_ref: str | None,
    section_body: str | None,
    own_words: bool,
    role: str | None,
    expect_absent: bool,
    based_on: tuple[str, ...],
    clear_based_on: bool,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Add VALUE to many-valued FIELD of SUBJECT (kind/id), beside the values there.

    A value already there is answered as done (--expect-absent refuses instead).
    A Subject of a known kind that does not exist yet is added. VALUE is text, as
    for set: a Subject as kind/id for a Subject-valued field. --none adds a none:
    FIELD does not hold VALUE (retire a value it holds in the same write file).
    --section-body appends the --section heading and cites it in the same write.
    """

    section_evidence, docs = _section_option_value(
        section_ref,
        section_body,
        own_words,
        others=(
            ("--evidence-file", evidence_file),
            ("--capture", capture),
            ("--evidence-contract", evidence_contract),
        ),
        workspace_root=workspace_root,
    )
    request = _write_request(
        WriteRequest,
        {
            "docs": docs,
            "changes": [
                {
                    "op": "add",
                    "subject": subject,
                    "field": field,
                    "value": {"none": value} if none_ else value,
                    "evidence": section_evidence
                    or _evidence_option_value(
                        evidence_file, capture, workspace_root, evidence_contract
                    ),
                    "role": role,
                    "expect_absent": expect_absent,
                    "based_on": _based_on_option_value(based_on, clear_based_on),
                }
            ],
            "because": because,
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example=(
            'cruxible add dev.item/tidy-cli governs dev.item/cli-docs --because "Linked in review."'
        ),
    )
    _workspace_write(
        request,
        workspace_root=workspace_root,
        send=lambda observed: _server_call(
            lambda client, instance_id: client.write(instance_id, request=observed),
            command_name="cruxible add",
        ),
        output_json=output_json,
    )


@playbill_group.command("retire")
@click.argument("target")
@click.argument("field", required=False)
@click.argument("value", required=False)
@click.option("--because", required=True, help="Why it ends.")
@click.option(
    "--reason",
    type=click.Choice(["was-rescinded", "was-wrong", "superseded"]),
    default="was-rescinded",
    show_default=True,
    help="was-rescinded: withdrawn; was-wrong: it was false; superseded: its shape is gone.",
)
@click.option("--expect", "expect", multiple=True, help=_EXPECT_HELP)
@click.option(
    "--with-claims",
    is_flag=True,
    help="Retiring a Subject: also retire its live Claims, under --reason.",
)
@_write_options
@handle_errors
def retire(
    target: str,
    field: str | None,
    value: str | None,
    because: str,
    reason: str,
    expect: tuple[str, ...],
    with_claims: bool,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Retire one live Claim, or a Subject: TARGET is a Claim ID, or kind/id.

    With FIELD (and VALUE, when FIELD holds several), TARGET's Claim in that
    field retires; Claims that depend on it retire with it, in the same change
    set. TARGET alone as kind/id retires the Subject and its key Claims: its
    other live Claims retire only with --with-claims, and Claims of other
    Subjects whose value it is must be re-pointed or retired first (the refusal
    lists each). To merge, alias the survivor with this Subject's id first.
    """

    if value is not None and field is None:
        raise click.UsageError("VALUE needs FIELD: cruxible retire SUBJECT FIELD VALUE")
    if with_claims and field is not None:
        raise click.UsageError("--with-claims retires a Subject: pass kind/id without FIELD")
    slot: dict[str, object] | None = None
    if field is not None:
        slot = {"subject": target, "field": field}
        if value is not None:
            slot["value"] = value
    request = _write_request(
        RetireRequest,
        {
            "target": target if slot is None else slot,
            "because": because,
            "reason": reason,
            "expect": _expect_option_value(expect),
            "with_claims": with_claims,
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example='cruxible retire CLM-0123456789abcdef0123456789abcdef --because "Wrong."',
    )
    outcome = _server_call(
        lambda client, instance_id: client.retire(instance_id, request=request),
        command_name="cruxible retire",
    )
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("rebind")
@click.argument("claim", required=False)
@click.option(
    "--from-capture",
    "from_capture",
    default=None,
    help="CAP-... or digest, instead of CLAIM: re-bind every live Claim citing this Capture "
    "onto --capture (at most 500).",
)
@click.option("--because", required=True, help="Why the new text backs it.")
@click.option(
    "--file",
    "file_anchor",
    default=None,
    help="FILE#ANCHOR: the text, found once in a catalogued file, the Claim rests on now.",
)
@click.option(
    "--section",
    "section_ref",
    default=None,
    help="FILE#Heading/Path: the section the Claim rests on now (FILE# for the preamble).",
)
@click.option(
    "--capture",
    "capture_ref",
    default=None,
    help="CAP-... or digest: a fresh Capture, under the replaced citation's contract.",
)
@click.option(
    "--citation",
    default=None,
    help="The citation to replace (12+ hex); default: the Claim's one citation of FILE "
    "or of the Capture's contract.",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog FILE is read through.",
)
@_write_options
@handle_errors
def rebind(
    claim: str | None,
    from_capture: str | None,
    because: str,
    file_anchor: str | None,
    section_ref: str | None,
    capture_ref: str | None,
    citation: str | None,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Re-cite CLAIM on the text it rests on now, or on a fresh Capture.

    The statement stays as it is: the named citation is replaced by the new
    one, recorded as one revision of the same Claim (history shows it as
    rebound). FILE is a catalogued file's path or its catalog name. --capture
    replaces a Capture citation with a fresh Capture under the same contract,
    which is how a Claim whose Capture was erased moves onto a new
    observation. A value that changed is restated with set; a Claim that no
    longer holds is retired.

    --from-capture CAP --capture NEW, instead of CLAIM, re-binds every live
    Claim citing CAP onto NEW in one write, each replacing its citation of
    CAP; it takes no --citation.
    """

    if (claim is None) == (from_capture is None):
        raise click.UsageError("pass CLAIM or --from-capture CAP, not both")
    if from_capture is not None and (capture_ref is None or citation is not None):
        raise click.UsageError(
            "--from-capture moves Claims onto a fresh Capture: pass --capture NEW and no --citation"
        )
    try:
        evidence = rebind_evidence(
            file=file_anchor,
            section=section_ref,
            capture=capture_ref,
            workspace=Path(workspace_root),
        )
    except ValueError as exc:
        raise click.UsageError(f"--file / --section / --capture: {exc}") from None
    change: dict[str, object] = {"op": "rebind", "evidence": evidence}
    if from_capture is not None:
        change["from_capture"] = from_capture
    else:
        change["target"] = claim
    if citation is not None:
        change["citation"] = citation
    request = _write_request(
        WriteRequest,
        {
            "changes": [change],
            "because": because,
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example=(
            "cruxible rebind CLM-0123456789abcdef0123456789abcdef "
            '--section docs/acme.md#Pricing --because "The price moved to its own section."'
        ),
    )
    try:
        observed = request.model_copy(
            update={"changes": observe_changes(request.changes, workspace=Path(workspace_root))}
        )
    except (ValidationError, CoreError, ValueError) as exc:
        raise click.UsageError(f"{file_anchor or section_ref or capture_ref}: {exc}") from None
    outcome = _server_call(
        lambda client, instance_id: client.write(instance_id, request=observed),
        command_name="cruxible rebind",
    )
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("write")
@click.argument("file", required=False, type=PayloadFile())
@click.option("--because", default=None, help="Why; overrides the file's because.")
@click.option("--schema", is_flag=True, help="Print the JSON schema FILE is validated against.")
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog file evidence reads.",
)
@click.option(
    "--based-on",
    "based_on",
    multiple=True,
    metavar="CLAIM_ID",
    help=_BASED_ON_HELP + " Applies to each set and add change that names no based_on.",
)
@_write_options
@handle_errors
def write_changes(
    file: str | None,
    because: str | None,
    schema: bool,
    workspace_root: str,
    based_on: tuple[str, ...],
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Apply FILE's document edits and create, set, add and retire changes.

    FILE (YAML or JSON; - reads stdin) holds {"because": ..., "changes": [...]},
    or a bare list of changes with --because. Each change is {"op": "set" |
    "add", "subject", "field", "value"}, {"op": "retire", "target"} or {"op":
    "create", "label", "subject_kind", "key"}, where key is one value or
    {field: value, ...} (e.g. {"occurrence": "kind/id", "vulnerability":
    "kind/id"}); other changes name a created Subject {"created": label}, as
    subject or value. A top-level "subject" is
    the Subject of every change that names none. --schema prints the schema.
    Optional docs hold append {file, under, text} or replace {file, section,
    text, expect}. Paths are heading lists; [] selects the preamble. Section
    evidence {kind: section, file, section} cites the edited text in the same call.
    """

    if schema:
        _emit_json(_WriteFileV1.model_json_schema())
        return
    if file is None:
        raise click.UsageError("pass FILE, or --schema to see what FILE holds")
    source = payload_label(file)
    payload = read_payload_document(file)
    if isinstance(payload, list):
        payload = {"changes": payload}
    try:
        parsed = _WriteFileV1.model_validate(payload)
    except ValidationError as exc:
        raise DataValidationError(
            f"{source} is not a valid write file (cruxible write --schema prints it)",
            errors=_model_field_errors(exc),
        ) from exc
    rationale = because or parsed.because
    if rationale is None:
        raise click.UsageError("give the write a reason: --because, or because in FILE")
    changes: Sequence[Change] = parsed.changes
    if based_on:
        changes = tuple(
            change.model_copy(update={"based_on": _based_on_option_value(based_on, False)})
            if isinstance(change, SetChange | AddChange) and change.based_on is None
            else change
            for change in changes
        )
    request = _write_request(
        WriteRequest,
        {
            "changes": changes,
            "docs": parsed.docs,
            "subject": parsed.subject,
            "because": rationale,
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example="cruxible write changes.yaml",
    )
    _workspace_write(
        request,
        workspace_root=workspace_root,
        send=lambda observed: _server_call(
            lambda client, instance_id: client.write(instance_id, request=observed),
            command_name="cruxible write",
        ),
        output_json=output_json,
    )


def _workspace_write(
    request: WriteRequest,
    *,
    workspace_root: str,
    send: Callable[[WriteRequest], WriteOutcome],
    output_json: bool,
) -> None:
    """Send a write through the workspace seam: cited files are read (and catalogued) here."""

    from cruxible_client.contracts.errors import WriteRefusalError
    from cruxible_core.cli.commands._common import _echo_refusal

    submitted = False

    def submit(observed: WriteRequest) -> WriteOutcome:
        nonlocal submitted
        submitted = True
        return send(observed)

    try:
        outcome = write_workspace(request, workspace=Path(workspace_root), submit=submit)
    except WriteRefusalError as exc:
        # A write refused while observing files never reached the daemon.
        if submitted or output_json:
            raise
        _echo_refusal(
            "not submitted",
            code=exc.error_code,
            message=exc.detail,
            change=exc.change,
            candidates=exc.candidates,
            repair=exc.repair_line,
        )
        raise SystemExit(1) from None
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("note")
@click.argument("file")
@click.option(
    "--text",
    required=True,
    help="The section to append; it starts with its heading, or is the body under --heading.",
)
@click.option(
    "--heading",
    default=None,
    help="The new heading's text, once; written at the depth --under needs.",
)
@click.option(
    "--under",
    default=None,
    help="Heading/Path to append under (default: the end of FILE, at TEXT's level).",
)
@click.option(
    "--about",
    "subjects",
    multiple=True,
    required=True,
    help="A Subject (kind/id) the note is about; repeat for more.",
)
@click.option("--because", required=True, help="Why this note is recorded.")
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog FILE is read through.",
)
@_write_options
@handle_errors
def note(
    file: str,
    text: str,
    heading: str | None,
    under: str | None,
    subjects: tuple[str, ...],
    because: str,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Append a section to FILE and link it about Subjects: a note, never a Claim.

    Shorthand for one write: an append of TEXT plus an about change on the new
    section. Same governance as write; the file is written once admitted. A
    note retains the section's bytes; backlinks on each Subject list it.
    """

    request = _write_request(
        WriteRequest,
        {
            "docs": [
                {
                    "op": "append",
                    "id": "note",
                    "file": file,
                    "under": [] if under is None else [p for p in under.split("/") if p],
                    "text": text,
                    **({} if heading is None else {"heading": heading}),
                }
            ],
            "changes": [{"op": "about", "ref": "note", "subjects": list(subjects)}],
            "because": because,
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example=(
            'cruxible note notes/triage.md --text "## wi-1\nBlocked on review." '
            '--about project.work_item/wi-1 --because "Triage context"'
        ),
    )

    def submit(observed: WriteRequest) -> WriteOutcome:
        return _server_call(
            lambda client, instance_id: client.write(instance_id, request=observed),
            command_name="cruxible note",
        )

    _finish_write(
        write_workspace(request, workspace=Path(workspace_root), submit=submit),
        output_json=output_json,
    )


@playbill_group.command("link")
@click.argument("source", metavar="FILE[#Heading/Path]")
@click.option(
    "--about",
    "subjects",
    multiple=True,
    help="With FILE#Heading/Path: a Subject (kind/id) the section is about; repeat for more.",
)
@click.option(
    "--guides",
    "guides",
    multiple=True,
    help="With a registered FILE: ClaimType:NAME or Procedure:NAME it guides; repeat for more.",
)
@click.option("--unlink", is_flag=True, help="Remove the named links instead of adding them.")
@click.option("--because", default=None, help="With --about: why the note is linked.")
@click.option(
    "--name",
    "proposal_name",
    default=None,
    help="With --guides: the Document proposal's name (default: link-<file>).",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog FILE is read through.",
)
@_write_options
@handle_errors
def link(
    source: str,
    subjects: tuple[str, ...],
    guides: tuple[str, ...],
    unlink: bool,
    because: str | None,
    proposal_name: str | None,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Link a section about Subjects (a note), or a registered page as guidance.

    FILE#Heading/Path --about kind/id links that section about Subjects: a
    note on what the text says, in the value lane under the same governance as
    write; backlinks on the Subject list it. FILE --guides ClaimType:NAME (or
    Procedure:NAME) links the whole registered page to a definition as its
    review guidance: the link is recorded on the file's entry in files.yaml
    and the Document's next revision is proposed from the file's bytes plus
    its links (later files propose keeps them); proposal review shows it.
    FILE is a path or a catalog name. --unlink removes the named links.
    """

    from cruxible_client.authoring.links import (
        LINK_ONE_LANE,
        about_link_request,
        link_guides,
    )
    from cruxible_client.contracts.errors import WriteRefusalError
    from cruxible_core.cli.commands._common import _echo_refusal

    accept: Literal["if_allowed", "never"] = "never" if no_accept else "if_allowed"
    try:
        if bool(subjects) == bool(guides):
            raise WriteRefusalError(
                LINK_ONE_LANE,
                "link takes --about (a section's note) or --guides (a page's guidance), "
                + ("not both" if subjects else "and got neither"),
                repair_line=(
                    'cruxible link FILE#Heading --about kind/id --because "..."; or '
                    "cruxible link FILE --guides ClaimType:NAME"
                ),
            )
        if subjects:
            if because is None:
                raise click.UsageError("--about records a note: give it --because")
            if proposal_name is not None:
                raise click.UsageError("--name names a --guides proposal; --about is a write")
            request = about_link_request(
                source,
                subjects=subjects,
                unlink=unlink,
                because=because,
                dry_run=dry_run,
                accept=accept,
                at=at_oid,
            )
        elif because is not None:
            raise click.UsageError(
                "--because goes with --about; a --guides link is a Document proposal "
                "(name it with --name)"
            )
    except WriteRefusalError as exc:
        if output_json:
            raise
        _echo_refusal(
            "not submitted", code=exc.error_code, message=exc.detail, repair=exc.repair_line
        )
        raise SystemExit(1) from None
    if subjects:
        _workspace_write(
            request,
            workspace_root=workspace_root,
            send=lambda observed: _server_call(
                lambda client, instance_id: client.write(instance_id, request=observed),
                command_name="cruxible link",
            ),
            output_json=output_json,
        )
        return

    def call(client: CruxibleClient, instance_id: str) -> Any:
        return link_guides(
            client,
            instance_id,
            workspace=Path(workspace_root),
            source=source,
            guides=guides,
            unlink=unlink,
            proposal_name=proposal_name,
            dry_run=dry_run,
            accept=accept,
            at=at_oid,
        )

    try:
        outcome = _server_call(call, command_name="cruxible link")
    except WriteRefusalError as exc:
        if output_json:
            raise
        _echo_refusal(
            "not submitted", code=exc.error_code, message=exc.detail, repair=exc.repair_line
        )
        raise SystemExit(1) from None
    refused = (
        not outcome.catalog_written
        and not outcome.catalog_conflict
        and not outcome.proposal.status.startswith("would_")
    )
    if output_json:
        _emit_json(outcome.model_dump(mode="json"))
        if refused or outcome.catalog_conflict:
            raise SystemExit(1)
        return
    result = outcome.proposal
    targets = ", ".join(item.target_identity for item in outcome.links if item.relation == "guides")
    click.echo(f"{outcome.file}: guides {targets or 'nothing'} ({result.status})")
    if outcome.declared_document:
        click.echo(f"Declared Document {outcome.document_id} on its catalog entry")
    admission = result.proposal.get("admission")
    proposal_id = admission.get("proposal_id") if isinstance(admission, dict) else None
    if proposal_id:
        click.echo(f"Proposal: {proposal_id}")
    _echo_landing(result.outcome)
    if outcome.catalog_written:
        click.echo("Recorded in files.yaml; files propose keeps these links")
    elif result.status.startswith("would_"):
        click.echo(
            f"Preview at {result.accepted_coordinate.git_oid}; nothing was written. "
            f"Commit it: rerun without --dry-run, with --at {result.accepted_coordinate.git_oid}"
        )
    elif outcome.catalog_conflict:
        click.echo(
            "the catalog changed this entry meanwhile, so the links were not recorded there; "
            "run the same link again to record them"
        )
        raise SystemExit(1)
    elif refused:
        click.echo("The proposal was refused; files.yaml is unchanged")
        raise SystemExit(1)


@playbill_group.command("backlinks")
@click.argument("ref", required=False)
@click.option(
    "--section",
    "section_ref",
    default=None,
    help="FILE#Heading/Path: the Claims resting on that workspace section (instead of REF).",
)
@click.option("--limit", type=click.IntRange(1, 100), default=25, show_default=True)
@click.option("--cursor", default=None, help="next_cursor of the previous page.")
@click.option("--at", "at_oid", default=None, help="Accepted git oid or generation; default head.")
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose files section rows are checked against for drift.",
)
@json_option
@handle_errors
def backlinks(
    ref: str | None,
    section_ref: str | None,
    limit: int,
    cursor: str | None,
    at_oid: str | None,
    workspace_root: str,
    output_json: bool,
) -> None:
    """What points into REF: notes and cited sections of a Subject, guidance of a ClaimType.

    REF is a Subject (kind/id) -- the notes about it, Documents linked about
    it, and the workspace sections its live Claims cite -- or a ClaimType or
    Procedure: the Documents that guide it. --section FILE#Heading/Path lists
    the live Claims resting on that section. Each section row says whether it
    changed since it was cited, against the file in the workspace now.
    """

    from cruxible_client.authoring.backlink_sections import observe_drift, section_source
    from cruxible_client.contracts.backlinks import BacklinksRequest

    if (ref is None) == (section_ref is None):
        raise click.UsageError("pass REF, or --section FILE#Heading/Path")
    workspace = Path(workspace_root)
    fields: dict[str, Any] = {"ref": ref}
    if section_ref is not None:
        path, mark, rest = section_ref.partition("#")
        heading = tuple(item for item in rest.split("/") if item)
        if not path or not mark or not heading:
            raise click.UsageError("--section takes FILE#Heading/Path")
        fields = {"file": section_source(workspace, path), "section": heading}
    try:
        request = BacklinksRequest(**fields, limit=limit, cursor=cursor, at=at_oid, surface="cli")
    except ValidationError as exc:
        raise click.UsageError(str(exc)) from None
    result = observe_drift(
        _server_call(
            lambda client, instance_id: client.backlinks(instance_id, request=request),
            command_name="cruxible backlinks",
        ),
        workspace,
    )
    if output_json:
        _emit_json(result.model_dump(mode="json", exclude_none=True))
        return
    file_paths = _display_file_paths(workspace)
    click.echo(
        f"backlinks of {file_ref_shown(result.target, file_paths)} "
        f"(generation {result.coordinate.generation}, "
        f"{result.coordinate.git_oid}): {len(result.links)}"
    )
    for link in result.links:
        click.echo("  " + _backlink_line(link, file_paths=file_paths))
    if result.next_cursor is not None:
        click.echo(f"next: cruxible backlinks ... --cursor {result.next_cursor}")


@playbill_group.command("section")
@click.argument("locator")
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog LOCATOR's file is read through.",
)
@json_option
@handle_errors
def section_read(locator: str, workspace_root: str, output_json: bool) -> None:
    """Read one section of a catalogued Markdown file: LOCATOR is FILE#Heading/Path.

    FILE is the file's path or its files.yaml name; FILE# reads the preamble.
    Prints the section's own body (its heading and prose, up to its first
    subsection) and the subsections to read next. The locators get, backlinks
    and floor cards print read here; --json adds the digest a citation records.
    Reads the workspace only; the daemon is not asked.
    """

    from cruxible_client.authoring.section_read import SectionReadError, read_section
    from cruxible_core.cli.commands._common import _echo_refusal

    try:
        result = read_section(Path(workspace_root), locator)
    except SectionReadError as exc:
        _echo_refusal(
            "not read",
            code=exc.code,
            message=exc.message,
            candidates=exc.candidates,
            repair=exc.repair,
        )
        raise SystemExit(1) from None
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(result.text, nl=not result.text.endswith("\n"))
    if result.subsections:
        prefix = "/".join(result.section)
        click.echo(
            "subsections: "
            + ", ".join(
                shlex.quote(f"{result.path}#{prefix + '/' if prefix else ''}{name}")
                for name in result.subsections
            )
        )


@playbill_group.command("get")
@click.argument("ref")
@click.option(
    "--detail",
    type=click.Choice(
        [
            "summary",
            "evidence",
            "why",
            "history",
            "incoming",
            "pairs",
            "runs",
            "backlinks",
            "proof",
            "proof-full",
            "body",
        ]
    ),
    default="summary",
    show_default=True,
    help=(
        "How deep to read the one thing REF names. On a Claim, proof is its verdict and "
        "cited Captures; proof-full the whole envelope and facts."
    ),
)
@click.option(
    "--range",
    "byte_range",
    default=None,
    help="Document body bytes start:end (with --detail body).",
)
@click.option(
    "--at",
    "at_oid",
    default=None,
    help=(
        "Accepted git oid, a unique 12+ hex prefix, or a generation number (all digits, "
        "11 or fewer, is always a generation); default head."
    ),
)
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Rows per --detail history, incoming, pairs, runs or backlinks page (default 20).",
)
@click.option(
    "--cursor",
    default=None,
    help="Continue --detail history, incoming, pairs, runs or backlinks from its next_cursor.",
)
@click.option(
    "--field",
    "field_name",
    default=None,
    help="With --detail history on a Subject: only this field's events.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="With --detail body: write the body's exact bytes to this new file (read in ranges).",
)
@json_option
@handle_errors
def get_by_ref(
    ref: str,
    detail: str,
    byte_range: str | None,
    at_oid: str | None,
    evaluation_time: str | None,
    limit: int | None,
    cursor: str | None,
    field_name: str | None,
    output_path: str | None,
    output_json: bool,
) -> None:
    """Read one governed thing by reference, values first.

    REF is any reference form: CLM-... (or a unique prefix), kind/id, a predicate,
    ClaimType:/Document:/Procedure:/query:/CaptureContract:<name>, Provider:NAME
    or Provider:NAME/BUILD, Contract:/Capability:<name>[@N], an artifact
    path, a proposal id or prefix, or an operational reference: Line:<name> (or
    the Line identity digest next names), CAP-<12+ hex> or Capture:<digest>,
    ResolutionContract:<name>, Mandate:<name>, or a note's NOTE-<32 hex>.
    """

    from cruxible_client.contracts.get_reads import ByteRange, GetRequest
    from cruxible_client.contracts.get_text import claim_proof_summary, claim_proof_summary_lines

    proof_full = detail == "proof-full"
    try:
        request = GetRequest.model_validate(
            {
                "ref": ref,
                "detail": "proof" if proof_full else detail,
                "range": None if byte_range is None else ByteRange.parse(byte_range),
                "at": at_oid,
                "evaluation_time": evaluation_time,
                "surface": "cli",
                "limit": limit,
                "cursor": cursor,
                "field": field_name,
            }
        )
    except (ValidationError, ValueError) as exc:
        errors = _model_field_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
        raise click.UsageError(
            "; ".join(errors) + " (example: cruxible get Document:design "
            "--detail body --range 0:4096)"
        ) from None
    if output_path is not None:
        if detail != "body":
            raise click.UsageError("--output writes a Document body; pass --detail body")
        _write_body(request, Path(output_path), whole=byte_range is None)
        return
    result = _server_call(
        lambda client, instance_id: client.get(instance_id, request=request),
        command_name="cruxible get",
    )
    if result.backlinks is not None:
        from cruxible_client.authoring.backlink_sections import observe_get_drift

        # Section rows checked against the workspace here, as `backlinks` does.
        result = observe_get_drift(result, Path("."))
    proof = result.proof
    claim_summary = result.kind == "claim" and result.detail == "proof" and not proof_full
    if output_json:
        if claim_summary and proof is not None:
            result = result.model_copy(update={"proof": claim_proof_summary(proof)})
        _emit_json(result.model_dump(mode="json"))
        return
    if claim_summary and proof is not None:
        click.echo(f"{result.ref}  ({result.coordinate.git_oid})")
        for line in claim_proof_summary_lines(proof):
            click.echo(line)
        return
    _emit_get_text(result, evidence_erased=_stale_claim_evidence_erased(request, result))
    if result.next_cursor is not None:
        narrowed = "" if field_name is None else f" --field {shlex.quote(field_name)}"
        click.echo(
            f"next: cruxible get {shlex.quote(ref)} --detail {detail}{narrowed} "
            f"--cursor {result.next_cursor}"
        )


def _write_body(request: Any, destination: Path, *, whole: bool) -> None:
    """Write a Document body's exact bytes to a new file, binary-safe.

    Without a range the whole body is read range by range at the first read's
    coordinate, so a body past the whole-body cap still lands in one file.
    """

    import base64

    from cruxible_client.contracts.get_reads import GET_BODY_RANGE_MAX_BYTES, ByteRange

    def read(window: Any, at: Any) -> Any:
        ranged = request.model_copy(update={"range": window, "at": at})
        result = _server_call(
            lambda client, instance_id: client.get(instance_id, request=ranged),
            command_name="cruxible get",
        )
        assert result.body is not None
        return result

    first = read(
        ByteRange(start=0, end=GET_BODY_RANGE_MAX_BYTES) if whole else request.range,
        request.at,
    )
    body = first.body
    pinned = first.coordinate.git_oid
    chunks: list[bytes] = []

    def chunk(part: Any) -> bytes:
        if part.text is not None:
            return str(part.text).encode("utf-8")
        return base64.b64decode(part.content_base64 or "", validate=True)

    chunks.append(chunk(body))
    end = 0 if body.range is None else body.range.end
    while whole and end < body.size:
        window = ByteRange(start=end, end=min(end + GET_BODY_RANGE_MAX_BYTES, body.size))
        part = read(window, pinned).body
        chunks.append(chunk(part))
        end = part.range.end
    with destination.open("xb") as handle:
        handle.write(b"".join(chunks))
    click.echo(
        f"wrote {sum(len(item) for item in chunks)} bytes to {destination} ({body.body_digest})"
    )


def _stale_claim_evidence_erased(request: Any, result: Any) -> bool:
    """Whether a stale Claim card reads stale because a cited Capture's body was erased.

    The summary card carries no erasure; only a stale Claim's card asks, with
    one evidence read pinned to the generation and time the card answered at.
    """

    card = result.card
    if result.kind != "claim" or card is None or getattr(card, "verdict", None) != "stale":
        return False
    evidence_request = request.model_copy(
        update={
            "detail": "evidence",
            "at": result.coordinate.git_oid,
            "evaluation_time": result.evaluation_time,
        }
    )
    evidence = _server_call(
        lambda client, instance_id: client.get(instance_id, request=evidence_request),
        command_name="cruxible get",
    ).evidence
    return evidence is not None and any(item.erased is not None for item in evidence.captures)


def _emit_get_text(result: Any, *, evidence_erased: bool = False) -> None:
    """Print one ``get`` answer through the shared renderer; ``--json`` carries it whole.

    ``evidence_erased`` marks a stale Claim's verdict as read from erased evidence.
    """

    names_files = result.evidence is not None or result.backlinks is not None
    text = render_get(
        result,
        evidence_erased=evidence_erased,
        file_paths=_display_file_paths() if names_files else None,
    )
    if text is None:
        _emit_json(result.proof)
        return
    body = getattr(result, "body", None)
    if body is None:
        click.echo(text)
        return
    click.echo(text, nl=False)
    if body.range is not None and body.range.end < body.size:
        following = min(body.size, 2 * body.range.end - body.range.start)
        click.echo(
            f"\n(bytes {body.range.start}:{body.range.end} of {body.size}; next: "
            f"--range {body.range.end}:{following})",
            err=True,
        )


def _echo_history(history: Any) -> None:
    for line in history_lines(history):
        click.echo(line)


def _echo_proposed_reason(reason: Any) -> None:
    for line in proposed_reason_lines(reason):
        click.echo(line)


@playbill_group.group("block")
def block_group() -> None:
    """Maintain local declarations without rendering or replacing authored prose."""


@block_group.command("depublish")
@click.argument("file_id")
@click.argument("block_id")
@change_control_options
@json_option
@handle_errors
def depublish_projection(
    file_id: str, block_id: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Release the declared registration that demands one page block.

    FILE_ID is the stored projection file identity; the file and catalog entry
    need not still exist.

    The registration is what `next` reads to decide a removed marker is a
    blocking row. Releasing it does not edit the page and does not touch the
    block's backings: strip the markers with `block detach PAGE` or by hand,
    and use this when the block itself is not coming back.
    """

    result = _server_call(
        lambda client, instance_id: client.depublish_block(
            instance_id,
            file_id,
            block_id,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible block depublish",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"{result.source_id}#{result.block_id}: {result.outcome}")
    echo_preview_next(result.outcome, result.coordinate)


@block_group.command("repin")
@click.argument("file")
@click.argument("block_id")
@click.option("--claim", "claims", multiple=True, help="Accepted Claim backing identity.")
@click.option("--query", "queries", multiple=True, help="Accepted QueryDefinition identity.")
@click.option(
    "--artifact", "artifacts", multiple=True, help="Subject or ClaimType qualified identity."
)
@click.option("--clear-claims", is_flag=True)
@click.option("--clear-queries", is_flag=True)
@click.option("--clear-artifacts", is_flag=True)
@click.option("--currency-policy", type=click.Choice(["warn", "require_current"]), default=None)
@click.option(
    "--backing",
    "backing_digest",
    default=None,
    help="Exact successor artifact digest selected from an ambiguity refusal.",
)
@click.option(
    "--params",
    "parameters",
    multiple=True,
    help="Canonical JSON object corresponding positionally to each --query.",
)
@click.option("--workspace-root", default=".", show_default=True, type=click.Path(file_okay=False))
@click.option("--evaluation-time", default=None, help="Explicit absolute ISO-8601 instant.")
@click.option(
    "--render",
    is_flag=True,
    help="Write the block body as a table or list from its one --query backing.",
)
@click.option("--dry-run", is_flag=True, help="Compute and check the stamp; write nothing.")
@json_option
@handle_errors
def repin_projection(
    file: str,
    block_id: str,
    claims: tuple[str, ...],
    queries: tuple[str, ...],
    artifacts: tuple[str, ...],
    clear_claims: bool,
    clear_queries: bool,
    clear_artifacts: bool,
    currency_policy: Literal["warn", "require_current"] | None,
    backing_digest: str | None,
    parameters: tuple[str, ...],
    workspace_root: str,
    evaluation_time: str | None,
    render: bool,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Stamp one block's marker from its backings and declare it to the instance.

    FILE is the page: its workspace path or its file catalog name. The client
    reads the backings and computes the stamp, rewrites the opening marker and
    then declares the block, creating its registration the first time. The
    authored body is kept as written, unless ``--render`` writes it from the
    block's one query backing (a table, or a list when the query projects no
    fields). A body that is still its query's rendering is rendered again, so
    a repin never stamps an old table current.
    """

    if (clear_claims and claims) or (clear_queries and queries) or (clear_artifacts and artifacts):
        raise click.ClickException("a backing category cannot be cleared and replaced together")
    if backing_digest is not None and (
        claims
        or queries
        or parameters
        or artifacts
        or clear_claims
        or clear_queries
        or clear_artifacts
    ):
        raise click.ClickException(
            "--backing cannot be combined with --claim, --query, or --params"
        )
    if parameters and len(parameters) != len(queries):
        raise click.ClickException("--params must appear once for each --query or not at all")
    resolved: list[tuple[str, Mapping[str, object]]] = []
    for index, name in enumerate(queries):
        if parameters:
            try:
                payload = json.loads(parameters[index])
                if (
                    not isinstance(payload, dict)
                    or canonical_bytes(payload).decode() != parameters[index]
                ):
                    raise ValueError("expected one canonical JSON object")
            except (CanonicalEncodingError, ValueError, TypeError) as exc:
                raise click.ClickException(
                    f"--params for query {name!r} is not canonical JSON"
                ) from exc
        else:
            payload = {}
        resolved.append((name, payload))
    try:
        instant = (
            datetime.now(UTC)
            if evaluation_time is None
            else datetime.fromisoformat(evaluation_time.replace("Z", "+00:00"))
        )
    except ValueError as exc:
        raise click.ClickException(
            "--evaluation-time must be an absolute ISO-8601 instant"
        ) from exc
    stamp = _server_call(
        lambda client, instance_id: repin_projection_block(
            client,
            instance_id,
            workspace=workspace_root,
            file=file,
            block_id=block_id,
            claims=claims if claims or clear_claims else None,
            queries=resolved if queries or clear_queries else None,
            artifacts=tuple(parse_artifact_identity(x) for x in artifacts)
            if artifacts or clear_artifacts
            else None,
            currency_policy=currency_policy,
            backing_digest=backing_digest,
            evaluation_time=instant,
            render=render,
            dry_run=dry_run,
        ),
        command_name="cruxible block repin",
    )
    if output_json:
        _emit_json(stamp.model_dump(mode="json"))
        return
    if dry_run:
        click.echo(
            f"Would repin {file}#{block_id} at generation {stamp.declared_generation}; "
            "nothing was written."
        )
        return
    click.echo(f"Repinned {file}#{block_id} at generation {stamp.declared_generation}.")


@block_group.command("sync")
@click.argument("paths", nargs=-1, type=click.Path(dir_okay=False))
@click.option("--all", "all_files", is_flag=True, help="Check every catalog file.")
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The attached workspace whose pages are checked.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Only report each block's drift: re-render nothing, record nothing, write nothing.",
)
@json_option
@handle_errors
def sync_projection(
    paths: tuple[str, ...],
    all_files: bool,
    workspace_root: str,
    check: bool,
    output_json: bool,
) -> None:
    """Check each block against its backings, and re-render what is a projection of state.

    Each block reads unchanged, stale (its backing moved), dirty (its prose
    moved from the stamp: re-check and repin) or skipped (unstamped). A stale
    block whose body is its query's rendering (`block repin --render`) is
    derived from state, so sync re-renders it from the query at the head and
    repins it (rendered); stale prose is only reported, since repinning it is
    the author declaring the text right. Sync then records the declared blocks
    of the pages it checked for block-churn detection. --check is the pure
    report. Exits non-zero on a refusal, which a require_current block's drift
    is.
    """

    result = _server_call(
        lambda client, instance_id: sync_projection_blocks(
            client,
            instance_id,
            workspace=workspace_root,
            paths=paths,
            all_files=all_files,
            converge_at=None if check else datetime.now(UTC),
        ),
        command_name="cruxible block sync",
    )
    _echo_block_sync(result, output_json=output_json)
    if result.has_refusals:
        raise click.exceptions.Exit(1)


@block_group.command("detach")
@click.argument("paths", nargs=-1, required=True, type=click.Path(dir_okay=False))
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The attached workspace the pages belong to.",
)
@change_control_options
@json_option
@handle_errors
def detach_projection(
    paths: tuple[str, ...],
    workspace_root: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Strip blocks' markers from these pages, keeping each body as prose.

    Detaches a block this instance no longer publishes (after `block
    depublish`), one whose every backing is retired, or one that belongs to
    another instance. A block the instance still publishes is refused with
    `block_still_published`; depublish it first. Edits pages only, never
    governed state. ``--dry-run`` reports what would change; ``--commit --at``
    commits only if the pages still hold the bytes the preview read.
    """

    root = Path(workspace_root).expanduser().resolve()
    pages = tuple(Path(item).expanduser().resolve() for item in paths)
    result = _server_call(
        lambda client, instance_id: detach_projection_pages(
            client, instance_id, root=root, pages=pages, dry_run=dry_run, at=at
        ),
        command_name="cruxible block detach",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_block_sync(result.sync, output_json=False)
        echo_preview_next(result.status, result.coordinate)
    if result.sync.has_refusals:
        raise click.exceptions.Exit(1)


def _echo_block_sync(result: BlockSyncResult, *, output_json: bool) -> None:
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for item in result.items:
        target = item.path
        if item.block_id is not None:
            target += f"#{item.block_id}"
        suffix = "" if item.reason is None else f":{item.reason}"
        click.echo(f"{target}: {item.outcome}{suffix}")
        if item.repair is not None:
            click.echo(f"  repair: {_block_repair_line(item.repair, path=item.path)}")


def _block_repair_line(repair: Any, *, path: str | None = None) -> str:
    """A block repair as the command that performs it, in the files vocabulary.

    The served arguments keep their wire names (``source_id`` is the catalog
    file name); the line is the CLI form, ``block repin FILE BLOCK_ID``, naming
    the page by ``path`` (the workspace path people read and type) when known.
    """

    if not isinstance(repair, RepairOperation):
        return render_repair(repair, surface="cli")
    values = repair.arguments
    operation = normalize_code(repair.operation)
    if operation == "cruxible.block.repin":
        file, block = path or values.get("source_id"), values.get("block_id")
        if not isinstance(file, str) or not isinstance(block, str):
            return render_repair(repair, surface="cli")
        parts = ["cruxible block repin", shlex.quote(file), shlex.quote(block)]
        claims = values.get("claim")
        for claim in claims if isinstance(claims, list) else ():
            parts.extend(["--claim", shlex.quote(str(claim))])
        if values.get("clear_claims") is True:
            parts.append("--clear-claims")
        candidates = values.get("backing_candidates")
        if isinstance(candidates, list) and candidates:
            parts.append("--backing '<DIGEST>'")
            line = " ".join(parts)
            return f"{line}  (<DIGEST>: one of {', '.join(map(str, candidates))})"
        return " ".join(parts)
    if operation == "cruxible.block.sync" and values.get("all") is True:
        return "cruxible block sync --all"
    if operation == "cruxible.block.depublish":
        file, block = values.get("source_id"), values.get("block_id")
        if isinstance(file, str) and isinstance(block, str):
            return f"cruxible block depublish {shlex.quote(file)} {shlex.quote(block)}"
        return render_repair(repair, surface="cli")
    paths = values.get("paths")
    if operation == "cruxible.block.detach" and isinstance(paths, list) and paths:
        return " ".join(["cruxible block detach", *(shlex.quote(str(path)) for path in paths)])
    return render_repair(repair, surface="cli")


def _echo_list_continuation(next_cursor: str | None) -> None:
    if next_cursor is not None:
        click.echo(f"Truncated. Next: --cursor {next_cursor}")


@playbill_group.group("compiler")
def compiler_group() -> None:
    """Explicit, reviewed changes to the instance's accepted compiler."""


@compiler_group.command("upgrade")
@click.option(
    "--to",
    "target_digest",
    default=None,
    help="Target compiler digest (default: the compiler the daemon runs).",
)
@click.option(
    "--name",
    "proposal_name",
    default=None,
    help="Proposal name (default: upgrade-to-<revision label>).",
)
@click.option(
    "--key",
    "private_key_path",
    default=None,
    envvar=PRINCIPAL_KEY_ENV,
    type=click.Path(dir_okay=False),
    help=(
        "Your private key (also CRUXIBLE_PRINCIPAL_KEY; default: the key your remembered "
        "settings or `cruxible init` keep). With it the upgrade is signed and activated "
        "in this command when no other principal's approval is needed."
    ),
)
@click.option(
    "--no-accept",
    is_flag=True,
    help="Only propose; sign and activate later with `cruxible proposal approve`.",
)
@change_control_options
@json_option
@handle_errors
def propose_compiler_upgrade(
    target_digest: str | None,
    proposal_name: str | None,
    private_key_path: str | None,
    no_accept: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose the compiler upgrade, and sign and activate it when nothing else is needed.

    \b
    compiler upgrade                    preview, then (at a terminal) ask to commit
    compiler upgrade --yes              commit without asking
    compiler upgrade --commit --at OID  commit exactly the preview at OID
    compiler upgrade --no-accept        propose only

    With no --to it targets the compiler the daemon runs; the proposal pins its
    full digest. When you hold a signing key and the approval policy needs no
    other principal, committing proposes, signs with your key and activates:
    activation is one-way, so the command previews first. Otherwise it proposes
    and the result names why it stopped and the next step.
    """
    from cruxible_client.contracts.types import CompilerCoordinate

    def call(client: CruxibleClient, instance_id: str) -> CompilerUpgradeResult:
        head = client.head(instance_id)
        signer_id = None if no_accept else client.whoami(instance_id).actor_id
        key = (
            None
            if signer_id is None
            else _signing_key(private_key_path, instance_id=instance_id, signer_id=signer_id)
        )
        proposed = client.propose_compiler_upgrade(
            instance_id,
            target=None if target_digest is None else CompilerCoordinate(rule_digest=target_digest),
            base=contracts.AcceptedCoordinate.model_validate(
                head.coordinate.model_dump(mode="json")
            ),
            proposal_name=proposal_name,
            # Signing activates, and activation cannot be undone: preview first.
            dry_run=True if dry_run is None and key is not None else dry_run,
            at=at,
        )
        if proposed.status != "admitted":
            return CompilerUpgradeResult(proposal=proposed, signing_key=key)
        return _land_compiler_upgrade(
            client,
            instance_id,
            proposed,
            key=key,
            no_accept=no_accept,
            signer_id=signer_id,
        )

    result = _server_call(call, command_name="cruxible compiler upgrade")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_compiler_upgrade(result)


def _signing_key(
    explicit: str | None, *, instance_id: str, signer_id: str
) -> CompilerUpgradeSigningKey | None:
    """The key that signs as ``signer_id``, by the lookup every client shares.

    `--key`; CRUXIBLE_PRINCIPAL_KEY unless the process acts as another
    principal; the signer's remembered settings; the folder record when it
    names the signer; the default key directory `cruxible init` uses. Never
    another principal's key: `--signer-id reviewer` from the owner's shell
    finds the reviewer's remembered key or refuses with the repair. The path
    stays on this client.
    """

    obj = _root_ctx_obj()
    from_option = (
        bool(explicit)
        and click.get_current_context().get_parameter_source("private_key_path")
        is not click.core.ParameterSource.ENVIRONMENT
    )
    workspace = obj.get("playbill_workspace")
    found = principal_key(
        signer_id,
        instance_id=instance_id,
        server_url=obj.get("server_url"),
        server_socket=obj.get("server_socket"),
        explicit=explicit if from_option else None,
        folder=(
            Path(str(workspace))
            if workspace is not None and obj.get("target_instance_source") == "workspace"
            else None
        ),
    )
    if found is None:
        return None
    source = found.source
    settings = found.settings_path
    if source == "environment" and obj.get("principal_key_source") is not None:
        # The CLI exported it from the acting principal's settings or folder record.
        source = obj["principal_key_source"]
        settings = obj.get("principal_settings")
    obj["signing_key_settings"] = None if settings is None else str(settings)
    return CompilerUpgradeSigningKey(
        signer_id=signer_id, private_key_path=str(found.private_key_path), source=source
    )


def _missing_signing_key_help(instance_id: str, signer_id: str) -> str:
    """No key signs as the signer here: the repairs, never another principal's key."""

    del instance_id
    return principal_key_missing(signer_id, client="cli")


def _signing_key_line(key: CompilerUpgradeSigningKey) -> str:
    settings = _root_ctx_obj().get("signing_key_settings")
    where = {
        "option": "--key",
        "environment": "CRUXIBLE_PRINCIPAL_KEY",
        "remembered_settings": "remembered settings" + (f" {settings}" if settings else ""),
        "folder": "the folder's identity record",
        "default_key_dir": "the default key directory",
    }[key.source]
    return f"Signing key for {key.signer_id}: {key.private_key_path} (from {where})"


def _land_compiler_upgrade(
    client: CruxibleClient,
    instance_id: str,
    proposed: contracts.ProposalInspection,
    *,
    key: CompilerUpgradeSigningKey | None,
    no_accept: bool,
    signer_id: str | None,
) -> CompilerUpgradeResult:
    """Sign and activate an admitted upgrade when policy and caller allow; else say why not.

    The caller's own signature is the one settlement requires when the policy
    asks for no independent approval; under one, the creator never counts, so
    nothing is signed and the eligible approver's `proposal approve` lands it.
    """
    proposal_id = _admitted_proposal_id(proposed)
    candidate = proposed.proposal.get("candidate")
    stop: CompilerUpgradeProposed | None = None
    if proposal_id is None or not isinstance(candidate, Mapping):
        stop = CompilerUpgradeProposed(
            proposal_id=proposal_id,
            reason="refused",
            message="not activated: the upgrade was evaluated and refused",
            next=None if proposal_id is None else f"cruxible get {proposal_id}",
        )
    elif no_accept:
        stop = CompilerUpgradeProposed(
            proposal_id=proposal_id,
            reason="accept_never",
            message="not activated: --no-accept proposes only",
            next=f"cruxible proposal approve {proposal_id}",
        )
    elif candidate.get("approval_requirements"):
        stop = CompilerUpgradeProposed(
            proposal_id=proposal_id,
            reason="independent_approval_required",
            message=(
                "not activated: the approval policy needs an ordinary principal other than "
                "the proposer; that principal's `cruxible proposal approve` activates it"
            ),
            next=f"cruxible proposal approve {proposal_id} --signer-id {_SIGNER_PLACEHOLDER}",
        )
    elif key is None or signer_id is None:
        stop = CompilerUpgradeProposed(
            proposal_id=proposal_id,
            reason="signing_key_missing",
            message="not activated: "
            + (
                _missing_signing_key_help(instance_id, signer_id)
                if signer_id is not None
                else "the credential acts as no principal, so nothing can be signed"
            ),
            next=f"cruxible proposal approve {proposal_id} --key PATH",
        )
    if stop is not None:
        return CompilerUpgradeResult(proposal=proposed, signing_key=key, outcome=stop)
    assert proposal_id is not None and key is not None and signer_id is not None
    approval = _approve_with_key(
        client,
        instance_id,
        proposal_id,
        signer_id=signer_id,
        private_key_path=Path(key.private_key_path),
    )
    activation = approval if isinstance(approval, contracts.ActivationReceipt) else None
    if isinstance(approval, contracts.ApprovalReceipt):
        activation = approval.activation
    outcome: CompilerUpgradeLanded | CompilerUpgradeProposed
    if isinstance(approval, contracts.ProposalRecompiled):
        outcome = CompilerUpgradeProposed(
            proposal_id=approval.proposal_id,
            reason="recompiled",
            message="not activated: the proposal was recompiled at a new head",
            next=f"cruxible proposal review {approval.proposal_id}",
        )
    elif (
        activation is not None
        and activation.accepted_coordinate is not None
        and (activation.status == "accepted")
    ):
        outcome = CompilerUpgradeLanded(
            proposal_id=proposal_id, accepted_coordinate=activation.accepted_coordinate
        )
    elif activation is not None or (
        isinstance(approval, contracts.ApprovalReceipt) and approval.not_activated == "stale"
    ):
        outcome = CompilerUpgradeProposed(
            proposal_id=proposal_id,
            reason="head_moved",
            message="not activated: accepted state moved after this proposal was checked",
            next=f"cruxible proposal readmit {proposal_id}",
        )
    elif isinstance(approval, contracts.ApprovalReceipt) and (
        approval.not_activated == "activation_not_permitted"
    ):
        outcome = CompilerUpgradeProposed(
            proposal_id=proposal_id,
            reason="activation_not_permitted",
            message="signed, not activated: activating a compiler upgrade requires admin",
            next=f"an admin runs cruxible proposal activate {proposal_id}",
        )
    else:
        remaining = (
            approval.approvals_remaining if isinstance(approval, contracts.ApprovalReceipt) else 0
        )
        outcome = CompilerUpgradeProposed(
            proposal_id=proposal_id,
            reason="approvals_remaining",
            message=f"signed, not activated: {remaining} more approval(s) required",
            next=f"cruxible proposal approve {proposal_id}",
        )
    return CompilerUpgradeResult(
        proposal=proposed, signing_key=key, approval=approval, outcome=outcome
    )


def _echo_compiler_upgrade(result: CompilerUpgradeResult) -> None:
    """The proposal, its one-way notice, the key used, and what happened or what is next."""
    inspection = result.proposal
    notice = inspection.compiler_upgrade
    if notice is not None:
        source = notice.source_revision or notice.source_compiler_digest
        target = notice.target_revision or notice.target_compiler_digest
        click.echo(f"Compiler upgrade: {source} -> {target}")
        click.echo(f"Target compiler: {notice.target_compiler_digest}")
        click.echo(notice.message)
    if result.signing_key is not None:
        click.echo(_signing_key_line(result.signing_key))
    outcome = result.outcome
    if outcome is None:
        if result.signing_key is not None and inspection.status == "would_propose":
            candidate = inspection.proposal.get("candidate")
            if isinstance(candidate, Mapping) and candidate.get("approval_requirements"):
                click.echo("On commit: proposes only; the approval policy needs another principal.")
            else:
                click.echo("On commit: proposes, signs with this key and activates.")
        _echo_proposal(inspection, inspection.lint)
        return
    admission = inspection.proposal.get("admission") or {}
    evaluation = inspection.proposal.get("evaluation") or {}
    verdict = evaluation.get("verdict", "-") if isinstance(evaluation, Mapping) else "-"
    proposal_id = admission.get("proposal_id") if isinstance(admission, Mapping) else None
    click.echo(f"{inspection.status}  {proposal_id or '-'}  verdict={verdict}")
    diagnostics = evaluation.get("diagnostics", ()) if isinstance(evaluation, Mapping) else ()
    for item in diagnostics:
        if isinstance(item, Mapping):
            click.echo(f"  {item.get('code')}: {printable(str(item.get('message', '')))}")
    if isinstance(outcome, CompilerUpgradeLanded):
        click.echo(f"Activated at {outcome.accepted_coordinate.git_oid}")
        return
    click.echo(printable(outcome.message))
    if outcome.next is not None:
        click.echo(f"Next: {outcome.next}")


class _QueryCommand(click.Command):
    """``cruxible query [KIND] ...``: KIND, when given, comes before every option."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if not args:
            click.echo(ctx.get_help(), color=ctx.color)
            ctx.exit()
        ctx.meta["playbill_query_args"] = list(args)
        if not args[0].startswith("-"):
            ctx.meta["playbill_query_kind"] = args[0]
            args = args[1:]
        return super().parse_args(ctx, args)


def _split_fields(values: Sequence[str]) -> list[str]:
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def _query_param_value(raw: str) -> object:
    try:
        decoded = json.loads(raw)
    except ValueError:
        return raw
    # JSON null binds an optional parameter explicitly (omitting it takes the
    # default); pass '"null"' for the four-letter string.
    return decoded if decoded is None or isinstance(decoded, str | int | bool) else raw


def _without_cursor(args: Sequence[str]) -> list[str]:
    kept: list[str] = []
    skip = False
    for item in args:
        if skip:
            skip = False
            continue
        if item == "--cursor":
            skip = True
            continue
        if item.startswith("--cursor="):
            continue
        kept.append(item)
    return kept


def _validation_problems(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'request'}: {error['msg']}"
        for error in exc.errors()
    )


_FOLLOW_OPTIONS = {"--follow": "forward", "--follow-in": "reverse"}


def _follow_order(
    raw: Sequence[str], forward: Sequence[str], reverse: Sequence[str]
) -> list[tuple[str, str]]:
    """Every follow as (option, spec), in command-line order.

    The order is read back from the raw arguments and used only when it names
    exactly the specs click parsed; otherwise forward follows come first.
    """

    parsed = [
        *(("--follow", item) for item in forward),
        *(("--follow-in", item) for item in reverse),
    ]
    seen: list[tuple[str, str]] = []
    index = 0
    while index < len(raw):
        token = raw[index]
        name, equals, value = token.partition("=")
        if name in _FOLLOW_OPTIONS and equals:
            seen.append((name, value))
        elif token in _FOLLOW_OPTIONS and index + 1 < len(raw):
            seen.append((token, raw[index + 1]))
            index += 1
        index += 1
    if all(
        [item for item in seen if item[0] == option] == [i for i in parsed if i[0] == option]
        for option in _FOLLOW_OPTIONS
    ):
        return seen
    return parsed


def _follow_entry(spec: str, option: str) -> dict[str, str]:
    field, _, alias = spec.partition(":")
    if not field or not alias:
        example = "project.task.part_of:task" if option == "--follow-in" else "closed_by:batch"
        raise click.BadParameter(
            f"{spec!r} is not field:alias, for example {example}", param_hint=option
        )
    entry = {"field": field, "as": alias}
    if _FOLLOW_OPTIONS[option] == "reverse":
        entry["direction"] = "reverse"
    return entry


@playbill_group.command("query", cls=_QueryCommand, context_settings={"allow_extra_args": True})
@click.option(
    "--where",
    "where_expressions",
    multiple=True,
    help=f"Filter, repeatable (all-of): {WHERE_SYNTAX}.",
)
@click.option("--contains", default=None, help="Case-insensitive text in any live Claim value.")
@click.option("--select", "select_fields", multiple=True, help="Columns: a,b (repeatable).")
@click.option(
    "--follow",
    "follow_specs",
    multiple=True,
    help="Follow a relation forward (repeatable): field:alias, a predicate of KIND.",
)
@click.option(
    "--follow-in",
    "follow_in_specs",
    multiple=True,
    help=(
        "Follow a relation backwards (repeatable): field:alias, another kind's predicate "
        "that points at KIND, e.g. project.task.part_of:task."
    ),
)
@click.option(
    "--order-by",
    "order_fields",
    multiple=True,
    help="Order: f or -f (repeatable); -last_changed first lists recent changes first.",
)
@click.option(
    "--limit",
    type=click.IntRange(1, contracts.QUERY_ANSWER_MAX_RESULTS),
    default=None,
    help=(
        f"Rows to show (default {contracts.QUERY_DEFAULT_LIMIT}); past "
        f"{contracts.QUERY_MAX_LIMIT} the CLI follows the cursor page by page."
    ),
)
@click.option(
    "--all",
    "all_rows",
    is_flag=True,
    help=(
        "Follow the cursor to the end of the answer "
        f"(at most {contracts.QUERY_ANSWER_MAX_RESULTS} rows)."
    ),
)
@click.option("--cursor", default=None, help="Continue a truncated page.")
@click.option(
    "--spec",
    "spec_path",
    type=PayloadFile(),
    default=None,
    help="A QueryDefinitionSpec file (JSON or YAML).",
)
@click.option(
    "--status",
    "statuses",
    multiple=True,
    type=click.Choice(["live", "ineligible", "overturned", "retired"]),
    help="Which Claims cells show (repeatable): live (default), overturned, ineligible, retired.",
)
@click.option("--claims", "with_claims", is_flag=True, help="Also show each cell's Claims.")
@click.option("--name", "query_name", default=None, help="Run an accepted named query.")
@click.option("--param", "param_pairs", multiple=True, help="Named query parameter k=v.")
@click.option(
    "--budgets",
    "budgets_json",
    default=None,
    help='Named query budgets as JSON, e.g. \'{"max_results": 100, "max_traversal_depth": 0}\'.',
)
@click.option(
    "--receipt",
    type=click.Choice(["compact", "full"]),
    default="compact",
    help="full adds a named query's replay receipt (Claims read, paths, verdict).",
)
@click.option(
    "--at",
    "at_oid",
    default=None,
    help=(
        "Read at this accepted git oid (or a unique 12+ hex prefix), or a generation "
        "number (all digits, 11 or fewer, is always a generation)."
    ),
)
@click.option("--evaluation-time", default=None, help="ISO-8601 instant; default now.")
@json_option
@click.pass_context
@handle_errors
def query_group(
    ctx: click.Context,
    where_expressions: tuple[str, ...],
    contains: str | None,
    select_fields: tuple[str, ...],
    follow_specs: tuple[str, ...],
    follow_in_specs: tuple[str, ...],
    order_fields: tuple[str, ...],
    limit: int | None,
    all_rows: bool,
    cursor: str | None,
    spec_path: str | None,
    statuses: tuple[str, ...],
    with_claims: bool,
    query_name: str | None,
    param_pairs: tuple[str, ...],
    budgets_json: str | None,
    receipt: str,
    at_oid: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Query accepted state: cruxible query [KIND] [--where 'f=v']...

    This answers one query and prints its values as a table with flags, then
    the next command when the page is truncated. KIND is a Subject kind or a
    definition kind: ClaimType, Procedure, Trigger, Line, SubjectKind,
    CaptureContract, ProcedureMandate or QueryDefinition (each lists its
    definitions with typed columns; --select adds the rest, such as a
    ClaimType's role, stale_after or kit, or a Trigger's cron); --name runs
    an accepted named query (orient --section queries lists them), --spec a
    full definition. A page holds at most 500 rows: --limit above that, or
    --all, follows the cursor and prints the pages as one table.
    """

    if all_rows and limit is not None:
        raise click.UsageError("pass --limit or --all, not both", ctx=ctx)
    if ctx.args:
        raise click.UsageError(
            f"{ctx.args[0]!r} is not an option; put KIND first: cruxible query KIND [--where ...]",
            ctx=ctx,
        )
    try:
        where = [parse_where(expression) for expression in where_expressions]
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--where") from exc
    follow = [
        _follow_entry(spec, option)
        for option, spec in _follow_order(
            ctx.meta.get("playbill_query_args", []), follow_specs, follow_in_specs
        )
    ]
    params: dict[str, object] | None = None
    if param_pairs:
        params = {}
        for pair in param_pairs:
            key, sep, raw = pair.partition("=")
            if not sep or not key:
                raise click.BadParameter(f"{pair!r} is not k=v", param_hint="--param")
            params[key] = _query_param_value(raw)
    spec: object = None
    if spec_path is not None:
        from cruxible_client.contracts.query.definitions import (
            QueryDefinitionSpec,
        )

        try:
            spec = TypeAdapter(QueryDefinitionSpec).validate_python(_read_mapping(spec_path))
        except ValidationError as exc:
            raise click.ClickException(
                f"{spec_path} is not a QueryDefinitionSpec: {_validation_problems(exc)}"
            ) from exc
    fields: dict[str, object] = {
        "kind": ctx.meta.get("playbill_query_kind"),
        "where": [item.model_dump(mode="json", by_alias=True) for item in where],
        "contains": contains,
        "select": _split_fields(select_fields),
        "follow": follow,
        "order_by": _split_fields(order_fields),
        "cursor": cursor,
        "spec": spec,
        "name": query_name,
        "params": params,
        "claims": with_claims,
        "receipt": receipt,
        "at": at_oid,
        "evaluation_time": None if evaluation_time is None else parse_datetime(evaluation_time),
    }
    wanted = contracts.QUERY_ANSWER_MAX_RESULTS if all_rows else limit
    if wanted is not None:
        fields["limit"] = min(wanted, contracts.QUERY_MAX_LIMIT)
    if statuses:
        fields["status"] = list(dict.fromkeys(statuses))
    if budgets_json is not None:
        try:
            fields["budgets"] = json.loads(budgets_json)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--budgets") from exc
    try:
        request = contracts.QueryRequest.model_validate(fields)
    except ValidationError as exc:
        raise click.ClickException(f"invalid query: {_validation_problems(exc)}") from exc
    result = _server_call(
        lambda client, instance_id: _query_rows(client, instance_id, request, wanted=wanted),
        command_name="cruxible query",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(render_query_table(result))
    for row in result.rows if with_claims else ():
        cells = row.get("claims") or {}
        for column, entries in cells.items() if isinstance(cells, dict) else ():
            for entry in entries if isinstance(entries, list) else ():
                claim = QueryClaim.model_validate(entry)
                click.echo(
                    f"claim {row.get('subject', '')} {column}: {claim.claim} "
                    f"{claim.status} {claim.verdict} {claim.role}"
                )
    if result.truncated and result.next_cursor is not None:
        again = _without_cursor(ctx.meta.get("playbill_query_args", []))
        click.echo(
            f"truncated: {result.remaining} more row{'' if result.remaining == 1 else 's'}; "
            "next: cruxible query "
            + " ".join(shlex.quote(item) for item in again)
            + f" --cursor {result.next_cursor}"
        )
    elif result.truncated:
        click.echo(
            f"truncated: capped at {', '.join(result.capped) or 'the server cap'}; "
            "narrow the query with --where"
        )


def _query_rows(
    client: CruxibleClient,
    instance_id: str,
    request: contracts.QueryRequest,
    *,
    wanted: int | None,
) -> contracts.QueryResultRecord:
    """Up to ``wanted`` rows of one answer: its pages, following the cursor, as one page.

    ``None`` reads the one page the request names.

    Every page is read at the coordinate and evaluation time the first page
    served (the cursor pins both), so the rows are one answer. The result is the
    last page with every row read before it; its cursor continues after them.
    """

    page = client.query(instance_id, request=request)
    rows = list(page.rows)
    while (
        wanted is not None
        and page.truncated
        and page.next_cursor is not None
        and len(rows) < wanted
    ):
        step = min(wanted - len(rows), contracts.QUERY_MAX_LIMIT)
        page = client.query(
            instance_id,
            request=request.model_copy(update={"cursor": page.next_cursor, "limit": step}),
        )
        rows.extend(page.rows)
    if len(rows) == len(page.rows):
        return page
    return page.model_copy(update={"rows": tuple(rows)})


@playbill_group.group("procedure")
def procedure_group() -> None:
    """Run and measure accepted Procedures; read one with cruxible get Procedure:<name>."""


def _cli_repair(repair: Any) -> str:
    """Render a served repair as the CLI command that performs it."""

    return render_repair(repair, surface="cli")


def _inbox_counts_text(counts: Any) -> str:
    from cruxible_client.contracts.text_display import inbox_counts_text

    return inbox_counts_text(counts)


def _counts_text(counts: Any) -> str:
    """A run's counts on one line; incompleteness and refusals named."""

    from cruxible_client.contracts.text_display import run_counts_text

    return run_counts_text(counts)


def _echo_run_outcome(result: contracts.ProcedureRunState, label: str) -> None:
    """Lead with the answer: the result on success, the code and repair on refusal."""

    counts = getattr(result, "counts", None)
    attention = counts is not None and counts.needs_attention
    click.echo(f"{label}: {result.status}" + ("  (needs attention)" if attention else ""))
    if counts is not None:
        click.echo("Counts: " + _counts_text(counts))
    if result.status == "succeeded" and result.result is not None:
        click.echo("Result: " + json.dumps(result.result, sort_keys=True))
    terminal = result.terminal
    code = getattr(terminal, "code", None)
    if code is not None:
        click.echo(f"Refused: {code}: {getattr(terminal, 'message', '')}")
        details = getattr(terminal, "details", None)
        if isinstance(details, dict) and details.get("field_path"):
            click.echo(f"Field: {details['field_path']}")
        repair = getattr(terminal, "repair", None)
        if repair is not None:
            click.echo(f"Repair: {_cli_repair(repair)}")
        elif isinstance(details, dict) and isinstance(details.get("repair"), str):
            click.echo(f"Repair: {details['repair']}")
        cause = getattr(terminal, "cause", None)
        if cause is not None:
            # The Provider's own account: a transport error and an HTTP status
            # differ in kind, which the code alone does not say.
            click.echo(
                f"Cause: {cause.node_id}: {cause.kind or cause.provider_code}: {cause.message}"
            )
    elif isinstance(terminal, ProcedureHaltTerminal) and terminal.reason:
        click.echo(f"Halted at {terminal.node_id}: {terminal.reason}")
    click.echo(f"Next: {result.next_operation['kind']}")


#: A run that ends in one of these exits 1, after its output, so `&&` stops.
_FAILED_RUN_STATUSES = frozenset(
    {"admission_refused", "node_refused", "operational_failed", "internal_failed"}
)


def _exit_for_run(result: contracts.ProcedureRunState) -> None:
    """Exit 1 when the run refused or failed; succeeded, halted and running exit 0."""

    if result.status in _FAILED_RUN_STATUSES:
        raise SystemExit(1)


def _echo_source_observations(result: contracts.ProcedureRunState) -> None:
    """Print what each admitted Source occurrence really observed.

    A run with no Source occurrence prints nothing extra. `--json` already
    carries the whole receipt; this is the one-glance version of it.
    """

    for observation in result.source_observations:
        receipt = observation.source_read_receipt
        where = observation.input_name or observation.occurrence_path
        if receipt is not None:
            click.echo(
                f"Read {where}: {receipt.relative_path} "
                f"({receipt.byte_length} bytes, {receipt.bytes_digest})"
            )
        if observation.capture_digest is not None:
            click.echo(f"Capture {where}: {observation.capture_digest}")
    for node in result.nodes:
        click.echo("Node " + _run_node_line(node.model_dump(mode="json")))
    _echo_terminal_egress(result)


#: A delivered proposal's changed item paths printed before the rest are counted.
_EGRESS_CHANGED_PATHS_SHOWN = 10


def _proposed_children_lines(
    proposal_id: str, children: Sequence[ProcedureTerminalEgressChild]
) -> list[str]:
    """A delivered proposal's items: one counts line, every item matching an earlier
    refusal, the first changed paths, then where the rest are."""

    if not children:
        return []
    changed = [child for child in children if not child.unchanged]
    refused = [child for child in children if child.prior_refusals]
    counts = [f"{len(changed)} changed"]
    if len(changed) != len(children):
        counts.append(f"{len(children) - len(changed)} unchanged")
    if refused:
        counts.append(f"{len(refused)} with earlier refusals")
    total = len(children)
    lines = [f"  {total} item{'s' if total != 1 else ''}: {', '.join(counts)}"]
    for child in refused:
        count = len(child.prior_refusals)
        lines.append(
            f"  {child.path or child.item_key} ({count} earlier refusal"
            f"{'s' if count != 1 else ''} of this statement on this evidence; "
            "see proposal review)"
        )
    listed = [child for child in changed if child.path is not None and not child.prior_refusals]
    lines.extend(f"  {child.path}" for child in listed[:_EGRESS_CHANGED_PATHS_SHOWN])
    if len(listed) > _EGRESS_CHANGED_PATHS_SHOWN:
        lines.append(
            f"  ... and {len(listed) - _EGRESS_CHANGED_PATHS_SHOWN} more: "
            f"cruxible proposal review {proposal_id} (or --json)"
        )
    return lines


def _restated_count(result: contracts.ProcedureRunState, egress: Any) -> int:
    """How many of a claims terminal's items restated accepted state: its summary's count."""

    for node in getattr(result, "nodes", ()):
        summary = getattr(node, "summary", None)
        if node.node == egress.node_id and getattr(summary, "kind", None) == "claims":
            return int(
                summary.re_observed  # type: ignore[union-attr]
                + summary.no_op  # type: ignore[union-attr]
                + sum(summary.reobservations_refused.values())  # type: ignore[union-attr]
            )
    return len(egress.children)


def _echo_terminal_egress(result: contracts.ProcedureRunState) -> None:
    """Print what each terminal of the run did, and the handles a manager needs.

    A delivered `claims` prints the proposal id and the exact
    candidate digest, which are the two arguments the existing proposal
    verbs take; a settled `claims` prints the generation it
    accepted, and one that fell back prints its proposal and why. A capped
    terminal names the authority it needed and the authority the run had; a
    refused one prints the code the run refused with. A Line run whose change
    is its Line's open proposal's says that proposal is still current; one
    whose change differs names the proposal it superseded.
    """

    for egress in result.terminal_egress:
        kept = egress.line_open_proposal
        if kept is not None and kept.outcome == "still_current":
            click.echo(
                f"Terminal {egress.node_id}: no change; open proposal {kept.proposal_id} "
                "still current"
            )
        elif egress.settle_outcome == "settled":
            click.echo(
                f"Settled {egress.node_id}: accepted {egress.accepted_git_oid} "
                f"(proposal {egress.proposal_id})"
            )
        elif egress.verdict == "delivered" and egress.proposal_id is not None:
            outside = (
                "; outside the mandate's scope: "
                + ", ".join(
                    f"{gap.claim_type} {gap.change}"
                    + (f" [{gap.basis}]" if gap.basis else "")
                    + f" ({gap.term})"
                    for gap in egress.outside_scope
                )
                if egress.outside_scope
                else ""
            )
            fallback = (
                f" (settle fell back: {egress.fallback_reason}{outside})"
                if egress.settle_outcome == "proposed"
                else ""
            )
            superseded = (
                f" (superseded {kept.proposal_id})"
                if kept is not None and kept.outcome == "superseded"
                else ""
            )
            click.echo(
                f"Proposal {egress.node_id}: {egress.proposal_id} "
                f"candidate {egress.candidate_digest}{fallback}{superseded}"
            )
            for line in _proposed_children_lines(egress.proposal_id, egress.children):
                click.echo(line)
        elif (
            egress.verdict == "delivered"
            and egress.kind == "claims"
            and all(child.unchanged for child in egress.children)
            and (count := _restated_count(result, egress)) > 0
        ):
            # Every item restates accepted state, so no proposal was needed. An
            # item restating exactly what is live has no child at all (ruling
            # 1e): the count is the node's summary's, not the children's.
            click.echo(
                f"Terminal {egress.node_id}: nothing changed ({count} item"
                f"{'s' if count != 1 else ''} restate{'' if count != 1 else 's'} accepted state)"
            )
        elif egress.verdict == "delivered":
            click.echo(f"Terminal {egress.node_id}: {egress.kind} delivered")
        elif egress.verdict == "refused_effective_authority":
            click.echo(
                f"Terminal {egress.node_id}: {egress.kind} needs {egress.required_authority}; "
                f"the {egress.limiting_term} term allowed {egress.effective_authority}"
            )
        else:
            code = egress.refusal_code or egress.verdict
            click.echo(f"Terminal {egress.node_id}: {egress.kind} {egress.verdict} ({code})")


@procedure_group.command("run")
@click.option("--bind", "bind_values", multiple=True, help="Bind a Provider slot: slot=provider.")
@click.argument("name")
@click.argument("input_file", type=PayloadFile())
@click.option(
    "--evaluation-time",
    default=None,
    help=(
        "Explicit ISO-8601 evaluation time; default: the instant the daemon admits the run. "
        "With the same current Procedure and matching input, an explicit time may reuse "
        "a retained Source observation under its replayability and age policy."
    ),
)
@click.option(
    "--at",
    "at_file",
    default=None,
    type=PayloadFile(),
    help=(
        "AcceptedCoordinate JSON/YAML file (- for stdin) to bind the run to; it runs only "
        "while the Procedure is still current there."
    ),
)
@click.option(
    "--resolution-contract",
    "contract_file",
    type=PayloadFile(),
    help="Exact accepted ResolutionContract reference JSON/YAML.",
)
@click.option(
    "--trigger-event",
    "event_file",
    type=PayloadFile(),
    help="Exact retained Capture event reference JSON/YAML.",
)
@click.option(
    "--parameters",
    "parameters_file",
    type=PayloadFile(),
    help=(
        "The Procedure's parameter values JSON/YAML, over its parameter_defaults "
        "(checked against its params Contract)."
    ),
)
@json_option
@handle_errors
def run_procedure(
    name: str,
    bind_values: tuple[str, ...],
    input_file: str,
    evaluation_time: str | None,
    at_file: str | None,
    output_json: bool,
    contract_file: str | None,
    event_file: str | None,
    parameters_file: str | None,
) -> None:
    """Run an accepted Procedure.

    Exits 1 after its output (`--json` too) when the run refused or failed:
    admission_refused, node_refused, operational_failed, internal_failed.
    """

    bindings = {}
    for binding in bind_values:
        slot, sep, provider = binding.partition("=")
        if not sep or not slot or not provider or slot in bindings:
            raise click.BadParameter("use one slot=provider per slot", param_hint="--bind")
        bindings[slot] = provider
    resolution_contract = (
        None if contract_file is None else _read_model(contract_file, ResolutionContractReference)
    )
    trigger_event = None if event_file is None else _read_model(event_file, TriggerEventReference)
    at = None if at_file is None else _read_model(at_file, AcceptedCoordinate)
    result = _server_call(
        lambda client, instance_id: client.run_procedure(
            instance_id,
            name,
            evaluation_time=evaluation_time,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
            at=None if at is None else at.model_dump(mode="json"),
            input=_read_mapping(input_file),
            **({"parameters": _read_mapping(parameters_file)} if parameters_file else {}),
            **({"bindings": bindings} if bindings else {}),
        ),
        command_name="cruxible procedure run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        _exit_for_run(result)
        return
    _echo_run_outcome(result, result.run_id or name)
    if result.receipt_digest is not None:
        click.echo(f"Receipt: {result.receipt_digest}")
    _echo_source_observations(result)
    _exit_for_run(result)


@procedure_group.command("measure")
@click.argument("name")
@click.option("--run-id", default=None, help="Credit this finalized run's exact grain.")
@click.option(
    "--measurement",
    "measurements",
    multiple=True,
    help="Evaluate only these declared measurements (default: every declaration).",
)
@click.option(
    "--evaluation-time",
    default=None,
    help="Explicit ISO-8601 OBSERVATION instant (default: now).",
)
@click.option(
    "--at",
    "at_file",
    default=None,
    type=PayloadFile(),
    help="AcceptedCoordinate JSON/YAML file naming the OBSERVATION coordinate.",
)
@json_option
@handle_errors
def procedure_measure(
    name: str,
    run_id: str | None,
    measurements: tuple[str, ...],
    evaluation_time: str | None,
    at_file: str | None,
    output_json: bool,
) -> None:
    """Evaluate due measurements from real evidence; retry replays, never duplicates."""

    at = None if at_file is None else _read_model(at_file, AcceptedCoordinate)
    request = contracts.ProcedureMeasureRequest(
        run_id=run_id,
        measurement_names=tuple(sorted(set(measurements), key=lambda item: item.encode())),
        evaluation_time=(
            None
            if evaluation_time is None
            else datetime.fromisoformat(evaluation_time.replace("Z", "+00:00"))
        ),
        at=at,
    )
    result = _server_call(
        lambda client, instance_id: client.measure_procedure(instance_id, name, request=request),
        command_name="cruxible procedure measure",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for row in result.rows:
        line = f"{row.measurement_name}: {row.status}"
        if row.resolution is not None:
            line += f" ({row.resolution.verdict}, {row.resolution.resolution_id})"
        if row.reading_status != "not_requested":
            line += f" reading={row.reading_status}"
            if row.reading is not None:
                line += f" {row.reading.reading_id}"
        click.echo(line)
        if row.detail:
            click.echo(f"  {row.detail}")


@procedure_group.command("dispose")
@click.argument("name")
@click.argument("resolution_id")
@click.option("--verdict", required=True, type=click.Choice(["upheld", "overturned"]))
@click.option("--reason", required=True, help="Your review reason, recorded verbatim.")
@click.option("--json", "output_json", is_flag=True, help="Emit the typed result as JSON.")
@handle_errors
def procedure_dispose(
    name: str,
    resolution_id: str,
    verdict: Literal["upheld", "overturned"],
    reason: str,
    output_json: bool,
) -> None:
    """Record a judgment on the latest measurement answer; find IDs with readings or get."""
    try:
        request = contracts.ProcedureResolutionDispositionRequest(
            resolution_id=resolution_id, verdict=verdict, reason=reason
        )
    except ValidationError as exc:
        raise click.ClickException(validation_summary(exc)) from exc
    result = _server_call(
        lambda client, instance_id: client.dispose_procedure_resolution(
            instance_id, name, request=request
        ),
        command_name="cruxible procedure dispose",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"{resolution_id}: {result.disposition.verdict} by {result.disposition.reviewer_id}")
    click.echo(f"Reason: {result.disposition.reason}")
    click.echo(f"Standing answer: {result.standing_resolution_id or 'none'}")


@procedure_group.command("readings")
@click.argument("name")
@click.option("--run-id", default=None, help="Only readings crediting this run.")
@click.option("--measurement", "measurements", multiple=True, help="Only these measurements.")
@click.option(
    "--subject-grain",
    type=click.Choice(["procedure_unit", "node", "arm"]),
    default=None,
    help="Only readings at this grain.",
)
@click.option(
    "--evaluation-time",
    default=None,
    help="Explicit ISO-8601 instant the standing is read at (default: now).",
)
@click.option(
    "--at",
    "at_file",
    default=None,
    type=PayloadFile(),
    help="AcceptedCoordinate JSON/YAML file (- for stdin) to read the Procedure at.",
)
@click.option("--limit", default=50, show_default=True, type=click.IntRange(1, 200))
@click.option("--cursor", default=None, help="Continue a previous page.")
@json_option
@handle_errors
def procedure_readings(
    name: str,
    run_id: str | None,
    measurements: tuple[str, ...],
    subject_grain: str | None,
    evaluation_time: str | None,
    at_file: str | None,
    limit: int,
    cursor: str | None,
    output_json: bool,
) -> None:
    """Inspect measurement standing and retained readings. Read-only.

    A measurement reads `pending` before its window opens, `open` while it is open
    and unresolved (run `cruxible procedure measure`), `resolved`, or `expired`.
    """

    request = contracts.ProcedureReadingsRequest(
        run_id=run_id,
        measurement_names=tuple(sorted(set(measurements), key=lambda item: item.encode())),
        subject_grain=subject_grain,  # type: ignore[arg-type]
        evaluation_time=(
            None
            if evaluation_time is None
            else datetime.fromisoformat(evaluation_time.replace("Z", "+00:00"))
        ),
        at=None if at_file is None else _read_model(at_file, AcceptedCoordinate),
        limit=limit,
        cursor=cursor,
    )
    result = _server_call(
        lambda client, instance_id: client.list_procedure_readings(
            instance_id, name, request=request
        ),
        command_name="cruxible procedure readings",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for contract in result.contracts:
        line = f"{contract.measurement_name}: {contract.status} readings={contract.reading_count}"
        if contract.resolution is not None:
            line += f" ({contract.resolution.verdict}, {contract.resolution.resolution_id})"
        click.echo(line)
        if contract.review is not None and contract.review.last_disposition is not None:
            review = contract.review.last_disposition
            click.echo(
                f"  {review.resolution_id}: {review.verdict} by {review.reviewer_id} "
                f"reason: {review.reason}"
            )
    for reading in result.readings:
        click.echo(
            f"{reading.reading_id} {reading.measurement_name} {reading.subject_grain} "
            f"{reading.verdict} run={reading.run_id}"
        )
    if result.cursor is not None:
        click.echo(f"Next: --cursor {result.cursor}")


@playbill_group.group("inbox")
def inbox_group() -> None:
    """Judge what Lines sent to intelligence.

    A Line's post_inbox terminal posts items it could not decide, as they are,
    with the Captures behind each. Posting never changes accepted state. Read
    them with list and get, then ack, dismiss (with a reason) or link each to
    the proposal or Subject that records your judgment. An item that recurs
    with the same payload is counted and keeps its decision (`linked (+N
    since)`); a changed payload is a new item.
    """


def _inbox_row(envelope: Any) -> str:
    from cruxible_client.contracts.text_display import inbox_row

    return inbox_row(envelope)


@inbox_group.command("list")
@click.option("--line", default=None, help="Only this Line's items (NAME or Line:NAME).")
@click.option(
    "--status",
    type=click.Choice(["unread", "acknowledged", "dismissed", "linked"]),
    default=None,
    help="Only items with this status.",
)
@click.option(
    "--limit",
    default=contracts.INBOX_LIST_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, contracts.INBOX_LIST_MAX_LIMIT),
    help="Items per page.",
)
@click.option("--cursor", default=None, help="Continue a previous page of the same listing.")
@json_option
@handle_errors
def inbox_list(
    line: str | None, status: str | None, limit: int, cursor: str | None, output_json: bool
) -> None:
    """List inbox items, most recently posted first, one compact row each.

    Each row is the item's id, status, Line, posting terminal, seen count and a
    one-line title; `inbox get ITEM` shows every field and the whole payload.
    """

    result = compact_inbox_list(
        _server_call(
            lambda client, instance_id: client.list_inbox(
                instance_id, line=line, status=cast(Any, status), limit=limit, cursor=cursor
            ),
            command_name="cruxible inbox list",
        )
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if not result.rows:
        click.echo("No inbox items.")
        return
    click.echo("STATUS  ITEM  LINE  KIND  SEEN  TITLE")
    for row in result.rows:
        click.echo(
            f"{row.status}  {row.item_id[:16]}  {row.line}  {row.kind}  {row.seen}  {row.title}"
        )
    _echo_list_continuation(result.next_cursor)


def _echo_inbox_disposition(result: Any) -> None:
    from cruxible_client.contracts.text_display import inbox_disposition_line

    click.echo(inbox_disposition_line(result))


@inbox_group.command("get")
@click.argument("item_id")
@json_option
@handle_errors
def inbox_get(item_id: str, output_json: bool) -> None:
    """Read one item whole: envelope, payload as posted, and its latest dispositions."""

    result = _server_call(
        lambda client, instance_id: client.get_inbox_item(instance_id, item_id),
        command_name="cruxible inbox get",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    from cruxible_client.contracts.text_display import inbox_item_lines

    for line in inbox_item_lines(result):
        click.echo(line)


@inbox_group.command("ack")
@click.argument("item_id")
@json_option
@handle_errors
def inbox_ack(item_id: str, output_json: bool) -> None:
    """Acknowledge an item: seen, and owed nothing further."""

    result = _server_call(
        lambda client, instance_id: client.ack_inbox_item(instance_id, item_id),
        command_name="cruxible inbox ack",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_inbox_disposition(result)


@inbox_group.command("dismiss")
@click.argument("item_id")
@click.option("--reason", required=True, help="Why the item needs nothing. Recorded verbatim.")
@json_option
@handle_errors
def inbox_dismiss(item_id: str, reason: str, output_json: bool) -> None:
    """Dismiss an item, recording why it needs nothing."""

    result = _server_call(
        lambda client, instance_id: client.dismiss_inbox_item(instance_id, item_id, reason=reason),
        command_name="cruxible inbox dismiss",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_inbox_disposition(result)


@inbox_group.command("link")
@click.argument("item_id")
@click.option("--proposal", default=None, help="The proposal that records your judgment.")
@click.option("--subject", default=None, help="The Subject (KIND/ID), such as a note.")
@json_option
@handle_errors
def inbox_link(item_id: str, proposal: str | None, subject: str | None, output_json: bool) -> None:
    """Link an item to the proposal or Subject that records your judgment of it."""

    if (proposal is None) == (subject is None):
        raise click.UsageError("name exactly one of --proposal or --subject")
    target: contracts.InboxLinkTarget = (
        contracts.InboxLinkProposal(proposal_id=proposal)
        if proposal is not None
        else contracts.InboxLinkSubject(subject=cast(str, subject))
    )
    result = _server_call(
        lambda client, instance_id: client.link_inbox_item(instance_id, item_id, target=target),
        command_name="cruxible inbox link",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_inbox_disposition(result)


@playbill_group.group("line")
def line_group() -> None:
    """Enable, run, grant and recover accepted Lines.

    enable, disable and run are the normal flow; evaluate and dispatch recover
    what automation missed, as `cruxible next` names it; grant writes the one
    settle Mandate over the observations a Line writes. `cruxible get
    Line:NAME` reads a Line's enablement, Triggers, pending work and runs.
    """


def _echo_line_enablement(result: Any) -> None:
    from cruxible_client.contracts.text_display import line_enablement_lines

    for line in line_enablement_lines(result):
        click.echo(line)
    echo_preview_next(result.outcome or "", result.coordinate)


def _runbook_lines(client: CruxibleClient, instance_id: str, runbook: str) -> tuple[str, ...]:
    """The Lines RUNBOOK's steps name, in first-step order, once each (bare names)."""

    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_client.contracts.runbooks import Runbook

    name = runbook.removeprefix("Runbook:")
    found = client.get(instance_id, request=GetRequest(ref=f"Runbook:{name}", detail="proof"))
    envelope = None if found.proof is None else found.proof.get("envelope")
    if envelope is None:
        raise click.ClickException(f"Runbook:{name} has no accepted definition to read Lines from")
    accepted = Runbook.model_validate(envelope)
    if accepted.lifecycle.state != "live":
        raise click.ClickException(
            f"Runbook:{name} is {accepted.lifecycle.state}; name its Lines instead"
        )
    if not accepted.lines:
        raise click.ClickException(
            f"Runbook:{name} lists no Lines (only actions); nothing to enable"
        )
    return tuple(identity.name for identity in accepted.lines)


def _enable_lines(
    lines: tuple[str, ...], dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Enable each Line in turn and report each: enabled, unchanged, or refused and why.

    Every Line is attempted; a refusal is reported beside the others and the
    command exits 1 after its output when any Line was refused. A transport,
    authentication or permission failure stops at once, as for one Line.
    """

    from cruxible_client.contracts.text_display import line_enablement_lines
    from cruxible_client.errors import AuthenticationError, ServerUnreachableError
    from cruxible_client.errors import CoreError as ClientCoreError
    from cruxible_core.cli.main import _is_permission_denied

    entries: list[dict[str, Any]] = []
    rendered: list[str] = []
    preview: Any = None
    counts = {"enabled": 0, "unchanged": 0, "previewed": 0, "refused": 0}

    def enable(name: str) -> Any:
        return _server_call(
            lambda client, instance_id: client.enable_line(
                instance_id, name, dry_run=dry_run, at=at
            ),
            command_name="cruxible line enable",
        )

    for name in lines:
        try:
            result = enable(name)
        except ClientCoreError as exc:
            if isinstance(exc, ServerUnreachableError | AuthenticationError) or (
                _is_permission_denied(exc)
            ):
                raise
            counts["refused"] += 1
            code = getattr(exc, "error_code", None)
            message = str(exc)
            repairs = [str(item) for item in getattr(exc, "repair_commands", ())]
            entries.append(
                {
                    "line": name,
                    "refused": {"code": code, "message": message, "repairs": repairs},
                }
            )
            shown = message if not code or message.startswith(f"{code}:") else f"{code}: {message}"
            rendered.append(f"Line:{name}: refused ({shown})")
            rendered.extend(f"  repair: {item}" for item in repairs)
            continue
        entries.append({"line": name, "enablement": result.model_dump(mode="json")})
        rendered.append(line_enablement_lines(result)[0])
        if result.detail:
            rendered.append(f"  {result.detail}")
        outcome = result.outcome or ""
        if outcome.startswith("would_"):
            counts["previewed"] += 1
            preview = preview or result
        else:
            counts["unchanged" if outcome.startswith("already_") else "enabled"] += 1
    if output_json:
        _emit_json({"lines": entries})
    else:
        for item in rendered:
            click.echo(item)
        click.echo("Lines: " + ", ".join(f"{n} {label}" for label, n in counts.items() if n))
        if preview is not None and not counts["refused"]:
            echo_preview_next(preview.outcome or "", preview.coordinate)
    if counts["refused"]:
        raise SystemExit(1)


@line_group.command("enable")
@click.argument("lines", nargs=-1, metavar="[LINE]...")
@click.option(
    "--runbook",
    default=None,
    metavar="RUNBOOK",
    help="Also enable every Line RUNBOOK's steps name.",
)
@change_control_options
@json_option
@handle_errors
def enable_line(
    lines: tuple[str, ...],
    runbook: str | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Enable LINE: its Triggers admit what they match from now on, under your credential.

    A Trigger aimed at a Line does nothing until the Line is enabled. The
    enablement pins the current Line version and the exact Trigger versions
    aimed at it; any change to them stops it until you enable it again. It
    never catches up: `cruxible next` names restart gaps and pending work for
    `line evaluate` and `line dispatch`. Enabling needs governed write, even for
    an observe-only Line; a Line that proposes or settles also needs a covering
    ProcedureMandate. Read it with `cruxible get Line:LINE`.

    Several LINEs, or `--runbook RUNBOOK` (every Line its steps name), enable
    each in turn, once each, and print one receipt line per Line (`--json`:
    `{"lines": [...]}`, each with its enablement or refusal); any refusal
    exits 1 after every Line is tried.
    """

    if not lines and runbook is None:
        raise click.UsageError("name a LINE, or --runbook RUNBOOK")
    if len(lines) == 1 and runbook is None:
        line = lines[0]
        result = _server_call(
            lambda client, instance_id: client.enable_line(
                instance_id, line, dry_run=dry_run, at=at
            ),
            command_name="cruxible line enable",
        )
        if output_json:
            _emit_json(result.model_dump(mode="json"))
        else:
            _echo_line_enablement(result)
        return
    named = [item.removeprefix("Line:") for item in lines]
    if runbook is not None:
        named.extend(
            _server_call(
                lambda client, instance_id: _runbook_lines(client, instance_id, runbook),
                command_name="cruxible line enable --runbook",
            )
        )
    _enable_lines(tuple(dict.fromkeys(named)), dry_run, at, output_json)


@line_group.command("disable")
@click.argument("line")
@change_control_options
@json_option
@handle_errors
def disable_line(line: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Stop LINE admitting work on its own; runs already admitted keep going.

    Disabling a Line whose enablement already stopped changes nothing. A Line
    never enabled has no enablement to stop and refuses
    (`cruxible.line.never_enabled`). A retired Line can be disabled too.
    """

    result = _server_call(
        lambda client, instance_id: client.disable_line(instance_id, line, dry_run=dry_run, at=at),
        command_name="cruxible line disable",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_line_enablement(result)


#: A settle grant's validity when ``--expires`` is not given.
LINE_GRANT_DEFAULT_EXPIRES = "90d"


def _grant_expiry(value: str, *, now: datetime) -> datetime:
    """``--expires``: a span (``30d``, ``12w``, ``1y``, ``36h``, ``P90D``) or an instant."""

    from cruxible_client.contracts.temporal import ensure_utc, parse_validity_span

    text = value.strip()
    if text.startswith("P") or text[-1:].lower() in {"h", "d", "w", "y"}:
        try:
            expires = now + parse_validity_span(text)
        except (ValueError, OverflowError) as exc:
            raise click.BadParameter(str(exc), param_hint="--expires") from exc
    else:
        try:
            expires = ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError as exc:
            raise click.BadParameter(
                "give a span (30d, 12w, 1y, 36h, P90D) or an ISO-8601 instant",
                param_hint="--expires",
            ) from exc
    if expires <= now:
        raise click.BadParameter("the grant must expire after now", param_hint="--expires")
    return expires


def _observation_grant_scope(
    client: CruxibleClient, instance_id: str, line: str, mandate: Any
) -> Any:
    """What ``scope: observations`` lowers to for LINE at the head, read as lowering reads it."""

    from cruxible_client.contracts.claim_types import ClaimType
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_client.contracts.observation_settle import observation_settle_scope
    from cruxible_client.contracts.procedures.line_specs import LineSpec

    def proof(ref: str) -> Any:
        found = client.get(instance_id, request=GetRequest(ref=ref, detail="proof")).proof
        return None if found is None else found.get("envelope")

    spec = LineSpec.model_validate(proof(f"Line:{line}"))
    readiness = client.procedure_readiness(instance_id, spec.procedure.target.name)
    if readiness.artifact_digest != spec.procedure.artifact_digest:
        raise click.ClickException(
            f"cruxible.authoring.procedure_mandate_observations_procedure_moved: Line {line!r} "
            f"pins another version of Procedure {spec.procedure.target.name!r} than the "
            "accepted one; revise the Line to pin it, then grant it."
        )

    def claim_type(predicate: str) -> ClaimType | None:
        try:
            envelope = proof(f"ClaimType:{predicate}")
        except CoreError:
            return None
        return None if envelope is None else ClaimType.model_validate(envelope)

    return observation_settle_scope(
        readiness.artifact.definition.nodes,
        claim_type,
        include_derived=mandate.include_derived,
        subject_creation=mandate.subject_creation,
    )


def _echo_observation_grant(mandate: Any, scope: Any) -> None:
    from cruxible_client.contracts.mandate_scope import MANDATE_CHANGE_KIND_ORDER

    click.echo(f"grant  Mandate:{mandate.name}  settle  Line:{mandate.line_name}")
    click.echo(f"valid  {_instant_text(mandate.valid_from)} .. {_instant_text(mandate.expires_at)}")
    click.echo(f"namespace  {', '.join(mandate.namespace)}")
    for item in scope.claim_types:
        changes = "  ".join(
            f"{kind}={','.join(item.changes.of(kind).basis)}"
            for kind in MANDATE_CHANGE_KIND_ORDER
            if item.changes.of(kind) is not None
        )
        click.echo(f"ClaimType:{item.claim_type}  {changes}")
    if mandate.subject_creation:
        click.echo(f"may create  {', '.join(mandate.subject_creation)}")
    unscoped = sorted(set(scope.creatable_subject_kinds) - set(mandate.subject_creation))
    if unscoped:
        click.echo(f"creates, proposes  {', '.join(unscoped)}  (--create-subjects to settle)")


def _instant_text(value: datetime) -> str:
    from cruxible_client.contracts.temporal import format_datetime

    return str(format_datetime(value))


@line_group.command("grant")
@click.argument("line")
@click.option(
    "--settle-observations",
    is_flag=True,
    help="Grant settle over every observation-only ClaimType LINE's Procedure writes.",
)
@click.option(
    "--include-derived",
    is_flag=True,
    help="Also settle values computed by Procedure logic (basis derived).",
)
@click.option(
    "--create-subjects",
    "create_subjects",
    multiple=True,
    metavar="KIND",
    help="A Subject kind the run may create by key and settle; repeatable.",
)
@click.option(
    "--expires",
    default=LINE_GRANT_DEFAULT_EXPIRES,
    show_default=True,
    help="Span (30d, 12w, 1y, 36h, P90D) or ISO-8601 instant the grant ends at.",
)
@click.option("--name", "mandate_name", default=None, help="Mandate name [default: LINE-settle].")
@click.option("--dry-run", is_flag=True, help="Preflight the grant; save nothing.")
@click.option("--no-accept", is_flag=True, help="Propose only; do not land the grant.")
@json_option
@handle_errors
def grant_line(
    line: str,
    settle_observations: bool,
    include_derived: bool,
    create_subjects: tuple[str, ...],
    expires: str,
    mandate_name: str | None,
    dry_run: bool,
    no_accept: bool,
    output_json: bool,
) -> None:
    """Grant LINE settle authority over the observations it writes, as one Mandate.

    --settle-observations pins every ClaimType LINE's Procedure writes (each
    must permit only the observation role) with the changes its templates
    make: creates and revises on extracted and measured values, retirements and
    inverses on a complete source's absence. Subject creation settles only for
    the kinds --create-subjects names. The grant is an ordinary Mandate pinned to
    LINE's exact version; it lands when policy and your tier allow.
    """

    from cruxible_client.authoring.inputs import ChangeSetInput
    from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
    from cruxible_client.contracts.observation_settle import (
        OBSERVATION_SETTLE_NAMESPACE,
        ObservationSettleRefusal,
    )
    from cruxible_client.contracts.procedure_mandates import (
        ProcedureMandateError,
        procedure_mandate_path,
    )

    if not settle_observations:
        raise click.UsageError("name the grant: --settle-observations")
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        mandate = ProcedureMandateInput(
            kind="procedure_mandate",
            name=mandate_name or f"{line}-settle",
            line_name=line,
            grants="settle",
            namespace=OBSERVATION_SETTLE_NAMESPACE,
            valid_from=now,
            expires_at=_grant_expiry(expires, now=now),
            scope="observations",
            include_derived=include_derived,
            subject_creation=tuple(sorted(set(create_subjects), key=str.encode)),
        )
        procedure_mandate_path(mandate.name)
    except (ValidationError, ProcedureMandateError) as exc:
        raise click.UsageError(
            f"cannot name the Mandate {mandate_name or line + '-settle'!r}: "
            "give --name in lowercase letters, digits, . _ -"
        ) from exc

    def call(client: CruxibleClient, instance_id: str) -> tuple[Any, Any]:
        scope = _observation_grant_scope(client, instance_id, line, mandate)
        if isinstance(scope, ObservationSettleRefusal):
            return scope, None
        payload = ChangeSetInput(
            kind="change_set",
            members=(mandate,),
            rationale=f"Settle the observations Line {line} writes.",
        ).model_dump(mode="json")
        if dry_run:
            return scope, client.preview_authoring_input(instance_id, input=payload)
        return scope, client.submit_authoring_input(
            instance_id, input=payload, accept="never" if no_accept else "if_allowed"
        )

    scope, result = _server_call(call, command_name="cruxible line grant")
    if isinstance(scope, ObservationSettleRefusal):
        if output_json:
            _emit_json({"refusal": scope.model_dump(mode="json")})
        else:
            click.echo(f"refused: {scope.code}: {printable(scope.message)}", err=True)
            click.echo(f"repair: {printable(scope.repair_description)}", err=True)
        raise click.exceptions.Exit(1)
    if output_json:
        _emit_json(
            {
                "mandate": mandate.model_dump(mode="json"),
                "scope": scope.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
            }
        )
        if isinstance(result, contracts.AuthoringPreflightResult) and result.verdict != "passed":
            raise click.exceptions.Exit(1)
        return
    _echo_observation_grant(mandate, scope)
    if isinstance(result, contracts.AuthoringPreflightResult):
        click.echo(f"would {'pass' if result.verdict == 'passed' else 'refuse'}")
        _exit_if_preflight_refused(result)
        return
    reason_line = _submit_refusal_reason(result)
    if reason_line is not None:
        click.echo(f"refused: {reason_line}", err=True)
        raise click.exceptions.Exit(1)
    _echo_landing(result.outcome, result.warnings)


@line_group.command("evaluate")
@click.argument("line")
@click.option("--since", default=None, help="Inclusive ISO-8601 start of the range.")
@click.option(
    "--until", default=None, help="Exclusive ISO-8601 end of the range, capped at daemon time."
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Only report what the range makes eligible; enqueue nothing (no range needed).",
)
@click.option("--limit", default=100, type=click.IntRange(1, 256), help="Occurrences per page.")
@click.option(
    "--cursor",
    default=None,
    help="Continue an incomplete page of the same range, in the same --dry-run mode.",
)
@click.option(
    "--dispatch",
    "and_dispatch",
    is_flag=True,
    help=(
        "Then dispatch what this evaluation enqueued: every page of the range is "
        "evaluated, and each occurrence it made pending is dispatched (one command "
        "for a restart gap)."
    ),
)
@json_option
@handle_errors
def evaluate_line(
    line: str,
    since: str | None,
    until: str | None,
    dry_run: bool,
    limit: int,
    cursor: str | None,
    and_dispatch: bool,
    output_json: bool,
) -> None:
    """Turn a missed range of LINE's Triggers into pending work; never runs anything.

    Use it to recover what automation missed (a daemon restart leaves a gap
    `cruxible next` names with the exact command), then `line dispatch`, or
    pass --dispatch to do both in one command. Without --dry-run, --since and
    --until are required.
    """

    from cruxible_client.contracts.line_dispatch import (
        LineDispatchRequest,
        LineEvaluateRequest,
    )

    if and_dispatch and dry_run:
        raise click.UsageError(
            "--dispatch runs what the evaluation enqueues; --dry-run enqueues nothing"
        )

    def evaluate(page_cursor: str | None) -> Any:
        request = LineEvaluateRequest.model_validate(
            dict(since=since, until=until, limit=limit, cursor=page_cursor, dry_run=dry_run)
        )
        return _server_call(
            lambda client, instance_id: client.evaluate_line(instance_id, line, request=request),
            command_name="cruxible line evaluate",
        )

    if not and_dispatch:
        result = evaluate(cursor)
        if output_json:
            _emit_json(result.model_dump(mode="json"))
            return
        _echo_line_evaluation(result)
        if result.cursor:
            click.echo(
                f"Next cursor: {result.cursor}; retain --until {result.checked_until.isoformat()}"
            )
        return
    # Every page of the same range: this call's own enqueue marks what it
    # made pending, across pages -- never an occurrence already pending from
    # an earlier evaluation, which the general `pending` flag cannot tell
    # apart from one this call just enqueued.
    pages = [evaluate(cursor)]
    while pages[-1].cursor is not None:
        pages.append(evaluate(pages[-1].cursor))
    newly_enqueued = [
        item.occurrence_id for page in pages for item in page.occurrences if item.newly_enqueued
    ]

    def dispatch(occurrence_id: str) -> Any:
        request = LineDispatchRequest(occurrence_id=occurrence_id, limit=1)
        return _server_call(
            lambda client, instance_id: client.dispatch_line(instance_id, line, request=request),
            command_name="cruxible line dispatch",
        )

    items: list[Any] = [
        item for occurrence_id in newly_enqueued for item in dispatch(occurrence_id).items
    ]
    combined = _LineEvaluateDispatched(evaluations=tuple(pages), dispatched=tuple(items))
    if output_json:
        _emit_json(combined.model_dump(mode="json"))
        return
    for page in pages:
        _echo_line_evaluation(page)
    if not items:
        click.echo("Nothing enqueued to dispatch.")
    for item in items:
        _echo_line_dispatch_item(item)


class _LineEvaluateDispatched(BaseModel):
    """`line evaluate --dispatch`: every evaluated page, then each enqueued dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-line-evaluate-dispatch-v1"] = "playbill-line-evaluate-dispatch-v1"
    evaluations: tuple[LineEvaluateResult, ...]
    dispatched: tuple[LineDispatchItem, ...]


def _echo_line_evaluation(result: LineEvaluateResult) -> None:
    click.echo(f"{result.line}: {result.status} ({len(result.occurrences)} occurrences)")
    if result.detail:
        click.echo(result.detail)


def _echo_line_dispatch_item(item: LineDispatchItem) -> None:
    click.echo(
        f"{item.occurrence_id}: {item.status}" + (f" run={item.run_id}" if item.run_id else "")
    )
    if item.detail:
        click.echo(item.detail)


#: Occurrences one `line dispatch` request attempts while draining.
_LINE_DISPATCH_PAGE = 100


@line_group.command("dispatch")
@click.argument("line")
@click.option("--occurrence-id", default=None, help="Dispatch only this pending occurrence.")
@click.option(
    "--retry",
    is_flag=True,
    help="Explicitly retry --occurrence-id against the current Line in the same epoch.",
)
@click.option(
    "--limit",
    default=None,
    type=click.IntRange(1, 100),
    help="Dispatch at most this many; by default every pending occurrence is drained.",
)
@json_option
@handle_errors
def dispatch_line(
    line: str, occurrence_id: str | None, limit: int | None, retry: bool, output_json: bool
) -> None:
    """Run LINE's pending occurrences under your current authority.

    An enabled Line admits only what it matched itself; work evaluated
    explicitly, or matched before a restart, waits for this command. By
    default it drains every pending occurrence; one that stays blocked is
    reported once.
    """

    from cruxible_client.contracts.line_dispatch import LineDispatchRequest, LineDispatchResult

    page = _LINE_DISPATCH_PAGE if limit is None else limit
    items: list[Any] = []
    cursor: str | None = None
    while True:
        request = LineDispatchRequest(
            occurrence_id=occurrence_id, limit=page, retry=retry, cursor=cursor
        )
        result = _server_call(
            lambda client, instance_id: client.dispatch_line(instance_id, line, request=request),
            command_name="cruxible line dispatch",
        )
        items.extend(result.items)
        # Each page continues past every occurrence the last one attempted, so
        # a page that stayed blocked never hides the occurrences after it.
        if limit is not None or result.cursor is None:
            break
        cursor = result.cursor
    drained = LineDispatchResult(items=tuple(items))
    if output_json:
        _emit_json(drained.model_dump(mode="json"))
    elif not drained.items:
        click.echo("No pending occurrences for this Line epoch.")
    else:
        for item in drained.items:
            _echo_line_dispatch_item(item)


@line_group.command("run")
@click.argument("line")
@click.option(
    "--event",
    "event_file",
    type=PayloadFile(),
    help=(
        "Retained Capture event reference JSON/YAML (- for stdin) for a Line whose "
        "Procedure takes an event input."
    ),
)
@click.option(
    "--repeat",
    is_flag=True,
    help="Run on an event the enabled Line already admitted, deliberately again.",
)
@click.option(
    "--resolution-contract",
    "contract_file",
    type=PayloadFile(),
    help="Exact accepted ResolutionContract reference JSON/YAML (- for stdin).",
)
@click.option(
    "--occurrence-id",
    default=None,
    help=(
        "Assert this admission's daemon-derived id; manual ids include its instant. "
        "Use line dispatch --occurrence-id to select retained pending work."
    ),
)
@click.option(
    "--evaluation-time",
    default=None,
    help="Assert the ISO-8601 instant you expect; the daemon clock decides.",
)
@click.option(
    "--parameters",
    "parameters_file",
    type=PayloadFile(),
    help=(
        "Parameter values JSON/YAML over the Line's, for this run only (checked against "
        "the Procedure's params Contract). A run on other parameters runs without the "
        "Line's mandate: it may propose, never settle."
    ),
)
@json_option
@handle_errors
def run_line(
    line: str,
    event_file: str | None,
    repeat: bool,
    contract_file: str | None,
    occurrence_id: str | None,
    evaluation_time: str | None,
    parameters_file: str | None,
    output_json: bool,
) -> None:
    """Run LINE once now: one manual occurrence under the Line's inputs, budgets and authority.

    It never selects, consumes or waits on a Trigger, and runs whether or not
    the Line is enabled; the run is recorded on the Line's history. Exits 1
    after its output (`--json` too) when the run refused or failed.
    """

    resolution_contract = (
        None if contract_file is None else _read_model(contract_file, ResolutionContractReference)
    )
    event = None if event_file is None else _read_model(event_file, TriggerEventReference)
    request = LineRunRequest.model_validate(
        {
            "line": line,
            "occurrence_id": occurrence_id,
            "evaluation_time": evaluation_time,
            "resolution_contract": resolution_contract,
            "event": event,
            "repeat": repeat,
            "parameters": None if parameters_file is None else _read_mapping(parameters_file),
        }
    )
    result = _server_call(
        lambda client, instance_id: client.run_line(
            instance_id,
            line,
            occurrence_id=request.occurrence_id,
            resolution_contract=resolution_contract,
            event=event,
            repeat=repeat,
            evaluation_time=(
                None if request.evaluation_time is None else request.evaluation_time.isoformat()
            ),
            **({"parameters": request.parameters} if request.parameters is not None else {}),
        ),
        command_name="cruxible line run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        _exit_for_run(result)
        return
    _echo_run_outcome(result, result.run_id or line)
    _echo_source_observations(result)
    _exit_for_run(result)


@playbill_group.group("runbook")
def runbook_group() -> None:
    """Run or preview an accepted Runbook: an ordered list of Lines and actions.

    `run` runs the steps in order and stops at the first one that fails or
    sends writes to review; `preview` (or `run --dry-run`) shows what each step
    can read, write, settle, propose and set off, from definitions only.
    """


def _echo_runbook_run(result: Any) -> None:
    from cruxible_client.contracts.text_display import runbook_run_lines

    for line in runbook_run_lines(result):
        click.echo(line)


def _echo_runbook_preview(preview: Any) -> None:
    from cruxible_client.contracts.text_display import runbook_preview_lines

    for line in runbook_preview_lines(preview):
        click.echo(line)


@runbook_group.command("run")
@click.argument("runbook")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show the static preview instead of running: definitions only, nothing executes.",
)
@json_option
@handle_errors
def run_runbook(runbook: str, dry_run: bool, output_json: bool) -> None:
    """Run RUNBOOK now: its steps in order, each Line as your manual run.

    Stops at the first step that fails or sends writes to review. Exits 1
    after its output (`--json` too) when a step failed; a stop for review
    exits 0.
    """

    if dry_run:
        preview = _server_call(
            lambda client, instance_id: client.preview_runbook(instance_id, runbook),
            command_name="cruxible runbook run --dry-run",
        )
        if output_json:
            _emit_json(preview.model_dump(mode="json"))
            return
        _echo_runbook_preview(preview)
        return
    result = _server_call(
        lambda client, instance_id: client.run_runbook(instance_id, runbook),
        command_name="cruxible runbook run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_runbook_run(result)
    if result.stop_reason in ("step_failed", "line_not_enabled"):
        raise SystemExit(1)


@runbook_group.command("preview")
@click.argument("runbook")
@json_option
@handle_errors
def preview_runbook(runbook: str, output_json: bool) -> None:
    """Show what RUNBOOK's steps can read, write, settle, propose and set off.

    Composed from accepted definitions only: nothing runs, and no outcome is
    predicted.
    """

    preview = _server_call(
        lambda client, instance_id: client.preview_runbook(instance_id, runbook),
        command_name="cruxible runbook preview",
    )
    if output_json:
        _emit_json(preview.model_dump(mode="json"))
        return
    _echo_runbook_preview(preview)


@playbill_group.command("next")
@click.option(
    "--evaluation-time",
    default=None,
    help="Explicit ISO-8601 evaluation time; otherwise the client stamps the current UTC time.",
)
@click.option(
    "--access-profile",
    "access_profile_path",
    default=None,
    type=PayloadFile(),
    help="CoverageAccessProfile JSON/YAML; defaults to public and instance access.",
)
@click.option(
    "--expiring-within",
    callback=_parse_expiring_duration,
    default="P7D",
    show_default=True,
    help="ISO-8601 lead window for evidence-expiration warnings (for example P7D or PT12H).",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose configured floor is observed locally.",
)
@click.option(
    "--delta",
    "since_result_digest",
    default=None,
    help="A prior result_digest; return only the rows new since that queue.",
)
@click.option(
    "--limit",
    default=contracts.NEXT_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, contracts.NEXT_MAX_LIMIT),
    help="Rows per page.",
)
@click.option("--cursor", default=None, help="Continue a previous page of the same queue.")
@brief_option
@json_option
@handle_errors
def next_work(
    evaluation_time: str | None,
    access_profile_path: str | None,
    expiring_within: int,
    workspace_root: str,
    since_result_digest: str | None,
    limit: int,
    cursor: str | None,
    output_brief: bool,
    output_json: bool,
) -> None:
    stamped_evaluation_time = (
        datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if evaluation_time is None
        else evaluation_time
    )
    profile = (
        CoverageAccessProfile(
            profile_id="cli-next",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_model(access_profile_path, CoverageAccessProfile).model_dump(mode="json")
    )
    workspace_observation = observe_next_workspace(Path(workspace_root))

    def _next_at_scanned_coordinate(
        client: CruxibleClient, instance_id: str
    ) -> contracts.NextResult:
        observed, coordinate = observe_next_workspace_with_coverage(
            client,
            instance_id,
            Path(workspace_root),
            observation=workspace_observation,
            access_profile=profile,
        )
        return client.next(
            instance_id,
            evaluation_time=stamped_evaluation_time,
            access_profile=profile,
            at=coordinate,
            expiring_within={"microseconds": expiring_within},
            workspace_observation=observed,
            since_result_digest=since_result_digest,
            limit=limit,
            cursor=cursor,
        )

    result = _server_call(
        _next_at_scanned_coordinate,
        command_name="cruxible next",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(
        "\n".join(
            next_lines(
                status=result.status,
                items=result.items,
                delta_since=result.delta_since,
                removed_item_ids=result.removed_item_ids,
                unobserved=next_unobserved_reasons(result.unobserved_domains, Path(workspace_root)),
                brief=output_brief,
                total_items=result.total_items,
                next_cursor=result.next_cursor,
            )
        )
    )


@playbill_group.command("duplicates")
@click.option("--kind", required=True, help="The Subject kind to audit.")
@click.option(
    "--limit",
    default=DUPLICATES_AUDIT_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, DUPLICATES_AUDIT_MAX_LIMIT),
    help="Groups per page.",
)
@click.option("--cursor", default=None, help="Continue a previous page of the same audit.")
@json_option
@handle_errors
def duplicates_audit(kind: str, limit: int, cursor: str | None, output_json: bool) -> None:
    """List possible duplicates of one kind; a pure, bounded read.

    Subjects whose ids or aliases, and values of one predicate, that meet under
    a broad comparison (casefold, no punctuation, the alias graph) but differ
    under the declared rules; and Subjects of a hint kind holding one key.
    Resolve a Subject or key group with an alias plus a retirement or with
    semantic.distinct_from; a value group with a governed canonical_form
    succession.
    """

    result = _server_call(
        lambda client, instance_id: client.duplicates(
            instance_id, kind=kind, limit=limit, cursor=cursor
        ),
        command_name="cruxible duplicates",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    scanned = result.scanned
    click.echo(
        f"Possible duplicates of {result.kind}: {len(result.groups)} group(s); read "
        f"{scanned.subjects} Subject(s), {scanned.aliases} alias(es), {scanned.values} value(s)"
        + (" (stopped at its bound)" if result.budget_exhausted else "")
        + "."
    )
    for group in result.groups:
        if group.group == "subjects":
            members = ", ".join(
                f"{item.subject} ({', '.join(item.names)})" for item in group.subjects
            )
            click.echo(f"  subjects {group.key!r}: {members}")
        elif group.group == "keys":
            key = ", ".join(f"{part.predicate}={part.value!r}" for part in group.key)
            click.echo(f"  key {key}: {', '.join(group.subjects)}")
        else:
            members = ", ".join(f"{item.value!r} {item.claim}" for item in group.values)
            click.echo(f"  {group.predicate} {group.key!r}: {members}")
    _echo_list_continuation(result.next_cursor)


@playbill_group.command("audit")
@click.option("--claim-type", "claim_types", multiple=True)
@click.option("--subject-kind", "subject_kinds", multiple=True)
@click.option(
    "--max-rows",
    default=AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    show_default=True,
    type=click.IntRange(AUDIT_BUDGET_MIN_MAX_ROWS, AUDIT_BUDGET_MAX_MAX_ROWS),
)
@click.option(
    "--max-bytes",
    default=AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    show_default=True,
    type=click.IntRange(AUDIT_BUDGET_MIN_MAX_BYTES, AUDIT_BUDGET_MAX_MAX_BYTES),
)
@click.option(
    "--access-profile",
    "access_profile_path",
    default=None,
    type=PayloadFile(),
    help="CoverageAccessProfile JSON/YAML; defaults to public and instance access.",
)
@click.option(
    "--cursor",
    "cursor_path",
    default=None,
    type=PayloadFile(),
    help="AuditCursor JSON/YAML returned by a prior page.",
)
@json_option
@handle_errors
def audit(
    claim_types: tuple[str, ...],
    subject_kinds: tuple[str, ...],
    max_rows: int,
    max_bytes: int,
    access_profile_path: str | None,
    cursor_path: str | None,
    output_json: bool,
) -> None:
    """Read the deterministic verification patrol without changing governed state."""

    profile = (
        CoverageAccessProfile(
            profile_id="cli-audit",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_model(access_profile_path, CoverageAccessProfile).model_dump(mode="json")
    )
    cursor = None if cursor_path is None else _read_model(cursor_path, contracts.AuditCursor)
    result = _server_call(
        lambda client, instance_id: client.audit(
            instance_id,
            evaluation_time=datetime.now(UTC).isoformat(),
            access_profile=profile,
            claim_type_identities=tuple(
                sorted(set(claim_types), key=lambda item: item.encode("utf-8"))
            ),
            subject_kinds=tuple(sorted(set(subject_kinds), key=lambda item: item.encode("utf-8"))),
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        ),
        command_name="cruxible audit",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    from cruxible_client.contracts.get_display import render_claim_line

    for row in result.rows:
        scores = (
            f"stake={row.factors.stake} weakness={row.factors.weakness} "
            f"staleness={row.factors.staleness}"
        )
        if row.statement is not None:
            click.echo(f"{render_claim_line(row.statement, lead=str(row.rank_score))}  {scores}")
        else:
            claim = f"{row.claim_identity['kind']}:{row.claim_identity['name']}"
            click.echo(f"{row.rank_score}  {claim}  {scores}")
    if not result.rows:
        click.echo("No Claims in the visible audit scope.")
    if result.next_cursor is not None:
        scope = [
            *(item for value in claim_types for item in ("--claim-type", value)),
            *(item for value in subject_kinds for item in ("--subject-kind", value)),
        ]
        click.echo(
            "More: "
            + _cursor_page_line(
                [
                    "audit",
                    *map(shlex.quote, scope),
                    *_page_bounds(max_rows, max_bytes),
                    *_profile_flag(access_profile_path),
                ],
                result.next_cursor.model_dump(mode="json"),
            )
        )


@playbill_group.command("since")
@click.argument("generation", type=click.IntRange(min=0))
@click.option(
    "--limit",
    "--max-rows",
    "max_rows",
    default=100,
    show_default=True,
    type=click.IntRange(1, 1000),
    help="Rows per page; a truncated page prints the command that continues it.",
)
@click.option("--max-bytes", default=65_536, show_default=True, type=click.IntRange(1, 1_048_576))
@click.option(
    "--access-profile",
    "access_profile_path",
    default=None,
    type=PayloadFile(),
    help="CoverageAccessProfile JSON/YAML; defaults to public and instance access.",
)
@click.option(
    "--cursor",
    "cursor_path",
    default=None,
    type=PayloadFile(),
    help="SinceCursor JSON/YAML returned by a prior page.",
)
@json_option
@handle_errors
def since(
    generation: int,
    max_rows: int,
    max_bytes: int,
    access_profile_path: str | None,
    cursor_path: str | None,
    output_json: bool,
) -> None:
    profile = (
        CoverageAccessProfile(
            profile_id="cli-since",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_since_access_profile(access_profile_path)
    )
    cursor = None if cursor_path is None else _read_mapping(cursor_path)
    result = _server_call(
        lambda client, instance_id: client.since(
            instance_id,
            generation=generation,
            access_profile=profile,
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        ),
        command_name="cruxible since",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    from cruxible_client.contracts.get_display import render_claim_line

    for row in result.rows:
        lead = f"{row.generation}  {row.disposition}"
        if row.statement is not None:
            # What changed, not where it is stored: `--json` keeps paths and digests.
            click.echo(render_claim_line(row.statement, lead=lead))
            continue
        name = row.member_path.split("/", 1)[-1].removesuffix(".json")
        click.echo(f"{lead}  {row.artifact_kind} {_public_name(name)}")
    if result.next_cursor is not None:
        click.echo(
            "More: "
            + _cursor_page_line(
                [
                    "since",
                    str(generation),
                    *_page_bounds(max_rows, max_bytes),
                    *_profile_flag(access_profile_path),
                ],
                result.next_cursor.model_dump(mode="json"),
            )
        )


def _page_bounds(max_rows: int, max_bytes: int) -> list[str]:
    """The page bounds a continuation repeats: a cursor continues the page it was cut from."""

    return ["--max-rows", str(max_rows), "--max-bytes", str(max_bytes)]


def _profile_flag(access_profile_path: str | None) -> list[str]:
    """The access profile a continuation repeats (the cursor binds it), when named by path."""

    if access_profile_path is None or access_profile_path == "-":
        return []
    return ["--access-profile", shlex.quote(access_profile_path)]


def _cursor_page_line(command: list[str], cursor: Mapping[str, Any]) -> str:
    """The next page as one command: the cursor travels on stdin, so no file is needed."""

    return " ".join(
        ["cruxible", *command, "--cursor", "-", "<<<", shlex.quote(canonical_json(cursor))]
    )


#: The orient text is the client's, so ``print(cx.orient())`` reads the same.
_render_orient = render_orient


@playbill_group.command("orient")
@click.option("--kind", default=None, help="Read one Subject kind in full.")
@click.option(
    "--section",
    type=click.Choice(list(get_args(contracts.OrientSection))),
    default=None,
    help=(
        "Page one artifact family, graph (live Subjects, then the Claims between them), "
        "pins (what each live definition pins), activity (what just happened, newest first) "
        "or sources (each logical source's landings: count, latest, contracts)."
    ),
)
@click.option(
    "--limit",
    type=click.IntRange(1, contracts.ORIENT_MAX_LIMIT),
    default=contracts.ORIENT_DEFAULT_LIMIT,
    show_default=True,
)
@click.option("--cursor", default=None, help="next_cursor from the previous page.")
@click.option(
    "--at",
    "at_oid",
    default=None,
    help=(
        "An accepted generation's Git OID, a unique 12+ hex prefix, or its number "
        "(all digits, 11 or fewer, is always a generation number)."
    ),
)
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def orient(
    kind: str | None,
    section: str | None,
    limit: int,
    cursor: str | None,
    at_oid: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Map accepted state: kinds, predicates, artifacts, attention and next commands.

    The map prints one line per kind (its first fields, then a count);
    --kind prints one kind's every field, and --json carries everything.
    """

    workspace_root = _local_workspace_root(fallback_to_start=False).workspace_root
    vocabulary_at = None if workspace_root is None else workspace_vocabulary_at(workspace_root)
    result = _server_call(
        lambda client, instance_id: client.orient(
            instance_id,
            kind=kind,
            section=cast(Any, section),
            limit=limit,
            cursor=cursor,
            at=at_oid,
            evaluation_time=evaluation_time,
            surface="cli",
            vocabulary_at=vocabulary_at,
        ),
        command_name="cruxible orient",
    )
    if workspace_root is not None:
        result = workspace_vocabulary_freshness(
            workspace_root, workspace_floor_freshness(workspace_root, result)
        )
    rendered = result.model_dump(mode="json")
    if output_json:
        _emit_json(rendered)
        return
    click.echo(_render_orient(rendered))


@playbill_group.command("stub")
@click.option(
    "--out",
    "out_path",
    default=None,
    help="Write the stub to this path instead of standard output.",
)
@handle_errors
def world_stub(out_path: str | None) -> None:
    """Write a .pyi stub typing this instance's accepted vocabulary.

    A world is discovered at runtime, so an editor and a model both see `Any`
    where the instance's own kinds, predicates and enum members are. This
    writes them down as types; Subjects are typed by kind, never one attribute
    each, so the stub moves only when the vocabulary does, and its header names
    the vocabulary's digest. `cruxible init` and `workspace attach` write it to
    .cruxible/world.pyi, and with floor delivery on the daemon keeps that file
    current; this is the explicit write (standard output, or --out).
    """

    rendered = _server_call(
        lambda client, instance_id: render_world_stub_for(
            client,
            instance_id,
            workspace=Path.cwd(),
        ),
        command_name="cruxible stub",
    )
    if out_path is None:
        click.echo(rendered, nl=False)
        return
    target = Path(out_path).expanduser()
    if target.parent != Path("."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    click.echo(f"Wrote {target}")


@playbill_group.group("vocabulary")
def vocabulary_group() -> None:
    """The vocabulary file: every kind and field in .cruxible/vocabulary.yaml.

    `export` writes it from accepted state; edit it, `plan` shows what would
    change, `apply` submits that as one change set. A kind or field left out is
    untouched; only `retired: true` retires.
    """


def _vocabulary_workspace(command_name: str) -> Path:
    resolution = _local_workspace_root(required=command_name)
    _emit_git_workspace_note(resolution)
    assert resolution.workspace_root is not None
    return Path(resolution.workspace_root)


def _vocabulary_path(file: str | None) -> Path | None:
    """A FILE argument as the shell meant it: relative to the current directory."""

    return None if file is None else Path(file).expanduser().resolve()


@vocabulary_group.command("export")
@click.option("--force", is_flag=True, help="Replace a file holding changes not in accepted state.")
@json_option
@handle_errors
def export_vocabulary(force: bool, output_json: bool) -> None:
    """Write the accepted vocabulary to .cruxible/vocabulary.yaml.

    Shorthand where it says the same thing, the full ClaimType input form
    otherwise; every entry is checked by lowering it back. The header records the
    accepted coordinate it was read at, which plan and apply read against.
    """

    workspace = _vocabulary_workspace("cruxible vocabulary export")
    result = _server_call(
        lambda client, instance_id: vocabulary_export_for(
            client, instance_id, workspace=workspace, force=force
        ),
        command_name="cruxible vocabulary export",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(str(result))


@vocabulary_group.command("plan")
@click.argument("file", required=False)
@json_option
@handle_errors
def plan_vocabulary(file: str | None, output_json: bool) -> None:
    """Show what applying the vocabulary file would add, replace and retire.

    Reads at the export the file names; writes nothing. Claims an enum mapping
    moves are listed first; definitions the file leaves out are listed as left
    as they are.
    """

    workspace = _vocabulary_workspace("cruxible vocabulary plan")
    plan = _server_call(
        lambda client, instance_id: vocabulary_plan_for(
            client, instance_id, workspace=workspace, path=_vocabulary_path(file)
        ),
        command_name="cruxible vocabulary plan",
    )
    if output_json:
        _emit_json(plan.model_dump(mode="json"))
        return
    click.echo(str(plan))


@vocabulary_group.command("apply")
@click.argument("file", required=False)
@click.option("--rationale", default=None, help="Why the vocabulary changes.")
@click.option("--no-accept", is_flag=True, help="Keep the proposal instead of landing it.")
@json_option
@handle_errors
def apply_vocabulary(
    file: str | None, rationale: str | None, no_accept: bool, output_json: bool
) -> None:
    """Submit the vocabulary file's changes as one change set; lands when policy allows."""

    workspace = _vocabulary_workspace("cruxible vocabulary apply")
    result = _server_call(
        lambda client, instance_id: vocabulary_apply_for(
            client,
            instance_id,
            workspace=workspace,
            path=_vocabulary_path(file),
            rationale=rationale,
            accept="never" if no_accept else "if_allowed",
        ),
        command_name="cruxible vocabulary apply",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(str(result))


@playbill_group.group("floor")
def floor_group() -> None:
    """The greppable floor of accepted state: export it, and choose who delivers it."""


@floor_group.command("export")
@click.option("--force", is_flag=True, help="Replace a non-empty .cruxible/floor cache.")
@click.option(
    "--with-discovery",
    is_flag=True,
    help=(
        "Also write the discovery cards (subjects/, claim-types/, procedures/, "
        "coverage-manifest.json); refresh after activation keeps them."
    ),
)
@json_option
@handle_errors
def export_floor(
    force: bool,
    with_discovery: bool,
    output_json: bool,
) -> None:
    """Write the accepted greppable floor to a deterministic local tree."""

    include: tuple[contracts.FloorExportPart, ...] = ("discovery",) if with_discovery else ()
    workspace_resolution = _local_workspace_root(required="cruxible floor export")
    _emit_git_workspace_note(workspace_resolution)
    workspace_root = workspace_resolution.workspace_root
    assert workspace_root is not None
    transport = _workspace_config_transport()
    if include:
        # The discovery cards are a full export's; they never travel in a delta.
        result, written = _server_call(
            lambda client, instance_id: write_workspace_floor(
                lambda: client.export_floor(instance_id, **floor_export_parts(include)),
                delivery=lambda: daemon_floor_delivery(
                    client, instance_id, workspace_root, include=include
                ),
                instance_id=instance_id,
                workspace=workspace_root,
                include=include,
                force=force,
                **transport,
            ),
            command_name="cruxible floor export",
        )
        manifest: dict[str, Any] = dict(result.manifest)
        touched = len(result.files)
    else:
        # The default floor goes through the one shared apply: the daemon is
        # sent this floor's own generation and answers with only what changed.
        delta, written = _server_call(
            lambda client, instance_id: write_workspace_floor_delta(
                lambda generation, renderer: client.floor_delta(
                    instance_id, base_generation=generation, base_renderer=renderer
                ),
                delivery=lambda: daemon_floor_delivery(client, instance_id, workspace_root),
                instance_id=instance_id,
                workspace=workspace_root,
                force=force,
                **transport,
            ),
            command_name="cruxible floor export",
        )
        manifest = json.loads(Path(written.destination, "manifest.json").read_text("utf-8"))
        touched = len(delta.files) + len(delta.tombstones)
    written = _with_git_workspace_note(written)
    if output_json:
        payload = manifest
        if written.git_workspace_note is not None:
            payload["git_workspace_note"] = written.git_workspace_note.model_dump(mode="json")
        _emit_json(payload)
        return
    if written.status == "unchanged":
        click.echo(f"Floor already current at {written.destination}; nothing written")
    else:
        click.echo(f"Wrote {touched} floor file(s) to {written.destination}")
    click.echo(f"Floor digest: {manifest['floor_digest']}")
    click.echo(f"Coordinate: {written.coordinate.git_oid}")


@floor_group.command("delivery")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--instance-id", default=None, help="Existing registered daemon host ID.")
@json_option
@handle_errors
def floor_delivery(state: str, instance_id: str | None, output_json: bool) -> None:
    """Choose whether the local daemon writes this workspace's floor.

    On (the default for an attached workspace), the daemon refreshes the floor
    after every accepted generation and is its only writer, so ``floor export``
    over TCP refuses. Off, nothing delivers it and ``floor export`` pulls it.
    Needs the local --server-socket the workspace was attached through.
    """

    ensure_cli_daemon()
    if not _root_ctx_obj().get("server_socket"):
        raise click.UsageError("floor delivery requires a local --server-socket")
    selected = instance_id or _require_instance_id()
    result = _dispatch_cli(
        lambda client: client.set_floor_delivery(selected, enabled=state == "on"),
        lambda: None,
        allow_local=False,
        command_name="cruxible floor delivery",
    )
    assert result is not None
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(f"Floor delivery {state} for {selected}")


@playbill_group.group("coverage")
def coverage_group() -> None:
    """Deliver what working files have to do with accepted state."""


def _coverage_options(function: Callable[..., Any]) -> Callable[..., Any]:
    function = click.option(
        "--bind",
        "bind_values",
        multiple=True,
        help="Override the catalog for one path as PATH=PLANE:IDENTITY (repeatable).",
    )(function)
    function = click.option(
        "--bindings",
        "bindings_path",
        default=None,
        type=PayloadFile(),
        help="A mapping of working path to PLANE:IDENTITY overriding the catalog (- for stdin).",
    )(function)
    function = click.option(
        "--root",
        default=None,
        type=click.Path(file_okay=False),
        help="Working root every bound path is read under (default: this worktree).",
    )(function)
    return function


def _coverage_bindings(
    root: Path,
    bind_values: tuple[str, ...],
    bindings_path: str | None,
) -> WorkingPathBindingsV1:
    """The file catalog's bindings, overridden path by path by declared ones.

    The catalog is the one path-to-source mapping; coverage never infers one.
    """

    try:
        declared: dict[str, str] = WorkspaceFiles(root).coverage_bindings()
    except SourceSelectionError:
        if not (bind_values or bindings_path):
            raise
        declared = {}
    if bindings_path is not None:
        for path, value in _read_mapping(bindings_path).items():
            if not isinstance(value, str):
                raise click.BadParameter("each binding value must be PLANE:IDENTITY")
            declared[str(path)] = value
    for entry in bind_values:
        path, separator, value = entry.partition("=")
        if not separator or not path or not value:
            raise click.BadParameter("a binding must be PATH=PLANE:IDENTITY")
        declared[path] = value

    return bindings_from_mapping(declared)


def _coverage_observations(
    bindings: WorkingPathBindingsV1,
    *,
    root: Path,
    files: tuple[str, ...],
    ranges: tuple[str, ...],
    grep_path: str | None,
    whole_working_set: bool,
) -> tuple[WorkingSourceObservation, ...]:
    """Read the working set locally and hand the operation observations.

    Whole-source and windowed requests over the same path collapse to one
    observation, because a source is observed once per snapshot. A path named
    as changed is asked about whole, which is what makes an edit's drift
    visible without the caller having to guess which window moved.
    """

    grep_text = None if grep_path is None else read_payload_text(grep_path)
    return observe_workspace(
        bindings,
        root=root,
        files=files,
        ranges=ranges,
        grep_text=grep_text,
        whole_working_set=whole_working_set,
    )


def _resolved_coverage_answer(
    observations: tuple[WorkingSourceObservation, ...],
    *,
    command_name: str,
) -> tuple[CoverageResultV3, tuple[contracts.ClaimLine, ...]]:
    """The frozen answer and, beside it, the Claim lines its cards address."""

    result = _server_call(
        lambda client, instance_id: client.resolve_coverage(
            instance_id,
            observations=[item.model_dump(mode="json") for item in observations],
        ),
        command_name=command_name,
    )
    return CoverageResultV3.model_validate(result.result), tuple(result.claims)


@coverage_group.command("resolve")
@_coverage_options
@click.option("--file", "files", multiple=True, help="A changed or read working path.")
@click.option("--range", "ranges", multiple=True, help="A read selection as PATH:START-END.")
@click.option(
    "--grep-results",
    "grep_path",
    default=None,
    type=PayloadFile(),
    help="A `grep -n` result batch to resolve as one operation (- for stdin).",
)
@click.option("--all", "whole_working_set", is_flag=True, help="Resolve every bound file.")
@click.option(
    "--view",
    type=click.Choice(["cards", "manifest"]),
    default="cards",
    show_default=True,
    help=(
        "cards: each governed span's Claims and a summary; manifest: the coverage "
        "manifest (epoch, health, completeness, scope) and each card's full binding."
    ),
)
@brief_option
@json_option
@handle_errors
def resolve_coverage(
    bind_values: tuple[str, ...],
    bindings_path: str | None,
    root: str | None,
    files: tuple[str, ...],
    ranges: tuple[str, ...],
    grep_path: str | None,
    whole_working_set: bool,
    view: str,
    output_brief: bool,
    output_json: bool,
) -> None:
    """Resolve what the working files you just read or changed are governed by.

    Files are bound to their sources by the workspace's file catalog;
    --bind and --bindings override it path by path. Governed spans are
    annotated inline and the ungoverned majority is summarized once;
    --all --view manifest renders the coverage manifest over every bound file.
    Changes no accepted state. It writes the coverage manifest cache, and a
    consumption receipt when the daemon runs with CRUXIBLE_CONSUMPTION_RECEIPTS=on.
    """

    if root is not None:
        working_root = Path(root).expanduser().resolve()
    else:
        resolution = _local_workspace_root()
        _emit_git_workspace_note(resolution)
        working_root = resolution.workspace_root or Path.cwd().resolve()
    bindings = _coverage_bindings(working_root, bind_values, bindings_path)
    observations = _coverage_observations(
        bindings,
        root=working_root,
        files=files,
        ranges=ranges,
        grep_path=grep_path,
        whole_working_set=whole_working_set,
    )
    result, claims = _resolved_coverage_answer(
        observations, command_name="cruxible coverage resolve"
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if output_brief:
        _emit_brief(
            outcome=(
                f"{result.health} ({result.summary.exact} exact, {result.summary.drifted} drifted)"
            ),
            ids={"coordinate": result.at.git_oid, "epoch": str(result.epoch)},
            next_command=(
                "cruxible next --brief"
                if result.summary.drifted or result.health != "complete"
                else None
            ),
        )
        return
    from cruxible_core.coverage.render import render_card, render_coverage_text

    rendered = (
        (
            *render_coverage_manifest(result),
            # Each card's whole binding: commitments, captures, handle, dependents.
            *(render_card(card) for span in result.spans for card in span.cards),
        )
        if view == "manifest"
        else render_coverage_text(
            result,
            claims,
            paths={item.source: item.path for item in bindings.bindings},
        )
    )
    for line in rendered:
        click.echo(line)


__all__ = ["playbill_group"]
