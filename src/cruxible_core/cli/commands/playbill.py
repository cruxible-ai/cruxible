"""Cruxible Family-1 CLI, including local compilation and client-held signing."""

from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import subprocess
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
    unpack_artifact,
    write_layout,
)
from cruxible_client.authoring.attestations import (
    append_prepared_claim_attestation,
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
    add_catalog_entry,
    compile_client_file_context,
    load_file_catalog,
    media_type_for,
    remove_catalog_entry,
    root_aliases,
)
from cruxible_client.authoring.inputs import AuthoringInput, ClaimInput
from cruxible_client.authoring.sdk_types import SourceSelectionError
from cruxible_client.authoring.selectors import WorkspaceFiles
from cruxible_client.authoring.signing import sign_runtime_credential_mint
from cruxible_client.authoring.workspace import (
    WorkspaceAttachmentError,
    daemon_floor_delivery,
    floor_export_parts,
    observe_next_workspace_with_coverage,
    observe_projection_coverage,
    validate_workspace_config_write,
    workspace_floor_freshness,
    write_workspace_config,
    write_workspace_floor,
    write_workspace_floor_delta,
)
from cruxible_client.authoring.world_stub import render_world_stub_for, write_world_stub
from cruxible_client.authoring.write_evidence import observe_changes, observe_evidence
from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.authoring.models import BlockSyncResult
from cruxible_client.contracts.builtin_claim_types import BUILTIN_CLAIM_TYPES, BuiltinClaimTypeName
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_attestations import (
    ClaimStance,
    PreparedClaimAttestationRequest,
)
from cruxible_client.contracts.claim_type_upgrade import ClaimTypeUpgradeRequest
from cruxible_client.contracts.codes import normalize_code
from cruxible_client.contracts.compact_query import QueryClaim
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.duplicates import (
    DUPLICATES_AUDIT_DEFAULT_LIMIT,
    DUPLICATES_AUDIT_MAX_LIMIT,
)
from cruxible_client.contracts.errors import (
    CanonicalEncodingError,
    SigningKeyError,
    SinceRequestInvalid,
)
from cruxible_client.contracts.file_catalog import FileCatalog, FileCompilationBundle
from cruxible_client.contracts.get_display import (
    GET_CLI_HISTORY_VALUE_WIDTH,
    GET_CLI_VALUE_WIDTH,
    get_value_display,
)
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitBuildRequest,
    KitChangeResult,
    KitRemoveRequest,
    kit_version_key,
)
from cruxible_client.contracts.line_dispatch import LineDispatchItem, LineEvaluateResult
from cruxible_client.contracts.principals import is_canonical_principal_id
from cruxible_client.contracts.procedures.results import ProcedureHaltTerminal
from cruxible_client.contracts.procedures.windows import TriggerEventReference
from cruxible_client.contracts.proposal_models import canonical_proposal_ref_name
from cruxible_client.contracts.provider_installation import (
    ProviderInstallRequest,
    ProviderMoveRequest,
    ProviderRetireRequest,
)
from cruxible_client.contracts.read_values import shown_plain
from cruxible_client.contracts.repairs import RepairOperation, render_served_repair
from cruxible_client.contracts.resolution_contracts import ResolutionContractReference
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_client.contracts.types import PrincipalKind, PrincipalRecord
from cruxible_client.contracts.validation_messages import validation_summary
from cruxible_client.contracts.write import (
    Change,
    FileEvidence,
    RetireRequest,
    SetRequest,
    SubjectRef,
    WriteOutcome,
    WriteRequest,
)
from cruxible_client.errors import DataValidationError
from cruxible_client.kits import (
    KIT_ARTIFACT,
    check_kit_updates,
    fetch_kit_image,
    resolve_kit,
    stage_kit_providers,
    write_kit_directory,
)
from cruxible_core.adapters.block_detach import detach_projection_pages
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord, claim_type_input_template
from cruxible_core.claims.claim_type_migrations import ClaimTypeMigrationRequestAny
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _dispatch_cli,
    _echo_active_write_target,
    _echo_creation_write_target,
    _emit_brief,
    _emit_json,
    _remember_principal_settings,
    _require_instance_id,
    _root_ctx_obj,
    _transport_target,
    and_activate_option,
    brief_option,
    change_control_options,
    echo_preview_next,
    json_option,
)
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
    render_coverage_result,
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
from cruxible_core.governance.keys import (
    ClientPrincipalKeyTarget,
    GeneratedKeyMaterial,
    adopt_client_principal_key,
    generate_client_principal_key,
    preview_client_principal,
    validate_client_principal_key_target,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
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

ResultT = TypeVar("ResultT")
_ISO8601_DURATION = re.compile(
    r"P(?:(?P<weeks>[0-9]+)W|(?:(?P<days>[0-9]+)D)?"
    r"(?:T(?:(?P<hours>[0-9]+)H)?(?:(?P<minutes>[0-9]+)M)?"
    r"(?:(?P<seconds>[0-9]+)(?:\.(?P<fraction>[0-9]{1,6}))?S)?)?)"
)


def _parse_expiring_duration(
    _context: click.Context,
    _parameter: click.Parameter,
    value: str,
) -> int:
    match = _ISO8601_DURATION.fullmatch(value)
    if match is None or not any(
        match.group(key) is not None
        for key in (
            "weeks",
            "days",
            "hours",
            "minutes",
            "seconds",
        )
    ):
        raise click.BadParameter(
            "use a nonnegative ISO-8601 duration with days, weeks, hours, minutes, or seconds "
            "(for example P7D or PT12H)"
        )
    if "T" in value and not any(
        match.group(key) is not None
        for key in (
            "hours",
            "minutes",
            "seconds",
        )
    ):
        raise click.BadParameter("the ISO-8601 time section must contain a duration component")
    fraction = match.group("fraction") or ""
    return (
        int(match.group("weeks") or 0) * 604_800_000_000
        + int(match.group("days") or 0) * 86_400_000_000
        + int(match.group("hours") or 0) * 3_600_000_000
        + int(match.group("minutes") or 0) * 60_000_000
        + int(match.group("seconds") or 0) * 1_000_000
        + int(fraction.ljust(6, "0") or 0)
    )


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
_CLAIM_TYPE_MIGRATION_ADAPTER: TypeAdapter[ClaimTypeMigrationRequestAny] = TypeAdapter(
    ClaimTypeMigrationRequestAny
)


def _authoring_examples_for(payload: Mapping[str, Any]) -> tuple[str, ...]:
    kind = payload.get("kind")
    if kind == "procedure":
        return ("procedure",)
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
class _LocalGitWorkspaceResult:
    """The CWD-selected worktree and any ignored ambient-repository advisory."""

    workspace_root: Path | None
    note: _GitWorkspaceEnvironmentNote | None


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


def _explicit_git_workspace_root(value: str) -> Path:
    try:
        selected = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise click.UsageError(f"--workspace is not an accessible directory: {value}") from exc
    workspace_root = containing_git_workspace_root(selected)
    if workspace_root is None:
        raise click.UsageError("--workspace must select a path inside one Git worktree")
    return workspace_root


def _workspace_config_transport() -> dict[str, str]:
    obj = _root_ctx_obj()
    if obj.get("server_socket"):
        return {"server_socket": str(obj["server_socket"])}
    if obj.get("server_url"):
        return {"server_url": str(obj["server_url"])}
    raise click.UsageError("Server mode is required to attach a workspace")


def _local_git_workspace_root() -> _LocalGitWorkspaceResult:
    """Resolve the CWD worktree without trusting an inherited repository selector."""

    repository_selectors = ("GIT_DIR", "GIT_WORK_TREE")
    inherited_selector = any(name in os.environ for name in repository_selectors)
    workspace_root = containing_git_workspace_root(Path.cwd())
    inherited_root = _inherited_git_workspace_root() if inherited_selector else workspace_root
    note = None
    if (
        workspace_root is not None
        and inherited_root is not None
        and inherited_root != workspace_root
    ):
        note = _GitWorkspaceEnvironmentNote(
            code="inherited_git_workspace_ignored",
            cwd_workspace_root=workspace_root,
            inherited_workspace_root=inherited_root,
        )
    return _LocalGitWorkspaceResult(workspace_root=workspace_root, note=note)


def _emit_git_workspace_note(resolution: _LocalGitWorkspaceResult) -> None:
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


def _custody_workspace_root() -> Path | None:
    """Resolve the one workspace boundary shared by every custody operation."""

    resolution = _local_git_workspace_root()
    _emit_git_workspace_note(resolution)
    return resolution.workspace_root


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


def _init_resume_marker(target: ClientPrincipalKeyTarget) -> Path:
    return target.directory / f".cruxible-init-resume-{target.principal.principal_id}.json"


def _init_resume_payload(
    target: ClientPrincipalKeyTarget,
    *,
    transport: str,
    instance_id: str,
    public_key: str,
) -> dict[str, str]:
    return {
        "tag": "playbill-init-key-resume-v1",
        "transport": transport,
        "instance_id": instance_id,
        "principal_id": target.principal.principal_id,
        "kind": target.principal.kind,
        "public_key": public_key,
    }


def _read_init_resume_marker(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SigningKeyError(
            f"Cruxible init retry marker is missing or malformed: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise SigningKeyError(f"Cruxible init retry marker is missing or malformed: {path}")
    return payload


def _write_init_resume_marker(path: Path, payload: Mapping[str, str]) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(canonical_bytes(dict(payload)) + b"\n")
        path.chmod(0o600)
    except FileExistsError as exc:
        raise SigningKeyError(f"refusing to replace Cruxible init retry marker: {path}") from exc


def _adopt_init_retry_key(
    target: ClientPrincipalKeyTarget,
    *,
    forbidden_roots: tuple[Path, ...],
    transport: str,
    instance_id: str,
) -> GeneratedKeyMaterial:
    marker = _init_resume_marker(target)
    if not marker.is_file() or marker.is_symlink():
        raise SigningKeyError(
            "Cruxible is already initialized for this custody, or the existing key material "
            "was provisioned independently; refusing to reuse it without this init's retry "
            f"marker: {marker}"
        )
    material = adopt_client_principal_key(
        target.directory,
        principal_id=target.principal.principal_id,
        kind=target.principal.kind,
        forbidden_roots=forbidden_roots,
    )
    expected = _init_resume_payload(
        target,
        transport=transport,
        instance_id=instance_id,
        public_key=material.principal.public_key,
    )
    if _read_init_resume_marker(marker) != expected:
        raise SigningKeyError(
            f"existing key material belongs to a different Cruxible init target: {marker}"
        )
    return material


def _prepare_init_custody(
    *,
    transport: str,
    instance_id: str,
    specifications: tuple[tuple[Path, str, PrincipalKind], ...],
    forbidden_roots: tuple[Path, ...],
) -> tuple[tuple[GeneratedKeyMaterial, ...], tuple[Path, ...]]:
    targets = tuple(
        validate_client_principal_key_target(
            directory,
            principal_id=principal_id,
            kind=kind,
            forbidden_roots=forbidden_roots,
        )
        for directory, principal_id, kind in specifications
    )
    if len({target.principal.principal_id for target in targets}) != len(targets):
        raise SigningKeyError("Cruxible init principal IDs must be distinct")
    key_paths = tuple(
        path for target in targets for path in (target.private_key_path, target.public_key_path)
    )
    if len(set(key_paths)) != len(key_paths):
        raise SigningKeyError("Cruxible init custody key paths must be distinct")

    prepared: list[GeneratedKeyMaterial | None] = []
    for target in targets:
        private_exists = target.private_key_path.exists()
        public_exists = target.public_key_path.exists()
        marker = _init_resume_marker(target)
        if private_exists != public_exists:
            raise SigningKeyError(
                f"Cruxible init retry requires a complete key pair for "
                f"{target.principal.principal_id}"
            )
        if private_exists:
            prepared.append(
                _adopt_init_retry_key(
                    target,
                    forbidden_roots=forbidden_roots,
                    transport=transport,
                    instance_id=instance_id,
                )
            )
        else:
            if marker.exists():
                raise SigningKeyError(
                    f"Cruxible init retry marker has no complete key pair: {marker}"
                )
            prepared.append(None)

    materials: list[GeneratedKeyMaterial] = []
    markers: list[Path] = []
    for target, existing in zip(targets, prepared, strict=True):
        material = existing
        marker = _init_resume_marker(target)
        if material is None:
            material = generate_client_principal_key(
                target.directory,
                principal_id=target.principal.principal_id,
                kind=target.principal.kind,
                forbidden_roots=forbidden_roots,
            )
            _write_init_resume_marker(
                marker,
                _init_resume_payload(
                    target,
                    transport=transport,
                    instance_id=instance_id,
                    public_key=material.principal.public_key,
                ),
            )
        materials.append(material)
        markers.append(marker)
    return tuple(materials), tuple(markers)


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
    """Attach this Git worktree to a daemon host, initialized or not.

    A host with no worktree registers this one (an initialized host included,
    as long as the worktree is in its ledger's Git object format); a host
    registered to this worktree just gets the client config.
    """

    resolution = _local_git_workspace_root()
    _emit_git_workspace_note(resolution)
    workspace = resolution.workspace_root
    if workspace is None:
        raise click.UsageError("cruxible workspace attach must run inside one Git worktree")
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
    if registration.status != "registered":
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
    if dry_run:
        click.echo(f"{workspace} is already attached to Cruxible host {selected}; nothing changes")
        return
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
    _write_world_stub(workspace, selected)
    result = contracts.WorkspaceAttachResult(
        instance_id=selected,
        workspace_root=str(workspace),
        config_path=str(config_path),
        transport=transport,
    )
    result = _with_git_workspace_note(result)
    if output_json:
        _emit_json(_json_receipt(result))
        return
    click.echo(f"Attached workspace {workspace} to Cruxible host {selected}")
    click.echo(f"Config: {config_path}")


@workspace_group.command("detach")
@click.option("--instance-id", default=None, help="Existing registered daemon host ID.")
@change_control_options
@json_option
@handle_errors
def detach_workspace(
    instance_id: str | None, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Release a daemon host from the Git worktree it is registered against.

    The registry allows one host per worktree, so re-binding a worktree to a
    second host needs the first one released. Nothing governed changes: the host
    keeps its ledger and every read it has ever served, and stops being the host
    of this directory. Attach the worktree to the new host afterwards.
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
    help="Explicit Git workspace to configure; remote paths stay client-local.",
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

    git_workspace: Path | None
    if workspace_path is not None:
        git_workspace = _explicit_git_workspace_root(workspace_path)
    else:
        workspace_resolution = _local_git_workspace_root()
        _emit_git_workspace_note(workspace_resolution)
        git_workspace = workspace_resolution.workspace_root
        _refuse_tcp_workspace_operation(git_workspace)
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
    help="Optional second ordinary-principal custody directory outside the workspace.",
)
@click.option(
    "--require-independent-approval",
    is_flag=True,
    help="Require one non-creator ordinary approval for governed changes.",
)
@click.option("--recovery-key-dir", default=None, help="Optional offline recovery custody dir.")
@click.option("--recovery-principal-id", default="recovery", show_default=True)
@click.option("--profile", type=click.Choice(["local", "cloud"]), default="local")
@click.option(
    "--workspace",
    "workspace_path",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Explicit Git workspace to configure; remote paths stay client-local.",
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
    output_json: bool,
) -> None:
    """Make you the owner: create client custody and bootstrap the approval policy.

    With no instance selected, init first creates the host (as `cruxible host
    create` does) and selects it, so a new project is one bare `cruxible init`.
    The owner principal ID defaults to your OS username and the key directory to
    a per-user config path outside the workspace and the daemon state root. The
    CLI remembers the owner's settings and acts as the owner from then on
    (`cruxible context use --principal ID` switches).

    With daemon auth off, the owner principal ID is the identity this process
    claims for the init request; no bootstrap secret is needed.
    """

    git_workspace = (
        _explicit_git_workspace_root(workspace_path)
        if workspace_path is not None
        else _custody_workspace_root()
    )
    if require_independent_approval and reviewer_key_dir is None:
        raise click.UsageError("--require-independent-approval requires --reviewer-key-dir")
    if workspace_path is None:
        _refuse_tcp_workspace_operation(git_workspace)
    if not (_root_ctx_obj().get("server_url") or _root_ctx_obj().get("server_socket")):
        raise click.UsageError("Local execution disabled for cruxible init; use server mode.")
    transport = _transport_target(_root_ctx_obj())
    if transport is None:  # pragma: no cover - guarded by _get_client above
        raise click.UsageError("Server mode is required for cruxible init")
    configured = _root_ctx_obj().get("principal_id")
    if principal_id is None:
        principal_id = configured if configured is not None else _os_principal_id()
    selected_instance = _root_ctx_obj().get("instance_id")
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
    if get_runtime_bearer_token() is None:
        # Auth off: the init request claims the owner it creates. With a bearer
        # credential the credential decides who acts, so no claim is sent.
        _root_ctx_obj()["principal_id"] = principal_id
    workspace = git_workspace
    custody_roots = _init_custody_roots(workspace)
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
    specifications: list[tuple[Path, str, PrincipalKind]] = [
        (owner_key_dir, principal_id, "ordinary")
    ]
    if reviewer_key_dir is not None:
        specifications.append((Path(reviewer_key_dir).expanduser(), "reviewer", "ordinary"))
    if recovery_key_dir is not None:
        specifications.append(
            (Path(recovery_key_dir).expanduser(), recovery_principal_id, "recovery")
        )
    materials, markers = _prepare_init_custody(
        transport=transport,
        instance_id=selected,
        specifications=tuple(specifications),
        forbidden_roots=custody_roots,
    )
    owner = materials[0]
    reviewer = materials[1] if reviewer_key_dir is not None else None
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
    _activate_server_instance(result.instance_id)
    owner_token = _mint_owner_credential(owner, principal_id=principal_id)
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
    if git_workspace is not None:
        _write_world_stub(git_workspace, result.instance_id)
    if output_json:
        _emit_json({**_json_receipt(result), "owner_settings_path": str(settings)})
        return
    click.echo(f"Cruxible initialized at {result.coordinate.git_oid}")
    click.echo(f"Approval policy: {result.approval_policy_mode}")
    click.echo(f"Workspace refs: {result.workspace_advertisement.status}")
    if result.workspace_advertisement.failure_code is not None:
        click.echo(f"Workspace ref failure: {result.workspace_advertisement.failure_code}")
    click.echo(f"Owner public key: {owner.principal.public_key}")
    click.echo(f"Owner private key retained locally at: {owner.private_key_path}")
    click.echo(f"Owner principal: {principal_id}")
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
    if reviewer is not None:
        click.echo(f"Reviewer public key: {reviewer.principal.public_key}")
        click.echo(f"Reviewer private key retained locally at: {reviewer.private_key_path}")


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


def _create_host_for_init(transport: str, *, workspace_root: str | None) -> str:
    """Allocate the host a bare init initializes, and select it before anything else.

    Selecting it first means a retry after a failed init initializes this same
    host instead of allocating another. The host is created attached to the
    worktree init will attach (on a local socket), so a worktree already
    attached to another host refuses here, before any host exists.
    """

    _echo_creation_write_target({})
    created = _dispatch_cli(
        lambda client: client.create_host(workspace_root=workspace_root, dry_run=False),
        lambda: None,
        allow_local=False,
        command_name="cruxible init",
    )
    assert isinstance(created, contracts.HostResult)
    _activate_server_instance(created.instance_id)
    click.echo(f"Created Cruxible host {created.instance_id} @ {transport}", err=True)
    return created.instance_id


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
        "Repair: to start a new project, `cruxible context clear` (or select no host) and run "
        f"`cruxible init` again; to act on {instance_id}, use its settings "
        f"(`cruxible context use --principal ID`)"
    )
    target = validate_client_principal_key_target(
        key_dir, principal_id=principal_id, kind="ordinary", forbidden_roots=custody_roots
    )
    if _init_resume_marker(target).exists():
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


def _mint_owner_credential(owner: GeneratedKeyMaterial, *, principal_id: str) -> str | None:
    """With daemon auth on, mint the new owner's own admin credential; return its token.

    The operator credential that ran init acts as no principal, so the owner
    needs one that acts as it. The owner's fresh key signs its consent. With auth
    off nothing is minted: the configured principal ID is the identity.
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
            permission_mode="admin",
            principal_proof=sign_runtime_credential_mint(
                instance_id=instance_id,
                principal_id=principal_id,
                permission_mode="admin",
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
    if result.detail is not None:
        click.echo(f"Detail: {printable(result.detail)}")
    echo_preview_next(result.status, result.coordinate)


@ledger_group.command("publish")
@click.option("--timeout", type=click.FloatRange(0, 60), default=60.0, show_default=True)
@change_control_options
@json_option
@handle_errors
def ledger_publish(timeout: float, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Wait for publication to the configured mirror; timeout 0 only requests it."""

    result = _server_call(
        lambda client, instance_id: client.publish_ledger(
            instance_id, timeout=timeout, dry_run=dry_run, at=at
        ),
        command_name="cruxible ledger publish",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Publication: {result.status}")
    if result.wait_sequence is not None:
        acknowledged = result.published_sequence >= result.wait_sequence
        click.echo(
            f"Request {result.wait_sequence}: "
            f"{'acknowledged' if acknowledged else 'awaiting acknowledgment'}"
        )
    if result.detail is not None:
        click.echo(f"Detail: {printable(result.detail)}")
    echo_preview_next(result.status, result.coordinate)


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
        interfaces = ", ".join(installed.interfaces) or "-"
        click.echo(f"  {installed.build}{runs}{marker}: {interfaces}")
    if result.packages:
        click.echo("Provider repository:")
    for package in result.packages:
        click.echo(f"  {package.name} {package.version}: {', '.join(package.interfaces)}")
    if result.detail:
        click.echo(result.detail)


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
    fetches, builds, registers and proposes nothing.
    """
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
        click.echo(f"{result.build or result.provider_id}: {result.status}")
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
                click.echo(f"{operation.interface_id}: {', '.join(operation.missing_requirements)}")


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
@click.option("--only", multiple=True, help="Move only this Procedure or Blueprint (repeatable).")
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
    """Move definitions to NAME's build; carry Lines disabled and Mandates propose-only."""
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
        else:
            click.echo(
                f"  {phase}carry: {item.identity}: {item.old_grant} -> propose"
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
    "re_adds_retired": "re-adds a definition you retired",
    "takes_over_outside_definition": "takes over a definition you defined outside the kit",
    "replaces_carried_definition": "replaces a definition the kit depends on",
    "release_dropped": "the release dropped it",
}


def _echo_kit_change(result: KitChangeResult) -> None:
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
    _echo_provider_repins(result.repins, result.stops, result.automation_carries)
    if result.provenance is not None:
        provenance = result.provenance
        click.echo(
            f"Built by {provenance.principal_id or 'an unattributed principal'} on "
            f"{provenance.instance_id} at {provenance.coordinate.git_oid[:12]} (claimed)"
        )
    groups: dict[str, list[str]] = {}
    for item in result.plan:
        if item.action == "unchanged":
            continue
        kind = (item.identity or item.path).partition(":")[0]
        line = f"  {item.action}: {item.identity or item.path}"
        notes = []
        if item.consequence is not None:
            notes.append(_KIT_CONSEQUENCE_TEXT[item.consequence])
        if item.dependent_count:
            notes.append(f"{item.dependent_count} dependent(s)")
        if item.detail is not None:
            notes.append(printable(item.detail))
        if notes:
            line += f" ({'; '.join(notes)})"
        groups.setdefault(kind, []).append(line)
    for kind in sorted(groups):
        click.echo(f"{kind}:")
        for line in groups[kind]:
            click.echo(line)
    if result.detail:
        click.echo(printable(result.detail))
    for refusal in result.refusals:
        click.echo(refusal.code)
        click.echo(f"Next: {printable(refusal.repair)}")
    for item in result.plan:
        if (
            result.status in {"would_propose", "would_block"}
            and item.action == "replace"
            and item.consequence == "takes_over_outside_definition"
        ):
            click.echo(f"Next: rerun cruxible kit add with --keep {item.identity} to keep yours.")
    if result.proposal_id:
        click.echo(f"Proposal: {result.proposal_id}")
        if result.status == "proposed":
            if result.approval_required:
                click.echo(
                    f"Next: cruxible proposal approve {result.proposal_id} "
                    "--signer-id ID --key FILE (the approval that completes it activates it)"
                )
            else:
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
        "A published provider package directory the kit names as the default "
        "implementation of its interfaces (repeatable): pyproject.toml, uv.lock and the "
        "published wheel in dist/ (path dependencies' wheels are not needed). The kit "
        "carries none of its files. kit add installs it by name at exactly this version "
        "from the provider index, refusing any other build, unless a live Provider already "
        "implements its interfaces; a daemon with provider_repository configured refuses "
        "that install (install the repository's build first with provider install)."
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
    output_json: bool,
) -> None:
    """Export this instance's owned definitions as one self-contained kit release.

    Every Provider a carried Procedure pins must be named with --provider or
    --default-provider (the same build this instance installed), and every
    ProviderInterface the kit carries must be exactly what a named package
    registers; kit add installs the providers before the definitions, a default
    one only when no installed Provider implements its interfaces.
    """
    if out.exists():
        raise click.UsageError(f"{out} already exists")

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
        )
        return client.build_kit(instance_id, request)

    bundle = _server_call(build, command_name="cruxible kit build").bundle
    write_kit_directory(bundle, out)
    if output_json:
        _emit_json(bundle.manifest.model_dump(mode="json"))
    else:
        click.echo(
            f"{kit_id} {version}: {len(bundle.artifacts)} artifacts, "
            f"{len(bundle.manifest.providers)} provider package(s), "
            f"{bundle.manifest.content_digest}"
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
    metavar="Procedure:NAME",
    help="Keep this Procedure on its current provider build (repeatable).",
)
@click.option(
    "--allow-downgrade", is_flag=True, help="Install a release older than the installed one."
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
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Install or upgrade KIT as one change set; it always proposes.

    KIT is a kit directory, an OCI image layout, or a registry reference such as
    ``project-state:1.0.0`` or ``ghcr.io/acme/kits/foo@sha256:...``. A definition
    this instance holds differently takes the release's version (the preview
    says what that does: overwrites your edit, re-adds a retired definition,
    takes over one defined outside the kit) unless --keep or --keep-local-edits
    keeps yours. Every Procedure on a shipped provider moves to its exact build;
    --keep-build Procedure:NAME keeps its current build. The preview names each
    re-pin, each carried disabled Line, and every Mandate downgraded to propose.
    Triggers stay unchanged; use line enable to resume and re-grant settle explicitly.
    Replaced definitions' other dependents are carried along. The change
    lands at once when the approval policy requires no approval, otherwise it
    stops at proposed. A kit that bundles provider packages installs the missing
    ones first (admin permission, the transfer install; registry dependencies
    resolve from the daemon's provider index, PyPI unless configured), installs a
    default provider by name from that index unless an installed Provider already
    implements its interfaces (satisfied), and proposes the definitions once they
    land; an install awaiting approval stops at awaiting_providers, and its
    activation proposes the definitions on their own, as you (kit status shows
    the resume). It previews by default; commit with
    ``--commit --at OID``.
    """
    bundle, origin = resolve_kit(kit)

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

    result = _server_call(add, command_name="cruxible kit add")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_kit_change(result)


@kit_group.command("pull")
@click.argument("reference")
@click.option("--out", "out", required=True, type=click.Path(path_type=Path))
@click.option(
    "--layout", is_flag=True, help="Write an OCI image layout instead of a kit directory."
)
@json_option
@handle_errors
def pull_kit(reference: str, out: Path, layout: bool, output_json: bool) -> None:
    """Fetch and verify a kit from a registry into OUT, without installing it."""
    if out.exists():
        raise click.UsageError(f"{out} already exists")
    # The layout keeps the published bytes exactly, so it carries the same digest.
    image, origin = fetch_kit_image(reference)
    bundle = unpack_artifact(KIT_ARTIFACT, image)
    if layout:
        write_layout(image, out, ref=origin)
    else:
        write_kit_directory(bundle, out)
    if output_json:
        _emit_json({"source": origin, "content_digest": bundle.manifest.content_digest})
    else:
        click.echo(f"{origin}: {len(bundle.artifacts)} artifacts")


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
    if not result.kits:
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
    for kit in result.kits:
        available = ""
        if kit.update_check == "checked" and kit.latest_available is not None:
            newer = kit_version_key(kit.latest_available) > kit_version_key(kit.version)
            available = f"; latest {kit.latest_available} available" if newer else "; up to date"
        elif kit.update_check != "checked":
            available = f"; update check {kit.update_check.replace('_', ' ')}"
        click.echo(f"{kit.kit_id} {kit.version} {kit.content_digest}{available}")
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
            if provider.installed_version is not None:
                state += f" (installed: {provider.installed_version})"
            click.echo(
                f"  provider {provider.provider_id} ({provider.package} {provider.version}): "
                f"{state}"
            )
        for path in kit.drifted:
            click.echo(f"  edited locally: {path}")
        for kept in kit.kept:
            click.echo(f"  kept: {kept.identity} ({_KIT_CONSEQUENCE_TEXT[kept.consequence]})")


@kit_group.command("remove")
@click.argument("kit_id")
@change_control_options
@json_option
@handle_errors
def remove_kit(kit_id: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Retire every artifact KIT_ID installed (previews by default).

    Lands at once when the approval policy requires no approval, otherwise it
    stops at proposed.
    """
    request = KitRemoveRequest(kit_id=kit_id, dry_run=dry_run, at=at)
    result = _server_call(
        lambda client, instance_id: client.remove_kit(instance_id, request),
        command_name="cruxible kit remove",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_kit_change(result)


@playbill_group.group("document")
def document_group() -> None:
    """Propose and read governed Documents."""


@document_group.command("propose")
@click.option("--envelope", type=PayloadFile())
@click.option("--template", is_flag=True, help="Print a Document proposal template.")
@click.option("--name", "proposal_name")
@change_control_options
@json_option
@handle_errors
def propose_document(
    envelope: str | None,
    template: bool,
    proposal_name: str | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Use the sanctioned command-local Document proposal path."""

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
        ),
        command_name="cruxible document propose",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_proposal(result, result.lint)


@playbill_group.group("capture")
def capture_group() -> None:
    """Read retained evidence from completed observations and Procedures."""


@capture_group.command("read")
@click.argument("capture_digest")
@click.option("--max-bytes", type=click.IntRange(min=0), default=4 * 1024 * 1024, show_default=True)
@handle_errors
def read_capture(capture_digest: str, max_bytes: int) -> None:
    from cruxible_client.contracts.capture_reads import CaptureReadRequest

    result = _server_call(
        lambda client, instance_id: client.read_capture(
            instance_id, CaptureReadRequest(capture_digest=capture_digest, max_bytes=max_bytes)
        ),
        command_name="cruxible capture read",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("proposal")
def proposal_group() -> None:
    """Inspect, review, approve, and activate candidates."""


@proposal_group.command("list")
@click.option("--status", type=click.Choice(["open", "settled", "incomplete"]), default=None)
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
    click.echo("STATUS  TERMINAL_REASON  PROPOSAL_ID  TARGET_REF  COORDINATE_TIME")
    for entry in result.entries:
        terminal = ",".join(entry.incomplete_reasons) or entry.terminal_reason or "-"
        click.echo(
            f"{entry.status}  {terminal}  {entry.proposal_id}  "
            f"{entry.target_ref or '-'}  {entry.admitted_at or '-'}"
        )
    click.echo(f"Coordinate: {result.coordinate.git_oid}")
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
@click.option("--include-body/--redacted", default=True)
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
@json_option
@handle_errors
def review_proposal(
    proposal_id: str,
    include_body: bool,
    workspace_root: str,
    item_reasons_after: str | None,
    item_reasons_limit: int | None,
    output_json: bool,
) -> None:
    workspace = Path(workspace_root)

    def _review_at_observed_coordinate(
        client: CruxibleClient, instance_id: str
    ) -> contracts.ProposalReview:
        head = client.head(instance_id)
        next_observation = observe_next_workspace(workspace)
        observation: dict[str, object] = {
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
            include_body=include_body,
            workspace_observation=observation,
            item_reasons_after=item_reasons_after,
            item_reasons_limit=item_reasons_limit,
        )

    result = _server_call(
        _review_at_observed_coordinate,
        command_name="cruxible proposal review",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
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
    required=True,
    envvar=PRINCIPAL_KEY_ENV,
    type=click.Path(dir_okay=False),
    help="The signer's private key (also CRUXIBLE_PRINCIPAL_KEY).",
)
@click.option("--yes", is_flag=True, help="Approve after rendering without an interactive prompt.")
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
    private_key_path: str,
    yes: bool,
    hold: bool,
    full: bool,
    output_json: bool,
) -> None:
    """Sign one reviewed candidate; the approval that completes it activates it.

    Activation runs in the same command when no approval is still required and
    your tier may activate (graph_write). `--hold` signs only, for a reviewer
    leaving activation timing to someone else, who runs `proposal activate`.
    Otherwise the result says what remains.
    """

    configured = _root_ctx_obj().get("principal_id")
    if signer_id is None:
        if configured is None:
            raise click.UsageError(
                "proposal approve needs the signing principal; repair: pass --signer-id ID "
                "or set CRUXIBLE_PRINCIPAL_ID"
            )
        signer_id = str(configured)
    resolved_signer: str = signer_id

    def _resolve_and_prepare(
        client: CruxibleClient, instance_id: str
    ) -> tuple[str, contracts.ApprovalChallenge]:
        resolved_id = client.resolve_proposal_selector(instance_id, proposal_id).proposal_id
        return resolved_id, client.prepare_approval(
            instance_id, resolved_id, signer_id=resolved_signer, include_body=True
        )

    resolved_id, challenge = _server_call(
        _resolve_and_prepare,
        command_name="cruxible proposal approve",
    )
    review = ProposalReview.model_validate(challenge.review.model_dump(mode="json"))
    if not output_json:
        render = render_playbill_proposal_review if full else render_playbill_approval_summary
        click.echo(render(review), nl=False)
    if not yes and not click.confirm("Sign this exact candidate?"):
        raise click.Abort()
    principal = PrincipalRecord.model_validate(challenge.signer_principal)
    signer = LocalEd25519ApprovalSigner.open(
        signer_id=resolved_signer,
        private_key_path=Path(private_key_path),
        expected_public_key=principal.public_key,
        forbidden_roots=_custody_forbidden_roots(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge.statement))
    result = _server_call(
        lambda client, instance_id: client.submit_approval(
            instance_id,
            resolved_id,
            attestation=attestation.model_dump(mode="json"),
            hold=hold,
        ),
        command_name="cruxible proposal approve",
    )
    result = _with_git_workspace_note(result)
    if output_json:
        _emit_json(_json_receipt(result))
        return
    if isinstance(result, contracts.ProposalRecompiled):
        click.echo(
            f"Recompiled as {result.proposal_id}; review this new proposal "
            f"(members identical: {result.members_identical})"
        )
        return
    click.echo(f"Approved {result.candidate_digest} as {result.signer_id}")
    for line in _approval_outcome_lines(result):
        click.echo(line)


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
            f"Awaiting {count} more approval{'s' if count != 1 else ''}; the approval "
            "that completes it activates it"
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
        click.echo(
            f"Recompiled as {result.proposal_id}; review this new proposal "
            f"(members identical: {result.members_identical})"
        )
        return
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
        resolution = _local_git_workspace_root()
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
    resolution = _local_git_workspace_root()
    _emit_git_workspace_note(resolution)
    if resolution.workspace_root is None:
        raise click.UsageError("cruxible files add/remove must run inside one Git worktree")
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
    shape = "Document" if result.entry.is_document else "evidence-only"
    click.echo(
        f"{result.status.capitalize()} file {result.name} -> {result.entry.locator} "
        f"({shape}) in {result.catalog_path}"
    )


@files_group.command("add")
@click.argument("name")
@click.argument("path", type=click.Path(dir_okay=False, path_type=Path))
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
    help="Workspace root the catalog belongs to (default: this worktree).",
)
@json_option
@handle_errors
def add_file(
    name: str,
    path: Path,
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
    PATH#ANCHOR` resolves through it). With --document-id, --document-kind,
    --title and --scope the entry also declares the Document it compiles to
    (files compile, files propose); without them it is evidence-only. The
    whole catalog is validated before the file is rewritten: an existing NAME
    refuses unless --replace, and a partial Document or a duplicate Document
    target refuses. Writes only the catalog file; commit it with Git.
    """

    fields: dict[str, object] = {
        key: value
        for key, value in {
            "document_id": document_id,
            "document_kind": document_kind,
            "title": title,
            "media_type": media_type,
            "required_tier": required_tier,
            "public_uri": public_uri,
        }.items()
        if value is not None
    }
    if document_id is not None:
        fields.setdefault("media_type", media_type_for(path))
        fields["governance_scope"] = tuple(
            sorted(set(governance_scope), key=lambda item: item.encode("utf-8"))
        )
        fields = {key: value for key, value in fields.items() if value is not None}
    elif governance_scope:
        fields["governance_scope"] = governance_scope
    result = add_catalog_entry(
        _catalog_edit_root(repository_root), name=name, path=path, fields=fields, replace=replace
    )
    _echo_catalog_edit(result, output_json=output_json)


@files_group.command("remove")
@click.argument("name")
@click.option(
    "--root",
    "repository_root",
    default=None,
    help="Workspace root the catalog belongs to (default: this worktree).",
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
@_file_options
@change_control_options
@json_option
@handle_errors
def propose_files(
    bundle_path: str | None,
    file: str,
    proposal_name: str,
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
    An ordinary Document proposal, reviewed, approved and activated as usual;
    the compilation it came from is recorded with it.
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
    signer = outcome.signer_id or "YOUR_PRINCIPAL_ID"
    steps = []
    if outcome.signer_id is None:
        steps.append(
            f"cruxible proposal approve {outcome.proposal_id} --signer-id {signer} "
            "--key YOUR_PRIVATE_KEY"
        )
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
) -> contracts.ProposalInspection:
    if dry_run:
        return _preview_principal_change(
            principal_id=target_id,
            kind=None,
            key_dir=Path(key_dir).expanduser(),
            proposal_name=proposal_name,
            at=at,
            refuse_existing=False,
        )

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        matches = principal_records(client, instance_id)
        target = next((item for item in matches if item.principal_id == target_id), None)
        if target is None:
            raise click.ClickException(f"Unknown Cruxible principal: {target_id}")
        material = generate_client_principal_key(
            Path(key_dir).expanduser(),
            principal_id=target_id,
            kind=target.kind,
            forbidden_roots=_custody_forbidden_roots(),
        )
        return client.propose_principal_change(
            instance_id,
            principal=material.principal.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        )

    return _server_call(call, command_name="cruxible principal change")


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
    """Propose a self-rotation; activation requires the actor's key signature."""

    result = _principal_successor(
        target_id=principal_id,
        key_dir=key_dir,
        proposal_name=proposal_name,
        dry_run=dry_run,
        at=at,
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_proposal(result, result.lint)


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
    """Use recovery identity for a narrowly governed key replacement."""

    result = _principal_successor(
        target_id=principal_id,
        key_dir=key_dir,
        proposal_name=proposal_name,
        dry_run=dry_run,
        at=at,
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_proposal(result, result.lint)


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
        _echo_proposal(result, result.lint)


@playbill_group.group("claim-type")
def claim_type_group() -> None:
    """Propose and read the governed predicate vocabulary."""


@claim_type_group.command("propose")
@click.option("--input", "input_path", type=PayloadFile())
@click.option(
    "--template",
    is_flag=True,
    help="Print one complete model-generated ClaimTypeInputRecord without contacting the daemon.",
)
@click.option(
    "--builtin",
    type=click.Choice(sorted(BUILTIN_CLAIM_TYPES)),
    help="Adopt the daemon's exact built-in ClaimType (new instances start with it).",
)
@click.option("--name", "proposal_name")
@change_control_options
@json_option
@handle_errors
def propose_claim_type(
    input_path: str | None,
    template: bool,
    builtin: str | None,
    proposal_name: str | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Use the sanctioned typed-input ClaimType proposal path."""

    if sum((input_path is not None, template, builtin is not None)) != 1:
        raise click.UsageError("provide exactly one of --input, --template or --builtin")
    if template:
        _emit_json(claim_type_input_template().model_dump(mode="json"))
        return
    if builtin is not None:
        builtin_result = _server_call(
            lambda client, instance_id: client.propose_builtin_claim_type(
                instance_id,
                builtin=cast(BuiltinClaimTypeName, builtin),
                proposal_name=proposal_name or f"builtin-{builtin.replace('.', '-')}",
                dry_run=dry_run,
                at=at,
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
            + ". Pass a complete ClaimTypeInputRecord whose evidence_admission_policy.rules "
            "match its capture contracts"
        ) from exc
    input_result = _server_call(
        lambda client, instance_id: client.propose_claim_type_input(
            instance_id,
            input=claim_type_input.model_dump(mode="json"),
            proposal_name=proposal_name or claim_type_input.predicate,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible claim-type propose",
    )
    if output_json:
        _emit_json(input_result.model_dump(mode="json"))
        return
    _echo_proposal(input_result.proposal, input_result.lint)


def _echo_proposal(
    inspection: contracts.ProposalInspection, lint: contracts.ClaimTypeProposalLint | None
) -> None:
    """A proposal in a few lines; ``--json`` carries the whole record."""

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
        click.echo(f"lint: {warning.code} at {warning.field_path}")
    if proposal_id:
        click.echo(f"next: cruxible get {proposal_id}")
    echo_preview_next(inspection.status, inspection.accepted_coordinate)


@claim_type_group.command("migrate")
@click.argument("request_file", type=PayloadFile())
@json_option
@handle_errors
def migrate_claim_type(request_file: str, output_json: bool) -> None:
    """Propose one ClaimType successor and every dependent disposition atomically."""

    try:
        request = _CLAIM_TYPE_MIGRATION_ADAPTER.validate_python(_read_mapping(request_file))
    except ValidationError as exc:
        raise click.ClickException(
            f"Invalid ClaimType migration: {validation_summary(exc)}"
        ) from exc
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
            click.echo(f"  {warning.field_path}: {warning.code}")


@claim_type_group.command("upgrade")
@click.option(
    "--claim-type",
    "claim_types",
    multiple=True,
    help="Predicate to upgrade; repeat for more. Default: every live ClaimType before v7.",
)
@click.option(
    "--revision-evidence",
    type=click.Choice(["replace", "accumulate"]),
    default="replace",
    show_default=True,
    help=(
        "What a revision that changes its statement keeps: only the evidence it cites "
        "(replace) or everything its predecessors cited too (accumulate, the meaning "
        "before v7)."
    ),
)
@change_control_options
@json_option
@handle_errors
def upgrade_claim_types(
    claim_types: tuple[str, ...],
    revision_evidence: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose moving live ClaimTypes to v7, one reviewed change set.

    v7 states what a ClaimType's Claims need (evidence_requirement, kept at
    self) and what a statement-changing revision keeps (revision_evidence).
    Every Claim is carried with its backing intact. It previews by default;
    commit the preview with ``--commit --at OID``, then approve as usual.
    """

    request = ClaimTypeUpgradeRequest.model_validate(
        {
            "claim_types": claim_types,
            "revision_evidence": revision_evidence,
            "dry_run": dry_run,
            "at": at,
        }
    )
    result = _server_call(
        lambda client, instance_id: client.upgrade_claim_types(instance_id, request),
        command_name="cruxible claim-type upgrade",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"ClaimType upgrade: {result.status}")
    for item in result.upgraded:
        click.echo(
            f"  upgraded {item.claim_type} ({item.from_format}): revision evidence "
            f"{item.revision_evidence_before} -> {item.revision_evidence_after}"
        )
        for version in item.widened_versions:
            click.echo(f"    now also admits {version}")
    for name in result.unchanged:
        click.echo(f"  already v7 {name}")
    for refusal in result.refused:
        click.echo(f"  left as is {refusal.claim_type}: {refusal.reason}")
    if result.carried_claims:
        click.echo(f"Claims carried: {result.carried_claims}")
    if result.detail:
        click.echo(result.detail)
    if result.proposal_id:
        click.echo(f"Next: cruxible proposal approve {result.proposal_id}")
    echo_preview_next(result.status, result.coordinate)


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
    listed. Pending (unaccepted) predictions and settlement outcomes are read with
    `cruxible get ResolutionContract:NAME` and `cruxible next`.
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
@click.option("--note")
@click.option(
    "--valid-until",
    default=None,
    help=(
        "ISO-8601 end of this attestation. An --unsure hold on stale or uncovered "
        "evidence otherwise lapses after the ClaimType's unsure_hold_for (default 30 days)."
    ),
)
@handle_errors
def attest_claim(
    claim_id: str,
    support: bool,
    contradict: bool,
    unsure: bool,
    note: str | None,
    valid_until: str | None,
) -> None:
    """Sign that this caller examined the current exact Claim.

    --unsure holds the Claim's contested `next` rows until what you examined
    changes, instead of forcing a judgment you are not confident in.
    """

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
    stance = selected[0]

    def call(client: CruxibleClient, instance_id: str):  # type: ignore[no-untyped-def]
        signer = local_attestation_signer_from_environment(
            client,
            instance_id,
            workspace_root=_custody_workspace_root(),
        )
        return append_prepared_claim_attestation(
            client,
            instance_id,
            prepared=PreparedClaimAttestationRequest(
                claim_id=claim_id.removeprefix("Claim:"),
                attestation_basis="examined_existing",
                stance=cast(ClaimStance, stance),
                attested_at=datetime.now(UTC),
                valid_until=(
                    None
                    if valid_until is None
                    else datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                ),
                note=note,
            ),
            signer=signer,
        )

    result = _server_call(call, command_name="cruxible claim attest")
    _emit_json(result.model_dump(mode="json"))


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
    Input kind family: claim | procedure | blueprint | blueprint_instance |
    subject | subject_kind | subject_succession | query_definition |
    approval_policy | procedure_runtime_policy | procedure_mandate |
    acquisition_policy | line | trigger | change_set (tagless).

    \b
    Change-set member kind family: claim | claim_type | claim_type_succession |
    claim_retirement | subject | subject_kind | subject_succession |
    query_definition | procedure_mandate | acquisition_policy | line | trigger |
    procedure | blueprint | blueprint_instance.
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
    `trigger`, `acquisition-policy` and `procedure-mandate` are accepted
    together as members of one change set.
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
    preflight: verdict, certificate and every diagnostic. Nothing is proposed
    until `authoring submit --intent-id ID`; for one-shot work, `authoring
    submit PAYLOAD` compiles and submits in one call.
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
        resolution = _local_git_workspace_root()
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
    """

    result = _server_call(
        lambda client, instance_id: client.preflight_authoring_intent(instance_id, intent_id),
        command_name="cruxible authoring preflight",
    )
    if output_brief:
        codes = [item.code for item in result.frontier.diagnostics]
        _emit_brief(
            outcome=result.verdict + (f" ({', '.join(codes)})" if codes else ""),
            ids={"intent": intent_id},
            next_command=(
                f"cruxible authoring submit --intent-id {intent_id}"
                if result.verdict == "passed"
                else f"cruxible authoring compile - --intent-id {intent_id}  # repaired payload"
            ),
        )
        return
    _emit_json(result.model_dump(mode="json"))


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
@and_activate_option
@brief_option
@handle_errors
def submit_authoring_intent(
    payload: str | None,
    intent_id: str | None,
    dry_run: bool,
    and_activate: bool,
    output_brief: bool,
) -> None:
    """Compile, check and submit PAYLOAD as one proposal, or submit a staged intent.

    \b
    authoring submit PAYLOAD                  compile and submit in one call
    authoring submit PAYLOAD --dry-run        every refusal, nothing saved
    authoring submit --intent-id ID           submit a staged intent
    authoring submit PAYLOAD --intent-id ID   revise a staged intent, then submit

    PAYLOAD is a tagless authoring input file, or - for stdin. A refused
    preflight returns the unsubmitted intent with its diagnostics; a passing one
    returns the proposal and what it still needs (approval or activation).
    Submitting never approves. --and-activate activates a candidate that needs
    nothing further and leaves any other untouched.
    """

    if payload is None and intent_id is None:
        raise click.UsageError("provide PAYLOAD, --intent-id, or both")
    if dry_run and (payload is None or intent_id is not None):
        raise click.UsageError("--dry-run previews a new PAYLOAD and takes no --intent-id")
    if dry_run and and_activate:
        raise click.UsageError("--dry-run saves nothing, so it cannot --and-activate")
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
            return
        _emit_json(preview.model_dump(mode="json"))
        return

    def call(client: CruxibleClient, instance_id: str) -> tuple[Any, Any]:
        if parsed is None:
            assert intent_id is not None
            submitted = client.submit_authoring_intent(instance_id, intent_id)
        else:
            submitted = client.submit_authoring_input(
                instance_id, input=parsed, intent_id=intent_id
            )
        if not and_activate or submitted.status.state != "ready_to_activate":
            return submitted, None
        # Only a candidate that needs nothing further is activated here. Anything
        # else returns the submit result untouched: a partly-activated candidate
        # would be a worse answer than an unactivated one.
        proposal_id = submitted.status.proposal_id
        if proposal_id is None:  # pragma: no cover - ready_to_activate carries one
            return submitted, None
        return submitted, client.activate_proposal(instance_id, proposal_id)

    submitted, activation = _server_call(call, command_name="cruxible authoring submit")
    result: dict[str, Any] = {"submit": submitted.model_dump(mode="json")}
    if activation is not None:
        result["activation"] = activation.model_dump(mode="json")
    elif and_activate:
        result["activation_note"] = _not_activated_note(submitted.status.state)
    if output_brief:
        accepted = activation is not None and activation.accepted_coordinate is not None
        _emit_brief(
            outcome=(
                str(activation.status)
                if activation is not None
                else f"submitted ({submitted.status.state})"
            ),
            ids={
                "intent": str(submitted.intent.get("intent_id")),
                "proposal": submitted.status.proposal_id,
                "coordinate": activation.accepted_coordinate.git_oid if accepted else None,
                "receipt": activation.tag if activation is not None else None,
            },
            reason=_submit_refusal_reason(submitted),
            next_command=_submit_next_command(submitted, activated=activation is not None),
        )
        return
    _emit_json(result if (and_activate or activation is not None) else result["submit"])


def _not_activated_note(state: str) -> str:
    """Say why --and-activate stopped, in the caller's terms."""

    if state == "awaiting_external_approval":
        return (
            "not activated: the candidate needs an external approval. "
            "The eligible signer's `cruxible proposal approve` activates it."
        )
    return f"not activated: the candidate is {state}, not ready_to_activate"


def _submit_refusal_reason(submitted: Any) -> str | None:
    """Render the complete typed preflight refusal on one transcript line."""

    if submitted.status.state != "preflight_refused" or not isinstance(submitted.intent, Mapping):
        return None
    preflight = submitted.intent.get("last_preflight")
    frontier = preflight.get("frontier") if isinstance(preflight, Mapping) else None
    diagnostics = frontier.get("diagnostics") if isinstance(frontier, Mapping) else None
    blocked_checks = frontier.get("blocked_checks") if isinstance(frontier, Mapping) else None
    if not isinstance(diagnostics, list | tuple) and not isinstance(blocked_checks, list | tuple):
        return "preflight refused without a delivered diagnostic"
    rendered: list[str] = []
    for diagnostic in diagnostics if isinstance(diagnostics, list | tuple) else ():
        if not isinstance(diagnostic, Mapping):
            continue
        code = diagnostic.get("code")
        message = diagnostic.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            continue
        one_line_message = " ".join(message.split())
        rendered.append(f"{code}: {one_line_message}")
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
        dependencies = ", ".join(blocked_by)
        one_line_reason = " ".join(reason.split())
        rendered.append(f"blocked {check} by {dependencies}: {one_line_reason}")
    return "; ".join(rendered) or "preflight refused without a delivered diagnostic"


def _submit_next_command(submitted: Any, *, activated: bool) -> str | None:
    if activated:
        return None
    proposal_id = submitted.status.proposal_id
    if submitted.status.state == "ready_to_activate" and proposal_id:
        return f"cruxible proposal activate {proposal_id}"
    if submitted.status.state == "awaiting_external_approval" and proposal_id:
        return f"cruxible proposal approve {proposal_id}"
    if submitted.status.state == "preflight_refused":
        return f"cruxible authoring preflight {submitted.intent['intent_id']}"
    return None


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
    changes: list[Change] = Field(min_length=1)


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
    head = outcome.status.replace("_", " ")
    click.echo(f"{head} (generation {outcome.coordinate.generation}, {outcome.coordinate.git_oid})")
    for change in outcome.changes:
        target = " ".join(part for part in (change.subject, change.field) if part)
        if change.op == "create":
            line = f"  create {change.subject or '(id assigned on submit)'}: {change.resolved}"
        elif change.op == "retire":
            line = f"  retire {target}: {_get_value_text(change.before)}"
        elif change.op == "add":
            line = f"  add {target}: {_get_value_text(change.after)}"
        elif change.before is None:
            line = f"  set {target}: {_get_value_text(change.after)}"
        else:
            line = (
                f"  set {target}: {_get_value_text(change.before)} -> "
                f"{_get_value_text(change.after)}"
            )
        details = [item for item in (change.claim,) if item]
        if change.already_live:
            details.append("already live")
        if change.verdict is not None:
            details.append(f"verdict {change.verdict}")
        if change.capture is not None:
            details.append(f"evidence {change.capture}")
        if change.retired:
            details.append(f"also retires {', '.join(change.retired)}")
        if change.contenders_created:
            details.append(f"contends with {', '.join(change.contenders_created)}")
        click.echo(line + (f"  [{'; '.join(details)}]" if details else ""))
    for subject in outcome.subjects_added:
        click.echo(f"  + subject {subject}")
    if outcome.proposal is not None and outcome.status != "accepted":
        click.echo(f"proposal: {outcome.proposal.proposal_id} ({outcome.proposal.state})")
    if outcome.approval is not None:
        approval = outcome.approval
        who = ", ".join(approval.eligible_approvers) or "you, with a tier that may activate"
        click.echo(f"awaiting: {approval.reason.replace('_', ' ')}; approvers: {who}")
    for warning in outcome.warnings:
        click.echo(f"warning {warning.code}: {warning.message}", err=True)
    if outcome.refusal is not None:
        refusal = outcome.refusal
        where = "" if refusal.change is None else f" (change {refusal.change})"
        click.echo(f"{refusal.code}{where}: {refusal.message}", err=True)
        if refusal.candidates:
            click.echo(f"  nearest: {', '.join(refusal.candidates)}", err=True)
        if refusal.repair:
            click.echo(f"  repair: {refusal.repair}", err=True)
    if outcome.next is not None:
        click.echo(f"next: {outcome.next}")


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
    try:
        observed = observe_evidence(
            FileEvidence(file=evidence_file), workspace=Path(workspace_root)
        )
    except (ValidationError, CoreError) as exc:
        raise click.UsageError(f"--evidence-file {evidence_file}: {exc}") from None
    assert observed is not None
    return observed.model_dump(mode="json")


@playbill_group.command("set")
@click.argument("subject")
@click.argument("field")
@click.argument("value")
@click.option("--because", required=True, help="Why; also the default evidence.")
@click.option(
    "--evidence-file",
    default=None,
    help="PATH#ANCHOR: cite text found once in a catalogued workspace file.",
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
@click.option("--role", default=None, help="Only when the field permits several roles.")
@click.option("--contend", is_flag=True, help="Contest the live value instead of replacing it.")
@click.option("--expect", "expect", multiple=True, help=_EXPECT_HELP)
@click.option(
    "--expect-absent",
    is_flag=True,
    help="Refuse unless the field holds no value now (compare-and-set on an empty field).",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog --evidence-file reads.",
)
@_write_options
@handle_errors
def set_value(
    subject: str,
    field: str,
    value: str,
    because: str,
    evidence_file: str | None,
    capture: str | None,
    evidence_contract: str | None,
    role: str | None,
    contend: bool,
    expect: tuple[str, ...],
    expect_absent: bool,
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
    for such fields, a Subject as kind/id, or the text itself for exact content.
    """

    if expect_absent and expect:
        raise click.UsageError("pass --expect or --expect-absent, not both")
    request = _write_request(
        SetRequest,
        {
            "subject": subject,
            "field": field,
            "value": value,
            "because": because,
            "evidence": _evidence_option_value(
                evidence_file, capture, workspace_root, evidence_contract
            ),
            "role": role,
            "contend": contend,
            "expect": () if expect_absent else _expect_option_value(expect),
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example='cruxible set dev.item/tidy-cli status done --because "Shipped."',
    )
    outcome = _server_call(
        lambda client, instance_id: client.set(instance_id, request=request),
        command_name="cruxible set",
    )
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("add")
@click.argument("subject")
@click.argument("field")
@click.argument("value")
@click.option("--because", required=True, help="Why; also the default evidence.")
@click.option(
    "--evidence-file",
    default=None,
    help="PATH#ANCHOR: cite text found once in a catalogued workspace file.",
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
@click.option("--role", default=None, help="Only when the field permits several roles.")
@click.option(
    "--expect-absent",
    is_flag=True,
    help="Refuse when the value is already there, instead of answering it as done.",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose file catalog --evidence-file reads.",
)
@_write_options
@handle_errors
def add_value(
    subject: str,
    field: str,
    value: str,
    because: str,
    evidence_file: str | None,
    capture: str | None,
    evidence_contract: str | None,
    role: str | None,
    expect_absent: bool,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Add VALUE to many-valued FIELD of SUBJECT (kind/id), beside the values there.

    A value already there is answered as done (--expect-absent refuses instead).
    A Subject of a known kind that does not exist yet is added. VALUE is text, as
    for set: a Subject as kind/id for a Subject-valued field.
    """

    request = _write_request(
        WriteRequest,
        {
            "changes": [
                {
                    "op": "add",
                    "subject": subject,
                    "field": field,
                    "value": value,
                    "evidence": _evidence_option_value(
                        evidence_file, capture, workspace_root, evidence_contract
                    ),
                    "role": role,
                    "expect_absent": expect_absent,
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
    outcome = _server_call(
        lambda client, instance_id: client.write(instance_id, request=request),
        command_name="cruxible add",
    )
    _finish_write(outcome, output_json=output_json)


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
@_write_options
@handle_errors
def retire(
    target: str,
    field: str | None,
    value: str | None,
    because: str,
    reason: str,
    expect: tuple[str, ...],
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Retire one live Claim: a Claim ID, or a Subject (kind/id), its FIELD and VALUE.

    VALUE names which value to retire; it may be left out when FIELD holds one.
    Claims that depend on it retire with it, in the same change set.
    """

    if value is not None and field is None:
        raise click.UsageError("VALUE needs FIELD: cruxible retire SUBJECT FIELD VALUE")
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
@_write_options
@handle_errors
def write_changes(
    file: str | None,
    because: str | None,
    schema: bool,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Apply FILE's create, set, add and retire changes as one change set.

    FILE (YAML or JSON; - reads stdin) holds {"because": ..., "changes": [...]},
    or a bare list of changes with --because. Each change is {"op": "set" |
    "add", "subject", "field", "value"}, {"op": "retire", "target"} or {"op":
    "create", "label", "subject_kind", "key"}; other changes name a created
    Subject {"created": label}, as subject or value. A top-level "subject" is
    the Subject of every change that names none. --schema prints the schema.
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
    request = _write_request(
        WriteRequest,
        {
            "changes": observe_changes(parsed.changes, workspace=Path(workspace_root)),
            "subject": parsed.subject,
            "because": rationale,
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example="cruxible write changes.yaml",
    )
    outcome = _server_call(
        lambda client, instance_id: client.write(instance_id, request=request),
        command_name="cruxible write",
    )
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("get")
@click.argument("ref")
@click.option(
    "--detail",
    type=click.Choice(["summary", "evidence", "why", "history", "proof", "body"]),
    default="summary",
    show_default=True,
    help="How deep to read the one thing REF names.",
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
    help="Revisions per --detail history page, newest first (default 20).",
)
@click.option("--cursor", default=None, help="Continue --detail history from its next_cursor.")
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
    or Provider:NAME/BUILD, an artifact
    path, a proposal id or prefix, or an operational reference: Line:<name> (or
    the Line identity digest next names), CAP-<12+ hex> or Capture:<digest>,
    ResolutionContract:<name>, Mandate:<name>.
    """

    from cruxible_client.contracts.get_reads import ByteRange, GetRequest

    try:
        request = GetRequest.model_validate(
            {
                "ref": ref,
                "detail": detail,
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
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _emit_get_text(result)
    if result.next_cursor is not None:
        narrowed = "" if field_name is None else f" --field {shlex.quote(field_name)}"
        click.echo(
            f"next: cruxible get {shlex.quote(ref)} --detail history{narrowed} "
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


def _get_value_text(
    value: object, *, width: int = GET_CLI_VALUE_WIDTH, evidence_hint: bool = True
) -> str:
    return get_value_display(value, width=width, evidence_hint=evidence_hint).text


def _public_name(name: str) -> str:
    """A stored ``playbill``-prefixed identity, mapped to its Cruxible display name.

    Stored and served names keep their ``playbill-*-vN`` spelling: digests and
    signatures cover them (AGENTS.md "Frozen format tags"). Public text output
    says Cruxible instead -- at render time only, never by renaming the stored
    value.
    """
    qualifier = "CaptureContract:"
    if name.startswith(qualifier):
        return qualifier + _public_name(name[len(qualifier) :])
    if name.startswith("playbill."):
        return "cruxible." + name[len("playbill.") :]
    return name


_WHY_SUMMARY_MAX_ITEMS = 5


def _why_scalar_text(value: object) -> str:
    text = str(value)
    return _public_name(text) if "playbill." in text else text


def _why_summary_lines(value: Any, *, key: str | None = None, depth: int = 0) -> list[str]:
    """``get --detail why`` as a readable, bounded summary; ``--json`` keeps the whole payload.

    The served explanation is a deeply nested typed envelope (law evidence,
    verdict detail, admission accounts, source handles, ...); printed as raw
    JSON it runs to hundreds of lines. This walks it generically -- every key
    still appears, nothing is renamed -- but bounds each list to a handful of
    items so the common case is a short, readable summary instead of a dump.
    """

    indent = "  " * depth
    if isinstance(value, Mapping):
        items = [(k, v) for k, v in value.items() if k != "tag" and v not in (None, [], {}, ())]
        if not items:
            return []
        lines = [] if key is None else [f"{indent}{key}:"]
        for item_key, item_value in items:
            lines.extend(_why_summary_lines(item_value, key=item_key, depth=depth + 1))
        return lines
    if isinstance(value, list | tuple):
        if not value:
            return []
        shown = value[:_WHY_SUMMARY_MAX_ITEMS]
        overflow = len(value) - len(shown)
        if all(isinstance(item, Mapping) for item in shown):
            header = [] if key is None else [f"{indent}{key} ({len(value)}):"]
            lines = list(header)
            flat = all(
                not isinstance(v, Mapping | list | tuple) for item in shown for v in item.values()
            )
            for item in shown:
                if flat:
                    # Every field is a scalar: one joined line per item, as
                    # get's other card rendering already does.
                    lines.append(
                        f"{indent}  " + "  ".join(_why_scalar_text(v) for v in item.values())
                    )
                else:
                    lines.extend(_why_summary_lines(item, depth=depth + 1))
            if overflow:
                lines.append(f"{indent}  ... and {overflow} more")
            return lines
        rendered = ", ".join(_why_scalar_text(item) for item in shown)
        if overflow:
            rendered += f", ... and {overflow} more"
        return [f"{indent}{key}: {rendered}" if key is not None else f"{indent}{rendered}"]
    text = _why_scalar_text(value)
    return [f"{indent}{key}: {text}" if key is not None else f"{indent}{text}"]


def _emit_get_text(result: Any) -> None:
    """Values first, per kind; ``--json`` carries the whole structured result."""

    if result.card is not None:
        card = result.card.model_dump(mode="json")
        if isinstance(card.get("evidence"), list):
            card["evidence"] = [_public_name(str(name)) for name in card["evidence"]]
        for rule in card.get("evidence_rules") or ():
            if isinstance(rule, dict) and isinstance(rule.get("contracts"), list):
                rule["contracts"] = [_public_name(str(name)) for name in rule["contracts"]]
        nexts = card.pop("next", [])
        if result.kind == "claim":
            click.echo(f"{card['subject']}  {card['predicate']} = {_get_value_text(card['value'])}")
            click.echo(
                f"{card['claim']}  verdict={card['verdict']} status={card['status']} "
                f"revision={card['revision']}"
                + (f" accepted={card['accepted']}" if card.get("accepted") else "")
            )
            for contender in card.get("contenders", []):
                click.echo(
                    f"  contender {contender['claim']} = {_get_value_text(contender['value'])} "
                    f"[{contender['verdict']}]"
                )
        elif result.kind == "subject":
            click.echo(
                f"{card['subject']}  ({card['lifecycle']}, {card['incoming_count']} incoming)"
            )
            rows = card["claims"]
            width = max((len(row["predicate"]) for row in rows), default=0)
            for row in rows:
                flags = f"  [{', '.join(row['flags'])}]" if row["flags"] else ""
                click.echo(
                    f"  {row['predicate'].ljust(width)}  {_get_value_text(row['value'])}{flags}"
                )
        else:
            for key, value in card.items():
                if value in (None, [], {}):
                    continue
                if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                    click.echo(f"{key}:")
                    for item in value:
                        click.echo("  " + "  ".join(str(part) for part in item.values()))
                    continue
                if isinstance(value, dict):
                    value = "  ".join(
                        f"{name}={', '.join(map(str, part)) if isinstance(part, list) else part}"
                        for name, part in value.items()
                    )
                if isinstance(value, list):
                    # Names are never cut: a truncated name is not a usable reference.
                    click.echo(f"{key}: {printable(', '.join(map(str, value)))}")
                    continue
                click.echo(f"{key}: {_get_value_text(value, width=200, evidence_hint=False)}")
        if card.get("flags"):
            click.echo(f"flags: {', '.join(card['flags'])}")
        for step in nexts:
            click.echo(f"next: {step}")
        return
    if result.evidence is not None:
        # Evidence carries the whole value; a summary card cuts a long one.
        whole = result.evidence.model_dump(mode="json")["value"]
        if isinstance(whole, str):
            click.echo(f"value: {printable(whole)}")
        else:
            click.echo(f"value: {printable(json.dumps(whole, ensure_ascii=False))}")
        if result.evidence.content_digest is not None:
            click.echo(f"content_digest: {result.evidence.content_digest}")
        for capture in result.evidence.captures:
            click.echo(
                f"capture {capture.capture}  {_public_name(capture.contract)} "
                f"v{capture.version}  source={capture.source}  "
                f"observed={capture.observed_at.isoformat()}  "
                f"{capture.role}{' admitted' if capture.admitted else ' not admitted'}"
            )
        for attestation in result.evidence.attestations:
            click.echo(
                f"attestation {attestation.stance} by {attestation.principal} "
                f"at {attestation.at.isoformat()}{'' if attestation.current else ' (not current)'}"
            )
        if result.evidence.rationale:
            click.echo(f"rationale: {printable(result.evidence.rationale)}")
        return
    if result.history is not None:
        _echo_history(result.history)
        return
    if result.body is not None:
        body = result.body
        click.echo(body.text if body.text is not None else body.content_base64 or "", nl=False)
        if body.range is not None and body.range.end < body.size:
            following = min(body.size, 2 * body.range.end - body.range.start)
            click.echo(
                f"\n(bytes {body.range.start}:{body.range.end} of {body.size}; next: "
                f"--range {body.range.end}:{following})",
                err=True,
            )
        return
    if result.why is not None:
        for line in _why_summary_lines(result.why) or ["(nothing to explain)"]:
            click.echo(line)
        if getattr(result, "reason", None) is not None:
            _echo_proposed_reason(result.reason)
        return
    _emit_json(result.proof)


def _history_value(value: Any) -> str:
    return _get_value_text(value, width=GET_CLI_HISTORY_VALUE_WIDTH)


def _reason_text(reason: Any) -> str:
    """One revision's reason on one escaped line: its item's words, else its change set's."""

    if reason.kind == "procedure_item":
        text = f"{printable(reason.reason)}  (Procedure {reason.procedure}, run {reason.run})"
    else:
        text = printable(reason.text)
    return text + ("  (truncated)" if reason.truncated else "")


def _echo_history(history: Any) -> None:
    """Revisions newest first; a Subject's field events grouped by field, each its timeline."""

    for revision in history.revisions:
        value = "" if revision.value is None else f"  = {_history_value(revision.value)}"
        change = "" if revision.change is None else f"  {revision.change}"
        if revision.retirement is not None:
            change += f" ({revision.retirement})"
        click.echo(
            f"rev {revision.revision}  seq {revision.sequence} at {revision.git_oid}  "
            f"{revision.accepted}  by {revision.actor or '-'}{change}{value}"
        )
        if revision.reason is not None:
            click.echo(f"    reason: {_reason_text(revision.reason)}")
        for step in revision.next:
            click.echo(f"next: {step}")
    for review in getattr(history, "measurement_reviews", ()):
        click.echo(f"{review.measurement_name}: standing={review.standing_resolution_id or 'none'}")
        for judgment in review.history:
            click.echo(
                f"  {judgment.resolution_id}: {judgment.verdict} by {judgment.reviewer_id} "
                f"at {judgment.recorded_at.isoformat()}  reason: {judgment.reason}"
            )
        if review.history_truncated:
            click.echo(f"  showing {len(review.history)} of {review.history_total} judgments")
    if history.kind != "subject":
        return
    by_field: dict[str, list[Any]] = {}
    for event in history.events:
        by_field.setdefault(event.field, []).append(event)
    for name, events in by_field.items():
        click.echo(printable(name))
        for event in events:
            if event.change == "changed":
                what = f"changed  {_history_value(event.before)} -> {_history_value(event.value)}"
            elif event.change == "retired":
                ended = "" if event.retirement is None else f" ({event.retirement})"
                what = f"retired{ended}  {_history_value(event.value)}"
            else:
                what = f"{event.change}  {_history_value(event.value)}"
                if getattr(event, "contended", False):
                    what += "  (contends with a live value)"
            click.echo(
                f"  seq {event.sequence}  {event.git_oid}  {event.accepted}  "
                f"by {event.actor or '-'}  {what}  [{event.claim}]"
            )
            if event.reason is not None:
                click.echo(f"      reason: {_reason_text(event.reason)}")
            for step in event.next:
                click.echo(f"      next: {step}")


def _echo_proposed_reason(reason: Any) -> None:
    """The Procedure item's own reason for the accepted revision, and who proposed it.

    Authored text goes through ``printable``: a reason cannot forge a line of
    daemon output, and a multi-line reason stays one escaped line.
    """

    click.echo(
        f"reason: {printable(reason.reason)}" + ("  (truncated)" if reason.truncated else "")
    )
    click.echo(
        f"proposed by {printable(reason.procedure)} run {printable(reason.run)} "
        f"terminal {printable(reason.terminal)} in proposal {printable(reason.proposal)}"
    )


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

    The client reads the backings and computes the stamp, rewrites the opening
    marker and then declares the block, creating its registration the first
    time. The authored body is kept as written, unless ``--render`` writes it
    from the block's one query backing (a table, or a list when the query
    projects no fields).
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
    """Strip retired blocks' markers from these pages, keeping each body as prose.

    Only blocks whose every backing is retired, or that belong to another
    instance, are detached; live blocks are refused. Edits pages only, never
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
            click.echo(f"  repair: {render_served_repair(item.repair)}")


def _echo_list_continuation(next_cursor: str | None) -> None:
    if next_cursor is not None:
        click.echo(f"Truncated. Next: --cursor {next_cursor}")


@playbill_group.group("compiler")
def compiler_group() -> None:
    """Explicit, reviewed changes to the instance's accepted compiler."""


@compiler_group.command("upgrade")
@click.option(
    "--to", "target_digest", required=True, help="Exact installed target compiler digest."
)
@click.option("--name", "proposal_name", required=True)
@change_control_options
@json_option
@handle_errors
def propose_compiler_upgrade(
    target_digest: str,
    proposal_name: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Create an upgrade proposal at the selected accepted head; does not activate it."""
    from cruxible_client.contracts.types import CompilerCoordinate

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        head = client.head(instance_id)
        return client.propose_compiler_upgrade(
            instance_id,
            target=CompilerCoordinate(rule_digest=target_digest),
            base=contracts.AcceptedCoordinate.model_validate(
                head.coordinate.model_dump(mode="json")
            ),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        )

    result = _server_call(call, command_name="cruxible compiler upgrade")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_proposal(result, result.lint)


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
@click.option("--order-by", "order_fields", multiple=True, help="Order: f or -f (repeatable).")
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
    type=click.Choice(["live", "overturned", "refused", "retired"]),
    help="Which Claims cells show (repeatable): live (default), overturned, refused, retired.",
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
    the next command when the page is truncated. KIND is a Subject kind,
    ClaimType / Procedure for definitions, or Trigger / Line (Triggers by name,
    schedule, target_kind, target and lifecycle; Lines by enabled; --select
    adds a Trigger's cron, cadence, capture_contract or version); --name runs
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
        from cruxible_client.contracts.query.definitions import QueryDefinitionSpec

        try:
            spec = QueryDefinitionSpec.model_validate(_read_mapping(spec_path))
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


#: Repair arguments a CLI leaf takes as its positional operand.
_POSITIONAL_REPAIR_ARGUMENTS = frozenset(
    {"line", "name", "example", "claim_id", "proposal_id", "prediction_id", "run_id"}
)


def _cli_repair(repair: Any) -> str:
    """Render a served repair as the CLI command that performs it."""

    operation = normalize_code(repair.operation) if isinstance(repair, RepairOperation) else ""
    if not isinstance(repair, RepairOperation) or not operation.startswith("cruxible."):
        return render_served_repair(repair)
    parts = ["cruxible", *operation.removeprefix("cruxible.").split(".")]
    for key, value in repair.arguments.items():
        if key in _POSITIONAL_REPAIR_ARGUMENTS:
            parts.append(shlex.quote(str(value)))
        elif value is True:
            parts.append("--" + key.replace("_", "-"))
        elif value not in (None, False):
            parts.extend(["--" + key.replace("_", "-"), shlex.quote(str(value))])
    return " ".join(parts)


def _echo_run_outcome(result: contracts.ProcedureRunState, label: str) -> None:
    """Lead with the answer: the result on success, the code and repair on refusal."""

    click.echo(f"{label}: {result.status}")
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
    elif isinstance(terminal, ProcedureHaltTerminal) and terminal.reason:
        click.echo(f"Halted at {terminal.node_id}: {terminal.reason}")
    click.echo(f"Next: {result.next_operation['kind']}")


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
    _echo_terminal_egress(result)


def _echo_terminal_egress(result: contracts.ProcedureRunState) -> None:
    """Print what each terminal of the run did, and the handles a manager needs.

    A delivered `propose_change_set` prints the proposal id and the exact
    candidate digest, which are the two arguments the existing proposal
    verbs take; a settled `settle_change_set` prints the generation it
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
                f"; outside the mandate's scope: {', '.join(egress.outside_scope_claim_types)}"
                if egress.outside_scope_claim_types
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
            for child in egress.children:
                if child.path is not None:
                    count = len(child.prior_refusals)
                    earlier = (
                        f" ({count} earlier refusal{'s' if count != 1 else ''} of this "
                        "statement on this evidence; see proposal review)"
                        if count
                        else ""
                    )
                    click.echo(f"  {child.path}{earlier}")
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
@json_option
@handle_errors
def run_procedure(
    name: str,
    input_file: str,
    evaluation_time: str | None,
    at_file: str | None,
    output_json: bool,
    contract_file: str | None,
    event_file: str | None,
) -> None:
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
        ),
        command_name="cruxible procedure run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_run_outcome(result, result.run_id or name)
    if result.receipt_digest is not None:
        click.echo(f"Receipt: {result.receipt_digest}")
    _echo_source_observations(result)


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
    the proposal or Subject that records your judgment. An item that recurs is
    unread again, its prior disposition still shown.
    """


def _inbox_row(envelope: Any) -> str:
    seen = f"x{envelope.occurrences}" if envelope.occurrences > 1 else "-"
    prior = (
        f" (was {envelope.disposition.disposition})"
        if envelope.status == "unread" and envelope.disposition is not None
        else ""
    )
    return (
        f"{envelope.status}{prior}  {envelope.item_id[:16]}  {envelope.line}  {seen}  "
        f"{format_datetime(envelope.last_sighting.posted_at)}"
    )


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
    """List inbox items, most recently posted first."""

    result = _server_call(
        lambda client, instance_id: client.list_inbox(
            instance_id, line=line, status=cast(Any, status), limit=limit, cursor=cursor
        ),
        command_name="cruxible inbox list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if not result.entries:
        click.echo("No inbox items.")
        return
    click.echo("STATUS  ITEM  LINE  SEEN  LAST_POSTED")
    for entry in result.entries:
        click.echo(_inbox_row(entry.item))
        preview = shown_plain(entry.payload_preview)
        click.echo(f"  {preview if isinstance(preview, str) else json.dumps(preview)}")
    _echo_list_continuation(result.next_cursor)


def _echo_inbox_disposition(result: Any) -> None:
    click.echo(_inbox_row(result.item) + ("" if result.recorded else "  (unchanged)"))


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
    envelope = result.item
    click.echo(_inbox_row(envelope))
    click.echo(f"Item: {envelope.item_id}")
    click.echo(f"Procedure: {envelope.procedure} node {envelope.node_id}")
    click.echo(f"Seen {envelope.occurrences} time(s)")
    for label, sighting in (
        ("First", envelope.first_sighting),
        *(("Recent", item) for item in envelope.recent_sightings),
    ):
        click.echo(
            f"{label}: {sighting.run_id} at {format_datetime(sighting.posted_at)} captures "
            f"{', '.join(sighting.source_capture_digests) or '-'}"
        )
    if envelope.disposition_count > len(result.recent_dispositions):
        click.echo(
            f"Dispositions: latest {len(result.recent_dispositions)} of "
            f"{envelope.disposition_count}"
        )
    for disposition in result.recent_dispositions:
        detail = getattr(disposition, "reason", None) or (
            disposition.target.model_dump(mode="json") if hasattr(disposition, "target") else ""
        )
        click.echo(
            f"Disposition: {disposition.disposition} by {disposition.actor_id} at "
            f"{format_datetime(disposition.decided_at)} {detail}".rstrip()
        )
    click.echo(json.dumps(result.payload, indent=2, sort_keys=True))


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
    """Enable, run and recover accepted Lines.

    enable, disable and run are the normal flow; evaluate and dispatch recover
    what automation missed, as `cruxible next` names it. `cruxible get
    Line:NAME` reads a Line's enablement, Triggers, pending work and runs.
    """


def _echo_line_enablement(result: Any) -> None:
    state = "enabled" if result.state == "enabled" else f"stopped ({result.stop_reason})"
    unchanged = {
        "already_enabled": "already enabled",
        "already_disabled": "already disabled",
        "would_enable": "preview: would enable",
        "would_reenable": "preview: would re-enable",
        "would_disable": "preview: would disable",
    }
    note = unchanged.get(result.outcome or "")
    click.echo(f"{result.line}: {state}" + (f" ({note}; nothing changed)" if note else ""))
    click.echo(f"Enabled by: {result.enabled_by.label} at {result.enabled_at.isoformat()}")
    click.echo(f"Matched through: {result.evaluated_until.isoformat()}")
    click.echo(
        f"Pending: {result.pending_automatic} automatic, "
        f"{result.pending_explicit} awaiting explicit dispatch"
    )
    if result.detail:
        click.echo(result.detail)
    echo_preview_next(result.outcome or "", result.coordinate)


@line_group.command("enable")
@click.argument("line")
@change_control_options
@json_option
@handle_errors
def enable_line(line: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Enable LINE: its Triggers admit what they match from now on, under your credential.

    A Trigger aimed at a Line does nothing until the Line is enabled. The
    enablement pins the current Line version and the exact Trigger versions
    aimed at it; any change to them stops it until you enable it again. It
    never catches up: `cruxible next` names restart gaps and pending work for
    `line evaluate` and `line dispatch`. Enabling needs governed write, even for
    an observe-only Line; a Line that proposes or settles also needs a covering
    ProcedureMandate. Read it with `cruxible get Line:LINE`.
    """

    result = _server_call(
        lambda client, instance_id: client.enable_line(instance_id, line, dry_run=dry_run, at=at),
        command_name="cruxible line enable",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_line_enablement(result)


@line_group.command("disable")
@click.argument("line")
@change_control_options
@json_option
@handle_errors
def disable_line(line: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Stop LINE admitting work on its own; runs already admitted keep going.

    Disabling a Line whose enablement already stopped changes nothing. A
    retired Line can be disabled too.
    """

    result = _server_call(
        lambda client, instance_id: client.disable_line(instance_id, line, dry_run=dry_run, at=at),
        command_name="cruxible line disable",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_line_enablement(result)


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
@json_option
@handle_errors
def run_line(
    line: str,
    event_file: str | None,
    repeat: bool,
    contract_file: str | None,
    occurrence_id: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Run LINE once now: one manual occurrence under the Line's inputs, budgets and authority.

    It never selects, consumes or waits on a Trigger, and runs whether or not
    the Line is enabled; the run is recorded on the Line's history.
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
        ),
        command_name="cruxible line run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_run_outcome(result, result.run_id or line)
    _echo_source_observations(result)


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
    _echo_next_status(result.status)
    if not result.items:
        click.echo(
            "No changes since the requested queue digest."
            if result.delta_since is not None
            else "No repair work in the observed domains."
        )
    removed_ids = frozenset(result.removed_item_ids)
    for item in result.items:
        change = (
            "removed  "
            if result.delta_since is not None and item.item_id in removed_ids
            else "added  "
            if result.delta_since is not None
            else ""
        )
        row = f"{change}{item.severity}  {item.reason}  {item.subject_identity}"
        if item.repair is None:
            needs = _next_requirement_hint(item.repair_requires)
            click.echo(row + ("" if output_brief else f"  repair withheld: {needs}"))
            if not output_brief:
                for finding in item.findings:
                    click.echo(
                        f"  also: {finding.severity}  {finding.reason}  {finding.subject_identity}"
                    )
            continue
        if output_brief:
            click.echo(row + (f"  next={item.repair.command}" if item.repair.command else ""))
            continue
        click.echo(f"{row}  next={item.repair.operation}")
        click.echo(f"  repair: {_next_repair_hint(item.repair)}")
        for finding in item.findings:
            click.echo(f"  also: {finding.severity}  {finding.reason}  {finding.subject_identity}")
    if result.unobserved_domains and not output_brief:
        click.echo("Unobserved: " + ", ".join(result.unobserved_domains))
    if result.next_cursor is not None:
        click.echo(
            f"Showing {len(result.items)} of {result.total_items} rows. "
            f"Next: --cursor {result.next_cursor}"
        )


def _next_requirement_hint(requires: contracts.NextRepairRequirement | None) -> str:
    """What running a withheld repair needs, in one phrase."""

    if requires is None:
        return "this caller cannot run it"
    needs = []
    if "tier" in requires.because:
        needs.append(f"the {requires.tier} tier")
    if "profile" in requires.because:
        needs.append(f"the {requires.profile} MCP tool profile")
    refusal = requires.authoring_refusal
    if "authoring" in requires.because and refusal is not None:
        # The identity repair comes first: no tier helps a caller that cannot author.
        needs.insert(0, f"a caller that can author ({refusal.code}: {refusal.detail})")
    return f"{requires.tool} needs " + " and ".join(needs)


def _next_repair_hint(repair: contracts.NextRepair) -> str:
    """The runnable command, or else the operation and the change it must make."""

    if repair.command is not None:
        return repair.command
    operation = "hand edit" if repair.operation == "hand_edit" else repair.operation
    return f"{operation} {repair.target}: {repair.required_change}"


#: Facet states the status header shows; healthy and unobserved facets stay quiet.
_NEXT_STATUS_ATTENTION = {
    "instance": {"decommissioned"},
    "floor": {"missing", "stale"},
    "ledger_mirror": {"behind", "never_published"},
    "provider_lane": {"unavailable"},
    "procedure_catalog": {"missing"},
    "compiler": {"upgrade_available"},
    "line_dispatch": {"due"},
    "consumers": {"lagging"},
    "triggers": {"unscheduled"},
}


def _echo_next_status(status: contracts.NextStatus) -> None:
    """Print the environment facets that need attention above the work rows."""

    if status.blocking:
        click.echo("BLOCKING: this instance refuses every write.")
    for facet, states in _NEXT_STATUS_ATTENTION.items():
        health: contracts.NextHealth = getattr(status, facet)
        if health.state not in states:
            continue
        repair = health.repair
        hint = None if repair is None else repair.command or repair.required_change
        if health.repair_hidden:
            hint = f"(repair withheld: {_next_requirement_hint(health.repair_requires)})"
        label = facet.replace("_", " ")
        click.echo(f"Status: {label} {health.state}" + (f"  next={hint}" if hint else ""))
    arms = (
        status.consumers.detail.get("line_enablements")
        if isinstance(status.consumers.detail, dict)
        else None
    )
    if isinstance(arms, dict) and (arms.get("stalled") or arms.get("stopped")):
        click.echo(
            f"Status: line enablements stalled={arms.get('stalled', 0)} "
            f"stopped={arms.get('stopped', 0)}"
            "  next=cruxible orient --section lines"
        )


@playbill_group.group("curation")
def curation_group() -> None:
    """Review the ontology-maintenance patterns curation detection records (beta).

    Curation is beta: detection runs only when a Trigger aimed at the
    curation.detect action (curation-detect) is authored; none is seeded.
    """


def _curation_ruling_options(function: Callable[..., Any]) -> Callable[..., Any]:
    function = click.option(
        "--attribution-ref",
        "attribution_refs",
        multiple=True,
        help="A reference recorded with the ruling, such as a ticket or Claim ID (repeatable).",
    )(function)
    function = click.option("--reason", required=True, help="Why, recorded with the ruling.")(
        function
    )
    return click.option(
        "--expected-latest-event-digest",
        required=True,
        help="The item's latest_event_digest from curation list; refuses if it moved.",
    )(function)


def _echo_curation_action(result: contracts.CurationActionResult, done: str) -> None:
    if result.status == "would_record":
        click.echo(f"Would record the ruling on {result.item['item_id']}; nothing was appended")
        echo_preview_next(result.status, result.coordinate)
        return
    click.echo(f"Curation item {result.item['item_id']}: {done}")


def _curation_item_line(item: Mapping[str, Any]) -> str:
    kind = str(item.get("pattern_kind", "")).removeprefix("playbill.curation.")
    subject = item.get("subject") or {}
    summary = f"{subject.get('kind', '')}:{subject.get('name', '')}"
    status = "" if item.get("status") == "open" else f" [{item.get('status')}]"
    return (
        f"{item.get('item_id')}  {kind}  {summary}{status}  "
        f"latest={item.get('latest_event_digest')}"
    )


@curation_group.command("list")
@click.option(
    "--access-profile",
    "access_profile_path",
    default=None,
    type=PayloadFile(),
    help="CoverageAccessProfile JSON/YAML (- for stdin); defaults to public and instance access.",
)
@click.option(
    "--limit",
    default=contracts.CURATION_LIST_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, contracts.CURATION_LIST_MAX_LIMIT),
    help="Queue items per page.",
)
@click.option("--cursor", default=None, help="Continue a previous page of the same queue.")
@json_option
@handle_errors
def curation_list(
    access_profile_path: str | None,
    limit: int,
    cursor: str | None,
    output_json: bool,
) -> None:
    """List the open curation items detection recorded; a pure read.

    Detection runs on its own on every accepted generation (the curation.detect
    Trigger). Each line is the item ID, its pattern kind, its subject and the
    latest_event_digest the rulings (overrule, accept-fixed, suppress) need.
    """

    profile = (
        CoverageAccessProfile(
            profile_id="cli-curation",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_model(access_profile_path, CoverageAccessProfile).model_dump(mode="json")
    )
    result = _server_call(
        lambda client, instance_id: client.list_curation(
            instance_id, access_profile=profile, limit=limit, cursor=cursor
        ),
        command_name="cruxible curation list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    detection = result.detection
    ran = (
        "never run"
        if detection.detected_through_generation is None
        else f"through generation {detection.detected_through_generation} "
        f"at {detection.detected_at}"
    )
    click.echo(
        f"Curation queue at generation {result.generation}: {len(result.items)} item(s); "
        f"detection {detection.state} ({ran}), trigger {detection.trigger}."
    )
    for item in result.items:
        click.echo(f"  {_curation_item_line(item)}")
    for inactive in result.inactive_detectors:
        click.echo(f"Inactive: {inactive.pattern_kind} ({inactive.reason})")
    _echo_list_continuation(result.next_cursor)


@curation_group.command("duplicates")
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
def curation_duplicates(kind: str, limit: int, cursor: str | None, output_json: bool) -> None:
    """List possible duplicates of one kind; a pure, bounded read.

    Subjects whose ids or aliases, and values of one predicate, that meet under
    a broad comparison (casefold, no punctuation, the alias graph) but differ
    under the declared rules; and Subjects of a hint kind holding one key.
    Resolve a Subject or key group with an alias plus a retirement or with
    semantic.distinct_from; a value group with a governed canonical_form
    succession.
    """

    result = _server_call(
        lambda client, instance_id: client.curation_duplicates(
            instance_id, kind=kind, limit=limit, cursor=cursor
        ),
        command_name="cruxible curation duplicates",
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


@curation_group.command("overrule")
@click.argument("item_id")
@_curation_ruling_options
@change_control_options
@json_option
@handle_errors
def curation_overrule(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: tuple[str, ...],
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Close an item as not applying here, permanently: its pattern is never raised again."""

    result = _server_call(
        lambda client, instance_id: client.overrule_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            attribution_refs=attribution_refs,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible curation overrule",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_curation_action(result, str(result.item["status"]))


@curation_group.command("accept-fixed")
@click.argument("item_id")
@_curation_ruling_options
@click.option("--proposal-id", default=None, help="The accepted proposal that fixed the item.")
@click.option(
    "--changeset-digest",
    default=None,
    help="Pins --proposal-id to its exact accepted ChangeSet (optional).",
)
@click.option(
    "--generation",
    type=click.IntRange(min=1),
    default=None,
    help="The accepted generation that fixed the item, instead of --proposal-id.",
)
@change_control_options
@json_option
@handle_errors
def curation_accept_fixed(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: tuple[str, ...],
    proposal_id: str | None,
    changeset_digest: str | None,
    generation: int | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Link an item to the accepted change that fixed it, closing it.

    Name the change by --proposal-id or --generation; the daemon resolves the
    rest. The change must postdate the item and touch its subject or evidence.
    """

    if (proposal_id is None) == (generation is None):
        raise click.UsageError("name the fixing change with --proposal-id or --generation")
    result = _server_call(
        lambda client, instance_id: client.accept_fixed_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=proposal_id,
            accepted_changeset_digest=changeset_digest,
            accepted_generation=generation,
            attribution_refs=attribution_refs,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible curation accept-fixed",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_curation_action(result, str(result.item["status"]))


@curation_group.command("suppress")
@click.argument("item_id")
@_curation_ruling_options
@click.option(
    "--scope",
    type=click.Choice(("item", "lineage")),
    required=True,
    help="item: hide this item; lineage: also hide every successor its pattern opens.",
)
@click.option(
    "--until-generation",
    type=click.IntRange(min=0),
    help="The last generation the suppression hides through (default: until lifted).",
)
@change_control_options
@json_option
@handle_errors
def curation_suppress(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: tuple[str, ...],
    scope: str,
    until_generation: int | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Hide an item, or its lineage, without resolving it; detection keeps running."""

    result = _server_call(
        lambda client, instance_id: client.suppress_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=cast(Any, scope),
            until_generation=until_generation,
            attribution_refs=attribution_refs,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible curation suppress",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_curation_action(result, f"suppressed ({scope})")


@curation_group.command("unsuppress")
@click.argument("item_id")
@_curation_ruling_options
@click.option(
    "--suppression",
    "suppression_event_id",
    default=None,
    help="The suppression's event ID; needed only when the item carries more than one.",
)
@change_control_options
@json_option
@handle_errors
def curation_unsuppress(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: tuple[str, ...],
    suppression_event_id: str | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Lift a suppression recorded on an item, so what it hid is listed again."""

    result = _server_call(
        lambda client, instance_id: client.unsuppress_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            suppression_event_id=suppression_event_id,
            attribution_refs=attribution_refs,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible curation unsuppress",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_curation_action(result, "unsuppressed")


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
    for row in result.rows:
        click.echo(
            f"{row.rank_score}  {row.claim_identity['kind']}:{row.claim_identity['name']}  "
            f"stake={row.factors.stake} weakness={row.factors.weakness} "
            f"staleness={row.factors.staleness}"
        )
    if not result.rows:
        click.echo("No Claims in the visible audit scope.")
    if result.next_cursor is not None:
        click.echo(f"More: {result.next_cursor.cursor_digest}")


@playbill_group.command("since")
@click.argument("generation", type=click.IntRange(min=0))
@click.option("--max-rows", default=100, show_default=True, type=click.IntRange(1, 1000))
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
    for row in result.rows:
        click.echo(f"{row.generation}  {row.disposition}  {row.artifact_kind}  {row.member_path}")
    if result.next_cursor is not None:
        click.echo(f"More: {result.next_cursor.cursor_digest}")


def _orient_predicate_line(item: Mapping[str, Any]) -> str:
    kind = item["type"]
    if item.get("members"):
        kind = "enum[" + "|".join(str(member) for member in item["members"]) + "]"
    parts = [str(item["name"]), str(item["cardinality"]), kind]
    if item.get("live_claims") is not None:
        parts.append(f"claims={item['live_claims']}")
    if item.get("stale_after"):
        parts.append(f"stale_after={item['stale_after']}")
    if item.get("evidence") is not None:
        parts.append("evidence=" + (",".join(map(_public_name, item["evidence"])) or "(none)"))
    return "  ".join(parts)


def _render_orient(result: Mapping[str, Any]) -> str:
    lines = [
        f"Cruxible {result['instance']} generation={result['generation']} "
        f"at {result['coordinate']['git_oid'][:12]} accepted {result['accepted_at']}"
    ]
    if result.get("mirror_url"):
        lines.append(f"Ledger mirror: {result['mirror_url']}")
    floor = result.get("floor")
    if floor is not None:
        behind = floor["generations_behind"]
        lines.append(
            f"Floor: .cruxible/floor at {floor['at'][:12]}, "
            + (
                "current"
                if behind == 0
                else "generations behind unknown"
                if behind is None
                else f"{behind} generation(s) behind"
            )
            + ("" if behind == 0 else "; refresh: cruxible floor export --force")
        )
    you = result.get("you")
    if you is not None:
        refusal = you.get("authoring_refusal") or {}
        verdict = (
            "can author"
            if you["can_author"]
            else f"cannot author ({refusal.get('code')}): {refusal.get('detail')}"
        )
        lines.append(f"You: {you['actor'] or '(no actor)'}, {verdict}")
    kinds = result.get("kinds")
    if kinds is not None:
        lines.append(f"Kinds ({len(kinds)}):")
        for kind in kinds:
            shared = kind.get("evidence")
            lines.append(
                f"  {kind['kind']}  subjects={kind['subjects']}"
                + (f"  evidence={','.join(map(_public_name, shared))}" if shared else "")
            )
            lines.extend(f"    {_orient_predicate_line(item)}" for item in kind["predicates"])
    artifacts = result.get("artifacts")
    if artifacts is not None:
        lines.append(
            "Artifacts: " + " ".join(f"{name}={count}" for name, count in artifacts.items())
        )
    detail = result.get("kind_detail")
    if detail is not None:
        shared = detail.get("evidence")
        lines.append(
            f"Kind {detail['kind']}  subjects={detail['subjects']}"
            + (f"  evidence={','.join(map(_public_name, shared))}" if shared else "")
        )
        lines.extend(f"  {_orient_predicate_line(item)}" for item in detail["predicates"])
        if detail.get("incoming"):
            lines.append("Incoming (--follow-in): " + ", ".join(detail["incoming"]))
        if detail["sample_subject_ids"]:
            lines.append("Sample subjects: " + ", ".join(detail["sample_subject_ids"]))
    section = result.get("section")
    if section is not None and not result[section]:
        lines.append(f"(no {section.replace('_', ' ')})")
    if section in {
        "documents",
        "procedures",
        "runs",
        "lines",
        "captures",
        "capture_contracts",
        "predictions",
        "mandates",
    }:
        for row in result[section]:
            lines.append("  ".join(str(value) for value in row.values()))
    if section == "interfaces":
        for row in result["interfaces"]:
            providers = (
                ",".join(item["provider"] for item in row.get("providers", ())) or "(no provider)"
            )
            lines.append(f"{row['name']}  effect={row['effect']}  providers={providers}")
            if row.get("description"):
                lines.append(f"  {row['description']}")
            lines.append(f"  in:  {', '.join(row.get('input', ())) or '-'}")
            lines.append(f"  out: {', '.join(row.get('output', ())) or '-'}")
    if section == "subject_kinds":
        for row in result["subject_kinds"]:
            rule = (
                "minted"
                if row["rule"] == "minted"
                else f"keys: {', '.join(row.get('keys', ()))} ({row.get('uniqueness')})"
            )
            lines.append(f"{row['kind']}  {rule}")
    if section == "claim_types":
        for row in result["claim_types"]:
            lines.append(
                f"{row['predicate']}  kinds={','.join(row['subject_kinds'])}  "
                + _orient_predicate_line(row).split("  ", 1)[1]
            )
    queries = result.get("queries")
    if queries:
        if section is None:
            lines.append("Queries:")
        for query in queries:
            params = ", ".join(query["params"])
            description = f"  {query['description']}" if query.get("description") else ""
            lines.append(f"  {query['name']}({params}){description}")
    attention = result.get("attention")
    if attention is not None:
        lines.append(
            f"Attention: next={attention['next_items']} "
            f"open_proposals={attention['open_proposals']}"
        )
        lines.extend(f"  {line}" for line in attention["top"])
        lines.extend(f"  note: {line}" for line in attention.get("notes", ()))
        enablements = attention.get("enablements")
        if enablements is not None:
            lines.append(
                f"  Line enablements: running={enablements['running']} "
                f"stalled={enablements['stalled']} stopped={enablements['stopped']}"
            )
            lines.extend(f"    {line}" for line in enablements.get("needs_attention", ()))
    if result.get("next"):
        lines.append("Next:")
        lines.extend(f"  {line}" for line in result["next"])
    return "\n".join(lines)


@playbill_group.command("orient")
@click.option("--kind", default=None, help="Read one Subject kind in full.")
@click.option(
    "--section",
    type=click.Choice(list(get_args(contracts.OrientSection))),
    default=None,
    help="Page one artifact family instead of the map.",
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
    """Map accepted state: kinds, predicates, artifacts, attention and next commands."""

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
        ),
        command_name="cruxible orient",
    )
    workspace_root = containing_git_workspace_root(Path.cwd())
    if workspace_root is not None:
        result = workspace_floor_freshness(workspace_root, result)
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
    workspace_resolution = _local_git_workspace_root()
    _emit_git_workspace_note(workspace_resolution)
    workspace_root = workspace_resolution.workspace_root
    if workspace_root is None:
        raise click.UsageError("cruxible floor export must run inside one Git worktree")
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


def _resolved_coverage(
    observations: tuple[WorkingSourceObservation, ...],
    *,
    command_name: str,
) -> CoverageResultV3:
    result = _server_call(
        lambda client, instance_id: client.resolve_coverage(
            instance_id,
            observations=[item.model_dump(mode="json") for item in observations],
        ),
        command_name=command_name,
    )
    return CoverageResultV3.model_validate(result.result)


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
        "cards: the governed spans and a summary; manifest: the coverage manifest "
        "(epoch, health, completeness, scope)."
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
        resolution = _local_git_workspace_root()
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
    result = _resolved_coverage(observations, command_name="cruxible coverage resolve")
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
    rendered = (
        render_coverage_manifest(result) if view == "manifest" else render_coverage_result(result)
    )
    for line in rendered:
        click.echo(line)


__all__ = ["playbill_group"]
